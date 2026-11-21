"""The stop budget follows from the configuration, and every supervisor setting allows it."""

import json
import re
from pathlib import Path
from typing import Any

import pytest

from mcp_gateway.config import GatewayConfig
from mcp_gateway.shutdown import (
    DETACHED_READS_SHUTDOWN_SECONDS,
    application_shutdown_seconds,
    in_flight_grace_seconds,
    stop_budget_seconds,
)
from mcp_gateway.telemetry import SHUTDOWN_SLACK_SECONDS

from .conftest import base_config_dict, config_from

ROOT = Path(__file__).resolve().parents[2]
OTLP: dict[str, Any] = {
    "endpoint": "http://collector.example.com:4318",
    "protocol": "http/protobuf",
    "headers_file": None,
    "allow_insecure_http": False,
    "timeout_seconds": 5,
    "export_interval_seconds": 10,
    "service_name": "mcp-gateway-test",
    "resource_attributes": {},
}


def config_with(read_timeouts: list[float], *, otlp: bool) -> GatewayConfig:
    data = base_config_dict()
    template = data["upstreams"]["main"]
    data["upstreams"] = {
        f"up{index}": {**template, "read_timeout_seconds": seconds}
        for index, seconds in enumerate(read_timeouts)
    }
    data["observability"] = {"otlp": OTLP if otlp else None}
    return config_from(data)


def test_in_flight_grace_is_the_largest_read_timeout_rounded_up() -> None:
    assert in_flight_grace_seconds(config_with([30, 130.5, 60], otlp=False)) == 131


def test_the_application_shutdown_is_two_read_deadlines_plus_both_telemetry_flushes() -> None:
    assert application_shutdown_seconds(config_with([5], otlp=False)) == (
        2 * DETACHED_READS_SHUTDOWN_SECONDS
    )
    with_otlp = application_shutdown_seconds(config_with([5], otlp=True))
    assert with_otlp == 2 * DETACHED_READS_SHUTDOWN_SECONDS + 2 * (5 + SHUTDOWN_SLACK_SECONDS)


def test_the_stop_budget_is_the_grace_plus_the_application_shutdown() -> None:
    config = config_with([130], otlp=True)
    assert stop_budget_seconds(config) == pytest.approx(
        130 + 0.1 + application_shutdown_seconds(config)
    )


def compose_stop_grace_seconds(text: str) -> int:
    match = re.search(r"^\s*stop_grace_period:\s*(\d+)s\s*$", text, re.MULTILINE)
    assert match, "the gateway service must set stop_grace_period in whole seconds"
    return int(match.group(1))


@pytest.mark.parametrize(
    "config",
    ["deploy/compose/config.static.example.json", "deploy/compose/config.store-redis.example.json"],
)
def test_compose_stop_grace_period_allows_the_stop_budget_of_its_configs(config: str) -> None:
    budget = stop_budget_seconds(config_from(json.loads((ROOT / config).read_text())))
    grace = compose_stop_grace_seconds((ROOT / "deploy/compose/compose.yaml").read_text())
    assert grace > budget, f"stop_grace_period {grace}s must exceed the stop budget {budget:.1f}s"
