"""Gateways that read the user's identity from the identity provider's userinfo endpoint
(``auth.userinfo``), end to end: real Keycloak and Hydra whose access tokens carry only the
subject, the gateway container, a real MCP server and the real Alien dev platform (metering).

Every test performs the whole OAuth journey itself, and every refusal has a positive control in
the same test: the same user, through a gateway that works, is billed in the same run.
"""

from typing import Any

import httpx
import pytest

from e2e.harness import (
    HTTP_TIMEOUT,
    TOOL,
    USERINFO_DOWN,
    USERINFO_KEYCLOAK,
    USERINFO_MISMATCH_SCOPE,
    USERINFO_PROVIDERS,
    Provider,
    Settings,
    identity_claims_in_token,
    mcp_session,
    new_consumer_events,
    obtain_grant,
    parse_challenge,
    raw_post,
    read_consumer_event,
    read_sponsor_event,
    rpc_error,
    take_feed_baseline,
    tools_call_payload,
)

pytestmark = pytest.mark.e2e

USERINFO_UNAVAILABLE = -31015
MISMATCH_DESCRIPTION = "userinfo does not match the token subject"


async def assert_billed_for(
    settings: Settings, persona_name: str, provider: Provider, token: str, evidence: dict[str, Any]
) -> dict[str, Any]:
    """Call the tool through ``provider`` and read the metered event back from the feeds."""
    persona = settings.personas[persona_name]
    baseline = await take_feed_baseline(settings, persona)
    async with mcp_session(provider.gateway_url, token) as (client, _):
        result = await client.call_tool(TOOL, {"dataset_id": settings.dataset_ref})
    assert result.is_error is False

    consumer = await read_consumer_event(settings, persona, baseline)
    assert consumer["user"]["id"] == persona.user_id
    assert consumer["organizationId"] == persona.org_id
    assert consumer["sourceType"] == "mcp"
    assert consumer["success"] is True
    sponsor = await read_sponsor_event(settings, persona, baseline, str(consumer["id"]))
    assert sponsor["sponsored"]["groupId"] == settings.group_id
    assert sponsor["sponsored"]["organizationId"] == persona.org_id
    evidence.update(consumer_event_id=consumer["id"], sponsor_event_id=sponsor["id"])
    return consumer


@pytest.mark.parametrize("provider", USERINFO_PROVIDERS, ids=lambda p: p.name)
@pytest.mark.parametrize("persona_name", ["alice", "bob"])
async def test_identity_from_userinfo_is_metered_for_the_client_and_the_sponsor(
    settings: Settings, provider: Provider, persona_name: str, evidence: dict[str, Any]
) -> None:
    grant = await obtain_grant(settings, provider, settings.personas[persona_name])
    # Positive control for the gateway's answer below: the token names the user and nothing else,
    # so the managed user can only have been found through the userinfo document.
    assert identity_claims_in_token(grant.claims) == []
    assert isinstance(grant.claims["sub"], str) and grant.claims["sub"]

    await assert_billed_for(settings, persona_name, provider, grant.token, evidence)
    evidence.update(provider=provider.name, persona=persona_name, token=grant.token_evidence)


async def test_a_userinfo_subject_that_is_not_the_tokens_is_refused_and_not_billed(
    settings: Settings, evidence: dict[str, Any]
) -> None:
    alice = settings.personas["alice"]
    provider = USERINFO_KEYCLOAK
    mismatching = await obtain_grant(settings, provider, alice, scope=USERINFO_MISMATCH_SCOPE)
    assert identity_claims_in_token(mismatching.claims) == []
    normal = await obtain_grant(settings, provider, alice)
    assert normal.claims["sub"] == mismatching.claims["sub"]

    baseline = await take_feed_baseline(settings, alice)
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as http:
        refused = await raw_post(
            http,
            provider.gateway_url,
            mismatching.token,
            tools_call_payload(TOOL, {"dataset_id": settings.dataset_ref}),
        )
    assert refused.status_code == 401, refused.text[:300]
    challenge = parse_challenge(refused.headers["www-authenticate"])
    assert challenge["error"] == "invalid_token", challenge
    assert challenge["error_description"] == MISMATCH_DESCRIPTION, challenge

    # Positive control, same persona and gateway, same run: the normal token is billed.
    async with mcp_session(provider.gateway_url, normal.token) as (client, _):
        result = await client.call_tool(TOOL, {"dataset_id": settings.dataset_ref})
    assert result.is_error is False
    consumer = await read_consumer_event(settings, alice, baseline)
    assert consumer["user"]["id"] == alice.user_id
    assert consumer["organizationId"] == alice.org_id

    # The feed holds the control's event and nothing from the refused call.
    events = await new_consumer_events(settings, alice, baseline)
    assert [event["id"] for event in events] == [consumer["id"]]
    evidence.update(
        refused_status=refused.status_code,
        challenge_error=challenge["error"],
        challenge_description=challenge["error_description"],
        control_event_id=consumer["id"],
        new_events=len(events),
    )


async def test_an_unreachable_userinfo_endpoint_gives_503_and_is_not_billed(
    settings: Settings, evidence: dict[str, Any]
) -> None:
    alice = settings.personas["alice"]
    down = await obtain_grant(settings, USERINFO_DOWN, alice)
    working = await obtain_grant(settings, USERINFO_KEYCLOAK, alice)
    assert identity_claims_in_token(down.claims) == []

    baseline = await take_feed_baseline(settings, alice)
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as http:
        refused = await raw_post(
            http,
            USERINFO_DOWN.gateway_url,
            down.token,
            tools_call_payload(TOOL, {"dataset_id": settings.dataset_ref}),
        )
    assert refused.status_code == 503, refused.text[:300]
    assert int(refused.headers["retry-after"]) > 0
    assert rpc_error(refused)["code"] == USERINFO_UNAVAILABLE
    assert "www-authenticate" not in refused.headers

    # Positive control, same persona, same run: through the gateway whose userinfo endpoint
    # answers, the call is billed. A zero in the feed means something only next to it.
    async with mcp_session(USERINFO_KEYCLOAK.gateway_url, working.token) as (client, _):
        result = await client.call_tool(TOOL, {"dataset_id": settings.dataset_ref})
    assert result.is_error is False
    consumer = await read_consumer_event(settings, alice, baseline)
    assert consumer["user"]["id"] == alice.user_id

    events = await new_consumer_events(settings, alice, baseline)
    assert [event["id"] for event in events] == [consumer["id"]]
    evidence.update(
        refused_status=refused.status_code,
        retry_after=refused.headers["retry-after"],
        rpc_error_code=USERINFO_UNAVAILABLE,
        control_event_id=consumer["id"],
        new_events=len(events),
    )
