"""What the gateway does between an authenticated user and the upstream: header policy, session
binding, streams, upstream failures and client disconnects. Real users, real tokens, the stub."""

import asyncio
import time
from typing import Any

import httpx
import pytest

from e2e.harness import Provider, initialize_payload, mcp_session, obtain_grant
from e2e.stub.stack import (
    KEYCLOAK,
    PERSONAS,
    STATIC_PROVIDERS,
    StubControl,
    StubSettings,
    bearer,
    echoed,
    error_code,
    events_of,
    json_of,
    open_session,
    post,
    read_body,
    tool_call,
)

pytestmark = pytest.mark.e2e_stub

static = pytest.mark.parametrize("provider", STATIC_PROVIDERS, ids=lambda p: p.name)
TEST_SECONDS = 60.0
DISCONNECT_SECONDS = 15.0
SESSION_NOT_FOUND = -32007
UPSTREAM_SESSION_NOT_FOUND = -32001
SSE = {"Accept": "text/event-stream"}


def session_headers(token: str, session: str, **extra: str) -> dict[str, str]:
    return bearer(token, **{"Mcp-Session-Id": session}, **extra)


# --- header policy and key injection ------------------------------------------------------


@static
async def test_upstream_sees_the_mapped_persona_and_none_of_the_forged_headers(
    settings: StubSettings,
    http: httpx.AsyncClient,
    stub: StubControl,
    provider: Provider,
    evidence: dict[str, Any],
) -> None:
    token = (await obtain_grant(settings, provider, PERSONAS["alice"])).token
    session = await open_session(http, provider, token)
    forged = {
        "X-Job-Id": "forged",
        "X-Organization-Id": "forged",
        "X-Mcp-Config-Id": "forged",
        "X-Oauth-Access-Token": "forged",
        "X-Forwarded-For": "203.0.113.9",
        "Cookie": "sid=forged",
        "Accept-Encoding": "gzip, br",
        "Mcp-Session-Id": session,
    }
    response = await post(http, provider, token, tool_call("echo"), **forged)
    seen = echoed(response)
    headers = seen["headers"]
    assert seen["persona"] == "alice", "the upstream must see alice's own key"
    assert headers["authorization"] == "persona:alice"
    for name in (
        "x-job-id",
        "x-organization-id",
        "x-mcp-config-id",
        "x-oauth-access-token",
        "x-forwarded-for",
        "cookie",
    ):
        assert name not in headers, f"{name} reached the upstream"
    # The gateway reads the stream as it is, so it never lets the upstream compress it.
    assert headers["accept-encoding"] == "identity"
    assert token not in response.text
    assert headers["mcp-session-id"] == session
    evidence.update(persona=seen["persona"], stripped=sorted(forged))


async def test_the_official_sdk_runs_a_whole_session_through_the_gateway(
    settings: StubSettings, evidence: dict[str, Any], stub: StubControl
) -> None:
    token = (await obtain_grant(settings, KEYCLOAK, PERSONAS["alice"])).token
    async with asyncio.timeout(TEST_SECONDS):
        async with mcp_session(KEYCLOAK.gateway_url, token) as (client, _):
            # The stub lists its tools on two pages: follow the cursor like a real client.
            first = await client.list_tools()
            assert first.next_cursor is not None
            second = await client.list_tools(cursor=first.next_cursor)
            tools = {tool.name for page in (first, second) for tool in page.tools}
            assert {"echo", "slow_sse", "status"} <= tools
            result = await client.call_tool("echo", {})
    assert result.is_error is False
    methods = [r["rpc_method"] for r in await stub.requests() if r["method"] == "POST"]
    assert methods[:2] == ["initialize", "notifications/initialized"]
    assert "tools/call" in methods
    assert {r["persona"] for r in await stub.requests()} == {"alice"}
    evidence.update(tools=sorted(tools), rpc_methods=methods)


# --- session binding ------------------------------------------------------------------------


@static
async def test_a_session_belongs_to_the_user_who_opened_it(
    settings: StubSettings,
    http: httpx.AsyncClient,
    stub: StubControl,
    provider: Provider,
    evidence: dict[str, Any],
) -> None:
    alice = (await obtain_grant(settings, provider, PERSONAS["alice"])).token
    bob = (await obtain_grant(settings, provider, PERSONAS["bob"])).token
    session = await open_session(http, provider, alice)

    own = await post(http, provider, alice, tool_call("echo"), **{"Mcp-Session-Id": session})
    assert echoed(own)["persona"] == "alice"

    seen = len(await stub.requests())
    stolen = await post(http, provider, bob, tool_call("echo"), **{"Mcp-Session-Id": session})
    assert stolen.status_code == 404
    assert error_code(stolen) == SESSION_NOT_FOUND
    assert len(await stub.requests()) == seen, "a foreign session must not be forwarded"

    again = await post(http, provider, alice, tool_call("echo"), **{"Mcp-Session-Id": session})
    assert echoed(again)["persona"] == "alice"
    evidence.update(refused_for_bob=stolen.status_code)


@static
async def test_delete_ends_the_session_for_good(
    settings: StubSettings,
    http: httpx.AsyncClient,
    stub: StubControl,
    provider: Provider,
) -> None:
    token = (await obtain_grant(settings, provider, PERSONAS["alice"])).token
    session = await open_session(http, provider, token)
    headers = session_headers(token, session)
    deleted = await http.delete(provider.gateway_url, headers=headers)
    assert deleted.status_code == 200

    seen = len(await stub.requests())
    reuse = await post(http, provider, token, tool_call("echo"), **{"Mcp-Session-Id": session})
    assert reuse.status_code == 404
    assert error_code(reuse) == SESSION_NOT_FOUND
    assert len(await stub.requests()) == seen


@static
async def test_a_session_the_upstream_expired_is_a_404_passed_through_then_dropped(
    settings: StubSettings,
    http: httpx.AsyncClient,
    stub: StubControl,
    provider: Provider,
    evidence: dict[str, Any],
) -> None:
    token = (await obtain_grant(settings, provider, PERSONAS["alice"])).token
    session = await open_session(http, provider, token)
    await stub.expire(session)

    first = await post(http, provider, token, tool_call("echo"), **{"Mcp-Session-Id": session})
    assert first.status_code == 404
    assert error_code(first) == UPSTREAM_SESSION_NOT_FOUND, "the upstream's own 404 body"
    seen = len(await stub.requests())
    second = await post(http, provider, token, tool_call("echo"), **{"Mcp-Session-Id": session})
    assert second.status_code == 404
    assert error_code(second) == SESSION_NOT_FOUND, "the gateway's own 404 body"
    assert len(await stub.requests()) == seen
    evidence.update(first_code=error_code(first), second_code=error_code(second))


# --- GET stream, stateless upstream ---------------------------------------------------------


async def read_event_ids(response: httpx.Response, count: int) -> list[str]:
    ids: list[str] = []
    async for line in response.aiter_lines():
        if line.startswith("id: "):
            ids.append(line[4:])
        if len(ids) == count:
            break
    return ids


async def test_get_stream_resumes_after_last_event_id(
    settings: StubSettings, http: httpx.AsyncClient, stub: StubControl, evidence: dict[str, Any]
) -> None:
    token = (await obtain_grant(settings, KEYCLOAK, PERSONAS["alice"])).token
    session = await open_session(http, KEYCLOAK, token)
    base = session_headers(token, session, **SSE)
    async with asyncio.timeout(TEST_SECONDS):
        async with http.stream("GET", KEYCLOAK.gateway_url, headers=base) as response:
            assert response.headers["content-type"].startswith("text/event-stream")
            ids = await read_event_ids(response, 3)
        assert ids == [f"{session}-{n}" for n in (1, 2, 3)]
        async with http.stream(
            "GET", KEYCLOAK.gateway_url, headers={**base, "Last-Event-ID": ids[0]}
        ) as response:
            replayed = await read_event_ids(response, 2)
    assert replayed == ids[1:]
    gets = [r for r in await stub.requests() if r["method"] == "GET"]
    assert gets[-1]["headers"]["last-event-id"] == ids[0]
    assert gets[-1]["headers"]["mcp-session-id"] == session
    evidence.update(first=ids, replayed=replayed)


@static
async def test_a_stateless_upstream_refuses_get_and_delete_with_405_untouched(
    settings: StubSettings, http: httpx.AsyncClient, provider: Provider
) -> None:
    token = (await obtain_grant(settings, provider, PERSONAS["carol"])).token
    got = await http.get(provider.gateway_url, headers=bearer(token, **SSE))
    deleted = await http.delete(provider.gateway_url, headers=bearer(token))
    assert (got.status_code, got.text) == (405, "Session termination not supported")
    assert deleted.status_code == 405


# --- upstream refusals and errors ------------------------------------------------------------


@pytest.mark.parametrize(
    ("who", "code", "upstream_status"),
    [("revoked", -32001, 401), ("unpaid", -32002, 402)],
)
async def test_a_key_the_upstream_refuses_is_a_jsonrpc_error_never_a_challenge(
    settings: StubSettings,
    http: httpx.AsyncClient,
    stub: StubControl,
    who: str,
    code: int,
    upstream_status: int,
    evidence: dict[str, Any],
) -> None:
    token = (await obtain_grant(settings, KEYCLOAK, PERSONAS[who])).token
    response = await post(http, KEYCLOAK, token, initialize_payload())
    assert response.status_code == 200
    assert error_code(response) == code
    assert "www-authenticate" not in response.headers
    assert response.json()["error"]["data"] == {"upstream_status": upstream_status}
    reached = [r for r in await stub.requests() if r["persona"] == who]
    assert [r["status"] for r in reached] == [upstream_status], "the stub refused it itself"
    evidence.update(code=code, upstream_status=upstream_status)


async def test_upstream_error_statuses_pass_through(
    settings: StubSettings, http: httpx.AsyncClient, evidence: dict[str, Any]
) -> None:
    token = (await obtain_grant(settings, KEYCLOAK, PERSONAS["carol"])).token
    seen: dict[int, int] = {}
    for status in (404, 500, 502, 503):
        response = await post(http, KEYCLOAK, token, tool_call("status", status=status))
        assert response.status_code == status
        assert response.json() == {"error": "stub status"}
        seen[status] = response.status_code
    evidence.update(statuses=seen)


# --- streams ----------------------------------------------------------------------------------


async def test_a_slow_sse_response_arrives_incrementally(
    settings: StubSettings, http: httpx.AsyncClient, evidence: dict[str, Any]
) -> None:
    token = (await obtain_grant(settings, KEYCLOAK, PERSONAS["carol"])).token
    arrivals: list[float] = []
    buffer = b""
    started = time.monotonic()
    async with asyncio.timeout(TEST_SECONDS):
        async with http.stream(
            "POST",
            KEYCLOAK.gateway_url,
            json=tool_call("slow_sse", count=4, delay_seconds=0.5),
            headers=bearer(token, Accept="application/json, text/event-stream"),
        ) as response:
            assert response.headers["content-type"].startswith("text/event-stream")
            async for chunk in response.aiter_raw():
                buffer += chunk
                while b"\n\n" in buffer:
                    _, buffer = buffer.split(b"\n\n", 1)
                    arrivals.append(time.monotonic() - started)
    assert len(arrivals) == 5, "four progress events and the reply"
    # The stub spaces events by 0.5 s: the first one is here long before the last exists.
    assert arrivals[-1] - arrivals[0] >= 1.2, arrivals
    assert arrivals[0] < arrivals[-1] - 1.0
    evidence.update(arrivals=[round(a, 2) for a in arrivals])


async def test_a_stream_that_fails_before_the_reply_ends_with_a_jsonrpc_error_event(
    settings: StubSettings, http: httpx.AsyncClient, evidence: dict[str, Any]
) -> None:
    token = (await obtain_grant(settings, KEYCLOAK, PERSONAS["carol"])).token
    async with asyncio.timeout(TEST_SECONDS):
        async with http.stream(
            "POST",
            KEYCLOAK.gateway_url,
            json=tool_call("fail_midstream_sse", before=2),
            headers=bearer(token, Accept="application/json, text/event-stream"),
        ) as response:
            body, error = await read_body(response)
    assert error is None, "the stream must end cleanly after the error event"
    *progress, final = events_of(body)
    assert len(progress) == 2
    assert json_of(final)["id"] == 7
    assert json_of(final)["error"]["code"] == -32006
    evidence.update(final=json_of(final)["error"])


async def test_a_stream_that_fails_after_the_reply_is_not_a_clean_end(
    settings: StubSettings, http: httpx.AsyncClient
) -> None:
    token = (await obtain_grant(settings, KEYCLOAK, PERSONAS["carol"])).token
    async with asyncio.timeout(TEST_SECONDS):
        async with http.stream(
            "POST",
            KEYCLOAK.gateway_url,
            json=tool_call("fail_midstream_sse", before=1, reply_first=True),
            headers=bearer(token, Accept="application/json, text/event-stream"),
        ) as response:
            body, error = await read_body(response)
    assert isinstance(error, httpx.RemoteProtocolError)
    assert json_of(events_of(body)[-1])["result"]["isError"] is False


async def test_a_json_body_cut_short_is_an_incomplete_response(
    settings: StubSettings, http: httpx.AsyncClient
) -> None:
    token = (await obtain_grant(settings, KEYCLOAK, PERSONAS["carol"])).token
    async with asyncio.timeout(TEST_SECONDS):
        async with http.stream(
            "POST",
            KEYCLOAK.gateway_url,
            json=tool_call("fail_midstream_json"),
            headers=bearer(token, Accept="application/json, text/event-stream"),
        ) as response:
            declared = int(response.headers["content-length"])
            body, error = await read_body(response)
    assert isinstance(error, httpx.RemoteProtocolError)
    assert len(body) < declared


async def test_a_client_going_away_during_a_hanging_call_aborts_the_upstream_request(
    settings: StubSettings,
    http: httpx.AsyncClient,
    stub: StubControl,
    evidence: dict[str, Any],
) -> None:
    token = (await obtain_grant(settings, KEYCLOAK, PERSONAS["carol"])).token
    async with asyncio.timeout(TEST_SECONDS):
        call = asyncio.create_task(post(http, KEYCLOAK, token, tool_call("hang")))
        await stub.wait_for(lambda r: r["tool"] == "hang", timeout=DISCONNECT_SECONDS)
        # Positive control: while the client is still here, the stub sees no disconnect.
        assert not [r for r in await stub.requests() if r["tool"] == "hang" and r["disconnected"]]
        started = time.monotonic()
        call.cancel()
        with pytest.raises(asyncio.CancelledError):
            await call
        record = await stub.wait_for(
            lambda r: r["tool"] == "hang" and r["disconnected"], timeout=DISCONNECT_SECONDS
        )
    assert record["persona"] == "carol"
    evidence.update(disconnect_seen_after_s=round(time.monotonic() - started, 2))
