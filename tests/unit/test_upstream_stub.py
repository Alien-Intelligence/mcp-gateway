"""Contract of the fault-injection stub, in process (the real-socket suite drives it over TCP)."""

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from starlette.applications import Starlette

from .stub_loader import load_stub_module, write_keys

INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}


def call(tool: str, request_id: int = 7, **arguments: Any) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "tools/call",
        "params": {"name": tool, "arguments": arguments},
    }


def auth(who: str = "alice") -> dict[str, str]:
    return {"Authorization": f"Bearer fake-alien-key-{who}_stub_key"}


@pytest.fixture
def stub_app(tmp_path: Path) -> Starlette:
    return load_stub_module().create_app(write_keys(tmp_path))


@pytest.fixture
async def stub(stub_app: Starlette) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=stub_app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://stub") as client:
        yield client


async def open_session(stub: httpx.AsyncClient, who: str = "alice") -> str:
    response = await stub.post("/stateful/mcp", json=INIT, headers=auth(who))
    return response.headers["mcp-session-id"]


async def test_stateful_initialize_mints_a_session_bound_to_the_key(
    stub: httpx.AsyncClient,
) -> None:
    session = await open_session(stub)
    ok = await stub.post(
        "/stateful/mcp", json=call("echo"), headers={**auth(), "Mcp-Session-Id": session}
    )
    assert ok.status_code == 200
    foreign = await stub.post(
        "/stateful/mcp", json=call("echo"), headers={**auth("bob"), "Mcp-Session-Id": session}
    )
    assert foreign.status_code == 404
    missing = await stub.post("/stateful/mcp", json=call("echo"), headers=auth())
    assert missing.status_code == 400


async def test_session_can_be_expired_by_the_control_endpoint(stub: httpx.AsyncClient) -> None:
    session = await open_session(stub)
    assert (await stub.post(f"/_stub/sessions/{session}/expire")).status_code == 204
    gone = await stub.post(
        "/stateful/mcp", json=call("echo"), headers={**auth(), "Mcp-Session-Id": session}
    )
    assert gone.status_code == 404
    assert (await stub.post("/_stub/sessions/unknown/expire")).status_code == 404


async def test_delete_ends_the_session(stub: httpx.AsyncClient) -> None:
    session = await open_session(stub)
    headers = {**auth(), "Mcp-Session-Id": session}
    assert (await stub.delete("/stateful/mcp", headers=headers)).status_code == 200
    assert (await stub.delete("/stateful/mcp", headers=headers)).status_code == 404


async def test_stateless_mode_has_no_sessions_and_refuses_get_and_delete(
    stub: httpx.AsyncClient,
) -> None:
    response = await stub.post("/stateless/mcp", json=INIT, headers=auth())
    assert "mcp-session-id" not in response.headers
    assert (await stub.get("/stateless/mcp", headers=auth())).status_code == 405
    assert (await stub.delete("/stateless/mcp", headers=auth())).status_code == 405
    ok = await stub.post(
        "/stateless/mcp",
        json=call("echo"),
        headers={**auth(), "Mcp-Method": "tools/call", "Mcp-Name": "echo"},
    )
    assert ok.status_code == 200


async def test_unknown_mode_is_404(stub: httpx.AsyncClient) -> None:
    assert (await stub.post("/other/mcp", json=INIT, headers=auth())).status_code == 404


async def test_key_flags_drive_401_and_402(stub: httpx.AsyncClient) -> None:
    revoked = await stub.post("/stateless/mcp", json=INIT, headers=auth("revoked"))
    assert revoked.status_code == 401
    assert revoked.headers["www-authenticate"].startswith("Bearer ")
    unknown = await stub.post("/stateless/mcp", json=INIT, headers={"Authorization": "Bearer nope"})
    assert unknown.status_code == 401
    assert (await stub.post("/stateless/mcp", json=INIT, headers=auth("broke"))).status_code == 402


async def test_echo_returns_headers_with_the_persona_instead_of_the_key(
    stub: httpx.AsyncClient,
) -> None:
    response = await stub.post(
        "/stateless/mcp",
        json=call("echo"),
        headers={**auth("carol"), "X-Oauth-Access-Token": "fake-alien-key-carol_stub_key"},
    )
    assert "fake-alien-key-carol_stub_key" not in response.text
    echoed = json.loads(response.json()["result"]["content"][0]["text"])
    assert echoed["persona"] == "carol"
    assert echoed["headers"]["authorization"] == "persona:carol"
    assert echoed["headers"]["x-oauth-access-token"] == "<key:carol>"


async def test_status_tool_answers_the_requested_status(stub: httpx.AsyncClient) -> None:
    for status in (404, 500, 502, 503):
        response = await stub.post(
            "/stateless/mcp", json=call("status", status=status), headers=auth()
        )
        assert response.status_code == status
    bad = await stub.post("/stateless/mcp", json=call("status", status=200), headers=auth())
    assert bad.json()["error"]["code"] == -32602


async def test_slow_sse_streams_progress_then_the_reply(stub: httpx.AsyncClient) -> None:
    response = await stub.post(
        "/stateless/mcp", json=call("slow_sse", count=2, delay_seconds=0), headers=auth()
    )
    events = [e for e in response.text.split("\n\n") if e]
    assert len(events) == 3
    assert json.loads(events[-1].split("data: ", 1)[1])["id"] == 7


async def test_requests_are_recorded_and_reset(stub: httpx.AsyncClient) -> None:
    await stub.post("/stateless/mcp", json=call("echo"), headers=auth())
    recorded = (await stub.get("/_stub/requests")).json()["requests"]
    assert [(r["persona"], r["tool"], r["rpc_method"]) for r in recorded] == [
        ("alice", "echo", "tools/call")
    ]
    assert recorded[0]["headers"]["authorization"] == "persona:alice"
    await stub.post("/_stub/reset")
    assert (await stub.get("/_stub/requests")).json() == {"requests": []}


def test_keys_file_must_be_strict(tmp_path: Path) -> None:
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({"k": {"persona": "x"}}))
    with pytest.raises(ValueError, match="persona"):
        load_stub_module().load_keys(path)
