"""The tool policy through ``create_app``: refusals, batches, tools/list filtering, no policy."""

import json
import logging
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from starlette.applications import Starlette

from mcp_gateway.app import create_app

from .conftest import FakeUpstream, TrackedStream, base_config_dict, config_from, mint
from .test_app import audit_lines, bearer, client_for

SSE = {"Content-Type": "text/event-stream"}
JSON_TYPE = {"Content-Type": "application/json"}
LIST = {"jsonrpc": "2.0", "id": 5, "method": "tools/list"}
GAP = "The gateway could not apply its tool policy to this response."


def call(name: Any = "search", request_id: Any = 7) -> dict[str, Any]:
    body: dict[str, Any] = {"jsonrpc": "2.0", "method": "tools/call", "params": {"name": name}}
    if request_id is not None:
        body["id"] = request_id
    return body


def tools_reply(request_id: Any = 5, **extra: Any) -> dict[str, Any]:
    tools = [{"name": "search"}, {"name": "delete_all"}, {"name": "get_entry"}]
    return {"jsonrpc": "2.0", "id": request_id, "result": {"tools": tools, **extra}}


POLICY = {
    "default": {"allow": ["search", "get_*"], "deny": ["get_secret"]},
    "rules": [
        {"match": {"organization": "admins"}, "allow": None, "deny": ["delete_all"]},
        {
            "match": {"label": {"name": "client", "value": "client-x"}},
            "allow": ["search"],
            "deny": [],
        },
    ],
}


def build(
    mapping_dir: Path,
    transport: httpx.MockTransport,
    *,
    tools: dict[str, Any] | None = POLICY,
    **limits: int,
) -> Starlette:
    data = base_config_dict(str(mapping_dir / "mapping.json"))
    data["policies"]["tools"] = tools
    data["limits"].update(limits)
    return create_app(config_from(data), http_transport=transport)


@pytest.fixture
async def http(
    mapping_dir: Path, transport: httpx.MockTransport
) -> AsyncIterator[httpx.AsyncClient]:
    async with client_for(build(mapping_dir, transport)) as client:
        yield client


@pytest.fixture
def alice(rsa_key: rsa.RSAPrivateKey) -> dict[str, str]:
    """Organization ``client-x``, resolver label ``client=client-x``: the label rule applies."""
    return bearer(mint(rsa_key))


@pytest.fixture
def admin(rsa_key: rsa.RSAPrivateKey) -> dict[str, str]:
    return bearer(mint(rsa_key, overrides={"org": "admins"}))


def respond_json(upstream: FakeUpstream, document: Any, **headers: str) -> None:
    upstream.responder = lambda _r: httpx.Response(
        200, headers={**JSON_TYPE, **headers}, content=json.dumps(document).encode()
    )


def respond_sse(upstream: FakeUpstream, chunks: list[bytes]) -> None:
    upstream.responder = lambda _r: httpx.Response(
        200, headers=SSE, stream=TrackedStream(list(chunks))
    )


def event(document: Any) -> bytes:
    return b"event: message\ndata: " + json.dumps(document).encode() + b"\n\n"


def sse_documents(raw: bytes) -> list[dict[str, Any]]:
    return [json.loads(line[5:]) for line in raw.split(b"\n") if line.startswith(b"data:")]


def tool_names(document: dict[str, Any]) -> list[str]:
    return [tool["name"] for tool in document["result"]["tools"]]


# --- tools/call -------------------------------------------------------------------------


async def test_a_denied_tool_looks_like_a_missing_one_and_is_never_forwarded(
    http: httpx.AsyncClient,
    alice: dict[str, str],
    upstream: FakeUpstream,
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.INFO, logger="mcp_gateway.audit"):
        response = await http.post("/mcp", json=call("delete_all"), headers=alice)
    assert response.status_code == 200
    assert response.json() == {
        "jsonrpc": "2.0",
        "id": 7,
        "error": {"code": -32602, "message": "Unknown tool: delete_all"},
    }
    assert upstream.requests == []
    (line,) = audit_lines(caplog)
    assert (line["outcome"], line["status"], line["tool"]) == ("tool_denied", 200, "delete_all")
    assert line["upstream_status"] is None


async def test_an_allowed_tool_is_forwarded_untouched(
    http: httpx.AsyncClient, alice: dict[str, str], upstream: FakeUpstream
) -> None:
    body = call("search")
    response = await http.post("/mcp", json=body, headers=alice)
    assert response.status_code == 200
    (sent,) = upstream.requests
    assert json.loads(sent.content) == body


async def test_the_allow_list_is_not_widened_by_the_default_when_a_rule_matches(
    http: httpx.AsyncClient, alice: dict[str, str], upstream: FakeUpstream
) -> None:
    # The default allows get_*, the label rule that applies to alice allows only search.
    response = await http.post("/mcp", json=call("get_entry"), headers=alice)
    assert response.json()["error"]["message"] == "Unknown tool: get_entry"
    assert upstream.requests == []


async def test_an_organization_rule_applies_and_deny_still_wins(
    http: httpx.AsyncClient, admin: dict[str, str], upstream: FakeUpstream
) -> None:
    refused = await http.post("/mcp", json=call("delete_all"), headers=admin)
    assert refused.json()["error"]["code"] == -32602
    assert upstream.requests == []
    await http.post("/mcp", json=call("anything_else"), headers=admin)
    assert len(upstream.requests) == 1


async def test_a_denied_tool_is_refused_in_the_default_policy_too(
    mapping_dir: Path,
    transport: httpx.MockTransport,
    upstream: FakeUpstream,
    rsa_key: rsa.RSAPrivateKey,
) -> None:
    only_default = {"default": POLICY["default"], "rules": []}
    token = bearer(mint(rsa_key))
    async with client_for(build(mapping_dir, transport, tools=only_default)) as client:
        secret = await client.post("/mcp", json=call("get_secret"), headers=token)
        assert secret.json()["error"]["message"] == "Unknown tool: get_secret"
        await client.post("/mcp", json=call("get_entry"), headers=token)
    assert len(upstream.requests) == 1


@pytest.mark.parametrize(
    "params",
    [None, {}, {"name": 5}, {"name": None}, {"name": ["search"]}, [], "search"],
)
async def test_a_call_whose_tool_cannot_be_read_is_refused(
    http: httpx.AsyncClient,
    alice: dict[str, str],
    upstream: FakeUpstream,
    caplog: pytest.LogCaptureFixture,
    params: Any,
) -> None:
    body: dict[str, Any] = {"jsonrpc": "2.0", "id": 7, "method": "tools/call"}
    if params is not None:
        body["params"] = params
    with caplog.at_level(logging.INFO, logger="mcp_gateway.audit"):
        response = await http.post("/mcp", json=body, headers=alice)
    assert response.status_code == 200
    assert response.json()["error"] == {"code": -32602, "message": "Invalid params"}
    assert response.json()["id"] == 7
    assert upstream.requests == []
    (line,) = audit_lines(caplog)
    assert (line["outcome"], line["reason"]) == ("tool_denied", "invalid_params")


@pytest.mark.parametrize("name", ["delete_all", None])
async def test_a_refused_call_without_an_id_is_an_http_400_with_a_null_id(
    http: httpx.AsyncClient, alice: dict[str, str], upstream: FakeUpstream, name: Any
) -> None:
    response = await http.post("/mcp", json=call(name, request_id=None), headers=alice)
    assert response.status_code == 400
    assert response.json()["id"] is None
    assert response.json()["error"]["code"] == -32602
    assert upstream.requests == []


async def test_a_call_with_a_string_id_is_answered_with_that_id(
    http: httpx.AsyncClient, alice: dict[str, str]
) -> None:
    response = await http.post("/mcp", json=call("delete_all", request_id="abc"), headers=alice)
    assert response.json()["id"] == "abc"


async def test_other_methods_are_not_subject_to_the_tool_policy(
    http: httpx.AsyncClient, alice: dict[str, str], upstream: FakeUpstream
) -> None:
    for method in ("initialize", "ping", "resources/list"):
        await http.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": method}, headers=alice)
    assert len(upstream.requests) == 3


# --- bodies the gateway cannot read ----------------------------------------------------

CALL_DELETE_ALL = b'{"jsonrpc":"2.0","id":7,"method":"tools/call","params":{"name":"delete_all"}'

UNREADABLE_BODIES = {
    "invalid_utf8": CALL_DELETE_ALL + b',"x":"\xff"}',
    "utf8_bom": b"\xef\xbb\xbf" + CALL_DELETE_ALL + b"}",
    "utf16": (CALL_DELETE_ALL + b"}").decode().encode("utf-16"),
    "truncated": CALL_DELETE_ALL,
    "not_json": b"delete_all please",
    "scalar_number": b"5",
    "scalar_string": b'"tools/call"',
    "scalar_null": b"null",
    "deep_nesting": b"[" * 10_000 + b"]" * 10_000,
}


@pytest.mark.parametrize("body", UNREADABLE_BODIES.values(), ids=UNREADABLE_BODIES.keys())
async def test_a_body_that_is_not_utf8_json_is_refused_and_never_forwarded(
    http: httpx.AsyncClient,
    alice: dict[str, str],
    upstream: FakeUpstream,
    caplog: pytest.LogCaptureFixture,
    body: bytes,
) -> None:
    with caplog.at_level(logging.INFO, logger="mcp_gateway.audit"):
        response = await http.post("/mcp", content=body, headers={**alice, **JSON_TYPE})
    assert response.status_code == 400
    assert response.json() == {
        "jsonrpc": "2.0",
        "id": None,
        "error": {"code": -32700, "message": "Parse error"},
    }
    assert upstream.requests == []
    (line,) = audit_lines(caplog)
    assert (line["outcome"], line["status"]) == ("unparsable_request", 400)


@pytest.mark.parametrize("charset", ["latin-1", "ISO-8859-1", "utf-16", "UTF-8-SIG", "nonsense"])
async def test_a_charset_other_than_utf8_is_refused_even_for_ascii_bodies(
    http: httpx.AsyncClient,
    alice: dict[str, str],
    upstream: FakeUpstream,
    caplog: pytest.LogCaptureFixture,
    charset: str,
) -> None:
    with caplog.at_level(logging.INFO, logger="mcp_gateway.audit"):
        response = await http.post(
            "/mcp",
            content=json.dumps(call("search")).encode(),
            headers={**alice, "Content-Type": f"application/json; charset={charset}"},
        )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == -32700
    assert upstream.requests == []
    (line,) = audit_lines(caplog)
    assert line["outcome"] == "unparsable_request"


@pytest.mark.parametrize(
    "content_type",
    [
        "application/json",
        "application/json; charset=utf-8",
        "application/json;charset=UTF-8",
        'application/json; charset="utf-8"',
    ],
)
async def test_an_absent_or_utf8_charset_is_accepted_in_any_case(
    http: httpx.AsyncClient, alice: dict[str, str], upstream: FakeUpstream, content_type: str
) -> None:
    response = await http.post(
        "/mcp",
        content=json.dumps(call("search")).encode(),
        headers={**alice, "Content-Type": content_type},
    )
    assert response.status_code == 200
    assert len(upstream.requests) == 1


async def test_an_empty_body_is_not_unparsable(
    http: httpx.AsyncClient, alice: dict[str, str], upstream: FakeUpstream
) -> None:
    upstream.responder = lambda _r: httpx.Response(202)
    response = await http.post("/mcp", content=b"", headers={**alice, **JSON_TYPE})
    assert response.status_code == 202
    assert len(upstream.requests) == 1


async def test_without_a_policy_an_unreadable_body_is_forwarded_untouched(
    mapping_dir: Path,
    transport: httpx.MockTransport,
    upstream: FakeUpstream,
    alice: dict[str, str],
) -> None:
    async with client_for(build(mapping_dir, transport, tools=None)) as client:
        for body in UNREADABLE_BODIES.values():
            response = await client.post(
                "/mcp",
                content=body,
                headers={**alice, "Content-Type": "application/json; charset=latin-1"},
            )
            assert response.status_code == 200
    assert [sent.content for sent in upstream.requests] == list(UNREADABLE_BODIES.values())


# --- request ids the gateway cannot correlate -----------------------------------------

BAD_IDS = {
    "float": 1.0,
    "fraction": 1.5,
    "true": True,
    "false": False,
    "null": None,
    "object": {"a": 1},
    "array": [1],
}


@pytest.mark.parametrize("bad_id", BAD_IDS.values(), ids=BAD_IDS.keys())
@pytest.mark.parametrize("method", ["tools/list", "tools/call", "ping"])
async def test_a_request_whose_id_is_not_a_string_or_integer_is_refused(
    http: httpx.AsyncClient,
    alice: dict[str, str],
    upstream: FakeUpstream,
    caplog: pytest.LogCaptureFixture,
    method: str,
    bad_id: Any,
) -> None:
    body = {"jsonrpc": "2.0", "id": bad_id, "method": method, "params": {"name": "delete_all"}}
    with caplog.at_level(logging.INFO, logger="mcp_gateway.audit"):
        response = await http.post("/mcp", json=body, headers=alice)
    assert response.status_code == 400
    assert response.json() == {
        "jsonrpc": "2.0",
        "id": None,
        "error": {"code": -32600, "message": "Invalid Request"},
    }
    assert upstream.requests == []
    (line,) = audit_lines(caplog)
    assert (line["outcome"], line["status"]) == ("invalid_request", 400)


@pytest.mark.parametrize("bad_id", BAD_IDS.values(), ids=BAD_IDS.keys())
async def test_a_batch_element_with_a_bad_id_is_refused(
    http: httpx.AsyncClient, alice: dict[str, str], upstream: FakeUpstream, bad_id: Any
) -> None:
    batch = [
        {"jsonrpc": "2.0", "id": 1, "method": "ping"},
        {"jsonrpc": "2.0", "id": bad_id, "method": "ping"},
    ]
    response = await http.post("/mcp", json=batch, headers=alice)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == -32600
    assert upstream.requests == []


@pytest.mark.parametrize("good_id", [0, -3, 2**40, "", "7"])
async def test_string_and_integer_ids_are_accepted(
    http: httpx.AsyncClient, alice: dict[str, str], upstream: FakeUpstream, good_id: Any
) -> None:
    response = await http.post("/mcp", json=call("search", request_id=good_id), headers=alice)
    assert response.status_code == 200
    assert len(upstream.requests) == 1


async def test_a_notification_and_a_client_response_are_not_checked_for_an_id(
    http: httpx.AsyncClient, alice: dict[str, str], upstream: FakeUpstream
) -> None:
    """Without a method the object is a response, not a request; without an id, a notification."""
    upstream.responder = lambda _r: httpx.Response(202)
    for body in (
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 1.5, "result": {}},
    ):
        response = await http.post("/mcp", json=body, headers=alice)
        assert response.status_code == 202
    assert len(upstream.requests) == 2


async def test_without_a_policy_a_bad_id_is_forwarded(
    mapping_dir: Path,
    transport: httpx.MockTransport,
    upstream: FakeUpstream,
    alice: dict[str, str],
) -> None:
    async with client_for(build(mapping_dir, transport, tools=None)) as client:
        response = await client.post(
            "/mcp", json={"jsonrpc": "2.0", "id": 1.0, "method": "ping"}, headers=alice
        )
    assert response.status_code == 200
    assert len(upstream.requests) == 1


# --- batches ----------------------------------------------------------------------------


@pytest.mark.parametrize("method", ["tools/call", "tools/list"])
async def test_a_batch_with_a_tool_request_is_refused_and_not_forwarded(
    http: httpx.AsyncClient,
    alice: dict[str, str],
    upstream: FakeUpstream,
    caplog: pytest.LogCaptureFixture,
    method: str,
) -> None:
    batch = [
        {"jsonrpc": "2.0", "id": 1, "method": "ping"},
        {"jsonrpc": "2.0", "id": 2, "method": method, "params": {"name": "delete_all"}},
    ]
    with caplog.at_level(logging.INFO, logger="mcp_gateway.audit"):
        response = await http.post("/mcp", json=batch, headers=alice)
    assert response.status_code == 400
    body = response.json()
    assert body["id"] is None and body["error"]["code"] == -32600
    assert "atch" in body["error"]["message"]
    assert upstream.requests == []
    (line,) = audit_lines(caplog)
    assert (line["outcome"], line["status"]) == ("batch_refused", 400)


async def test_a_batch_without_tool_methods_is_forwarded(
    http: httpx.AsyncClient, alice: dict[str, str], upstream: FakeUpstream
) -> None:
    batch = [{"jsonrpc": "2.0", "id": 1, "method": "ping"}]
    await http.post("/mcp", json=batch, headers=alice)
    (sent,) = upstream.requests
    assert json.loads(sent.content) == batch


# --- tools/list, JSON -------------------------------------------------------------------


async def test_a_json_tool_list_is_filtered_for_the_caller(
    http: httpx.AsyncClient, alice: dict[str, str], admin: dict[str, str], upstream: FakeUpstream
) -> None:
    respond_json(upstream, tools_reply())
    mine = await http.post("/mcp", json=LIST, headers=alice)
    theirs = await http.post("/mcp", json=LIST, headers=admin)
    assert tool_names(mine.json()) == ["search"]
    assert tool_names(theirs.json()) == ["search", "get_entry"]


async def test_the_filtered_response_has_a_correct_length_and_the_upstream_headers(
    http: httpx.AsyncClient,
    alice: dict[str, str],
    upstream: FakeUpstream,
    caplog: pytest.LogCaptureFixture,
) -> None:
    respond_json(
        upstream,
        tools_reply(),
        **{"Mcp-Session-Id": "s-1", "X-Request-Id": "up-1", "X-Custom": "kept"},
    )
    with caplog.at_level(logging.INFO, logger="mcp_gateway.audit"):
        response = await http.post("/mcp", json=LIST, headers=alice)
    assert int(response.headers["content-length"]) == len(response.content)
    assert response.headers["content-type"] == "application/json"
    assert response.headers["x-custom"] == "kept"
    assert response.headers["x-request-id"] != "up-1"
    (line,) = audit_lines(caplog)
    assert (line["outcome"], line["status"], line["upstream_request_id"]) == (
        "forwarded",
        200,
        "up-1",
    )


async def test_cache_scope_ttl_and_cursor_survive_filtering(
    http: httpx.AsyncClient, alice: dict[str, str], upstream: FakeUpstream
) -> None:
    respond_json(upstream, tools_reply(cacheScope="public", ttlMs=30000, nextCursor="p2"))
    result = (await http.post("/mcp", json=LIST, headers=alice)).json()["result"]
    assert (result["cacheScope"], result["ttlMs"], result["nextCursor"]) == ("private", 30000, "p2")


async def test_an_absent_cache_scope_stays_absent(
    http: httpx.AsyncClient, alice: dict[str, str], upstream: FakeUpstream
) -> None:
    respond_json(upstream, tools_reply(ttlMs=1))
    result = (await http.post("/mcp", json=LIST, headers=alice)).json()["result"]
    assert "cacheScope" not in result


async def test_an_upstream_json_rpc_error_to_a_tool_list_is_relayed_unchanged(
    http: httpx.AsyncClient, alice: dict[str, str], upstream: FakeUpstream
) -> None:
    raw = b'{ "jsonrpc": "2.0", "id": 5, "error": {"code": -32603, "message": "boom"} }'
    upstream.responder = lambda _r: httpx.Response(200, headers=JSON_TYPE, content=raw)
    response = await http.post("/mcp", json=LIST, headers=alice)
    assert response.content == raw


@pytest.mark.parametrize(
    "content",
    [
        b"not json",
        b"",
        b'{"jsonrpc":"2.0","id":5,"result":{"tools":"x"}}',
        b'{"jsonrpc":"2.0","id":5,"result":{}}',
        b'{"jsonrpc":"2.0","id":6,"result":{"tools":[{"name":"delete_all"}]}}',
        b'[{"jsonrpc":"2.0","id":5,"result":{"tools":[{"name":"delete_all"}]}}]',
    ],
)
async def test_an_unfilterable_json_reply_is_replaced_and_nothing_unfiltered_is_relayed(
    http: httpx.AsyncClient,
    alice: dict[str, str],
    upstream: FakeUpstream,
    caplog: pytest.LogCaptureFixture,
    content: bytes,
) -> None:
    upstream.responder = lambda _r: httpx.Response(200, headers=JSON_TYPE, content=content)
    with caplog.at_level(logging.INFO, logger="mcp_gateway.audit"):
        response = await http.post("/mcp", json=LIST, headers=alice)
    assert response.status_code == 200
    assert response.json() == {
        "jsonrpc": "2.0",
        "id": 5,
        "error": {"code": -32005, "message": GAP},
    }
    assert b"delete_all" not in response.content
    (line,) = audit_lines(caplog)
    assert line["outcome"] == "tool_policy_failed" and line["reason"] is not None


async def test_an_oversize_json_reply_is_replaced(
    mapping_dir: Path,
    transport: httpx.MockTransport,
    upstream: FakeUpstream,
    alice: dict[str, str],
) -> None:
    respond_json(upstream, tools_reply())
    async with client_for(build(mapping_dir, transport, max_filtered_response_bytes=50)) as client:
        response = await client.post("/mcp", json=LIST, headers=alice)
    assert response.json()["error"]["code"] == -32005
    assert b"delete_all" not in response.content


async def test_an_oversize_chunked_json_reply_is_replaced(
    mapping_dir: Path,
    transport: httpx.MockTransport,
    upstream: FakeUpstream,
    alice: dict[str, str],
) -> None:
    raw = json.dumps(tools_reply()).encode()
    stream = TrackedStream([raw[:40], raw[40:80], raw[80:]])
    upstream.responder = lambda _r: httpx.Response(200, headers=JSON_TYPE, stream=stream)
    async with client_for(build(mapping_dir, transport, max_filtered_response_bytes=50)) as client:
        response = await client.post("/mcp", json=LIST, headers=alice)
    assert response.json()["error"]["code"] == -32005
    assert stream.closed


async def test_an_encoded_json_reply_is_replaced(
    http: httpx.AsyncClient, alice: dict[str, str], upstream: FakeUpstream
) -> None:
    upstream.responder = lambda _r: httpx.Response(
        200,
        headers={**JSON_TYPE, "Content-Encoding": "gzip"},
        content=json.dumps(tools_reply()).encode(),
    )
    response = await http.post("/mcp", json=LIST, headers=alice)
    assert response.json()["error"]["code"] == -32005


async def test_a_success_reply_that_is_neither_json_nor_sse_is_replaced(
    http: httpx.AsyncClient, alice: dict[str, str], upstream: FakeUpstream
) -> None:
    upstream.responder = lambda _r: httpx.Response(202)
    response = await http.post("/mcp", json=LIST, headers=alice)
    assert response.json() == {
        "jsonrpc": "2.0",
        "id": 5,
        "error": {"code": -32005, "message": GAP},
    }


UPSTREAM_ERROR = "The service returned an error for this request."


@pytest.mark.parametrize("status", [400, 404, 429, 500, 503])
async def test_a_failed_tool_list_keeps_its_status_but_never_its_body(
    http: httpx.AsyncClient,
    alice: dict[str, str],
    upstream: FakeUpstream,
    caplog: pytest.LogCaptureFixture,
    status: int,
) -> None:
    leak = json.dumps(tools_reply()).encode()
    upstream.responder = lambda _r: httpx.Response(
        status, headers={**JSON_TYPE, "Retry-After": "3"}, content=leak
    )
    with caplog.at_level(logging.INFO, logger="mcp_gateway.audit"):
        response = await http.post("/mcp", json=LIST, headers=alice)
    assert response.status_code == status
    assert response.json() == {
        "jsonrpc": "2.0",
        "id": 5,
        "error": {"code": -32005, "message": UPSTREAM_ERROR},
    }
    assert response.headers["retry-after"] == "3"
    (line,) = audit_lines(caplog)
    assert (line["outcome"], line["status"], line["upstream_status"]) == (
        "upstream_error",
        status,
        status,
    )


async def test_a_failed_tool_list_sent_as_a_stream_is_replaced_too(
    http: httpx.AsyncClient, alice: dict[str, str], upstream: FakeUpstream
) -> None:
    stream = TrackedStream([event(tools_reply())])
    upstream.responder = lambda _r: httpx.Response(500, headers=SSE, stream=stream)
    response = await http.post("/mcp", json=LIST, headers=alice)
    assert response.status_code == 500
    assert response.json()["error"]["message"] == UPSTREAM_ERROR
    assert b"delete_all" not in response.content
    assert stream.closed


async def test_the_other_failed_responses_are_still_relayed_as_they_came(
    http: httpx.AsyncClient, alice: dict[str, str], upstream: FakeUpstream
) -> None:
    upstream.responder = lambda _r: httpx.Response(500, headers=JSON_TYPE, content=b'{"e":1}')
    response = await http.post("/mcp", json=call("search"), headers=alice)
    assert (response.status_code, response.content) == (500, b'{"e":1}')


async def test_a_tool_list_without_an_id_is_forwarded_as_it_is_not_a_request(
    http: httpx.AsyncClient, alice: dict[str, str], upstream: FakeUpstream
) -> None:
    upstream.responder = lambda _r: httpx.Response(202)
    response = await http.post(
        "/mcp", json={"jsonrpc": "2.0", "method": "tools/list"}, headers=alice
    )
    assert response.status_code == 202
    assert len(upstream.requests) == 1


# --- tools/list, SSE --------------------------------------------------------------------


async def test_an_sse_tool_list_is_filtered_and_progress_events_pass_unchanged(
    http: httpx.AsyncClient, alice: dict[str, str], upstream: FakeUpstream
) -> None:
    progress = b'event: message\ndata: {"jsonrpc":"2.0","method":"notifications/progress"}\n\n'
    respond_sse(upstream, [progress, event(tools_reply(cacheScope="public", ttlMs=9))])
    response = await http.post("/mcp", json=LIST, headers=alice)
    assert response.content.startswith(progress)
    (reply,) = sse_documents(response.content[len(progress) :])
    assert tool_names(reply) == ["search"]
    assert (reply["result"]["cacheScope"], reply["result"]["ttlMs"]) == ("private", 9)


async def test_an_sse_reply_cut_into_small_chunks_is_still_filtered(
    http: httpx.AsyncClient, alice: dict[str, str], upstream: FakeUpstream
) -> None:
    raw = event(tools_reply())
    respond_sse(upstream, [raw[i : i + 7] for i in range(0, len(raw), 7)])
    response = await http.post("/mcp", json=LIST, headers=alice)
    (reply,) = sse_documents(response.content)
    assert tool_names(reply) == ["search"]


async def test_an_unfilterable_sse_reply_is_replaced_by_the_error_event(
    http: httpx.AsyncClient,
    alice: dict[str, str],
    upstream: FakeUpstream,
    caplog: pytest.LogCaptureFixture,
) -> None:
    progress = b'data: {"jsonrpc":"2.0","method":"notifications/progress"}\n\n'
    bad = {"jsonrpc": "2.0", "id": 5, "result": {"tools": "delete_all"}}
    respond_sse(upstream, [progress, event(bad)])
    with caplog.at_level(logging.INFO, logger="mcp_gateway.audit"):
        response = await http.post("/mcp", json=LIST, headers=alice)
    assert response.content.startswith(progress)
    (error,) = sse_documents(response.content[len(progress) :])
    assert error == {"jsonrpc": "2.0", "id": 5, "error": {"code": -32005, "message": GAP}}
    assert b"delete_all" not in response.content
    (line,) = audit_lines(caplog)
    assert line["outcome"] == "tool_policy_failed"


async def test_a_batched_sse_reply_is_replaced(
    http: httpx.AsyncClient, alice: dict[str, str], upstream: FakeUpstream
) -> None:
    respond_sse(upstream, [event([tools_reply()])])
    response = await http.post("/mcp", json=LIST, headers=alice)
    (error,) = sse_documents(response.content)
    assert error["error"]["code"] == -32005
    assert b"delete_all" not in response.content


async def test_an_sse_reply_made_too_large_by_rewriting_respects_the_event_limit(
    mapping_dir: Path,
    transport: httpx.MockTransport,
    upstream: FakeUpstream,
    alice: dict[str, str],
) -> None:
    # Escaped non-ASCII text re-serialises larger than it arrived (UTF-8 in, \\u escapes out).
    reply = tools_reply()
    reply["result"]["tools"][0]["description"] = "é" * 40
    raw = (
        b"data: " + json.dumps(reply, ensure_ascii=False, separators=(",", ":")).encode() + b"\n\n"
    )
    respond_sse(upstream, [raw])
    async with client_for(build(mapping_dir, transport, max_sse_event_bytes=len(raw))) as client:
        response = await client.post("/mcp", json=LIST, headers=alice)
    (error,) = sse_documents(response.content)
    assert error["error"]["code"] == -32005


# --- no policy --------------------------------------------------------------------------


NO_POLICY_JSON = b'{ "jsonrpc" : "2.0",\n "id": 5, "result": {"tools": [{"name": "delete_all"}], "cacheScope": "public"} }'
NO_POLICY_SSE = [
    b": hello\n\n",
    b'id: 1\nevent: message\ndata:   {"jsonrpc":"2.0","id":5,\r\ndata: "result":{"tools":[{"name":"delete_all"}]}}\r\n\r\n',
]


async def test_without_a_policy_a_json_response_is_relayed_byte_for_byte(
    mapping_dir: Path,
    transport: httpx.MockTransport,
    upstream: FakeUpstream,
    alice: dict[str, str],
) -> None:
    upstream.responder = lambda _r: httpx.Response(200, headers=JSON_TYPE, content=NO_POLICY_JSON)
    async with client_for(build(mapping_dir, transport, tools=None)) as client:
        response = await client.post("/mcp", json=LIST, headers=alice)
    assert response.content == NO_POLICY_JSON


async def test_without_a_policy_an_sse_response_is_relayed_byte_for_byte(
    mapping_dir: Path,
    transport: httpx.MockTransport,
    upstream: FakeUpstream,
    alice: dict[str, str],
) -> None:
    respond_sse(upstream, NO_POLICY_SSE)
    async with client_for(build(mapping_dir, transport, tools=None)) as client:
        response = await client.post("/mcp", json=LIST, headers=alice)
    assert response.content == b"".join(NO_POLICY_SSE)


async def test_without_a_policy_nothing_is_refused(
    mapping_dir: Path,
    transport: httpx.MockTransport,
    upstream: FakeUpstream,
    alice: dict[str, str],
) -> None:
    batch = [{"jsonrpc": "2.0", "id": 1, "method": "tools/list"}]
    async with client_for(build(mapping_dir, transport, tools=None)) as client:
        await client.post("/mcp", json=call("delete_all"), headers=alice)
        await client.post("/mcp", json=call(None), headers=alice)
        await client.post("/mcp", json=batch, headers=alice)
    assert len(upstream.requests) == 3


# --- SSE filter: every response-shaped event, nothing unreadable ------------------------


async def post_list(
    http: httpx.AsyncClient, alice: dict[str, str], upstream: FakeUpstream, chunks: list[bytes]
) -> bytes:
    respond_sse(upstream, chunks)
    response = await http.post("/mcp", json=LIST, headers=alice)
    assert response.status_code == 200
    return response.content


def gateway_error_event(request_id: Any) -> bytes:
    body = {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32005, "message": GAP}}
    return b"event: message\ndata: " + json.dumps(body, separators=(",", ":")).encode() + b"\n\n"


@pytest.mark.parametrize("reply_id", ["5", 5.0, 99, None, "abc"])
async def test_a_reply_event_is_filtered_whatever_its_id(
    http: httpx.AsyncClient,
    alice: dict[str, str],
    upstream: FakeUpstream,
    reply_id: Any,
) -> None:
    raw = await post_list(http, alice, upstream, [event(tools_reply(reply_id))])
    (reply,) = sse_documents(raw)
    assert tool_names(reply) == ["search"]
    assert b"delete_all" not in raw


async def test_every_reply_event_of_the_stream_is_filtered_not_only_the_first(
    http: httpx.AsyncClient, alice: dict[str, str], upstream: FakeUpstream
) -> None:
    raw = await post_list(
        http, alice, upstream, [event(tools_reply(5)), event(tools_reply(5)), event(tools_reply(8))]
    )
    replies = sse_documents(raw)
    assert [tool_names(reply) for reply in replies] == [["search"]] * 3
    assert b"delete_all" not in raw


@pytest.mark.parametrize(
    "data",
    [
        b'{"jsonrpc":"2.0","id":5,"result":{"tools":[{"name":"delete_all","x":"\xff"}]}}',
        b"not json at all",
        b'{"jsonrpc":"2.0","id":5,"result":{"tools":[{"name":"delete_all"}',
        b'[{"jsonrpc":"2.0","id":5,"result":{"tools":[{"name":"delete_all"}]}}]',
        b'{"jsonrpc":"2.0","id":5,"result":{"tools":[{"name":"delete_all","s":'
        + b"[" * 10_000
        + b"]" * 10_000
        + b"}]}}",
    ],
    ids=["invalid_utf8", "not_json", "truncated", "array", "deep_nesting"],
)
async def test_an_event_the_gateway_cannot_parse_becomes_the_error_event(
    http: httpx.AsyncClient,
    alice: dict[str, str],
    upstream: FakeUpstream,
    caplog: pytest.LogCaptureFixture,
    data: bytes,
) -> None:
    with caplog.at_level(logging.INFO, logger="mcp_gateway.audit"):
        raw = await post_list(http, alice, upstream, [b"data: " + data + b"\n\n"])
    assert raw == gateway_error_event(5)
    assert b"delete_all" not in raw
    (line,) = audit_lines(caplog)
    assert (line["outcome"], line["status"]) == ("tool_policy_failed", 200)


async def test_two_unreadable_events_are_audited_once(
    http: httpx.AsyncClient,
    alice: dict[str, str],
    upstream: FakeUpstream,
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.INFO, logger="mcp_gateway.audit"):
        raw = await post_list(http, alice, upstream, [b"data: x\n\n", b"data: y\n\n"])
    assert raw == gateway_error_event(5) * 2
    assert len(audit_lines(caplog)) == 1


async def test_a_leading_byte_order_mark_does_not_hide_the_first_event(
    http: httpx.AsyncClient, alice: dict[str, str], upstream: FakeUpstream
) -> None:
    first = b"\xef\xbb\xbfdata: " + json.dumps(tools_reply()).encode() + b"\n\n"
    raw = await post_list(http, alice, upstream, [first])
    (reply,) = sse_documents(raw.removeprefix(b"\xef\xbb\xbf"))
    assert tool_names(reply) == ["search"]
    assert b"delete_all" not in raw


async def test_events_that_carry_no_message_pass_byte_identical(
    http: httpx.AsyncClient, alice: dict[str, str], upstream: FakeUpstream
) -> None:
    chunks = [
        b": keep-alive\n\n",
        b"retry: 1000\n\n",
        b"id: 4\ndata:\n\n",
        b'event: message\ndata: {"jsonrpc":"2.0","method":"notifications/progress"}\n\n',
        b'data: {"jsonrpc":"2.0","id":3,"method":"sampling/createMessage"}\n\n',
        b'data: {"jsonrpc":"2.0","id":5,"error":{"code":-1,"message":"x"}}\n\n',
    ]
    assert await post_list(http, alice, upstream, chunks) == b"".join(chunks)


# --- every event stream is filtered, not only the one of a tools/list POST ---------------


async def test_a_resumed_get_stream_that_replays_a_tool_list_is_filtered(
    http: httpx.AsyncClient, alice: dict[str, str], upstream: FakeUpstream
) -> None:
    notification = b'id: 1\ndata: {"jsonrpc":"2.0","method":"notifications/message"}\n\n'
    respond_sse(upstream, [notification, event(tools_reply(5))])
    response = await http.get(
        "/mcp", headers={**alice, "Last-Event-ID": "0", "Accept": "text/event-stream"}
    )
    assert response.status_code == 200
    assert response.content.startswith(notification)
    (reply,) = sse_documents(response.content[len(notification) :])
    assert tool_names(reply) == ["search"]
    assert b"delete_all" not in response.content


async def test_a_get_stream_event_the_gateway_cannot_read_becomes_an_error_with_a_null_id(
    http: httpx.AsyncClient,
    alice: dict[str, str],
    upstream: FakeUpstream,
    caplog: pytest.LogCaptureFixture,
) -> None:
    respond_sse(upstream, [b"data: \xff\n\n"])
    with caplog.at_level(logging.INFO, logger="mcp_gateway.audit"):
        response = await http.get("/mcp", headers={**alice, "Accept": "text/event-stream"})
    assert response.content == gateway_error_event(None)
    (line,) = audit_lines(caplog)
    assert line["outcome"] == "tool_policy_failed"


async def test_a_tool_list_on_the_stream_of_another_method_is_filtered(
    http: httpx.AsyncClient, alice: dict[str, str], upstream: FakeUpstream
) -> None:
    respond_sse(upstream, [event(tools_reply(7))])
    response = await http.post("/mcp", json=call("search", request_id=7), headers=alice)
    (reply,) = sse_documents(response.content)
    assert tool_names(reply) == ["search"]


async def test_the_events_of_a_tool_call_stream_pass_byte_identical(
    http: httpx.AsyncClient, alice: dict[str, str], upstream: FakeUpstream
) -> None:
    chunks = [
        b'event: message\ndata: {"jsonrpc":"2.0","method":"notifications/progress"}\n\n',
        b'id: 3\nevent: message\ndata:  {"jsonrpc":"2.0",\r\ndata: "id":7,"result":{"content":[{"type":"text","text":"ok"}]}}\r\n\r\n',
    ]
    respond_sse(upstream, chunks)
    response = await http.post("/mcp", json=call("search", request_id=7), headers=alice)
    assert response.content == b"".join(chunks)


async def test_a_json_response_to_another_method_is_not_parsed(
    http: httpx.AsyncClient, alice: dict[str, str], upstream: FakeUpstream
) -> None:
    """A JSON body answers exactly its own POST, so only a tools/list one is filtered."""
    body = b'{ "jsonrpc": "2.0", "id": 7, "result": {"content": []} }'
    upstream.responder = lambda _r: httpx.Response(200, headers=JSON_TYPE, content=body)
    response = await http.post("/mcp", json=call("search", request_id=7), headers=alice)
    assert response.content == body


async def test_the_stream_of_a_request_without_a_policy_is_untouched(
    mapping_dir: Path,
    transport: httpx.MockTransport,
    upstream: FakeUpstream,
    alice: dict[str, str],
) -> None:
    chunks = [b"data: \xff\n\n", event(tools_reply(5))]
    respond_sse(upstream, chunks)
    async with client_for(build(mapping_dir, transport, tools=None)) as client:
        response = await client.get("/mcp", headers={**alice, "Accept": "text/event-stream"})
    assert response.content == b"".join(chunks)


async def test_a_buffered_tool_list_that_never_finishes_ends_in_a_504(
    mapping_dir: Path,
    transport: httpx.MockTransport,
    upstream: FakeUpstream,
    alice: dict[str, str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The read timeout is a wall-clock total for the buffered body, not only per silence."""
    data = base_config_dict(str(mapping_dir / "mapping.json"))
    data["policies"]["tools"] = POLICY
    data["upstreams"]["main"]["read_timeout_seconds"] = 0.2
    raw = json.dumps(tools_reply()).encode()
    stream = TrackedStream([raw[:30]], hang=True)
    upstream.responder = lambda _r: httpx.Response(200, headers=JSON_TYPE, stream=stream)
    async with client_for(create_app(config_from(data), http_transport=transport)) as client:
        with caplog.at_level(logging.INFO, logger="mcp_gateway.audit"):
            response = await client.post("/mcp", json=LIST, headers=alice)
    assert response.status_code == 504
    assert response.json()["error"]["code"] == -32004
    assert stream.closed
    (line,) = audit_lines(caplog)
    assert line["outcome"] == "upstream_error"
