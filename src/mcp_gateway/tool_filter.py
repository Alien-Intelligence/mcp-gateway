"""Rewrites the one thing the gateway ever changes in a response: a filtered ``tools/list`` result.

Everything that edits upstream content lives here, apart from the relay mechanics. The gateway
only comes here while a tool policy is active and the request was a ``tools/list`` with an id.

Two rules shape the module. A response the gateway cannot filter is never relayed as it came:
``ToolPolicyError`` tells the caller to answer with the gateway error instead. And an upstream
JSON-RPC error is not a tool list, so it is relayed unchanged.
"""

import json
from typing import Any

from mcp_gateway.jsonrpc import GATEWAY_ERROR, error_body
from mcp_gateway.sse import data_of
from mcp_gateway.tool_policy import EffectivePolicy

POLICY_FAILED_MESSAGE = "The gateway could not apply its tool policy to this response."
_PRIVATE = "private"


class ToolPolicyError(Exception):
    """The response cannot be filtered. ``reason`` is a stable label, never content."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def policy_failed_body(request_id: str | int) -> dict[str, Any]:
    return error_body(GATEWAY_ERROR, POLICY_FAILED_MESSAGE, request_id)


def policy_failed_event(request_id: str | int) -> bytes:
    """The SSE event that takes the place of a reply the gateway could not filter."""
    return b"event: message\ndata: " + _serialize(policy_failed_body(request_id)) + b"\n\n"


def _serialize(document: dict[str, Any]) -> bytes:
    try:
        return json.dumps(document, separators=(",", ":"), allow_nan=False).encode()
    except (ValueError, RecursionError) as exc:
        raise ToolPolicyError("unserializable") from exc


def _is_response_to(document: dict[str, Any], request_id: str | int) -> bool:
    if "result" not in document and "error" not in document:
        return False
    reply_id = document.get("id")
    return type(reply_id) is type(request_id) and reply_id == request_id


def _filtered(document: dict[str, Any], policy: EffectivePolicy) -> dict[str, Any] | None:
    """The document with its tools filtered; None for an error response (relayed unchanged)."""
    if "result" not in document:
        return None
    result = document["result"]
    tools = result.get("tools") if isinstance(result, dict) else None
    if not isinstance(tools, list):
        raise ToolPolicyError("unexpected_shape")
    result["tools"] = policy.filter_tools(tools)
    # A filtered list varies per caller. Only a result that declares a scope is touched: an
    # older protocol era has no such field and must not gain one.
    if "cacheScope" in result:
        result["cacheScope"] = _PRIVATE
    return document


def filter_json_reply(body: bytes, request_id: str | int, policy: EffectivePolicy) -> bytes:
    """The body to send for the ``application/json`` reply to a ``tools/list``."""
    try:
        document: Any = json.loads(body)
    except (ValueError, RecursionError) as exc:
        raise ToolPolicyError("unparsable") from exc
    if not isinstance(document, dict):
        raise ToolPolicyError("unexpected_shape")
    if not _is_response_to(document, request_id):
        raise ToolPolicyError("unexpected_reply")
    filtered = _filtered(document, policy)
    return body if filtered is None else _serialize(filtered)


def _with_data(event: bytes, data: bytes) -> bytes:
    """The event with its ``data`` lines replaced by one; every other field is kept as sent."""
    lines = event.replace(b"\r\n", b"\n").replace(b"\r", b"\n").split(b"\n")
    out: list[bytes] = []
    placed = False
    for line in lines:
        if not line:
            continue
        if line.startswith(b"data:") or line == b"data":
            if not placed:
                out.append(b"data: " + data)
                placed = True
            continue
        out.append(line)
    return b"\n".join(out) + b"\n\n"


class SseReplyFilter:
    """Filters the event that carries the reply to one ``tools/list``; others pass untouched."""

    def __init__(
        self, request_id: str | int, policy: EffectivePolicy, max_event_bytes: int
    ) -> None:
        self._request_id = request_id
        self._policy = policy
        self._max = max_event_bytes
        self._answered = False

    @property
    def request_id(self) -> str | int:
        return self._request_id

    def apply(self, event: bytes) -> bytes:
        """The event to relay. Raises ``ToolPolicyError`` when it is the reply and unfilterable."""
        if self._answered:
            return event
        try:
            document: Any = json.loads(data_of(event))
        except (ValueError, RecursionError):
            # Not JSON: a keep-alive, a comment or a message no client can read as a reply.
            return event
        if isinstance(document, list):
            # A batched reply could hide the tool list from the id check below.
            raise ToolPolicyError("batched_reply")
        if not isinstance(document, dict) or not _is_response_to(document, self._request_id):
            return event
        self._answered = True
        filtered = _filtered(document, self._policy)
        if filtered is None:
            return event
        rewritten = _with_data(event, _serialize(filtered))
        if len(rewritten) > self._max:
            raise ToolPolicyError("event_too_large")
        return rewritten
