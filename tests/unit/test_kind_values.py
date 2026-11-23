"""The kind install's Helm values carry a configuration the gateway accepts."""

import importlib.util
import json
from pathlib import Path
from types import ModuleType

from mcp_gateway.config import parse_config

_PATH = Path(__file__).resolve().parents[2] / "dev" / "kind" / "gateway_values.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("kind_gateway_values", _PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_kind_values_config_is_accepted_by_the_gateway_loader() -> None:
    values = _load().values("kind", "http://upstream.example.com/mcp")
    config = parse_config(json.dumps(values["config"]))
    assert config.version == 1
