"""The credential resolvers through gateway-chain (Redis, PostgreSQL, webhook behind a cache),
and the gateway's answer when the webhook cannot be reached. Real users, real tokens."""

import asyncio
from typing import Any

import httpx
import pytest

from e2e.harness import CHAIN, obtain_grant
from e2e.stub.stack import (
    PERSONAS,
    UNREACHABLE,
    StubControl,
    StubSettings,
    echoed,
    error_code,
    open_session,
    post,
    tool_call,
    webhook_resolve_count,
)

pytestmark = pytest.mark.e2e_stub

NO_MAPPING = -32010
RESOLVER_UNAVAILABLE = -32009
# The chain gateway keeps a positive answer 4 s (compose.yaml); wait a little longer to be sure
# nothing cached by an earlier test is left.
CACHE_TTL_SECONDS = 4.0
CACHE_EXPIRY_MARGIN_SECONDS = 1.0


@pytest.mark.parametrize(
    ("who", "store", "mode"),
    [
        ("alice", "redis", "stateless"),
        ("bob", "postgres", "stateful"),
        ("carol", "webhook", "stateless"),
    ],
)
async def test_each_resolver_serves_its_persona(
    settings: StubSettings,
    http: httpx.AsyncClient,
    stub: StubControl,
    who: str,
    store: str,
    mode: str,
    evidence: dict[str, Any],
) -> None:
    # Each persona is known to exactly one backend (see scripts/state_layout.py), so being
    # served as the right persona proves that backend answered; the entry also names the
    # upstream, so the persona reaches the stateful or the stateless stub endpoint accordingly.
    token = (await obtain_grant(settings, CHAIN, PERSONAS[who])).token
    headers: dict[str, str] = {}
    if mode == "stateful":
        headers["Mcp-Session-Id"] = await open_session(http, CHAIN, token)
    response = await post(http, CHAIN, token, tool_call("echo"), **headers)
    seen = echoed(response)
    assert seen["persona"] == who
    assert seen["mode"] == mode
    evidence.update(store=store, persona=seen["persona"], mode=seen["mode"])


async def test_a_user_no_backend_knows_is_denied(
    settings: StubSettings,
    http: httpx.AsyncClient,
    stub: StubControl,
    evidence: dict[str, Any],
) -> None:
    token = (await obtain_grant(settings, CHAIN, PERSONAS["nomap"])).token
    response = await post(http, CHAIN, token, tool_call("echo"))
    assert response.status_code == 403
    assert error_code(response) == NO_MAPPING
    assert "www-authenticate" not in response.headers
    assert await stub.requests() == []
    evidence.update(code=NO_MAPPING)


async def test_a_cached_answer_does_not_call_the_webhook_again(
    settings: StubSettings,
    http: httpx.AsyncClient,
    stub: StubControl,
    evidence: dict[str, Any],
) -> None:
    token = (await obtain_grant(settings, CHAIN, PERSONAS["carol"])).token
    # Let any answer cached by an earlier test expire, so the next call must ask the webhook.
    await asyncio.sleep(CACHE_TTL_SECONDS + CACHE_EXPIRY_MARGIN_SECONDS)
    before = await webhook_resolve_count(http)

    first = await post(http, CHAIN, token, tool_call("echo"))
    assert echoed(first)["persona"] == "carol"
    after_first = await webhook_resolve_count(http)
    assert after_first == before + 1, "positive control: a cache miss reaches the webhook once"

    second = await post(http, CHAIN, token, tool_call("echo"))
    assert echoed(second)["persona"] == "carol"
    assert await webhook_resolve_count(http) == after_first, "a cache hit must not call it"
    evidence.update(webhook_calls=[before, after_first])


async def test_an_unreachable_webhook_gives_503_with_retry_after(
    settings: StubSettings,
    http: httpx.AsyncClient,
    stub: StubControl,
    evidence: dict[str, Any],
) -> None:
    carol = PERSONAS["carol"]
    # Positive control: the same user is served where the webhook is reachable.
    served = await post(
        http, CHAIN, (await obtain_grant(settings, CHAIN, carol)).token, tool_call("echo")
    )
    assert echoed(served)["persona"] == "carol"

    token = (await obtain_grant(settings, UNREACHABLE, carol)).token
    seen = len(await stub.requests())
    response = await post(http, UNREACHABLE, token, tool_call("echo"))
    assert response.status_code == 503
    assert error_code(response) == RESOLVER_UNAVAILABLE
    assert int(response.headers["retry-after"]) > 0
    assert len(await stub.requests()) == seen, "no credential, no upstream call"
    evidence.update(retry_after=response.headers["retry-after"])
