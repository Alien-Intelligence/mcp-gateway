"""Streaming reverse proxy to one upstream MCP server.

Responses are relayed as they come. The one exception is a ``tools/list`` reply while a tool
policy is active: it is filtered by ``tool_filter`` (the relay mechanics stay here).
"""

import asyncio
import logging
from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

import anyio
import httpx
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.types import Receive, Scope, Send

from mcp_gateway.audit import AuditRecord
from mcp_gateway.config import UpstreamConfig
from mcp_gateway.detached import DetachedReads, ReadHandle
from mcp_gateway.headers import (
    build_upstream_headers,
    filter_response_headers,
    has_content_encoding,
)
from mcp_gateway.identity import Identity
from mcp_gateway.jsonrpc import (
    GATEWAY_ERROR,
    UPSTREAM_PAYMENT_REQUIRED,
    UPSTREAM_TIMEOUT,
    UPSTREAM_UNAUTHORIZED,
    UPSTREAM_UNREACHABLE,
    RpcInfo,
    error_body,
)
from mcp_gateway.resolver import Credential, CredentialResolver, ResolverError
from mcp_gateway.sse import (
    EncodedStreamError,
    SseError,
    SseEventSplitter,
    stream_failed_event,
)
from mcp_gateway.state import SessionStore, StateUnavailableError
from mcp_gateway.tool_filter import (
    SseListFilter,
    ToolPolicyError,
    filter_json_reply,
    policy_failed_body,
    policy_failed_event,
)
from mcp_gateway.tool_policy import EffectivePolicy

logger = logging.getLogger(__name__)

# Upstream 401/402 bodies are drained (to free the connection) but never relayed.
_DRAIN_LIMIT_BYTES = 64 * 1024
_CLOSE_TIMEOUT_SECONDS = 5.0

UPSTREAM_ERROR_MESSAGE = "The service returned an error for this request."

_AUTH_REPLIES: dict[int, tuple[int, str]] = {
    401: (
        UPSTREAM_UNAUTHORIZED,
        "The credential configured for this account was rejected by the service. "
        "Contact your administrator.",
    ),
    402: (
        UPSTREAM_PAYMENT_REQUIRED,
        "The account behind this connection cannot be charged for this request. "
        "Contact your administrator.",
    ),
}


def build_client(
    config: UpstreamConfig, transport: httpx.AsyncBaseTransport | None
) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=transport,
        timeout=httpx.Timeout(
            connect=config.connect_timeout_seconds,
            read=config.read_timeout_seconds,
            write=config.write_timeout_seconds,
            pool=config.pool_timeout_seconds,
        ),
        limits=httpx.Limits(
            max_connections=config.max_connections,
            max_keepalive_connections=config.max_connections,
        ),
        follow_redirects=False,
    )


def json_rpc_response(
    status: int,
    code: int,
    message: str,
    rpc_id: str | int | None,
    data: dict[str, Any] | None = None,
) -> JSONResponse:
    return JSONResponse(error_body(code, message, rpc_id, data), status_code=status)


async def _close(response: httpx.Response) -> None:
    """Close the upstream response even when the surrounding task is being cancelled."""
    with anyio.CancelScope(shield=True), anyio.move_on_after(_CLOSE_TIMEOUT_SECONDS) as scope:
        await response.aclose()
    if scope.cancelled_caught:
        logger.warning("closing the upstream response timed out")


async def watching_disconnect[T](
    receive: Receive, operation: Callable[[], Awaitable[T]]
) -> tuple[T | None, bool]:
    """Run ``operation`` while listening for the client going away.

    Returns ``(result, disconnected)``. On a disconnect the operation is cancelled and the
    result is None; a result that completed in the same instant is still returned so the
    caller can release it. Exceptions from ``operation`` propagate unchanged.
    """
    outcome: list[T] = []
    failure: list[BaseException] = []
    disconnected = False

    async def watch(scope: anyio.CancelScope) -> None:
        nonlocal disconnected
        while (await receive())["type"] != "http.disconnect":
            pass
        disconnected = True
        scope.cancel()

    async def run(scope: anyio.CancelScope) -> None:
        try:
            outcome.append(await operation())
        except Exception as exc:  # re-raised below, outside the task group
            failure.append(exc)
        finally:
            scope.cancel()

    async with anyio.create_task_group() as group:
        group.start_soon(watch, group.cancel_scope)
        group.start_soon(run, group.cancel_scope)
    if failure:
        raise failure[0]
    return (outcome[0] if outcome else None), disconnected


async def _drain(response: httpx.Response) -> None:
    received = 0
    async for chunk in response.aiter_raw():
        received += len(chunk)
        if received > _DRAIN_LIMIT_BYTES:
            break


async def _drain_and_close(response: httpx.Response) -> None:
    try:
        await _drain(response)
    finally:
        await _close(response)


async def _discard(response: httpx.Response) -> None:
    """Read the rest of the response and throw it away, then close it, whatever happens."""
    try:
        async for _chunk in response.aiter_raw():
            pass
    finally:
        await _close(response)


async def _until_disconnect(receive: Receive) -> None:
    while (await receive())["type"] != "http.disconnect":
        pass


async def client_already_gone(receive: Receive) -> bool:
    """True when the client's disconnect is already queued: one look, one scheduling checkpoint."""
    watcher = asyncio.create_task(_until_disconnect(receive))
    try:
        await asyncio.sleep(0)
        if watcher.done():
            watcher.result()
            return True
        return False
    finally:
        watcher.cancel()


async def await_read[T](read: ReadHandle[T], receive: Receive) -> tuple[T | None, bool]:
    """Wait for an owned upstream read to end, or for the client to leave: whichever is first.

    Returns ``(result, client_gone)``. When the client leaves first the read is handed over
    (``ReadHandle.leave``): it carries on detached, or is cancelled when it must not outlive the
    client, and the result is None. A result that is ready in the same instant is still returned.
    Exceptions raised by the read propagate unchanged.
    """
    watcher = asyncio.create_task(_until_disconnect(receive))
    try:
        await asyncio.wait({read.task, watcher}, return_when=asyncio.FIRST_COMPLETED)
        if watcher.done():
            watcher.result()  # a failing receive is raised, not mistaken for a disconnect
        if read.task.done():
            return read.task.result(), watcher.done()
        read.leave()
        await read.settled()
        return None, True
    except BaseException:
        read.leave()
        raise
    finally:
        watcher.cancel()


class UpstreamStreamAbortedError(Exception):
    """Raised out of the response body so the ASGI server drops the connection.

    Without a terminating chunk the client sees an incomplete response, never a clean end.
    """


def _is_event_stream(response: httpx.Response) -> bool:
    content_type = response.headers.get("content-type", "")
    return content_type.split(";", 1)[0].strip().lower() == "text/event-stream"


def _is_json(response: httpx.Response) -> bool:
    content_type = response.headers.get("content-type", "")
    return content_type.split(";", 1)[0].strip().lower() == "application/json"


def _request_session_id(headers: list[tuple[str, str]]) -> str | None:
    values = [value for name, value in headers if name.lower() == "mcp-session-id"]
    return values[0] if values else None


@dataclass
class _Ownership:
    """Who must close an upstream response: ``forward`` until a read takes it over."""

    taken: bool = False


class _Release:
    """Releases one upstream response and records the request, each exactly once.

    ``release_response`` is called by whoever read the response last (the read task, which may
    outlive the client); ``record_outcome`` by the request side, when it is done with the client.
    """

    def __init__(
        self,
        response: httpx.Response,
        sessions: SessionStore,
        upstream: str,
        session_id: str | None,
        audit: AuditRecord,
    ) -> None:
        self._response = response
        self._sessions = sessions
        self._upstream = upstream
        self._session_id = session_id
        self._audit = audit
        self._released = False
        # Set by the relay when the upstream body was relayed to its end.
        self.completed = False

    async def _touch_session(self, session_id: str) -> None:
        try:
            await self._sessions.touch(self._upstream, session_id)
        except StateUnavailableError:
            # The client already has its answer, so there is nothing to fail: the binding is
            # simply not refreshed and expires on its idle TTL.
            logger.error("session binding not refreshed: shared state unavailable")

    async def release_response(self) -> None:
        """Close the upstream response and refresh the session binding (shielded from cancellation)."""
        if self._released:
            return
        self._released = True
        with anyio.CancelScope(shield=True):
            try:
                await _close(self._response)
            finally:
                if self._session_id is not None:
                    await self._touch_session(self._session_id)

    def record_outcome(self, *, delivered: bool) -> None:
        """Audit the request: forwarded when the client got all of it, else the client left."""
        if self.completed and delivered:
            self._audit.emit(status=self._response.status_code, outcome="forwarded")
        else:
            # A failure was audited already (emit is first-wins); this is the client leaving.
            self._audit.emit(status=499, outcome="client_disconnected")


@dataclass(frozen=True)
class _Abort:
    """Put in the relay's stream by the reader: the client's connection must be dropped."""

    reason: str


type _RelayItem = bytes | _Abort

# One event of look-ahead: the reader stays at most one event ahead of a slow client.
_RELAY_BUFFER_EVENTS = 1


class _Outlet:
    """Where a reader sends what it relays: the client's stream, until the client is gone.

    Once the client has left (its end of the stream is closed, or the read was detached) nothing
    is sent any more and the reader only discards what the upstream sends.
    """

    def __init__(self, handle: ReadHandle[None], sink: MemoryObjectSendStream[_RelayItem]) -> None:
        self._handle = handle
        self._sink = sink
        self._broken = False

    @property
    def open(self) -> bool:
        return not self._broken and not self._handle.detached

    def close(self) -> None:
        """End the client's stream: it delivers what was put in it, then finishes."""
        self._sink.close()

    async def put(self, item: _RelayItem) -> None:
        if not self.open:
            return
        try:
            await self._sink.send(item)
        except anyio.BrokenResourceError:
            self._broken = True


async def _client_events(
    source: MemoryObjectReceiveStream[_RelayItem],
) -> AsyncGenerator[bytes, None]:
    """What the client is sent: the reader's items, in order, until it closes its end."""
    async with source:
        async for item in source:
            if isinstance(item, _Abort):
                raise UpstreamStreamAbortedError(item.reason)
            yield item


class RelayResponse(StreamingResponse):
    """Streaming response fed by a reader task that owns the upstream response.

    Starlette only notices a disconnect on the next write, so an idle SSE stream would keep
    the upstream connection open until the next event. Here a disconnect is noticed at once and
    handed to the reader: for a request that was sent upstream it keeps reading and discards
    (the maintainers' decision, see ``ReadHandle``), otherwise it is cancelled. Either way the upstream
    response is released by the reader, whatever way the relay ends.
    """

    def __init__(
        self,
        source: MemoryObjectReceiveStream[_RelayItem],
        start_reader: Callable[[], ReadHandle[None]],
        release: _Release,
        status_code: int,
        headers: Mapping[str, str],
    ) -> None:
        events = _client_events(source)
        super().__init__(events, status_code=status_code, headers=headers)
        self._events = events
        self._source = source
        self._start_reader = start_reader
        self._release = release

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        reader = self._start_reader()
        failure: list[Exception] = []
        delivered = False
        try:
            async with anyio.create_task_group() as group:

                async def pump() -> None:
                    nonlocal delivered
                    try:
                        await self.stream_response(send)
                        delivered = True
                    except OSError:
                        logger.info("client disconnected while streaming")
                    except Exception as exc:  # re-raised below, outside the task group
                        failure.append(exc)
                    finally:
                        group.cancel_scope.cancel()

                async def watch() -> None:
                    await self.listen_for_disconnect(receive)
                    group.cancel_scope.cancel()

                group.start_soon(pump)
                group.start_soon(watch)
        finally:
            with anyio.CancelScope(shield=True):
                await self._events.aclose()
                self._source.close()
                if not delivered:
                    # A client that got everything has nothing to be detached from.
                    reader.leave()
                await reader.settled()
                self._release.record_outcome(delivered=delivered)
        if failure:
            raise failure[0]


def _wire_headers(headers: list[tuple[str, str]]) -> list[tuple[bytes, bytes]]:
    """Encode headers as the ASGI server decoded them (latin-1), so inbound bytes survive intact.

    httpx would refuse a non-ASCII ``str`` value; forwarding the raw bytes keeps the proxy
    transparent and leaves validation to the upstream HTTP stack.
    """
    return [(name.encode("latin-1"), value.encode("latin-1")) for name, value in headers]


class UpstreamProxy:
    def __init__(
        self,
        name: str,
        config: UpstreamConfig,
        client: httpx.AsyncClient,
        sessions: SessionStore,
        max_event_bytes: int,
        max_filtered_bytes: int,
        reads: DetachedReads,
    ) -> None:
        self._name = name
        self._url = config.url
        self._client = client
        self._sessions = sessions
        self._max_event_bytes = max_event_bytes
        self._max_filtered_bytes = max_filtered_bytes
        self._read_timeout = config.read_timeout_seconds
        self._reads = reads
        self._max_detached = config.max_detached_reads

    def _own[T](
        self, detachable: bool, body: Callable[[ReadHandle[T]], Awaitable[T]]
    ) -> ReadHandle[T]:
        """Run ``body`` as a read owned by the application (see ``DetachedReads``)."""
        return self._reads.start(
            upstream=self._name,
            ceiling_seconds=self._read_timeout,
            detachable=detachable,
            detach_limit=self._max_detached,
            body=body,
        )

    async def _send_or_discard(
        self, request: httpx.Request, handle: ReadHandle[httpx.Response | None]
    ) -> httpx.Response | None:
        """Send the request and return the response; if the client left meanwhile, finish it here."""
        response = await self._client.send(request, stream=True)
        if handle.detached:
            await _discard(response)
            return None
        return response

    def _own_response[T](
        self,
        response: httpx.Response,
        ownership: _Ownership,
        detachable: bool,
        body: Callable[[ReadHandle[T]], Awaitable[T]],
    ) -> ReadHandle[T]:
        """Start the read that takes the response over (its body closes it)."""
        read = self._own(detachable, body)
        ownership.taken = True
        return read

    async def _release_unowned(self, response: httpx.Response, *, detachable: bool) -> None:
        """Dispose of a response that arrived in the instant its client left."""
        if not detachable:
            await _close(response)
            return
        self._own(True, lambda _handle: _discard(response)).leave()

    async def forward(
        self,
        *,
        method: str,
        inbound_headers: list[tuple[str, str]],
        body: bytes,
        receive: Receive,
        rpc: RpcInfo,
        credential: Credential,
        identity: Identity,
        resolver: CredentialResolver,
        subject_key: str,
        audit: AuditRecord,
        tool_policy: EffectivePolicy | None,
    ) -> Response:
        """Send the request upstream and relay the answer.

        ``tool_policy`` is the caller's effective tool policy, None when no policy is configured.
        """
        request = self._client.build_request(
            method,
            self._url,
            headers=_wire_headers(build_upstream_headers(inbound_headers, credential.api_key)),
            content=body or None,
        )
        audit.upstream = self._name
        reply_id = rpc.id if rpc.expects_reply else None
        if await client_already_gone(receive):
            # It left while the gateway was still deciding (session check, policy): nothing was
            # in flight, so nothing is sent.
            return self._client_gone(audit)
        # Only a POST carrying a request is a unit of work the upstream is asked to do; a listen
        # stream, a DELETE and a notification have nothing to complete. For the former a client
        # disconnect does not abort the upstream request (decision of 2026-11-20: "disconnect is not a
        # cancelation"; a deliberate departure from the 2026-07-28 rule that closing the stream
        # cancels): see ``ReadHandle.leave``. A request not yet sent is abandoned as before.
        detachable = method == "POST" and rpc.expects_reply
        step = audit.observer.begin_upstream(self._name)
        try:
            read = self._own(detachable, lambda handle: self._send_or_discard(request, handle))
            response, gone = await await_read(read, receive)
        except httpx.TimeoutException as exc:
            step.finish(status=None, error="timeout")
            logger.error("upstream %s timed out (%s)", self._name, type(exc).__name__)
            audit.emit(status=504, outcome="upstream_error")
            return json_rpc_response(
                504, UPSTREAM_TIMEOUT, "The service did not answer in time.", reply_id
            )
        except httpx.TransportError as exc:
            step.finish(status=None, error="unreachable")
            logger.error("upstream %s unreachable (%s)", self._name, type(exc).__name__)
            audit.emit(status=502, outcome="upstream_error")
            return json_rpc_response(
                502, UPSTREAM_UNREACHABLE, "The service could not be reached.", reply_id
            )
        except BaseException:
            step.finish(status=None, error="aborted")
            raise
        if gone:
            step.finish(status=None if response is None else response.status_code)
            if response is not None:
                await self._release_unowned(response, detachable=detachable)
            return self._client_gone(audit)
        if response is None:
            step.finish(status=None, error="unreachable")
            raise RuntimeError("upstream send finished without a response or a disconnect")

        ownership = _Ownership()
        try:
            step.finish(status=response.status_code)
            audit.upstream_status = response.status_code
            audit.upstream_request_id = response.headers.get("x-request-id")
            audit.upstream_session = "mcp-session-id" in response.headers
            session_id = _request_session_id(inbound_headers)
            if response.status_code in _AUTH_REPLIES:
                return await self._reject(
                    response, ownership, receive, rpc, identity, resolver, audit, detachable
                )
            await self._track_session(method, session_id, response, subject_key)
            if tool_policy is not None and rpc.method == "tools/list" and rpc.expects_reply:
                if not response.is_success:
                    return await self._replace_failure(
                        response, ownership, rpc, receive, audit, detachable
                    )
                if not _is_event_stream(response):
                    # A JSON body answers exactly its own POST: only a tools/list one is filtered.
                    return await self._respond_filtered(
                        response,
                        ownership,
                        rpc,
                        tool_policy,
                        session_id,
                        receive,
                        audit,
                        detachable,
                    )
            return self.stream(response, rpc, session_id, audit, tool_policy, detachable=detachable)
        except BaseException:
            # Close it rather than leak the connection, unless a read owns it now: that read
            # closes it when it ends, and must not be cut short by this request failing.
            if not ownership.taken:
                await _close(response)
            raise

    async def _track_session(
        self,
        method: str,
        session_id: str | None,
        response: httpx.Response,
        subject_key: str,
    ) -> None:
        """Keep the registry in step with what the upstream says about the session.

        Binding a new session raises ``StateUnavailableError``: the caller closes the upstream
        response and answers 503, so a session id the gateway could not bind is never relayed.
        """
        status = response.status_code
        issued = response.headers.get("mcp-session-id")
        if session_id is not None and (
            status == 404 or (method == "DELETE" and 200 <= status < 300)
        ):
            # The upstream no longer knows the session, or the client just ended it.
            try:
                await self._sessions.drop(self._name, session_id)
            except StateUnavailableError:
                # The response is decided (the upstream said 404 or the DELETE succeeded) and
                # is relayed as is; a stale binding costs at most one more upstream 404, then it expires.
                logger.error("session binding not dropped: shared state unavailable")
        elif issued is not None and 200 <= status < 300:
            await self._sessions.bind(self._name, issued, subject_key)

    @staticmethod
    def _client_gone(audit: AuditRecord) -> Response:
        logger.info("client disconnected while waiting for the upstream")
        audit.emit(status=499, outcome="client_disconnected")
        return Response(status_code=499)

    async def _reject(
        self,
        response: httpx.Response,
        ownership: _Ownership,
        receive: Receive,
        rpc: RpcInfo,
        identity: Identity,
        resolver: CredentialResolver,
        audit: AuditRecord,
        detachable: bool,
    ) -> Response:
        upstream_status = response.status_code
        gone = False
        try:
            _, gone = await await_read(
                self._own_response(
                    response, ownership, detachable, lambda _handle: _drain_and_close(response)
                ),
                receive,
            )
        except httpx.HTTPError as exc:
            logger.warning(
                "draining upstream %d body failed (%s)", upstream_status, type(exc).__name__
            )
        # The upstream rejected the credential whether or not the client is still there to be told.
        try:
            await resolver.invalidate(identity)
        except ResolverError:
            logger.exception("credential invalidation failed after upstream %d", upstream_status)
        if gone:
            return self._client_gone(audit)
        code, message = _AUTH_REPLIES[upstream_status]
        status = 200 if rpc.expects_reply else 502
        audit.emit(status=status, outcome="upstream_auth_rejected")
        return json_rpc_response(
            status,
            code,
            message,
            rpc.id if rpc.expects_reply else None,
            {"upstream_status": upstream_status},
        )

    async def _drain_within_read_timeout(self, response: httpx.Response) -> None:
        try:
            with anyio.fail_after(self._read_timeout):
                await _drain(response)
        finally:
            await _close(response)

    async def _replace_failure(
        self,
        response: httpx.Response,
        ownership: _Ownership,
        rpc: RpcInfo,
        receive: Receive,
        audit: AuditRecord,
        detachable: bool,
    ) -> Response:
        """Answer a failed ``tools/list`` with the gateway's error: the status stays, the body goes.

        A failure body is free-form text or JSON the gateway cannot vouch for, and one that
        looks like a tool list must not reach the client. The status is kept (a ``404`` still
        tells the client its session ended), and so is ``Retry-After``.
        """
        reply_id = rpc.id
        if reply_id is None:
            raise RuntimeError("a filtered tools/list always carries a request id")
        status = response.status_code
        gone = False
        try:
            _, gone = await await_read(
                self._own_response(
                    response,
                    ownership,
                    detachable,
                    lambda _handle: self._drain_within_read_timeout(response),
                ),
                receive,
            )
        except (httpx.HTTPError, TimeoutError) as exc:
            logger.warning("draining upstream %d body failed (%s)", status, type(exc).__name__)
        if gone:
            return self._client_gone(audit)
        audit.reason = "upstream_status"
        audit.emit(status=status, outcome="upstream_error")
        reply = json_rpc_response(status, GATEWAY_ERROR, UPSTREAM_ERROR_MESSAGE, reply_id)
        retry_after = response.headers.get("retry-after")
        if retry_after is not None:
            reply.headers["Retry-After"] = retry_after
        return reply

    async def _read_filterable_body(
        self, response: httpx.Response, handle: ReadHandle[bytes], release: _Release
    ) -> bytes:
        """The whole upstream body, within ``max_filtered_response_bytes`` and the read timeout.

        Once the client has left the body is still read to its end but no longer kept (the
        answer has nobody to go to), and the response is released here, whenever that is.
        """
        try:
            if not _is_json(response):
                raise ToolPolicyError("unexpected_content_type")
            if has_content_encoding(response.headers.multi_items()):
                raise ToolPolicyError("encoded_reply")
            declared = response.headers.get("content-length")
            if (
                declared is not None
                and declared.isdigit()
                and int(declared) > self._max_filtered_bytes
            ):
                raise ToolPolicyError("reply_too_large")
            chunks: list[bytes] = []
            total = 0
            # The httpx read timeout bounds each silence; this bounds a body that trickles in.
            with anyio.fail_after(self._read_timeout):
                async for chunk in response.aiter_raw():
                    if handle.detached:
                        chunks.clear()
                        continue
                    total += len(chunk)
                    if total > self._max_filtered_bytes:
                        raise ToolPolicyError("reply_too_large")
                    chunks.append(chunk)
            return b"".join(chunks)
        finally:
            await release.release_response()

    async def _respond_filtered(
        self,
        response: httpx.Response,
        ownership: _Ownership,
        rpc: RpcInfo,
        policy: EffectivePolicy,
        session_id: str | None,
        receive: Receive,
        audit: AuditRecord,
        detachable: bool,
    ) -> Response:
        """Answer a ``tools/list`` with the upstream's JSON reply, filtered; never unfiltered."""
        reply_id = rpc.id
        if reply_id is None:
            raise RuntimeError("a filtered tools/list always carries a request id")
        release = _Release(response, self._sessions, self._name, session_id, audit)
        headers = filter_response_headers(response.headers.multi_items())
        headers.pop("x-request-id", None)
        # The body is rewritten, so the upstream's length is not true: Starlette sets the new one.
        headers.pop("content-length", None)
        try:
            try:
                raw, gone = await await_read(
                    self._own_response(
                        response,
                        ownership,
                        detachable,
                        lambda handle: self._read_filterable_body(response, handle, release),
                    ),
                    receive,
                )
                if gone or raw is None:
                    return self._client_gone(audit)
                content = filter_json_reply(raw, reply_id, policy)
            except ToolPolicyError as exc:
                logger.error("upstream %s reply could not be filtered (%s)", self._name, exc.reason)
                audit.reason = exc.reason
                audit.emit(status=200, outcome="tool_policy_failed")
                return JSONResponse(policy_failed_body(reply_id))
            except (httpx.TimeoutException, TimeoutError) as exc:
                logger.error("upstream %s timed out (%s)", self._name, type(exc).__name__)
                audit.emit(status=504, outcome="upstream_error")
                return json_rpc_response(
                    504, UPSTREAM_TIMEOUT, "The service did not answer in time.", reply_id
                )
            except httpx.HTTPError as exc:
                logger.error("upstream %s unreachable (%s)", self._name, type(exc).__name__)
                audit.emit(status=502, outcome="upstream_error")
                return json_rpc_response(
                    502, UPSTREAM_UNREACHABLE, "The service could not be reached.", reply_id
                )
            release.completed = True
            return Response(content, status_code=response.status_code, headers=headers)
        finally:
            release.record_outcome(delivered=True)

    def stream(
        self,
        response: httpx.Response,
        rpc: RpcInfo,
        session_id: str | None,
        audit: AuditRecord,
        tool_policy: EffectivePolicy | None,
        *,
        detachable: bool,
    ) -> Response:
        """Relay the upstream response as it comes.

        While a tool policy is active every event stream goes through ``SseListFilter``, whatever
        method or HTTP verb opened it: a GET stream resumed with ``Last-Event-ID`` replays old
        replies, and any stream may carry a tool list.

        One reader task owns the upstream response from start to end and feeds the client through
        a bounded stream; ``detachable`` says whether it may carry on without the client.
        """
        release = _Release(response, self._sessions, self._name, session_id, audit)
        headers = filter_response_headers(response.headers.multi_items())
        # The client is given the gateway's request id; the upstream's is in the audit record.
        headers.pop("x-request-id", None)
        if _is_event_stream(response):
            # The relay may replace, drop or refuse bytes, so the upstream's length is never
            # true for what the client gets: the body goes out chunked.
            headers.pop("content-length", None)
            if has_content_encoding(response.headers.multi_items()):
                # Never relayed: the gateway reports the failure in the clear instead.
                headers.pop("content-encoding", None)
        reply_id = rpc.id if rpc.expects_reply else None
        reply_filter = (
            SseListFilter(
                reply_id,
                tool_policy,
                self._max_event_bytes,
                answers_tools_list=rpc.method == "tools/list",
            )
            if tool_policy is not None and _is_event_stream(response)
            else None
        )
        sink, source = anyio.create_memory_object_stream[_RelayItem](_RELAY_BUFFER_EVENTS)

        def start_reader() -> ReadHandle[None]:
            return self._own(
                detachable,
                lambda handle: self._read_upstream(
                    response, _Outlet(handle, sink), reply_id, audit, reply_filter, release
                ),
            )

        return RelayResponse(
            source,
            start_reader,
            release,
            status_code=response.status_code,
            headers=headers,
        )

    def _filtered_event(
        self,
        event: bytes,
        reply_filter: SseListFilter | None,
        status: int,
        audit: AuditRecord,
    ) -> bytes:
        """The event to send: filtered when it is the reply, the error event when it cannot be."""
        if reply_filter is None:
            return event
        try:
            return reply_filter.apply(event)
        except ToolPolicyError as exc:
            logger.error("upstream %s reply could not be filtered (%s)", self._name, exc.reason)
            audit.reason = exc.reason
            audit.emit(status=status, outcome="tool_policy_failed")
            return policy_failed_event(reply_filter.request_id)

    async def _read_upstream(
        self,
        response: httpx.Response,
        out: _Outlet,
        reply_id: str | int | None,
        audit: AuditRecord,
        reply_filter: SseListFilter | None,
        release: _Release,
    ) -> None:
        """Read the whole upstream response, handing it to the client while the client is there."""
        try:
            await self._relay(response, out, reply_id, audit, reply_filter, release)
        except (httpx.HTTPError, SseError):
            # An upstream failure: audited and reported to the client by ``_relay`` while it is
            # there; once it left, the failure is the read's result.
            raise
        except Exception:
            logger.exception("relaying the upstream response failed")
            await out.put(_Abort("internal_error"))
            raise
        finally:
            # The response is released first: the client's stream ends only afterwards, so a
            # client that stayed to the end never leaves a read behind to be detached.
            await release.release_response()
            out.close()

    async def _relay(
        self,
        response: httpx.Response,
        out: _Outlet,
        reply_id: str | int | None,
        audit: AuditRecord,
        reply_filter: SseListFilter | None,
        release: _Release,
    ) -> None:
        status = response.status_code
        events = (
            SseEventSplitter(self._max_event_bytes, reply_id)
            if _is_event_stream(response)
            else None
        )
        try:
            if events is not None and has_content_encoding(response.headers.multi_items()):
                raise EncodedStreamError
            async for chunk in response.aiter_raw():
                if not out.open:
                    continue  # the client is gone: read on, keep nothing
                if events is None:
                    await out.put(chunk)
                    continue
                for event in events.feed(chunk):
                    await out.put(self._filtered_event(event, reply_filter, status, audit))
            if events is not None and out.open:
                for event in events.finish():
                    await out.put(self._filtered_event(event, reply_filter, status, audit))
            release.completed = True
        except (httpx.HTTPError, SseError) as exc:
            reason = exc.reason if isinstance(exc, SseError) else type(exc).__name__
            logger.error("upstream %s stream failed (%s)", self._name, reason)
            audit.reason = reason
            audit.emit(status=status, outcome="upstream_stream_failed")
            if not out.open:
                # Nobody to tell: the failure is the detached read's result (`failed`/`timeout`).
                raise
            if events is not None and reply_id is not None and events.can_report_failure:
                # The partial event is discarded; the client learns the request failed.
                await out.put(stream_failed_event(reply_id))
                return
            await out.put(_Abort(reason))
