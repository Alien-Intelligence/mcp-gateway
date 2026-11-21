"""The committed JSON Schema must be the one the config model produces."""

import json
from typing import Any

import jsonschema
import pytest
from gen_config_schema import SCHEMA_PATH, main, render_schema

from mcp_gateway.config import ConfigError, parse_config


def test_committed_schema_is_current() -> None:
    assert SCHEMA_PATH.read_text(encoding="utf-8") == render_schema(), (
        "docs/config.schema.v1.json is stale: run `uv run python scripts/gen_config_schema.py`"
    )
    assert main(["--check"]) == 0


def test_schema_declares_version_1_and_requires_it() -> None:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    assert schema["properties"]["version"] == {"const": 1, "title": "Version", "type": "integer"}
    assert "version" in schema["required"]


def test_schema_accepts_a_valid_config(config_dict: dict[str, Any]) -> None:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    jsonschema.validate(config_dict, schema)


@pytest.mark.parametrize("version", [None, 0, 2, "1", True, 1.0, 1.5])
def test_missing_or_other_version_is_refused_naming_the_supported_one(
    config_dict: dict[str, Any], version: object
) -> None:
    if version is None:
        del config_dict["version"]
    else:
        config_dict["version"] = version
    with pytest.raises(ConfigError, match=r'"version": 1') as error:
        parse_config(json.dumps(config_dict))
    assert "only supported" in str(error.value)
