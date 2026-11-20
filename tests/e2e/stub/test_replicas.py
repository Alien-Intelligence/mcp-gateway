"""Two gateway replicas sharing their state through Redis: sessions and the resolver cache.

Every test posts to the replicas directly (A on 18005, B on 18006) so that which replica served a
request is known, with a token issued for the pair's public URL; the load balancer is used where
the point is that it spreads requests. Real users, real tokens, the stub as the upstream.
"""

import json
from typing import Any

import httpx
import pytest
from redis.asyncio import Redis

from e2e.harness import obtain_grant
from e2e.stub.stack import (
    HA,
    HA_A,
    HA_B,
    INVALID_PARAMS,
    PERSONAS,
    SESSION_NOT_FOUND,
    UPSTREAM_UNAUTHORIZED,
    StubControl,
    StubSettings,
    echoed,
    error_code,
    flush_resolver_cache,
    ha_store_key,
    open_session,
    post,
    tool_call,
)

pytestmark = [pytest.mark.e2e_stub, pytest.mark.usefixtures("fresh_rate_limits")]

SESSION_HEADER = "Mcp-Session-Id"


async def test_a_session_opened_on_one_replica_works_on_the_other(
    settings: StubSettings,
    http: httpx.AsyncClient,
    stub: StubControl,
    evidence: dict[str, Any],
) -> None:
    token = (await obtain_grant(settings, HA, PERSONAS["bob"])).token
    session = await open_session(http, HA_A, token)

    response = await post(http, HA_B, token, tool_call("echo"), **{SESSION_HEADER: session})
    seen = echoed(response)
    assert seen["persona"] == "bob"
    assert seen["mode"] == "stateful"
    # Both requests reached the stub, on the one session the stub minted.
    sessions = {r["headers"].get("mcp-session-id") for r in await stub.requests()}
    assert sessions == {None, session}
    evidence.update(served_by="b", persona=seen["persona"])


async def test_another_user_cannot_use_the_session_on_either_replica(
    settings: StubSettings,
    http: httpx.AsyncClient,
    stub: StubControl,
    evidence: dict[str, Any],
) -> None:
    bob = (await obtain_grant(settings, HA, PERSONAS["bob"])).token
    carol = (await obtain_grant(settings, HA, PERSONAS["carol"])).token
    session = await open_session(http, HA_A, bob)
    # Positive control: carol is stateful on the pair too and gets a session of her own, so the
    # refusals below are about bob's session and not about carol's setup.
    own = await open_session(http, HA_B, carol)
    assert own != session
    seen = len(await stub.requests())

    for replica in (HA_A, HA_B):
        stolen = await post(http, replica, carol, tool_call("echo"), **{SESSION_HEADER: session})
        assert stolen.status_code == 404, f"{replica.name}: {stolen.text[:300]}"
        assert error_code(stolen) == SESSION_NOT_FOUND
    assert len(await stub.requests()) == seen, "a foreign session id must never be forwarded"

    # The refused attempts did not touch bob's binding.
    owner = await post(http, HA_B, bob, tool_call("echo"), **{SESSION_HEADER: session})
    assert echoed(owner)["persona"] == "bob"
    evidence.update(refused_on=["a", "b"])


async def test_ending_a_session_on_one_replica_ends_it_on_the_other(
    settings: StubSettings,
    http: httpx.AsyncClient,
    stub: StubControl,
    evidence: dict[str, Any],
) -> None:
    token = (await obtain_grant(settings, HA, PERSONAS["bob"])).token
    session = await open_session(http, HA_A, token)

    ended = await http.delete(
        HA_B.gateway_url,
        headers={"Authorization": f"Bearer {token}", SESSION_HEADER: session},
    )
    assert ended.status_code == 200, ended.text[:300]
    seen = len(await stub.requests())

    after = await post(http, HA_A, token, tool_call("echo"), **{SESSION_HEADER: session})
    assert after.status_code == 404
    assert error_code(after) == SESSION_NOT_FOUND
    assert len(await stub.requests()) == seen, "an ended session must not reach the stub"
    evidence.update(deleted_on="b", refused_on="a")


async def test_a_session_survives_the_load_balancer_spreading_its_requests(
    settings: StubSettings,
    http: httpx.AsyncClient,
    stub: StubControl,
    evidence: dict[str, Any],
) -> None:
    token = (await obtain_grant(settings, HA, PERSONAS["bob"])).token
    session = await open_session(http, HA, token)
    # Round robin: six requests on one session cannot all land on the replica that opened it.
    for number in range(6):
        response = await post(
            http, HA, token, tool_call("echo", request_id=10 + number), **{SESSION_HEADER: session}
        )
        assert echoed(response)["persona"] == "bob", f"request {number}"
    evidence.update(requests=6)


async def test_an_invalidation_on_one_replica_reaches_the_other(
    settings: StubSettings,
    http: httpx.AsyncClient,
    stub: StubControl,
    shared_redis: Redis,
    evidence: dict[str, Any],
) -> None:
    # Dave has no other test on the pair. His store entry is rewritten here and put back at the
    # end; the resolver cache is emptied first so that no earlier run's entry hides step 2.
    dave = PERSONAS["dave"]
    token = (await obtain_grant(settings, HA, dave)).token
    key = ha_store_key(dave.email)
    original = await shared_redis.get(key)
    assert original is not None, "the seed step did not write dave's entry"
    entry = json.loads(original)
    try:
        await flush_resolver_cache(shared_redis)

        # 1-2. The store holds a revoked key. B resolves it and caches it; the policy then
        # refuses the probe tool, so nothing is forwarded.
        revoked = {**entry, "api_key": settings.read_key("revoked")}
        await shared_redis.set(key, json.dumps(revoked))
        probe = await post(http, HA_B, token, tool_call("policy_denied_probe"))
        assert probe.status_code == 200
        assert error_code(probe) == INVALID_PARAMS
        assert await stub.requests() == []

        # 3-4. The store now holds a valid key, but A does not read the store: it finds B's cached
        # answer in the shared cache, which proves B put it there, and the stub refuses the key.
        await shared_redis.set(key, original)
        stale = await post(http, HA_A, token, tool_call("echo"))
        assert stale.status_code == 401, stale.text[:300]
        assert error_code(stale) == UPSTREAM_UNAUTHORIZED

        # 5. A's 401 dropped the shared entry, so B asks the store again and gets the valid key.
        fresh = await post(http, HA_B, token, tool_call("echo"))
        assert echoed(fresh)["persona"] == "dave"
        statuses = [record["status"] for record in await stub.requests()]
        assert statuses == [401, 200], "one refused call, then the recovered one"
    finally:
        await shared_redis.set(key, original)
        await flush_resolver_cache(shared_redis)
    evidence.update(statuses=statuses)
