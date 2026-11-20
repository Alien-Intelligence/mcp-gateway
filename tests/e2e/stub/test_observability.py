"""Observability through gateway-chain: the request id on every response, and the telemetry the
gateway exports to the test collector (spans, metrics) with a scan proving it leaks nothing.
Real users, real tokens, the stub upstream, a real OpenTelemetry collector."""

import re
import uuid
from typing import Any

import httpx
import pytest

from e2e.harness import CHAIN, initialize_payload, obtain_grant
from e2e.stub.otlp import (
    DataPoint,
    Span,
    data_points_in,
    spans_in,
    wait_until,
    whole_output,
)
from e2e.stub.stack import (
    PERSONAS,
    StubControl,
    StubSettings,
    echoed,
    open_session,
    post,
    tool_call,
)

pytestmark = pytest.mark.e2e_stub

REQUEST_ID_HEADER = "x-request-id"
REQUEST_SPAN = "mcp-gateway request"
RESOLVE_SPAN = "mcp-gateway resolve_credential"
UPSTREAM_SPAN = "mcp-gateway upstream"
# The gateway exports every 2 s (compose.yaml); the collector flushes its files every second.
EXPORT_WAIT_SECONDS = 30.0
TOOL_MARKER_PREFIX = "tool-argument-marker-"


def assert_request_id(response: httpx.Response) -> str:
    request_id = response.headers[REQUEST_ID_HEADER]
    assert str(uuid.UUID(request_id, version=4)) == request_id, request_id
    return request_id


async def test_every_response_carries_a_request_id_and_an_inbound_one_is_not_forwarded(
    settings: StubSettings,
    http: httpx.AsyncClient,
    stub: StubControl,
    evidence: dict[str, Any],
) -> None:
    alice = PERSONAS["alice"]
    token = (await obtain_grant(settings, CHAIN, alice)).token
    chosen = "client-chosen-request-id"

    unauthenticated = await post(http, CHAIN, None, initialize_payload())
    assert unauthenticated.status_code == 401
    unauthenticated_id = assert_request_id(unauthenticated)

    unmapped = await post(
        http,
        CHAIN,
        (await obtain_grant(settings, CHAIN, PERSONAS["nomap"])).token,
        tool_call("echo"),
    )
    assert unmapped.status_code == 403
    unmapped_id = assert_request_id(unmapped)

    served = await post(http, CHAIN, token, tool_call("echo"), **{REQUEST_ID_HEADER: chosen})
    seen = echoed(served)
    served_id = assert_request_id(served)
    assert served_id != chosen, "the gateway mints its own id, whatever the client sent"
    assert REQUEST_ID_HEADER not in seen["headers"], (
        "an inbound x-request-id must not reach upstream"
    )
    assert len({unauthenticated_id, unmapped_id, served_id}) == 3

    # Positive control: the header was really on the inbound request, and the stub does record
    # the headers it receives (the echo lists them), so its absence above means something.
    assert "authorization" in seen["headers"]
    evidence.update(request_ids=[unauthenticated_id, unmapped_id, served_id], sent=chosen)


def find_request_span(settings: StubSettings, request_id: str) -> Span | None:
    for span in spans_in(settings.otel_dir):
        if span.attributes.get("mcp_gateway.request_id") == request_id:
            return span
    return None


def find_point(settings: StubSettings, metric: str, **attributes: str) -> DataPoint | None:
    for point in data_points_in(settings.otel_dir):
        if point.metric == metric and all(
            point.attributes.get(k) == v for k, v in attributes.items()
        ):
            return point
    return None


async def test_the_request_span_and_metrics_reach_the_collector_and_leak_nothing(
    settings: StubSettings,
    http: httpx.AsyncClient,
    stub: StubControl,
    evidence: dict[str, Any],
) -> None:
    # Bob is resolved by PostgreSQL and served by the stateful upstream, so the request can
    # carry a real session id.
    bob = PERSONAS["bob"]
    grant = await obtain_grant(settings, CHAIN, bob)
    session_id = await open_session(http, CHAIN, grant.token)
    marker = TOOL_MARKER_PREFIX + uuid.uuid4().hex

    response = await post(
        http,
        CHAIN,
        grant.token,
        tool_call("echo", marker=marker),
        **{"Mcp-Session-Id": session_id},
    )
    seen = echoed(response)
    request_id = assert_request_id(response)

    # Positive controls: every value the scan below looks for really travelled.
    api_key = settings.read_key("bob")
    assert seen["persona"] == "bob", "the resolved API key reached the upstream"
    assert seen["headers"]["mcp-session-id"] == session_id
    assert seen["arguments"] == {"marker": marker}
    assert grant.claims["email"] == bob.email
    subject = grant.claims["sub"]
    assert isinstance(subject, str) and subject

    span = await wait_until(
        lambda: find_request_span(settings, request_id),
        timeout=EXPORT_WAIT_SECONDS,
        what=f"the request span of {request_id}",
    )
    assert span.name == REQUEST_SPAN
    assert span.resource["service.name"] == "mcp-gateway"
    assert span.resource["deployment.environment"] == "dev-stack"
    expected = {
        "http.request.method": "POST",
        "http.response.status_code": 200,
        "mcp.method.name": "tools/call",
        "mcp.tool.name": "echo",
        "mcp_gateway.outcome": "forwarded",
        "mcp_gateway.request_id": request_id,
        "mcp_gateway.session.present": True,
        "mcp_gateway.upstream": "stateful",
        "mcp_gateway.upstream.status_code": 200,
        "mcp_gateway.label.client": "client-y",
    }
    assert {k: span.attributes.get(k) for k in expected} == expected

    # The same trace holds the credential resolution and the upstream call.
    trace = [s for s in spans_in(settings.otel_dir) if s.trace_id == span.trace_id]
    by_name = {s.name: s for s in trace}
    assert {REQUEST_SPAN, RESOLVE_SPAN, UPSTREAM_SPAN} <= by_name.keys(), sorted(by_name)
    resolve = by_name[RESOLVE_SPAN]
    assert resolve.attributes["mcp_gateway.resolver.type"] == "chain"
    assert resolve.attributes["mcp_gateway.resolver.result"] == "credential"
    upstream = by_name[UPSTREAM_SPAN]
    assert upstream.attributes["mcp_gateway.upstream"] == "stateful"
    assert upstream.attributes["http.response.status_code"] == 200

    metric_attributes = {
        "outcome": "forwarded",
        "status_class": "2xx",
        "rpc_method": "tools/call",
        "upstream": "stateful",
    }
    requests = await wait_until(
        lambda: find_point(settings, "mcp_gateway.requests", **metric_attributes),
        timeout=EXPORT_WAIT_SECONDS,
        what="the mcp_gateway.requests counter",
    )
    assert requests.value >= 1
    duration = await wait_until(
        lambda: find_point(settings, "mcp_gateway.request.duration", **metric_attributes),
        timeout=EXPORT_WAIT_SECONDS,
        what="the mcp_gateway.request.duration histogram",
    )
    assert duration.count is not None and duration.count >= 1
    lookups = find_point(settings, "mcp_gateway.resolver.lookups", resolver_type="chain")
    assert lookups is not None and lookups.value >= 1
    upstream_duration = find_point(
        settings, "mcp_gateway.upstream.duration", upstream="stateful", status_class="2xx"
    )
    assert upstream_duration is not None

    output = whole_output(settings.otel_dir)
    assert request_id in output, "positive control: the scan reads the exported data"
    leaked = {
        name: secret
        for name, secret in {
            "access token": grant.token,
            "API key": api_key,
            "e-mail": bob.email,
            "subject": subject,
            "session id": session_id,
            "tool argument": marker,
        }.items()
        if secret in output
    }
    assert not leaked, f"the telemetry holds: {sorted(leaked)}"
    assert (
        re.search(r"[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}", output) is None
    ), "no JWT-shaped string in the telemetry"
    evidence.update(
        request_id=request_id,
        trace_id=span.trace_id,
        spans=sorted(by_name),
        request_count=requests.value,
        scanned_bytes=len(output),
    )
