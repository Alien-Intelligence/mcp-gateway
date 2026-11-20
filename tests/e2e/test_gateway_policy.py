"""The tool policy on the real dev platform. Both static gateways run ``default deny
[datacluster_keyword_search, policy_denied_probe]`` with one rule: the ``client=client-y`` label
(bob) may only use ``datacluster_get_dataset``, ``datacluster_list_datasets`` and ``echo``.

The claim that matters is billing: a denied call is refused at the gateway, so the platform must
never see it. An absence is only evidence next to a positive control, so the same call is made
through ``gateway-chain`` (no policy) and shows up as exactly one event."""

import asyncio
from typing import Any

import httpx
import pytest

from e2e.harness import (
    CHAIN,
    FEED_DEADLINE_SECONDS,
    HTTP_TIMEOUT,
    PROVIDERS,
    TOOL,
    Provider,
    Settings,
    mcp_session,
    new_consumer_events,
    obtain_grant,
    raw_post,
    read_consumer_event,
    read_sponsor_event,
    rpc_message,
    take_feed_baseline,
    tools_call_payload,
    wait_for_new_consumer_events,
)

pytestmark = pytest.mark.e2e

provider_param = pytest.mark.parametrize("provider", PROVIDERS, ids=lambda p: p.name)

DENIED_TOOL = "datacluster_keyword_search"
BOB_ALLOWED = {"datacluster_get_dataset", "datacluster_list_datasets", "echo"}
INVALID_PARAMS = -32602
SETTLE_FLOOR_SECONDS = 10.0


def keyword_search_arguments(settings: Settings) -> dict[str, Any]:
    return {"query": "policy", "dataset_ids": [settings.dataset_ref], "limit": 1}


@provider_param
async def test_tools_list_is_filtered_per_persona(
    settings: Settings, provider: Provider, evidence: dict[str, Any]
) -> None:
    listings: dict[str, set[str]] = {}
    for name in ("alice", "bob"):
        grant = await obtain_grant(settings, provider, settings.personas[name])
        async with mcp_session(provider.gateway_url, grant.token) as (client, _):
            listing = await client.list_tools()
        listings[name] = {tool.name for tool in listing.tools}

    assert DENIED_TOOL not in listings["alice"]
    assert TOOL in listings["alice"]
    assert listings["bob"] and listings["bob"] <= BOB_ALLOWED
    evidence.update(
        provider=provider.name,
        alice_tools=sorted(listings["alice"]),
        bob_tools=sorted(listings["bob"]),
    )


@provider_param
async def test_denied_tool_is_refused_and_never_billed(
    settings: Settings, provider: Provider, evidence: dict[str, Any]
) -> None:
    alice = settings.personas["alice"]
    arguments = keyword_search_arguments(settings)
    grant = await obtain_grant(settings, provider, alice)
    chain_grant = await obtain_grant(settings, CHAIN, alice)
    baseline = await take_feed_baseline(settings, alice)

    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as http:
        denied = await raw_post(
            http, provider.gateway_url, grant.token, tools_call_payload(DENIED_TOOL, arguments)
        )
        # Positive control: the same call through the gateway without a policy runs upstream.
        control = await raw_post(
            http, CHAIN.gateway_url, chain_grant.token, tools_call_payload(DENIED_TOOL, arguments)
        )

    assert denied.status_code == 200, (denied.status_code, denied.text[:300])
    error = rpc_message(denied)["error"]
    assert error["code"] == INVALID_PARAMS
    assert error["message"] == f"Unknown tool: {DENIED_TOOL}", error
    assert control.status_code == 200, (control.status_code, control.text[:300])
    assert "result" in rpc_message(control), control.text[:300]

    events, metering_seconds = await wait_for_new_consumer_events(
        settings, alice, baseline, at_least=1
    )
    assert len(events) == 1, events
    # A denied call's event could show up no later than the control's; wait as long again.
    await asyncio.sleep(min(max(metering_seconds, SETTLE_FLOOR_SECONDS), FEED_DEADLINE_SECONDS))
    events = await new_consumer_events(settings, alice, baseline)
    assert len(events) == 1, f"the feed holds more than the control's event: {events}"
    assert events[0]["action"] == "keyword-search", events[0]
    assert events[0]["success"] is True

    evidence.update(
        provider=provider.name,
        persona=alice.name,
        denied_http_status=denied.status_code,
        denied_error=error,
        control_event_id=events[0]["id"],
        control_event_action=events[0]["action"],
        events_since_baseline=[e["id"] for e in events],
        metering_seconds=round(metering_seconds, 1),
    )


@provider_param
async def test_allowed_tool_through_the_policy_is_billed(
    settings: Settings, provider: Provider, evidence: dict[str, Any]
) -> None:
    alice = settings.personas["alice"]
    grant = await obtain_grant(settings, provider, alice)
    baseline = await take_feed_baseline(settings, alice)

    async with mcp_session(provider.gateway_url, grant.token) as (client, _):
        result = await client.call_tool(TOOL, {"dataset_id": settings.dataset_ref})
    assert result.is_error is False

    consumer = await read_consumer_event(settings, alice, baseline)
    assert consumer["success"] is True
    sponsor = await read_sponsor_event(settings, alice, baseline, str(consumer["id"]))
    evidence.update(
        provider=provider.name,
        persona=alice.name,
        consumer_event_id=consumer["id"],
        sponsor_event_id=sponsor["id"],
    )
