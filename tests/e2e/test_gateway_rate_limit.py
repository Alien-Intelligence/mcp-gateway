"""Per-subject rate limits on the real dev platform. Both static gateways allow a burst of 40
requests and refill 20 per minute per subject. Carol is used by no other test on these gateways.

The bucket is burnt with denied ``tools/call`` requests: the gateway refuses them locally, so
spending the bucket puts no load on the upstream and bills nothing."""

import asyncio
import time
from typing import Any

import httpx
import pytest

from e2e.harness import (
    HTTP_TIMEOUT,
    PROVIDERS,
    RATE_LIMITED,
    TOOL,
    Provider,
    Settings,
    assert_rate_limited_tool_result,
    is_rate_limited_tool_result,
    mcp_session,
    new_consumer_events,
    obtain_grant,
    raw_post,
    read_consumer_event,
    rpc_message,
    take_feed_baseline,
    tools_call_payload,
)

pytestmark = pytest.mark.e2e

provider_param = pytest.mark.parametrize("provider", PROVIDERS, ids=lambda p: p.name)

DENIED_TOOL = "datacluster_keyword_search"
INVALID_PARAMS = -32602
BURST = 40
MAX_BURN_REQUESTS = 60
SLACK = 5
SETTLE_FLOOR_SECONDS = 10.0


@provider_param
async def test_bucket_exhaustion_refuses_in_the_documented_shapes(
    settings: Settings, provider: Provider, evidence: dict[str, Any]
) -> None:
    carol = settings.personas["carol"]
    grant = await obtain_grant(settings, provider, carol)
    baseline = await take_feed_baseline(settings, carol)

    # Positive control: carol is metered when she is allowed through.
    async with mcp_session(provider.gateway_url, grant.token) as (client, _):
        result = await client.call_tool(TOOL, {"dataset_id": settings.dataset_ref})
    assert result.is_error is False
    metering_started = time.monotonic()
    consumer = await read_consumer_event(settings, carol, baseline)
    metering_seconds = time.monotonic() - metering_started
    assert consumer["success"] is True

    # The realm's tokens live 60 s and the feed took longer than that to show the event.
    grant = await obtain_grant(settings, provider, carol)
    denied_args = {"query": "burn", "dataset_ids": [settings.dataset_ref], "limit": 1}
    sent = 0
    limited = None
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as http:
        while sent < MAX_BURN_REQUESTS:
            sent += 1
            response = await raw_post(
                http,
                provider.gateway_url,
                grant.token,
                tools_call_payload(DENIED_TOOL, denied_args, request_id=sent),
            )
            if is_rate_limited_tool_result(response):
                limited = response
                break
            assert response.status_code == 200, (response.status_code, response.text[:300])
            assert rpc_message(response)["error"]["code"] == INVALID_PARAMS
        assert limited is not None, f"no rate limit after {sent} requests"
        # The wait for the feed refilled the bucket, so the burn meets a full one: BURST requests
        # pass, the next is refused (a few more only if a refill lands mid-loop).
        assert sent <= BURST + SLACK, f"limited only after {sent} requests"
        burn_retry_after = assert_rate_limited_tool_result(limited)

        allowed = await raw_post(
            http,
            provider.gateway_url,
            grant.token,
            tools_call_payload(TOOL, {"dataset_id": settings.dataset_ref}, request_id=1001),
        )
        allowed_retry_after = assert_rate_limited_tool_result(allowed)

        ping = await raw_post(
            http,
            provider.gateway_url,
            grant.token,
            {"jsonrpc": "2.0", "id": 1002, "method": "ping"},
        )
    assert ping.status_code == 200, (ping.status_code, ping.text[:300])
    ping_error = rpc_message(ping)["error"]
    assert ping_error["code"] == RATE_LIMITED
    assert ping_error["data"]["retry_after_seconds"] >= 1
    assert ping.headers["retry-after"] == str(ping_error["data"]["retry_after_seconds"])

    # The refused allowed call was not forwarded: the feed still holds the control's event only.
    # Wait as long as the control took to show up (a refused call's event could not be later).
    await asyncio.sleep(max(metering_seconds, SETTLE_FLOOR_SECONDS))
    events = await new_consumer_events(settings, carol, baseline)
    assert [e["id"] for e in events] == [consumer["id"]], events

    evidence.update(
        provider=provider.name,
        persona=carol.name,
        control_event_id=consumer["id"],
        metering_seconds=round(metering_seconds, 1),
        requests_until_limited=sent,
        burn_retry_after_seconds=burn_retry_after,
        allowed_call_retry_after_seconds=allowed_retry_after,
        ping_error=ping_error,
        events_since_baseline=[e["id"] for e in events],
    )
