"""Building blocks of the stub-target suite: the compose stack's fixed topology, the stub's and
the webhook's control endpoints, and raw MCP requests.

Every gateway here is reached with a real token from a real identity provider (Keycloak or
Hydra) and forwards to the fault-injection upstream stub. Secrets (the users' password) come
from the environment and are never printed or written to the evidence file.
"""

import asyncio
import json
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
import pytest
from redis.asyncio import Redis

from e2e.harness import MCP_ACCEPT, Persona, Provider, initialize_payload

HTTP_TIMEOUT = httpx.Timeout(30.0, connect=5.0)
STUB_URL = "http://127.0.0.1:14000"
WEBHOOK_URL = "http://127.0.0.1:18090"
KEYCLOAK_ISSUER = "http://localhost:18080/realms/alien-gateway-dev"
HYDRA_ISSUER = "http://localhost:14444"
REQUIRED_ENV = (
    "E2E_USER_PASSWORD",
    "E2E_RUN_DIR",
    "E2E_STATE_DIR",
    "E2E_OTEL_DIR",
    "MCP_GATEWAY_TEST_REDIS_URL",
)

KEYCLOAK = Provider(
    name="keycloak",
    gateway_url="http://localhost:18000/mcp",
    issuer=KEYCLOAK_ISSUER,
    wrong_audience={"scope": "openid"},
)
HYDRA = Provider(
    name="hydra",
    gateway_url="http://localhost:18001/mcp",
    issuer=HYDRA_ISSUER,
    wrong_audience={"resource": "http://localhost:18999/mcp"},
)
# Keycloak, a webhook resolver whose endpoint nobody listens to.
UNREACHABLE = Provider(
    name="unreachable",
    gateway_url="http://localhost:18003/mcp",
    issuer=KEYCLOAK_ISSUER,
    wrong_audience={"scope": "openid"},
)
STATIC_PROVIDERS = (KEYCLOAK, HYDRA)
# The replica pair behind its load balancer (Keycloak). Tokens are issued for this address, which
# is the public URL of both replicas.
HA = Provider(
    name="ha",
    gateway_url="http://localhost:18004/mcp",
    issuer=KEYCLOAK_ISSUER,
    wrong_audience={"scope": "openid"},
)
# The same two replicas reached directly. Only for posting: the token is the pair's (HA), and
# the resource metadata of each replica still names the pair's public URL.
HA_A = replace(HA, name="ha-a", gateway_url="http://localhost:18005/mcp")
HA_B = replace(HA, name="ha-b", gateway_url="http://localhost:18006/mcp")
# Keycloak, shared state pointing at a Redis nobody listens on.
NOREDIS = Provider(
    name="noredis",
    gateway_url="http://localhost:18007/mcp",
    issuer=KEYCLOAK_ISSUER,
    wrong_audience={"scope": "openid"},
)
# What each gateway's 401 challenge advertises.
ADVERTISED_SCOPE = {
    "keycloak": "openid mcp-gateway",
    "hydra": "openid",
    "chain": "openid mcp-gateway mcp-write",
    "unreachable": "openid mcp-gateway",
    "ha": "openid mcp-gateway",
    "noredis": "openid mcp-gateway",
}
# Test-only browser origin allowed on every gateway, and one that is not.
ALLOWED_ORIGIN = "http://localhost:6274"
FOREIGN_ORIGIN = "https://evil.example"


def persona(name: str, domain: str) -> Persona:
    return Persona(name, f"{name}@{domain}", None, None, None)


PERSONAS = {
    "alice": persona("alice", "client-x.test"),
    "bob": persona("bob", "client-y.test"),
    "carol": persona("carol", "client-x.test"),
    "dave": persona("dave", "client-x.test"),
    "erin": persona("erin", "client-q.test"),
    "frank": persona("frank", "client-q.test"),
    "revoked": persona("revoked", "client-r.test"),
    "unpaid": persona("unpaid", "client-p.test"),
    "nomap": persona("nomap", "client-z.test"),
}


@dataclass(frozen=True)
class StubSettings:
    user_password: str = field(repr=False)
    run_dir: Path
    # The stack's state volume (the personas' API key files) and the test collector's output.
    state_dir: Path
    otel_dir: Path
    # The stack's Redis, the one the replica pair shares (see ``flush_rate_limits``).
    redis_url: str = field(repr=False)

    def read_key(self, persona: str) -> str:
        key = (self.state_dir / f"{persona}.key").read_text(encoding="utf-8").strip()
        if not key:
            raise ValueError(f"key file of {persona} is empty")
        return key


def load_stub_settings() -> StubSettings:
    missing = [name for name in REQUIRED_ENV if not os.environ.get(name)]
    if missing:
        pytest.fail("stub harness is not configured, missing: " + ", ".join(missing), pytrace=False)
    return StubSettings(
        os.environ["E2E_USER_PASSWORD"],
        Path(os.environ["E2E_RUN_DIR"]),
        Path(os.environ["E2E_STATE_DIR"]),
        Path(os.environ["E2E_OTEL_DIR"]),
        os.environ["MCP_GATEWAY_TEST_REDIS_URL"],
    )


def prm_url(provider: Provider) -> str:
    scheme_host, _, path = provider.gateway_url.partition("/mcp")
    return f"{scheme_host}/.well-known/oauth-protected-resource/mcp{path}"


def origin_of(provider: Provider) -> str:
    return provider.gateway_url.removesuffix("/mcp")


# ---------------------------------------------------------------------------------------------
# Raw MCP requests (status codes and headers matter, so no SDK here)
# ---------------------------------------------------------------------------------------------


def tool_call(tool: str, request_id: int = 7, **arguments: Any) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "tools/call",
        "params": {"name": tool, "arguments": arguments},
    }


TOOLS_LIST = {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}


def bearer(token: str, **extra: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", **extra}


async def post(
    client: httpx.AsyncClient,
    provider: Provider,
    token: str | None,
    payload: dict[str, Any],
    **headers: str,
) -> httpx.Response:
    sent = {"Accept": MCP_ACCEPT, "Content-Type": "application/json", **headers}
    if token is not None:
        sent["Authorization"] = f"Bearer {token}"
    return await client.post(provider.gateway_url, json=payload, headers=sent)


async def open_session(client: httpx.AsyncClient, provider: Provider, token: str) -> str:
    """``initialize`` on a stateful upstream: the session id the stub minted, relayed untouched."""
    response = await post(client, provider, token, initialize_payload())
    assert response.status_code == 200, response.text[:300]
    return response.headers["mcp-session-id"]


def echoed(response: httpx.Response) -> dict[str, Any]:
    """The stub's ``echo`` tool result: mode, persona and the headers it received."""
    assert response.status_code == 200, f"{response.status_code}: {response.text[:300]}"
    text = response.json()["result"]["content"][0]["text"]
    return json.loads(text)


def error_code(response: httpx.Response) -> int:
    body = response.json()
    assert isinstance(body.get("error"), dict), f"no JSON-RPC error: {response.text[:300]}"
    return body["error"]["code"]


def events_of(raw: bytes) -> list[bytes]:
    return [event for event in raw.split(b"\n\n") if event]


def json_of(event: bytes) -> dict[str, Any]:
    return json.loads(event.split(b"data: ", 1)[1])


async def read_body(response: httpx.Response) -> tuple[bytes, Exception | None]:
    """Everything the client received and the error that ended the body, if any."""
    received = b""
    try:
        async for chunk in response.aiter_raw():
            received += chunk
    except httpx.HTTPError as exc:
        return received, exc
    return received, None


# ---------------------------------------------------------------------------------------------
# The stub and the webhook service
# ---------------------------------------------------------------------------------------------


class StubControl:
    """The upstream stub's control endpoints: what reached it, and session expiry."""

    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    async def reset(self) -> None:
        response = await self._client.post(f"{STUB_URL}/_stub/reset")
        assert response.status_code == 204, response.text

    async def requests(self) -> list[dict[str, Any]]:
        response = await self._client.get(f"{STUB_URL}/_stub/requests")
        assert response.status_code == 200, response.text
        return response.json()["requests"]

    async def expire(self, session_id: str) -> None:
        response = await self._client.post(f"{STUB_URL}/_stub/sessions/{session_id}/expire")
        assert response.status_code == 204, response.text

    async def wait_for(
        self, predicate: Callable[[dict[str, Any]], bool], *, timeout: float
    ) -> dict[str, Any]:
        """Poll the request log until a record matches, within ``timeout`` seconds."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for record in await self.requests():
                if predicate(record):
                    return record
            await asyncio.sleep(0.1)
        raise AssertionError(f"no matching request reached the stub within {timeout} s")


async def webhook_resolve_count(client: httpx.AsyncClient) -> int:
    """Signed resolve calls the dev webhook service has served since it started."""
    response = await client.get(f"{WEBHOOK_URL}/_stats")
    assert response.status_code == 200, response.text
    return response.json()["resolve_requests"]


# ---------------------------------------------------------------------------------------------
# The replica pair: tools/list, the shared Redis and test isolation
# ---------------------------------------------------------------------------------------------

# The upstream stub's tools, in its order (dev/upstream-stub/app.py: TOOLS). The first three are
# its first tools/list page. `policy_denied_probe` is the one the pair's tool policy denies.
STUB_TOOLS = (
    "echo",
    "policy_denied_probe",
    "slow_sse",
    "fail_midstream_sse",
    "fail_midstream_json",
    "hang",
    "status",
)
DENIED_PROBE = "policy_denied_probe"
INVALID_PARAMS = -32602
INVALID_REQUEST = -32600
UPSTREAM_UNAUTHORIZED = -31001
SESSION_NOT_FOUND = -31007
STATE_UNAVAILABLE = -31013
RATE_LIMITED = -31014
RETRY_AFTER_META_KEY = "alien.club/retryAfterSeconds"

# compose.yaml: `state.key_prefix` of the pair, and the key pattern of its resolver store.
STATE_PREFIX = "mcp-gateway:state:"
HA_STORE_PREFIX = "mcp-gateway:ha:"
# The base of the stub's `_meta` flag that makes it answer tools/list as an SSE stream.
STUB_SSE_FLAG = "stub/sse"


def tools_list(*, cursor: str | None = None, sse: bool = False) -> dict[str, Any]:
    params: dict[str, Any] = {}
    if cursor is not None:
        params["cursor"] = cursor
    if sse:
        params["_meta"] = {STUB_SSE_FLAG: True}
    payload: dict[str, Any] = {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}
    if params:
        payload["params"] = params
    return payload


def list_result(response: httpx.Response) -> dict[str, Any]:
    """The ``result`` of a tools/list answer, whether it came as a JSON body or as SSE."""
    assert response.status_code == 200, f"{response.status_code}: {response.text[:300]}"
    if response.headers["content-type"].startswith("text/event-stream"):
        messages = [json_of(event) for event in events_of(response.content)]
        replies = [message for message in messages if "result" in message]
        assert len(replies) == 1, f"expected one reply event, got: {response.text[:300]}"
        return replies[0]["result"]
    body = response.json()
    assert "result" in body, f"no result: {response.text[:300]}"
    return body["result"]


def ha_store_key(email: str) -> str:
    """The Redis key of an identity in the pair's resolver store (as ``scripts/seed_stores.py``)."""
    return f"{HA_STORE_PREFIX}{quote(email, safe='')}"


def notification(method: str = "notifications/initialized") -> dict[str, Any]:
    return {"jsonrpc": "2.0", "method": method}


async def delete_keys(client: Redis, pattern: str) -> None:
    keys = [key async for key in client.scan_iter(match=pattern, count=200)]
    if keys:
        await client.delete(*keys)


async def flush_rate_limits(client: Redis) -> None:
    """Empty every rate limit bucket of the pair. Test isolation: the buckets refill slowly on
    purpose, so each test that spends them starts from full ones instead of waiting."""
    await delete_keys(client, f"{STATE_PREFIX}rl:*")


async def flush_resolver_cache(client: Redis) -> None:
    """Empty the pair's shared resolver cache (not the invalidation counters)."""
    await delete_keys(client, f"{STATE_PREFIX}c:*")
