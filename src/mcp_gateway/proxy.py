"""Streaming reverse proxy to one upstream MCP server. Responses are never rewritten."""

import logging
from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping
from typing import Any

import anyio
import httpx
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.types import Receive, Scope, Send

from mcp_gateway.audit import AuditRecord
from mcp_gateway.config import UpstreamConfig
from mcp_gateway.headers import (
    build_upstream_headers,
    filter_response_headers,
    has_content_encoding,
)
from mcp_gateway.identity import Identity
from mcp_gateway.jsonrpc import (
    UPSTREAM_PAYMENT_REQUIRED,
    UPSTREAM_TIMEOUT,
    UPSTREAM_UNAUTHORIZED,
    UPSTREAM_UNREACHABLE,
    RpcInfo,
    error_body,
)
from mcp_gateway.resolver import Credential, CredentialResolver, ResolverError
from mcp_gateway.sessions import SessionRegistry
from mcp_gateway.sse import (
    EncodedStreamError,
    SseError,
    SseEventSplitter,
    stream_failed_event,
)

logger = logging.getLogger(__name__)

# Upstream 401/402 bodies are drained (to free the connection) but never relayed.
_DRAIN_LIMIT_BYTES = 64 * 1024
_CLOSE_TIMEOUT_SECONDS = 5.0

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


async def _watching_disconnect[T](
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


class UpstreamStreamAbortedError(Exception):
    """Raised out of the response body so the ASGI server drops the connection.

    Without a terminating chunk the client sees an incomplete response, never a clean end.
    """


def _is_event_stream(response: httpx.Response) -> bool:
    content_type = response.headers.get("content-type", "")
    return content_type.split(";", 1)[0].strip().lower() == "text/event-stream"


def _request_session_id(headers: list[tuple[str, str]]) -> str | None:
    values = [value for name, value in headers if name.lower() == "mcp-session-id"]
    return values[0] if values else None


class _Release:
    """Releases one upstream response and records the request, exactly once.

    The relay generator calls ``finalize`` from its ``finally``. That is not enough: closing an
    async generator that never started does not run its ``finally``, so ``RelayResponse`` calls
    it too. The guard makes the second call a no-op.
    """

    def __init__(
        self,
        response: httpx.Response,
        sessions: SessionRegistry,
        upstream: str,
        session_id: str | None,
        audit: AuditRecord,
    ) -> None:
        self._response = response
        self._sessions = sessions
        self._upstream = upstream
        self._session_id = session_id
        self._audit = audit
        self._finalized = False
        # Set by the relay when the upstream body was relayed to its end.
        self.completed = False

    async def finalize(self) -> None:
        if self._finalized:
            return
        self._finalized = True
        try:
            await _close(self._response)
        finally:
            if self._session_id is not None:
                self._sessions.touch(self._upstream, self._session_id)
            if self.completed:
                self._audit.emit(status=self._response.status_code, outcome="forwarded")
            else:
                # A failure was audited already (emit is first-wins); this is the client leaving.
                self._audit.emit(status=499, outcome="client_disconnected")


class RelayResponse(StreamingResponse):
    """Streaming response that always watches for the client going away.

    Starlette only notices a disconnect on the next write, so an idle SSE stream would keep
    the upstream connection open until the next event. Here a disconnect cancels the pump
    immediately, and the upstream response is released whatever way the relay ends.
    """

    def __init__(
        self,
        relay: AsyncGenerator[bytes, None],
        release: _Release,
        status_code: int,
        headers: Mapping[str, str],
    ) -> None:
        super().__init__(relay, status_code=status_code, headers=headers)
        self._relay = relay
        self._release = release

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        failure: list[Exception] = []
        try:
            async with anyio.create_task_group() as group:

                async def pump() -> None:
                    try:
                        await self.stream_response(send)
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
                await self._relay.aclose()
                await self._release.finalize()
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
        sessions: SessionRegistry,
        max_event_bytes: int,
    ) -> None:
        self._name = name
        self._url = config.url
        self._client = client
        self._sessions = sessions
        self._max_event_bytes = max_event_bytes

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
    ) -> Response:
        request = self._client.build_request(
            method,
            self._url,
            headers=_wire_headers(build_upstream_headers(inbound_headers, credential.api_key)),
            content=body or None,
        )
        audit.upstream = self._name
        reply_id = rpc.id if rpc.expects_reply else None
        try:
            response, gone = await _watching_disconnect(
                receive, lambda: self._client.send(request, stream=True)
            )
        except httpx.TimeoutException as exc:
            logger.error("upstream %s timed out (%s)", self._name, type(exc).__name__)
            audit.emit(status=504, outcome="upstream_error")
            return json_rpc_response(
                504, UPSTREAM_TIMEOUT, "The service did not answer in time.", reply_id
            )
        except httpx.TransportError as exc:
            logger.error("upstream %s unreachable (%s)", self._name, type(exc).__name__)
            audit.emit(status=502, outcome="upstream_error")
            return json_rpc_response(
                502, UPSTREAM_UNREACHABLE, "The service could not be reached.", reply_id
            )
        if gone:
            if response is not None:
                await _close(response)
            return self._client_gone(audit)
        if response is None:
            raise RuntimeError("upstream send finished without a response or a disconnect")

        audit.upstream_status = response.status_code
        audit.upstream_session = "mcp-session-id" in response.headers
        session_id = _request_session_id(inbound_headers)
        if response.status_code in _AUTH_REPLIES:
            return await self._reject(response, receive, rpc, identity, resolver, audit)
        self._track_session(method, session_id, response, subject_key)
        return self.stream(response, rpc, session_id, audit)

    def _track_session(
        self,
        method: str,
        session_id: str | None,
        response: httpx.Response,
        subject_key: str,
    ) -> None:
        """Keep the registry in step with what the upstream says about the session."""
        status = response.status_code
        issued = response.headers.get("mcp-session-id")
        if session_id is not None and (
            status == 404 or (method == "DELETE" and 200 <= status < 300)
        ):
            # The upstream no longer knows the session, or the client just ended it.
            self._sessions.drop(self._name, session_id)
        elif issued is not None and 200 <= status < 300:
            self._sessions.bind(self._name, issued, subject_key)

    @staticmethod
    def _client_gone(audit: AuditRecord) -> Response:
        logger.info("client disconnected while waiting for the upstream")
        audit.emit(status=499, outcome="client_disconnected")
        return Response(status_code=499)

    async def _reject(
        self,
        response: httpx.Response,
        receive: Receive,
        rpc: RpcInfo,
        identity: Identity,
        resolver: CredentialResolver,
        audit: AuditRecord,
    ) -> Response:
        upstream_status = response.status_code
        gone = False
        try:
            _, gone = await _watching_disconnect(receive, lambda: _drain(response))
        except httpx.HTTPError as exc:
            logger.warning(
                "draining upstream %d body failed (%s)", upstream_status, type(exc).__name__
            )
        finally:
            await _close(response)
        if gone:
            return self._client_gone(audit)
        try:
            await resolver.invalidate(identity)
        except ResolverError:
            logger.exception("credential invalidation failed after upstream %d", upstream_status)
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

    def stream(
        self,
        response: httpx.Response,
        rpc: RpcInfo,
        session_id: str | None,
        audit: AuditRecord,
    ) -> Response:
        release = _Release(response, self._sessions, self._name, session_id, audit)
        headers = filter_response_headers(response.headers.multi_items())
        if _is_event_stream(response) and has_content_encoding(response.headers.multi_items()):
            # Never relayed: the gateway reports the failure in the clear instead.
            headers.pop("content-encoding", None)
        return RelayResponse(
            self._relay(response, rpc.id if rpc.expects_reply else None, release, audit),
            release,
            status_code=response.status_code,
            headers=headers,
        )

    async def _relay(
        self,
        response: httpx.Response,
        reply_id: str | int | None,
        release: _Release,
        audit: AuditRecord,
    ) -> AsyncGenerator[bytes, None]:
        status = response.status_code
        events = (
            SseEventSplitter(self._max_event_bytes, reply_id)
            if _is_event_stream(response)
            else None
        )
        try:
            try:
                if events is not None and has_content_encoding(response.headers.multi_items()):
                    raise EncodedStreamError
                async for chunk in response.aiter_raw():
                    if events is None:
                        yield chunk
                        continue
                    for event in events.feed(chunk):
                        yield event
                if events is not None:
                    for event in events.finish():
                        yield event
                release.completed = True
            except (httpx.HTTPError, SseError) as exc:
                reason = exc.reason if isinstance(exc, SseError) else type(exc).__name__
                logger.error("upstream %s stream failed (%s)", self._name, reason)
                audit.reason = reason
                audit.emit(status=status, outcome="upstream_stream_failed")
                if events is not None and reply_id is not None and events.can_report_failure:
                    # The partial event is discarded; the client learns the request failed.
                    yield stream_failed_event(reply_id)
                    return
                raise UpstreamStreamAbortedError(reason) from exc
        finally:
            # Runs on completion, upstream failure and client disconnect (cancellation); the
            # response wrapper repeats it for a relay that never started.
            await release.finalize()
