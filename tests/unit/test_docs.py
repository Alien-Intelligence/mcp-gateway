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
    assert sorted(defined) == list(range(-31015, -31000))


ROOT = DOC.parents[1]
_SKIPPED_DIRECTORIES = {".git", ".venv", "node_modules"}


def _markdown_files() -> list[Path]:
    """Every Markdown file of the repository, whatever its directory."""
    return sorted(
        path
        for path in ROOT.rglob("*.md")
        if not _SKIPPED_DIRECTORIES.intersection(path.relative_to(ROOT).parts)
    )


def test_every_markdown_file_of_the_repository_is_checked() -> None:
    names = {path.relative_to(ROOT).as_posix() for path in _markdown_files()}
    assert {
        "README.md",
        "docs/configuration.md",
        "docs/tutorial/fr/08-operations.md",
        "charts/mcp-gateway/README.md",
        "deploy/compose/README.md",
    } <= names


@pytest.mark.parametrize(
    "path", _markdown_files(), ids=lambda path: path.relative_to(ROOT).as_posix()
)
def test_no_markdown_file_uses_the_old_codes_or_meta_prefix(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    # The paragraph that explains why the family sits outside -32768..-32000 names the SDKs' own
    # -32001, which is not a gateway code.
    paragraphs = [p for p in text.split("\n\n") if not p.startswith("The gateway family is")]
    assert not re.search(r"-320(0[1-9]|1[0-4])\b", "\n\n".join(paragraphs))
    assert "club.alien" not in text
