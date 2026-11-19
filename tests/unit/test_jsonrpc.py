import json

import pytest

from mcp_gateway.jsonrpc import NO_RPC, error_body, parse_request


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
