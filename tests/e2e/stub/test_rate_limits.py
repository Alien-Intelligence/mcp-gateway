"""Rate limits shared by the two replicas (burst 8 per subject and 12 per organization, one token
refilled per minute; see ``HA_RATE_LIMITS`` in scripts/ci_e2e_stub.sh). Every test starts from
full buckets (``fresh_rate_limits``) and spends them within seconds, so the refill never decides
an outcome. Real users, real tokens, the stub as the upstream."""

import itertools
from collections.abc import Iterator
from typing import Any

import httpx
import pytest

from e2e.harness import Provider, obtain_grant
from e2e.stub.stack import (
    HA,
    HA_A,
    HA_B,
    PERSONAS,
    RATE_LIMITED,
    RETRY_AFTER_META_KEY,
    StubControl,
    StubSettings,
    error_code,
    notification,
    post,
    tool_call,
    tools_list,
)

pytestmark = [pytest.mark.e2e_stub, pytest.mark.usefixtures("fresh_rate_limits")]

SUBJECT_BURST = 8
ORGANIZATION_BURST = 12
# The organization's bucket is the subject's, and nothing else spends it: after erin's burst it
# holds this many tokens, plus at most one the refill gave back while the test ran.
ORGANIZATION_LEFT = ORGANIZATION_BURST - SUBJECT_BURST


def replicas() -> Iterator[Provider]:
    """Alternate between the two replicas, A first."""
    return itertools.cycle((HA_A, HA_B))


def served(response: httpx.Response) -> bool:
    """True when the gateway let a tools/list through (the stub answered it with a result)."""
    return response.status_code == 200 and "result" in response.json()


def refused_for_rate(response: httpx.Response) -> bool:
    return response.status_code == 200 and response.json().get("error", {}).get("code") == (
        RATE_LIMITED
    )


async def test_the_subject_burst_is_shared_by_the_replicas_and_each_refusal_is_readable(
    settings: StubSettings,
    http: httpx.AsyncClient,
    stub: StubControl,
    evidence: dict[str, Any],
) -> None:
    token = (await obtain_grant(settings, HA, PERSONAS["alice"])).token
    alternate = replicas()
    for number in range(SUBJECT_BURST):
        response = await post(http, next(alternate), token, tools_list())
        assert served(response), f"request {number + 1} of the burst: {response.text[:300]}"

    # The bucket is empty on both replicas: A and B take turns refusing, whatever the method.
    ping = await post(http, next(alternate), token, {"jsonrpc": "2.0", "id": 5, "method": "ping"})
    assert ping.status_code == 200
    assert error_code(ping) == RATE_LIMITED
    seconds = ping.json()["error"]["data"]["retry_after_seconds"]
    assert seconds >= 1
    assert int(ping.headers["retry-after"]) == seconds

    call = await post(http, next(alternate), token, tool_call("echo"))
    assert call.status_code == 200
    result = call.json()["result"]
    assert result["isError"] is True, "a tools/call refusal is a tool error the model can read"
    assert "Rate limit" in result["content"][0]["text"]
    assert result["_meta"][RETRY_AFTER_META_KEY] >= 1
    assert int(call.headers["retry-after"]) >= 1

    nobody_to_tell = await post(http, next(alternate), token, notification())
    assert nobody_to_tell.status_code == 429
    assert int(nobody_to_tell.headers["retry-after"]) >= 1

    # Positive control for "refused means not forwarded": the stub saw the burst and only it.
    methods = [record["rpc_method"] for record in await stub.requests()]
    assert methods == ["tools/list"] * SUBJECT_BURST
    evidence.update(burst=SUBJECT_BURST, retry_after=seconds)


async def test_the_organization_bucket_is_shared_by_its_users(
    settings: StubSettings,
    http: httpx.AsyncClient,
    stub: StubControl,
    evidence: dict[str, Any],
) -> None:
    erin = (await obtain_grant(settings, HA, PERSONAS["erin"])).token
    frank = (await obtain_grant(settings, HA, PERSONAS["frank"])).token
    alternate = replicas()

    for number in range(SUBJECT_BURST):
        response = await post(http, next(alternate), erin, tools_list())
        assert served(response), f"erin request {number + 1}: {response.text[:300]}"
    own_limit = await post(
        http, next(alternate), erin, {"jsonrpc": "2.0", "id": 5, "method": "ping"}
    )
    assert refused_for_rate(own_limit), "erin's own bucket is empty"

    # Frank's own bucket is full, so what stops him is the organization's.
    allowed = 0
    for _ in range(SUBJECT_BURST):
        response = await post(http, next(alternate), frank, tools_list())
        if not served(response):
            assert refused_for_rate(response), response.text[:300]
            break
        allowed += 1
    else:
        pytest.fail("the organization bucket never refused frank")
    assert ORGANIZATION_LEFT <= allowed <= ORGANIZATION_LEFT + 1, allowed
    assert allowed < SUBJECT_BURST, "frank's own bucket still had tokens"
    evidence.update(erin=SUBJECT_BURST, frank_allowed=allowed)
