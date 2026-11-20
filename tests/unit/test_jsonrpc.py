import json

import pytest

from mcp_gateway.jsonrpc import (
    NO_RPC,
    check_mcp_headers,
    error_body,
    is_tool_batch,
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


@pytest.mark.parametrize("body", [b"", b"not json", b"\xff\xfe", b"[1,2]", b'"str"', b"12"])
def test_non_request_bodies_yield_unknown(body: bytes) -> None:
    assert parse_request(body) == NO_RPC


def test_boolean_id_is_not_an_id() -> None:
    assert parse_request(b'{"id": true, "method": "x"}').id is None


def test_batch_is_not_a_single_request() -> None:
    assert parse_request(b'[{"id":1,"method":"a"}]').expects_reply is False


def test_error_body_shape() -> None:
    assert error_body(-32001, "m", 3, {"upstream_status": 401}) == {
        "jsonrpc": "2.0",
        "id": 3,
        "error": {"code": -32001, "message": "m", "data": {"upstream_status": 401}},
    }
    assert "data" not in error_body(-32001, "m", None)["error"]


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


def test_a_deeply_nested_body_is_unreadable_not_a_crash() -> None:
    assert parse_request(DEEP) == NO_RPC
    assert is_tool_batch(DEEP) is False
