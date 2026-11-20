"""One structured JSON audit line per request. Never request or response content.

The same terminal call that writes the line ends the request's span and records its metrics,
so a request has exactly one audit line and one finished span, whatever way it ends.
"""

import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from time import perf_counter
from typing import Any

from mcp_gateway.telemetry import RequestObserver, RequestSummary, Telemetry

audit_logger = logging.getLogger("mcp_gateway.audit")

_MAX_TEXT = 256


# Every value ``AuditRecord.emit`` is called with. A test walks each of them.
OUTCOMES = frozenset(
    {
        "origin_refused",
        "unauthenticated",
        "insufficient_scope",
        "idp_unavailable",
        "request_body_timeout",
        "body_too_large",
        "mcp_header_mismatch",
        "duplicate_header",
        "resolver_unavailable",
        "state_unavailable",
        "gateway_error",
        "denied",
        "rate_limited",
        "session_not_found",
        "upstream_error",
        "upstream_auth_rejected",
        "upstream_stream_failed",
        "tool_denied",
        "batch_refused",
        "tool_policy_failed",
        "forwarded",
        "client_disconnected",
        "internal_error",
    }
)


def _noop_observer() -> RequestObserver:
    return Telemetry.noop().start_request([])


def _clip(value: str | int | None) -> str | int | None:
    return value[:_MAX_TEXT] if isinstance(value, str) else value


@dataclass
class AuditRecord:
    http_method: str
    enabled: bool
    started: float = field(default_factory=perf_counter)
    subject: str | None = None
    organization: str | None = None
    labels: dict[str, str] | None = None
    rpc_method: str | None = None
    rpc_id: str | int | None = None
    tool: str | None = None
    upstream: str | None = None
    upstream_status: int | None = None
    reason: str | None = None
    # Whether the upstream response carried an Mcp-Session-Id. Presence only, never the id.
    upstream_session: bool | None = None
    # Whether the client sent an Mcp-Session-Id. Presence only, never the id.
    session_present: bool = False
    # Generated here, returned to the client as x-request-id; an inbound one is never used.
    request_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    # The x-request-id the upstream sent, if any. It is not relayed to the client.
    upstream_request_id: str | None = None
    observer: RequestObserver = field(default_factory=_noop_observer)
    _emitted: bool = False

    def emit(self, *, status: int, outcome: str) -> None:
        if self._emitted:
            return
        self._emitted = True
        elapsed = perf_counter() - self.started
        try:
            self.observer.finish(
                RequestSummary(
                    request_id=self.request_id,
                    http_method=self.http_method,
                    status=status,
                    outcome=outcome,
                    elapsed_seconds=elapsed,
                    rpc_method=self.rpc_method,
                    tool=self.tool,
                    upstream=self.upstream,
                    upstream_status=self.upstream_status,
                    labels=self.labels,
                    session_present=self.session_present,
                    reason=self.reason,
                )
            )
        # Catching is allowed here, and only here: by the time emit runs the request's answer is
        # already decided or sent. Raising would abort a response the client is entitled to, or
        # crash a stream after its end, because recording telemetry broke. What is caught is
        # any Exception raised while ending the span or recording the metrics (not
        # BaseException: cancellation and exit still propagate). It is never silent: it is
        # logged at ERROR with the exception type and counted in
        # mcp_gateway.telemetry.failures{signal="record"}; the audit line below is still written.
        except Exception as exc:
            self.observer.telemetry.failures.increment("record")
            logging.getLogger(__name__).error(
                "recording request telemetry failed (%s)", type(exc).__name__, exc_info=exc
            )
        if not self.enabled:
            return
        line: dict[str, Any] = {
            "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "event": "mcp_request",
            "request_id": self.request_id,
            "trace_id": self.observer.trace_id,
            "subject": _clip(self.subject),
            "organization": _clip(self.organization),
            "labels": self.labels,
            "http_method": self.http_method,
            "rpc_method": _clip(self.rpc_method),
            "rpc_id": _clip(self.rpc_id),
            "tool": _clip(self.tool),
            "status": status,
            "outcome": outcome,
            "reason": self.reason,
            "upstream": self.upstream,
            "latency_ms": round(elapsed * 1000, 1),
            "upstream_status": self.upstream_status,
            "upstream_request_id": _clip(self.upstream_request_id),
            "upstream_session": self.upstream_session,
        }
        audit_logger.info(json.dumps(line, separators=(",", ":")))
