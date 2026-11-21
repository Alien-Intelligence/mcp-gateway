"""The gateway end to end, as a real MCP client meets it. Nothing is mocked: two identity
providers, the gateway container, a real MCP server and the real Alien dev platform (metering).

Every test performs the whole journey itself (challenge, discovery, anonymous registration,
authorization through the real login form, code exchange) before the behaviour under test.
"""

import asyncio
import time
from typing import Any

import httpx
import pytest

from e2e.harness import (
    CLOCK_SKEW_SECONDS,
    EXPIRY_MARGIN_SECONDS,
    PROVIDERS,
    REVOCATION_DEADLINE_SECONDS,
    TOOL,
    DirectJobControl,
    Provider,
    Settings,
    audiences,
    mcp_session,
    mint_revoked_key,
    obtain_grant,
    parse_challenge,
    raw_initialize,
    read_consumer_event,
    read_sponsor_event,
    rpc_error,
    sdk_handshake_error_codes,
    take_feed_baseline,
    temporarily_replaced_key,
)

pytestmark = pytest.mark.e2e

provider_param = pytest.mark.parametrize("provider", PROVIDERS, ids=lambda p: p.name)

FORGED_HEADERS = {
    "x-oauth-access-token": "garbage",
    "x-mcp-config-id": "cfg_forged",
    "Cookie": "a=b",
}
NO_MAPPING = -31010
UPSTREAM_UNAUTHORIZED = -31001


def assert_invalid_token_challenge(response: httpx.Response) -> dict[str, str]:
    assert response.status_code == 401, response.text[:300]
    challenge = parse_challenge(response.headers["www-authenticate"])
    assert challenge["error"] == "invalid_token", challenge
    assert challenge["resource_metadata"], challenge
    return challenge


@provider_param
@pytest.mark.parametrize("persona_name", ["alice", "bob"])
async def test_happy_path_is_metered_for_the_client_and_the_sponsor(
    settings: Settings, provider: Provider, persona_name: str, evidence: dict[str, Any]
) -> None:
    persona = settings.personas[persona_name]
    grant = await obtain_grant(settings, provider, persona)
    baseline = await take_feed_baseline(settings, persona)

    async with mcp_session(provider.gateway_url, grant.token) as (client, _):
        listing = await client.list_tools()
        tools = {tool.name: tool for tool in listing.tools}
        assert "dataset_id" in tools[TOOL].input_schema["properties"]
        result = await client.call_tool(TOOL, {"dataset_id": settings.dataset_ref})
    assert result.is_error is False

    consumer = await read_consumer_event(settings, persona, baseline)
    assert consumer["user"]["id"] == persona.user_id
    assert consumer["organizationId"] == persona.org_id
    assert consumer["sourceType"] == "mcp"
    assert consumer["success"] is True
    assert consumer["jobId"] is None
    assert consumer["sponsored"] is None

    sponsor = await read_sponsor_event(settings, persona, baseline, str(consumer["id"]))
    assert sponsor["sponsored"]["groupId"] == settings.group_id
    assert sponsor["sponsored"]["organizationId"] == persona.org_id

    evidence.update(
        provider=provider.name,
        persona=persona.name,
        token=grant.token_evidence,
        tool_is_error=result.is_error,
        consumer_event_id=consumer["id"],
        sponsor_event_id=sponsor["id"],
    )


@provider_param
async def test_token_without_the_gateway_audience_is_refused(
    settings: Settings, provider: Provider, evidence: dict[str, Any]
) -> None:
    alice = settings.personas["alice"]
    grant = await obtain_grant(settings, provider, alice, **provider.wrong_audience)
    assert provider.gateway_url not in audiences(grant.claims)

    response = await raw_initialize(provider.gateway_url, grant.token)

    challenge = assert_invalid_token_challenge(response)
    evidence.update(
        provider=provider.name,
        persona=alice.name,
        token=grant.token_evidence,
        http_status=response.status_code,
        challenge_error=challenge["error"],
    )


@provider_param
async def test_expired_token_is_refused(
    settings: Settings, provider: Provider, evidence: dict[str, Any]
) -> None:
    alice = settings.personas["alice"]
    grant = await obtain_grant(settings, provider, alice)

    before = await raw_initialize(provider.gateway_url, grant.token)
    assert before.status_code == 200, before.text[:300]

    wait = grant.claims["exp"] + CLOCK_SKEW_SECONDS + EXPIRY_MARGIN_SECONDS - time.time()
    await asyncio.sleep(max(wait, 0.0))
    after = await raw_initialize(provider.gateway_url, grant.token)

    challenge = assert_invalid_token_challenge(after)
    evidence.update(
        provider=provider.name,
        persona=alice.name,
        token=grant.token_evidence,
        http_status_before_expiry=before.status_code,
        http_status_after_expiry=after.status_code,
        challenge_error=challenge["error"],
    )


@provider_param
async def test_user_without_mapping_is_denied_without_a_challenge(
    settings: Settings, provider: Provider, evidence: dict[str, Any]
) -> None:
    nomap = settings.personas["nomap"]
    grant = await obtain_grant(settings, provider, nomap)

    response = await raw_initialize(provider.gateway_url, grant.token)

    assert response.status_code == 403, response.text[:300]
    assert rpc_error(response)["code"] == NO_MAPPING
    assert "www-authenticate" not in response.headers

    sdk_codes = await sdk_handshake_error_codes(provider.gateway_url, grant.token)
    assert sdk_codes == [NO_MAPPING]

    evidence.update(
        provider=provider.name,
        persona=nomap.name,
        token=grant.token_evidence,
        http_status=response.status_code,
        rpc_error_code=NO_MAPPING,
        sdk_error_codes=sdk_codes,
    )


@provider_param
async def test_revoked_alien_key_gives_a_jsonrpc_error_not_a_challenge(
    settings: Settings, provider: Provider, evidence: dict[str, Any]
) -> None:
    alice = settings.personas["alice"]
    revoked_key = await asyncio.to_thread(mint_revoked_key, settings, alice)
    grant = await obtain_grant(settings, provider, alice)

    # Positive control: the upstream itself answers the revoked key with a real challenge
    # (revocation may take a moment to propagate, so wait for it, bounded).
    deadline = time.monotonic() + REVOCATION_DEADLINE_SECONDS
    delay = 1.0
    while True:
        direct = await raw_initialize(settings.upstream_url, revoked_key)
        if direct.status_code == 401 or time.monotonic() >= deadline:
            break
        await asyncio.sleep(delay)
        delay = min(delay * 2, 8.0)
    assert direct.status_code == 401, direct.text[:300]
    assert "www-authenticate" in direct.headers

    with temporarily_replaced_key(settings, alice, revoked_key):
        response = await raw_initialize(provider.gateway_url, grant.token)
        sdk_codes = await sdk_handshake_error_codes(provider.gateway_url, grant.token)

    assert response.status_code == 200, response.text[:300]
    assert rpc_error(response)["code"] == UPSTREAM_UNAUTHORIZED
    assert "www-authenticate" not in response.headers
    assert sdk_codes == [UPSTREAM_UNAUTHORIZED]

    evidence.update(
        provider=provider.name,
        persona=alice.name,
        token=grant.token_evidence,
        upstream_direct_status=direct.status_code,
        upstream_direct_has_challenge=True,
        gateway_http_status=response.status_code,
        gateway_rpc_error_code=UPSTREAM_UNAUTHORIZED,
        sdk_error_codes=sdk_codes,
    )


@provider_param
async def test_forged_headers_are_not_seen_upstream(
    settings: Settings,
    provider: Provider,
    direct_job_control: DirectJobControl,
    evidence: dict[str, Any],
) -> None:
    alice = settings.personas["alice"]
    foreign_org_id = settings.personas["bob"].org_id
    assert foreign_org_id is not None and foreign_org_id != alice.org_id
    control = await direct_job_control.outcome()
    # Positive control: sent straight to the upstream, a foreign job id is refused, so the header
    # does reach the backend when nothing strips it.
    assert control["is_error"] is True, control

    grant = await obtain_grant(settings, provider, alice)
    baseline = await take_feed_baseline(settings, alice)
    forged = {
        "x-job-id": str(settings.existing_job_id),
        "x-organization-id": str(foreign_org_id),
        **FORGED_HEADERS,
    }

    async with mcp_session(provider.gateway_url, grant.token) as (client, http):
        http.headers.update(forged)
        result = await client.call_tool(TOOL, {"dataset_id": settings.dataset_ref})
    assert result.is_error is False

    consumer = await read_consumer_event(settings, alice, baseline)
    assert consumer["jobId"] is None
    assert consumer["organizationId"] == alice.org_id
    assert consumer["success"] is True

    evidence.update(
        provider=provider.name,
        persona=alice.name,
        token=grant.token_evidence,
        forged_headers=sorted(forged),
        forged_organization_id=foreign_org_id,
        gateway_tool_is_error=result.is_error,
        gateway_event_id=consumer["id"],
        gateway_event_job_id=consumer["jobId"],
        gateway_event_organization_id=consumer["organizationId"],
        direct_control=control,
    )
