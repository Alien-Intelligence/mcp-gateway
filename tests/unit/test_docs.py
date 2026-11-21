"""The configuration reference must match what the gateway accepts and writes."""

import json
import logging
import re
from pathlib import Path

import pytest

from mcp_gateway import jsonrpc
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


def test_the_tool_policy_example_in_the_reference_is_valid_config() -> None:
    policy = re.search(r"```json\n(.*?)\n```", _section("### `policies.tools`"), re.DOTALL)
    complete = re.search(r"```json\n(.*?)\n```", _section("## Complete example"), re.DOTALL)
    assert policy is not None and complete is not None
    data = json.loads(complete.group(1))
    data["policies"]["tools"] = json.loads(policy.group(1))
    tools = parse_config(json.dumps(data)).policies.tools
    assert tools is not None
    assert len(tools.rules) == 2


PROTOCOL_DOC = DOC.with_name("protocol.md")


def test_the_protocol_contract_lists_every_gateway_code_with_its_constant() -> None:
    documented = {
        int(code): constant
        for code, constant in re.findall(
            r"^\| `(-31\d{3})` \| `([A-Z_]+)` \|", PROTOCOL_DOC.read_text(encoding="utf-8"), re.M
        )
    }
    defined = {
        value: name
        for name, value in vars(jsonrpc).items()
        if name.isupper() and isinstance(value, int) and -31999 <= value <= -31000
    }
    assert documented == defined
    assert sorted(defined) == list(range(-31014, -31000))


def test_the_configuration_reference_uses_the_current_codes_and_meta_prefix() -> None:
    text = DOC.read_text(encoding="utf-8")
    assert not re.search(r"-320(0[1-9]|1[0-4])\b", text)
    assert "club.alien" not in text
