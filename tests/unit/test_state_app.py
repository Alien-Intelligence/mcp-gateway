"""The gateway fails closed when its shared state is unavailable, and reports readiness."""

import base64
import logging
from pathlib import Path
from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from starlette.applications import Starlette

from mcp_gateway.app import create_app
from mcp_gateway.config import GatewayConfig, ResolverCacheConfig
from mcp_gateway.resolver import (
    CachingResolver,
    Credential,
    CredentialResolver,
    Denied,
    MemoryCacheStore,
    StaticResolver,
)
from mcp_gateway.resolver.cache import CacheKey
from mcp_gateway.state import StateUnavailableError, build_state

from .conftest import FAKE_API_KEY, FakeUpstream, TrackedStream, base_config_dict, config_from, mint
from .state_fakes import BrokenCache, state_with
from .test_app import RPC_BODY, audit_lines, bearer, client_for, open_session

INITIALIZE = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
UNAVAILABLE_MESSAGE = "The gateway's shared state is unavailable. Retry shortly."


@pytest.fixture
def token(rsa_key: rsa.RSAPrivateKey) -> str:
    return mint(rsa_key)


def app_with(
    mapping_dir: Path,
    transport: httpx.MockTransport,
    state: Any,
    resolver: CredentialResolver | None = None,
    **overrides: Any,
) -> Starlette:
    data = base_config_dict(str(mapping_dir / "mapping.json"))
    for dotted, value in overrides.items():
        node = data
        *parents, leaf = dotted.split("__")
        for part in parents:
            node = node[part]
        node[leaf] = value
    return create_app(config_from(data), resolver=resolver, http_transport=transport, state=state)


def assert_state_unavailable(response: httpx.Response) -> None:
    assert response.status_code == 503
    assert response.headers["retry-after"] == "5"
    error = response.json()["error"]
    assert error["code"] == -32013
    assert error["message"] == UNAVAILABLE_MESSAGE


async def test_a_session_check_that_fails_answers_503_and_forwards_nothing(
    mapping_dir: Path,
    transport: httpx.MockTransport,
    upstream: FakeUpstream,
    token: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    state, _ = state_with("check")
    app = app_with(mapping_dir, transport, state)
    with caplog.at_level(logging.INFO, logger="mcp_gateway.audit"):
        async with client_for(app) as http:
            response = await http.post(
                "/mcp", json=RPC_BODY, headers={**bearer(token), "Mcp-Session-Id": "sess-1"}
            )
    assert_state_unavailable(response)
    assert response.json()["id"] == RPC_BODY["id"]
    assert upstream.requests == [], "a session that could not be checked must never be forwarded"
    (line,) = audit_lines(caplog)
    assert (line["status"], line["outcome"]) == (503, "state_unavailable")


async def test_binding_that_fails_after_initialize_discards_the_upstream_session(
    mapping_dir: Path,
    transport: httpx.MockTransport,
    upstream: FakeUpstream,
    token: str,
) -> None:
    stream = TrackedStream([b'{"jsonrpc":"2.0","id":1,"result":{}}'])
    upstream.responder = lambda _r: httpx.Response(
        200,
        headers={"Mcp-Session-Id": "issued", "Content-Type": "application/json"},
        stream=stream,
    )
    state, sessions = state_with("bind")
    app = app_with(mapping_dir, transport, state)
    async with client_for(app) as http:
        response = await http.post("/mcp", json=INITIALIZE, headers=bearer(token))
    assert_state_unavailable(response)
    assert "mcp-session-id" not in response.headers, (
        "a session the gateway cannot bind is not relayed"
    )
    assert stream.closed, "the upstream response must be released"
    assert sessions.calls == ["bind"]


async def test_a_cache_that_is_down_never_lets_the_resolver_run(
    mapping_dir: Path,
    transport: httpx.MockTransport,
    upstream: FakeUpstream,
    token: str,
) -> None:
    calls: list[str] = []

    class CountingResolver:
        async def resolve(self, identity: Any) -> Credential | Denied:
            calls.append("resolve")
            return Denied("no_mapping")

        async def invalidate(self, identity: Any) -> None:
            return None

        async def aclose(self) -> None:
            return None

    state, _ = state_with(cache_down=True)
    cached = CachingResolver(
        CountingResolver(),
        ResolverCacheConfig(positive_ttl_seconds=60, negative_ttl_seconds=5, max_entries=None),
        BrokenCache(),
    )
    app = app_with(mapping_dir, transport, state, resolver=cached)
    async with client_for(app) as http:
        response = await http.post("/mcp", json=RPC_BODY, headers=bearer(token))
    assert_state_unavailable(response)
    assert calls == [], "the resolver must not be called around a broken cache"
    assert upstream.requests == []


async def test_a_failed_invalidation_after_an_upstream_401_answers_503(
    mapping_dir: Path,
    transport: httpx.MockTransport,
    upstream: FakeUpstream,
    token: str,
) -> None:
    class StubbornCache(MemoryCacheStore):
        async def invalidate(self, key: CacheKey) -> None:
            raise StateUnavailableError("shared state unavailable (FakeError)")

    config = ResolverCacheConfig(positive_ttl_seconds=60, negative_ttl_seconds=5, max_entries=10)
    inner = StaticResolver(Path(mapping_dir / "mapping.json"), ["main"], email_matching=True)
    cached = CachingResolver(inner, config, StubbornCache(10))
    upstream.responder = lambda _r: httpx.Response(401, json={})
    state, _ = state_with()
    app = app_with(mapping_dir, transport, state, resolver=cached)
    async with client_for(app) as http:
        response = await http.post("/mcp", json=RPC_BODY, headers=bearer(token))
    assert_state_unavailable(response)
    assert FAKE_API_KEY not in response.text


async def test_refreshing_a_binding_after_the_stream_cannot_change_the_answer(
    mapping_dir: Path,
    transport: httpx.MockTransport,
    upstream: FakeUpstream,
    token: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    state, sessions = state_with("touch")
    app = app_with(mapping_dir, transport, state)
    async with client_for(app) as http:
        await open_session(http, upstream, token)
        with caplog.at_level(logging.ERROR, logger="mcp_gateway.proxy"):
            response = await http.post(
                "/mcp", json=RPC_BODY, headers={**bearer(token), "Mcp-Session-Id": "sess-1"}
            )
    assert response.status_code == 200
    assert "touch" in sessions.calls
    assert [r.getMessage() for r in caplog.records] == [
        "session binding not refreshed: shared state unavailable"
    ]


async def test_dropping_a_binding_after_an_upstream_404_still_relays_the_404(
    mapping_dir: Path,
    transport: httpx.MockTransport,
    upstream: FakeUpstream,
    token: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    state, sessions = state_with("drop")
    app = app_with(mapping_dir, transport, state)
    async with client_for(app) as http:
        await open_session(http, upstream, token)
        upstream.responder = lambda _r: httpx.Response(404, json={"gone": True})
        with caplog.at_level(logging.ERROR, logger="mcp_gateway.proxy"):
            response = await http.post(
                "/mcp", json=RPC_BODY, headers={**bearer(token), "Mcp-Session-Id": "sess-1"}
            )
    assert response.status_code == 404
    assert "drop" in sessions.calls
    assert "session binding not dropped: shared state unavailable" in [
        r.getMessage() for r in caplog.records
    ]


async def test_a_session_survives_the_memory_store_end_to_end(
    mapping_dir: Path,
    transport: httpx.MockTransport,
    upstream: FakeUpstream,
    token: str,
    rsa_key: rsa.RSAPrivateKey,
) -> None:
    """The refusal tests above would pass for any broken store; this proves the happy path."""
    state, _ = state_with()
    app = app_with(mapping_dir, transport, state)
    async with client_for(app) as http:
        await open_session(http, upstream, token)
        response = await http.post(
            "/mcp", json=RPC_BODY, headers={**bearer(token), "Mcp-Session-Id": "sess-1"}
        )
    assert response.status_code == 200
    assert len(upstream.requests) == 1


# --- readiness ------------------------------------------------------------------------


def dead_redis_config(tmp_path: Path, mapping_file: str) -> GatewayConfig:
    """A redis state whose server does not exist: nothing listens on port 1."""
    (tmp_path / "url").write_text("redis://127.0.0.1:1/0\n")
    (tmp_path / "key").write_text(base64.b64encode(bytes(32)).decode() + "\n")
    data = base_config_dict(mapping_file)
    data["sessions"]["max_entries"] = None
    data["state"] = {
        "type": "redis",
        "url_file": str(tmp_path / "url"),
        "key_prefix": "t:",
        "connect_timeout_seconds": 0.5,
        "operation_timeout_seconds": 0.5,
        "max_connections": 2,
        "encryption_key_file": str(tmp_path / "key"),
    }
    return config_from(data)


async def test_readyz_is_ready_with_the_memory_backend(
    mapping_dir: Path, transport: httpx.MockTransport
) -> None:
    app = app_with(mapping_dir, transport, None)
    async with client_for(app) as http:
        response = await http.get("/readyz")
    assert (response.status_code, response.json()) == (200, {"status": "ready"})


async def test_readyz_drains_a_replica_that_lost_redis_but_healthz_stays_up(
    mapping_dir: Path, transport: httpx.MockTransport, tmp_path: Path
) -> None:
    config = dead_redis_config(tmp_path, str(mapping_dir / "mapping.json"))
    app = create_app(config, http_transport=transport)
    async with client_for(app) as http:
        ready = await http.get("/readyz")
        alive = await http.get("/healthz")
    assert (ready.status_code, ready.json()) == (503, {"status": "state_unavailable"})
    assert alive.status_code == 200


async def test_a_dead_redis_fails_requests_closed_within_the_timeout(
    mapping_dir: Path,
    transport: httpx.MockTransport,
    upstream: FakeUpstream,
    token: str,
    tmp_path: Path,
) -> None:
    config = dead_redis_config(tmp_path, str(mapping_dir / "mapping.json"))
    app = create_app(config, http_transport=transport)
    async with client_for(app) as http:
        response = await http.post(
            "/mcp", json=RPC_BODY, headers={**bearer(token), "Mcp-Session-Id": "sess-1"}
        )
    assert_state_unavailable(response)
    assert upstream.requests == []


def test_build_state_exposes_the_redis_client_and_prefix_for_other_features(
    mapping_dir: Path, tmp_path: Path
) -> None:
    config = dead_redis_config(tmp_path, str(mapping_dir / "mapping.json"))
    state = build_state(config)
    assert state.redis is not None
    assert state.key_prefix == "t:"
    memory = build_state(config_from(base_config_dict()))
    assert (memory.redis, memory.key_prefix) == (None, None)
