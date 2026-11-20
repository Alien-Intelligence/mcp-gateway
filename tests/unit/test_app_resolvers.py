"""The gateway wired to real webhook resolvers and the cache."""

import logging
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from mcp_gateway.app import create_app
from mcp_gateway.config import ResolverCacheConfig
from mcp_gateway.resolver import (
    CachingResolver,
    WebhookResolver,
)

from .conftest import FAKE_API_KEY, FakeUpstream, base_config_dict, config_from, mint
from .test_app import RPC_BODY, SpyResolver, audit_lines, bearer, build_app, client_for
from .test_webhook_resolver import ALLOW, SECRET, UPSTREAMS, webhook_config
from .webhook_server import WebhookServer, json_responder


@pytest.fixture
def token(rsa_key: rsa.RSAPrivateKey) -> str:
    return mint(rsa_key)


@pytest.fixture
async def server() -> AsyncIterator[WebhookServer]:
    srv = WebhookServer(secret=SECRET.encode())
    srv.responders = [json_responder(ALLOW)]
    await srv.start()
    yield srv
    await srv.stop()


@pytest.fixture
def secret_file(tmp_path: Path) -> str:
    path = tmp_path / "hmac.secret"
    path.write_text(SECRET)
    return str(path)


def webhook_resolver(server: WebhookServer, secret_file: str, **overrides: Any) -> WebhookResolver:
    config = webhook_config(server.url(), {"type": "hmac", "secret_file": secret_file}, **overrides)
    return WebhookResolver(config, UPSTREAMS)


async def test_webhook_down_is_a_503_with_retry_after_and_a_jsonrpc_error(
    mapping_dir: Path,
    transport: httpx.MockTransport,
    token: str,
    upstream: FakeUpstream,
    server: WebhookServer,
    secret_file: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    resolver = webhook_resolver(server, secret_file, max_attempts=2)
    await server.stop()  # the customer endpoint is gone
    app = build_app(mapping_dir, transport, resolver=resolver)
    try:
        with caplog.at_level(logging.INFO, logger="mcp_gateway.audit"):
            async with client_for(app) as http:
                response = await http.post("/mcp", json=RPC_BODY, headers=bearer(token))
    finally:
        await resolver.aclose()
    assert response.status_code == 503
    assert response.headers["retry-after"] == "5"
    body = response.json()
    assert body["id"] == 7
    assert body["error"]["code"] == -32009
    assert "www-authenticate" not in response.headers
    assert upstream.requests == []
    assert audit_lines(caplog)[0]["outcome"] == "resolver_unavailable"
    assert "127.0.0.1" not in response.text


async def test_webhook_answering_503_forever_is_also_a_503(
    mapping_dir: Path,
    transport: httpx.MockTransport,
    token: str,
    server: WebhookServer,
    secret_file: str,
) -> None:
    server.responders = [json_responder({"e": 1}, 503)]
    resolver = webhook_resolver(server, secret_file)
    app = build_app(mapping_dir, transport, resolver=resolver)
    try:
        async with client_for(app) as http:
            response = await http.post("/mcp", json=RPC_BODY, headers=bearer(token))
    finally:
        await resolver.aclose()
    assert response.status_code == 503
    assert len(server.seen) == 3


async def test_webhook_protocol_error_stays_a_generic_500(
    mapping_dir: Path,
    transport: httpx.MockTransport,
    token: str,
    server: WebhookServer,
    secret_file: str,
) -> None:
    server.responders = [json_responder({"decision": "allow", "api_key": FAKE_API_KEY})]
    resolver = webhook_resolver(server, secret_file)
    app = build_app(mapping_dir, transport, resolver=resolver)
    try:
        async with client_for(app) as http:
            response = await http.post("/mcp", json=RPC_BODY, headers=bearer(token))
    finally:
        await resolver.aclose()
    assert response.status_code == 500
    assert response.json()["error"]["code"] == -32005
    assert FAKE_API_KEY not in response.text


async def test_upstream_401_invalidates_the_cache_so_the_next_request_asks_again(
    mapping_dir: Path,
    transport: httpx.MockTransport,
    token: str,
    upstream: FakeUpstream,
    server: WebhookServer,
    secret_file: str,
) -> None:
    inner = webhook_resolver(server, secret_file)
    cache = CachingResolver(
        inner,
        ResolverCacheConfig(positive_ttl_seconds=60, negative_ttl_seconds=5, max_entries=10),
    )
    spy = SpyResolver(cache)
    app = build_app(mapping_dir, transport, resolver=spy)
    statuses = iter([200, 200, 401, 200])
    upstream.responder = lambda _r: httpx.Response(next(statuses), json={"jsonrpc": "2.0", "id": 7})
    try:
        async with client_for(app) as http:
            first = await http.post("/mcp", json=RPC_BODY, headers=bearer(token))
            second = await http.post("/mcp", json=RPC_BODY, headers=bearer(token))
            assert len(server.seen) == 1, "the second request must be served from the cache"
            rejected = await http.post("/mcp", json=RPC_BODY, headers=bearer(token))
            assert len(server.seen) == 1
            fourth = await http.post("/mcp", json=RPC_BODY, headers=bearer(token))
    finally:
        await cache.aclose()
    assert [first.status_code, second.status_code, fourth.status_code] == [200, 200, 200]
    assert rejected.json()["error"]["code"] == -32001
    assert [i.subject for i in spy.invalidated] == ["user-1"]
    assert len(server.seen) == 2, "after the 401 the webhook must be asked again"


async def test_config_builds_the_cached_resolver_and_the_lifespan_closes_it(
    mapping_dir: Path,
    transport: httpx.MockTransport,
    token: str,
    server: WebhookServer,
    secret_file: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = base_config_dict(str(mapping_dir / "mapping.json"))
    data["resolver"] = {
        "type": "webhook",
        "url": server.url(),
        "auth": {"type": "hmac", "secret_file": secret_file},
        "ca_file": None,
        "default_upstream": "main",
        "allow_insecure_http": True,
        "timeout_seconds": 2,
        "total_timeout_seconds": 10,
        "max_attempts": 2,
        "backoff_base_seconds": 0.01,
        "backoff_max_seconds": 0.02,
        "max_ttl_seconds": 600,
    }
    data["resolver_cache"] = {
        "positive_ttl_seconds": 60,
        "negative_ttl_seconds": 5,
        "max_entries": 10,
    }
    closed: list[WebhookResolver] = []
    original_aclose = WebhookResolver.aclose

    async def recording_aclose(self: WebhookResolver) -> None:
        closed.append(self)
        await original_aclose(self)

    monkeypatch.setattr(WebhookResolver, "aclose", recording_aclose)
    app = create_app(config_from(data), http_transport=transport)
    async with app.router.lifespan_context(app), client_for(app) as http:
        for _ in range(3):
            response = await http.post("/mcp", json=RPC_BODY, headers=bearer(token))
            assert response.status_code == 200
        assert len(server.seen) == 1, "the configured cache must absorb repeat lookups"
    assert len(closed) == 1, "the lifespan must close the resolver built from the config"
