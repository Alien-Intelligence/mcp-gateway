"""Minimal JSON-RPC helpers: request metadata for audit/errors and error bodies."""

import base64
import binascii
import json
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

# Gateway-originated JSON-RPC error codes (server-defined range).
UPSTREAM_UNAUTHORIZED = -32001
UPSTREAM_PAYMENT_REQUIRED = -32002
UPSTREAM_UNREACHABLE = -32003
UPSTREAM_TIMEOUT = -32004
GATEWAY_ERROR = -32005
UPSTREAM_STREAM_FAILED = -32006
SESSION_NOT_FOUND = -32007
ORIGIN_REFUSED = -32008
RESOLVER_UNAVAILABLE = -32009
NO_MAPPING = -32010
AMBIGUOUS_MAPPING = -32011
DENIED = -32012
STATE_UNAVAILABLE = -32013
# Allocated by the MCP 2026-07-28 transport spec for a header that disagrees with the body.
HEADER_MISMATCH = -32020
INVALID_REQUEST = -32600
INVALID_PARAMS = -32602

DENY_CODES: dict[str, int] = {
    "no_mapping": NO_MAPPING,
    "ambiguous_mapping": AMBIGUOUS_MAPPING,
}


# Methods whose ``Mcp-Name`` header mirrors a body field (MCP 2026-07-28, Standard Request Headers).
_NAMED_BY_FIELD = {"tools/call": "name", "prompts/get": "name", "resources/read": "uri"}

_BASE64_PREFIX = "=?base64?"
_BASE64_SUFFIX = "?="


@dataclass(frozen=True)
class RpcInfo:
    """What the gateway reads from a request body; never the arguments."""

    method: str | None
    id: str | int | None
    tool: str | None
    # What ``Mcp-Name`` mirrors: ``params.name`` (tools/call, prompts/get) or ``params.uri``
    # (resources/read). None for other methods or when the body lacks it.
    target: str | None
    # True only for a single JSON-RPC request carrying an id (a reply can be correlated).
    expects_reply: bool


NO_RPC = RpcInfo(method=None, id=None, tool=None, target=None, expects_reply=False)


def parse_request(body: bytes) -> RpcInfo:
    """Extract id/method/tool. A non-JSON or unexpected body yields ``NO_RPC``."""
    if not body:
        return NO_RPC
    try:
        document: Any = json.loads(body)
    except ValueError:
        # Not JSON (or not UTF-8): forwarded untouched, method unknown.
        return NO_RPC
    if not isinstance(document, dict):
        return NO_RPC
    method = document.get("method")
    method = method if isinstance(method, str) else None
    raw_id = document.get("id")
    rpc_id: str | int | None = None
    if isinstance(raw_id, str) or isinstance(raw_id, int) and not isinstance(raw_id, bool):
        rpc_id = raw_id
    params = document.get("params")
    target: str | None = None
    if isinstance(params, dict) and method in _NAMED_BY_FIELD:
        value = params.get(_NAMED_BY_FIELD[method])
        target = value if isinstance(value, str) else None
    tool = target if method == "tools/call" else None
    return RpcInfo(
        method=method, id=rpc_id, tool=tool, target=target, expects_reply=rpc_id is not None
    )


# Methods whose answer or arguments the tool policy governs.
TOOL_METHODS = frozenset({"tools/call", "tools/list"})


def is_tool_batch(body: bytes) -> bool:
    """True for a JSON array body holding any ``tools/call`` or ``tools/list`` element.

    ``parse_request`` reads single objects only, so a batch is invisible to it; the tool
    policy needs to know when one hides a tool request.
    """
    try:
        document: Any = json.loads(body)
    except ValueError:
        return False
    return isinstance(document, list) and any(
        isinstance(element, dict) and element.get("method") in TOOL_METHODS for element in document
    )


def error_body(
    code: int, message: str, rpc_id: str | int | None, data: dict[str, Any] | None = None
) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": rpc_id, "error": error}


def _decode_header_value(value: str) -> str | None:
    """Undo the ``=?base64?...?=`` sentinel encoding; None when the payload is not valid."""
    if not (value.startswith(_BASE64_PREFIX) and value.endswith(_BASE64_SUFFIX)):
        return value
    payload = value[len(_BASE64_PREFIX) : -len(_BASE64_SUFFIX)]
    try:
        return base64.b64decode(payload, validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        return None


def check_mcp_headers(headers: Iterable[tuple[str, str]], rpc: RpcInfo) -> str | None:
    """Mirror check for ``Mcp-Method`` / ``Mcp-Name`` against the parsed body.

    Returns a message naming the offending header (never its value) or None when consistent.
    Only headers that are present are compared, because clients of earlier protocol eras do not
    send them; a body that is not a JSON-RPC request is not inspected.
    """
    if rpc.method is None:
        return None
    supplied = [(name.lower(), value) for name, value in headers]
    methods = [value for name, value in supplied if name == "mcp-method"]
    names = [value for name, value in supplied if name == "mcp-name"]
    if len(methods) > 1 or len(names) > 1:
        return "Header mismatch: Mcp-Method and Mcp-Name must each appear once."
    if methods and methods[0] != rpc.method:
        return "Header mismatch: Mcp-Method does not match the request body."
    if names and rpc.method in _NAMED_BY_FIELD:
        decoded = _decode_header_value(names[0])
        if decoded is None or decoded != rpc.target:
            return "Header mismatch: Mcp-Name does not match the request body."
    return None
