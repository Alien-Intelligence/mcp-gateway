"""The configuration reference must match what the gateway accepts and writes."""

import json
import logging
import re
from pathlib import Path

import pytest

from mcp_gateway.audit import OUTCOMES, AuditRecord
from mcp_gateway.config import parse_config

DOC = Path(__file__).resolve().parents[2] / "docs" / "configuration.md"


def _section(start: str, end: str | None = None) -> str:
    text = DOC.read_text(encoding="utf-8").split(start, 1)[1]
    return text if end is None else text.split(end, 1)[0]


def test_the_complete_example_in_the_reference_is_valid_config() -> None:
    match = re.search(r"```json\n(.*?)\n```", _section("## Complete example"), re.DOTALL)
    assert match is not None
    config = parse_config(match.group(1))
    assert config.observability.otlp is not None
    assert config.observability.otlp.headers_file == "/run/secrets/otlp-headers"


def test_the_audit_keys_and_outcomes_in_the_reference_are_the_ones_emitted(
    caplog: pytest.LogCaptureFixture,
) -> None:
    section = _section("### Audit line", "## `observability`")
    documented = re.findall(r"^\| `([a-z_]+)` \|", section, re.MULTILINE)
    with caplog.at_level(logging.INFO, logger="mcp_gateway.audit"):
        AuditRecord(http_method="POST", enabled=True).emit(status=200, outcome="forwarded")
    (record,) = [r for r in caplog.records if r.name == "mcp_gateway.audit"]
    assert documented == list(json.loads(record.getMessage()))
    outcomes = re.search(r"`outcome` is one of: (.*?)\.\n", section)
    assert outcomes is not None
    assert set(re.findall(r"`([a-z_]+)`", outcomes.group(1))) == OUTCOMES
