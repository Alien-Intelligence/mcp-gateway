"""The tool policy through the gateways that carry one: the replica pair behind its load balancer
(default policy denying one tool) and the static Keycloak gateway (a rule on the ``client`` label).
Real users, real tokens, the stub as the upstream."""

from typing import Any

import httpx
import pytest

from e2e.harness import obtain_grant
from e2e.stub.stack import (
    DENIED_PROBE,
    HA,
    INVALID_PARAMS,
    INVALID_REQUEST,
    KEYCLOAK,
    PERSONAS,
    STUB_TOOLS,
    StubControl,
    StubSettings,
    error_code,
    list_result,
    open_session,
    post,
    tool_call,
    tools_list,
)

pytestmark = [pytest.mark.e2e_stub, pytest.mark.usefixtures("fresh_rate_limits")]

FIRST_PAGE = ("echo", "slow_sse")
SECOND_PAGE = ("fail_midstream_sse", "fail_midstream_json", "hang", "status")
STUB_TTL_MS = 60_000


@pytest.mark.parametrize("sse", [False, True], ids=["json", "sse"])
async def test_a_tool_list_loses_the_denied_tool_on_every_page(
    settings: StubSettings,
    http: httpx.AsyncClient,
    stub: StubControl,
    sse: bool,
    evidence: dict[str, Any],
) -> None:
    token = (await obtain_grant(settings, HA, PERSONAS["alice"])).token

    first = list_result(await post(http, HA, token, tools_list(sse=sse)))
    assert [tool["name"] for tool in first["tools"]] == list(FIRST_PAGE)
    assert first["nextCursor"], "the stub's first page names a second one; the filter keeps it"
    second = list_result(
        await post(http, HA, token, tools_list(cursor=first["nextCursor"], sse=sse))
    )
    assert [tool["name"] for tool in second["tools"]] == list(SECOND_PAGE)
    assert "nextCursor" not in second

    for page in (first, second):
        # The stub says "public"; a list filtered for one caller must not be shared.
        assert page["cacheScope"] == "private"
        assert page["ttlMs"] == STUB_TTL_MS
    # The probe is the only stub tool missing: the stub does list it (see STUB_TOOLS).
    assert set(STUB_TOOLS) - {DENIED_PROBE} == {
        t["name"] for t in (*first["tools"], *second["tools"])
    }
    evidence.update(sse=sse, pages=2)


async def test_calling_the_denied_tool_looks_like_calling_a_missing_one(
    settings: StubSettings,
    http: httpx.AsyncClient,
    stub: StubControl,
    evidence: dict[str, Any],
) -> None:
    token = (await obtain_grant(settings, HA, PERSONAS["alice"])).token
    allowed = await post(http, HA, token, tool_call("echo"))
    assert allowed.status_code == 200 and "result" in allowed.json(), "positive control"

    denied = await post(http, HA, token, tool_call(DENIED_PROBE))
    assert denied.status_code == 200
    assert error_code(denied) == INVALID_PARAMS
    assert denied.json()["error"]["message"] == f"Unknown tool: {DENIED_PROBE}"
    assert [r["tool"] for r in await stub.requests()] == ["echo"], "the probe was never forwarded"
    evidence.update(code=INVALID_PARAMS)


async def test_a_batch_carrying_a_tool_call_is_refused(
    settings: StubSettings,
    http: httpx.AsyncClient,
    stub: StubControl,
    evidence: dict[str, Any],
) -> None:
    token = (await obtain_grant(settings, HA, PERSONAS["alice"])).token
    # A batch would let a denied tool call past a filter that reads single messages.
    response = await http.post(
        HA.gateway_url,
        json=[tool_call(DENIED_PROBE, request_id=1), tool_call("echo", request_id=2)],
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json, text/event-stream",
        },
    )
    assert response.status_code == 400
    assert error_code(response) == INVALID_REQUEST
    assert await stub.requests() == [], "nothing of a refused batch is forwarded"
    evidence.update(code=INVALID_REQUEST)


async def test_a_label_rule_narrows_one_client_and_not_the_others(
    settings: StubSettings,
    http: httpx.AsyncClient,
    stub: StubControl,
    evidence: dict[str, Any],
) -> None:
    # The static mapping labels every persona client=<its organization>. Bob's client has a rule
    # that allows `echo` among the stub's tools; alice's falls to the default (all but the probe).
    seen: dict[str, set[str]] = {}
    for who in ("bob", "alice"):
        token = (await obtain_grant(settings, KEYCLOAK, PERSONAS[who])).token
        session = await open_session(http, KEYCLOAK, token)
        names: set[str] = set()
        cursor: str | None = None
        for _ in range(2):
            page = list_result(
                await post(
                    http,
                    KEYCLOAK,
                    token,
                    tools_list(cursor=cursor),
                    **{"Mcp-Session-Id": session},
                )
            )
            names |= {tool["name"] for tool in page["tools"]}
            cursor = page.get("nextCursor")
        seen[who] = names
    assert seen["bob"] == {"echo"}
    assert seen["alice"] == set(STUB_TOOLS) - {DENIED_PROBE}
    evidence.update(bob=sorted(seen["bob"]), alice=sorted(seen["alice"]))
