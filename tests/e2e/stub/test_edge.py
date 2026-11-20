"""The gateway's front door against real identity providers: metadata, challenges, scopes, the
Origin policy and the MCP routing headers. Every test performs the whole OAuth journey first."""

from typing import Any

import httpx
import pytest

from e2e.harness import (
    CHAIN,
    MCP_ACCEPT,
    Provider,
    audiences,
    initialize_payload,
    obtain_grant,
    parse_challenge,
)
from e2e.stub.stack import (
    ADVERTISED_SCOPE,
    ALLOWED_ORIGIN,
    FOREIGN_ORIGIN,
    PERSONAS,
    STATIC_PROVIDERS,
    StubControl,
    StubSettings,
    bearer,
    echoed,
    error_code,
    open_session,
    origin_of,
    post,
    prm_url,
    tool_call,
)

pytestmark = pytest.mark.e2e_stub

static = pytest.mark.parametrize("provider", STATIC_PROVIDERS, ids=lambda p: p.name)
HEADER_MISMATCH = -32020
ORIGIN_REFUSED = -32008


@static
async def test_protected_resource_metadata_is_path_specific_and_the_root_is_404(
    settings: StubSettings,
    http: httpx.AsyncClient,
    provider: Provider,
    evidence: dict[str, Any],
) -> None:
    grant = await obtain_grant(settings, provider, PERSONAS["carol"])
    specific = await http.get(prm_url(provider))
    assert specific.status_code == 200, specific.text
    metadata = specific.json()
    assert metadata["resource"] == provider.gateway_url
    assert metadata["authorization_servers"] == [provider.issuer]
    # The token was issued for exactly the resource the metadata names.
    assert provider.gateway_url in audiences(grant.claims)

    root = await http.get(f"{origin_of(provider)}/.well-known/oauth-protected-resource")
    assert root.status_code == 404, root.text
    evidence.update(resource=metadata["resource"], root_status=root.status_code)


@static
async def test_challenge_names_the_metadata_and_the_scope(
    settings: StubSettings,
    http: httpx.AsyncClient,
    provider: Provider,
    evidence: dict[str, Any],
) -> None:
    grant = await obtain_grant(settings, provider, PERSONAS["carol"])
    assert grant.challenge["resource_metadata"] == prm_url(provider)
    assert grant.challenge["scope"] == ADVERTISED_SCOPE[provider.name]

    anonymous = await post(http, provider, None, initialize_payload())
    assert anonymous.status_code == 401
    challenge = parse_challenge(anonymous.headers["www-authenticate"])
    assert "error" not in challenge, "a request without a token is not an invalid token"
    assert challenge["resource_metadata"] == prm_url(provider)

    garbage = await post(http, provider, "not.a.jwt", initialize_payload())
    assert garbage.status_code == 401
    assert parse_challenge(garbage.headers["www-authenticate"])["error"] == "invalid_token"
    evidence.update(challenge=challenge)


async def test_token_without_the_required_scope_gets_403_insufficient_scope(
    settings: StubSettings,
    http: httpx.AsyncClient,
    stub: StubControl,
    evidence: dict[str, Any],
) -> None:
    alice = PERSONAS["alice"]
    narrow = await obtain_grant(settings, CHAIN, alice, scope="openid mcp-gateway")
    scopes = str(narrow.claims["scope"]).split()
    assert "mcp-write" not in scopes
    assert CHAIN.gateway_url in audiences(narrow.claims), (
        "the audience is right: only the scope is not"
    )

    refused = await post(http, CHAIN, narrow.token, tool_call("echo"))
    assert refused.status_code == 403, refused.text[:300]
    challenge = parse_challenge(refused.headers["www-authenticate"])
    assert challenge["error"] == "insufficient_scope"
    assert challenge["scope"] == "mcp-write"
    assert refused.json()["error"] == "insufficient_scope"
    assert await stub.requests() == [], "a refused request must not be forwarded"

    # Positive control: the same user with the scope the challenge advertises is served.
    full = await obtain_grant(settings, CHAIN, alice)
    assert "mcp-write" in str(full.claims["scope"]).split()
    served = await post(http, CHAIN, full.token, tool_call("echo"))
    assert echoed(served)["persona"] == "alice"
    evidence.update(refused_scopes=scopes, challenge=challenge)


@static
async def test_origin_allowed_refused_and_absent(
    settings: StubSettings,
    http: httpx.AsyncClient,
    stub: StubControl,
    provider: Provider,
    evidence: dict[str, Any],
) -> None:
    grant = await obtain_grant(settings, provider, PERSONAS["carol"])
    token = grant.token

    absent = await post(http, provider, token, tool_call("echo"))
    assert echoed(absent)["persona"] == "carol"

    allowed = await post(http, provider, token, tool_call("echo"), Origin=ALLOWED_ORIGIN)
    seen = echoed(allowed)
    assert seen["persona"] == "carol"
    assert "origin" not in seen["headers"], "the browser origin is not the upstream's business"

    seen = len(await stub.requests())
    refused = await post(http, provider, token, tool_call("echo"), Origin=FOREIGN_ORIGIN)
    assert refused.status_code == 403
    assert error_code(refused) == ORIGIN_REFUSED
    # The origin is judged before authentication: no token, still refused for the origin.
    unauthenticated = await post(http, provider, None, tool_call("echo"), Origin=FOREIGN_ORIGIN)
    assert unauthenticated.status_code == 403
    assert error_code(unauthenticated) == ORIGIN_REFUSED
    assert len(await stub.requests()) == seen, "a refused origin must not reach the upstream"
    evidence.update(allowed=ALLOWED_ORIGIN, refused=FOREIGN_ORIGIN)


@static
async def test_mcp_method_and_name_headers_must_match_the_body(
    settings: StubSettings,
    http: httpx.AsyncClient,
    stub: StubControl,
    provider: Provider,
    evidence: dict[str, Any],
) -> None:
    token = (await obtain_grant(settings, provider, PERSONAS["carol"])).token

    matching = await post(
        http, provider, token, tool_call("echo"), **{"Mcp-Method": "tools/call", "Mcp-Name": "echo"}
    )
    headers = echoed(matching)["headers"]
    assert (headers["mcp-method"], headers["mcp-name"]) == ("tools/call", "echo")

    seen = len(await stub.requests())
    wrong_method = await post(
        http, provider, token, tool_call("echo"), **{"Mcp-Method": "tools/list"}
    )
    wrong_name = await post(
        http, provider, token, tool_call("echo"), **{"Mcp-Method": "tools/call", "Mcp-Name": "hang"}
    )
    for refused in (wrong_method, wrong_name):
        assert refused.status_code == 400
        assert error_code(refused) == HEADER_MISMATCH
    assert len(await stub.requests()) == seen, "a mismatch must not reach the upstream"
    evidence.update(mismatch_code=HEADER_MISMATCH)


async def test_a_session_header_is_not_needed_to_list_tools_on_a_stateless_upstream(
    settings: StubSettings, http: httpx.AsyncClient
) -> None:
    # Positive control for the Origin and header tests above: plain requests are served.
    token = (await obtain_grant(settings, STATIC_PROVIDERS[0], PERSONAS["carol"])).token
    response = await http.post(
        STATIC_PROVIDERS[0].gateway_url,
        json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        headers=bearer(token, Accept="application/json, text/event-stream"),
    )
    names = [tool["name"] for tool in response.json()["result"]["tools"]]
    assert "echo" in names and "slow_sse" in names


@static
async def test_a_repeated_session_header_is_refused_with_400_and_not_forwarded(
    settings: StubSettings,
    http: httpx.AsyncClient,
    stub: StubControl,
    provider: Provider,
    evidence: dict[str, Any],
) -> None:
    # Alice is on the stateful upstream: she has a real session to present.
    token = (await obtain_grant(settings, provider, PERSONAS["alice"])).token
    session = await open_session(http, provider, token)
    # Positive control: the same request with one header is served.
    served = await post(http, provider, token, tool_call("echo"), **{"Mcp-Session-Id": session})
    assert served.status_code == 200, served.text[:300]

    seen = len(await stub.requests())
    repeated = await http.post(
        provider.gateway_url,
        json=tool_call("echo"),
        headers=[
            ("Authorization", f"Bearer {token}"),
            ("Accept", MCP_ACCEPT),
            ("Mcp-Session-Id", session),
            ("Mcp-Session-Id", session),
        ],
    )
    assert repeated.status_code == 400, repeated.text[:300]
    assert "x-request-id" in repeated.headers
    assert len(await stub.requests()) == seen, "a refused request must not be forwarded"
    evidence.update(status=repeated.status_code)
