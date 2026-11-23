"""Real-TCP stack for protocol tests: uvicorn serving the gateway and the stub on port 0.

Only the identity provider is in process (a fake JWKS answered by ``FakeIdp``); every byte
between the test client, the gateway and the stub crosses a real socket.
"""

import asyncio
import json
import socket
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import anyio
import httpx
import uvicorn
from starlette.types import ASGIApp

from mcp_gateway.app import create_app

from .conftest import FakeIdp, base_config_dict, config_from, mint
from .stub_loader import load_stub_module, write_keys

STARTUP_SECONDS = 10.0
SHUTDOWN_SECONDS = 10.0
IDP_HOST = "idp.example.com"
PERSONAS = ("alice", "bob", "carol", "revoked", "broke")
# Personas that talk to the stateless (2026-07-28 style) stub endpoint.
STATELESS_PERSONAS = ("carol",)


class SplitTransport(httpx.AsyncBaseTransport):
    """The fake IdP answers in process; every other host goes over a real connection."""

    def __init__(self, idp: FakeIdp) -> None:
        self._idp = httpx.MockTransport(idp.handle)
        self._real = httpx.AsyncHTTPTransport()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == IDP_HOST:
            return await self._idp.handle_async_request(request)
        return await self._real.handle_async_request(request)

    async def aclose(self) -> None:
        await self._real.aclose()


@asynccontextmanager
async def serve(app: ASGIApp) -> AsyncIterator[str]:
    """Run ``app`` under uvicorn on 127.0.0.1 with an OS-assigned port; yield its base URL."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    # "critical": deliberate aborts are logged as ASGI exceptions and would drown the output.
    config = uvicorn.Config(app, log_level="critical", timeout_graceful_shutdown=1)
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        with anyio.fail_after(STARTUP_SECONDS):
            while not server.started:
                if task.done():
                    task.result()
                await anyio.sleep(0.01)
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        with anyio.fail_after(SHUTDOWN_SECONDS):
            await task
        sock.close()


@dataclass
class Stack:
    gateway: str
    stub: str
    tokens: dict[str, str]
    client: httpx.AsyncClient

    def headers(self, who: str, **extra: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.tokens[who]}", **extra}

    async def stub_requests(self) -> list[dict[str, Any]]:
        response = await self.client.get(f"{self.stub}/_stub/requests")
        return response.json()["requests"]

    async def wait_for_stub(
        self, predicate: Callable[[dict[str, Any]], bool], *, timeout: float = 10.0
    ) -> dict[str, Any]:
        """Poll the stub's request log until a record matches, within a bound."""
        with anyio.fail_after(timeout):
            while True:
                for record in await self.stub_requests():
                    if predicate(record):
                        return record
                await anyio.sleep(0.05)


def rpc_call(tool: str, request_id: int = 7, **arguments: Any) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "tools/call",
        "params": {"name": tool, "arguments": arguments},
    }


INITIALIZE = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
ACCEPT = {"Accept": "application/json, text/event-stream"}


@asynccontextmanager
async def running_stack(
    tmp_path: Path, idp: FakeIdp, rsa_key: Any, *, config_edit: Callable[[dict[str, Any]], None]
) -> AsyncIterator[Stack]:
    stub_app = load_stub_module().create_app(write_keys(tmp_path))
    async with serve(stub_app) as stub_url:
        state = tmp_path / "state"
        state.mkdir()
        entries = []
        for who in PERSONAS:
            (state / f"{who}.key").write_text(f"fake-alien-key-{who}_stub_key\n")
            entries.append(
                {
                    "match": {"email": f"{who}@client-x.example"},
                    "api_key_file": f"{who}.key",
                    "upstream": "stateless" if who in STATELESS_PERSONAS else "stateful",
                    "labels": {"client": who},
                }
            )
        (state / "mapping.json").write_text(json.dumps({"entries": entries}))
        data = base_config_dict(str(state / "mapping.json"))
        timeouts = {
            "connect_timeout_seconds": 5,
            "read_timeout_seconds": 30,
            "write_timeout_seconds": 5,
            "pool_timeout_seconds": 5,
            "max_connections": 50,
            "max_detached_reads": 10,
        }
        data["upstreams"] = {
            "stateful": {"url": f"{stub_url}/stateful/mcp", **timeouts},
            "stateless": {"url": f"{stub_url}/stateless/mcp", **timeouts},
        }
        data["auth"]["jwks_uri"] = f"https://{IDP_HOST}/jwks"
        config_edit(data)
        app = create_app(config_from(data), http_transport=SplitTransport(idp))
        tokens = {
            who: mint(rsa_key, overrides={"sub": who, "email": f"{who}@client-x.example"})
            for who in PERSONAS
        }
        async with serve(app) as gateway_url, httpx.AsyncClient(timeout=10.0) as client:
            yield Stack(gateway_url, stub_url, tokens, client)
