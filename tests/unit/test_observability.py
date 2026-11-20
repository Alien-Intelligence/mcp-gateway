"""Spans, metrics, the audit record's ids and what none of them may carry."""

import asyncio
import json
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from opentelemetry import trace
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind, StatusCode
from starlette.applications import Starlette

from mcp_gateway.app import create_app
from mcp_gateway.audit import OUTCOMES
from mcp_gateway.proxy import UpstreamStreamAbortedError
from mcp_gateway.resolver import Denied, ResolverError, ResolverUnavailableError
from mcp_gateway.state import State
from mcp_gateway.telemetry import (
    REQUEST_SPAN,
    RESOLVE_SPAN,
    UPSTREAM_SPAN,
    bounded_rpc_method,
    build_telemetry,
)

from .conftest import FakeIdp, FakeUpstream, TrackedStream, base_config_dict, config_from, mint
from .state_fakes import state_with
from .test_app import (
    RPC_BODY,
    FixedResolver,
    asgi_scope,
    bearer,
    call_then_disconnect,
    client_for,
    routed_transport,
)

SRC = Path(__file__).resolve().parents[2] / "src" / "mcp_gateway"
TRACEPARENT = "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"
INBOUND_TRACE_ID = "0af7651916cd43dd8448eb211c80319c"


@dataclass
class Observed:
    app: Starlette
    spans: InMemorySpanExporter
    reader: InMemoryMetricReader


def build_observed(
    mapping_dir: Path,
    transport: httpx.MockTransport,
    *,
    resolver: Any = None,
    state: State | None = None,
    spans: InMemorySpanExporter | None = None,
    reader: InMemoryMetricReader | None = None,
    **overrides: Any,
) -> Observed:
    data = base_config_dict(str(mapping_dir / "mapping.json"))
    for dotted, value in overrides.items():
        node = data
        *parents, leaf = dotted.split("__")
        for part in parents:
            node = node[part]
        node[leaf] = value
    span_exporter = spans or InMemorySpanExporter()
    metric_reader = reader or InMemoryMetricReader()
    app = create_app(
        config_from(data),
        resolver=resolver,
        state=state,
        http_transport=transport,
        span_exporter=span_exporter,
        metric_reader=metric_reader,
    )
    return Observed(app, span_exporter, metric_reader)


def audit_lines(caplog: pytest.LogCaptureFixture) -> list[dict[str, Any]]:
    return [json.loads(r.getMessage()) for r in caplog.records if r.name == "mcp_gateway.audit"]


def metric_points(reader: InMemoryMetricReader, name: str) -> list[Any]:
    data = reader.get_metrics_data()
    if data is None:
        return []
    return [
        point
        for resource in data.resource_metrics
        for scope in resource.scope_metrics
        for metric in scope.metrics
        if metric.name == name
        for point in metric.data.data_points
    ]


def spans_named(exporter: InMemorySpanExporter, name: str) -> list[ReadableSpan]:
    return [span for span in exporter.get_finished_spans() if span.name == name]


def attributes_of(span: ReadableSpan) -> dict[str, Any]:
    return dict(span.attributes or {})


@pytest.fixture
def token(rsa_key: rsa.RSAPrivateKey) -> str:
    return mint(rsa_key)


# --- one request, fully observed ------------------------------------------------------------


async def test_a_forwarded_tool_call_has_a_span_tree_metrics_and_matching_ids(
    mapping_dir: Path,
    transport: httpx.MockTransport,
    token: str,
    upstream: FakeUpstream,
    caplog: pytest.LogCaptureFixture,
) -> None:
    upstream.responder = lambda _r: httpx.Response(
        200,
        headers={"x-request-id": "up-123", "content-type": "application/json"},
        json={"jsonrpc": "2.0", "id": 7, "result": {}},
    )
    observed = build_observed(mapping_dir, transport)
    with caplog.at_level(logging.INFO, logger="mcp_gateway.audit"):
        async with client_for(observed.app) as http:
            response = await http.post("/mcp", json=RPC_BODY, headers=bearer(token))

    (line,) = audit_lines(caplog)
    (request_span,) = spans_named(observed.spans, REQUEST_SPAN)
    (resolve_span,) = spans_named(observed.spans, RESOLVE_SPAN)
    (upstream_span,) = spans_named(observed.spans, UPSTREAM_SPAN)

    # The ids tie the client, the audit line and the trace together.
    assert response.headers["x-request-id"] == line["request_id"]
    assert response.headers.get_list("x-request-id") == [line["request_id"]]
    assert line["upstream_request_id"] == "up-123"
    assert line["trace_id"] == format(request_span.context.trace_id, "032x")
    assert attributes_of(request_span)["mcp_gateway.request_id"] == line["request_id"]

    assert request_span.kind == SpanKind.SERVER
    assert request_span.parent is None
    assert request_span.status.status_code == StatusCode.UNSET
    assert attributes_of(request_span) == {
        "http.request.method": "POST",
        "http.response.status_code": 200,
        "mcp_gateway.outcome": "forwarded",
        "mcp_gateway.request_id": line["request_id"],
        "mcp_gateway.session.present": False,
        "mcp.method.name": "tools/call",
        "mcp.tool.name": "search",
        "mcp_gateway.upstream": "main",
        "mcp_gateway.upstream.status_code": 200,
        "mcp_gateway.label.client": "client-x",
    }

    for child in (resolve_span, upstream_span):
        assert child.parent is not None
        assert child.parent.span_id == request_span.context.span_id
        assert child.context.trace_id == request_span.context.trace_id
    assert attributes_of(resolve_span) == {
        "mcp_gateway.resolver.type": "static",
        "mcp_gateway.resolver.cache": "none",
        "mcp_gateway.resolver.result": "credential",
    }
    assert upstream_span.kind == SpanKind.CLIENT
    assert attributes_of(upstream_span) == {
        "mcp_gateway.upstream": "main",
        "http.response.status_code": 200,
    }

    (requests,) = metric_points(observed.reader, "mcp_gateway.requests")
    assert requests.value == 1
    assert dict(requests.attributes) == {
        "outcome": "forwarded",
        "status_class": "2xx",
        "rpc_method": "tools/call",
        "upstream": "main",
    }
    (duration,) = metric_points(observed.reader, "mcp_gateway.request.duration")
    assert duration.count == 1
    (up_duration,) = metric_points(observed.reader, "mcp_gateway.upstream.duration")
    assert dict(up_duration.attributes) == {"upstream": "main", "status_class": "2xx"}
    (lookup,) = metric_points(observed.reader, "mcp_gateway.resolver.lookups")
    assert dict(lookup.attributes) == {
        "resolver_type": "static",
        "cache": "none",
        "result": "credential",
    }


async def test_the_sessions_gauge_follows_the_registry(
    mapping_dir: Path, transport: httpx.MockTransport, token: str, upstream: FakeUpstream
) -> None:
    observed = build_observed(mapping_dir, transport)
    (initial,) = metric_points(observed.reader, "mcp_gateway.sessions.bound")
    assert initial.value == 0
    upstream.responder = lambda _r: httpx.Response(
        200, headers={"Mcp-Session-Id": "s-1"}, json={"jsonrpc": "2.0", "id": 1, "result": {}}
    )
    initialize = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
    async with client_for(observed.app) as http:
        await http.post("/mcp", json=initialize, headers=bearer(token))
    (bound,) = metric_points(observed.reader, "mcp_gateway.sessions.bound")
    assert bound.value == 1


async def test_the_session_flag_is_presence_only(
    mapping_dir: Path, transport: httpx.MockTransport, token: str, upstream: FakeUpstream
) -> None:
    observed = build_observed(mapping_dir, transport)
    async with client_for(observed.app) as http:
        await http.post(
            "/mcp", json=RPC_BODY, headers={**bearer(token), "Mcp-Session-Id": "unknown-session"}
        )
    (span,) = spans_named(observed.spans, REQUEST_SPAN)
    assert attributes_of(span)["mcp_gateway.session.present"] is True
    assert attributes_of(span)["mcp_gateway.outcome"] == "session_not_found"


# --- resolver cache results -------------------------------------------------------------


async def test_the_resolver_cache_result_is_hit_miss_or_negative(
    mapping_dir: Path,
    transport: httpx.MockTransport,
    rsa_key: rsa.RSAPrivateKey,
    token: str,
) -> None:
    observed = build_observed(
        mapping_dir,
        transport,
        resolver_cache={"positive_ttl_seconds": 60, "negative_ttl_seconds": 60, "max_entries": 10},
    )
    stranger = mint(rsa_key, overrides={"email": "stranger@example.com", "org": "nobody"})
    async with client_for(observed.app) as http:
        for who in (token, token, stranger, stranger):
            await http.post("/mcp", json=RPC_BODY, headers=bearer(who))
    results = [
        attributes_of(span)["mcp_gateway.resolver.cache"]
        for span in spans_named(observed.spans, RESOLVE_SPAN)
    ]
    assert results == ["miss", "hit", "miss", "negative"]
    lookups = {
        (point.attributes["cache"], point.attributes["result"]): point.value
        for point in metric_points(observed.reader, "mcp_gateway.resolver.lookups")
    }
    assert lookups == {
        ("miss", "credential"): 1,
        ("hit", "credential"): 1,
        ("miss", "denied"): 1,
        ("negative", "denied"): 1,
    }


# --- upstream errors ----------------------------------------------------------------------


async def test_an_unreachable_upstream_is_an_error_span_without_a_message(
    mapping_dir: Path, transport: httpx.MockTransport, token: str, upstream: FakeUpstream
) -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("secret-host-detail.internal refused", request=request)

    upstream.responder = refuse
    observed = build_observed(mapping_dir, transport)
    async with client_for(observed.app) as http:
        response = await http.post("/mcp", json=RPC_BODY, headers=bearer(token))
    assert response.status_code == 502
    (request_span,) = spans_named(observed.spans, REQUEST_SPAN)
    (upstream_span,) = spans_named(observed.spans, UPSTREAM_SPAN)
    for span in (request_span, upstream_span):
        assert span.status.status_code == StatusCode.ERROR
        assert span.status.description is None
        assert span.events == ()
    assert attributes_of(upstream_span)["mcp_gateway.upstream.error"] == "unreachable"
    assert "secret-host-detail" not in request_span.to_json() + upstream_span.to_json()
    (up_duration,) = metric_points(observed.reader, "mcp_gateway.upstream.duration")
    assert dict(up_duration.attributes) == {"upstream": "main", "status_class": "error"}


# --- request ids ----------------------------------------------------------------------------


async def test_the_clients_own_request_id_is_ignored_and_never_forwarded(
    mapping_dir: Path, transport: httpx.MockTransport, token: str, upstream: FakeUpstream
) -> None:
    observed = build_observed(mapping_dir, transport)
    async with client_for(observed.app) as http:
        response = await http.post(
            "/mcp", json=RPC_BODY, headers={**bearer(token), "X-Request-Id": "client-chosen"}
        )
    assert response.headers["x-request-id"] != "client-chosen"
    assert re.fullmatch(r"[0-9a-f-]{36}", response.headers["x-request-id"])
    assert "x-request-id" not in upstream.requests[0].headers


async def test_every_gateway_made_response_carries_the_request_id(
    mapping_dir: Path, transport: httpx.MockTransport
) -> None:
    observed = build_observed(mapping_dir, transport)
    async with client_for(observed.app) as http:
        unauthenticated = await http.post("/mcp", json=RPC_BODY)
    assert unauthenticated.status_code == 401
    assert re.fullmatch(r"[0-9a-f-]{36}", unauthenticated.headers["x-request-id"])


async def test_request_ids_are_unique_per_request(
    mapping_dir: Path, transport: httpx.MockTransport, token: str
) -> None:
    observed = build_observed(mapping_dir, transport)
    async with client_for(observed.app) as http:
        first = await http.post("/mcp", json=RPC_BODY, headers=bearer(token))
        second = await http.post("/mcp", json=RPC_BODY, headers=bearer(token))
    assert first.headers["x-request-id"] != second.headers["x-request-id"]


# --- inbound trace context ---------------------------------------------------------------


async def test_an_inbound_traceparent_is_a_link_never_the_parent(
    mapping_dir: Path, transport: httpx.MockTransport, token: str, upstream: FakeUpstream
) -> None:
    observed = build_observed(mapping_dir, transport)
    async with client_for(observed.app) as http:
        await http.post(
            "/mcp",
            json=RPC_BODY,
            headers={**bearer(token), "traceparent": TRACEPARENT, "tracestate": "k=v"},
        )
    (span,) = spans_named(observed.spans, REQUEST_SPAN)
    assert span.parent is None
    assert format(span.context.trace_id, "032x") != INBOUND_TRACE_ID
    (link,) = span.links
    assert format(link.context.trace_id, "032x") == INBOUND_TRACE_ID
    assert not link.context.trace_state
    # Nothing of it reaches the upstream.
    assert "traceparent" not in upstream.requests[0].headers
    assert "tracestate" not in upstream.requests[0].headers


@pytest.mark.parametrize(
    "headers",
    [
        [("traceparent", "garbage")],
        [("traceparent", TRACEPARENT), ("traceparent", TRACEPARENT)],
        [("traceparent", "00-00000000000000000000000000000000-b7ad6b7169203331-01")],
    ],
    ids=["malformed", "repeated", "all-zero-trace-id"],
)
async def test_an_unusable_traceparent_adds_no_link(
    mapping_dir: Path,
    transport: httpx.MockTransport,
    token: str,
    headers: list[tuple[str, str]],
) -> None:
    observed = build_observed(mapping_dir, transport)
    async with client_for(observed.app) as http:
        await http.post(
            "/mcp", json=RPC_BODY, headers=[("authorization", f"Bearer {token}"), *headers]
        )
    (span,) = spans_named(observed.spans, REQUEST_SPAN)
    assert span.links == ()


# --- nothing installed when disabled -----------------------------------------------------------


async def test_without_otlp_nothing_is_installed_and_the_audit_has_no_trace_id(
    mapping_dir: Path,
    transport: httpx.MockTransport,
    token: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    provider_before = trace.get_tracer_provider()
    data = base_config_dict(str(mapping_dir / "mapping.json"))
    app = create_app(config_from(data), http_transport=transport)
    with caplog.at_level(logging.INFO, logger="mcp_gateway.audit"):
        async with client_for(app) as http:
            response = await http.post("/mcp", json=RPC_BODY, headers=bearer(token))
    assert response.status_code == 200
    assert trace.get_tracer_provider() is provider_before
    (line,) = audit_lines(caplog)
    assert line["trace_id"] is None
    assert line["request_id"] == response.headers["x-request-id"]
    assert build_telemetry(config_from(data).observability).closers == []


async def test_injected_exporters_never_become_the_global_provider(
    mapping_dir: Path, transport: httpx.MockTransport
) -> None:
    before = trace.get_tracer_provider()
    build_observed(mapping_dir, transport)
    assert trace.get_tracer_provider() is before


# --- unexpected failure -----------------------------------------------------------------------


async def test_an_unexpected_exception_is_audited_and_answered_with_the_id(
    mapping_dir: Path,
    transport: httpx.MockTransport,
    token: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    observed = build_observed(
        mapping_dir, transport, resolver=FixedResolver(RuntimeError("secret-detail"))
    )
    with caplog.at_level(logging.INFO, logger="mcp_gateway.audit"):
        async with client_for(observed.app) as http:
            response = await http.post("/mcp", json=RPC_BODY, headers=bearer(token))
    assert response.status_code == 500
    assert response.json()["error"]["code"] == -32005
    assert "secret-detail" not in response.text
    (line,) = audit_lines(caplog)
    assert (line["outcome"], line["status"]) == ("internal_error", 500)
    assert response.headers["x-request-id"] == line["request_id"]
    (span,) = spans_named(observed.spans, REQUEST_SPAN)
    assert span.status.status_code == StatusCode.ERROR
    assert len(spans_named(observed.spans, RESOLVE_SPAN)) == 1, "the resolve span must be ended"


# --- every terminal path: one audit line, one span, one count --------------------------------


@dataclass
class Env:
    mapping_dir: Path
    transport: httpx.MockTransport
    idp: FakeIdp
    upstream: FakeUpstream
    rsa_key: rsa.RSAPrivateKey
    token: str
    observed: Observed

    def rebuild(self, **kwargs: Any) -> Observed:
        self.observed = build_observed(
            self.mapping_dir,
            kwargs.pop("transport", self.transport),
            spans=self.observed.spans,
            reader=self.observed.reader,
            **kwargs,
        )
        return self.observed

    async def post(self, observed: Observed, **kwargs: Any) -> httpx.Response:
        kwargs.setdefault("json", RPC_BODY)
        kwargs.setdefault("headers", bearer(self.token))
        async with client_for(observed.app) as http:
            return await http.post("/mcp", **kwargs)


type Scenario = Callable[[Env], Awaitable[None]]


async def _origin_refused(env: Env) -> None:
    await env.post(
        env.rebuild(), headers={**bearer(env.token), "Origin": "https://evil.example.com"}
    )


async def _unauthenticated(env: Env) -> None:
    await env.post(env.rebuild(), headers={})


async def _insufficient_scope(env: Env) -> None:
    await env.post(env.rebuild(auth__required_scopes=["mcp-gateway"]))


async def _idp_unavailable(env: Env) -> None:
    env.idp.keys.clear()
    env.idp.down = True
    await env.post(env.rebuild())


async def _request_body_timeout(env: Env) -> None:
    app = env.rebuild(limits__request_body_timeout_seconds=0.2).app
    delivered = False

    async def receive() -> dict[str, Any]:
        nonlocal delivered
        if not delivered:
            delivered = True
            return {"type": "http.request", "body": b"{", "more_body": True}
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    async def send(message: dict[str, Any]) -> None:
        return None

    await asyncio.wait_for(app(asgi_scope(env.token, b"x" * 50), receive, send), timeout=5)


async def _body_too_large(env: Env) -> None:
    await env.post(env.rebuild(limits__max_request_body_bytes=10), content=b"x" * 50, json=None)


async def _mcp_header_mismatch(env: Env) -> None:
    await env.post(env.rebuild(), headers={**bearer(env.token), "Mcp-Method": "tools/list"})


async def _duplicate_header(env: Env) -> None:
    await env.post(
        env.rebuild(),
        headers=[
            ("authorization", f"Bearer {env.token}"),
            ("mcp-session-id", "a"),
            ("mcp-session-id", "b"),
        ],
    )


async def _resolver_unavailable(env: Env) -> None:
    await env.post(env.rebuild(resolver=FixedResolver(ResolverUnavailableError("down"))))


async def _state_unavailable(env: Env) -> None:
    state, _ = state_with("check")
    await env.post(
        env.rebuild(state=state), headers={**bearer(env.token), "Mcp-Session-Id": "session-1"}
    )


async def _gateway_error(env: Env) -> None:
    await env.post(env.rebuild(resolver=FixedResolver(ResolverError("broken"))))


async def _denied(env: Env) -> None:
    await env.post(env.rebuild(resolver=FixedResolver(Denied("no_mapping"))))


async def _session_not_found(env: Env) -> None:
    await env.post(env.rebuild(), headers={**bearer(env.token), "Mcp-Session-Id": "nope"})


async def _upstream_error(env: Env) -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    env.upstream.responder = refuse
    await env.post(env.rebuild())


async def _upstream_auth_rejected(env: Env) -> None:
    env.upstream.responder = lambda _r: httpx.Response(401)
    await env.post(env.rebuild())


async def _upstream_stream_failed(env: Env) -> None:
    reply = b'data: {"jsonrpc":"2.0","id":7,"result":{}}\n\n'
    stream = TrackedStream([reply], fail_after=httpx.ReadError("boom"))
    env.upstream.responder = lambda _r: httpx.Response(
        200, headers={"Content-Type": "text/event-stream"}, stream=stream
    )
    with pytest.raises(UpstreamStreamAbortedError):
        await env.post(env.rebuild())


DENY_BLOCKED = {"default": {"allow": None, "deny": ["blocked"]}, "rules": []}


async def _tool_denied(env: Env) -> None:
    call = {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": "blocked"}}
    await env.post(env.rebuild(policies__tools=DENY_BLOCKED), json=call)


async def _batch_refused(env: Env) -> None:
    batch = [{"jsonrpc": "2.0", "id": 1, "method": "tools/list"}]
    await env.post(env.rebuild(policies__tools=DENY_BLOCKED), json=batch)


async def _tool_policy_failed(env: Env) -> None:
    env.upstream.responder = lambda _r: httpx.Response(
        200, headers={"Content-Type": "application/json"}, content=b"not json"
    )
    listing = {"jsonrpc": "2.0", "id": 7, "method": "tools/list"}
    await env.post(env.rebuild(policies__tools=DENY_BLOCKED), json=listing)


async def _forwarded(env: Env) -> None:
    await env.post(env.rebuild())


async def _client_disconnected(env: Env) -> None:
    async def never_answers(request: httpx.Request) -> httpx.Response:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    observed = env.rebuild(transport=routed_transport(env.idp, never_answers))
    await call_then_disconnect(observed.app, env.token, after_seconds=0.1)


async def _internal_error(env: Env) -> None:
    await env.post(env.rebuild(resolver=FixedResolver(RuntimeError("boom"))))


SCENARIOS: dict[str, Scenario] = {
    "origin_refused": _origin_refused,
    "unauthenticated": _unauthenticated,
    "insufficient_scope": _insufficient_scope,
    "idp_unavailable": _idp_unavailable,
    "request_body_timeout": _request_body_timeout,
    "body_too_large": _body_too_large,
    "mcp_header_mismatch": _mcp_header_mismatch,
    "duplicate_header": _duplicate_header,
    "resolver_unavailable": _resolver_unavailable,
    "state_unavailable": _state_unavailable,
    "gateway_error": _gateway_error,
    "denied": _denied,
    "session_not_found": _session_not_found,
    "upstream_error": _upstream_error,
    "upstream_auth_rejected": _upstream_auth_rejected,
    "upstream_stream_failed": _upstream_stream_failed,
    "tool_denied": _tool_denied,
    "batch_refused": _batch_refused,
    "tool_policy_failed": _tool_policy_failed,
    "forwarded": _forwarded,
    "client_disconnected": _client_disconnected,
    "internal_error": _internal_error,
}


def test_the_outcome_list_is_the_set_the_code_can_emit() -> None:
    literal = re.compile(r'outcome="([a-z_]+)"')
    emitted = {
        match
        for path in SRC.rglob("*.py")
        for match in literal.findall(path.read_text(encoding="utf-8"))
    }
    assert emitted == OUTCOMES
    assert set(SCENARIOS) == OUTCOMES, "every outcome needs a scenario that reaches it"


@pytest.mark.parametrize("outcome", sorted(SCENARIOS))
async def test_every_terminal_path_emits_exactly_one_audit_line_one_span_and_one_count(
    outcome: str,
    mapping_dir: Path,
    transport: httpx.MockTransport,
    idp: FakeIdp,
    upstream: FakeUpstream,
    rsa_key: rsa.RSAPrivateKey,
    token: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    env = Env(
        mapping_dir,
        transport,
        idp,
        upstream,
        rsa_key,
        token,
        Observed(Starlette(), InMemorySpanExporter(), InMemoryMetricReader()),
    )
    with caplog.at_level(logging.INFO, logger="mcp_gateway.audit"):
        await SCENARIOS[outcome](env)

    lines = audit_lines(caplog)
    assert [line["outcome"] for line in lines] == [outcome]
    (line,) = lines
    spans = spans_named(env.observed.spans, REQUEST_SPAN)
    assert len(spans) == 1, "the request span must be ended exactly once"
    assert attributes_of(spans[0])["mcp_gateway.outcome"] == outcome
    assert attributes_of(spans[0])["http.response.status_code"] == line["status"]
    assert attributes_of(spans[0])["mcp_gateway.request_id"] == line["request_id"]
    (counted,) = metric_points(env.observed.reader, "mcp_gateway.requests")
    assert counted.value == 1
    assert counted.attributes["outcome"] == outcome
    for span in env.observed.spans.get_finished_spans():
        assert span.end_time is not None


# --- what spans, metrics and the audit may not carry ----------------------------------------


SUBJECT = "subject-7f3a9c41"
EMAIL = "carol.private@client-secret.example"
ORGANIZATION = "org-secret-5521"
SESSION_ID = "session-secret-9d8c7b6a"
ARGUMENT = "classified-argument-text"
UPSTREAM_KEY = "fake-alien-key-real_looking_upstream_key_0123456789abcdef"
RESPONSE_TEXT = "classified-response-text"


async def test_no_secret_or_identifier_reaches_a_span_a_metric_or_the_audit_line(
    tmp_path: Path,
    transport: httpx.MockTransport,
    upstream: FakeUpstream,
    rsa_key: rsa.RSAPrivateKey,
    caplog: pytest.LogCaptureFixture,
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    (state / "carol.key").write_text(f"{UPSTREAM_KEY}\n")
    (state / "mapping.json").write_text(
        json.dumps(
            {
                "entries": [
                    {
                        "match": {"email": EMAIL},
                        "api_key_file": "carol.key",
                        "upstream": "main",
                        "labels": {"tier": "gold"},
                    }
                ]
            }
        )
    )
    token = mint(
        rsa_key,
        overrides={"sub": SUBJECT, "email": EMAIL, "email_verified": True, "org": ORGANIZATION},
    )
    payload = token.split(".")[1]
    upstream.responder = lambda request: httpx.Response(
        200,
        headers={"Mcp-Session-Id": SESSION_ID, "x-request-id": "up-1"},
        json={"jsonrpc": "2.0", "id": 7, "result": {"content": RESPONSE_TEXT}},
    )
    call = {
        "jsonrpc": "2.0",
        "id": 7,
        "method": "tools/call",
        "params": {"name": "search", "arguments": {"q": ARGUMENT}},
    }
    observed = build_observed(state, transport)
    with caplog.at_level(logging.DEBUG):
        async with client_for(observed.app) as http:
            await http.post(
                "/mcp",
                json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                headers=bearer(token),
            )
            await http.post(
                "/mcp",
                json=call,
                headers={**bearer(token), "Mcp-Session-Id": SESSION_ID, "traceparent": TRACEPARENT},
            )
            await http.post("/mcp", json=call, headers=bearer(token + "tampered"))
            await http.post(
                "/mcp", json=call, headers={**bearer(token), "Mcp-Session-Id": "another-secret-id"}
            )

    lines = audit_lines(caplog)
    assert len(lines) == 4, "the positive control: every request was audited"
    audit_text = "\n".join(json.dumps(line) for line in lines)
    # The audit log is where subject and organization live.
    assert SUBJECT in audit_text and ORGANIZATION in audit_text

    spans = observed.spans.get_finished_spans()
    assert {span.name for span in spans} >= {REQUEST_SPAN, RESOLVE_SPAN, UPSTREAM_SPAN}
    metrics = observed.reader.get_metrics_data()
    assert metrics is not None
    telemetry_text = "\n".join([*(span.to_json() for span in spans), metrics.to_json()])
    log_text = caplog.text + "\n".join(r.getMessage() for r in caplog.records)

    forbidden = {
        "token": token,
        "token payload": payload,
        "api key": UPSTREAM_KEY,
        "e-mail": EMAIL,
        "session id": SESSION_ID,
        "other session id": "another-secret-id",
        "tool argument": ARGUMENT,
        "response content": RESPONSE_TEXT,
    }
    for what, value in forbidden.items():
        assert value not in audit_text, f"{what} reached the audit line"
        assert value not in telemetry_text, f"{what} reached a span or a metric"
        assert value not in log_text, f"{what} reached a log"
    assert SUBJECT not in telemetry_text
    assert ORGANIZATION not in telemetry_text
    # The label (bounded by the resolver's data) is a span attribute and never a metric attribute.
    assert "mcp_gateway.label.tier" in telemetry_text
    assert "gold" not in metrics.to_json()


# --- metric cardinality ------------------------------------------------------------------------


def test_client_chosen_rpc_methods_are_bounded_for_metrics() -> None:
    assert bounded_rpc_method("tools/call") == "tools/call"
    assert bounded_rpc_method(None) == "none"
    assert bounded_rpc_method("tools/call/" + "x" * 5000) == "other"
    assert bounded_rpc_method("made-up-method-1234") == "other"


async def test_metrics_never_carry_the_tool_name_or_a_raw_method(
    mapping_dir: Path, transport: httpx.MockTransport, token: str
) -> None:
    observed = build_observed(mapping_dir, transport)
    body = {"jsonrpc": "2.0", "id": 3, "method": "vendor/odd-method-77", "params": {}}
    async with client_for(observed.app) as http:
        await http.post("/mcp", json=RPC_BODY, headers=bearer(token))
        await http.post("/mcp", json=body, headers=bearer(token))
    values = {
        value
        for point in metric_points(observed.reader, "mcp_gateway.requests")
        for value in point.attributes.values()
    }
    assert "search" not in values
    assert "vendor/odd-method-77" not in values
    assert {"tools/call", "other"} <= values
