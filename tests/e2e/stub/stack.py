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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest

from e2e.harness import MCP_ACCEPT, Persona, Provider, initialize_payload

HTTP_TIMEOUT = httpx.Timeout(30.0, connect=5.0)
STUB_URL = "http://127.0.0.1:14000"
WEBHOOK_URL = "http://127.0.0.1:18090"
KEYCLOAK_ISSUER = "http://localhost:18080/realms/alien-gateway-dev"
HYDRA_ISSUER = "http://localhost:14444"
REQUIRED_ENV = ("E2E_USER_PASSWORD", "E2E_RUN_DIR", "E2E_STATE_DIR", "E2E_OTEL_DIR")

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
# What each gateway's 401 challenge advertises.
ADVERTISED_SCOPE = {
    "keycloak": "openid mcp-gateway",
    "hydra": "openid",
    "chain": "openid mcp-gateway mcp-write",
    "unreachable": "openid mcp-gateway",
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
