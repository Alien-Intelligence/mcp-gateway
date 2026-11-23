"""JSON-lines logging to stdout."""

import json
import logging
import sys
from datetime import UTC, datetime

from mcp_gateway.audit import audit_logger

# HTTP client libraries log every request line, URL included, at INFO. The URLs the gateway calls
# (key set, userinfo, webhook, upstreams) are configuration, not log content: the gateway's own
# lines name the setting instead. Their warnings and errors still come through.
QUIET_LIBRARY_LOGGERS = ("httpx", "httpcore")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        line: dict[str, str] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            line["exception"] = self.formatException(record.exc_info)
        return json.dumps(line, separators=(",", ":"))


def configure_logging() -> None:
    root = logging.getLogger()
    app_handler = logging.StreamHandler(sys.stdout)
    app_handler.setFormatter(JsonFormatter())
    root.handlers[:] = [app_handler]
    root.setLevel(logging.INFO)
    for name in QUIET_LIBRARY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)

    # Audit lines are already JSON documents: emit them verbatim.
    audit_handler = logging.StreamHandler(sys.stdout)
    audit_handler.setFormatter(logging.Formatter("%(message)s"))
    audit_logger.handlers[:] = [audit_handler]
    audit_logger.propagate = False
    audit_logger.setLevel(logging.INFO)
