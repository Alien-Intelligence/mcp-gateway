"""Streaming reverse proxy to one upstream MCP server. Responses are never rewritten."""

import logging
from collections.abc import AsyncGenerator, Mapping
from typing import Any

import anyio
import httpx
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.types import Receive, Scope, Send

from mcp_gateway.audit import AuditRecord
from mcp_gateway.config import UpstreamConfig
from mcp_gateway.headers import build_upstream_headers, filter_response_headers
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


async def _drain(response: httpx.Response) -> None:
    received = 0
    async for chunk in response.aiter_raw():
        received += len(chunk)
        if received > _DRAIN_LIMIT_BYTES:
            break


class RelayResponse(StreamingResponse):
    """Streaming response that always watches for the client going away.

    Starlette only notices a disconnect on the next write, so an idle SSE stream would keep
    the upstream connection open until the next event. Here a disconnect cancels the pump
    immediately, and the relay generator is closed so the upstream response is released.
    """

    def __init__(
        self, relay: AsyncGenerator[bytes, None], status_code: int, headers: Mapping[str, str]
    ) -> None:
        super().__init__(relay, status_code=status_code, headers=headers)
        self._relay = relay

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
        if failure:
            raise failure[0]


class UpstreamProxy:
    def __init__(self, name: str, config: UpstreamConfig, client: httpx.AsyncClient) -> None:
        self._name = name
        self._url = config.url
        self._client = client

    async def forward(
        self,
        *,
        method: str,
        inbound_headers: list[tuple[str, str]],
        body: bytes,
        rpc: RpcInfo,
        credential: Credential,
        identity: Identity,
        resolver: CredentialResolver,
        audit: AuditRecord,
    ) -> Response:
        request = self._client.build_request(
            method,
            self._url,
            headers=build_upstream_headers(inbound_headers, credential.api_key),
            content=body or None,
        )
        audit.upstream = self._name
        reply_id = rpc.id if rpc.expects_reply else None
        try:
            response = await self._client.send(request, stream=True)
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

        audit.upstream_status = response.status_code
        if response.status_code in _AUTH_REPLIES:
            return await self._reject(response, rpc, identity, resolver, audit)
        return self._stream(response, audit)

    async def _reject(
        self,
        response: httpx.Response,
        rpc: RpcInfo,
        identity: Identity,
        resolver: CredentialResolver,
        audit: AuditRecord,
    ) -> Response:
        upstream_status = response.status_code
        try:
            await _drain(response)
        except httpx.HTTPError as exc:
            logger.warning(
                "draining upstream %d body failed (%s)", upstream_status, type(exc).__name__
            )
        finally:
            await _close(response)
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

    def _stream(self, response: httpx.Response, audit: AuditRecord) -> Response:
        status = response.status_code

        async def relay() -> AsyncGenerator[bytes, None]:
            try:
                async for chunk in response.aiter_raw():
                    yield chunk
            except httpx.HTTPError as exc:
                logger.error("upstream %s stream failed (%s)", self._name, type(exc).__name__)
                audit.emit(status=status, outcome="upstream_error")
                raise
            finally:
                # Runs on completion, upstream failure and client disconnect (cancellation).
                await _close(response)
                audit.emit(status=status, outcome="forwarded")

        return RelayResponse(
            relay(),
            status_code=status,
            headers=filter_response_headers(response.headers.multi_items()),
        )
