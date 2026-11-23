"""Rewrites the one thing the gateway ever changes in a response: a filtered ``tools/list`` result.

Everything that edits upstream content lives here, apart from the relay mechanics. The gateway
only comes here while a tool policy is active.

Two rules shape the module. A response the gateway cannot filter is never relayed as it came:
``ToolPolicyError`` tells the caller to answer with the gateway error instead. And an upstream
JSON-RPC error is not a tool list, so it is relayed unchanged.
"""

import json
from typing import Any

from mcp_gateway import boundedjson
from mcp_gateway.jsonrpc import GATEWAY_ERROR, error_body
from mcp_gateway.sse import data_of, without_bom
from mcp_gateway.tool_policy import EffectivePolicy

POLICY_FAILED_MESSAGE = "The gateway could not apply its tool policy to this response."
_PRIVATE = "private"


class ToolPolicyError(Exception):
    """The response cannot be filtered. ``reason`` is a stable label, never content."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def policy_failed_body(request_id: str | int | None) -> dict[str, Any]:
    return error_body(GATEWAY_ERROR, POLICY_FAILED_MESSAGE, request_id)


def policy_failed_event(request_id: str | int | None) -> bytes:
    """The SSE event that takes the place of a reply the gateway could not filter."""
    return b"event: message\ndata: " + _serialize(policy_failed_body(request_id)) + b"\n\n"


def _serialize(document: dict[str, Any]) -> bytes:
    try:
        return json.dumps(document, separators=(",", ":"), allow_nan=False).encode()
    except ValueError as exc:
        raise ToolPolicyError("unserializable") from exc


def _is_response_to(document: dict[str, Any], request_id: str | int) -> bool:
    if "result" not in document and "error" not in document:
        return False
    reply_id = document.get("id")
    return type(reply_id) is type(request_id) and reply_id == request_id


def _filtered(
    document: dict[str, Any], policy: EffectivePolicy, *, tools_required: bool
) -> dict[str, Any] | None:
    """The document with its tools filtered; None when there is nothing to filter.

    An error response, and (unless ``tools_required``) a result without a tool list, are not
    tool lists and are relayed unchanged. ``tools_required`` is set for the reply to a
    ``tools/list`` request: that one must carry a list.
    """
    if "result" not in document:
        return None
    result = document["result"]
    tools = result.get("tools") if isinstance(result, dict) else None
    if not isinstance(tools, list):
        if tools_required:
            raise ToolPolicyError("unexpected_shape")
        return None
    result["tools"] = policy.filter_tools(tools)
    # A filtered list varies per caller. Only a result that declares a scope is touched: an
    # older protocol era has no such field and must not gain one.
    if "cacheScope" in result:
        result["cacheScope"] = _PRIVATE
    return document


def filter_json_reply(body: bytes, request_id: str | int, policy: EffectivePolicy) -> bytes:
    """The body to send for the ``application/json`` reply to a ``tools/list``."""
    try:
        # Strict UTF-8, as the request side reads it: ``json.loads`` on bytes would accept UTF-16.
        document: Any = boundedjson.loads(body.decode("utf-8"))
    except boundedjson.JsonTooDeepError as exc:
        raise ToolPolicyError("too_deep") from exc
    except ValueError as exc:
        raise ToolPolicyError("unparsable") from exc
    if not isinstance(document, dict):
        raise ToolPolicyError("unexpected_shape")
    if not _is_response_to(document, request_id):
        raise ToolPolicyError("unexpected_reply")
    filtered = _filtered(document, policy, tools_required=True)
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


def _is_response(document: dict[str, Any]) -> bool:
    """A JSON-RPC response (a result or an error), as opposed to a request or notification."""
    return ("result" in document or "error" in document) and "method" not in document


class SseListFilter:
    """Filters every tool list a stream carries; everything else passes byte for byte.

    A stream is not trusted to say which event answers which request: a reply may come under
    another id, twice, or on a stream resumed with ``Last-Event-ID``. So every response-shaped
    event is looked at, whatever its id. An event whose data the gateway cannot read could hide
    a tool list, so it is replaced like an unfilterable reply.
    """

    def __init__(
        self,
        request_id: str | int | None,
        policy: EffectivePolicy,
        max_event_bytes: int,
        *,
        answers_tools_list: bool,
    ) -> None:
        self._request_id = request_id
        self._policy = policy
        self._max = max_event_bytes
        # True for the stream of a ``tools/list`` request: its reply must carry a tool list.
        self._answers_tools_list = answers_tools_list
        self._first_event = True

    @property
    def request_id(self) -> str | int | None:
        """The id the error event answers: the request's, else null."""
        return self._request_id

    def apply(self, event: bytes) -> bytes:
        """The event to relay. Raises ``ToolPolicyError`` when it cannot be read or filtered."""
        readable = without_bom(event) if self._first_event else event
        self._first_event = False
        data = data_of(readable)
        if not data.strip():
            # A keep-alive, a comment or a priming event: no client reads a message from it.
            return event
        document = self._parse(data)
        if isinstance(document, list):
            # A batched reply could hide the tool list from the checks below.
            raise ToolPolicyError("batched_reply")
        if not isinstance(document, dict) or not _is_response(document):
            return event
        is_reply = self._request_id is not None and _is_response_to(document, self._request_id)
        filtered = _filtered(
            document, self._policy, tools_required=self._answers_tools_list and is_reply
        )
        if filtered is None:
            return event
        rewritten = _with_data(readable, _serialize(filtered))
        if len(rewritten) > self._max:
            raise ToolPolicyError("event_too_large")
        return rewritten

    @staticmethod
    def _parse(data: bytes) -> Any:
        try:
            return boundedjson.loads(data.decode("utf-8"))
        except boundedjson.JsonTooDeepError as exc:
            raise ToolPolicyError("too_deep") from exc
        except ValueError as exc:
            # Not UTF-8, or not JSON.
            raise ToolPolicyError("unparsable") from exc
