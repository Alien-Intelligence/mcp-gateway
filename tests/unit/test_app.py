import asyncio
import gzip
import json
import logging
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from pydantic import SecretStr
from starlette.applications import Starlette

from mcp_gateway.app import DENIED_MESSAGE, create_app
from mcp_gateway.identity import Identity
from mcp_gateway.proxy import UpstreamStreamAbortedError
from mcp_gateway.resolver import Credential, Denied, ResolverError, StaticResolver

from .conftest import (
    FAKE_API_KEY,
    ISSUER,
    PUBLIC_URL,
    UPSTREAM_URL,
    FakeIdp,
    FakeUpstream,
    TrackedStream,
    base_config_dict,
    config_from,
    mint,
)

PRM_URL = "https://gw.example.com/.well-known/oauth-protected-resource/mcp"
RPC_BODY = {
    "jsonrpc": "2.0",
    "id": 7,
    "method": "tools/call",
    "params": {"name": "search", "arguments": {"q": "classified-text"}},
}


class SpyResolver:
    """Wraps a real resolver, recording invalidations."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.invalidated: list[Identity] = []

    async def resolve(self, identity: Identity) -> Credential | Denied:
        return await self.inner.resolve(identity)

    async def invalidate(self, identity: Identity) -> None:
        self.invalidated.append(identity)
        await self.inner.invalidate(identity)

    async def aclose(self) -> None:
        await self.inner.aclose()


class FixedResolver:
    def __init__(self, outcome: Credential | Denied | Exception) -> None:
        self.outcome = outcome

    async def resolve(self, identity: Identity) -> Credential | Denied:
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome

    async def invalidate(self, identity: Identity) -> None:
        return None

    async def aclose(self) -> None:
        return None


def build_app(
    mapping_dir: Path,
    transport: httpx.MockTransport,
    *,
    resolver: Any = None,
    **overrides: Any,
) -> Starlette:
    data = base_config_dict(str(mapping_dir / "mapping.json"))
    for dotted, value in overrides.items():
        node = data
        *parents, leaf = dotted.split("__")
        for part in parents:
            node = node[part]
        node[leaf] = value
    return create_app(config_from(data), resolver=resolver, http_transport=transport)


def client_for(app: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://gw.example.com"
    )


@pytest.fixture
def token(rsa_key: rsa.RSAPrivateKey) -> str:
    return mint(rsa_key)


@pytest.fixture
def app(mapping_dir: Path, transport: httpx.MockTransport) -> Starlette:
    return build_app(mapping_dir, transport)


@pytest.fixture
async def client(app: Starlette) -> AsyncIterator[httpx.AsyncClient]:
    async with client_for(app) as http:
        yield http


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def open_session(
    http: httpx.AsyncClient, upstream: FakeUpstream, token: str, session_id: str = "sess-1"
) -> None:
    """Initialize through the gateway so the upstream's session id is bound to this token."""
    original = upstream.responder
    upstream.responder = lambda _r: httpx.Response(
        200,
        headers={"Mcp-Session-Id": session_id},
        json={"jsonrpc": "2.0", "id": 1, "result": {}},
    )
    initialize = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
    response = await http.post("/mcp", json=initialize, headers=bearer(token))
    assert response.headers["mcp-session-id"] == session_id
    upstream.responder = original
    upstream.requests.clear()


def audit_lines(caplog: pytest.LogCaptureFixture) -> list[dict[str, Any]]:
    return [json.loads(r.getMessage()) for r in caplog.records if r.name == "mcp_gateway.audit"]


# --- discovery and health -------------------------------------------------------------


async def test_protected_resource_metadata_is_served_for_the_public_path_only(
    client: httpx.AsyncClient,
) -> None:
    response = await client.get("/.well-known/oauth-protected-resource/mcp")
    assert response.status_code == 200
    assert response.json() == {
        "resource": PUBLIC_URL,
        "authorization_servers": [ISSUER],
        "scopes_supported": ["openid", "mcp-gateway"],
        "bearer_methods_supported": ["header"],
    }


async def test_root_protected_resource_metadata_is_not_served(
    client: httpx.AsyncClient,
) -> None:
    # RFC 9728 section 3.3: the root document would describe the origin, not the MCP resource.
    for path in ("/.well-known/oauth-protected-resource", "/.well-known/oauth-protected-resource/"):
        assert (await client.get(path)).status_code == 404


async def test_healthz_needs_no_auth(client: httpx.AsyncClient) -> None:
    response = await client.get("/healthz")
    assert (response.status_code, response.json()) == (200, {"status": "ok"})


async def test_other_methods_and_paths_are_not_routed(
    client: httpx.AsyncClient, token: str
) -> None:
    assert (await client.put("/mcp", headers=bearer(token))).status_code == 405
    assert (await client.post("/elsewhere", headers=bearer(token))).status_code == 404


# --- authentication challenges --------------------------------------------------------


async def test_no_token_gets_the_exact_401_challenge(client: httpx.AsyncClient) -> None:
    response = await client.post("/mcp", json=RPC_BODY)
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == (
        f'Bearer resource_metadata="{PRM_URL}", scope="openid mcp-gateway"'
    )
    assert response.json()["error"] == "invalid_token"


async def test_invalid_token_challenge_carries_error_attributes(
    client: httpx.AsyncClient, other_rsa_key: rsa.RSAPrivateKey
) -> None:
    response = await client.post("/mcp", json=RPC_BODY, headers=bearer(mint(other_rsa_key)))
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == (
        f'Bearer resource_metadata="{PRM_URL}", scope="openid mcp-gateway", '
        'error="invalid_token", error_description="token signature is invalid"'
    )
    assert response.json() == {
        "error": "invalid_token",
        "error_description": "token signature is invalid",
    }


async def test_wrong_audience_is_401(client: httpx.AsyncClient, rsa_key: rsa.RSAPrivateKey) -> None:
    token = mint(rsa_key, overrides={"aud": "https://other.example.com/mcp"})
    response = await client.post("/mcp", json=RPC_BODY, headers=bearer(token))
    assert response.status_code == 401


async def test_non_bearer_scheme_is_treated_as_no_token(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/mcp", json=RPC_BODY, headers={"Authorization": "Basic Zm9vOmJhcg=="}
    )
    assert response.status_code == 401
    assert "error=" not in response.headers["www-authenticate"]


async def test_token_without_subject_is_401(
    client: httpx.AsyncClient, rsa_key: rsa.RSAPrivateKey
) -> None:
    response = await client.post(
        "/mcp", json=RPC_BODY, headers=bearer(mint(rsa_key, drop=("sub",)))
    )
    assert response.status_code == 401
    assert "invalid_token" in response.headers["www-authenticate"]


async def test_insufficient_scope_is_403_with_challenge(
    mapping_dir: Path,
    transport: httpx.MockTransport,
    rsa_key: rsa.RSAPrivateKey,
    upstream: FakeUpstream,
) -> None:
    app = build_app(mapping_dir, transport, auth__required_scopes=["mcp-gateway"])
    async with client_for(app) as http:
        response = await http.post("/mcp", json=RPC_BODY, headers=bearer(mint(rsa_key)))
    assert response.status_code == 403
    challenge = response.headers["www-authenticate"]
    assert 'error="insufficient_scope"' in challenge
    assert 'scope="mcp-gateway"' in challenge
    assert upstream.requests == []


async def test_idp_down_is_503_with_retry_after_never_401(
    client: httpx.AsyncClient, idp: FakeIdp, token: str, upstream: FakeUpstream
) -> None:
    idp.down = True
    response = await client.post("/mcp", json=RPC_BODY, headers=bearer(token))
    assert response.status_code == 503
    assert response.headers["retry-after"] == "5"
    assert "www-authenticate" not in response.headers
    assert upstream.requests == []


# --- resolver outcomes ----------------------------------------------------------------


async def test_no_mapping_is_403_jsonrpc_without_challenge(
    client: httpx.AsyncClient, rsa_key: rsa.RSAPrivateKey, upstream: FakeUpstream
) -> None:
    token = mint(rsa_key, overrides={"email": "stranger@example.com", "org": "nobody"})
    response = await client.post("/mcp", json=RPC_BODY, headers=bearer(token))
    assert response.status_code == 403
    assert "www-authenticate" not in response.headers
    assert response.json() == {
        "jsonrpc": "2.0",
        "id": 7,
        "error": {"code": -32010, "message": DENIED_MESSAGE},
    }
    assert upstream.requests == []


async def test_denial_for_a_notification_has_null_id(
    client: httpx.AsyncClient, rsa_key: rsa.RSAPrivateKey
) -> None:
    token = mint(rsa_key, overrides={"email": "stranger@example.com"})
    body = {"jsonrpc": "2.0", "method": "notifications/initialized"}
    response = await client.post("/mcp", json=body, headers=bearer(token))
    assert response.json()["id"] is None


async def test_ambiguous_mapping_uses_its_own_code(
    mapping_dir: Path, transport: httpx.MockTransport, token: str
) -> None:
    app = build_app(mapping_dir, transport, resolver=FixedResolver(Denied("ambiguous_mapping")))
    async with client_for(app) as http:
        response = await http.post("/mcp", json=RPC_BODY, headers=bearer(token))
    assert response.status_code == 403
    assert response.json()["error"]["code"] == -32011


async def test_unknown_denial_reason_still_403_without_challenge(
    mapping_dir: Path, transport: httpx.MockTransport, token: str
) -> None:
    app = build_app(mapping_dir, transport, resolver=FixedResolver(Denied("quota_exceeded")))
    async with client_for(app) as http:
        response = await http.post("/mcp", json=RPC_BODY, headers=bearer(token))
    assert response.status_code == 403
    assert response.json()["error"]["code"] == -32012
    assert "www-authenticate" not in response.headers


async def test_resolver_failure_is_a_generic_500(
    mapping_dir: Path, transport: httpx.MockTransport, token: str, upstream: FakeUpstream
) -> None:
    secret_detail = "api key file unreadable for entry 3 /run/secret/path"
    app = build_app(mapping_dir, transport, resolver=FixedResolver(ResolverError(secret_detail)))
    async with client_for(app) as http:
        response = await http.post("/mcp", json=RPC_BODY, headers=bearer(token))
    assert response.status_code == 500
    assert response.json()["error"]["code"] == -32005
    assert "entry 3" not in response.text
    assert "/run/secret" not in response.text
    assert upstream.requests == []


async def test_resolver_naming_an_unknown_upstream_is_a_500(
    mapping_dir: Path, transport: httpx.MockTransport, token: str
) -> None:
    credential = Credential(api_key=SecretStr("k"), upstream="missing", labels={})
    app = build_app(mapping_dir, transport, resolver=FixedResolver(credential))
    async with client_for(app) as http:
        response = await http.post("/mcp", json=RPC_BODY, headers=bearer(token))
    assert response.status_code == 500


# --- proxying -------------------------------------------------------------------------


async def test_proxied_request_carries_only_allowed_headers_and_the_injected_key(
    client: httpx.AsyncClient, token: str, upstream: FakeUpstream
) -> None:
    forged = {
        "X-Job-Id": "forged-job",
        "X-Organization-Id": "forged-org",
        "X-Mcp-Config-Id": "forged-config",
        "X-Oauth-Access-Token": "forged-token",
        "X-Mcp-Session-Id": "forged-session",
        "X-Request-Id": "forged-request",
        "X-Source-Type": "forged-source",
        "X-Forwarded-For": "6.6.6.6",
        "Cookie": "sid=forged",
        "X-Unlisted": "nope",
    }
    await open_session(client, upstream, token)
    response = await client.post(
        "/mcp",
        json=RPC_BODY,
        headers={
            **bearer(token),
            **forged,
            "Accept": "application/json, text/event-stream",
            "Mcp-Session-Id": "sess-1",
            "Mcp-Protocol-Version": "2025-06-18",
            "User-Agent": "client/1.0",
        },
    )
    assert response.status_code == 200
    (sent,) = upstream.requests
    assert str(sent.url) == UPSTREAM_URL
    assert sent.method == "POST"
    assert sent.headers["authorization"] == f"Bearer {FAKE_API_KEY}"
    assert sent.headers["mcp-session-id"] == "sess-1"
    assert sent.headers["mcp-protocol-version"] == "2025-06-18"
    assert sent.headers["accept"] == "application/json, text/event-stream"
    assert sent.headers["user-agent"] == "client/1.0"
    for name in forged:
        assert name.lower() not in sent.headers
    assert token not in json.dumps(dict(sent.headers))
    assert json.loads(sent.content) == RPC_BODY


async def test_get_and_delete_are_proxied(
    client: httpx.AsyncClient, token: str, upstream: FakeUpstream
) -> None:
    await open_session(client, upstream, token, "s")
    await client.get("/mcp", headers={**bearer(token), "Accept": "text/event-stream"})
    await client.delete("/mcp", headers={**bearer(token), "Mcp-Session-Id": "s"})
    assert [r.method for r in upstream.requests] == ["GET", "DELETE"]
    assert all(r.headers["authorization"] == f"Bearer {FAKE_API_KEY}" for r in upstream.requests)


async def test_client_query_string_is_not_forwarded(
    client: httpx.AsyncClient, token: str, upstream: FakeUpstream
) -> None:
    await client.post("/mcp?access_token=leak&x=1", json=RPC_BODY, headers=bearer(token))
    assert str(upstream.requests[0].url) == UPSTREAM_URL


async def test_non_json_body_is_forwarded_untouched(
    client: httpx.AsyncClient,
    token: str,
    upstream: FakeUpstream,
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.INFO, logger="mcp_gateway.audit"):
        await client.post(
            "/mcp",
            content=b"\x00not json\xff",
            headers={**bearer(token), "Content-Type": "text/plain"},
        )
    assert upstream.requests[0].content == b"\x00not json\xff"
    assert audit_lines(caplog)[0]["rpc_method"] is None


async def test_upstream_response_passes_through_and_is_filtered(
    client: httpx.AsyncClient, token: str, upstream: FakeUpstream
) -> None:
    upstream.responder = lambda _r: httpx.Response(
        201,
        content=b'{"jsonrpc":"2.0","id":7,"result":{"ok":true}}',
        headers={
            "Content-Type": "application/json",
            "Mcp-Session-Id": "sess-9",
            "Cache-Control": "no-store",
            "Set-Cookie": "sid=upstream",
            "WWW-Authenticate": 'Bearer realm="upstream"',
            "Keep-Alive": "timeout=5",
            "X-Upstream-Extra": "kept",
        },
    )
    response = await client.post("/mcp", json=RPC_BODY, headers=bearer(token))
    assert response.status_code == 201
    assert response.content == b'{"jsonrpc":"2.0","id":7,"result":{"ok":true}}'
    assert response.headers["mcp-session-id"] == "sess-9"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-accel-buffering"] == "no"
    assert response.headers["x-upstream-extra"] == "kept"
    for name in ("set-cookie", "www-authenticate", "keep-alive"):
        assert name not in response.headers


async def test_upstream_error_statuses_other_than_401_402_pass_through(
    client: httpx.AsyncClient, token: str, upstream: FakeUpstream
) -> None:
    upstream.responder = lambda _r: httpx.Response(429, json={"detail": "slow down"})
    response = await client.post("/mcp", json=RPC_BODY, headers=bearer(token))
    assert response.status_code == 429
    assert response.json() == {"detail": "slow down"}


async def test_event_stream_is_relayed_chunk_by_chunk_and_closed(
    client: httpx.AsyncClient, token: str, upstream: FakeUpstream
) -> None:
    stream = TrackedStream([b"event: message\n", b'data: {"a":1}\n\n', b'data: {"b":2}\n\n'])
    upstream.responder = lambda _r: httpx.Response(
        200, headers={"Content-Type": "text/event-stream"}, stream=stream
    )
    response = await client.post("/mcp", json=RPC_BODY, headers=bearer(token))
    assert response.content == b'event: message\ndata: {"a":1}\n\ndata: {"b":2}\n\n'
    assert response.headers["content-type"] == "text/event-stream"
    assert stream.closed


async def test_upstream_content_encoding_is_not_decoded(
    client: httpx.AsyncClient, token: str, upstream: FakeUpstream
) -> None:
    payload = gzip.compress(b'{"jsonrpc":"2.0","id":7,"result":{}}')
    upstream.responder = lambda _r: httpx.Response(
        200,
        content=payload,
        headers={"Content-Encoding": "gzip", "Content-Type": "application/json"},
    )
    # httpx would transparently decode on the client side; read the raw bytes instead.
    response = await client.post(
        "/mcp", json=RPC_BODY, headers={**bearer(token), "Accept-Encoding": "gzip"}
    )
    assert response.headers["content-encoding"] == "gzip"
    assert response.json()["id"] == 7
    assert upstream.requests[0].headers["accept-encoding"] == "identity"


async def test_midstream_failure_after_the_reply_is_aborted_not_ended_cleanly(
    app: Starlette,
    token: str,
    upstream: FakeUpstream,
    caplog: pytest.LogCaptureFixture,
) -> None:
    reply = b'data: {"jsonrpc":"2.0","id":7,"result":{}}\n\n'
    stream = TrackedStream([reply], fail_after=httpx.ReadError("boom"))
    upstream.responder = lambda _r: httpx.Response(
        200, headers={"Content-Type": "text/event-stream"}, stream=stream
    )
    with caplog.at_level(logging.INFO):
        async with client_for(app) as http:
            with pytest.raises(UpstreamStreamAbortedError):
                await http.post("/mcp", json=RPC_BODY, headers=bearer(token))
    assert stream.closed
    (line,) = audit_lines(caplog)
    assert line["outcome"] == "upstream_stream_failed"
    assert line["reason"] == "ReadError"


async def test_client_disconnect_aborts_the_upstream_request(
    app: Starlette, token: str, upstream: FakeUpstream, caplog: pytest.LogCaptureFixture
) -> None:
    stream = TrackedStream([b"data: first\n\n"], hang=True)
    upstream.responder = lambda _r: httpx.Response(
        200, headers={"Content-Type": "text/event-stream"}, stream=stream
    )
    body = json.dumps(RPC_BODY).encode()
    disconnect = asyncio.Event()
    delivered = False

    async def receive() -> dict[str, Any]:
        nonlocal delivered
        if not delivered:
            delivered = True
            return {"type": "http.request", "body": body, "more_body": False}
        await disconnect.wait()
        return {"type": "http.disconnect"}

    sent: list[dict[str, Any]] = []

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)
        if message["type"] == "http.response.body" and message["body"]:
            disconnect.set()  # the client goes away after the first event

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.4"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "https",
        "path": "/mcp",
        "raw_path": b"/mcp",
        "query_string": b"",
        "headers": [
            (b"host", b"gw.example.com"),
            (b"authorization", f"Bearer {token}".encode()),
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode()),
        ],
        "server": ("gw.example.com", 443),
        "client": ("203.0.113.9", 1234),
    }
    with caplog.at_level(logging.INFO, logger="mcp_gateway.audit"):
        await asyncio.wait_for(app(scope, receive, send), timeout=5)
    assert stream.closed, "the upstream response must be closed when the client disconnects"
    assert [m for m in sent if m["type"] == "http.response.body" and m["body"]]
    assert audit_lines(caplog)[0]["outcome"] == "client_disconnected"


class HangingResolver:
    """Never answers; records being cancelled."""

    def __init__(self) -> None:
        self.cancelled = False

    async def resolve(self, identity: Identity) -> Credential | Denied:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        raise AssertionError("unreachable")

    async def invalidate(self, identity: Identity) -> None:
        return None

    async def aclose(self) -> None:
        return None


async def test_client_leaving_during_resolution_cancels_it(
    mapping_dir: Path,
    transport: httpx.MockTransport,
    upstream: FakeUpstream,
    token: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    resolver = HangingResolver()
    app = build_app(mapping_dir, transport, resolver=resolver)
    body = json.dumps(RPC_BODY).encode()
    body_sent = False

    async def receive() -> dict[str, Any]:
        nonlocal body_sent
        if not body_sent:
            body_sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        await asyncio.sleep(0.05)  # the client gives up while the lookup is running
        return {"type": "http.disconnect"}

    sent: list[dict[str, Any]] = []

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.4"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "https",
        "path": "/mcp",
        "raw_path": b"/mcp",
        "query_string": b"",
        "headers": [
            (b"host", b"gw.example.com"),
            (b"authorization", f"Bearer {token}".encode()),
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode()),
        ],
        "server": ("gw.example.com", 443),
        "client": ("203.0.113.9", 1234),
    }
    with caplog.at_level(logging.INFO, logger="mcp_gateway.audit"):
        await asyncio.wait_for(app(scope, receive, send), timeout=5)
    assert resolver.cancelled
    assert upstream.requests == []
    (line,) = audit_lines(caplog)
    assert (line["status"], line["outcome"]) == (499, "client_disconnected")


# --- upstream auth failures -----------------------------------------------------------


@pytest.mark.parametrize(("upstream_status", "code"), [(401, -32001), (402, -32002)])
async def test_upstream_auth_failure_becomes_jsonrpc_error_with_http_200(
    mapping_dir: Path,
    transport: httpx.MockTransport,
    token: str,
    upstream: FakeUpstream,
    upstream_status: int,
    code: int,
) -> None:
    spy = SpyResolver(StaticResolver(mapping_dir / "mapping.json", ["primary"], email_matching=True))
    upstream.responder = lambda _r: httpx.Response(
        upstream_status,
        json={"detail": "internal upstream detail"},
        headers={"WWW-Authenticate": 'Bearer realm="internal.example"'},
    )
    app = build_app(mapping_dir, transport, resolver=spy)
    async with client_for(app) as http:
        response = await http.post("/mcp", json=RPC_BODY, headers=bearer(token))
    assert response.status_code == 200
    assert "www-authenticate" not in response.headers
    body = response.json()
    assert body["jsonrpc"] == "2.0"
    assert body["id"] == 7
    assert body["error"]["code"] == code
    assert body["error"]["data"] == {"upstream_status": upstream_status}
    assert "internal" not in response.text
    assert [i.subject for i in spy.invalidated] == ["user-1"]


async def test_upstream_auth_failure_without_a_request_id_is_http_502(
    client: httpx.AsyncClient, token: str, upstream: FakeUpstream
) -> None:
    upstream.responder = lambda _r: httpx.Response(401)
    notification = {"jsonrpc": "2.0", "method": "notifications/initialized"}
    response = await client.post("/mcp", json=notification, headers=bearer(token))
    assert response.status_code == 502
    assert response.json()["id"] is None
    assert response.json()["error"]["code"] == -32001
    assert "www-authenticate" not in response.headers


async def test_upstream_auth_failure_on_get_is_http_502(
    client: httpx.AsyncClient, token: str, upstream: FakeUpstream
) -> None:
    upstream.responder = lambda _r: httpx.Response(402)
    response = await client.get("/mcp", headers=bearer(token))
    assert response.status_code == 502
    assert response.json()["error"]["code"] == -32002


async def test_oversized_upstream_401_body_is_drained_not_relayed(
    client: httpx.AsyncClient, token: str, upstream: FakeUpstream
) -> None:
    stream = TrackedStream([b"x" * 40_000] * 5)
    upstream.responder = lambda _r: httpx.Response(401, stream=stream)
    response = await client.post("/mcp", json=RPC_BODY, headers=bearer(token))
    assert response.status_code == 200
    assert stream.closed
    assert "xxxx" not in response.text


# --- upstream transport failures ------------------------------------------------------


@pytest.mark.parametrize(
    ("failure", "status", "code"),
    [
        (httpx.ConnectError("refused"), 502, -32003),
        (httpx.ConnectTimeout("slow"), 504, -32004),
        (httpx.ReadTimeout("slow"), 504, -32004),
        (httpx.PoolTimeout("busy"), 504, -32004),
    ],
)
async def test_upstream_transport_failures(
    mapping_dir: Path,
    token: str,
    idp: FakeIdp,
    failure: httpx.TransportError,
    status: int,
    code: int,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def route(request: httpx.Request) -> httpx.Response:
        if request.url.host == "idp.example.com":
            return idp.handle(request)
        raise failure

    app = build_app(mapping_dir, httpx.MockTransport(route))
    with caplog.at_level(logging.INFO):
        async with client_for(app) as http:
            response = await http.post("/mcp", json=RPC_BODY, headers=bearer(token))
    assert response.status_code == status
    assert response.json()["error"]["code"] == code
    assert response.json()["id"] == 7
    assert type(failure).__name__ in caplog.text
    assert audit_lines(caplog)[-1]["outcome"] == "upstream_error"


# --- body limit -----------------------------------------------------------------------


async def test_declared_oversize_body_is_413_without_reaching_upstream(
    mapping_dir: Path, transport: httpx.MockTransport, token: str, upstream: FakeUpstream
) -> None:
    app = build_app(mapping_dir, transport, limits__max_request_body_bytes=100)
    async with client_for(app) as http:
        response = await http.post("/mcp", content=b"x" * 101, headers=bearer(token))
    assert response.status_code == 413
    assert response.json()["error"]["code"] == -32600
    assert upstream.requests == []


async def test_chunked_oversize_body_stops_reading_at_the_limit(
    mapping_dir: Path, transport: httpx.MockTransport, token: str, upstream: FakeUpstream
) -> None:
    app = build_app(mapping_dir, transport, limits__max_request_body_bytes=100)
    pulled = 0

    async def chunks() -> AsyncIterator[bytes]:
        nonlocal pulled
        for _ in range(50):
            pulled += 1
            yield b"y" * 60

    async with client_for(app) as http:
        response = await http.post("/mcp", content=chunks(), headers=bearer(token))
    assert response.status_code == 413
    assert pulled <= 3
    assert upstream.requests == []


async def test_body_at_the_limit_is_accepted(
    mapping_dir: Path, transport: httpx.MockTransport, token: str, upstream: FakeUpstream
) -> None:
    app = build_app(mapping_dir, transport, limits__max_request_body_bytes=100)
    async with client_for(app) as http:
        response = await http.post("/mcp", content=b"z" * 100, headers=bearer(token))
    assert response.status_code == 200
    assert len(upstream.requests) == 1


async def test_unauthenticated_oversize_body_is_never_read(
    client: httpx.AsyncClient, upstream: FakeUpstream
) -> None:
    response = await client.post("/mcp", content=b"x" * 5_000_000)
    assert response.status_code == 401
    assert upstream.requests == []


# --- audit ----------------------------------------------------------------------------


async def test_audit_line_for_a_forwarded_tool_call(
    client: httpx.AsyncClient, token: str, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger="mcp_gateway.audit"):
        await client.post("/mcp", json=RPC_BODY, headers=bearer(token))
    (line,) = audit_lines(caplog)
    assert set(line) == {
        "ts",
        "event",
        "subject",
        "organization",
        "labels",
        "http_method",
        "rpc_method",
        "rpc_id",
        "tool",
        "status",
        "outcome",
        "reason",
        "upstream",
        "latency_ms",
        "upstream_status",
        "upstream_session",
    }
    assert line["upstream_session"] is False
    assert line["event"] == "mcp_request"
    assert line["subject"] == "user-1"
    assert line["organization"] == "client-x"
    assert line["labels"] == {"client": "client-x"}
    assert line["http_method"] == "POST"
    assert line["rpc_method"] == "tools/call"
    assert line["rpc_id"] == 7
    assert line["tool"] == "search"
    assert line["status"] == 200
    assert line["outcome"] == "forwarded"
    assert line["upstream"] == "primary"
    assert line["upstream_status"] == 200
    assert line["latency_ms"] >= 0


async def test_audit_outcomes_for_failures(
    client: httpx.AsyncClient,
    rsa_key: rsa.RSAPrivateKey,
    idp: FakeIdp,
    upstream: FakeUpstream,
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.INFO, logger="mcp_gateway.audit"):
        await client.post("/mcp", json=RPC_BODY)
        await client.post(
            "/mcp",
            json=RPC_BODY,
            headers=bearer(mint(rsa_key, overrides={"email": "x@example.com", "org": "none"})),
        )
        upstream.responder = lambda _r: httpx.Response(401)
        await client.post("/mcp", json=RPC_BODY, headers=bearer(mint(rsa_key)))
    unauth, denied, rejected = audit_lines(caplog)
    assert (unauth["outcome"], unauth["status"], unauth["subject"]) == (
        "unauthenticated",
        401,
        None,
    )
    assert (denied["outcome"], denied["reason"], denied["status"]) == ("denied", "no_mapping", 403)
    assert (rejected["outcome"], rejected["upstream_status"]) == ("upstream_auth_rejected", 401)


async def test_audit_outcomes_for_idp_down_and_body_too_large(
    mapping_dir: Path,
    transport: httpx.MockTransport,
    rsa_key: rsa.RSAPrivateKey,
    idp: FakeIdp,
    caplog: pytest.LogCaptureFixture,
) -> None:
    app = build_app(mapping_dir, transport, limits__max_request_body_bytes=10)
    with caplog.at_level(logging.INFO, logger="mcp_gateway.audit"):
        async with client_for(app) as http:
            await http.post("/mcp", content=b"x" * 50, headers=bearer(mint(rsa_key)))
            idp.keys.clear()
            idp.down = True
            fresh = build_app(mapping_dir, transport)
        async with client_for(fresh) as http:
            await http.post("/mcp", json=RPC_BODY, headers=bearer(mint(rsa_key)))
    too_large, down = audit_lines(caplog)
    assert too_large["outcome"] == "body_too_large"
    assert down["outcome"] == "idp_unavailable"


async def test_audit_never_contains_keys_tokens_or_arguments(
    client: httpx.AsyncClient,
    token: str,
    upstream: FakeUpstream,
    caplog: pytest.LogCaptureFixture,
) -> None:
    upstream.responder = lambda _r: httpx.Response(
        200, json={"jsonrpc": "2.0", "id": 7, "result": {"content": "response-secret-text"}}
    )
    with caplog.at_level(logging.DEBUG):
        await client.post("/mcp", json=RPC_BODY, headers=bearer(token))
        await client.post("/mcp", json=RPC_BODY, headers=bearer(token + "tampered"))
    everything = caplog.text + "\n".join(r.getMessage() for r in caplog.records)
    assert audit_lines(caplog), "the positive control: audit lines were captured"
    assert FAKE_API_KEY not in everything
    assert token not in everything
    assert token.split(".")[1] not in everything
    assert "classified-text" not in everything
    assert "response-secret-text" not in everything


async def test_audit_can_be_disabled(
    mapping_dir: Path,
    transport: httpx.MockTransport,
    token: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    app = build_app(mapping_dir, transport, audit={"enabled": False})
    with caplog.at_level(logging.INFO, logger="mcp_gateway.audit"):
        async with client_for(app) as http:
            response = await http.post("/mcp", json=RPC_BODY, headers=bearer(token))
    assert response.status_code == 200
    assert audit_lines(caplog) == []


# --- raw ASGI drivers ---------------------------------------------------------------------


def asgi_scope(
    token: str, body: bytes, extra_headers: tuple[tuple[bytes, bytes], ...] = ()
) -> dict[str, Any]:
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.4"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "https",
        "path": "/mcp",
        "raw_path": b"/mcp",
        "query_string": b"",
        "headers": [
            (b"host", b"gw.example.com"),
            (b"authorization", f"Bearer {token}".encode()),
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode()),
            *extra_headers,
        ],
        "server": ("gw.example.com", 443),
        "client": ("203.0.113.9", 1234),
    }


def routed_transport(idp: FakeIdp, upstream_handler: Any) -> httpx.MockTransport:
    async def route(request: httpx.Request) -> httpx.Response:
        if request.url.host == "idp.example.com":
            return idp.handle(request)
        return await upstream_handler(request)

    return httpx.MockTransport(route)


async def call_then_disconnect(
    app: Starlette, token: str, *, after_seconds: float
) -> tuple[list[dict[str, Any]], float]:
    """POST a tools/call, then send http.disconnect ``after_seconds`` later.

    Returns the ASGI messages sent and how long the app took to return.
    """
    body = json.dumps(RPC_BODY).encode()
    delivered = False

    async def receive() -> dict[str, Any]:
        nonlocal delivered
        if not delivered:
            delivered = True
            return {"type": "http.request", "body": body, "more_body": False}
        await asyncio.sleep(after_seconds)
        return {"type": "http.disconnect"}

    sent: list[dict[str, Any]] = []

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    started = asyncio.get_running_loop().time()
    await asyncio.wait_for(app(asgi_scope(token, body), receive, send), timeout=5)
    return sent, asyncio.get_running_loop().time() - started


async def test_client_disconnect_while_waiting_for_upstream_headers_aborts_the_request(
    mapping_dir: Path,
    idp: FakeIdp,
    token: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    cancelled = asyncio.Event()

    async def never_answers(request: httpx.Request) -> httpx.Response:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise
        raise AssertionError("unreachable")

    app = build_app(mapping_dir, routed_transport(idp, never_answers))
    with caplog.at_level(logging.INFO, logger="mcp_gateway.audit"):
        sent, elapsed = await call_then_disconnect(app, token, after_seconds=0.1)
    assert cancelled.is_set(), "the in-flight upstream request must be cancelled"
    assert elapsed < 2
    assert not [m for m in sent if m["type"] == "http.response.start" and m["status"] != 499]
    (line,) = audit_lines(caplog)
    assert line["outcome"] == "client_disconnected"
    assert line["status"] == 499


async def test_client_disconnect_while_draining_an_upstream_401_stops_the_drain(
    mapping_dir: Path,
    idp: FakeIdp,
    token: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    stream = TrackedStream([b"denied"], hang=True)

    async def rejects_slowly(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, stream=stream)

    app = build_app(mapping_dir, routed_transport(idp, rejects_slowly))
    with caplog.at_level(logging.INFO, logger="mcp_gateway.audit"):
        _, elapsed = await call_then_disconnect(app, token, after_seconds=0.1)
    assert elapsed < 2
    assert stream.closed
    (line,) = audit_lines(caplog)
    assert line["outcome"] == "client_disconnected"


# --- request body timeout -----------------------------------------------------------------


async def test_a_stalled_request_body_is_408_and_audited(
    mapping_dir: Path,
    transport: httpx.MockTransport,
    token: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    app = build_app(mapping_dir, transport, limits__request_body_timeout_seconds=0.2)
    delivered = False

    async def receive() -> dict[str, Any]:
        nonlocal delivered
        if not delivered:
            delivered = True
            return {"type": "http.request", "body": b'{"jsonrpc"', "more_body": True}
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    sent: list[dict[str, Any]] = []

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    with caplog.at_level(logging.INFO, logger="mcp_gateway.audit"):
        await asyncio.wait_for(app(asgi_scope(token, b"x" * 100), receive, send), timeout=5)
    (start,) = [m for m in sent if m["type"] == "http.response.start"]
    assert start["status"] == 408
    (line,) = audit_lines(caplog)
    assert line["outcome"] == "request_body_timeout"
    assert line["status"] == 408


# --- non-ASCII inbound headers --------------------------------------------------------------


async def test_non_ascii_header_bytes_are_forwarded_verbatim(
    app: Starlette, token: str, upstream: FakeUpstream
) -> None:
    body = json.dumps(RPC_BODY).encode()
    delivered = False

    async def receive() -> dict[str, Any]:
        nonlocal delivered
        if not delivered:
            delivered = True
            return {"type": "http.request", "body": body, "more_body": False}
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    sent: list[dict[str, Any]] = []

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    scope = asgi_scope(token, body, ((b"user-agent", "café-client".encode()),))
    await asyncio.wait_for(app(scope, receive, send), timeout=5)
    (start,) = [m for m in sent if m["type"] == "http.response.start"]
    assert start["status"] == 200
    assert (b"user-agent", "café-client".encode()) in upstream.requests[0].headers.raw
