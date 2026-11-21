"""Idempotent fixture for the gateway end-to-end tests, driving a real Alien dev platform.

Sets up (finding every object by name first and reusing it, since organizations cannot be
deleted):

* two managed client organizations, created inside the sponsored group by the sponsor's service
  account (a client-managed org-admin user, authenticated by its api-secret key), holding three
  client-managed users (alice and carol in client-x, bob in client-y), each with one api-key
  token minted by that same service account,
* a read grant on one dataset of a sponsor organization's private cluster,
* a sponsored group in the sponsor organization holding that grant and both client organizations.

Everything, including the consumption feed reads, runs as the sponsor service account: the
platform admin is not a member of a managed organization and cannot read its feed.

It then proves each minted key through the real MCP server, reads the consumption feeds (consumer
and sponsor side) as a positive control, and writes the gateway state (static mapping, the Redis, PostgreSQL and webhook seeds: see
``state_layout.py``), the key files and a non-secret ``fixture.json`` into the state directory.

Secrets: the sponsor key is read from a key file, never from argv, never printed. Key files are
written mode 600 atomically. Only token identifiers are printed.

The reusable read helpers (``find_consumption_events``, ``call_mcp_tool``, ``probe_key``) are
importable by the end-to-end harness.
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
from state_layout import (
    REAL_ROUTING,
    KeyedPersona,
    ensure_private_dir,
    write_atomic,
    write_layout,
)

DESCRIPTION = (
    "Idempotent fixture for the gateway end-to-end tests, driving a real Alien dev platform."
)
CONNECT_TIMEOUT_S = 10.0
READ_TIMEOUT_S = 30.0
BODY_EXCERPT_CHARS = 500
MCP_PROTOCOL_VERSION = "2025-11-25"
MCP_TOOL = "datacluster_get_dataset"
# The prefix of the shared fixture's object names. Another prefix sets up a separate chain (its
# own grant, group, organizations and users), for example to exercise creation again: nothing
# the fixture creates can be deleted once metered.
SHARED_NAME_PREFIX = "mcp-gateway-e2e"
_NAME_PREFIX = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")
FEED_DEADLINE_S = 60.0
MAX_FEED_PAGES = 20
FEED_SKEW = timedelta(seconds=60)
FORGED_JOB_ID = 999999999
MAX_JOB_LOOKUPS = 15


class FixtureError(Exception):
    """A fixture precondition or invariant failed."""


class AlienApiError(FixtureError):
    """A non-2xx answer (or a malformed envelope) from the Alien API."""

    def __init__(self, status: int, method: str, path: str, body_excerpt: str) -> None:
        super().__init__(f"{method} {path} -> HTTP {status}: {body_excerpt}")
        self.status = status
        self.method = method
        self.path = path
        self.body_excerpt = body_excerpt


@dataclass(frozen=True)
class Persona:
    """One persona of the fixture. ``client`` is the short name of its client organization (also
    what the IdP's organization claim carries); several personas may share one organization.
    ``email`` is its identity at the IdPs (what the gateway's mapping matches)."""

    name: str
    client: str
    email: str
    key_file: str


PERSONAS: tuple[Persona, ...] = (
    Persona("alice", "client-x", "alice@client-x.test", "managed-client-x-alice.key"),
    Persona("bob", "client-y", "bob@client-y.test", "managed-client-y-bob.key"),
    Persona("carol", "client-x", "carol@client-x.test", "managed-client-x-carol.key"),
)


@dataclass(frozen=True)
class Naming:
    """The names of the fixture's objects on the Alien platform, all derived from one prefix."""

    prefix: str

    def __post_init__(self) -> None:
        if _NAME_PREFIX.fullmatch(self.prefix) is None:
            raise FixtureError(
                f"name prefix {self.prefix!r}: 1 to 40 characters of a-z 0-9 -, "
                "starting with a letter or digit"
            )

    @property
    def grant(self) -> str:
        return f"{self.prefix}-dataset-read"

    @property
    def group(self) -> str:
        return f"{self.prefix}-sponsored"

    @property
    def token(self) -> str:
        return self.prefix

    def org_name(self, client: str) -> str:
        return f"{self.prefix}-managed-{client}"

    def alien_email(self, persona: Persona) -> str:
        """The client-managed user's address on Alien. Addresses are unique across the whole
        platform, so another prefix needs other addresses; the shared fixture keeps the ones
        its users were created with."""
        if self.prefix == SHARED_NAME_PREFIX:
            return f"{persona.name}@managed-{persona.client}.test"
        return f"{persona.name}@{self.prefix}.managed-{persona.client}.test"


@dataclass(frozen=True)
class ClientSpec:
    """One persona as a client-managed user in a managed client organization.

    ``name`` is the short name of the client organization; ``email`` the persona's identity at
    the IdPs; ``alien_email`` the client-managed user's own address on the Alien platform.
    """

    persona: str
    name: str
    email: str
    alien_email: str
    key_file: str
    org_name: str


def client_specs(naming: Naming) -> tuple[ClientSpec, ...]:
    return tuple(
        ClientSpec(
            persona=p.name,
            name=p.client,
            email=p.email,
            alien_email=naming.alien_email(p),
            key_file=p.key_file,
            org_name=naming.org_name(p.client),
        )
        for p in PERSONAS
    )


@dataclass(frozen=True)
class Config:
    sponsor_key_file: Path
    api_base: str
    mcp_url: str
    sponsor_org_id: int
    cluster_id: int
    dataset_id: int
    state_dir: Path
    rotate: bool
    probe_forged_headers: bool
    naming: Naming


@dataclass
class ClientState:
    spec: ClientSpec
    org_id: int
    user_id: int
    token_identifier: str
    abilities: list[str]
    minted: bool
    events: dict[str, list[int]] = field(default_factory=dict)


@dataclass
class Tally:
    """What this run created; a second run must leave it at zero."""

    created: list[str] = field(default_factory=list)

    def add(self, what: str) -> None:
        self.created.append(what)
        log(f"CREATED {what}")


def log(message: str) -> None:
    print(message, flush=True)


# --------------------------------------------------------------------------------------------
# Credentials and files
# --------------------------------------------------------------------------------------------


def read_key_file(path: Path) -> str | None:
    if not path.is_file():
        return None
    key = path.read_text(encoding="utf-8").strip()
    if not key.startswith("oat_"):
        raise FixtureError(f"{path} does not hold an oat_ token")
    return key


# --------------------------------------------------------------------------------------------
# Alien API client
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ApiResult:
    status: int
    data: Any
    meta: Any


class FixtureApi:
    """Thin httpx wrapper over the Alien envelope. Never logs headers."""

    def __init__(self, api_base: str, token: str) -> None:
        self._client = httpx.Client(
            base_url=api_base.rstrip("/"),
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            timeout=httpx.Timeout(READ_TIMEOUT_S, connect=CONNECT_TIMEOUT_S),
        )

    def close(self) -> None:
        self._client.close()

    def request(
        self,
        method: str,
        path: str,
        *,
        org_id: int | None = None,
        json_body: Mapping[str, Any] | None = None,
        params: Mapping[str, Any] | None = None,
        handled: Sequence[int] = (),
    ) -> ApiResult:
        """Call the API; raise ``AlienApiError`` on non-2xx unless the status is in ``handled``."""
        headers = {"x-organization-id": str(org_id)} if org_id is not None else {}
        response = self._client.request(
            method, path, headers=headers, json=json_body, params=params
        )
        if response.status_code in handled:
            return ApiResult(response.status_code, *_envelope_parts(response))
        if not 200 <= response.status_code < 300:
            raise AlienApiError(
                response.status_code, method, path, response.text[:BODY_EXCERPT_CHARS]
            )
        body = _json_body(response, method, path)
        if body.get("success") is not True:
            raise AlienApiError(
                response.status_code, method, path, response.text[:BODY_EXCERPT_CHARS]
            )
        return ApiResult(response.status_code, body.get("data"), body.get("meta"))

    def get(self, path: str, **kwargs: Any) -> Any:
        return self.request("GET", path, **kwargs).data

    def post(self, path: str, body: Mapping[str, Any] | None = None, **kwargs: Any) -> Any:
        return self.request("POST", path, json_body=body or {}, **kwargs).data

    def patch(self, path: str, body: Mapping[str, Any], **kwargs: Any) -> Any:
        return self.request("PATCH", path, json_body=body, **kwargs).data

    def delete(self, path: str, **kwargs: Any) -> Any:
        return self.request("DELETE", path, **kwargs).data


def _json_body(response: httpx.Response, method: str, path: str) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError as exc:
        raise AlienApiError(
            response.status_code, method, path, response.text[:BODY_EXCERPT_CHARS]
        ) from exc
    if not isinstance(body, dict):
        raise AlienApiError(response.status_code, method, path, response.text[:BODY_EXCERPT_CHARS])
    return body


def _envelope_parts(response: httpx.Response) -> tuple[Any, Any]:
    """Data and meta of a handled (possibly error) answer; the error object rides as data."""
    try:
        body = response.json()
    except ValueError:
        return response.text[:BODY_EXCERPT_CHARS], None
    if not isinstance(body, dict):
        return body, None
    if body.get("success") is False:
        return body.get("error"), None
    return body.get("data"), body.get("meta")


def error_name(result: ApiResult) -> str:
    """The ``error.name`` of a handled error answer."""
    if isinstance(result.data, dict):
        return str(result.data.get("name"))
    return ""


def require_list(data: Any, what: str) -> list[dict[str, Any]]:
    if not isinstance(data, list):
        raise FixtureError(f"{what}: expected a list, got {type(data).__name__}: {data!r:.200}")
    return data


def require_dict(data: Any, what: str) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise FixtureError(f"{what}: expected an object, got {type(data).__name__}: {data!r:.200}")
    return data


# --------------------------------------------------------------------------------------------
# Organizations, users, tokens
# --------------------------------------------------------------------------------------------


def verify_dataset(api: FixtureApi, cfg: Config) -> None:
    data = api.get(
        "/clusters/listDatasets", params={"clusterIds": cfg.cluster_id}, org_id=cfg.sponsor_org_id
    )
    results = require_dict(
        require_dict(data, "listDatasets").get("results"), "listDatasets results"
    )
    cluster = require_dict(results.get(str(cfg.cluster_id)), f"cluster {cfg.cluster_id} results")
    ids = [d.get("id") for d in require_list(cluster.get("datasets"), "cluster datasets")]
    if cfg.dataset_id not in ids:
        raise FixtureError(
            f"dataset {cfg.cluster_id}:{cfg.dataset_id} not in cluster {cfg.cluster_id} "
            f"(sponsor org {cfg.sponsor_org_id}); found ids: {ids[:20]}"
        )
    log(f"dataset {cfg.cluster_id}:{cfg.dataset_id} present")


def ensure_org(
    sponsor: FixtureApi, cfg: Config, group_id: int, spec: ClientSpec, tally: Tally
) -> dict[str, Any]:
    """Find the managed client org by name among the group's organizations, else create it
    through the group's managed-organization route (which also attaches it to the group)."""
    org = _find_group_org(sponsor, cfg, group_id, spec.org_name)
    if org is None:
        created = require_dict(
            sponsor.post(
                f"/groups/{group_id}/organizations/managed",
                {"name": spec.org_name, "slug": spec.org_name},
                org_id=cfg.sponsor_org_id,
            ),
            "create managed organization",
        )
        tally.add(f"managed organization {spec.org_name} (id {created['id']})")
        org = created
    if org.get("accountType") != "managed":
        raise FixtureError(
            f"organization {spec.org_name} (id {org['id']}) is {org.get('accountType')!r}, "
            "not managed"
        )
    return {"id": int(org["id"]), "name": spec.org_name}


def _find_group_org(
    sponsor: FixtureApi, cfg: Config, group_id: int, name: str
) -> dict[str, Any] | None:
    orgs = require_list(
        sponsor.get(f"/groups/{group_id}/organizations", org_id=cfg.sponsor_org_id),
        "group organizations",
    )
    matches = [o for o in orgs if o.get("name") == name]
    if len(matches) > 1:
        raise FixtureError(f"{len(matches)} organizations of group {group_id} are named {name!r}")
    return matches[0] if matches else None


def ensure_user(api: FixtureApi, org_id: int, spec: ClientSpec, tally: Tally) -> int:
    email = spec.alien_email
    users = require_list(api.get(f"/organizations/{org_id}/users"), "org users")
    for user in users:
        if str(user.get("email", "")).lower() == email:
            if user.get("userType") != "client-managed":
                raise FixtureError(f"user {email} exists but is {user.get('userType')!r}")
            return int(user["id"])
    created = api.post(
        f"/organizations/{org_id}/users",
        {
            "userType": "client-managed",
            "email": email,
            "description": "mcp-gateway end-to-end fixture user",
        },
    )
    user_id = int(require_dict(created, "create user")["id"])
    tally.add(f"client-managed user {email} (id {user_id}) in org {org_id}")
    return user_id


def list_named_tokens(
    api: FixtureApi, org_id: int, user_id: int, token_name: str
) -> list[dict[str, Any]]:
    tokens = require_list(api.get(f"/organizations/{org_id}/users/{user_id}/tokens"), "user tokens")
    return [t for t in tokens if str(t.get("name", "")).endswith(token_name)]


def probe_key(api_base: str, key: str) -> dict[str, Any] | None:
    """``GET /users/me`` with ``key``: the user object, or None when the key is rejected (401)."""
    api = FixtureApi(api_base, key)
    try:
        result = api.request("GET", "/users/me", handled=(401,))
    finally:
        api.close()
    if result.status == 401:
        return None
    return require_dict(result.data, "users/me")


def ensure_key(
    sponsor: FixtureApi, cfg: Config, org_id: int, user_id: int, spec: ClientSpec, tally: Tally
) -> tuple[str, bool]:
    """Return (token identifier, minted). Reuse a live key unless rotation is requested."""
    key_path = cfg.state_dir / spec.key_file
    named = list_named_tokens(sponsor, org_id, user_id, cfg.naming.token)
    existing_key = read_key_file(key_path)
    if existing_key is not None and named and not cfg.rotate:
        me = probe_key(cfg.api_base, existing_key)
        if me is not None and me.get("id") == user_id:
            if me.get("currentOrganizationId") != org_id:
                raise FixtureError(f"{spec.alien_email}: key's current org is not {org_id}")
            identifier = _identifier_of(existing_key, named)
            _revoke_others(sponsor, org_id, user_id, named, identifier)
            log(f"reused key for {spec.alien_email}: token {identifier}")
            return identifier, False
    for token in named:
        sponsor.delete(f"/organizations/{org_id}/users/{user_id}/tokens/{token['identifier']}")
        log(f"revoked stale token {token['identifier']} of {spec.alien_email}")
    minted = require_dict(
        sponsor.post(
            f"/organizations/{org_id}/users/{user_id}/tokens",
            {"name": cfg.naming.token, "scope": "api-key"},
        ),
        "mint token",
    )
    raw = minted.get("token")
    if not isinstance(raw, str) or not raw.startswith("oat_"):
        raise FixtureError(f"mint token for {spec.alien_email}: no oat_ token in the answer")
    write_atomic(key_path, raw + "\n", 0o600)
    tally.add(f"token {minted['identifier']} for {spec.alien_email}")
    return str(minted["identifier"]), True


def _identifier_of(key: str, named: list[dict[str, Any]]) -> str:
    """The list identifier of the token a key file holds (the key prefix is its base64 id)."""
    prefix = key.removeprefix("oat_").split(".", 1)[0]
    try:
        token_id = base64.urlsafe_b64decode(prefix + "=" * (-len(prefix) % 4)).decode("ascii")
    except (ValueError, UnicodeDecodeError) as exc:
        raise FixtureError("key file prefix is not a base64 token id") from exc
    for token in named:
        if str(token["identifier"]) == token_id:
            return token_id
    raise FixtureError(
        f"key file holds token {token_id}, not among the named tokens "
        f"{[t['identifier'] for t in named]}"
    )


def _revoke_others(
    sponsor: FixtureApi, org_id: int, user_id: int, named: list[dict[str, Any]], keep: str
) -> None:
    for token in named:
        if str(token["identifier"]) != keep:
            sponsor.delete(f"/organizations/{org_id}/users/{user_id}/tokens/{token['identifier']}")
            log(f"revoked duplicate token {token['identifier']}")


def verify_key(cfg: Config, key: str, user_id: int, org_id: int, email: str) -> list[str]:
    """Assert the key is the client-managed user in the client org; return its abilities."""
    me = probe_key(cfg.api_base, key)
    if me is None:
        raise FixtureError(f"{email}: freshly validated key rejected by /users/me")
    if me.get("id") != user_id or me.get("userType") != "client-managed":
        raise FixtureError(f"{email}: key resolves to user {me.get('id')} {me.get('userType')}")
    if me.get("currentOrganizationId") != org_id:
        raise FixtureError(
            f"{email}: key current org {me.get('currentOrganizationId')} != {org_id}"
        )
    current = require_dict(me.get("currentAccessToken"), "currentAccessToken")
    abilities = [str(a) for a in current.get("abilities", [])]
    log(f"key ok for {email}: user {user_id} org {org_id} abilities {abilities}")
    return abilities


# --------------------------------------------------------------------------------------------
# Sponsor side: grant and group
# --------------------------------------------------------------------------------------------


def ensure_grant(api: FixtureApi, cfg: Config, tally: Tally) -> int:
    org = cfg.sponsor_org_id
    name = cfg.naming.grant
    for grant in require_list(api.get("/grants", org_id=org), "grants"):
        if grant.get("name") == name:
            return int(grant["id"])
    result = api.request(
        "POST",
        "/grants",
        org_id=org,
        json_body={
            "name": name,
            "description": "mcp-gateway end-to-end fixture: read one dataset",
            "abilities": [
                {
                    "path": [
                        {"resource": "cluster", "resourceId": cfg.cluster_id},
                        {"resource": "dataset", "resourceId": cfg.dataset_id},
                    ],
                    "accessType": "read",
                }
            ],
        },
        handled=(409,),
    )
    if result.status == 409:
        raise FixtureError(f"grant {name} answered 409 ({error_name(result)}) but not listed")
    grant_id = int(require_dict(result.data, "create grant")["id"])
    tally.add(f"grant {name} (id {grant_id})")
    return grant_id


def ensure_group(api: FixtureApi, cfg: Config, tally: Tally) -> dict[str, Any]:
    org = cfg.sponsor_org_id
    name = cfg.naming.group
    group = _find_group(api, org, name)
    if group is not None:
        return group
    data = require_dict(
        api.post(
            "/groups",
            {"name": name, "description": "mcp-gateway end-to-end fixture group"},
            org_id=org,
        ),
        "create group",
    )
    if data.get("status") != "created":
        raise FixtureError(f"create group {name}: status {data.get('status')!r}: {data!r:.300}")
    group = require_dict(data.get("group"), "created group")
    tally.add(f"group {name} (id {group['id']})")
    return group


def _find_group(api: FixtureApi, org: int, name: str) -> dict[str, Any] | None:
    groups = require_list(api.get("/groups", org_id=org), "groups")
    matches = [g for g in groups if g.get("name") == name]
    return matches[0] if matches else None


def _attached_ids(api: FixtureApi, org: int, group_id: int, kind: str) -> set[int]:
    items = require_list(api.get(f"/groups/{group_id}/{kind}", org_id=org), f"group {kind}")
    return {int(item["id"]) for item in items}


def sponsor_group(
    api: FixtureApi, cfg: Config, group: dict[str, Any], grant_id: int, t: Tally
) -> int:
    """Attach the grant, then make the group sponsored: the backend sponsors only a group whose
    grants name cluster data, so the grant goes first."""
    org = cfg.sponsor_org_id
    group_id = int(group["id"])
    if grant_id not in _attached_ids(api, org, group_id, "grants"):
        api.post(f"/groups/{group_id}/grants", {"grantIds": [grant_id]}, org_id=org)
        t.add(f"grant {grant_id} attached to group {group_id}")
    current = require_dict(api.get(f"/groups/{group_id}", org_id=org), "group")
    if current.get("isSponsored") is not True:
        api.patch(f"/groups/{group_id}", {"isSponsored": True}, org_id=org)
        t.add(f"group {group_id} made sponsored")
    return group_id


def attach_orgs(api: FixtureApi, cfg: Config, group_id: int, org_ids: list[int], t: Tally) -> None:
    """Attach the client organizations the group does not hold yet (the managed-organization
    route attaches what it creates; an organization created otherwise is attached here)."""
    org = cfg.sponsor_org_id
    attached = _attached_ids(api, org, group_id, "organizations")
    missing = [o for o in org_ids if o not in attached]
    if missing:
        api.post(f"/groups/{group_id}/organizations", {"organizationIds": missing}, org_id=org)
        t.add(f"organizations {missing} attached to group {group_id}")


def prepare_group(
    sponsor: FixtureApi, cfg: Config, specs: Sequence[ClientSpec], tally: Tally
) -> tuple[int, int, list[dict[str, Any]]]:
    """The sponsor side, in the order the backend accepts: grant, group, grant attached, group
    sponsored, then the managed organizations (refused with 409 GROUP_NOT_SPONSORED in a group
    that is not sponsored yet), then any organization still unattached. Every step finds what
    exists first, so a run that stopped half-way is completed by the next one.

    Returns the group id, the grant id and one organization per spec, in order.
    """
    grant_id = ensure_grant(sponsor, cfg, tally)
    group = ensure_group(sponsor, cfg, tally)
    group_id = sponsor_group(sponsor, cfg, group, grant_id, tally)
    orgs = [ensure_org(sponsor, cfg, group_id, spec, tally) for spec in specs]
    attach_orgs(sponsor, cfg, group_id, list(dict.fromkeys(int(o["id"]) for o in orgs)), tally)
    return group_id, grant_id, orgs


# --------------------------------------------------------------------------------------------
# MCP access proof
# --------------------------------------------------------------------------------------------


def _mcp_post(
    client: httpx.Client, url: str, key: str, payload: Mapping[str, Any], extra: Mapping[str, str]
) -> httpx.Response:
    headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {key}",
        **extra,
    }
    return client.post(url, json=payload, headers=headers)


def _mcp_message(response: httpx.Response) -> dict[str, Any]:
    """The JSON-RPC message of a JSON or single-event SSE answer."""
    text = response.text
    if response.headers.get("content-type", "").startswith("text/event-stream"):
        datas = [ln[5:].strip() for ln in text.splitlines() if ln.startswith("data:")]
        if not datas:
            raise FixtureError(f"MCP SSE answer without data: {text[:BODY_EXCERPT_CHARS]}")
        text = datas[-1]
    try:
        message = json.loads(text)
    except ValueError as exc:
        raise FixtureError(f"MCP answer is not JSON: {text[:BODY_EXCERPT_CHARS]}") from exc
    return require_dict(message, "MCP message")


@dataclass(frozen=True)
class McpOutcome:
    status: int
    body_excerpt: str
    is_error: bool | None


def call_mcp_tool(
    mcp_url: str,
    key: str,
    tool: str,
    arguments: Mapping[str, Any],
    *,
    call_headers: Mapping[str, str] | None = None,
    check_arguments: bool = True,
) -> McpOutcome:
    """initialize, notifications/initialized, (tools/list), then one tools/call.

    ``call_headers`` ride only on the final ``tools/call``. The tools/call HTTP status is never
    raised on, so forged-header probes can record any answer; setup requests are strict.
    """
    timeout = httpx.Timeout(READ_TIMEOUT_S, connect=CONNECT_TIMEOUT_S)
    with httpx.Client(timeout=timeout) as client:
        init = _mcp_post(
            client,
            mcp_url,
            key,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": MCP_PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "mcp-gateway-dev-fixture", "version": "0"},
                },
            },
            {},
        )
        if init.status_code != 200:
            raise FixtureError(f"MCP initialize -> HTTP {init.status_code}: {init.text[:500]}")
        _mcp_message(init)
        session: dict[str, str] = {}
        if sid := init.headers.get("mcp-session-id"):
            session["mcp-session-id"] = sid
        session["mcp-protocol-version"] = MCP_PROTOCOL_VERSION
        note = _mcp_post(
            client,
            mcp_url,
            key,
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            session,
        )
        if note.status_code not in (200, 202):
            raise FixtureError(f"MCP initialized -> HTTP {note.status_code}: {note.text[:500]}")
        if check_arguments:
            _check_tool_arguments(client, mcp_url, key, session, tool, arguments)
        response = _mcp_post(
            client,
            mcp_url,
            key,
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": tool, "arguments": dict(arguments)},
            },
            {**session, **(call_headers or {})},
        )
    excerpt = response.text[:BODY_EXCERPT_CHARS]
    if response.status_code != 200:
        return McpOutcome(response.status_code, excerpt, None)
    message = _mcp_message(response)
    result = message.get("result")
    if not isinstance(result, dict):
        return McpOutcome(response.status_code, excerpt, True)
    return McpOutcome(response.status_code, excerpt, bool(result.get("isError")))


def _check_tool_arguments(
    client: httpx.Client,
    url: str,
    key: str,
    session: Mapping[str, str],
    tool: str,
    arguments: Mapping[str, Any],
) -> None:
    response = _mcp_post(
        client, url, key, {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}, session
    )
    if response.status_code != 200:
        raise FixtureError(f"MCP tools/list -> HTTP {response.status_code}: {response.text[:500]}")
    tools = require_dict(_mcp_message(response).get("result"), "tools/list result").get("tools")
    by_name = {t["name"]: t for t in require_list(tools, "tools")}
    if tool not in by_name:
        raise FixtureError(f"MCP tool {tool} not offered; offered: {sorted(by_name)}")
    properties = by_name[tool].get("inputSchema", {}).get("properties", {})
    unknown = [a for a in arguments if a not in properties]
    if unknown:
        raise FixtureError(f"tool {tool} has no argument(s) {unknown}; has {sorted(properties)}")


def prove_mcp_access(cfg: Config, key: str, email: str) -> None:
    outcome = call_mcp_tool(
        cfg.mcp_url, key, MCP_TOOL, {"dataset_id": f"{cfg.cluster_id}:{cfg.dataset_id}"}
    )
    if outcome.status != 200 or outcome.is_error is not False:
        raise FixtureError(
            f"MCP {MCP_TOOL} as {email}: HTTP {outcome.status} isError={outcome.is_error}: "
            f"{outcome.body_excerpt}"
        )
    log(f"MCP {MCP_TOOL} ok as {email}")


# --------------------------------------------------------------------------------------------
# Consumption feeds
# --------------------------------------------------------------------------------------------


def read_consumption_events(
    api: FixtureApi, org_id: int, *, start: datetime, first: int = 100
) -> list[dict[str, Any]]:
    """The org's MCP consumption feed from ``start`` on (ascending; every page is read).

    The caller must be able to read the org's feed (a member admin, or the sponsor of a managed
    org). Paging is bounded by ``MAX_FEED_PAGES``.
    """
    events: list[dict[str, Any]] = []
    cursor: str | None = None
    for _ in range(MAX_FEED_PAGES):
        params: dict[str, Any] = {
            "first": first,
            "sourceType": "mcp",
            "start": start.astimezone(UTC).isoformat(),
        }
        if cursor is not None:
            params["after"] = cursor
        data = require_dict(
            api.get(f"/organizations/{org_id}/consumption/events", org_id=org_id, params=params),
            "consumption events",
        )
        events.extend(require_list(data.get("events"), "consumption events list"))
        page = require_dict(data.get("pageInfo"), "consumption pageInfo")
        if not page.get("hasNextPage"):
            return events
        cursor = str(page["endCursor"])
    raise FixtureError(f"org {org_id} feed has more than {MAX_FEED_PAGES} pages since {start}")


def called_at(event: Mapping[str, Any]) -> datetime:
    return datetime.fromisoformat(str(event["calledAt"]))


def find_consumption_events(
    api: FixtureApi,
    org_id: int,
    predicate: Callable[[dict[str, Any]], bool],
    *,
    since: datetime,
    deadline_s: float = FEED_DEADLINE_S,
    sleep: Callable[[float], None] = time.sleep,
) -> list[dict[str, Any]]:
    """Poll the org feed until events newer than ``since`` match ``predicate`` (bounded).

    Metering is asynchronous, so this retries with backoff until ``deadline_s`` and returns the
    matches (possibly empty: the caller decides whether that is a failure).
    """
    deadline = time.monotonic() + deadline_s
    delay = 1.0
    while True:
        matches = [
            e for e in read_consumption_events(api, org_id, start=since - FEED_SKEW) if predicate(e)
        ]
        if matches or time.monotonic() >= deadline:
            return matches
        sleep(min(delay, max(0.0, deadline - time.monotonic())))
        delay = min(delay * 2, 8.0)


def consumer_predicate(user_id: int, org_id: int) -> Callable[[dict[str, Any]], bool]:
    def match(event: dict[str, Any]) -> bool:
        user = event.get("user") or {}
        return user.get("id") == user_id and event.get("organizationId") == org_id

    return match


def sponsor_predicate(group_id: int, beneficiary_org_id: int) -> Callable[[dict[str, Any]], bool]:
    def match(event: dict[str, Any]) -> bool:
        sponsored = event.get("sponsored") or {}
        return (
            sponsored.get("groupId") == group_id
            and sponsored.get("organizationId") == beneficiary_org_id
        )

    return match


def read_back_feeds(
    api: FixtureApi, cfg: Config, group_id: int, client: ClientState, since: datetime
) -> None:
    """Positive control: both feeds must show the proof call; record the event ids."""
    consumer = find_consumption_events(
        api, client.org_id, consumer_predicate(client.user_id, client.org_id), since=since
    )
    sponsor = find_consumption_events(
        api, cfg.sponsor_org_id, sponsor_predicate(group_id, client.org_id), since=since
    )
    client.events = {
        "consumer": [int(e["id"]) for e in consumer],
        "sponsor": [int(e["id"]) for e in sponsor],
    }
    for e in consumer:
        log(
            f"{client.spec.name} consumer feed event {e['id']}: user {e['user']['id']} "
            f"org {e['organizationId']} jobId {e.get('jobId')} sponsored {e.get('sponsored')}"
        )
    for e in sponsor:
        log(
            f"{client.spec.name} sponsor feed event {e['id']}: user {(e.get('user') or {}).get('id')} "
            f"jobId {e.get('jobId')} sponsored {e.get('sponsored')}"
        )
    if not consumer or not sponsor:
        raise FixtureError(
            f"{client.spec.name}: feed read-back incomplete (consumer {len(consumer)}, "
            f"sponsor {len(sponsor)}) within {FEED_DEADLINE_S:.0f}s"
        )


# --------------------------------------------------------------------------------------------
# Forged header probe
# --------------------------------------------------------------------------------------------


def find_existing_job_id(api: FixtureApi, org_id: int) -> int | None:
    """Read-only: the id of some existing job, via the org's workflows' latest jobs."""
    workflows = require_list(api.get("/workflows", org_id=org_id), "workflows")
    for workflow in workflows[:MAX_JOB_LOOKUPS]:
        result = api.request(
            "GET", f"/workflows/{workflow['id']}/latest-job", org_id=org_id, handled=(404,)
        )
        if result.status == 200:
            return int(require_dict(result.data, "latest job")["id"])
    return None


def probe_forged_headers(
    sponsor: FixtureApi,
    cfg: Config,
    key: str,
    consumer_org_id: int,
    consumer_user_id: int,
    foreign_org_id: int,
) -> list[dict[str, Any]]:
    """Call the MCP directly with forged x-job-id / x-organization-id; record what happens."""
    args = {"dataset_id": f"{cfg.cluster_id}:{cfg.dataset_id}"}
    cases: list[tuple[str, dict[str, str]]] = [
        ("nonexistent job id", {"x-job-id": str(FORGED_JOB_ID)}),
        ("foreign org id", {"x-organization-id": str(foreign_org_id)}),
        (
            "nonexistent job id + foreign org id",
            {"x-job-id": str(FORGED_JOB_ID), "x-organization-id": str(foreign_org_id)},
        ),
    ]
    real_job = find_existing_job_id(sponsor, cfg.sponsor_org_id)
    if real_job is not None:
        cases.append((f"existing job id {real_job}", {"x-job-id": str(real_job)}))
    outcomes: list[dict[str, Any]] = []
    for label, headers in cases:
        since = datetime.now(UTC)
        outcome = call_mcp_tool(
            cfg.mcp_url, key, MCP_TOOL, args, call_headers=headers, check_arguments=False
        )
        record: dict[str, Any] = {
            "case": label,
            "headers": sorted(headers),
            "http_status": outcome.status,
            "is_error": outcome.is_error,
            "body_excerpt": outcome.body_excerpt,
        }
        if outcome.status == 200 and outcome.is_error is False:
            events = find_consumption_events(
                sponsor,
                consumer_org_id,
                lambda e: (e.get("user") or {}).get("id") == consumer_user_id,
                since=since,
                deadline_s=FEED_DEADLINE_S,
            )
            newest = sorted(events, key=called_at)[-1:] if events else []
            record["consumer_events"] = [
                {"id": e["id"], "jobId": e.get("jobId"), "organizationId": e.get("organizationId")}
                for e in newest
            ]
        outcomes.append(record)
        log(f"forged-header probe [{label}]: {json.dumps(record, default=str)}")
    return outcomes


# --------------------------------------------------------------------------------------------
# Output files
# --------------------------------------------------------------------------------------------


def write_outputs(cfg: Config, group_id: int, grant_id: int, clients: list[ClientState]) -> None:
    facts = {
        "api_base": cfg.api_base,
        "mcp_url": cfg.mcp_url,
        "sponsor_org_id": cfg.sponsor_org_id,
        "cluster_id": cfg.cluster_id,
        "dataset_id": cfg.dataset_id,
        "grant_id": grant_id,
        "group_id": group_id,
        "clients": [
            {
                "name": c.spec.name,
                "org_id": c.org_id,
                "org_name": c.spec.org_name,
                "user_id": c.user_id,
                "email": c.spec.email,
                "alien_email": c.spec.alien_email,
                "token_identifier": c.token_identifier,
                "abilities": c.abilities,
                "key_file": c.spec.key_file,
            }
            for c in clients
        ],
    }
    personas = [
        KeyedPersona(c.spec.persona, c.spec.email, c.spec.name, c.spec.key_file) for c in clients
    ]
    write_layout(cfg.state_dir, personas, REAL_ROUTING)
    write_atomic(cfg.state_dir / "fixture.json", json.dumps(facts, indent=2) + "\n", 0o600)


# --------------------------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------------------------


def run(cfg: Config) -> int:
    sponsor_key = read_key_file(cfg.sponsor_key_file)
    if sponsor_key is None:
        raise FixtureError(f"sponsor key file {cfg.sponsor_key_file} does not exist")
    ensure_private_dir(cfg.state_dir)
    sponsor = FixtureApi(cfg.api_base, sponsor_key)
    tally = Tally()
    try:
        sponsor_me = require_dict(sponsor.get("/users/me"), "sponsor users/me")
        log(f"sponsor service account: user {sponsor_me['id']} {sponsor_me.get('userType')}")
        clients, group_id, grant_id = build_chain(sponsor, cfg, tally)
        started = datetime.now(UTC)
        for client in clients:
            key = _require_key(cfg, client.spec)
            prove_mcp_access(cfg, key, client.spec.alien_email)
        for client in clients:
            read_back_feeds(sponsor, cfg, group_id, client, started)
        if cfg.probe_forged_headers:
            run_probe(sponsor, cfg, clients)
        write_outputs(cfg, group_id, grant_id, clients)
    finally:
        sponsor.close()
    print_summary(cfg, tally, group_id, grant_id, clients)
    return 0


def build_chain(
    sponsor: FixtureApi, cfg: Config, tally: Tally
) -> tuple[list[ClientState], int, int]:
    verify_dataset(sponsor, cfg)
    specs = client_specs(cfg.naming)
    group_id, grant_id, orgs = prepare_group(sponsor, cfg, specs, tally)
    clients: list[ClientState] = []
    for spec, org in zip(specs, orgs, strict=True):
        user_id = ensure_user(sponsor, int(org["id"]), spec, tally)
        identifier, minted = ensure_key(sponsor, cfg, int(org["id"]), user_id, spec, tally)
        key = _require_key(cfg, spec)
        abilities = verify_key(cfg, key, user_id, int(org["id"]), spec.alien_email)
        clients.append(ClientState(spec, int(org["id"]), user_id, identifier, abilities, minted))
    return clients, group_id, grant_id


def _require_key(cfg: Config, spec: ClientSpec) -> str:
    key = read_key_file(cfg.state_dir / spec.key_file)
    if key is None:
        raise FixtureError(f"key file {spec.key_file} missing after ensure_key")
    return key


def run_probe(sponsor: FixtureApi, cfg: Config, clients: list[ClientState]) -> None:
    alice, bob = clients[0], clients[1]
    outcomes = probe_forged_headers(
        sponsor, cfg, _require_key(cfg, alice.spec), alice.org_id, alice.user_id, bob.org_id
    )
    write_atomic(
        cfg.state_dir / "forged-probe.json", json.dumps(outcomes, indent=2, default=str), 0o600
    )


def print_summary(
    cfg: Config, tally: Tally, group_id: int, grant_id: int, clients: list[ClientState]
) -> None:
    log("---- summary (ids only) ----")
    log(f"sponsor org {cfg.sponsor_org_id}, dataset {cfg.cluster_id}:{cfg.dataset_id}")
    log(f"grant {grant_id}, group {group_id}")
    for c in clients:
        log(
            f"{c.spec.name}: org {c.org_id}, user {c.user_id}, token {c.token_identifier}, "
            f"minted={c.minted}, abilities {c.abilities}, events {c.events}"
        )
    log(f"objects created this run: {len(tally.created)}")


def parse_args(argv: Sequence[str]) -> Config:
    parser = argparse.ArgumentParser(description=DESCRIPTION)
    parser.add_argument(
        "--sponsor-key-file",
        type=Path,
        required=True,
        help="file holding the api-secret key of the sponsor's service account",
    )
    parser.add_argument("--api-base", required=True)
    parser.add_argument("--mcp-url", required=True)
    parser.add_argument("--sponsor-org-id", type=int, required=True)
    parser.add_argument("--cluster-id", type=int, required=True)
    parser.add_argument("--dataset-id", type=int, required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--rotate", action="store_true", help="mint new keys, revoke the old")
    parser.add_argument(
        "--name-prefix",
        default=SHARED_NAME_PREFIX,
        help=(
            "prefix of every object name (grant, group, organizations, token; the Alien e-mails "
            f"too, except for the shared fixture's {SHARED_NAME_PREFIX!r}): another prefix "
            "sets up a separate chain"
        ),
    )
    parser.add_argument(
        "--probe-forged-headers",
        action="store_true",
        help="also probe whether the MCP passes forged x-job-id / x-organization-id through",
    )
    args = parser.parse_args(argv)
    try:
        naming = Naming(args.name_prefix)
    except FixtureError as exc:
        parser.error(str(exc))
    return Config(
        sponsor_key_file=args.sponsor_key_file,
        api_base=args.api_base,
        mcp_url=args.mcp_url,
        sponsor_org_id=args.sponsor_org_id,
        cluster_id=args.cluster_id,
        dataset_id=args.dataset_id,
        state_dir=args.state_dir,
        rotate=args.rotate,
        probe_forged_headers=args.probe_forged_headers,
        naming=naming,
    )


def main(argv: Sequence[str] | None = None) -> int:
    cfg = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        return run(cfg)
    except (FixtureError, httpx.HTTPError, OSError) as exc:
        log(f"FIXTURE FAILED: {type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
