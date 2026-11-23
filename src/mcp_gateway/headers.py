"""Header policy: pure functions, no I/O.

Inbound to upstream is an allow-list. Upstream to client passes through minus hop-by-hop
headers, the upstream OAuth challenge and cookies.
"""

from collections.abc import Iterable

from pydantic import SecretStr

FORWARD_REQUEST_HEADERS = frozenset(
    {
        "accept",
        "content-type",
        "mcp-session-id",
        "mcp-protocol-version",
        "last-event-id",
        "mcp-method",
        "mcp-name",
        "user-agent",
    }
)
FORWARD_REQUEST_PREFIXES = ("mcp-param-",)

# Never forwarded. The allow-list already drops them; this list is what tests assert on.
MUST_STRIP_REQUEST_HEADERS = frozenset(
    {
        "authorization",
        "proxy-authorization",
        "cookie",
        "x-oauth-access-token",
        "x-job-id",
        "x-organization-id",
        "x-mcp-config-id",
        "x-request-id",
        "x-source-type",
        "x-mcp-session-id",
        "x-forwarded-for",
        "x-forwarded-host",
        "x-forwarded-proto",
        "x-forwarded-port",
        "x-forwarded-prefix",
        "forwarded",
        "x-real-ip",
        "host",
        "connection",
        "keep-alive",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
        "content-length",
    }
)

HOP_BY_HOP_RESPONSE_HEADERS = frozenset(
    {"connection", "keep-alive", "transfer-encoding", "te", "trailer", "upgrade"}
)
# Removed from every upstream response regardless of hop-by-hop rules. ``date`` and ``server``
# are set by the ASGI server that answers the client; relaying the upstream's too would send
# each of them twice, which proxies in front of the gateway reject or warn about.
ALWAYS_STRIPPED_RESPONSE_HEADERS = frozenset({"www-authenticate", "set-cookie", "date", "server"})


def _is_forwardable(name: str) -> bool:
    return name in FORWARD_REQUEST_HEADERS or name.startswith(FORWARD_REQUEST_PREFIXES)


def build_upstream_headers(
    inbound: Iterable[tuple[str, str]], api_key: SecretStr
) -> list[tuple[str, str]]:
    """Allow-listed client headers plus the injected Alien API key.

    ``accept-encoding`` is always ``identity``, whatever the client sent: the relay cuts SSE
    into whole events and reads the JSON-RPC ids inside them, which a compressed body would
    make impossible. The client's value is dropped, never forwarded.
    """
    headers = [(name.lower(), value) for name, value in inbound if _is_forwardable(name.lower())]
    headers.append(("accept-encoding", "identity"))
    headers.append(("authorization", f"Bearer {api_key.get_secret_value()}"))
    return headers


def has_content_encoding(upstream: Iterable[tuple[str, str]]) -> bool:
    """True when the response declares any encoding other than ``identity``."""
    return any(
        token.strip() not in ("", "identity")
        for name, value in upstream
        if name.lower() == "content-encoding"
        for token in value.lower().split(",")
    )


def filter_response_headers(upstream: Iterable[tuple[str, str]]) -> dict[str, str]:
    """Upstream response headers safe to hand to the client, plus ``x-accel-buffering``."""
    pairs = [(name.lower(), value) for name, value in upstream]
    # Tokens named by Connection are hop-by-hop for this hop too.
    named_by_connection = {
        token.strip()
        for name, value in pairs
        if name == "connection"
        for token in value.lower().split(",")
        if token.strip()
    }
    kept: dict[str, str] = {}
    for name, value in pairs:
        if (
            name in HOP_BY_HOP_RESPONSE_HEADERS
            or name in ALWAYS_STRIPPED_RESPONSE_HEADERS
            or name in named_by_connection
            or name.startswith("proxy-")
        ):
            continue
        kept[name] = f"{kept[name]}, {value}" if name in kept else value
    kept["x-accel-buffering"] = "no"
    return kept
