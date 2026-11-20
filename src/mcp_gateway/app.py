"""Starlette application: OAuth resource server in front of the proxy."""

import asyncio
import logging
import re
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

import anyio
import httpx
from opentelemetry.sdk.metrics.export import MetricReader
from opentelemetry.sdk.trace.export import SpanExporter
from starlette.applications import Starlette
from starlette.requests import ClientDisconnect, Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route
from starlette.types import Receive

from mcp_gateway.audit import AuditRecord
from mcp_gateway.auth import (
    InsufficientScopeError,
    InvalidTokenError,
    JwksUnavailableError,
    TokenVerifier,
)
from mcp_gateway.closing import close_all
from mcp_gateway.config import GatewayConfig
from mcp_gateway.identity import Identity, IdentityError, build_identity
from mcp_gateway.jsonrpc import (
    DENIED,
    DENY_CODES,
    GATEWAY_ERROR,
    HEADER_MISMATCH,
    INVALID_PARAMS,
    INVALID_REQUEST,
    NO_RPC,
    ORIGIN_REFUSED,
    RATE_LIMITED,
    RESOLVER_UNAVAILABLE,
    SESSION_NOT_FOUND,
    STATE_UNAVAILABLE,
    RpcInfo,
    check_mcp_headers,
    error_body,
    is_tool_batch,
    parse_request,
    tool_error_result,
)
from mcp_gateway.origin import OriginPolicy
from mcp_gateway.proxy import (
    UpstreamProxy,
    build_client,
    json_rpc_response,
    watching_disconnect,
)
from mcp_gateway.ratelimit import (
    RateLimiter,
    bucket_specs,
    build_rate_limiter,
)
from mcp_gateway.resolver import (
    CachingResolver,
    Credential,
    CredentialResolver,
    Denied,
    ResolverError,
    ResolverUnavailableError,
    build_resolver,
    with_cache,
)
from mcp_gateway.state import (
    MemorySessionStore,
    SessionStore,
    State,
    StateUnavailableError,
    build_state,
)
from mcp_gateway.telemetry import Telemetry, build_telemetry
from mcp_gateway.tool_policy import EffectivePolicy, ToolPolicy

logger = logging.getLogger(__name__)

JWKS_RETRY_AFTER_SECONDS = 5
DENIED_MESSAGE = "This account is not enabled for this service. Contact your administrator."
GATEWAY_ERROR_MESSAGE = "The gateway could not obtain credentials for this request."
INTERNAL_ERROR_MESSAGE = "The gateway failed to handle this request."
REQUEST_ID_HEADER = "x-request-id"
ORIGIN_REFUSED_MESSAGE = "The request origin is not allowed."
SESSION_NOT_FOUND_MESSAGE = "The session was not found. Start a new session."
BATCH_REFUSED_MESSAGE = "Batched tool requests are not accepted. Send one request per call."
INVALID_PARAMS_MESSAGE = "Invalid params"

# RFC 6750 section 3: a quoted auth-param value may hold only %x20-21 / %x23-5B / %x5D-7E.
_QUOTED_UNSAFE = re.compile(r"[^\x20\x21\x23-\x5b\x5d-\x7e]")


def _quoted(value: str) -> str:
    """Make ``value`` safe inside a quoted WWW-Authenticate parameter (no quote, backslash, controls)."""
    return _QUOTED_UNSAFE.sub(" ", value)


RESOLVER_UNAVAILABLE_MESSAGE = "Credentials are temporarily unavailable. Retry shortly."
RESOLVER_RETRY_AFTER_SECONDS = 5
STATE_UNAVAILABLE_MESSAGE = "The gateway's shared state is unavailable. Retry shortly."
STATE_RETRY_AFTER_SECONDS = 5


def _state_unavailable(audit: AuditRecord, reply_id: str | int | None) -> Response:
    """The 503 for any request the gateway cannot serve without its shared state (fail closed)."""
    audit.emit(status=503, outcome="state_unavailable")
    response = json_rpc_response(503, STATE_UNAVAILABLE, STATE_UNAVAILABLE_MESSAGE, reply_id)
    response.headers["Retry-After"] = str(STATE_RETRY_AFTER_SECONDS)
    return response


RETRY_AFTER_META_KEY = "club.alien/retryAfterSeconds"


def _rate_limited_message(retry_after: int) -> str:
    return f"Rate limit reached for this account. Retry in {retry_after} s."


def _rate_limited_response(rpc: RpcInfo, retry_after: int) -> Response:
    """Answer a refused request in the one shape its sender will actually read.

    Only a tool execution error reliably reaches the model, and a 2xx never disturbs the
    transport (several clients treat any other status as a connection failure), so a request
    that has an id is answered with 200. A request nobody can be answered (notification, client
    response, GET, DELETE, unparsable body) gets the plain 429.
    """
    message = _rate_limited_message(retry_after)
    headers = {"Retry-After": str(retry_after)}
    if rpc.id is None or rpc.method is None:
        return JSONResponse(
            error_body(RATE_LIMITED, message, None, {"retry_after_seconds": retry_after}),
            status_code=429,
            headers=headers,
        )
    if rpc.method == "tools/call":
        return JSONResponse(
            tool_error_result(rpc.id, message, {RETRY_AFTER_META_KEY: retry_after}),
            headers=headers,
        )
    return JSONResponse(
        error_body(RATE_LIMITED, message, rpc.id, {"retry_after_seconds": retry_after}),
        headers=headers,
    )


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


_SINGLE_VALUE_HEADERS = ("Mcp-Session-Id", "Mcp-Protocol-Version")


def _repeated_header(request: Request) -> str | None:
    for name in _SINGLE_VALUE_HEADERS:
        if len(request.headers.getlist(name)) > 1:
            return name
    return None


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
        sessions: SessionStore,
        telemetry: Telemetry,
        rate_limiter: RateLimiter | None,
    ) -> None:
        self._config = config
        self._rate_limiter = rate_limiter
        self._telemetry = telemetry
        self._origins = OriginPolicy(config.policies.allowed_origins)
        self._sessions = sessions
        self._verifier = verifier
        self._resolver = resolver
        self._proxies = proxies
        tools = config.policies.tools
        self._tool_policy = None if tools is None else ToolPolicy(tools)

    def _challenge(
        self, *, scope: list[str], error: str | None = None, description: str | None = None
    ) -> str:
        parts = [
            f'resource_metadata="{_quoted(self._config.resource_metadata_url)}"',
            f'scope="{_quoted(" ".join(scope))}"',
        ]
        if error is not None:
            parts.append(f'error="{_quoted(error)}"')
            parts.append(f'error_description="{_quoted(description or "")}"')
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

    async def _rate_limit(
        self, identity: Identity, rpc: RpcInfo, audit: AuditRecord, *, subject_key: str
    ) -> Response | None:
        """Take a token from every applicable bucket, or answer why not. Never calls the resolver."""
        limits = self._config.policies.rate_limits
        limiter = self._rate_limiter
        if limits is None or limiter is None:
            return None
        buckets = bucket_specs(limits, subject_key=subject_key, organization=identity.organization)
        if not buckets:
            return None
        try:
            decision = await limiter.acquire(buckets)
        except StateUnavailableError as exc:
            logger.error("rate limit state unavailable: %s", exc)
            return _state_unavailable(audit, rpc.id if rpc.expects_reply else None)
        if decision.allowed:
            return None
        audit.reason = decision.limited_scope
        response = _rate_limited_response(rpc, decision.retry_after_seconds)
        audit.emit(status=response.status_code, outcome="rate_limited")
        return response

    async def _resolve(
        self, identity: Identity, rpc: RpcInfo, audit: AuditRecord, receive: Receive
    ) -> Credential | Response:
        reply_id = rpc.id if rpc.expects_reply else None
        step = audit.observer.begin_resolve(self._config.resolver.type)
        cache: str = "none"

        async def lookup() -> Credential | Denied:
            nonlocal cache
            if isinstance(self._resolver, CachingResolver):
                found, cache = await self._resolver.resolve_detailed(identity)
                return found
            return await self._resolver.resolve(identity)

        try:
            # A client that leaves must not keep a (possibly slow) lookup running.
            outcome, gone = await watching_disconnect(receive, lookup)
        except ResolverUnavailableError as exc:
            step.finish(cache=cache, result="unavailable")
            logger.error("credential backend unavailable: %s", exc)
            audit.emit(status=503, outcome="resolver_unavailable")
            response = json_rpc_response(
                503, RESOLVER_UNAVAILABLE, RESOLVER_UNAVAILABLE_MESSAGE, reply_id
            )
            response.headers["Retry-After"] = str(RESOLVER_RETRY_AFTER_SECONDS)
            return response
        except StateUnavailableError as exc:
            step.finish(cache=cache, result="unavailable")
            logger.error("credential cache unavailable: %s", exc)
            return _state_unavailable(audit, reply_id)
        except ResolverError as exc:
            step.finish(cache=cache, result="error")
            logger.error("credential resolution failed: %s", exc)
            audit.emit(status=500, outcome="gateway_error")
            return json_rpc_response(500, GATEWAY_ERROR, GATEWAY_ERROR_MESSAGE, reply_id)
        except BaseException:
            step.finish(cache=cache, result="aborted")
            raise
        if gone or outcome is None:
            step.finish(cache=cache, result="disconnected")
            logger.info("client disconnected while the credential was being resolved")
            audit.emit(status=499, outcome="client_disconnected")
            return Response(status_code=499)
        step.finish(cache=cache, result="denied" if isinstance(outcome, Denied) else "credential")
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
        audit = AuditRecord(
            http_method=request.method,
            enabled=self._config.audit.enabled,
            observer=self._telemetry.start_request(request.headers.getlist("traceparent")),
            session_present="mcp-session-id" in request.headers,
        )
        try:
            response = await self._handle(request, audit)
        except asyncio.CancelledError:
            audit.emit(status=499, outcome="client_disconnected")
            raise
        except Exception:
            logger.exception("request handling failed")
            audit.emit(status=500, outcome="internal_error")
            response = json_rpc_response(500, GATEWAY_ERROR, INTERNAL_ERROR_MESSAGE, None)
        # Whatever the upstream sent as x-request-id was dropped (and audited) by the proxy.
        response.headers[REQUEST_ID_HEADER] = audit.request_id
        return response

    async def _handle(self, request: Request, audit: AuditRecord) -> Response:
        # Before authentication: a refused origin learns nothing about tokens.
        if not self._origins.allows(request.headers.getlist("origin")):
            audit.emit(status=403, outcome="origin_refused")
            return json_rpc_response(403, ORIGIN_REFUSED, ORIGIN_REFUSED_MESSAGE, None)
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

        reply_id = rpc.id if rpc.expects_reply else None
        inbound_headers = list(request.headers.items())
        mismatch = check_mcp_headers(inbound_headers, rpc)
        if mismatch is not None:
            audit.emit(status=400, outcome="mcp_header_mismatch")
            return json_rpc_response(400, HEADER_MISMATCH, mismatch, reply_id)

        repeated = _repeated_header(request)
        if repeated is not None:
            # Two values would let a front proxy and the upstream disagree about which one counts,
            # and the binding check would look at the other one.
            audit.emit(status=400, outcome="duplicate_header")
            return json_rpc_response(
                400, INVALID_REQUEST, f"The {repeated} header must appear once.", reply_id
            )

        subject_key = f"{self._config.auth.issuer}\x1f{identity.subject}"
        # After the identity is known and before any credential lookup, so a flood is refused
        # without ever reaching the resolver backend.
        limited = await self._rate_limit(identity, rpc, audit, subject_key=subject_key)
        if limited is not None:
            return limited

        credential = await self._resolve(identity, rpc, audit, request.receive)
        if isinstance(credential, Response):
            return credential
        audit.labels = dict(credential.labels)
        try:
            return await self._serve(
                request, body, inbound_headers, rpc, identity, credential, subject_key, audit
            )
        except StateUnavailableError as exc:
            # Raised before a response was chosen (session check, binding a new session,
            # invalidating a rejected credential): nothing is relayed.
            logger.error("shared state unavailable: %s", exc)
            return _state_unavailable(audit, reply_id)

    async def _serve(
        self,
        request: Request,
        body: bytes,
        inbound_headers: list[tuple[str, str]],
        rpc: RpcInfo,
        identity: Identity,
        credential: Credential,
        subject_key: str,
        audit: AuditRecord,
    ) -> Response:
        reply_id = rpc.id if rpc.expects_reply else None
        session_id = request.headers.get("mcp-session-id")
        if session_id is not None and not await self._sessions.check(
            credential.upstream, session_id, subject_key
        ):
            # Unknown here, expired, or bound to another subject: never forwarded.
            audit.upstream = credential.upstream
            audit.emit(status=404, outcome="session_not_found")
            return json_rpc_response(404, SESSION_NOT_FOUND, SESSION_NOT_FOUND_MESSAGE, reply_id)

        tool_policy = (
            None if self._tool_policy is None else self._tool_policy.effective(identity, credential)
        )
        if tool_policy is not None:
            audit.upstream = credential.upstream
            refusal = self._refuse_tool_request(tool_policy, rpc, body, audit)
            if refusal is not None:
                return refusal

        return await self._proxies[credential.upstream].forward(
            method=request.method,
            inbound_headers=inbound_headers,
            body=body,
            receive=request.receive,
            rpc=rpc,
            credential=credential,
            identity=identity,
            resolver=self._resolver,
            subject_key=subject_key,
            audit=audit,
            tool_policy=tool_policy,
        )

    @staticmethod
    def _refuse_tool_request(
        policy: EffectivePolicy, rpc: RpcInfo, body: bytes, audit: AuditRecord
    ) -> Response | None:
        """Refuse a tool request the policy does not allow, or None to forward it.

        A denied tool is answered exactly like a tool that does not exist. A request whose tool
        cannot be read is refused too: the gateway never forwards what it cannot check.
        """
        if rpc.method is None and body and is_tool_batch(body):
            # A batch is invisible to the single-request checks, so it is refused outright.
            audit.emit(status=400, outcome="batch_refused")
            return json_rpc_response(400, INVALID_REQUEST, BATCH_REFUSED_MESSAGE, None)
        if rpc.method != "tools/call":
            return None
        # Without an id no reply can be correlated: the refusal is an HTTP error.
        status = 200 if rpc.expects_reply else 400
        reply_id = rpc.id if rpc.expects_reply else None
        if rpc.tool is None:
            audit.reason = "invalid_params"
            audit.emit(status=status, outcome="tool_denied")
            return json_rpc_response(status, INVALID_PARAMS, INVALID_PARAMS_MESSAGE, reply_id)
        if policy.allows(rpc.tool):
            return None
        audit.emit(status=status, outcome="tool_denied")
        return json_rpc_response(status, INVALID_PARAMS, f"Unknown tool: {rpc.tool}", reply_id)


def create_app(
    config: GatewayConfig,
    *,
    resolver: CredentialResolver | None = None,
    http_transport: httpx.AsyncBaseTransport | None = None,
    session_clock: Callable[[], float] = time.monotonic,
    state: State | None = None,
    span_exporter: SpanExporter | None = None,
    metric_reader: MetricReader | None = None,
    rate_limiter: RateLimiter | None = None,
) -> Starlette:
    """Build the app. HTTP clients are created here and closed by the lifespan.

    ``span_exporter`` and ``metric_reader`` replace the configured OTLP export; tests use them
    to read spans and metrics in memory. ``state`` replaces the backend ``config.state`` names
    (tests inject failing stores); the lifespan closes it either way. ``rate_limiter`` replaces the
    limiter that ``policies.rate_limits`` otherwise installs; it is unused while that key is null.
    """
    if state is None:
        state = build_state(config, session_clock=session_clock)
    if rate_limiter is None:
        rate_limiter = build_rate_limiter(config.policies.rate_limits, state)
    telemetry = build_telemetry(
        config.observability, span_exporter=span_exporter, metric_reader=metric_reader
    )
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
            state.cache,
        )

    jwks_client = httpx.AsyncClient(
        transport=http_transport,
        timeout=config.auth.jwks_fetch_timeout_seconds,
        follow_redirects=False,
    )
    clients = {name: build_client(up, http_transport) for name, up in config.upstreams.items()}
    sessions = state.sessions
    proxies = {
        name: UpstreamProxy(
            name,
            config.upstreams[name],
            client,
            sessions,
            config.limits.max_sse_event_bytes,
            config.limits.max_filtered_response_bytes,
        )
        for name, client in clients.items()
    }
    if isinstance(sessions, MemorySessionStore):
        # The shared store has no cheap count, so the gauge exists with the memory backend only.
        telemetry.observe_sessions(lambda: len(sessions))
    gateway = Gateway(
        config,
        TokenVerifier(config.auth, jwks_client),
        resolver,
        proxies,
        sessions,
        telemetry,
        rate_limiter,
    )

    # RFC 9728 section 3.3: the document describes the resource it was fetched for, so only the
    # path-specific URL is served (resource = public_url). The origin-level document would
    # describe a different resource and answers 404.
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

    async def readyz(request: Request) -> Response:
        """Readiness: can this replica reach its shared state? A load balancer drains it if not."""
        try:
            await state.ping()
        except StateUnavailableError as exc:
            logger.error("not ready: %s", exc)
            return JSONResponse({"status": "state_unavailable"}, status_code=503)
        return JSONResponse({"status": "ready"})

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        try:
            yield
        finally:
            # Telemetry last: it flushes what the requests above produced while closing.
            await close_all(
                [
                    resolver.aclose,
                    jwks_client.aclose,
                    state.aclose,
                    *(c.aclose for c in clients.values()),
                    *telemetry.closers,
                ]
            )

    return Starlette(
        routes=[
            Route("/healthz", healthz, methods=["GET"]),
            Route("/readyz", readyz, methods=["GET"]),
            Route(config.resource_metadata_path, protected_resource_metadata),
            Route(config.public_path, gateway.mcp, methods=["GET", "POST", "DELETE"]),
        ],
        lifespan=lifespan,
    )
