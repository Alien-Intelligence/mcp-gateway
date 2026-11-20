"""Starlette application: OAuth resource server in front of the proxy."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import anyio
import httpx
from starlette.applications import Starlette
from starlette.requests import ClientDisconnect, Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from mcp_gateway.audit import AuditRecord
from mcp_gateway.auth import (
    InsufficientScopeError,
    InvalidTokenError,
    JwksUnavailableError,
    TokenVerifier,
)
from mcp_gateway.config import GatewayConfig
from mcp_gateway.identity import Identity, IdentityError, build_identity
from mcp_gateway.jsonrpc import (
    DENIED,
    DENY_CODES,
    GATEWAY_ERROR,
    INVALID_REQUEST,
    NO_RPC,
    RESOLVER_UNAVAILABLE,
    RpcInfo,
    parse_request,
)
from mcp_gateway.proxy import UpstreamProxy, build_client, json_rpc_response
from mcp_gateway.resolver import (
    Credential,
    CredentialResolver,
    Denied,
    ResolverError,
    ResolverUnavailableError,
    build_resolver,
    with_cache,
)

logger = logging.getLogger(__name__)

JWKS_RETRY_AFTER_SECONDS = 5
DENIED_MESSAGE = "This account is not enabled for this service. Contact your administrator."
GATEWAY_ERROR_MESSAGE = "The gateway could not obtain credentials for this request."
RESOLVER_UNAVAILABLE_MESSAGE = "Credentials are temporarily unavailable. Retry shortly."
RESOLVER_RETRY_AFTER_SECONDS = 5


async def _read_bounded_body(request: Request, limit: int) -> bytes | None:
    """Read the whole body, or return None as soon as it is known to exceed ``limit``."""
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > limit:
        return None
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > limit:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


def _bearer_token(request: Request) -> str | None:
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    token = token.strip()
    return token if scheme.lower() == "bearer" and token else None


class Gateway:
    def __init__(
        self,
        config: GatewayConfig,
        verifier: TokenVerifier,
        resolver: CredentialResolver,
        proxies: dict[str, UpstreamProxy],
    ) -> None:
        self._config = config
        self._verifier = verifier
        self._resolver = resolver
        self._proxies = proxies

    def _challenge(
        self, *, scope: list[str], error: str | None = None, description: str | None = None
    ) -> str:
        parts = [
            f'resource_metadata="{self._config.resource_metadata_url}"',
            f'scope="{" ".join(scope)}"',
        ]
        if error is not None:
            parts.append(f'error="{error}"')
            parts.append(f'error_description="{description}"')
        return "Bearer " + ", ".join(parts)

    def _unauthorized(self, description: str, *, token_presented: bool) -> Response:
        challenge = self._challenge(
            scope=self._config.auth.scopes_advertised,
            error="invalid_token" if token_presented else None,
            description=description if token_presented else None,
        )
        return JSONResponse(
            {"error": "invalid_token", "error_description": description},
            status_code=401,
            headers={"WWW-Authenticate": challenge},
        )

    def _insufficient_scope(self, description: str) -> Response:
        challenge = self._challenge(
            scope=self._config.auth.required_scopes,
            error="insufficient_scope",
            description=description,
        )
        return JSONResponse(
            {"error": "insufficient_scope", "error_description": description},
            status_code=403,
            headers={"WWW-Authenticate": challenge},
        )

    async def _authenticate(self, request: Request, audit: AuditRecord) -> Identity | Response:
        token = _bearer_token(request)
        if token is None:
            audit.emit(status=401, outcome="unauthenticated")
            return self._unauthorized("missing bearer token", token_presented=False)
        try:
            claims = await self._verifier.verify(token)
            return build_identity(claims, self._config.auth.identity)
        except InvalidTokenError as exc:
            audit.emit(status=401, outcome="unauthenticated")
            return self._unauthorized(exc.description, token_presented=True)
        except IdentityError as exc:
            logger.warning("token claims rejected: %s", exc)
            audit.emit(status=401, outcome="unauthenticated")
            return self._unauthorized("token does not identify a user", token_presented=True)
        except InsufficientScopeError as exc:
            audit.emit(status=403, outcome="insufficient_scope")
            return self._insufficient_scope(exc.description)
        except JwksUnavailableError:
            audit.emit(status=503, outcome="idp_unavailable")
            return JSONResponse(
                {
                    "error": "temporarily_unavailable",
                    "error_description": "identity provider keys are unavailable",
                },
                status_code=503,
                headers={"Retry-After": str(JWKS_RETRY_AFTER_SECONDS)},
            )

    async def _resolve(
        self, identity: Identity, rpc: RpcInfo, audit: AuditRecord
    ) -> Credential | Response:
        reply_id = rpc.id if rpc.expects_reply else None
        try:
            outcome = await self._resolver.resolve(identity)
        except ResolverUnavailableError as exc:
            logger.error("credential backend unavailable: %s", exc)
            audit.emit(status=503, outcome="resolver_unavailable")
            response = json_rpc_response(
                503, RESOLVER_UNAVAILABLE, RESOLVER_UNAVAILABLE_MESSAGE, reply_id
            )
            response.headers["Retry-After"] = str(RESOLVER_RETRY_AFTER_SECONDS)
            return response
        except ResolverError as exc:
            logger.error("credential resolution failed: %s", exc)
            audit.emit(status=500, outcome="gateway_error")
            return json_rpc_response(500, GATEWAY_ERROR, GATEWAY_ERROR_MESSAGE, reply_id)
        if isinstance(outcome, Denied):
            audit.reason = outcome.reason
            audit.emit(status=403, outcome="denied")
            return json_rpc_response(
                403, DENY_CODES.get(outcome.reason, DENIED), DENIED_MESSAGE, reply_id
            )
        if outcome.upstream not in self._proxies:
            logger.error("resolver returned an unknown upstream")
            audit.emit(status=500, outcome="gateway_error")
            return json_rpc_response(500, GATEWAY_ERROR, GATEWAY_ERROR_MESSAGE, reply_id)
        return outcome

    async def mcp(self, request: Request) -> Response:
        audit = AuditRecord(http_method=request.method, enabled=self._config.audit.enabled)
        identity = await self._authenticate(request, audit)
        if isinstance(identity, Response):
            return identity
        audit.subject = identity.subject
        audit.organization = identity.organization

        try:
            with anyio.fail_after(self._config.limits.request_body_timeout_seconds):
                body = await _read_bounded_body(request, self._config.limits.max_request_body_bytes)
        except TimeoutError:
            logger.warning("request body not received within the configured timeout")
            audit.emit(status=408, outcome="request_body_timeout")
            return json_rpc_response(
                408, INVALID_REQUEST, "The request body was not received in time.", None
            )
        except ClientDisconnect:
            logger.info("client disconnected while sending the request body")
            audit.emit(status=499, outcome="client_disconnected")
            return Response(status_code=499)
        if body is None:
            audit.emit(status=413, outcome="body_too_large")
            return json_rpc_response(413, INVALID_REQUEST, "The request body is too large.", None)

        rpc = parse_request(body) if body else NO_RPC
        audit.rpc_method, audit.rpc_id, audit.tool = rpc.method, rpc.id, rpc.tool

        credential = await self._resolve(identity, rpc, audit)
        if isinstance(credential, Response):
            return credential
        audit.labels = dict(credential.labels)

        return await self._proxies[credential.upstream].forward(
            method=request.method,
            inbound_headers=list(request.headers.items()),
            body=body,
            receive=request.receive,
            rpc=rpc,
            credential=credential,
            identity=identity,
            resolver=self._resolver,
            audit=audit,
        )


def create_app(
    config: GatewayConfig,
    *,
    resolver: CredentialResolver | None = None,
    http_transport: httpx.AsyncBaseTransport | None = None,
) -> Starlette:
    """Build the app. HTTP clients are created here and closed by the lifespan."""
    if resolver is None:
        resolver = with_cache(
            build_resolver(
                config.resolver,
                config.upstreams.keys(),
                email_matching=(
                    config.auth.identity.email is not None
                    and config.auth.identity.email_verified is not None
                ),
            ),
            config.resolver_cache,
        )

    jwks_client = httpx.AsyncClient(
        transport=http_transport,
        timeout=config.auth.jwks_fetch_timeout_seconds,
        follow_redirects=False,
    )
    clients = {name: build_client(up, http_transport) for name, up in config.upstreams.items()}
    proxies = {
        name: UpstreamProxy(name, config.upstreams[name], client)
        for name, client in clients.items()
    }
    gateway = Gateway(config, TokenVerifier(config.auth, jwks_client), resolver, proxies)

    # The root document mirrors the path-specific one: MCP clients probe the path variant first.
    prm = {
        "resource": config.public_url,
        "authorization_servers": [config.auth.issuer],
        "scopes_supported": config.auth.scopes_advertised,
        "bearer_methods_supported": ["header"],
    }

    async def protected_resource_metadata(request: Request) -> Response:
        return JSONResponse(prm)

    async def healthz(request: Request) -> Response:
        return JSONResponse({"status": "ok"})

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        try:
            yield
        finally:
            await resolver.aclose()
            await jwks_client.aclose()
            for client in clients.values():
                await client.aclose()

    return Starlette(
        routes=[
            Route("/healthz", healthz, methods=["GET"]),
            Route("/.well-known/oauth-protected-resource", protected_resource_metadata),
            Route(config.resource_metadata_path, protected_resource_metadata),
            Route(config.public_path, gateway.mcp, methods=["GET", "POST", "DELETE"]),
        ],
        lifespan=lifespan,
    )
