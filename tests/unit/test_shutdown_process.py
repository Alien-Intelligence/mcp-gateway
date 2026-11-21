"""SIGTERM on a real gateway process: it stops within its bound and loses nothing in flight.

One real ``mcp-gateway`` process, real sockets. A listen stream is open and idle, a
``tools/call`` is mid-reply and the exporter has data it only sends at shutdown. After SIGTERM:
the call must finish, the idle listen stream must be closed at once (not held until the grace
period ends), the process must exit by itself within the bound, and the shutdown hook must have
run (the span of the call reaches the collector, which the long export interval never sent
before). Every wait is bounded.
"""

import asyncio
import json
import signal
import socket
import sys
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path

import anyio
import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Scope, Send

from mcp_gateway.config import CONFIG_ENV_VAR
from mcp_gateway.telemetry import REQUEST_SPAN

from .conftest import FakeIdp, base_config_dict, jwk_for, mint
from .socket_stack import ACCEPT, INITIALIZE, rpc_call, serve
from .stub_loader import load_stub_module, write_keys

STATEFUL_PATH = "/stateful/mcp"
TEST_SECONDS = 60.0
STARTUP_SECONDS = 20.0
# The longest an upstream may stay silent: the gateway's in-flight grace at shutdown.
READ_TIMEOUT_SECONDS = 8
# The call below lasts about three seconds, well inside the grace.
CALL_EVENTS = 6
CALL_DELAY_SECONDS = 0.5
# The grace, plus a margin for the process to flush and exit. No read is detached here, so the
# application's own 10 s + 10 s are not spent.
EXIT_BOUND_SECONDS = READ_TIMEOUT_SECONDS + 6.0


@dataclass
class Collector:
    """An OTLP/HTTP receiver that remembers what it was sent."""

    posts: list[tuple[str, bytes]] = field(default_factory=list)

    def received(self, path: str) -> list[bytes]:
        return [body for received_path, body in self.posts if received_path == path]


def collector_app(collector: Collector) -> Starlette:
    async def receive(request: Request) -> Response:
        collector.posts.append((request.url.path, await request.body()))
        return Response(status_code=200)

    return Starlette(
        routes=[
            Route("/v1/traces", receive, methods=["POST"]),
            Route("/v1/metrics", receive, methods=["POST"]),
        ]
    )


def jwks_app(idp: FakeIdp) -> Starlette:
    async def jwks(request: Request) -> Response:
        return JSONResponse({"keys": [jwk_for(key, kid) for kid, key in idp.keys.items()]})

    return Starlette(routes=[Route("/jwks", jwks)])


def with_pinging_listen_stream(stub: ASGIApp) -> ASGIApp:
    """The stub, except that its listen stream (GET) is kept alive by pings, like a real server's.

    Without them the upstream read timeout would end the idle stream by itself.
    """

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] != "GET" or scope["path"] != STATEFUL_PATH:
            await stub(scope, receive, send)
            return

        async def pings() -> AsyncIterator[bytes]:
            while True:
                yield b": ping\n\n"
                await anyio.sleep(1.0)

        await StreamingResponse(pings(), media_type="text/event-stream")(scope, receive, send)

    return app


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@dataclass
class Gateway:
    process: asyncio.subprocess.Process
    url: str
    token: str
    collector: Collector
    log: Path


async def wait_until_up(url: str, process: asyncio.subprocess.Process, log: Path) -> None:
    async with httpx.AsyncClient(timeout=2.0) as client:
        with anyio.fail_after(STARTUP_SECONDS):
            while True:
                if process.returncode is not None:
                    raise AssertionError(f"the gateway exited at start:\n{log.read_text()}")
                try:
                    if (await client.get(f"{url}/healthz")).status_code == 200:
                        return
                except httpx.TransportError:
                    pass
                await anyio.sleep(0.1)


@pytest.fixture
async def gateway(
    tmp_path: Path, idp: FakeIdp, rsa_key: rsa.RSAPrivateKey
) -> AsyncIterator[Gateway]:
    collector = Collector()
    stub_app = with_pinging_listen_stream(load_stub_module().create_app(write_keys(tmp_path)))
    async with (
        serve(stub_app) as stub_url,
        serve(jwks_app(idp)) as jwks_url,
        serve(collector_app(collector)) as collector_url,
    ):
        state = tmp_path / "state"
        state.mkdir()
        (state / "alice.key").write_text("fake-alien-key-alice_stub_key\n")
        entry = {
            "match": {"email": "alice@client-x.example"},
            "api_key_file": "alice.key",
            "upstream": "stateful",
            "labels": {"client": "alice"},
        }
        (state / "mapping.json").write_text(json.dumps({"entries": [entry]}))
        port = free_port()
        data = base_config_dict(str(state / "mapping.json"))
        data["listen"] = {"host": "127.0.0.1", "port": port}
        data["auth"]["jwks_uri"] = f"{jwks_url}/jwks"
        data["upstreams"] = {
            "stateful": {
                "url": f"{stub_url}{STATEFUL_PATH}",
                "connect_timeout_seconds": 5,
                "read_timeout_seconds": READ_TIMEOUT_SECONDS,
                "write_timeout_seconds": 5,
                "pool_timeout_seconds": 5,
                "max_connections": 50,
                "max_detached_reads": 10,
            }
        }
        data["observability"] = {
            "otlp": {
                "endpoint": collector_url,
                "protocol": "http/protobuf",
                "headers_file": None,
                "allow_insecure_http": False,
                "timeout_seconds": 2,
                # Longer than the test: only the shutdown flush can deliver anything.
                "export_interval_seconds": 600,
                "service_name": "mcp-gateway-shutdown-test",
                "resource_attributes": {},
            }
        }
        log = tmp_path / "gateway.log"
        env = {"PATH": "/usr/bin:/bin", CONFIG_ENV_VAR: json.dumps(data)}
        with log.open("wb") as sink:
            process = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "mcp_gateway", env=env, stdout=sink, stderr=sink
            )
        url = f"http://127.0.0.1:{port}"
        token = mint(rsa_key, overrides={"sub": "alice", "email": "alice@client-x.example"})
        try:
            await wait_until_up(url, process, log)
            yield Gateway(process, url, token, collector, log)
        finally:
            if process.returncode is None:
                process.kill()
            await process.wait()


async def test_sigterm_finishes_the_call_closes_the_listen_stream_and_flushes(
    gateway: Gateway,
) -> None:
    headers = {"Authorization": f"Bearer {gateway.token}", **ACCEPT}
    ended: dict[str, float] = {}
    first_progress = asyncio.Event()
    listening = asyncio.Event()

    async def listen(session: str) -> None:
        async with (
            httpx.AsyncClient(timeout=None) as client,
            client.stream(
                "GET", f"{gateway.url}/mcp", headers={**headers, "Mcp-Session-Id": session}
            ) as response,
        ):
            assert response.status_code == 200
            async for chunk in response.aiter_raw():
                if chunk:
                    listening.set()
        ended["listen"] = time.monotonic()

    async def call(session: str) -> bytes:
        body = rpc_call("slow_sse", count=CALL_EVENTS, delay_seconds=CALL_DELAY_SECONDS)
        received = b""
        async with (
            httpx.AsyncClient(timeout=None) as client,
            client.stream(
                "POST",
                f"{gateway.url}/mcp",
                json=body,
                headers={**headers, "Mcp-Session-Id": session},
            ) as response,
        ):
            assert response.status_code == 200
            async for chunk in response.aiter_raw():
                received += chunk
                first_progress.set()
        ended["call"] = time.monotonic()
        return received

    with anyio.fail_after(TEST_SECONDS):
        async with httpx.AsyncClient(timeout=10.0) as client:
            opened = await client.post(f"{gateway.url}/mcp", json=INITIALIZE, headers=headers)
        assert opened.status_code == 200
        session = opened.headers["mcp-session-id"]

        listen_task = asyncio.create_task(listen(session))
        await listening.wait()
        call_task = asyncio.create_task(call(session))
        await first_progress.wait()
        assert gateway.collector.posts == [], "nothing may be exported before the shutdown"

        terminated = time.monotonic()
        gateway.process.send_signal(signal.SIGTERM)
        await asyncio.wait_for(asyncio.gather(listen_task, call_task), EXIT_BOUND_SECONDS)
        reply = call_task.result()
        exit_code = await asyncio.wait_for(gateway.process.wait(), EXIT_BOUND_SECONDS)
        exited = time.monotonic()

    log = gateway.log.read_text()
    assert b'"id":7' in reply and b'"result"' in reply, "the in-flight call must get its reply"
    assert ended["listen"] < ended["call"], "the idle listen stream must be closed at once"
    assert exited - terminated < EXIT_BOUND_SECONDS
    # uvicorn re-raises the signal it handled once it has shut down cleanly.
    assert exit_code in (0, -signal.SIGTERM), log
    assert "Application shutdown complete" in log, "the application's shutdown must have run"
    exported = gateway.collector.received("/v1/traces")
    assert any(REQUEST_SPAN.encode() in body for body in exported), (
        "the shutdown hook must flush the finished spans"
    )
