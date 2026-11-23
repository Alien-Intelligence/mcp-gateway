"""Gateways that read the user's identity from the identity provider's userinfo endpoint
(``auth.userinfo``): real Keycloak and Hydra whose access tokens carry only the subject, the
gateway container and the fault-injection upstream stub. Real users, real tokens.

Every refusal has a positive control in the same test: the same user is served in the same run.
"""

from typing import Any

import httpx
import pytest

from e2e.harness import (
    IDENTITY_CLAIMS,
    USERINFO_MISMATCH_SCOPE,
    Provider,
    identity_claims_in_token,
    obtain_grant,
    parse_challenge,
)
from e2e.stub.otlp import Span, spans_in, wait_until
from e2e.stub.stack import (
    PERSONAS,
    USERINFO_DOWN,
    USERINFO_KEYCLOAK,
    USERINFO_PROVIDERS,
    USERINFO_UNAVAILABLE,
    StubControl,
    StubSettings,
    echoed,
    error_code,
    post,
    tool_call,
)

pytestmark = pytest.mark.e2e_stub

MISMATCH_DESCRIPTION = "userinfo does not match the token subject"
REQUEST_SPAN = "mcp-gateway request"
USERINFO_SPAN = "mcp-gateway userinfo"
REQUEST_ID_HEADER = "x-request-id"
# The gateway exports every 2 s (compose.yaml); the collector flushes its files every second.
EXPORT_WAIT_SECONDS = 30.0


@pytest.mark.parametrize("provider", USERINFO_PROVIDERS, ids=lambda p: p.name)
@pytest.mark.parametrize("who", ["carol", "dave"])
async def test_identity_from_userinfo_resolves_the_key_the_stub_sees(
    settings: StubSettings,
    http: httpx.AsyncClient,
    stub: StubControl,
    provider: Provider,
    who: str,
    evidence: dict[str, Any],
) -> None:
    grant = await obtain_grant(settings, provider, PERSONAS[who])
    # The token names the user and nothing else: the mapping (keyed by e-mail) can only have
    # been matched through the userinfo document.
    assert identity_claims_in_token(grant.claims) == [], IDENTITY_CLAIMS

    seen = echoed(await post(http, provider, grant.token, tool_call("echo")))
    # The stub names the persona whose key it received and echoes that name in place of the key.
    assert seen["persona"] == who
    assert seen["headers"]["authorization"] == f"persona:{who}"
    assert grant.token not in str(seen["headers"])
    evidence.update(provider=provider.name, persona=seen["persona"])


async def test_a_userinfo_subject_that_is_not_the_tokens_is_refused(
    settings: StubSettings,
    http: httpx.AsyncClient,
    stub: StubControl,
    evidence: dict[str, Any],
) -> None:
    carol = PERSONAS["carol"]
    mismatching = await obtain_grant(
        settings, USERINFO_KEYCLOAK, carol, scope=USERINFO_MISMATCH_SCOPE
    )
    normal = await obtain_grant(settings, USERINFO_KEYCLOAK, carol)
    assert identity_claims_in_token(mismatching.claims) == []

    # Positive control: the same user through the same gateway is served.
    assert (
        echoed(await post(http, USERINFO_KEYCLOAK, normal.token, tool_call("echo")))["persona"]
        == "carol"
    )
    seen = len(await stub.requests())

    refused = await post(http, USERINFO_KEYCLOAK, mismatching.token, tool_call("echo"))
    assert refused.status_code == 401
    challenge = parse_challenge(refused.headers["www-authenticate"])
    assert challenge["error"] == "invalid_token"
    assert challenge["error_description"] == MISMATCH_DESCRIPTION
    assert len(await stub.requests()) == seen, "no verified identity, no upstream call"
    evidence.update(challenge_error=challenge["error"], description=challenge["error_description"])


async def test_an_unreachable_userinfo_endpoint_gives_503_with_retry_after(
    settings: StubSettings,
    http: httpx.AsyncClient,
    stub: StubControl,
    evidence: dict[str, Any],
) -> None:
    carol = PERSONAS["carol"]
    # Positive control: the same user is served where the userinfo endpoint answers.
    served = await post(
        http,
        USERINFO_KEYCLOAK,
        (await obtain_grant(settings, USERINFO_KEYCLOAK, carol)).token,
        tool_call("echo"),
    )
    assert echoed(served)["persona"] == "carol"
    seen = len(await stub.requests())

    token = (await obtain_grant(settings, USERINFO_DOWN, carol)).token
    refused = await post(http, USERINFO_DOWN, token, tool_call("echo"))
    assert refused.status_code == 503
    assert error_code(refused) == USERINFO_UNAVAILABLE
    assert int(refused.headers["retry-after"]) > 0
    assert "www-authenticate" not in refused.headers
    assert len(await stub.requests()) == seen, "no identity, no upstream call"
    evidence.update(code=USERINFO_UNAVAILABLE, retry_after=refused.headers["retry-after"])


def userinfo_cache_outcome(settings: StubSettings, request_id: str) -> tuple[str, str] | None:
    """The cache and result attributes of the userinfo span in the trace of a request."""
    spans = spans_in(settings.otel_dir)
    request: Span | None = next(
        (
            s
            for s in spans
            if s.name == REQUEST_SPAN and s.attributes.get("mcp_gateway.request_id") == request_id
        ),
        None,
    )
    if request is None:
        return None
    lookups = [s for s in spans if s.trace_id == request.trace_id and s.name == USERINFO_SPAN]
    if len(lookups) != 1:
        return None
    attributes = lookups[0].attributes
    return attributes["mcp_gateway.userinfo.cache"], attributes["mcp_gateway.userinfo.result"]


async def test_one_token_is_looked_up_once_and_then_served_from_the_cache(
    settings: StubSettings,
    http: httpx.AsyncClient,
    stub: StubControl,
    evidence: dict[str, Any],
) -> None:
    token = (await obtain_grant(settings, USERINFO_KEYCLOAK, PERSONAS["dave"])).token
    ids: list[str] = []
    for _ in range(2):
        response = await post(http, USERINFO_KEYCLOAK, token, tool_call("echo"))
        assert echoed(response)["persona"] == "dave"
        ids.append(response.headers[REQUEST_ID_HEADER])

    # The gateway exports the userinfo span of each request; a fresh token's first lookup misses
    # (it called the identity provider), the second finds the entry.
    outcomes = [
        await wait_until(
            lambda request_id=request_id: userinfo_cache_outcome(settings, request_id),
            timeout=EXPORT_WAIT_SECONDS,
            what=f"the userinfo span of request {request_id}",
        )
        for request_id in ids
    ]
    assert outcomes == [("miss", "profile"), ("hit", "profile")]
    evidence.update(request_ids=ids, outcomes=outcomes)
