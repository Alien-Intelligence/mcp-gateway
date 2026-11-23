"""The upstream response is released and the request audited on every way a relay can end.

A relay that must not outlive its client (a GET stream, a notification) is cancelled when the
client leaves; one that carries a request already sent upstream is detached and read to its end.
"""

import json
import logging
from collections.abc import Callable
from typing import Any

import anyio
import httpx
import pytest
from pydantic import SecretStr
from starlette.types import Message

from mcp_gateway.audit import AuditRecord
from mcp_gateway.config import UpstreamConfig
from mcp_gateway.detached import DetachedReads, ReadsClosedError
from mcp_gateway.identity import Identity
from mcp_gateway.jsonrpc import BodyKind, RpcInfo
from mcp_gateway.proxy import RelayResponse, UpstreamProxy, build_client
from mcp_gateway.resolver.base import Credential, Denied
from mcp_gateway.state import MemorySessionStore, SessionStore
from mcp_gateway.telemetry import Telemetry

from .conftest import GatedStream, TrackedStream, until

SSE = {"Content-Type": "text/event-stream"}
EVENT = b'data: {"jsonrpc":"2.0","method":"notifications/progress"}\n\n'
UPSTREAM = "main"
SESSION = "sess-1"


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class Harness:
    """A relay built the way ``UpstreamProxy`` builds it, with observable side effects."""

    def __init__(
        self,
        stream: TrackedStream | GatedStream,
        caplog: pytest.LogCaptureFixture,
        *,
        detachable: bool,
    ) -> None:
        self.stream = stream
        self.reads = DetachedReads(Telemetry.noop())
        self.clock = Clock()
        self.sessions = MemorySessionStore(
            max_entries=10, max_entries_per_subject=10, idle_ttl_seconds=600, clock=self.clock
        )
        self.audit = AuditRecord(http_method="POST", enabled=True)
        self.caplog = caplog
        upstream = httpx.Response(200, headers=SSE, stream=stream)
        self.relay = build_relay(
            upstream, self.sessions, self.audit, self.reads, detachable=detachable
        )

    @classmethod
    async def create(
        cls,
        stream: TrackedStream | GatedStream,
        caplog: pytest.LogCaptureFixture,
        *,
        detachable: bool,
    ) -> "Harness":
        harness = cls(stream, caplog, detachable=detachable)
        await harness.sessions.bind(UPSTREAM, SESSION, "alice")
        # Bound at 0, the relay ends at 500, the check happens at 1000: alive only if touched.
        harness.clock.now = 500.0
        return harness

    def audit_lines(self) -> list[dict[str, Any]]:
        return [
            json.loads(r.getMessage()) for r in self.caplog.records if r.name == "mcp_gateway.audit"
        ]

    async def session_survives_after_the_clock_moves(self) -> bool:
        """True when ``touch`` refreshed the idle timer (otherwise the entry has expired)."""
        self.clock.now = 1000.0
        return await self.sessions.check(UPSTREAM, SESSION, "alice")


def build_relay(
    upstream: httpx.Response,
    sessions: SessionStore,
    audit: AuditRecord,
    reads: DetachedReads,
    *,
    detachable: bool,
) -> RelayResponse:
    config = UpstreamConfig.model_validate(
        {
            "url": "https://upstream.example.com/mcp",
            "connect_timeout_seconds": 1,
            "read_timeout_seconds": 1,
            "write_timeout_seconds": 1,
            "pool_timeout_seconds": 1,
            "max_connections": 10,
            "max_detached_reads": 5,
        }
    )
    proxy = UpstreamProxy(
        UPSTREAM, config, build_client(config, None), sessions, 65536, 65536, reads
    )
    request_with_id = RpcInfo(method="tools/call", id=7, tool="x", target="x", kind=BodyKind.OBJECT)
    response = proxy.stream(upstream, request_with_id, SESSION, audit, None, detachable=detachable)
    assert isinstance(response, RelayResponse)
    return response


async def run(
    harness: Harness,
    *,
    receive: Callable[[], Any],
    send: Callable[[Message], Any],
) -> None:
    with (
        anyio.fail_after(5),
        harness.caplog.at_level(logging.INFO, logger="mcp_gateway.audit"),
    ):
        await harness.relay({"type": "http"}, receive, send)


async def yielding_send(message: Message) -> None:
    await anyio.sleep(0)


async def test_disconnect_queued_before_the_relay_starts_still_releases_everything(
    caplog: pytest.LogCaptureFixture,
) -> None:
    harness = await Harness.create(TrackedStream([EVENT], hang=True), caplog, detachable=False)

    async def receive() -> Message:
        return {"type": "http.disconnect"}

    await run(harness, receive=receive, send=yielding_send)
    assert harness.stream.closed
    assert await harness.session_survives_after_the_clock_moves()
    (line,) = harness.audit_lines()
    assert line["outcome"] == "client_disconnected"


@pytest.mark.parametrize("detachable", [False, True])
async def test_send_failing_on_the_response_start_still_releases_everything(
    caplog: pytest.LogCaptureFixture, detachable: bool
) -> None:
    harness = await Harness.create(TrackedStream([EVENT]), caplog, detachable=detachable)

    async def receive() -> Message:
        await anyio.sleep_forever()
        raise AssertionError("unreachable")

    async def send(message: Message) -> None:
        raise OSError("client reset the connection")

    await run(harness, receive=receive, send=send)
    await until(lambda: harness.stream.closed)
    assert await harness.session_survives_after_the_clock_moves()
    (line,) = harness.audit_lines()
    assert line["outcome"] == "client_disconnected"
    await until(lambda: len(harness.reads) == 0)


async def test_disconnect_mid_stream_of_a_stream_that_must_not_outlive_its_client_cancels_it(
    caplog: pytest.LogCaptureFixture,
) -> None:
    harness = await Harness.create(TrackedStream([EVENT], hang=True), caplog, detachable=False)
    first_event_sent = anyio.Event()

    async def receive() -> Message:
        await first_event_sent.wait()
        return {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        if message["type"] == "http.response.body" and message.get("body"):
            first_event_sent.set()
        await anyio.sleep(0)

    await run(harness, receive=receive, send=send)
    assert harness.stream.closed, "cancelled and closed before the relay returned"
    assert len(harness.reads) == 0
    (line,) = harness.audit_lines()
    assert line["outcome"] == "client_disconnected"


async def test_disconnect_mid_stream_of_a_request_sent_upstream_is_read_to_its_end(
    caplog: pytest.LogCaptureFixture,
) -> None:
    stream = GatedStream([EVENT])
    harness = await Harness.create(stream, caplog, detachable=True)
    first_event_sent = anyio.Event()

    async def receive() -> Message:
        await first_event_sent.wait()
        return {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        if message["type"] == "http.response.body" and message.get("body"):
            first_event_sent.set()
        await anyio.sleep(0)

    await run(harness, receive=receive, send=send)
    (line,) = harness.audit_lines()
    assert line["outcome"] == "client_disconnected", "audited when the client left"
    assert not stream.closed, "the upstream is still being read"
    assert not stream.finished
    assert harness.reads.active == 1
    stream.gate.set()
    await until(lambda: stream.closed)
    assert stream.finished, "the end of the upstream response was read"
    await until(lambda: len(harness.reads) == 0)
    assert await harness.session_survives_after_the_clock_moves()
    assert len(harness.audit_lines()) == 1, "no second audit line for the detached completion"


async def test_a_stream_that_ends_cleanly_is_forwarded_and_finalised_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    harness = await Harness.create(TrackedStream([EVENT]), caplog, detachable=True)

    async def receive() -> Message:
        await anyio.sleep_forever()
        raise AssertionError("unreachable")

    await run(harness, receive=receive, send=yielding_send)
    await until(lambda: harness.stream.closed)
    (line,) = harness.audit_lines()
    assert line["outcome"] == "forwarded"
    await until(lambda: len(harness.reads) == 0)


class ExplodingRegistry(MemorySessionStore):
    """A registry whose bookkeeping fails between the upstream answering and the relay existing."""

    async def bind(self, upstream: str, session_id: str, subject_key: str) -> bool:
        raise RuntimeError("registry failed")


class NoResolver:
    async def resolve(self, identity: Identity) -> Credential | Denied:
        raise AssertionError("not reached")

    async def invalidate(self, identity: Identity) -> None:
        raise AssertionError("not reached")

    async def aclose(self) -> None:
        return None


async def test_response_is_closed_when_the_setup_after_the_send_raises() -> None:
    stream = TrackedStream([EVENT], hang=True)

    def upstream_answers(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={**SSE, "mcp-session-id": "issued"}, stream=stream)

    config = UpstreamConfig.model_validate(
        {
            "url": "https://upstream.example.com/mcp",
            "connect_timeout_seconds": 1,
            "read_timeout_seconds": 1,
            "write_timeout_seconds": 1,
            "pool_timeout_seconds": 1,
            "max_connections": 10,
            "max_detached_reads": 5,
        }
    )
    sessions = ExplodingRegistry(
        max_entries=10, max_entries_per_subject=10, idle_ttl_seconds=600, clock=Clock()
    )
    proxy = UpstreamProxy(
        UPSTREAM,
        config,
        build_client(config, httpx.MockTransport(upstream_answers)),
        sessions,
        65536,
        65536,
        DetachedReads(Telemetry.noop()),
    )

    async def receive() -> Message:
        await anyio.sleep_forever()
        raise AssertionError("unreachable")

    identity = Identity(subject="alice", email=None, organization=None, groups=(), claims={})
    with pytest.raises(RuntimeError, match="registry failed"), anyio.fail_after(5):
        await proxy.forward(
            method="POST",
            inbound_headers=[],
            body=b"{}",
            receive=receive,
            rpc=RpcInfo(method="initialize", id=1, tool=None, target=None, kind=BodyKind.OBJECT),
            credential=Credential(api_key=SecretStr("k"), upstream=UPSTREAM, labels={}),
            identity=identity,
            resolver=NoResolver(),
            subject_key="alice",
            audit=AuditRecord(http_method="POST", enabled=False),
            tool_policy=None,
        )
    assert stream.closed


async def test_a_relay_whose_reader_cannot_start_closes_the_response(
    caplog: pytest.LogCaptureFixture,
) -> None:
    stream = TrackedStream([EVENT])
    harness = await Harness.create(stream, caplog, detachable=True)
    await harness.reads.aclose(1)

    async def receive() -> Message:
        await anyio.sleep_forever()
        raise AssertionError("unreachable")

    with pytest.raises(ReadsClosedError):
        await run(harness, receive=receive, send=yielding_send)
    assert stream.closed
    assert await harness.session_survives_after_the_clock_moves()
