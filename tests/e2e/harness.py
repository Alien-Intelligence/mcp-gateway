"""Building blocks of the end-to-end harness: settings, providers, the real-client OAuth
journey, MCP sessions through the official SDK, and reads of the Alien consumption feeds.

Nothing here is mocked. Secrets (user password, tokens, keys) come from the environment, the
gitignored ``.env`` or the state directory and are never printed, logged or written to the
evidence file.
"""

import asyncio
import json
import os
import re
import time
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

import httpx
import httpx2
import pytest
from dev_fixture import (
    FEED_SKEW,
    FixtureApi,
    call_mcp_tool,
    find_consumption_events,
    probe_key,
    read_consumption_events,
    sponsor_predicate,
    write_atomic,
)
from mcp import Client, MCPError
from mcp.client.streamable_http import streamable_http_client

from e2e import oauth_flow

REPO_ROOT = Path(__file__).resolve().parents[2]
CLIENT_REDIRECT_URI = "http://127.0.0.1:53682/callback"
CLIENT_NAME = "mcp-gateway-e2e-client"
HTTP_TIMEOUT = httpx.Timeout(30.0, connect=5.0)
MCP_PROTOCOL_VERSION = "2025-11-25"
MCP_ACCEPT = "application/json, text/event-stream"
TOOL = "datacluster_get_dataset"
TOOL_ACTION = "get-dataset"
CLOCK_SKEW_SECONDS = 5
EXPIRY_MARGIN_SECONDS = 3
FEED_DEADLINE_SECONDS = 90.0
REVOCATION_DEADLINE_SECONDS = 60.0
PASSWORD_VARIABLE = "E2E_USER_PASSWORD"
REQUIRED_ENV = (
    "E2E_STATE_DIR",
    "E2E_SPONSOR_KEY_FILE",
    "E2E_API_BASE",
    "E2E_UPSTREAM_URL",
    "E2E_RUN_DIR",
    "E2E_EXISTING_JOB_ID",
)
NOMAP_EMAIL = "nomap@client-z.test"
REVOKED_TOKEN_NAME = "mcp-gateway-e2e-revoked"
KEY_FILE_MODE = 0o600


# ---------------------------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------------------------


def _read_dotenv_value(path: Path, name: str) -> str | None:
    if not path.is_file():
        return None
    for line in path.read_text(encoding="utf-8").splitlines():
        key, separator, value = line.strip().partition("=")
        if separator and key.strip() == name:
            return value.strip().strip("'\"")
    return None


@dataclass(frozen=True)
class Persona:
    name: str
    email: str
    org_id: int | None
    user_id: int | None
    key_file: str | None


@dataclass(frozen=True)
class Settings:
    state_dir: Path
    sponsor_key_file: Path
    api_base: str
    upstream_url: str
    run_dir: Path
    existing_job_id: int
    user_password: str = field(repr=False)
    sponsor_org_id: int
    group_id: int
    cluster_id: int
    dataset_id: int
    personas: dict[str, Persona]

    @property
    def dataset_ref(self) -> str:
        return f"{self.cluster_id}:{self.dataset_id}"

    def sponsor_api(self) -> FixtureApi:
        """The sponsor's service account: it reads the feeds of the managed client orgs and
        runs their users' keys (the platform admin is not a member of a managed org)."""
        key = self.sponsor_key_file.read_text(encoding="utf-8").strip()
        if not key:
            raise ValueError("the sponsor key file is empty")
        return FixtureApi(self.api_base, key)

    def key_path(self, persona: Persona) -> Path:
        if persona.key_file is None:
            raise ValueError(f"persona {persona.name} has no key file")
        return self.state_dir / persona.key_file

    def read_key(self, persona: Persona) -> str:
        key = self.key_path(persona).read_text(encoding="utf-8").strip()
        if not key:
            raise ValueError(f"key file of {persona.name} is empty")
        return key


def load_settings() -> Settings:
    """Read the environment and the fixture; every missing name is reported at once."""
    missing = [name for name in REQUIRED_ENV if not os.environ.get(name)]
    password = os.environ.get(PASSWORD_VARIABLE) or _read_dotenv_value(
        REPO_ROOT / ".env", PASSWORD_VARIABLE
    )
    if not password:
        missing.append(f"{PASSWORD_VARIABLE} (environment or .env)")
    if missing:
        pytest.fail("e2e harness is not configured, missing: " + ", ".join(missing), pytrace=False)
    assert password is not None
    state_dir = Path(os.environ["E2E_STATE_DIR"])
    fixture = json.loads((state_dir / "fixture.json").read_text(encoding="utf-8"))
    personas = {
        client["email"].split("@", 1)[0]: Persona(
            name=client["email"].split("@", 1)[0],
            email=client["email"],
            org_id=int(client["org_id"]),
            user_id=int(client["user_id"]),
            key_file=client["key_file"],
        )
        for client in fixture["clients"]
    }
    personas["nomap"] = Persona("nomap", NOMAP_EMAIL, None, None, None)
    return Settings(
        state_dir=state_dir,
        sponsor_key_file=Path(os.environ["E2E_SPONSOR_KEY_FILE"]),
        api_base=os.environ["E2E_API_BASE"].rstrip("/"),
        upstream_url=os.environ["E2E_UPSTREAM_URL"],
        run_dir=Path(os.environ["E2E_RUN_DIR"]),
        existing_job_id=int(os.environ["E2E_EXISTING_JOB_ID"]),
        user_password=password,
        sponsor_org_id=int(fixture["sponsor_org_id"]),
        group_id=int(fixture["group_id"]),
        cluster_id=int(fixture["cluster_id"]),
        dataset_id=int(fixture["dataset_id"]),
        personas=personas,
    )


# ---------------------------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Provider:
    name: str
    gateway_url: str
    issuer: str
    # Keyword arguments of obtain_grant that make the issued token lack the gateway audience.
    wrong_audience: dict[str, str]


PROVIDERS = (
    Provider(
        name="keycloak",
        gateway_url="http://localhost:18000/mcp",
        issuer="http://localhost:18080/realms/alien-gateway-dev",
        wrong_audience={"scope": "openid"},
    ),
    Provider(
        name="hydra",
        gateway_url="http://localhost:18001/mcp",
        issuer="http://localhost:14444",
        wrong_audience={"resource": "http://localhost:18999/mcp"},
    ),
)


# Keycloak, resolver chain [store_redis, store_sql, webhook] behind a cache, scope mcp-write
# required. Not in PROVIDERS: the per-provider tests run on the two static gateways.
CHAIN = Provider(
    name="chain",
    gateway_url="http://localhost:18002/mcp",
    issuer="http://localhost:18080/realms/alien-gateway-dev",
    wrong_audience={"scope": "openid"},
)
# The replica pair behind nginx (Keycloak, store_redis resolver, state in Redis).
REPLICAS = Provider(
    name="replicas",
    gateway_url="http://localhost:18004/mcp",
    issuer="http://localhost:18080/realms/alien-gateway-dev",
    wrong_audience={"scope": "openid"},
)
# Which store of the chain resolves each persona (see scripts/state_layout.py).
CHAIN_STORE = {"alice": "redis", "bob": "postgres", "carol": "webhook"}


# ---------------------------------------------------------------------------------------------
# Raw MCP requests (status codes and headers matter, so no SDK here)
# ---------------------------------------------------------------------------------------------


def initialize_payload() -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "mcp-gateway-e2e", "version": "0"},
        },
    }


async def raw_initialize(url: str, token: str | None) -> httpx.Response:
    headers = {"Accept": MCP_ACCEPT, "Content-Type": "application/json"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        return await client.post(url, json=initialize_payload(), headers=headers)


_CHALLENGE_PARAM = re.compile(r'(\w+)="([^"]*)"')


def parse_challenge(header: str) -> dict[str, str]:
    """The auth-params of a ``Bearer`` WWW-Authenticate challenge."""
    scheme, _, rest = header.partition(" ")
    assert scheme == "Bearer", f"not a Bearer challenge: {header!r}"
    return dict(_CHALLENGE_PARAM.findall(rest))


def rpc_error(response: httpx.Response) -> dict[str, Any]:
    body = response.json()
    assert isinstance(body, dict) and isinstance(body.get("error"), dict), (
        f"no JSON-RPC error in body: {response.text[:300]}"
    )
    return body["error"]


def tools_call_payload(name: str, arguments: dict[str, Any], request_id: int = 2) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }


async def raw_post(
    client: httpx.AsyncClient, url: str, token: str, payload: dict[str, Any]
) -> httpx.Response:
    """One JSON-RPC message posted without a handshake (the dev upstream is stateless)."""
    headers = {
        "Accept": MCP_ACCEPT,
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
        "MCP-Protocol-Version": MCP_PROTOCOL_VERSION,
    }
    return await client.post(url, json=payload, headers=headers)


def rpc_message(response: httpx.Response) -> dict[str, Any]:
    """The JSON-RPC message of a reply, whether it came as JSON or as one SSE event."""
    if response.headers.get("content-type", "").startswith("text/event-stream"):
        data = [
            line.removeprefix("data:").strip()
            for line in response.text.splitlines()
            if line.startswith("data:")
        ]
        assert data, f"no data event in stream: {response.text[:300]}"
        body = json.loads(data[-1])
    else:
        body = response.json()
    assert isinstance(body, dict), body
    return body


RETRY_AFTER_META_KEY = "club.alien/retryAfterSeconds"
RATE_LIMITED = -32014


def is_rate_limited_tool_result(response: httpx.Response) -> bool:
    """The refusal shape of a rate-limited ``tools/call`` (HTTP 200, isError, Retry-After)."""
    if response.status_code != 200 or "retry-after" not in response.headers:
        return False
    result = rpc_message(response).get("result")
    return isinstance(result, dict) and result.get("isError") is True


def assert_rate_limited_tool_result(response: httpx.Response) -> int:
    """Assert the full shape and return the advertised retry delay in seconds."""
    assert response.status_code == 200, (response.status_code, response.text[:300])
    result = rpc_message(response)["result"]
    assert result["isError"] is True, result
    retry_after = result["_meta"][RETRY_AFTER_META_KEY]
    assert isinstance(retry_after, int) and retry_after >= 1, result
    assert response.headers["retry-after"] == str(retry_after)
    return retry_after


# ---------------------------------------------------------------------------------------------
# The real-client OAuth journey
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Grant:
    provider: Provider
    persona: Persona
    token: str = field(repr=False)
    claims: dict[str, Any]
    challenge: dict[str, str]

    @property
    def token_evidence(self) -> dict[str, Any]:
        return {
            "iss": self.claims.get("iss"),
            "aud": self.claims.get("aud"),
            "exp": self.claims.get("exp"),
        }


class HasUserPassword(Protocol):
    """What the OAuth journey needs from a harness's settings: the test users' password."""

    @property
    def user_password(self) -> str: ...


async def obtain_grant(
    settings: HasUserPassword,
    provider: Provider,
    persona: Persona,
    *,
    scope: str | None = None,
    resource: str | None = None,
) -> Grant:
    """Steps 1 to 6: challenge, PRM, AS discovery, anonymous DCR, authorize (PKCE S256 +
    resource + scope through the real login form), code exchange.

    ``scope`` / ``resource`` override what the challenge and the PRM advertise.
    """
    async with oauth_flow.new_client() as client:
        unauthenticated = await client.post(
            provider.gateway_url,
            json=initialize_payload(),
            headers={"Accept": MCP_ACCEPT, "Content-Type": "application/json"},
        )
        assert unauthenticated.status_code == 401, unauthenticated.text[:300]
        challenge = parse_challenge(unauthenticated.headers["www-authenticate"])
        assert challenge["resource_metadata"] and challenge["scope"], challenge

        prm = await oauth_flow.fetch_protected_resource_metadata(
            client, challenge["resource_metadata"]
        )
        assert prm["resource"] == provider.gateway_url
        assert prm["authorization_servers"][0] == provider.issuer

        metadata = await oauth_flow.discover_authorization_server(
            client, prm["authorization_servers"][0]
        )
        registration = await oauth_flow.register_client(
            client, metadata["registration_endpoint"], CLIENT_REDIRECT_URI, CLIENT_NAME
        )
        requested_resource = resource if resource is not None else prm["resource"]
        authorization = await oauth_flow.authorize(
            client,
            authorization_endpoint=metadata["authorization_endpoint"],
            client_id=registration["client_id"],
            redirect_uri=CLIENT_REDIRECT_URI,
            resource=requested_resource,
            scope=scope if scope is not None else challenge["scope"],
            username=persona.email,
            password=settings.user_password,
            issuer=metadata["issuer"],
        )
        token_response = await oauth_flow.exchange_code(
            client,
            token_endpoint=metadata["token_endpoint"],
            client_id=registration["client_id"],
            authorization=authorization,
            resource=requested_resource,
        )
    token = token_response["access_token"]
    return Grant(provider, persona, token, oauth_flow.decode_jwt_payload(token), challenge)


# ---------------------------------------------------------------------------------------------
# MCP traffic through the official SDK
# ---------------------------------------------------------------------------------------------


@asynccontextmanager
async def mcp_session(url: str, token: str) -> AsyncIterator[tuple[Client, httpx2.AsyncClient]]:
    """An initialized SDK client over streamable HTTP; the HTTP client is exposed so a test can
    add headers once the handshake is done."""
    async with httpx2.AsyncClient(
        headers={"Authorization": f"Bearer {token}"}, timeout=httpx2.Timeout(30.0, connect=5.0)
    ) as http:
        transport = streamable_http_client(url, http_client=http)
        async with Client(transport, mode="legacy") as client:
            yield client, http


# ---------------------------------------------------------------------------------------------
# Alien consumption feeds
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class FeedBaseline:
    since: datetime
    consumer_known: frozenset[str]
    sponsor_known: frozenset[str]


def _known_ids(api: FixtureApi, org_id: int, since: datetime) -> frozenset[str]:
    events = read_consumption_events(api, org_id, start=since - FEED_SKEW)
    return frozenset(str(e["id"]) for e in events)


def _tool_event_of(user_id: int, org_id: int, known: frozenset[str]) -> Callable[..., bool]:
    def match(event: dict[str, Any]) -> bool:
        user = event.get("user") or {}
        return (
            str(event["id"]) not in known
            and user.get("id") == user_id
            and event.get("organizationId") == org_id
            and event.get("action") == TOOL_ACTION
        )

    return match


async def take_feed_baseline(settings: Settings, persona: Persona) -> FeedBaseline:
    """Remember the events already in both feeds, just before the call under test."""
    assert persona.org_id is not None
    since = datetime.now(UTC)

    def read() -> FeedBaseline:
        api = settings.sponsor_api()
        try:
            return FeedBaseline(
                since,
                _known_ids(api, persona.org_id or 0, since),
                _known_ids(api, settings.sponsor_org_id, since),
            )
        finally:
            api.close()

    return await asyncio.to_thread(read)


async def read_consumer_event(
    settings: Settings, persona: Persona, baseline: FeedBaseline
) -> dict[str, Any]:
    """The single new consumer-feed event of the persona (metering lags: bounded polling)."""
    assert persona.org_id is not None and persona.user_id is not None

    def read() -> list[dict[str, Any]]:
        api = settings.sponsor_api()
        try:
            return find_consumption_events(
                api,
                persona.org_id or 0,
                _tool_event_of(persona.user_id or 0, persona.org_id or 0, baseline.consumer_known),
                since=baseline.since,
                deadline_s=FEED_DEADLINE_SECONDS,
            )
        finally:
            api.close()

    events = await asyncio.to_thread(read)
    assert len(events) == 1, f"expected one new consumer event, got {len(events)}: {events}"
    return events[0]


def _persona_events_since(
    settings: Settings, persona: Persona, baseline: FeedBaseline
) -> list[dict[str, Any]]:
    """Every consumer-feed event of the persona that is not in the baseline (any action)."""
    assert persona.org_id is not None
    api = settings.sponsor_api()
    try:
        events = read_consumption_events(api, persona.org_id, start=baseline.since - FEED_SKEW)
    finally:
        api.close()
    return [
        e
        for e in events
        if str(e["id"]) not in baseline.consumer_known
        and (e.get("user") or {}).get("id") == persona.user_id
    ]


async def new_consumer_events(
    settings: Settings, persona: Persona, baseline: FeedBaseline
) -> list[dict[str, Any]]:
    """One read of the feed, no waiting: all new events of the persona, whatever the action."""
    return await asyncio.to_thread(_persona_events_since, settings, persona, baseline)


async def wait_for_new_consumer_events(
    settings: Settings,
    persona: Persona,
    baseline: FeedBaseline,
    *,
    at_least: int,
    deadline_s: float = FEED_DEADLINE_SECONDS,
) -> tuple[list[dict[str, Any]], float]:
    """Poll (bounded, backing off) until ``at_least`` new events show; returns them and the
    seconds the metering took to show up."""
    started = time.monotonic()
    delay = 1.0
    while True:
        events = await new_consumer_events(settings, persona, baseline)
        elapsed = time.monotonic() - started
        if len(events) >= at_least or elapsed >= deadline_s:
            return events, elapsed
        await asyncio.sleep(delay)
        delay = min(delay * 2, 8.0)


async def read_sponsor_event(
    settings: Settings, persona: Persona, baseline: FeedBaseline, event_id: str
) -> dict[str, Any]:
    """The sponsor-feed event with the same id as the consumer event."""
    assert persona.org_id is not None
    beneficiary = sponsor_predicate(settings.group_id, persona.org_id)

    def read() -> list[dict[str, Any]]:
        api = settings.sponsor_api()
        try:
            return find_consumption_events(
                api,
                settings.sponsor_org_id,
                lambda e: str(e["id"]) == event_id and beneficiary(e),
                since=baseline.since,
                deadline_s=FEED_DEADLINE_SECONDS,
            )
        finally:
            api.close()

    events = await asyncio.to_thread(read)
    assert len(events) == 1, f"expected the sponsor event {event_id}, got {events}"
    return events[0]


# ---------------------------------------------------------------------------------------------
# Alien keys
# ---------------------------------------------------------------------------------------------


def mint_revoked_key(settings: Settings, persona: Persona) -> str:
    """Mint a fresh api-key token for the persona's user, then revoke it: a key the platform
    knew and now rejects."""
    assert persona.org_id is not None and persona.user_id is not None
    base = f"/organizations/{persona.org_id}/users/{persona.user_id}/tokens"
    api = settings.sponsor_api()
    try:
        minted = api.post(base, {"name": REVOKED_TOKEN_NAME, "scope": "api-key"})
        key, identifier = minted["token"], minted["identifier"]
        if not isinstance(key, str) or not key.startswith("oat_"):
            raise ValueError("the platform did not return an oat_ token")
        api.delete(f"{base}/{identifier}")
    finally:
        api.close()
    return key


@contextmanager
def temporarily_replaced_key(
    settings: Settings, persona: Persona, replacement: str
) -> Iterator[None]:
    """Point the persona's key file at ``replacement``; the original is always restored
    (atomic replace, mode 600) and proven live again with ``GET /users/me``."""
    path = settings.key_path(persona)
    original = path.read_text(encoding="utf-8")
    write_atomic(path, replacement + "\n", KEY_FILE_MODE)
    try:
        yield
    finally:
        write_atomic(path, original, KEY_FILE_MODE)
        me = probe_key(settings.api_base, original.strip())
        if me is None or me.get("id") != persona.user_id:
            raise RuntimeError(f"restored key of {persona.name} is not accepted by the platform")


# ---------------------------------------------------------------------------------------------
# Errors raised by the SDK
# ---------------------------------------------------------------------------------------------


def leaf_exceptions(error: BaseException) -> list[BaseException]:
    """The exceptions inside (possibly nested) exception groups, as the SDK's task groups raise."""
    if isinstance(error, BaseExceptionGroup):
        return [leaf for inner in error.exceptions for leaf in leaf_exceptions(inner)]
    return [error]


def sdk_error_codes(error: BaseException) -> list[int]:
    """JSON-RPC error codes carried by the SDK's ``MCPError`` leaves of ``error``."""
    return [leaf.code for leaf in leaf_exceptions(error) if isinstance(leaf, MCPError)]


async def sdk_handshake_error_codes(url: str, token: str) -> list[int]:
    """JSON-RPC error codes the SDK reports when the handshake is refused with a JSON-RPC error."""
    try:
        async with mcp_session(url, token):
            pass
    except BaseExceptionGroup as group:
        unexpected = [e for e in leaf_exceptions(group) if not isinstance(e, MCPError)]
        if unexpected:
            raise
        return sdk_error_codes(group)
    raise AssertionError("the handshake was expected to be refused")


def audiences(claims: dict[str, Any]) -> list[str]:
    """The ``aud`` claim as a list (a JWT may carry a string or an array)."""
    aud = claims.get("aud")
    if aud is None:
        return []
    return [aud] if isinstance(aud, str) else list(aud)


# ---------------------------------------------------------------------------------------------
# Direct-to-upstream positive control (forged job header)
# ---------------------------------------------------------------------------------------------


class DirectJobControl:
    """The same tools/call sent straight to the upstream with a real key and a foreign ``x-job-id``.

    Since the backend refuses a job the caller does not own, the MCP answers ``isError`` with a
    validation failure: that proves the header reaches the backend when nothing strips it. The
    upstream is called once per session; every provider's test reads the same recorded outcome.
    """

    REFUSAL_TEXT = "Validation failure"

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._outcome: dict[str, Any] | None = None

    async def outcome(self) -> dict[str, Any]:
        if self._outcome is None:
            self._outcome = await self._run()
        return self._outcome

    async def _run(self) -> dict[str, Any]:
        settings = self._settings
        alice = settings.personas["alice"]
        call = await asyncio.to_thread(
            call_mcp_tool,
            settings.upstream_url,
            settings.read_key(alice),
            TOOL,
            {"dataset_id": settings.dataset_ref},
            call_headers={"x-job-id": str(settings.existing_job_id)},
            check_arguments=False,
        )
        assert call.status == 200 and call.is_error is True, call
        assert self.REFUSAL_TEXT in call.body_excerpt, call
        return {
            "http_status": call.status,
            "is_error": call.is_error,
            "refusal": self.REFUSAL_TEXT,
            "foreign_job_id": settings.existing_job_id,
        }
