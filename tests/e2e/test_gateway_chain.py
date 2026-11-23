"""The resolver chain on the real dev platform: gateway-chain (Keycloak, a chain of Redis,
PostgreSQL and a signed webhook behind a cache, scope ``mcp-write`` required) in front of the real,
stateless dev MCP server. Each persona is known to exactly one store, so a metered call billed to
the right managed user proves that store answered."""

import uuid
from typing import Any

import httpx
import httpx2
import pytest

from e2e.harness import (
    CHAIN,
    CHAIN_STORE,
    HTTP_TIMEOUT,
    MCP_ACCEPT,
    TOOL,
    Settings,
    mcp_session,
    obtain_grant,
    read_consumer_event,
    read_sponsor_event,
    take_feed_baseline,
)

pytestmark = pytest.mark.e2e

REQUEST_ID_HEADER = "x-request-id"
METHOD_NOT_ALLOWED = 405


def assert_request_id(value: str | None) -> str:
    assert value is not None, "the response has no x-request-id"
    assert str(uuid.UUID(value, version=4)) == value, value
    return value


@pytest.mark.parametrize("persona_name", list(CHAIN_STORE), ids=lambda n: f"{n}-{CHAIN_STORE[n]}")
async def test_each_store_serves_its_persona_and_the_call_is_metered(
    settings: Settings, persona_name: str, evidence: dict[str, Any]
) -> None:
    persona = settings.personas[persona_name]
    grant = await obtain_grant(settings, CHAIN, persona)
    assert "mcp-write" in str(grant.claims["scope"]).split()
    baseline = await take_feed_baseline(settings, persona)
    request_ids: list[str] = []

    async def collect(response: httpx2.Response) -> None:
        request_ids.append(assert_request_id(response.headers.get(REQUEST_ID_HEADER)))

    async with mcp_session(CHAIN.gateway_url, grant.token) as (client, http):
        http.event_hooks["response"].append(collect)
        listing = await client.list_tools()
        assert TOOL in {tool.name for tool in listing.tools}
        result = await client.call_tool(TOOL, {"dataset_id": settings.dataset_ref})
    assert result.is_error is False
    assert len(request_ids) >= 2 and len(set(request_ids)) == len(request_ids)

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
        persona=persona.name,
        store=CHAIN_STORE[persona_name],
        token=grant.token_evidence,
        request_ids=request_ids,
        consumer_event_id=consumer["id"],
        consumer_user_id=consumer["user"]["id"],
        consumer_organization_id=consumer["organizationId"],
        sponsor_event_id=sponsor["id"],
        sponsor_group_id=sponsor["sponsored"]["groupId"],
    )


@pytest.mark.parametrize("method", ["GET", "DELETE"])
async def test_the_stateless_upstreams_405_passes_through_untouched(
    settings: Settings, method: str, evidence: dict[str, Any]
) -> None:
    alice = settings.personas["alice"]
    grant = await obtain_grant(settings, CHAIN, alice)
    headers = {"Accept": MCP_ACCEPT}

    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        # Positive control: the upstream itself answers 405 to the same request.
        direct = await client.request(
            method,
            settings.upstream_url,
            headers={**headers, "Authorization": f"Bearer {settings.read_key(alice)}"},
        )
        assert direct.status_code == METHOD_NOT_ALLOWED, (direct.status_code, direct.text[:300])

        through = await client.request(
            method,
            CHAIN.gateway_url,
            headers={**headers, "Authorization": f"Bearer {grant.token}"},
        )

    assert through.status_code == METHOD_NOT_ALLOWED, (through.status_code, through.text[:300])
    assert through.content == direct.content
    assert through.headers.get("allow") == direct.headers.get("allow")
    assert through.headers.get("content-type") == direct.headers.get("content-type")
    request_id = assert_request_id(through.headers.get(REQUEST_ID_HEADER))
    evidence.update(
        method=method,
        status=through.status_code,
        body=through.text[:200],
        allow=through.headers.get("allow"),
        request_id=request_id,
    )
