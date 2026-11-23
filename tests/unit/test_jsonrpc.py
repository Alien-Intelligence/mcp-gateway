import json

import pytest

from mcp_gateway.jsonrpc import (
    NO_RPC,
    TOO_DEEP,
    UNPARSABLE,
    BodyKind,
    check_mcp_headers,
    declares_non_utf8_charset,
    error_body,
    parse_request,
)


def test_single_request_with_id() -> None:
    body = json.dumps({"jsonrpc": "2.0", "id": 7, "method": "tools/list"}).encode()
    info = parse_request(body)
    assert (info.method, info.id, info.tool, info.expects_reply) == ("tools/list", 7, None, True)


def test_tool_name_extracted_but_not_arguments() -> None:
    body = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": "a",
            "method": "tools/call",
            "params": {"name": "search", "arguments": {"q": "secret"}},
        }
    ).encode()
    info = parse_request(body)
    assert info.tool == "search"
    assert "secret" not in repr(info)


def test_notification_has_no_reply() -> None:
    info = parse_request(b'{"jsonrpc":"2.0","method":"notifications/initialized"}')
    assert info.method == "notifications/initialized"
    assert info.expects_reply is False


def test_an_empty_body_is_not_unparsable() -> None:
    assert parse_request(b"") == NO_RPC
    assert NO_RPC.kind is BodyKind.EMPTY


@pytest.mark.parametrize(
    "body",
    [
        b"not json",
        b"\xff\xfe",
        b'{"method":"x"}\xff',
        b'\xef\xbb\xbf{"method":"tools/call"}',
        '{"method":"x"}'.encode("utf-16"),
        b'"str"',
        b"12",
        b"null",
        b"true",
    ],
)
def test_a_body_that_is_not_a_utf8_json_object_or_array_is_unparsable(body: bytes) -> None:
    assert parse_request(body) == UNPARSABLE
    assert UNPARSABLE.kind is BodyKind.UNPARSABLE


def test_an_array_is_a_batch_whose_elements_are_not_single_requests() -> None:
    info = parse_request(b"[1,2]")
    assert (info.kind, info.method, info.expects_reply, info.tool_batch) == (
        BodyKind.ARRAY,
        None,
        False,
        False,
    )


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (b'[{"jsonrpc":"2.0","id":1,"method":"tools/call"}]', True),
        (b'[{"jsonrpc":"2.0","id":1,"method":"ping"},{"id":2,"method":"tools/list"}]', True),
        (b'[{"jsonrpc":"2.0","id":1,"method":"ping"}]', False),
        (b'[1, null, "tools/call"]', False),
        (b"[]", False),
        (b'{"method":"tools/call"}', False),
    ],
)
def test_a_batch_holding_a_tool_request_is_flagged(body: bytes, expected: bool) -> None:
    assert parse_request(body).tool_batch is expected


@pytest.mark.parametrize(
    ("headers", "refused"),
    [
        ([], False),
        ([("Content-Type", "application/json")], False),
        ([("content-type", "application/json; charset=utf-8")], False),
        ([("Content-Type", "application/json; CHARSET=UTF-8")], False),
        ([("Content-Type", 'application/json; charset="utf8"')], False),
        ([("Content-Type", "application/json; charset=latin-1")], True),
        ([("Content-Type", "application/json; charset=utf-16")], True),
        ([("Content-Type", "application/json; charset=bogus")], True),
        (
            [("Content-Type", "application/json"), ("content-type", "text/plain; charset=ascii")],
            True,
        ),
        ([("Accept", "text/plain; charset=latin-1")], False),
    ],
)
def test_only_a_non_utf8_charset_is_refused(headers: list[tuple[str, str]], refused: bool) -> None:
    assert declares_non_utf8_charset(headers) is refused


def test_boolean_id_is_not_an_id() -> None:
    assert parse_request(b'{"id": true, "method": "x"}').id is None


def test_batch_is_not_a_single_request() -> None:
    assert parse_request(b'[{"id":1,"method":"a"}]').expects_reply is False


def test_error_body_shape() -> None:
    assert error_body(-31001, "m", 3, {"upstream_status": 401}) == {
        "jsonrpc": "2.0",
        "id": 3,
        "error": {"code": -31001, "message": "m", "data": {"upstream_status": 401}},
    }
    assert "data" not in error_body(-31001, "m", None)["error"]


def _rpc(method: str, **params: object) -> bytes:
    return json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()


def test_target_follows_the_method() -> None:
    assert parse_request(_rpc("tools/call", name="t")).target == "t"
    assert parse_request(_rpc("prompts/get", name="p")).target == "p"
    assert parse_request(_rpc("resources/read", uri="file:///x")).target == "file:///x"
    assert parse_request(_rpc("tools/list")).target is None
    assert parse_request(_rpc("prompts/get", name="p")).tool is None


def test_consistent_headers_pass() -> None:
    rpc = parse_request(_rpc("tools/call", name="get_weather"))
    headers = [("Mcp-Method", "tools/call"), ("MCP-NAME", "get_weather")]
    assert check_mcp_headers(headers, rpc) is None


def test_absent_headers_pass_for_earlier_protocol_eras() -> None:
    assert check_mcp_headers([], parse_request(_rpc("tools/call", name="t"))) is None


def test_method_mismatch_is_reported_without_values() -> None:
    rpc = parse_request(_rpc("tools/call", name="t"))
    message = check_mcp_headers([("mcp-method", "tools/list")], rpc)
    assert message is not None
    assert "Mcp-Method" in message
    assert "tools/list" not in message


@pytest.mark.parametrize(
    ("method", "params", "header"),
    [
        ("tools/call", {"name": "real"}, "other"),
        ("prompts/get", {"name": "real"}, "other"),
        ("resources/read", {"uri": "file:///real"}, "file:///other"),
        ("tools/call", {"name": "real"}, "=?base64?b3RoZXI=?="),
        ("tools/call", {"name": "real"}, "=?base64?%%%?="),
        ("tools/call", {}, "anything"),
    ],
)
def test_name_mismatch_is_reported(method: str, params: dict[str, str], header: str) -> None:
    rpc = parse_request(_rpc(method, **params))
    message = check_mcp_headers([("mcp-method", method), ("mcp-name", header)], rpc)
    assert message is not None
    assert "Mcp-Name" in message


def test_base64_sentinel_is_decoded_before_comparison() -> None:
    rpc = parse_request(_rpc("tools/call", name="Hello, 世界"))
    encoded = "=?base64?SGVsbG8sIOS4lueVjA==?="
    assert check_mcp_headers([("mcp-name", encoded)], rpc) is None


def test_duplicate_headers_are_a_mismatch() -> None:
    rpc = parse_request(_rpc("tools/call", name="t"))
    assert check_mcp_headers([("mcp-method", "tools/call"), ("mcp-method", "tools/call")], rpc)


def test_name_header_on_a_method_without_a_name_is_ignored() -> None:
    assert check_mcp_headers([("mcp-name", "x")], parse_request(_rpc("tools/list"))) is None


def test_non_rpc_bodies_are_not_inspected() -> None:
    assert check_mcp_headers([("mcp-method", "tools/list")], NO_RPC) is None


@pytest.mark.parametrize(
    "body",
    [
        b'{"jsonrpc":"2.0","id":3,"result":{}}',
        b'{"jsonrpc":"2.0","id":"a","error":{"code":-1,"message":"x"}}',
    ],
)
def test_a_client_response_is_not_a_request_that_expects_a_reply(body: bytes) -> None:
    info = parse_request(body)
    assert info.method is None
    assert info.expects_reply is False


DEEP = b"[" * 10_000 + b"]" * 10_000
# Readable by CPython 3.14 and refused by 3.12: only the gateway's own limit gives one answer.
MID_DEEP = b"[" * 500 + b"]" * 500


def test_a_deeply_nested_body_is_unreadable_not_a_crash() -> None:
    assert parse_request(DEEP) == TOO_DEEP


def test_a_body_nested_past_the_gateway_limit_is_unreadable_on_every_python() -> None:
    assert parse_request(MID_DEEP) == TOO_DEEP


def test_a_too_deep_body_is_unparsable_with_its_own_flag() -> None:
    assert TOO_DEEP.kind is BodyKind.UNPARSABLE and TOO_DEEP.too_deep
    assert not UNPARSABLE.too_deep


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (b'{"id":1.0,"method":"x"}', True),
        (b'{"id":null,"method":"x"}', True),
        (b'{"id":true,"method":"x"}', True),
        (b'{"id":1,"method":"x"}', False),
        (b'{"id":"a","method":"x"}', False),
        (b'{"method":"x"}', False),
        (b'{"method":"tools/list"}', True),
        (b'{"method":"tools/call","params":{"name":"x"}}', True),
        (b'{"id":1.0,"result":{}}', False),
        (b'[{"id":1,"method":"x"},{"id":[],"method":"x"}]', True),
        (b'[{"id":1,"method":"x"}]', False),
    ],
)
def test_a_request_with_an_id_that_is_not_a_string_or_integer_is_flagged(
    body: bytes, expected: bool
) -> None:
    assert parse_request(body).invalid_id is expected
