"""A client disconnect is not a cancellation (decision of 2026-11-20).

Once a POST carrying a JSON-RPC request has gone upstream, the client leaving does not abort it:
the gateway reads the upstream response to its end and discards it. Everything else (a request
not yet forwarded, a GET stream, a DELETE, a notification) is abandoned as before. The fake
upstream here observes what a real one would: whether the end of its response was read, and
whether it was closed.
"""

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from starlette.applications import Starlette

from mcp_gateway.app import create_app
from mcp_gateway.detached import DetachedReads, ReadsClosedError
from mcp_gateway.resolver import StaticResolver
from mcp_gateway.state import MemorySessionStore

from .conftest import (
    FailingStream,
    GatedStream,
    TrackedStream,
    base_config_dict,
    config_from,
    mint,
    until,
)
from .test_app import (
    RPC_BODY,
    SpyResolver,
    asgi_scope,
    audit_lines,
    routed_transport,
)
from .test_observability import metric_points

SSE = {"Content-Type": "text/event-stream"}
JSON_TYPE = {"Content-Type": "application/json"}
EVENT = b'data: {"jsonrpc":"2.0","method":"notifications/progress"}\n\n'
LIST = {"jsonrpc": "2.0", "id": 5, "method": "tools/list"}
LIST_REPLY = json.dumps(
    {"jsonrpc": "2.0", "id": 5, "result": {"tools": [{"name": "search"}]}}
).encode()
ALLOW_ALL = {"default": {"allow": None, "deny": []}, "rules": []}
Handler = Callable[[httpx.Request], Awaitable[httpx.Response]]


class Call:
    """A scripted client: sends its request, then leaves when ``leave`` is set."""

    def __init__(
        self,
        app: Starlette,
        token: str,
        document: dict[str, Any] | None,
        method: str,
        *,
        pre_departed: bool,
    ) -> None:
        self._app = app
        self._body = b"" if document is None else json.dumps(document).encode()
        self._scope = {**asgi_scope(token, self._body), "method": method}
        self._delivered = False
        self.leave = asyncio.Event()
        if pre_departed:
            self.leave.set()
        self.first_body = asyncio.Event()
        self.sent: list[dict[str, Any]] = []

    def add_header(self, name: bytes, value: bytes) -> None:
        self._scope["headers"].append((name, value))

    async def _receive(self) -> dict[str, Any]:
        if not self._delivered:
            self._delivered = True
            return {"type": "http.request", "body": self._body, "more_body": False}
        await self.leave.wait()
        return {"type": "http.disconnect"}

    async def _send(self, message: dict[str, Any]) -> None:
        self.sent.append(message)
        if message["type"] == "http.response.body" and message.get("body"):
            self.first_body.set()

    async def run(self) -> None:
        await asyncio.wait_for(self._app(self._scope, self._receive, self._send), timeout=5)

    def start(self) -> "asyncio.Task[None]":
        return asyncio.create_task(self.run())


class Rig:
    """One gateway over a scripted upstream, with the metrics and the reads in reach."""

    def __init__(
        self,
        mapping_dir: Path,
        idp: Any,
        handler: Handler,
        token: str,
        *,
        resolver: Any = None,
        read_timeout: float = 130,
        policy: dict[str, Any] | None = None,
        max_detached_reads: int = 50,
    ) -> None:
        data = base_config_dict(str(mapping_dir / "mapping.json"))
        data["upstreams"]["main"]["max_detached_reads"] = max_detached_reads
        data["upstreams"]["main"]["read_timeout_seconds"] = read_timeout
        data["policies"]["tools"] = policy
        self.metrics = InMemoryMetricReader()
        self.app: Starlette = create_app(
            config_from(data),
            resolver=resolver,
            http_transport=routed_transport(idp, handler),
            metric_reader=self.metrics,
        )
        self.token = token

    @property
    def reads(self) -> DetachedReads:
        return self.app.state.upstream_reads

    def detached(self) -> dict[str, int]:
        return {
            point.attributes["result"]: point.value
            for point in metric_points(self.metrics, "mcp_gateway.upstream.detached")
        }

    def gauge(self) -> int:
        (point,) = metric_points(self.metrics, "mcp_gateway.upstream.detached.active")
        return point.value

    def call(
        self, document: dict[str, Any] | None, *, method: str = "POST", pre_departed: bool = False
    ) -> Call:
        return Call(self.app, self.token, document, method, pre_departed=pre_departed)

    async def idle(self) -> None:
        """No upstream read is left, whatever its client did: nothing is orphaned."""
        await until(lambda: len(self.reads) == 0)


@pytest.fixture
def token(rsa_key: rsa.RSAPrivateKey) -> str:
    return mint(rsa_key)


def sse_response(stream: GatedStream) -> httpx.Response:
    return httpx.Response(200, headers=SSE, stream=stream)


def assert_one_disconnect_audit_line(caplog: pytest.LogCaptureFixture) -> None:
    (line,) = audit_lines(caplog)
    assert (line["status"], line["outcome"]) == (499, "client_disconnected")


async def finish_and_check(rig: Rig, stream: GatedStream) -> None:
    """Let the upstream finish: its whole response is read, then closed, and nothing is left."""
    assert not stream.closed, "still being read: not closed yet"
    stream.gate.set()
    await until(lambda: stream.closed)
    assert stream.finished, "the end of the upstream response was read"
    await rig.idle()


# --- a request sent upstream runs to its end -------------------------------------------------


async def test_leaving_while_waiting_for_the_response_headers_does_not_abort_the_request(
    mapping_dir: Path, idp: Any, token: str, caplog: pytest.LogCaptureFixture
) -> None:
    stream = GatedStream([EVENT])
    asked = asyncio.Event()
    answer = asyncio.Event()
    aborted: list[bool] = []

    async def slow_upstream(request: httpx.Request) -> httpx.Response:
        asked.set()
        try:
            await answer.wait()
        except asyncio.CancelledError:
            aborted.append(True)
            raise
        return sse_response(stream)

    rig = Rig(mapping_dir, idp, slow_upstream, token)
    call = rig.call(RPC_BODY)
    with caplog.at_level(logging.INFO, logger="mcp_gateway.audit"):
        running = call.start()
        await asked.wait()
        call.leave.set()
        await running
    assert_one_disconnect_audit_line(caplog)
    assert rig.reads.active == 1
    assert not aborted, "the in-flight upstream request must not be cancelled"
    answer.set()
    await stream.started.wait()
    await finish_and_check(rig, stream)
    assert rig.detached() == {"completed": 1}
    assert rig.gauge() == 0


async def test_leaving_mid_sse_relay_does_not_abort_the_request(
    mapping_dir: Path, idp: Any, token: str, caplog: pytest.LogCaptureFixture
) -> None:
    stream = GatedStream([EVENT, EVENT])

    async def upstream(request: httpx.Request) -> httpx.Response:
        return sse_response(stream)

    rig = Rig(mapping_dir, idp, upstream, token)
    call = rig.call(RPC_BODY)
    with caplog.at_level(logging.INFO, logger="mcp_gateway.audit"):
        running = call.start()
        await call.first_body.wait()
        call.leave.set()
        await running
    assert_one_disconnect_audit_line(caplog)
    assert rig.gauge() == 1
    await finish_and_check(rig, stream)
    assert rig.detached() == {"completed": 1}
    assert len(audit_lines(caplog)) == 1, "no second audit line for the detached completion"


async def test_leaving_mid_json_relay_does_not_abort_the_request(
    mapping_dir: Path, idp: Any, token: str, caplog: pytest.LogCaptureFixture
) -> None:
    stream = GatedStream([b'{"jsonrpc":"2.0",'])

    async def upstream(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers=JSON_TYPE, stream=stream)

    rig = Rig(mapping_dir, idp, upstream, token)
    call = rig.call(RPC_BODY)
    with caplog.at_level(logging.INFO, logger="mcp_gateway.audit"):
        running = call.start()
        await call.first_body.wait()
        call.leave.set()
        await running
    assert_one_disconnect_audit_line(caplog)
    await finish_and_check(rig, stream)
    assert rig.detached() == {"completed": 1}


async def test_leaving_during_a_filtered_tool_list_read_does_not_abort_the_request(
    mapping_dir: Path, idp: Any, token: str, caplog: pytest.LogCaptureFixture
) -> None:
    stream = GatedStream([LIST_REPLY[:20]])

    async def upstream(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers=JSON_TYPE, stream=stream)

    rig = Rig(mapping_dir, idp, upstream, token, policy=ALLOW_ALL)
    call = rig.call(LIST)
    with caplog.at_level(logging.INFO, logger="mcp_gateway.audit"):
        running = call.start()
        await stream.started.wait()
        call.leave.set()
        await running
    assert_one_disconnect_audit_line(caplog)
    await finish_and_check(rig, stream)
    assert rig.detached() == {"completed": 1}


async def test_leaving_during_the_drain_of_a_failed_tool_list_does_not_abort_the_request(
    mapping_dir: Path, idp: Any, token: str, caplog: pytest.LogCaptureFixture
) -> None:
    stream = GatedStream([b"boom"])

    async def upstream(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, stream=stream)

    rig = Rig(mapping_dir, idp, upstream, token, policy=ALLOW_ALL)
    call = rig.call(LIST)
    with caplog.at_level(logging.INFO, logger="mcp_gateway.audit"):
        running = call.start()
        await stream.started.wait()
        call.leave.set()
        await running
    assert_one_disconnect_audit_line(caplog)
    await finish_and_check(rig, stream)
    assert rig.detached() == {"completed": 1}


async def test_leaving_during_the_drain_of_an_upstream_401_still_invalidates_the_credential(
    mapping_dir: Path, idp: Any, token: str, caplog: pytest.LogCaptureFixture
) -> None:
    stream = GatedStream([b"denied"])

    async def upstream(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, stream=stream)

    resolver = SpyResolver(
        StaticResolver(mapping_dir / "mapping.json", ["main"], email_matching=True)
    )
    rig = Rig(mapping_dir, idp, upstream, token, resolver=resolver)
    call = rig.call(RPC_BODY)
    with caplog.at_level(logging.INFO, logger="mcp_gateway.audit"):
        running = call.start()
        await stream.started.wait()
        call.leave.set()
        await running
    assert_one_disconnect_audit_line(caplog)
    assert len(resolver.invalidated) == 1, "the credential was rejected, whoever is listening"
    await finish_and_check(rig, stream)
    assert rig.detached() == {"completed": 1}


# --- the ceiling ----------------------------------------------------------------------------


async def test_a_detached_read_that_never_finishes_is_closed_at_the_ceiling(
    mapping_dir: Path, idp: Any, token: str, caplog: pytest.LogCaptureFixture
) -> None:
    stream = GatedStream([EVENT], never_ends=True)

    async def upstream(request: httpx.Request) -> httpx.Response:
        return sse_response(stream)

    rig = Rig(mapping_dir, idp, upstream, token, read_timeout=0.3)
    call = rig.call(RPC_BODY)
    with caplog.at_level(logging.INFO):
        running = call.start()
        await call.first_body.wait()
        call.leave.set()
        await running
        await until(lambda: stream.closed)
    assert not stream.finished
    await rig.idle()
    assert rig.detached() == {"timeout": 1}
    assert rig.gauge() == 0
    assert "result=timeout" in caplog.text


# --- what is still abandoned -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "document"),
    [
        ("GET", None),
        ("DELETE", None),
        ("POST", {"jsonrpc": "2.0", "method": "notifications/progress"}),
    ],
)
async def test_a_stream_that_has_no_request_to_complete_is_closed_when_the_client_leaves(
    mapping_dir: Path,
    idp: Any,
    token: str,
    caplog: pytest.LogCaptureFixture,
    method: str,
    document: dict[str, Any] | None,
) -> None:
    stream = GatedStream([EVENT], never_ends=True)

    async def upstream(request: httpx.Request) -> httpx.Response:
        return sse_response(stream)

    rig = Rig(mapping_dir, idp, upstream, token)
    call = rig.call(document, method=method)
    with caplog.at_level(logging.INFO, logger="mcp_gateway.audit"):
        running = call.start()
        await call.first_body.wait()
        call.leave.set()
        await running
    assert stream.closed, "closed before the request returned"
    assert not stream.finished
    assert len(rig.reads) == 0
    assert_one_disconnect_audit_line(caplog)
    assert rig.detached() == {}


async def test_a_request_that_was_not_forwarded_yet_is_abandoned(
    mapping_dir: Path, idp: Any, token: str
) -> None:
    sent: list[httpx.Request] = []

    async def upstream(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        raise AssertionError("not reached")

    class Slow:
        async def resolve(self, identity: Any) -> Any:
            await asyncio.Event().wait()

        async def invalidate(self, identity: Any) -> None:
            return None

        async def aclose(self) -> None:
            return None

    rig = Rig(mapping_dir, idp, upstream, token, resolver=Slow())
    await rig.call(RPC_BODY, pre_departed=True).run()
    assert sent == []
    assert len(rig.reads) == 0
    assert rig.detached() == {}


# --- cancellation is explicit -----------------------------------------------------------------


@pytest.mark.parametrize("policy", [None, ALLOW_ALL])
async def test_notifications_cancelled_is_forwarded_like_any_notification(
    mapping_dir: Path, idp: Any, token: str, policy: dict[str, Any] | None
) -> None:
    sent: list[httpx.Request] = []

    async def upstream(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(202, stream=TrackedStream([]))

    rig = Rig(mapping_dir, idp, upstream, token, policy=policy)
    cancel = {
        "jsonrpc": "2.0",
        "method": "notifications/cancelled",
        "params": {"requestId": 7, "reason": "user stopped it"},
    }
    call = rig.call(cancel)
    await call.run()
    (request,) = sent
    assert json.loads(request.content) == cancel
    (start,) = [m for m in call.sent if m["type"] == "http.response.start"]
    assert start["status"] == 202


# --- shutdown -----------------------------------------------------------------------------------


async def test_closing_the_application_cancels_and_awaits_the_detached_reads(
    mapping_dir: Path, idp: Any, token: str
) -> None:
    stream = GatedStream([EVENT], never_ends=True)

    async def upstream(request: httpx.Request) -> httpx.Response:
        return sse_response(stream)

    rig = Rig(mapping_dir, idp, upstream, token)
    async with rig.app.router.lifespan_context(rig.app):
        call = rig.call(RPC_BODY)
        running = call.start()
        await call.first_body.wait()
        call.leave.set()
        await running
        assert rig.reads.active == 1
        assert not stream.closed
    assert stream.closed, "the lifespan closed the response of the read it cancelled"
    assert len(rig.reads) == 0
    assert rig.detached() == {"failed": 1}


# --- a request that was fully delivered is not a detached one ---------------------------------


async def test_a_request_whose_client_stayed_to_the_end_is_not_counted_as_detached(
    mapping_dir: Path, idp: Any, token: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    touched = asyncio.Event()

    async def check(self: Any, upstream: str, session_id: str, subject_key: str) -> bool:
        return True

    async def slow_touch(self: Any, upstream: str, session_id: str) -> None:
        await asyncio.sleep(0.02)  # a shared-state round trip suspends
        touched.set()

    monkeypatch.setattr(MemorySessionStore, "check", check)
    monkeypatch.setattr(MemorySessionStore, "touch", slow_touch)
    stream = GatedStream([EVENT])
    stream.gate.set()

    async def upstream(request: httpx.Request) -> httpx.Response:
        return sse_response(stream)

    rig = Rig(mapping_dir, idp, upstream, token)
    call = rig.call(RPC_BODY)
    call.add_header(b"mcp-session-id", b"s-1")
    await call.run()
    await rig.idle()
    assert touched.is_set(), "the session binding was refreshed"
    assert rig.detached() == {}


@pytest.mark.parametrize(
    ("error", "result"),
    [(httpx.ReadError("reset"), "failed"), (httpx.ReadTimeout("silent"), "timeout")],
)
@pytest.mark.parametrize("kind", ["sse", "json"])
async def test_an_upstream_that_breaks_after_the_client_left_is_not_counted_completed(
    mapping_dir: Path, idp: Any, token: str, kind: str, error: Exception, result: str
) -> None:
    stream = FailingStream([EVENT if kind == "sse" else b'{"jsonrpc":'], error)
    headers = SSE if kind == "sse" else JSON_TYPE

    async def upstream(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers=headers, stream=stream)

    rig = Rig(mapping_dir, idp, upstream, token)
    call = rig.call(RPC_BODY)
    running = call.start()
    await call.first_body.wait()
    call.leave.set()
    await running
    stream.gate.set()
    await rig.idle()
    assert rig.detached() == {result: 1}


# --- a request whose client left before it was sent is never sent -------------------------------


async def test_a_client_that_left_during_the_session_check_is_not_forwarded(
    mapping_dir: Path, idp: Any, token: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    checking = asyncio.Event()
    sent: list[httpx.Request] = []

    async def slow_check(self: Any, upstream: str, session_id: str, subject_key: str) -> bool:
        checking.set()
        await asyncio.sleep(0.05)  # a shared-state round trip
        return True

    async def upstream(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, headers=JSON_TYPE, stream=GatedStream([b"{}"]))

    monkeypatch.setattr(MemorySessionStore, "check", slow_check)
    rig = Rig(mapping_dir, idp, upstream, token)
    call = rig.call(RPC_BODY)
    call.add_header(b"mcp-session-id", b"s-1")
    running = call.start()
    await checking.wait()
    call.leave.set()
    await running
    await rig.idle()
    assert sent == [], "nothing was in flight, so nothing is sent for a client that is gone"
    assert rig.detached() == {}


# --- the cap on detached reads ----------------------------------------------------------------


async def test_beyond_the_cap_a_leaving_clients_read_is_shed_not_detached(
    mapping_dir: Path, idp: Any, token: str, caplog: pytest.LogCaptureFixture
) -> None:
    streams = [GatedStream([EVENT]) for _ in range(3)]
    handed_out = iter(streams)

    async def upstream(request: httpx.Request) -> httpx.Response:
        return sse_response(next(handed_out))

    rig = Rig(mapping_dir, idp, upstream, token, max_detached_reads=2)
    calls = [rig.call(RPC_BODY) for _ in streams]
    with caplog.at_level(logging.INFO):
        for call in calls:
            running = call.start()
            await call.first_body.wait()
            call.leave.set()
            await running
        assert rig.gauge() == 2
        assert rig.detached() == {"shed": 1}
        assert "result=shed" in caplog.text
    assert [s.closed for s in streams] == [False, False, True], "the third was cut, not detached"
    for stream in streams[:2]:
        stream.gate.set()
    await rig.idle()
    assert rig.detached() == {"shed": 1, "completed": 2}
    assert all(s.closed for s in streams)


# --- ownership --------------------------------------------------------------------------------


async def test_cancelling_the_request_task_leaves_the_read_it_started_to_finish(
    mapping_dir: Path, idp: Any, token: str
) -> None:
    stream = GatedStream([LIST_REPLY[:20]])

    async def upstream(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers=JSON_TYPE, stream=stream)

    rig = Rig(mapping_dir, idp, upstream, token, policy=ALLOW_ALL)
    running = rig.call(LIST).start()
    await stream.started.wait()
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    assert not stream.closed, "the response belongs to the read, not to the cancelled request"
    await finish_and_check(rig, stream)
    assert rig.detached() == {"completed": 1}


async def test_a_read_refused_after_shutdown_began_closes_the_upstream_response(
    mapping_dir: Path, idp: Any, token: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    stream = GatedStream([LIST_REPLY])
    stream.gate.set()
    original = DetachedReads.start
    starts = 0

    def refusing_after_the_send(self: DetachedReads, **kwargs: Any) -> Any:
        nonlocal starts
        starts += 1
        if starts > 1:
            raise ReadsClosedError("the upstream reads are closed")
        return original(self, **kwargs)

    monkeypatch.setattr(DetachedReads, "start", refusing_after_the_send)

    async def upstream(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers=JSON_TYPE, stream=stream)

    rig = Rig(mapping_dir, idp, upstream, token, policy=ALLOW_ALL)
    call = rig.call(LIST)
    await call.run()
    assert stream.closed, "the response whose read could not start is closed, not leaked"
    assert [m["status"] for m in call.sent if m["type"] == "http.response.start"] == [500]


# --- what a response that arrives after the client left still does ------------------------------


async def answer_after_leaving(
    rig_factory: Callable[[Handler], Rig],
    response: Callable[[], httpx.Response],
    *,
    session: bool = False,
    document: dict[str, Any] = RPC_BODY,
) -> Rig:
    asked = asyncio.Event()
    answer = asyncio.Event()

    async def upstream(request: httpx.Request) -> httpx.Response:
        asked.set()
        await answer.wait()
        return response()

    rig = rig_factory(upstream)
    call = rig.call(document)
    if session:
        call.add_header(b"mcp-session-id", b"s-1")
    running = call.start()
    await asked.wait()
    call.leave.set()
    await running
    answer.set()
    await rig.idle()
    return rig


async def test_a_401_that_arrives_after_the_client_left_still_invalidates_the_credential(
    mapping_dir: Path, idp: Any, token: str
) -> None:
    stream = GatedStream([b"denied"])
    stream.gate.set()
    resolver = SpyResolver(
        StaticResolver(mapping_dir / "mapping.json", ["main"], email_matching=True)
    )
    await answer_after_leaving(
        lambda handler: Rig(mapping_dir, idp, handler, token, resolver=resolver),
        lambda: httpx.Response(401, stream=stream),
    )
    assert len(resolver.invalidated) == 1
    assert stream.closed


async def test_a_404_that_arrives_after_the_client_left_still_drops_the_session_binding(
    mapping_dir: Path, idp: Any, token: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    dropped: list[str] = []

    async def check(self: Any, upstream: str, session_id: str, subject_key: str) -> bool:
        return True

    async def drop(self: Any, upstream: str, session_id: str) -> None:
        dropped.append(session_id)

    monkeypatch.setattr(MemorySessionStore, "check", check)
    monkeypatch.setattr(MemorySessionStore, "drop", drop)
    stream = GatedStream([b"gone"])
    stream.gate.set()
    await answer_after_leaving(
        lambda handler: Rig(mapping_dir, idp, handler, token),
        lambda: httpx.Response(404, stream=stream),
        session=True,
    )
    assert dropped == ["s-1"]
    assert stream.closed


async def test_a_session_issued_after_the_client_left_is_not_bound(
    mapping_dir: Path, idp: Any, token: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    bound: list[str] = []

    async def bind(self: Any, upstream: str, session_id: str, subject_key: str) -> bool:
        bound.append(session_id)
        return True

    monkeypatch.setattr(MemorySessionStore, "bind", bind)
    stream = GatedStream([b"{}"])
    stream.gate.set()
    await answer_after_leaving(
        lambda handler: Rig(mapping_dir, idp, handler, token),
        lambda: httpx.Response(
            200, headers={**JSON_TYPE, "mcp-session-id": "issued"}, stream=stream
        ),
        document={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
    )
    assert bound == [], "the client never learned this session id: it expires upstream by itself"
    assert stream.closed
