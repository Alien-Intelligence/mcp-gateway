import pytest
from pydantic import SecretStr

from mcp_gateway.headers import (
    FORWARD_REQUEST_HEADERS,
    MUST_STRIP_REQUEST_HEADERS,
    build_upstream_headers,
    filter_response_headers,
)

KEY = SecretStr("fake-alien-key-unit_test_key")


def _as_dict(headers: list[tuple[str, str]]) -> dict[str, str]:
    return dict(headers)


def test_allow_list_and_strip_list_do_not_overlap() -> None:
    assert not FORWARD_REQUEST_HEADERS & MUST_STRIP_REQUEST_HEADERS


@pytest.mark.parametrize("name", sorted(MUST_STRIP_REQUEST_HEADERS))
def test_every_must_strip_header_is_dropped(name: str) -> None:
    out = build_upstream_headers([(name, "forged")], KEY)
    assert all(value != "forged" for _, value in out)
    # Mixed case must not bypass the policy.
    out = build_upstream_headers([(name.upper(), "forged")], KEY)
    assert all(value != "forged" for _, value in out)


def test_client_authorization_is_replaced_by_the_injected_key() -> None:
    out = build_upstream_headers([("Authorization", "Bearer client-token")], KEY)
    auth = [value for name, value in out if name == "authorization"]
    assert auth == ["Bearer fake-alien-key-unit_test_key"]


def test_unknown_headers_are_dropped() -> None:
    out = build_upstream_headers([("x-anything-else", "v"), ("x-custom", "v")], KEY)
    assert {name for name, _ in out} == {"accept-encoding", "authorization"}


def test_allow_listed_headers_pass_with_original_values() -> None:
    inbound = [
        ("Accept", "application/json, text/event-stream"),
        ("Content-Type", "application/json"),
        ("Mcp-Session-Id", "abc"),
        ("MCP-Protocol-Version", "2025-06-18"),
        ("Last-Event-ID", "42"),
        ("Mcp-Method", "tools/call"),
        ("Mcp-Name", "search"),
        ("Mcp-Param-Query", "x"),
        ("User-Agent", "client/1.0"),
    ]
    out = _as_dict(build_upstream_headers(inbound, KEY))
    for name, value in inbound:
        assert out[name.lower()] == value


def test_accept_encoding_defaults_to_identity_and_is_forwarded_when_present() -> None:
    assert _as_dict(build_upstream_headers([], KEY))["accept-encoding"] == "identity"
    out = _as_dict(build_upstream_headers([("Accept-Encoding", "gzip")], KEY))
    assert out["accept-encoding"] == "gzip"


def test_repeated_allowed_headers_are_kept() -> None:
    out = build_upstream_headers([("accept", "a"), ("accept", "b")], KEY)
    assert [v for n, v in out if n == "accept"] == ["a", "b"]


def test_response_strips_hop_by_hop_challenge_and_cookies() -> None:
    upstream = [
        ("Connection", "keep-alive, x-private"),
        ("Keep-Alive", "timeout=5"),
        ("Transfer-Encoding", "chunked"),
        ("TE", "trailers"),
        ("Trailer", "x"),
        ("Upgrade", "h2c"),
        ("Proxy-Authenticate", "Basic"),
        ("Proxy-Connection", "keep-alive"),
        ("WWW-Authenticate", 'Bearer resource_metadata="https://internal.example/x"'),
        ("Set-Cookie", "sid=1"),
        ("X-Private", "named by Connection"),
    ]
    out = filter_response_headers(upstream)
    assert set(out) == {"x-accel-buffering"}


def test_response_keeps_protocol_headers_and_disables_buffering() -> None:
    upstream = [
        ("Content-Type", "text/event-stream"),
        ("Content-Encoding", "gzip"),
        ("Content-Length", "12"),
        ("Mcp-Session-Id", "s1"),
        ("Mcp-Protocol-Version", "2025-06-18"),
        ("Cache-Control", "no-cache"),
    ]
    out = filter_response_headers(upstream)
    assert out["content-type"] == "text/event-stream"
    assert out["content-encoding"] == "gzip"
    assert out["content-length"] == "12"
    assert out["mcp-session-id"] == "s1"
    assert out["mcp-protocol-version"] == "2025-06-18"
    assert out["cache-control"] == "no-cache"
    assert out["x-accel-buffering"] == "no"


def test_response_buffering_header_is_overridden_not_trusted() -> None:
    assert filter_response_headers([("X-Accel-Buffering", "yes")])["x-accel-buffering"] == "no"
