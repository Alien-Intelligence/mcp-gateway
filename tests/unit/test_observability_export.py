"""The observability config, the headers file and real OTLP export to a local receiver."""

import copy
import gzip
import inspect
import json
import logging
import threading
import time
from collections.abc import Iterator, Sequence
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from opentelemetry.exporter.otlp.proto.http.metric_exporter import (
    OTLPMetricExporter as HttpMetricExporter,
)
from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import (
    ExportMetricsServiceRequest,
)
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from opentelemetry.proto.common.v1.common_pb2 import KeyValue
from opentelemetry.sdk.metrics import Counter, Histogram
from opentelemetry.sdk.metrics.export import (
    AggregationTemporality,
    MetricExporter,
    MetricExportResult,
    MetricsData,
)
from opentelemetry.sdk.metrics.view import DropAggregation
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult

from mcp_gateway.app import create_app
from mcp_gateway.config import ConfigError, OtlpConfig, parse_config
from mcp_gateway.telemetry import (
    REQUEST_SPAN,
    TelemetryFailures,
    _CountingMetricExporter,
    _CountingSpanExporter,
    _exporters,
    bounded_shutdown,
    build_telemetry,
    read_headers_file,
)

from .conftest import base_config_dict, config_from, mint
from .test_app import RPC_BODY, bearer, client_for

OTLP: dict[str, Any] = {
    "endpoint": "http://collector.example.com:4318",
    "protocol": "http/protobuf",
    "headers_file": None,
    "allow_insecure_http": False,
    "timeout_seconds": 5,
    "export_interval_seconds": 10,
    "service_name": "mcp-gateway-test",
    "resource_attributes": {"deployment.environment": "test"},
}


def with_otlp(**changes: Any) -> dict[str, Any]:
    data = base_config_dict()
    data["observability"] = {"otlp": {**copy.deepcopy(OTLP), **changes}}
    return data


# --- config ---------------------------------------------------------------------------------


def test_otlp_null_and_a_complete_otlp_block_are_both_valid() -> None:
    assert config_from(base_config_dict()).observability.otlp is None
    otlp = config_from(with_otlp()).observability.otlp
    assert otlp is not None
    assert (otlp.protocol, otlp.service_name) == ("http/protobuf", "mcp-gateway-test")


@pytest.mark.parametrize("key", sorted(OTLP))
def test_every_otlp_key_is_required(key: str) -> None:
    data = with_otlp()
    del data["observability"]["otlp"][key]
    with pytest.raises(ConfigError, match=key):
        parse_config(json.dumps(data))


def test_unknown_observability_keys_are_rejected() -> None:
    data = base_config_dict()
    data["observability"]["surprise"] = 1
    with pytest.raises(ConfigError, match="surprise"):
        parse_config(json.dumps(data))
    data = with_otlp()
    data["observability"]["otlp"]["surprise"] = 1
    with pytest.raises(ConfigError, match="surprise"):
        parse_config(json.dumps(data))


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"endpoint": "collector:4318"}, "http"),
        ({"endpoint": "ftp://collector.example.com"}, "http"),
        ({"endpoint": "https://user:pw@collector.example.com"}, "credentials"),
        ({"endpoint": "https://collector.example.com/?token=abc"}, "query"),
        ({"protocol": "http/json"}, "protocol"),
        ({"timeout_seconds": 0}, "timeout_seconds"),
        ({"export_interval_seconds": -1}, "export_interval_seconds"),
        ({"timeout_seconds": 30, "export_interval_seconds": 10}, "export_interval_seconds"),
        ({"service_name": ""}, "service_name"),
        ({"headers_file": ""}, "headers_file"),
        ({"allow_insecure_http": "yes"}, "allow_insecure_http"),
        ({"headers_file": "/run/secrets/otlp-headers"}, "allow_insecure_http"),
        ({"endpoint": "http://collector.example.com", "headers_file": "h"}, "allow_insecure_http"),
        ({"resource_attributes": {"service.name": "x"}}, "service_name"),
        ({"resource_attributes": {"": "x"}}, "non-empty"),
        ({"resource_attributes": {"k": 1}}, "resource_attributes"),
    ],
)
def test_invalid_otlp_values_are_refused_without_echoing_them(
    changes: dict[str, Any], message: str
) -> None:
    with pytest.raises(ConfigError, match=message) as raised:
        parse_config(json.dumps(with_otlp(**changes)))
    assert "pw@" not in str(raised.value)
    assert "token=abc" not in str(raised.value)


# --- the headers file -----------------------------------------------------------------------


def test_headers_file_lines_become_lowercase_headers(tmp_path: Path) -> None:
    path = tmp_path / "otlp.headers"
    path.write_text("# a comment\n\nAuthorization = Bearer s3cret=value\nX-Tenant=acme\n")
    assert read_headers_file(str(path)) == {
        "authorization": "Bearer s3cret=value",
        "x-tenant": "acme",
    }


@pytest.mark.parametrize(
    "text",
    [
        "",
        "# only a comment\n",
        "no-separator\n",
        "=value\n",
        "name=\n",
        "bad name=v\n",
        "a=1\nA=2\n",
    ],
    ids=[
        "empty",
        "comments-only",
        "no-equals",
        "no-name",
        "no-value",
        "space-in-name",
        "duplicate",
    ],
)
def test_a_malformed_headers_file_fails_startup_without_echoing_values(
    tmp_path: Path, text: str
) -> None:
    path = tmp_path / "otlp.headers"
    path.write_text(text)
    with pytest.raises(ConfigError) as raised:
        read_headers_file(str(path))
    assert str(path) in str(raised.value)


def test_a_missing_headers_file_fails_startup(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="cannot be read"):
        read_headers_file(str(tmp_path / "absent"))


def test_the_gateway_refuses_to_start_when_the_headers_file_is_missing(
    tmp_path: Path, mapping_dir: Path
) -> None:
    data = with_otlp(headers_file=str(tmp_path / "absent"), allow_insecure_http=True)
    data["resolver"]["mapping_file"] = str(mapping_dir / "mapping.json")
    with pytest.raises(ConfigError, match="cannot be read"):
        create_app(config_from(data))


# --- a real OTLP/HTTP receiver ---------------------------------------------------------------


class Receiver:
    """Accepts OTLP/HTTP protobuf posts and keeps what it received."""

    def __init__(self) -> None:
        self.posts: list[tuple[str, dict[str, str], bytes]] = []
        receiver = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers.get("content-length", "0"))
                body = self.rfile.read(length)
                if self.headers.get("content-encoding") == "gzip":
                    body = gzip.decompress(body)
                receiver.posts.append(
                    (self.path, {k.lower(): v for k, v in self.headers.items()}, body)
                )
                self.send_response(200)
                self.send_header("content-type", "application/x-protobuf")
                self.send_header("content-length", "0")
                self.end_headers()

            def log_message(self, format: str, *args: object) -> None:
                return None

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        host, port = self.server.server_address[:2]
        return f"http://{host!s}:{port}"

    def bodies(self, path: str) -> list[bytes]:
        return [body for p, _, body in self.posts if p == path]

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def receiver() -> Iterator[Receiver]:
    server = Receiver()
    server.start()
    yield server
    server.stop()


def attribute_map(attributes: Any) -> dict[str, Any]:
    def value(item: KeyValue) -> Any:
        kind = item.value.WhichOneof("value")
        return None if kind is None else getattr(item.value, kind)

    return {item.key: value(item) for item in attributes}


async def test_spans_and_metrics_reach_an_otlp_http_receiver_with_the_file_headers(
    tmp_path: Path,
    receiver: Receiver,
    transport: httpx.MockTransport,
    rsa_key: rsa.RSAPrivateKey,
    mapping_dir: Path,
) -> None:
    headers_file = tmp_path / "otlp.headers"
    headers_file.write_text("Authorization=Bearer otlp-auth-token-77\nX-Scope-Org=acme\n")
    data = base_config_dict(str(mapping_dir / "mapping.json"))
    data["observability"] = {
        "otlp": {
            **OTLP,
            "endpoint": receiver.url,
            "headers_file": str(headers_file),
            "allow_insecure_http": True,
            "export_interval_seconds": 30,
            "service_name": "gateway-under-test",
            "resource_attributes": {"deployment.environment": "unit"},
        }
    }
    app = create_app(config_from(data), http_transport=transport)
    async with app.router.lifespan_context(app):
        async with client_for(app) as http:
            response = await http.post("/mcp", json=RPC_BODY, headers=bearer(mint(rsa_key)))
        assert response.status_code == 200
    # Leaving the lifespan flushed both providers: nothing waited for the 30 s interval.

    trace_posts = [p for p in receiver.posts if p[0] == "/v1/traces"]
    metric_posts = [p for p in receiver.posts if p[0] == "/v1/metrics"]
    assert trace_posts and metric_posts
    for _, headers, _ in [*trace_posts, *metric_posts]:
        assert headers["authorization"] == "Bearer otlp-auth-token-77"
        assert headers["x-scope-org"] == "acme"
        assert headers["content-type"] == "application/x-protobuf"

    spans = []
    resource_attributes: dict[str, Any] = {}
    for body in receiver.bodies("/v1/traces"):
        export = ExportTraceServiceRequest.FromString(body)
        for resource_spans in export.resource_spans:
            resource_attributes = attribute_map(resource_spans.resource.attributes)
            spans += [s for scope in resource_spans.scope_spans for s in scope.spans]
    assert {span.name for span in spans} == {
        REQUEST_SPAN,
        "mcp-gateway resolve_credential",
        "mcp-gateway upstream",
    }
    assert resource_attributes["service.name"] == "gateway-under-test"
    assert resource_attributes["deployment.environment"] == "unit"
    (request_span,) = [s for s in spans if s.name == REQUEST_SPAN]
    assert attribute_map(request_span.attributes)["mcp_gateway.outcome"] == "forwarded"

    names: set[str] = set()
    for body in receiver.bodies("/v1/metrics"):
        export = ExportMetricsServiceRequest.FromString(body)
        for resource_metrics in export.resource_metrics:
            assert attribute_map(resource_metrics.resource.attributes)["service.name"] == (
                "gateway-under-test"
            )
            for scope in resource_metrics.scope_metrics:
                names |= {metric.name for metric in scope.metrics}
    assert {
        "mcp_gateway.requests",
        "mcp_gateway.request.duration",
        "mcp_gateway.upstream.duration",
        "mcp_gateway.resolver.lookups",
        "mcp_gateway.sessions.bound",
    } <= names


async def test_a_receiver_that_is_down_does_not_fail_requests_and_shutdown_is_bounded(
    transport: httpx.MockTransport,
    rsa_key: rsa.RSAPrivateKey,
    mapping_dir: Path,
) -> None:
    data = base_config_dict(str(mapping_dir / "mapping.json"))
    data["observability"] = {
        "otlp": {
            **OTLP,
            "endpoint": "http://127.0.0.1:9",  # the discard port: nothing listens
            "timeout_seconds": 1,
            "export_interval_seconds": 5,
        }
    }
    app = create_app(config_from(data), http_transport=transport)
    started = time.monotonic()
    async with app.router.lifespan_context(app):
        async with client_for(app) as http:
            response = await http.post("/mcp", json=RPC_BODY, headers=bearer(mint(rsa_key)))
        assert response.status_code == 200
    assert time.monotonic() - started < 15


async def test_a_blocking_shutdown_is_cut_off_and_reported(
    caplog: pytest.LogCaptureFixture,
) -> None:
    release = threading.Event()
    close = bounded_shutdown("the test exporter", lambda: release.wait(10), 0.2)
    started = time.monotonic()
    try:
        with caplog.at_level(logging.ERROR), pytest.raises(TimeoutError):
            await close()
    finally:
        release.set()
    assert time.monotonic() - started < 2
    assert "the test exporter did not shut down" in caplog.text


async def test_a_grpc_config_builds_exporters_and_shuts_down_cleanly() -> None:
    data = with_otlp(protocol="grpc", endpoint="http://127.0.0.1:9")
    telemetry = build_telemetry(config_from(data).observability)
    assert len(telemetry.closers) == 2
    for close in telemetry.closers:
        await close()


def test_the_allow_insecure_http_key_is_required() -> None:
    otlp = {k: v for k, v in OTLP.items() if k != "allow_insecure_http"}
    data = base_config_dict()
    data["observability"] = {"otlp": otlp}
    with pytest.raises(ConfigError, match="allow_insecure_http"):
        parse_config(json.dumps(data))


@pytest.mark.parametrize(
    "changes",
    [
        {"endpoint": "https://collector.example.com", "headers_file": "/run/h"},
        {"endpoint": "http://collector.example.com", "headers_file": None},
        {
            "endpoint": "http://collector.example.com",
            "headers_file": "/run/h",
            "allow_insecure_http": True,
        },
    ],
    ids=["https-with-headers", "http-without-headers", "http-with-headers-allowed"],
)
def test_the_auth_header_travels_in_clear_only_when_allowed(changes: dict[str, Any]) -> None:
    parse_config(json.dumps(with_otlp(**changes)))


# --- exporter failures are counted, never changed ---------------------------------------------


class _ScriptedSpanExporter(SpanExporter):
    def __init__(self, outcome: SpanExportResult | BaseException) -> None:
        self.outcome = outcome
        self.calls = 0

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        self.calls += 1
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome


class _ScriptedMetricExporter(MetricExporter):
    def __init__(self, outcome: MetricExportResult | BaseException) -> None:
        super().__init__(
            preferred_temporality={Counter: AggregationTemporality.DELTA},
            preferred_aggregation={Histogram: DropAggregation()},
        )
        self.outcome = outcome
        self.flushed: list[float] = []
        self.shut_down: list[float] = []

    def export(
        self, metrics_data: MetricsData, timeout_millis: float = 10_000, **kwargs: object
    ) -> MetricExportResult:
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome

    def force_flush(self, timeout_millis: float = 10_000) -> bool:
        self.flushed.append(timeout_millis)
        return True

    def shutdown(self, timeout_millis: float = 30_000, **kwargs: object) -> None:
        self.shut_down.append(timeout_millis)


@pytest.mark.parametrize(
    ("outcome", "counted"),
    [(SpanExportResult.SUCCESS, 0), (SpanExportResult.FAILURE, 1)],
)
def test_a_span_export_result_is_returned_unchanged_and_failures_counted(
    outcome: SpanExportResult, counted: int
) -> None:
    failures = TelemetryFailures()
    wrapper = _CountingSpanExporter(_ScriptedSpanExporter(outcome), failures)
    assert wrapper.export([]) is outcome
    assert failures.snapshot() == {"record": 0, "traces": counted, "metrics": 0}


def test_a_span_export_that_raises_is_counted_and_reraised_as_is() -> None:
    failures = TelemetryFailures()
    boom = ConnectionError("collector down")
    wrapper = _CountingSpanExporter(_ScriptedSpanExporter(boom), failures)
    with pytest.raises(ConnectionError) as raised:
        wrapper.export([])
    assert raised.value is boom
    assert failures.snapshot()["traces"] == 1


@pytest.mark.parametrize(
    ("outcome", "counted"),
    [(MetricExportResult.SUCCESS, 0), (MetricExportResult.FAILURE, 1)],
)
def test_a_metric_export_result_is_returned_unchanged_and_failures_counted(
    outcome: MetricExportResult, counted: int
) -> None:
    failures = TelemetryFailures()
    wrapper = _CountingMetricExporter(_ScriptedMetricExporter(outcome), failures)
    assert wrapper.export(MetricsData(resource_metrics=[])) is outcome
    assert failures.snapshot() == {"record": 0, "traces": 0, "metrics": counted}


def test_a_metric_export_that_raises_is_counted_and_reraised_as_is() -> None:
    failures = TelemetryFailures()
    boom = TimeoutError("collector slow")
    wrapper = _CountingMetricExporter(_ScriptedMetricExporter(boom), failures)
    with pytest.raises(TimeoutError) as raised:
        wrapper.export(MetricsData(resource_metrics=[]))
    assert raised.value is boom
    assert failures.snapshot()["metrics"] == 1


class _Stop(BaseException):
    """Stands for KeyboardInterrupt, SystemExit or a cancellation: not an export failure."""


def test_an_interrupt_in_an_export_is_not_counted_as_an_export_failure() -> None:
    failures = TelemetryFailures()
    span = _CountingSpanExporter(_ScriptedSpanExporter(_Stop()), failures)
    with pytest.raises(_Stop):
        span.export([])
    metric = _CountingMetricExporter(_ScriptedMetricExporter(_Stop()), failures)
    with pytest.raises(_Stop):
        metric.export(MetricsData(resource_metrics=[]))
    assert failures.snapshot() == {"record": 0, "traces": 0, "metrics": 0}


class _TimedShutdownSpanExporter(_ScriptedSpanExporter):
    def __init__(self) -> None:
        super().__init__(SpanExportResult.SUCCESS)
        self.shut_down: list[float] = []

    def shutdown(self, timeout_millis: float = 30_000, **kwargs: object) -> None:
        self.shut_down.append(timeout_millis)


def test_the_span_wrapper_shows_the_sdk_the_timeout_it_looks_for_and_forwards_it() -> None:
    """The SDK checks the exporter's shutdown for ``timeout_millis`` and passes the time left."""
    inner = _TimedShutdownSpanExporter()
    wrapper = _CountingSpanExporter(inner, TelemetryFailures())
    assert "timeout_millis" in inspect.getfullargspec(wrapper.shutdown).args
    wrapper.shutdown(timeout_millis=1234)
    assert inner.shut_down == [1234]


def test_the_span_wrapper_shuts_down_an_exporter_that_takes_no_timeout() -> None:
    inner = _ScriptedSpanExporter(SpanExportResult.SUCCESS)
    assert "timeout_millis" not in inspect.signature(inner.shutdown).parameters
    _CountingSpanExporter(inner, TelemetryFailures()).shutdown(timeout_millis=1234)


def test_the_wrappers_delegate_everything_else_to_the_wrapped_exporter() -> None:
    inner = _ScriptedMetricExporter(MetricExportResult.SUCCESS)
    wrapper = _CountingMetricExporter(inner, TelemetryFailures())
    assert wrapper._preferred_temporality == inner._preferred_temporality
    assert wrapper._preferred_aggregation == inner._preferred_aggregation
    assert wrapper.force_flush(123) is True
    wrapper.shutdown(456)
    assert (inner.flushed, inner.shut_down) == ([123], [456])
    # and the real OTLP exporter, as built from the config, keeps its own settings
    built_span, built_metric = _exporters(OtlpConfig.model_validate(OTLP), TelemetryFailures())
    assert isinstance(built_span, _CountingSpanExporter)
    assert isinstance(built_metric, _CountingMetricExporter)
    real = HttpMetricExporter(endpoint="http://collector.example.com:4318/v1/metrics")
    assert built_metric._preferred_temporality == real._preferred_temporality
    assert set(built_metric._preferred_aggregation or {}) == set(real._preferred_aggregation or {})


def test_the_failure_counts_are_exact_under_concurrent_exporters() -> None:
    failures = TelemetryFailures()
    threads = [
        threading.Thread(target=lambda: [failures.increment("traces") for _ in range(500)])
        for _ in range(8)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert failures.snapshot()["traces"] == 4000
