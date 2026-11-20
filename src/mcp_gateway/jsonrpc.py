"""Minimal JSON-RPC helpers: request metadata for audit/errors and error bodies."""

import json
from dataclasses import dataclass
from typing import Any

# Gateway-originated JSON-RPC error codes (server-defined range).
UPSTREAM_UNAUTHORIZED = -32001
UPSTREAM_PAYMENT_REQUIRED = -32002
UPSTREAM_UNREACHABLE = -32003
UPSTREAM_TIMEOUT = -32004
GATEWAY_ERROR = -32005
RESOLVER_UNAVAILABLE = -32006
NO_MAPPING = -32010
AMBIGUOUS_MAPPING = -32011
DENIED = -32012
INVALID_REQUEST = -32600

DENY_CODES: dict[str, int] = {
    "no_mapping": NO_MAPPING,
    "ambiguous_mapping": AMBIGUOUS_MAPPING,
}


@dataclass(frozen=True)
class RpcInfo:
    """What the gateway reads from a request body; never the arguments."""

    method: str | None
    id: str | int | None
    tool: str | None
    # True only for a single JSON-RPC request carrying an id (a reply can be correlated).
    expects_reply: bool


NO_RPC = RpcInfo(method=None, id=None, tool=None, expects_reply=False)


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
    tool: str | None = None
    if method == "tools/call":
        params = document.get("params")
        name = params.get("name") if isinstance(params, dict) else None
        tool = name if isinstance(name, str) else None
    return RpcInfo(method=method, id=rpc_id, tool=tool, expects_reply=rpc_id is not None)


def error_body(
    code: int, message: str, rpc_id: str | int | None, data: dict[str, Any] | None = None
) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": rpc_id, "error": error}
