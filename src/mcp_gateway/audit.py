"""One structured JSON audit line per request. Never request or response content."""

import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from time import perf_counter
from typing import Any

audit_logger = logging.getLogger("mcp_gateway.audit")

_MAX_TEXT = 256


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
    _emitted: bool = False

    def emit(self, *, status: int, outcome: str) -> None:
        if not self.enabled or self._emitted:
            return
        self._emitted = True
        line: dict[str, Any] = {
            "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "event": "mcp_request",
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
            "latency_ms": round((perf_counter() - self.started) * 1000, 1),
            "upstream_status": self.upstream_status,
        }
        audit_logger.info(json.dumps(line, separators=(",", ":")))
