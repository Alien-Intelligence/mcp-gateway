"""Gateway-level protocol rules through ``create_app``: origin, Mcp-* mirror, sessions, streams."""

import json
import logging
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from starlette.applications import Starlette

from mcp_gateway.app import _quoted, create_app
from mcp_gateway.proxy import UpstreamStreamAbortedError

from .conftest import FakeUpstream, TrackedStream, base_config_dict, config_from, mint
from .test_app import PRM_URL, RPC_BODY, audit_lines, bearer, client_for

INITIALIZE = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
SSE = {"Content-Type": "text/event-stream"}
REPLY = b'event: message\ndata: {"jsonrpc":"2.0","id":7,"result":{}}\n\n'
PROGRESS = b'data: {"jsonrpc":"2.0","method":"notifications/progress","params":{}}\n\n'


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def two_user_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "two"
    directory.mkdir()
    entries = []
    for who in ("alice", "bob"):
        (directory / f"{who}.key").write_text(f"fake-alien-key-{who}_key\n")
        entries.append(
            {
                "match": {"email": f"{who}@client-x.example"},
                "api_key_file": f"{who}.key",
                "upstream": "primary",
                "labels": {"client": who},
            }
        )
    (directory / "mapping.json").write_text(json.dumps({"entries": entries}))
    return directory


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def gateway(two_user_dir: Path, transport: httpx.MockTransport, clock: Clock) -> Starlette:
    data = base_config_dict(str(two_user_dir / "mapping.json"))
    return create_app(config_from(data), http_transport=transport, session_clock=clock)


@pytest.fixture
async def http(gateway: Starlette) -> AsyncIterator[httpx.AsyncClient]:
    async with client_for(gateway) as client:
        yield client


@pytest.fixture
def alice(rsa_key: rsa.RSAPrivateKey) -> dict[str, str]:
    return bearer(mint(rsa_key, overrides={"sub": "alice", "email": "alice@client-x.example"}))


@pytest.fixture
def bob(rsa_key: rsa.RSAPrivateKey) -> dict[str, str]:
    return bearer(mint(rsa_key, overrides={"sub": "bob", "email": "bob@client-x.example"}))


def issue_session(upstream: FakeUpstream, session_id: str) -> None:
    upstream.responder = lambda _r: httpx.Response(
        200, headers={"Mcp-Session-Id": session_id}, json={"jsonrpc": "2.0", "id": 1, "result": {}}
    )


async def initialize(
    http: httpx.AsyncClient, upstream: FakeUpstream, who: dict[str, str], session_id: str
) -> None:
    issue_session(upstream, session_id)
    response = await http.post("/mcp", json=INITIALIZE, headers=who)
    assert response.headers["mcp-session-id"] == session_id
    upstream.requests.clear()
    upstream.responder = lambda _r: httpx.Response(200, json={"jsonrpc": "2.0", "id": 7})


# --- PRM and challenges (P1) ----------------------------------------------------------


async def test_prm_for_the_public_path_is_the_only_metadata_document(
    http: httpx.AsyncClient,
) -> None:
    ok = await http.get("/.well-known/oauth-protected-resource/mcp")
    assert ok.json()["resource"] == "https://gw.example.com/mcp"
    assert (await http.get("/.well-known/oauth-protected-resource")).status_code == 404


async def test_challenge_resource_metadata_points_at_the_served_document(
    http: httpx.AsyncClient,
) -> None:
    response = await http.post("/mcp", json=RPC_BODY)
    assert f'resource_metadata="{PRM_URL}"' in response.headers["www-authenticate"]
    assert (await http.get(PRM_URL)).status_code == 200


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ('say "hi"', "say  hi "),
        ("back\\slash", "back slash"),
        ("line\r\nbreak", "line  break"),
        ("café", "caf "),
        ("plain text-1.0", "plain text-1.0"),
    ],
)
def test_challenge_values_follow_the_rfc_6750_quoted_string_grammar(
    raw: str, expected: str
) -> None:
    assert _quoted(raw) == expected


# --- origin (P4) ----------------------------------------------------------------------


async def test_unlisted_origin_is_403_before_authentication(
    http: httpx.AsyncClient, upstream: FakeUpstream, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger="mcp_gateway.audit"):
        response = await http.post(
            "/mcp", json=RPC_BODY, headers={"Origin": "https://evil.example"}
        )
    assert response.status_code == 403
    assert "www-authenticate" not in response.headers
    assert response.json() == {
        "jsonrpc": "2.0",
        "id": None,
        "error": {"code": -32008, "message": "The request origin is not allowed."},
    }
    assert upstream.requests == []
    (line,) = audit_lines(caplog)
    assert (line["status"], line["outcome"], line["subject"]) == (403, "origin_refused", None)


@pytest.mark.parametrize("method", ["POST", "GET", "DELETE"])
async def test_origin_is_checked_on_every_method(http: httpx.AsyncClient, method: str) -> None:
    response = await http.request(method, "/mcp", headers={"Origin": "https://evil.example"})
    assert response.status_code == 403


@pytest.mark.parametrize("origin", ["https://app.example.com", "HTTPS://App.Example.Com:443"])
async def test_listed_origin_passes_in_any_spelling(
    http: httpx.AsyncClient, alice: dict[str, str], upstream: FakeUpstream, origin: str
) -> None:
    response = await http.post("/mcp", json=RPC_BODY, headers={**alice, "Origin": origin})
    assert response.status_code == 200
    assert "origin" not in upstream.requests[0].headers


async def test_no_origin_header_passes(http: httpx.AsyncClient, alice: dict[str, str]) -> None:
    assert (await http.post("/mcp", json=RPC_BODY, headers=alice)).status_code == 200


async def test_origin_does_not_gate_discovery(http: httpx.AsyncClient) -> None:
    response = await http.get(
        "/.well-known/oauth-protected-resource/mcp", headers={"Origin": "https://evil.example"}
    )
    assert response.status_code == 200


# --- Mcp-Method / Mcp-Name (P5) -------------------------------------------------------


async def test_method_header_that_disagrees_with_the_body_is_400(
    http: httpx.AsyncClient,
    alice: dict[str, str],
    upstream: FakeUpstream,
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.INFO, logger="mcp_gateway.audit"):
        response = await http.post(
            "/mcp", json=RPC_BODY, headers={**alice, "Mcp-Method": "tools/list"}
        )
    assert response.status_code == 400
    assert response.json() == {
        "jsonrpc": "2.0",
        "id": 7,
        "error": {
            "code": -32020,
            "message": "Header mismatch: Mcp-Method does not match the request body.",
        },
    }
    assert upstream.requests == []
    (line,) = audit_lines(caplog)
    assert (line["status"], line["outcome"]) == (400, "mcp_header_mismatch")


async def test_name_header_that_disagrees_with_the_body_is_400(
    http: httpx.AsyncClient, alice: dict[str, str], upstream: FakeUpstream
) -> None:
    response = await http.post(
        "/mcp", json=RPC_BODY, headers={**alice, "Mcp-Method": "tools/call", "Mcp-Name": "other"}
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == -32020
    assert upstream.requests == []


async def test_matching_headers_are_forwarded(
    http: httpx.AsyncClient, alice: dict[str, str], upstream: FakeUpstream
) -> None:
    response = await http.post(
        "/mcp", json=RPC_BODY, headers={**alice, "Mcp-Method": "tools/call", "Mcp-Name": "search"}
    )
    assert response.status_code == 200
    assert upstream.requests[0].headers["mcp-method"] == "tools/call"
    assert upstream.requests[0].headers["mcp-name"] == "search"


async def test_mismatch_is_checked_after_authentication(http: httpx.AsyncClient) -> None:
    response = await http.post("/mcp", json=RPC_BODY, headers={"Mcp-Method": "tools/list"})
    assert response.status_code == 401


# --- session binding (P3) -------------------------------------------------------------


async def test_session_issued_to_alice_is_usable_by_alice_only(
    http: httpx.AsyncClient,
    alice: dict[str, str],
    bob: dict[str, str],
    upstream: FakeUpstream,
    caplog: pytest.LogCaptureFixture,
) -> None:
    await initialize(http, upstream, alice, "sess-a")
    session = {"Mcp-Session-Id": "sess-a"}
    assert (await http.post("/mcp", json=RPC_BODY, headers={**alice, **session})).status_code == 200
    assert upstream.requests[-1].headers["mcp-session-id"] == "sess-a"
    upstream.requests.clear()

    with caplog.at_level(logging.INFO, logger="mcp_gateway.audit"):
        stolen = await http.post("/mcp", json=RPC_BODY, headers={**bob, **session})
    assert stolen.status_code == 404
    assert stolen.json() == {
        "jsonrpc": "2.0",
        "id": 7,
        "error": {"code": -32007, "message": "The session was not found. Start a new session."},
    }
    assert upstream.requests == []
    (line,) = audit_lines(caplog)
    assert (line["status"], line["outcome"], line["subject"]) == (404, "session_not_found", "bob")
    # Refusing bob must not disturb alice's session.
    assert (await http.post("/mcp", json=RPC_BODY, headers={**alice, **session})).status_code == 200


async def test_unknown_session_id_is_404_and_not_forwarded(
    http: httpx.AsyncClient, alice: dict[str, str], upstream: FakeUpstream
) -> None:
    for method in ("POST", "GET", "DELETE"):
        response = await http.request(
            method, "/mcp", headers={**alice, "Mcp-Session-Id": "never-issued"}
        )
        assert response.status_code == 404
    assert upstream.requests == []


async def test_requests_without_a_session_id_are_unaffected(
    http: httpx.AsyncClient, alice: dict[str, str], upstream: FakeUpstream
) -> None:
    await initialize(http, upstream, alice, "sess-a")
    assert (await http.post("/mcp", json=RPC_BODY, headers=alice)).status_code == 200


async def test_successful_delete_ends_the_binding(
    http: httpx.AsyncClient, alice: dict[str, str], upstream: FakeUpstream
) -> None:
    await initialize(http, upstream, alice, "sess-a")
    session = {**alice, "Mcp-Session-Id": "sess-a"}
    upstream.responder = lambda _r: httpx.Response(204)
    assert (await http.delete("/mcp", headers=session)).status_code == 204
    upstream.requests.clear()
    assert (await http.post("/mcp", json=RPC_BODY, headers=session)).status_code == 404
    assert upstream.requests == []


async def test_refused_delete_keeps_the_binding(
    http: httpx.AsyncClient, alice: dict[str, str], upstream: FakeUpstream
) -> None:
    await initialize(http, upstream, alice, "sess-a")
    session = {**alice, "Mcp-Session-Id": "sess-a"}
    upstream.responder = lambda _r: httpx.Response(405, text="Session termination not supported")
    response = await http.delete("/mcp", headers=session)
    assert (response.status_code, response.text) == (405, "Session termination not supported")
    upstream.responder = lambda _r: httpx.Response(200, json={"jsonrpc": "2.0", "id": 7})
    assert (await http.post("/mcp", json=RPC_BODY, headers=session)).status_code == 200


async def test_upstream_404_passes_through_untouched_and_drops_the_binding(
    http: httpx.AsyncClient, alice: dict[str, str], upstream: FakeUpstream
) -> None:
    await initialize(http, upstream, alice, "sess-a")
    session = {**alice, "Mcp-Session-Id": "sess-a"}
    upstream.responder = lambda _r: httpx.Response(
        404, headers={"X-Upstream": "yes"}, json={"error": "session gone"}
    )
    response = await http.post("/mcp", json=RPC_BODY, headers=session)
    assert response.status_code == 404
    assert response.json() == {"error": "session gone"}
    assert response.headers["x-upstream"] == "yes"
    upstream.requests.clear()
    # Now unknown to the gateway: refused locally.
    again = await http.post("/mcp", json=RPC_BODY, headers=session)
    assert again.json()["error"]["code"] == -32007
    assert upstream.requests == []


async def test_idle_session_expires(
    http: httpx.AsyncClient, alice: dict[str, str], upstream: FakeUpstream, clock: Clock
) -> None:
    await initialize(http, upstream, alice, "sess-a")
    session = {**alice, "Mcp-Session-Id": "sess-a"}
    clock.now += 500
    assert (await http.post("/mcp", json=RPC_BODY, headers=session)).status_code == 200
    clock.now += 500  # 500 s since the last use: still inside the 600 s TTL
    assert (await http.post("/mcp", json=RPC_BODY, headers=session)).status_code == 200
    clock.now += 601
    assert (await http.post("/mcp", json=RPC_BODY, headers=session)).status_code == 404


async def test_session_id_from_a_failed_response_is_not_bound(
    http: httpx.AsyncClient, alice: dict[str, str], upstream: FakeUpstream
) -> None:
    upstream.responder = lambda _r: httpx.Response(500, headers={"Mcp-Session-Id": "ghost"})
    await http.post("/mcp", json=INITIALIZE, headers=alice)
    upstream.requests.clear()
    response = await http.post("/mcp", json=RPC_BODY, headers={**alice, "Mcp-Session-Id": "ghost"})
    assert response.status_code == 404
    assert upstream.requests == []


async def test_audit_records_upstream_session_presence_never_the_id(
    http: httpx.AsyncClient,
    alice: dict[str, str],
    upstream: FakeUpstream,
    caplog: pytest.LogCaptureFixture,
) -> None:
    issue_session(upstream, "sess-secret-id")
    with caplog.at_level(logging.INFO, logger="mcp_gateway.audit"):
        await http.post("/mcp", json=INITIALIZE, headers=alice)
    (line,) = audit_lines(caplog)
    assert line["upstream_session"] is True
    assert "sess-secret-id" not in caplog.text


# --- GET, DELETE, Last-Event-ID (2025-11-25 era) --------------------------------------


async def test_get_stream_forwards_session_and_last_event_id(
    http: httpx.AsyncClient, alice: dict[str, str], upstream: FakeUpstream
) -> None:
    await initialize(http, upstream, alice, "sess-a")
    upstream.responder = lambda _r: httpx.Response(
        200, headers=SSE, content=b"id: 3\ndata: replayed\n\n"
    )
    response = await http.get(
        "/mcp",
        headers={
            **alice,
            "Accept": "text/event-stream",
            "Mcp-Session-Id": "sess-a",
            "Last-Event-ID": "2",
        },
    )
    assert response.content == b"id: 3\ndata: replayed\n\n"
    assert response.headers["x-accel-buffering"] == "no"
    sent = upstream.requests[0]
    assert (sent.method, sent.headers["last-event-id"], sent.headers["mcp-session-id"]) == (
        "GET",
        "2",
        "sess-a",
    )


async def test_upstream_405_on_get_passes_through(
    http: httpx.AsyncClient, alice: dict[str, str], upstream: FakeUpstream
) -> None:
    upstream.responder = lambda _r: httpx.Response(405, headers={"Allow": "POST"}, text="no")
    response = await http.get("/mcp", headers={**alice, "Accept": "text/event-stream"})
    assert (response.status_code, response.text, response.headers["allow"]) == (405, "no", "POST")


# --- mid-stream failure (P2) ----------------------------------------------------------


def sse_responder(upstream: FakeUpstream, stream: TrackedStream) -> None:
    upstream.responder = lambda _r: httpx.Response(200, headers=SSE, stream=stream)


async def test_failure_before_the_reply_discards_the_partial_event_and_reports_the_id(
    http: httpx.AsyncClient,
    alice: dict[str, str],
    upstream: FakeUpstream,
    caplog: pytest.LogCaptureFixture,
) -> None:
    stream = TrackedStream(
        [PROGRESS, b'data: {"jsonrpc":"2.0","id":7,"res'], fail_after=httpx.ReadError("boom")
    )
    sse_responder(upstream, stream)
    with caplog.at_level(logging.INFO, logger="mcp_gateway.audit"):
        response = await http.post("/mcp", json=RPC_BODY, headers=alice)
    assert response.status_code == 200
    events = response.content.split(b"\n\n")
    assert events[0] + b"\n\n" == PROGRESS
    assert b'"id":7,"res' not in response.content.replace(b'"id":7,"error"', b"")
    final = events[1]
    assert final.startswith(b"event: message\ndata: ")
    assert json.loads(final.split(b"data: ", 1)[1]) == {
        "jsonrpc": "2.0",
        "id": 7,
        "error": {
            "code": -32006,
            "message": "The service stream failed before the response was complete.",
        },
    }
    assert events[2:] == [b""]
    assert stream.closed
    (line,) = audit_lines(caplog)
    assert (line["outcome"], line["status"], line["reason"]) == (
        "upstream_stream_failed",
        200,
        "ReadError",
    )


async def test_clean_end_with_a_cut_event_is_also_a_failure(
    http: httpx.AsyncClient, alice: dict[str, str], upstream: FakeUpstream
) -> None:
    sse_responder(upstream, TrackedStream([PROGRESS, b"data: cut"]))
    response = await http.post("/mcp", json=RPC_BODY, headers=alice)
    assert b"data: cut" not in response.content
    assert b'"code":-32006' in response.content


async def test_events_are_relayed_whole_even_when_the_upstream_splits_them(
    http: httpx.AsyncClient, alice: dict[str, str], upstream: FakeUpstream
) -> None:
    payload = PROGRESS + REPLY
    pieces = [payload[i : i + 5] for i in range(0, len(payload), 5)]
    sse_responder(upstream, TrackedStream(pieces))
    response = await http.post("/mcp", json=RPC_BODY, headers=alice)
    assert response.content == payload


async def test_clean_end_without_a_reply_is_left_alone(
    http: httpx.AsyncClient, alice: dict[str, str], upstream: FakeUpstream
) -> None:
    # A server may end the connection after a priming event and let the client poll with GET.
    sse_responder(upstream, TrackedStream([b"id: 1\ndata:\n\n"]))
    response = await http.post("/mcp", json=RPC_BODY, headers=alice)
    assert response.content == b"id: 1\ndata:\n\n"


async def test_failure_after_the_reply_aborts_the_connection(
    http: httpx.AsyncClient,
    alice: dict[str, str],
    upstream: FakeUpstream,
    caplog: pytest.LogCaptureFixture,
) -> None:
    stream = TrackedStream([REPLY], fail_after=httpx.ReadError("boom"))
    sse_responder(upstream, stream)
    with (
        caplog.at_level(logging.INFO, logger="mcp_gateway.audit"),
        pytest.raises(UpstreamStreamAbortedError),
    ):
        await http.post("/mcp", json=RPC_BODY, headers=alice)
    (line,) = audit_lines(caplog)
    assert line["outcome"] == "upstream_stream_failed"
    assert stream.closed


async def test_failure_of_a_request_without_an_id_aborts(
    http: httpx.AsyncClient, alice: dict[str, str], upstream: FakeUpstream
) -> None:
    sse_responder(upstream, TrackedStream([PROGRESS], fail_after=httpx.ReadError("boom")))
    with pytest.raises(UpstreamStreamAbortedError):
        await http.get("/mcp", headers={**alice, "Accept": "text/event-stream"})
    notification = {"jsonrpc": "2.0", "method": "notifications/initialized"}
    sse_responder(upstream, TrackedStream([PROGRESS], fail_after=httpx.ReadError("boom")))
    with pytest.raises(UpstreamStreamAbortedError):
        await http.post("/mcp", json=notification, headers=alice)


async def test_failure_of_a_json_body_aborts_instead_of_ending_cleanly(
    http: httpx.AsyncClient,
    alice: dict[str, str],
    upstream: FakeUpstream,
    caplog: pytest.LogCaptureFixture,
) -> None:
    stream = TrackedStream([b'{"jsonrpc":"2.0","id":7,"re'], fail_after=httpx.ReadError("boom"))
    upstream.responder = lambda _r: httpx.Response(
        200, headers={"Content-Type": "application/json"}, stream=stream
    )
    with (
        caplog.at_level(logging.INFO, logger="mcp_gateway.audit"),
        pytest.raises(UpstreamStreamAbortedError),
    ):
        await http.post("/mcp", json=RPC_BODY, headers=alice)
    (line,) = audit_lines(caplog)
    assert line["outcome"] == "upstream_stream_failed"


async def test_event_over_the_cap_aborts_even_while_the_reply_is_owed(
    two_user_dir: Path,
    transport: httpx.MockTransport,
    upstream: FakeUpstream,
    alice: dict[str, str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    data = base_config_dict(str(two_user_dir / "mapping.json"))
    data["limits"]["max_sse_event_bytes"] = 64
    app = create_app(config_from(data), http_transport=transport)
    stream = TrackedStream([PROGRESS[:20], b"x" * 200, b"\n\n"])
    sse_responder(upstream, stream)
    with caplog.at_level(logging.INFO, logger="mcp_gateway.audit"):
        async with client_for(app) as client:
            with pytest.raises(UpstreamStreamAbortedError):
                await client.post("/mcp", json=RPC_BODY, headers=alice)
    (line,) = audit_lines(caplog)
    assert (line["outcome"], line["reason"]) == ("upstream_stream_failed", "event_too_large")
    assert stream.closed
