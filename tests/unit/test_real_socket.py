"""Protocol behaviour over real TCP: uvicorn gateway, uvicorn stub, real clients.

These cover what an in-process ASGI transport cannot: connection aborts, disconnect
propagation and incremental streaming. Every wait is bounded.
"""

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import anyio
import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from .conftest import FakeIdp
from .socket_stack import ACCEPT, INITIALIZE, Stack, rpc_call, running_stack

TEST_SECONDS = 45.0
DISCONNECT_SECONDS = 10.0


@pytest.fixture
async def stack(tmp_path: Path, idp: FakeIdp, rsa_key: rsa.RSAPrivateKey) -> AsyncIterator[Stack]:
    async with running_stack(tmp_path, idp, rsa_key, config_edit=lambda _data: None) as running:
        yield running


def events_of(raw: bytes) -> list[bytes]:
    return [event for event in raw.split(b"\n\n") if event]


def json_of(event: bytes) -> dict[str, Any]:
    return json.loads(event.split(b"data: ", 1)[1])


async def read_body(response: httpx.Response) -> tuple[bytes, Exception | None]:
    """Everything the client received and the error that ended the body, if any."""
    received = b""
    try:
        async for chunk in response.aiter_raw():
            received += chunk
    except httpx.HTTPError as exc:
        return received, exc
    return received, None


async def open_session(stack: Stack, who: str) -> str:
    response = await stack.client.post(
        f"{stack.gateway}/mcp", json=INITIALIZE, headers=stack.headers(who, **ACCEPT)
    )
    assert response.status_code == 200
    return response.headers["mcp-session-id"]


# --- mid-stream failure (P2) ----------------------------------------------------------


async def test_sse_failure_before_the_reply_gives_the_client_a_jsonrpc_error_event(
    stack: Stack,
) -> None:
    with anyio.fail_after(TEST_SECONDS):
        async with stack.client.stream(
            "POST",
            f"{stack.gateway}/mcp",
            json=rpc_call("fail_midstream_sse", before=2),
            headers=stack.headers("carol", **ACCEPT),
        ) as response:
            assert response.headers["x-accel-buffering"] == "no"
            body, error = await read_body(response)
    assert error is None, "the stream must end cleanly after the error event"
    *progress, final = events_of(body)
    assert len(progress) == 2
    assert b'"progress":' not in final
    assert final.startswith(b"event: message\n")
    assert json_of(final) == {
        "jsonrpc": "2.0",
        "id": 7,
        "error": {
            "code": -32006,
            "message": "The service stream failed before the response was complete.",
        },
    }


async def test_sse_failure_after_the_reply_is_not_a_clean_end(stack: Stack) -> None:
    with anyio.fail_after(TEST_SECONDS):
        async with stack.client.stream(
            "POST",
            f"{stack.gateway}/mcp",
            json=rpc_call("fail_midstream_sse", before=1, reply_first=True),
            headers=stack.headers("carol", **ACCEPT),
        ) as response:
            body, error = await read_body(response)
    assert isinstance(error, httpx.RemoteProtocolError)
    events = events_of(body)
    assert json_of(events[-1])["result"] == {
        "content": [{"type": "text", "text": '{"events": 1}'}],
        "isError": False,
    }
    assert b'progress":' not in events[-1]


async def test_json_body_cut_short_is_not_a_clean_end(stack: Stack) -> None:
    with anyio.fail_after(TEST_SECONDS):
        async with stack.client.stream(
            "POST",
            f"{stack.gateway}/mcp",
            json=rpc_call("fail_midstream_json"),
            headers=stack.headers("carol", **ACCEPT),
        ) as response:
            assert response.headers["content-type"] == "application/json"
            declared = int(response.headers["content-length"])
            body, error = await read_body(response)
    assert isinstance(error, httpx.RemoteProtocolError)
    assert len(body) < declared


async def test_complete_stream_is_relayed_whole(stack: Stack) -> None:
    with anyio.fail_after(TEST_SECONDS):
        response = await stack.client.post(
            f"{stack.gateway}/mcp",
            json=rpc_call("slow_sse", count=3, delay_seconds=0.01),
            headers=stack.headers("carol", **ACCEPT),
        )
    events = events_of(response.content)
    assert len(events) == 4
    assert json_of(events[-1])["id"] == 7
    # Positive control for the disconnect tests: a client that stays is never recorded as gone.
    record = await stack.wait_for_stub(lambda r: r["tool"] == "slow_sse")
    assert record["disconnected"] is False


# --- client disconnect ----------------------------------------------------------------


async def test_closing_an_idle_sse_response_aborts_the_upstream_request(stack: Stack) -> None:
    with anyio.fail_after(TEST_SECONDS):
        async with stack.client.stream(
            "POST",
            f"{stack.gateway}/mcp",
            json=rpc_call("slow_sse", count=1, delay_seconds=0, hold=True),
            headers=stack.headers("carol", **ACCEPT),
        ) as response:
            async for _first in response.aiter_raw():
                break  # one event arrived; the stream is now idle
        record = await stack.wait_for_stub(
            lambda r: r["tool"] == "slow_sse" and r["disconnected"], timeout=DISCONNECT_SECONDS
        )
    assert record["persona"] == "carol"


async def test_client_going_away_during_a_hanging_call_aborts_the_upstream_request(
    stack: Stack,
) -> None:
    with anyio.fail_after(TEST_SECONDS):
        call = asyncio.create_task(
            stack.client.post(
                f"{stack.gateway}/mcp",
                json=rpc_call("hang"),
                headers=stack.headers("carol", **ACCEPT),
            )
        )
        await stack.wait_for_stub(lambda r: r["tool"] == "hang")
        call.cancel()
        with pytest.raises(asyncio.CancelledError):
            await call
        record = await stack.wait_for_stub(
            lambda r: r["tool"] == "hang" and r["disconnected"], timeout=DISCONNECT_SECONDS
        )
    assert record["persona"] == "carol"


async def test_closing_a_get_stream_aborts_the_upstream_stream(stack: Stack) -> None:
    with anyio.fail_after(TEST_SECONDS):
        session = await open_session(stack, "alice")
        async with stack.client.stream(
            "GET",
            f"{stack.gateway}/mcp",
            headers=stack.headers(
                "alice", **{"Accept": "text/event-stream", "Mcp-Session-Id": session}
            ),
        ) as response:
            async for _first in response.aiter_raw():
                break
        await stack.wait_for_stub(
            lambda r: r["method"] == "GET" and r["disconnected"], timeout=DISCONNECT_SECONDS
        )


# --- GET stream and Last-Event-ID (2025-11-25 era) ------------------------------------


async def read_event_ids(response: httpx.Response, count: int) -> list[str]:
    ids: list[str] = []
    async for line in response.aiter_lines():
        if line.startswith("id: "):
            ids.append(line[4:])
        if len(ids) == count:
            break
    return ids


async def test_get_stream_resumes_after_last_event_id(stack: Stack) -> None:
    with anyio.fail_after(TEST_SECONDS):
        session = await open_session(stack, "alice")
        base = stack.headers("alice", **{"Accept": "text/event-stream", "Mcp-Session-Id": session})
        async with stack.client.stream("GET", f"{stack.gateway}/mcp", headers=base) as response:
            assert response.headers["content-type"].startswith("text/event-stream")
            ids = await read_event_ids(response, 3)
        assert ids == [f"{session}-{n}" for n in (1, 2, 3)]
        async with stack.client.stream(
            "GET", f"{stack.gateway}/mcp", headers={**base, "Last-Event-ID": ids[0]}
        ) as response:
            replayed = await read_event_ids(response, 2)
    assert replayed == ids[1:]
    sent = [r for r in await stack.stub_requests() if r["method"] == "GET"]
    assert sent[-1]["headers"]["last-event-id"] == ids[0]
    assert sent[-1]["headers"]["mcp-session-id"] == session


async def test_stateless_upstream_refuses_get_and_delete_with_405_untouched(stack: Stack) -> None:
    with anyio.fail_after(TEST_SECONDS):
        got = await stack.client.get(
            f"{stack.gateway}/mcp", headers=stack.headers("carol", Accept="text/event-stream")
        )
        deleted = await stack.client.delete(f"{stack.gateway}/mcp", headers=stack.headers("carol"))
    assert (got.status_code, got.text) == (405, "Session termination not supported")
    assert deleted.status_code == 405


# --- session binding (P3) -------------------------------------------------------------


async def test_session_of_alice_is_refused_to_bob_and_still_works_for_alice(stack: Stack) -> None:
    with anyio.fail_after(TEST_SECONDS):
        session = await open_session(stack, "alice")
        await stack.client.post(
            f"{stack.gateway}/mcp",
            json=rpc_call("echo"),
            headers=stack.headers("alice", **{"Mcp-Session-Id": session}),
        )
        seen = len(await stack.stub_requests())
        stolen = await stack.client.post(
            f"{stack.gateway}/mcp",
            json=rpc_call("echo"),
            headers=stack.headers("bob", **{"Mcp-Session-Id": session}),
        )
        assert stolen.status_code == 404
        assert stolen.json()["error"]["code"] == -32007
        assert len(await stack.stub_requests()) == seen, "a refused request must not be forwarded"
        again = await stack.client.post(
            f"{stack.gateway}/mcp",
            json=rpc_call("echo"),
            headers=stack.headers("alice", **{"Mcp-Session-Id": session}),
        )
    assert again.status_code == 200
    echoed = json.loads(again.json()["result"]["content"][0]["text"])
    assert echoed["persona"] == "alice"


async def test_delete_ends_the_session_for_good(stack: Stack) -> None:
    with anyio.fail_after(TEST_SECONDS):
        session = await open_session(stack, "alice")
        headers = stack.headers("alice", **{"Mcp-Session-Id": session})
        assert (
            await stack.client.delete(f"{stack.gateway}/mcp", headers=headers)
        ).status_code == 200
        seen = len(await stack.stub_requests())
        reuse = await stack.client.post(
            f"{stack.gateway}/mcp", json=rpc_call("echo"), headers=headers
        )
    assert reuse.status_code == 404
    assert reuse.json()["error"]["code"] == -32007
    assert len(await stack.stub_requests()) == seen


async def test_upstream_expired_session_is_a_404_passed_through_then_dropped(stack: Stack) -> None:
    with anyio.fail_after(TEST_SECONDS):
        session = await open_session(stack, "alice")
        headers = stack.headers("alice", **{"Mcp-Session-Id": session})
        control = await stack.client.post(f"{stack.stub}/_stub/sessions/{session}/expire")
        assert control.status_code == 204
        first = await stack.client.post(
            f"{stack.gateway}/mcp", json=rpc_call("echo"), headers=headers
        )
        seen = len(await stack.stub_requests())
        second = await stack.client.post(
            f"{stack.gateway}/mcp", json=rpc_call("echo"), headers=headers
        )
    # The upstream's own 404 body, not the gateway's.
    assert first.status_code == 404
    assert first.json()["error"] == {"code": -32001, "message": "Session not found"}
    assert second.status_code == 404
    assert second.json()["error"]["code"] == -32007
    assert len(await stack.stub_requests()) == seen


# --- header policy against a real upstream ---------------------------------------------


async def test_upstream_sees_the_persona_and_none_of_the_forged_headers(stack: Stack) -> None:
    forged = {
        "X-Job-Id": "forged",
        "X-Organization-Id": "forged",
        "X-Oauth-Access-Token": "forged",
        "Cookie": "sid=forged",
        "Mcp-Method": "tools/call",
        "Mcp-Name": "echo",
        "MCP-Protocol-Version": "2026-07-28",
    }
    with anyio.fail_after(TEST_SECONDS):
        response = await stack.client.post(
            f"{stack.gateway}/mcp",
            json=rpc_call("echo"),
            headers=stack.headers("carol", **ACCEPT, **forged),
        )
    echoed = json.loads(response.json()["result"]["content"][0]["text"])
    headers = echoed["headers"]
    assert echoed["mode"] == "stateless"
    assert headers["authorization"] == "persona:carol"
    assert headers["mcp-method"] == "tools/call"
    assert headers["mcp-name"] == "echo"
    assert headers["mcp-protocol-version"] == "2026-07-28"
    for name in ("x-job-id", "x-organization-id", "x-oauth-access-token", "cookie"):
        assert name not in headers
    assert stack.tokens["carol"] not in response.text


@pytest.mark.parametrize("status", [404, 500, 502, 503])
async def test_upstream_error_statuses_pass_through(stack: Stack, status: int) -> None:
    with anyio.fail_after(TEST_SECONDS):
        response = await stack.client.post(
            f"{stack.gateway}/mcp",
            json=rpc_call("status", status=status),
            headers=stack.headers("carol", **ACCEPT),
        )
    assert response.status_code == status
    assert response.json() == {"error": "stub status"}


async def test_revoked_and_unpaid_keys_become_jsonrpc_errors_never_a_challenge(
    stack: Stack,
) -> None:
    with anyio.fail_after(TEST_SECONDS):
        revoked = await stack.client.post(
            f"{stack.gateway}/mcp", json=rpc_call("echo"), headers=stack.headers("revoked")
        )
        unpaid = await stack.client.post(
            f"{stack.gateway}/mcp", json=rpc_call("echo"), headers=stack.headers("broke")
        )
    assert (revoked.status_code, revoked.json()["error"]["code"]) == (200, -32001)
    assert (unpaid.status_code, unpaid.json()["error"]["code"]) == (200, -32002)
    assert "www-authenticate" not in revoked.headers
