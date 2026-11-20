"""The rewriting of a tools/list reply, on its own: JSON body and SSE event."""

import json
from typing import Any

import pytest

from mcp_gateway.tool_filter import (
    POLICY_FAILED_MESSAGE,
    SseReplyFilter,
    ToolPolicyError,
    filter_json_reply,
    policy_failed_event,
)
from mcp_gateway.tool_policy import EffectivePolicy, ToolPattern

POLICY = EffectivePolicy(allow=(ToolPattern.compile("keep_*"),), deny=())


def reply(result: Any, request_id: Any = 3) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def listing(**extra: Any) -> dict[str, Any]:
    return {"tools": [{"name": "keep_a"}, {"name": "drop_b"}, {"name": "keep_c"}], **extra}


def run_json(document: Any, request_id: str | int = 3) -> dict[str, Any]:
    out = filter_json_reply(json.dumps(document).encode(), request_id, POLICY)
    parsed: dict[str, Any] = json.loads(out)
    return parsed


def names(document: dict[str, Any]) -> list[str]:
    return [tool["name"] for tool in document["result"]["tools"]]


# --- JSON -------------------------------------------------------------------------------


def test_json_reply_is_filtered() -> None:
    assert names(run_json(reply(listing()))) == ["keep_a", "keep_c"]


def test_cache_scope_public_becomes_private() -> None:
    assert run_json(reply(listing(cacheScope="public")))["result"]["cacheScope"] == "private"


def test_any_present_cache_scope_becomes_private() -> None:
    assert run_json(reply(listing(cacheScope="shared")))["result"]["cacheScope"] == "private"


def test_absent_cache_scope_stays_absent() -> None:
    assert "cacheScope" not in run_json(reply(listing()))["result"]


def test_ttl_cursor_and_unknown_fields_are_untouched() -> None:
    extra = {
        "ttlMs": 60000,
        "nextCursor": "page2",
        "_meta": {"a": [1, 2.5, None]},
        "future": {"x": 1},
    }
    document = run_json(reply(listing(cacheScope="public", **extra)))
    assert {k: document["result"][k] for k in extra} == extra
    assert document["jsonrpc"] == "2.0" and document["id"] == 3


def test_string_ids_are_matched_exactly() -> None:
    assert names(run_json(reply(listing(), "abc"), "abc")) == ["keep_a", "keep_c"]


def test_an_error_reply_is_relayed_unchanged() -> None:
    body = b'{ "jsonrpc": "2.0", "id": 3, "error": {"code": -32603, "message": "x"} }'
    assert filter_json_reply(body, 3, POLICY) == body


@pytest.mark.parametrize(
    "body",
    [
        b"not json",
        b"",
        b"\xff\xfe",
        b"[]",
        b'"text"',
        b'{"jsonrpc":"2.0","id":3,"result":[]}',
        b'{"jsonrpc":"2.0","id":3,"result":{"tools":"x"}}',
        b'{"jsonrpc":"2.0","id":3,"result":{}}',
        b'{"jsonrpc":"2.0","id":4,"result":{"tools":[]}}',
        b'{"jsonrpc":"2.0","id":"3","result":{"tools":[]}}',
        b'{"jsonrpc":"2.0","id":true,"result":{"tools":[]}}',
        b'{"jsonrpc":"2.0","result":{"tools":[]}}',
        b'{"jsonrpc":"2.0","method":"notifications/x"}',
        b'{"jsonrpc":"2.0","id":3,"result":{"tools":[{"name":NaN}],"x":NaN}}',
        b"[" * 100000,
    ],
)
def test_a_reply_that_cannot_be_filtered_raises(body: bytes) -> None:
    with pytest.raises(ToolPolicyError):
        filter_json_reply(body, 3, POLICY)


# --- SSE --------------------------------------------------------------------------------


def event(document: Any, *, prefix: bytes = b"") -> bytes:
    return prefix + b"data: " + json.dumps(document).encode() + b"\n\n"


def sse_filter(max_bytes: int = 65536) -> SseReplyFilter:
    return SseReplyFilter(3, POLICY, max_bytes)


def data_json(raw: bytes) -> dict[str, Any]:
    (line,) = [ln for ln in raw.split(b"\n") if ln.startswith(b"data:")]
    parsed: dict[str, Any] = json.loads(line[5:])
    return parsed


def test_the_reply_event_is_filtered_and_stays_well_formed() -> None:
    out = sse_filter().apply(event(reply(listing(cacheScope="public"))))
    assert out.endswith(b"\n\n") and not out.endswith(b"\n\n\n")
    assert names(data_json(out)) == ["keep_a", "keep_c"]
    assert data_json(out)["result"]["cacheScope"] == "private"


def test_other_fields_of_the_reply_event_are_kept_in_order() -> None:
    raw = (
        b"id: 12\nevent: message\nretry: 500\n: a comment\ndata: "
        + json.dumps(reply(listing())).encode()
        + b"\n\n"
    )
    out = sse_filter().apply(raw)
    assert out.split(b"\n")[:4] == [b"id: 12", b"event: message", b"retry: 500", b": a comment"]
    assert names(data_json(out)) == ["keep_a", "keep_c"]


def test_a_reply_split_over_several_data_lines_is_joined() -> None:
    text = json.dumps(reply(listing()), indent=1)
    lines = b"".join(b"data: " + line.encode() + b"\r\n" for line in text.split("\n"))
    out = sse_filter().apply(b"event: message\r\n" + lines + b"\r\n")
    assert out.startswith(b"event: message\ndata: {") and out.count(b"data:") == 1
    assert names(data_json(out)) == ["keep_a", "keep_c"]


def test_events_before_the_reply_pass_byte_identical() -> None:
    progress = b'event: message\ndata: {"jsonrpc":"2.0","method":"notifications/progress"}\n\n'
    server_request = b'data: {"jsonrpc":"2.0","id":3,"method":"sampling/createMessage"}\n\n'
    comment = b": keep-alive\n\n"
    plain = b"data: not json\n\n"
    other_response = event(reply({"tools": []}, 99))
    flow = sse_filter()
    for raw in (progress, server_request, comment, plain, other_response):
        assert flow.apply(raw) == raw


def test_an_error_reply_event_passes_unchanged() -> None:
    raw = b'event: message\ndata: {"jsonrpc":"2.0","id":3,"error":{"code":-1,"message":"x"}}\n\n'
    assert sse_filter().apply(raw) == raw


def test_events_after_the_reply_pass_unchanged() -> None:
    flow = sse_filter()
    flow.apply(event(reply(listing())))
    later = event(reply(listing()))
    assert flow.apply(later) == later


@pytest.mark.parametrize(
    "document",
    [
        reply({"tools": "x"}),
        reply({}),
        reply([]),
        [reply(listing())],
        {"jsonrpc": "2.0", "id": 3, "result": {"tools": [], "x": float("nan")}},
    ],
)
def test_a_reply_event_of_the_wrong_shape_raises(document: Any) -> None:
    raw = b"data: " + json.dumps(document).encode() + b"\n\n"
    with pytest.raises(ToolPolicyError):
        sse_filter().apply(raw)


def test_a_rewritten_event_over_the_event_limit_raises() -> None:
    raw = event(reply(listing()))
    with pytest.raises(ToolPolicyError, match="event_too_large"):
        sse_filter(max_bytes=20).apply(raw)


def test_the_failure_event_is_a_gateway_error_reply() -> None:
    raw = policy_failed_event(3)
    assert raw.startswith(b"event: message\ndata: ") and raw.endswith(b"\n\n")
    assert data_json(raw) == {
        "jsonrpc": "2.0",
        "id": 3,
        "error": {"code": -32005, "message": POLICY_FAILED_MESSAGE},
    }
