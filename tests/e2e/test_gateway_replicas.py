"""The replica pair on the real dev platform: two gateway replicas behind nginx (round robin),
the Redis resolver and shared Redis state."""

from typing import Any

import pytest

from e2e.harness import (
    REPLICAS,
    TOOL,
    Settings,
    mcp_session,
    obtain_grant,
    take_feed_baseline,
    wait_for_new_consumer_events,
)

pytestmark = pytest.mark.e2e

CALLS = 4


async def test_every_call_through_the_load_balancer_is_billed(
    settings: Settings, evidence: dict[str, Any]
) -> None:
    """The dev MCP is stateless, so no session is bound to a replica: the requests of one
    client alternate between both replicas and each must be resolved and metered."""
    alice = settings.personas["alice"]
    grant = await obtain_grant(settings, REPLICAS, alice)
    baseline = await take_feed_baseline(settings, alice)

    async with mcp_session(REPLICAS.gateway_url, grant.token) as (client, _):
        for _ in range(CALLS):
            result = await client.call_tool(TOOL, {"dataset_id": settings.dataset_ref})
            assert result.is_error is False

    events, metering_seconds = await wait_for_new_consumer_events(
        settings, alice, baseline, at_least=CALLS
    )
    assert len(events) == CALLS, events
    assert all(e["action"] == "get-dataset" and e["success"] is True for e in events), events
    assert len({e["id"] for e in events}) == CALLS
    evidence.update(
        persona=alice.name,
        calls=CALLS,
        event_ids=[e["id"] for e in events],
        metering_seconds=round(metering_seconds, 1),
    )
