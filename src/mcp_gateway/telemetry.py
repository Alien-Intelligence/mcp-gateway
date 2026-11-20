"""Traces and metrics through OpenTelemetry, with nothing installed globally.

The providers are built here and owned by the app: no global tracer or meter provider is ever
set, so a library in the same process cannot export through ours and ours cannot pick up
someone else's. With ``observability.otlp`` null (and nothing injected) the instruments are
the API's no-ops.

What may appear in a span or a metric is decided in one place, ``RequestObserver.finish``:
method, tool, outcome, status, upstream name, the resolver's labels (spans only), presence of a
session id, the gateway's own request id. Never the subject, e-mail, organization, a token,
a key, a session id, arguments or content: those exist in the audit log and nowhere else.
Errors are a span status without a message, never a recorded exception.
"""

import logging
import re
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter
from urllib.parse import urlsplit

import anyio
from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import (
    OTLPMetricExporter as GrpcMetricExporter,
)
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
    OTLPSpanExporter as GrpcSpanExporter,
)
from opentelemetry.exporter.otlp.proto.http.metric_exporter import (
    OTLPMetricExporter as HttpMetricExporter,
)
from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
    OTLPSpanExporter as HttpSpanExporter,
)
from opentelemetry.metrics import CallbackOptions, Meter, NoOpMeter, Observation
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import (
    MetricExporter,
    MetricReader,
    PeriodicExportingMetricReader,
)
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    SimpleSpanProcessor,
    SpanExporter,
)
from opentelemetry.sdk.trace.sampling import ALWAYS_ON
from opentelemetry.trace import Link, Span, SpanKind, StatusCode, Tracer
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from mcp_gateway.config import ConfigError, ObservabilityConfig, OtlpConfig

logger = logging.getLogger(__name__)

INSTRUMENTATION_NAME = "mcp_gateway"
DEFAULT_SERVICE_NAME = "mcp-gateway"
REQUEST_SPAN = "mcp-gateway request"
RESOLVE_SPAN = "mcp-gateway resolve_credential"
UPSTREAM_SPAN = "mcp-gateway upstream"
LABEL_ATTRIBUTE_PREFIX = "mcp_gateway.label."

# Seconds. The SDK default boundaries are tuned for milliseconds and would put every request
# in the first bucket.
_DURATION_BUCKETS = [0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120]
_MAX_TEXT = 256
# Slack on top of the export timeout for the shutdown of one provider.
_SHUTDOWN_SLACK_SECONDS = 2.0

# Outcomes that are failures of the gateway or of the service behind it, whatever the status.
_ERROR_OUTCOMES = frozenset({"upstream_stream_failed", "internal_error"})

# JSON-RPC methods are client-chosen text: only these become a metric attribute value.
KNOWN_RPC_METHODS = frozenset(
    {
        "initialize",
        "ping",
        "tools/list",
        "tools/call",
        "resources/list",
        "resources/read",
        "resources/templates/list",
        "resources/subscribe",
        "resources/unsubscribe",
        "prompts/list",
        "prompts/get",
        "completion/complete",
        "logging/setLevel",
        "sampling/createMessage",
        "roots/list",
        "elicitation/create",
        "notifications/initialized",
        "notifications/cancelled",
        "notifications/progress",
        "notifications/roots/list_changed",
    }
)

_HEADER_NAME = re.compile(r"[A-Za-z0-9!#$%&'*+.^_`|~-]+")
_propagator = TraceContextTextMapPropagator()


def bounded_rpc_method(method: str | None) -> str:
    """A metric-safe name for a JSON-RPC method: a known MCP method, ``other`` or ``none``."""
    if method is None:
        return "none"
    return method if method in KNOWN_RPC_METHODS else "other"


def status_class(status: int) -> str:
    return f"{status // 100}xx"


def _clip(value: str) -> str:
    return value[:_MAX_TEXT]


@dataclass(frozen=True)
class RequestSummary:
    """The fields of one finished request that telemetry may carry."""

    request_id: str
    http_method: str
    status: int
    outcome: str
    elapsed_seconds: float
    rpc_method: str | None
    tool: str | None
    upstream: str | None
    upstream_status: int | None
    labels: Mapping[str, str] | None
    session_present: bool
    reason: str | None


class _Instruments:
    def __init__(self, meter: Meter) -> None:
        self.requests = meter.create_counter(
            "mcp_gateway.requests",
            unit="{request}",
            description="MCP requests handled, by outcome, status class, JSON-RPC method and upstream.",
        )
        self.request_duration = meter.create_histogram(
            "mcp_gateway.request.duration",
            unit="s",
            description="Time from receipt to the end of the response (the end of the stream for SSE).",
            explicit_bucket_boundaries_advisory=_DURATION_BUCKETS,
        )
        self.upstream_duration = meter.create_histogram(
            "mcp_gateway.upstream.duration",
            unit="s",
            description="Time from sending the upstream request to its response headers.",
            explicit_bucket_boundaries_advisory=_DURATION_BUCKETS,
        )
        self.resolver_lookups = meter.create_counter(
            "mcp_gateway.resolver.lookups",
            unit="{lookup}",
            description="Credential lookups, by resolver type, cache result and result.",
        )


class ResolveStep:
    """The credential-resolution child span and its counter."""

    def __init__(self, observer: "RequestObserver", span: Span, resolver_type: str) -> None:
        self._observer = observer
        self._span = span
        self._resolver_type = resolver_type
        self._finished = False

    def finish(self, *, cache: str, result: str) -> None:
        """``cache``: hit, miss, negative or none. ``result``: credential, denied, ..."""
        if self._finished:
            return
        self._finished = True
        self._span.set_attribute("mcp_gateway.resolver.cache", cache)
        self._span.set_attribute("mcp_gateway.resolver.result", result)
        if result in ("unavailable", "error"):
            self._span.set_status(StatusCode.ERROR)
        self._span.end()
        self._observer.telemetry.instruments.resolver_lookups.add(
            1,
            {
                "resolver_type": self._resolver_type,
                "cache": cache,
                "result": result,
            },
        )


class UpstreamStep:
    """The upstream-call child span (to the response headers) and its histogram."""

    def __init__(self, observer: "RequestObserver", span: Span, upstream: str) -> None:
        self._observer = observer
        self._span = span
        self._upstream = upstream
        self._started = perf_counter()
        self._finished = False

    def finish(self, *, status: int | None, error: str | None = None) -> None:
        """``status`` is the upstream HTTP status; ``error`` is timeout, unreachable or aborted."""
        if self._finished:
            return
        self._finished = True
        if status is not None:
            self._span.set_attribute("http.response.status_code", status)
        if error is not None:
            self._span.set_attribute("mcp_gateway.upstream.error", error)
        if error is not None or (status is not None and status >= 500):
            self._span.set_status(StatusCode.ERROR)
        self._span.end()
        self._observer.telemetry.instruments.upstream_duration.record(
            perf_counter() - self._started,
            {
                "upstream": self._upstream,
                "status_class": "error" if status is None else status_class(status),
            },
        )


class RequestObserver:
    """One request's server span. ``finish`` ends it and records the request metrics."""

    def __init__(self, telemetry: "Telemetry", span: Span) -> None:
        self.telemetry = telemetry
        self._span = span
        self._finished = False

    @property
    def trace_id(self) -> str | None:
        context = self._span.get_span_context()
        return format(context.trace_id, "032x") if context.is_valid else None

    def _child(self, name: str, kind: SpanKind) -> Span:
        return self.telemetry.tracer.start_span(
            name, context=trace.set_span_in_context(self._span), kind=kind
        )

    def begin_resolve(self, resolver_type: str) -> ResolveStep:
        span = self._child(RESOLVE_SPAN, SpanKind.INTERNAL)
        span.set_attribute("mcp_gateway.resolver.type", resolver_type)
        return ResolveStep(self, span, resolver_type)

    def begin_upstream(self, upstream: str) -> UpstreamStep:
        span = self._child(UPSTREAM_SPAN, SpanKind.CLIENT)
        span.set_attribute("mcp_gateway.upstream", upstream)
        return UpstreamStep(self, span, upstream)

    def finish(self, summary: RequestSummary) -> None:
        if self._finished:
            return
        self._finished = True
        span = self._span
        if span.is_recording():
            span.set_attribute("http.request.method", summary.http_method)
            span.set_attribute("http.response.status_code", summary.status)
            span.set_attribute("mcp_gateway.outcome", summary.outcome)
            span.set_attribute("mcp_gateway.request_id", summary.request_id)
            span.set_attribute("mcp_gateway.session.present", summary.session_present)
            if summary.rpc_method is not None:
                span.set_attribute("mcp.method.name", _clip(summary.rpc_method))
            if summary.tool is not None:
                span.set_attribute("mcp.tool.name", _clip(summary.tool))
            if summary.upstream is not None:
                span.set_attribute("mcp_gateway.upstream", summary.upstream)
            if summary.upstream_status is not None:
                span.set_attribute("mcp_gateway.upstream.status_code", summary.upstream_status)
            if summary.reason is not None:
                span.set_attribute("mcp_gateway.reason", _clip(summary.reason))
            for key, value in (summary.labels or {}).items():
                span.set_attribute(LABEL_ATTRIBUTE_PREFIX + _clip(key), _clip(value))
            if summary.status >= 500 or summary.outcome in _ERROR_OUTCOMES:
                span.set_status(StatusCode.ERROR)
        span.end()
        attributes = {
            "outcome": summary.outcome,
            "status_class": status_class(summary.status),
            "rpc_method": bounded_rpc_method(summary.rpc_method),
            "upstream": summary.upstream or "none",
        }
        instruments = self.telemetry.instruments
        instruments.requests.add(1, attributes)
        instruments.request_duration.record(summary.elapsed_seconds, attributes)


class Telemetry:
    """Tracer, instruments and the shutdown of whatever providers were built."""

    def __init__(
        self,
        tracer: Tracer,
        meter: Meter,
        closers: list[Callable[[], Awaitable[None]]],
    ) -> None:
        self.tracer = tracer
        self.meter = meter
        self.instruments = _Instruments(meter)
        self.closers = closers

    @classmethod
    def noop(cls) -> "Telemetry":
        return cls(trace.NoOpTracer(), NoOpMeter(INSTRUMENTATION_NAME), [])

    def start_request(self, traceparents: list[str]) -> RequestObserver:
        """Open the server span. It is always a new root trace.

        An inbound ``traceparent`` is never the parent: a client choosing our trace and span
        ids could splice its requests into someone else's trace or collide with ours. A valid
        one is kept as a span link, which correlates without that authority.
        """
        links: list[Link] = []
        if len(traceparents) == 1:  # W3C: a repeated header is not a usable context
            remote = trace.get_current_span(
                _propagator.extract({"traceparent": traceparents[0]})
            ).get_span_context()
            if remote.is_valid:
                links.append(Link(remote))
        span = self.tracer.start_span(
            REQUEST_SPAN, context=Context(), kind=SpanKind.SERVER, links=links
        )
        return RequestObserver(self, span)

    def observe_sessions(self, count: Callable[[], int]) -> None:
        """Expose the number of bound upstream sessions as an observable gauge."""

        def callback(options: CallbackOptions) -> Iterable[Observation]:
            yield Observation(count())

        self.meter.create_observable_gauge(
            "mcp_gateway.sessions.bound",
            callbacks=[callback],
            unit="{session}",
            description="Upstream MCP sessions currently bound to a subject.",
        )


def read_headers_file(path: str) -> dict[str, str]:
    """Parse ``key=value`` lines (blank lines and ``#`` comments skipped). Never echoes values."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigError(
            f"otlp headers_file {path!r} cannot be read ({type(exc).__name__})"
        ) from exc
    headers: dict[str, str] = {}
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        name, separator, value = line.partition("=")
        name, value = name.strip(), value.strip()
        if not separator or not _HEADER_NAME.fullmatch(name) or not value:
            raise ConfigError(f"otlp headers_file {path!r} line {number} is not key=value")
        key = name.lower()
        if key in headers:
            raise ConfigError(f"otlp headers_file {path!r} line {number} repeats a header name")
        headers[key] = value
    if not headers:
        raise ConfigError(f"otlp headers_file {path!r} holds no header; use null for none")
    return headers


def _exporters(otlp: OtlpConfig) -> tuple[SpanExporter, MetricExporter]:
    headers = read_headers_file(otlp.headers_file) if otlp.headers_file is not None else None
    if otlp.protocol == "grpc":
        insecure = urlsplit(otlp.endpoint).scheme == "http"
        return (
            GrpcSpanExporter(
                endpoint=otlp.endpoint,
                insecure=insecure,
                headers=headers,
                timeout=otlp.timeout_seconds,
            ),
            GrpcMetricExporter(
                endpoint=otlp.endpoint,
                insecure=insecure,
                headers=headers,
                timeout=otlp.timeout_seconds,
            ),
        )
    base = otlp.endpoint.rstrip("/")
    return (
        HttpSpanExporter(
            endpoint=f"{base}/v1/traces", headers=headers, timeout=otlp.timeout_seconds
        ),
        HttpMetricExporter(
            endpoint=f"{base}/v1/metrics", headers=headers, timeout=otlp.timeout_seconds
        ),
    )


def bounded_shutdown(
    what: str, shutdown: Callable[[], object], timeout_seconds: float
) -> Callable[[], Awaitable[None]]:
    """Run a blocking provider shutdown (which flushes) off the loop, with a deadline."""

    async def close() -> None:
        try:
            with anyio.fail_after(timeout_seconds):
                await anyio.to_thread.run_sync(shutdown, abandon_on_cancel=True)
        except TimeoutError:
            logger.error(
                "%s did not shut down within %.1f s; pending data may be lost",
                what,
                timeout_seconds,
            )
            raise

    return close


def build_telemetry(
    config: ObservabilityConfig,
    *,
    span_exporter: SpanExporter | None = None,
    metric_reader: MetricReader | None = None,
) -> Telemetry:
    """Build the providers the config asks for.

    ``span_exporter`` and ``metric_reader`` replace the OTLP ones (tests inject in-memory
    ones); an injected span exporter is driven synchronously so a test reads it right away.
    """
    otlp = config.otlp
    if otlp is None and span_exporter is None and metric_reader is None:
        return Telemetry.noop()

    service_name = otlp.service_name if otlp else DEFAULT_SERVICE_NAME
    resource = Resource.create(
        {"service.name": service_name, **(otlp.resource_attributes if otlp else {})}
    )
    otlp_span: SpanExporter | None = None
    otlp_metric: MetricExporter | None = None
    if otlp is not None:
        otlp_span, otlp_metric = _exporters(otlp)
    interval_ms = int(otlp.export_interval_seconds * 1000) if otlp else None
    timeout_ms = int(otlp.timeout_seconds * 1000) if otlp else None
    shutdown_seconds = (otlp.timeout_seconds if otlp else 1.0) + _SHUTDOWN_SLACK_SECONDS

    closers: list[Callable[[], Awaitable[None]]] = []
    tracer: Tracer = trace.NoOpTracer()
    meter: Meter = NoOpMeter(INSTRUMENTATION_NAME)

    if span_exporter is not None or otlp_span is not None:
        provider = TracerProvider(resource=resource, sampler=ALWAYS_ON)
        if span_exporter is not None:
            provider.add_span_processor(SimpleSpanProcessor(span_exporter))
        elif otlp_span is not None and otlp is not None:
            provider.add_span_processor(
                BatchSpanProcessor(
                    otlp_span,
                    schedule_delay_millis=interval_ms or 5000,
                    export_timeout_millis=timeout_ms or 30000,
                )
            )
        tracer = provider.get_tracer(INSTRUMENTATION_NAME)
        closers.append(bounded_shutdown("the span exporter", provider.shutdown, shutdown_seconds))

    if metric_reader is not None or otlp_metric is not None:
        reader = metric_reader
        if reader is None and otlp_metric is not None:
            reader = PeriodicExportingMetricReader(
                otlp_metric, export_interval_millis=interval_ms, export_timeout_millis=timeout_ms
            )
        meter_provider = MeterProvider(
            resource=resource, metric_readers=[reader] if reader is not None else []
        )
        meter = meter_provider.get_meter(INSTRUMENTATION_NAME)
        closers.append(
            bounded_shutdown("the metric exporter", meter_provider.shutdown, shutdown_seconds)
        )

    return Telemetry(tracer, meter, closers)
