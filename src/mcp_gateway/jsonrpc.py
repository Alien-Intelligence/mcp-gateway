"""Minimal JSON-RPC helpers: request metadata for audit/errors and error bodies."""

import base64
import binascii
import codecs
import json
from collections.abc import Iterable
from dataclasses import dataclass
from email.message import Message
from enum import Enum
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
RATE_LIMITED = -32014
# Allocated by the MCP 2026-07-28 transport spec for a header that disagrees with the body.
HEADER_MISMATCH = -32020
PARSE_ERROR = -32700
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


class BodyKind(Enum):
    """What the request body turned out to be. Only ``UNPARSABLE`` means the gateway cannot check it."""

    EMPTY = "empty"
    OBJECT = "object"
    ARRAY = "array"
    # Not strict UTF-8 JSON, or JSON that is neither an object nor an array.
    UNPARSABLE = "unparsable"


@dataclass(frozen=True)
class RpcInfo:
    """What the gateway reads from a request body; never the arguments."""

    method: str | None
    id: str | int | None
    tool: str | None
    # What ``Mcp-Name`` mirrors: ``params.name`` (tools/call, prompts/get) or ``params.uri``
    # (resources/read). None for other methods or when the body lacks it.
    target: str | None
    kind: BodyKind
    # A JSON array holding any ``tools/call`` or ``tools/list`` element: the single-request
    # fields above cannot see inside a batch, so the tool policy needs to be told.
    tool_batch: bool = False
    # A request (an object with a ``method``) whose ``id`` key is present but is neither a string
    # nor an integer, or a ``tools/call`` / ``tools/list`` with no ``id`` at all. No reply can be
    # correlated with it, and the upstream may read it differently from the gateway (1.0 is 1
    # to some parsers), so a policy refuses it.
    invalid_id: bool = False

    @property
    def expects_reply(self) -> bool:
        """True only for a single JSON-RPC request carrying an id (a reply can be correlated).

        A response from the client has an id too, but nothing answers it.
        """
        return self.method is not None and self.id is not None


NO_RPC = RpcInfo(method=None, id=None, tool=None, target=None, kind=BodyKind.EMPTY)
UNPARSABLE = RpcInfo(method=None, id=None, tool=None, target=None, kind=BodyKind.UNPARSABLE)

# Methods whose answer or arguments the tool policy governs.
TOOL_METHODS = frozenset({"tools/call", "tools/list"})

_UNREADABLE = object()


def _decode(body: bytes) -> Any:
    """The JSON document of a strict UTF-8 body, else ``_UNREADABLE``.

    ``json.loads`` on bytes would also accept UTF-16/32 and a UTF-8 BOM; a body the
    gateway reads differently from the upstream is exactly what must never pass a check.
    """
    try:
        return json.loads(body.decode("utf-8"))
    except (ValueError, RecursionError):
        # Not UTF-8, not JSON, or nested past the interpreter's limit.
        return _UNREADABLE


def _valid_id(raw_id: Any) -> bool:
    return isinstance(raw_id, str) or (isinstance(raw_id, int) and not isinstance(raw_id, bool))


def _has_invalid_id(document: Any) -> bool:
    """A request whose id is unusable, or a tool request with none (those are never notifications)."""
    if not isinstance(document, dict) or "method" not in document:
        return False
    if "id" not in document:
        return document["method"] in TOOL_METHODS
    return not _valid_id(document["id"])


def _single(document: dict[str, Any]) -> RpcInfo:
    method = document.get("method")
    method = method if isinstance(method, str) else None
    raw_id = document.get("id")
    rpc_id: str | int | None = raw_id if _valid_id(raw_id) else None
    params = document.get("params")
    target: str | None = None
    if isinstance(params, dict) and method in _NAMED_BY_FIELD:
        value = params.get(_NAMED_BY_FIELD[method])
        target = value if isinstance(value, str) else None
    tool = target if method == "tools/call" else None
    return RpcInfo(
        method=method,
        id=rpc_id,
        tool=tool,
        target=target,
        kind=BodyKind.OBJECT,
        invalid_id=_has_invalid_id(document),
    )


def parse_request(body: bytes) -> RpcInfo:
    """Extract id/method/tool from a request body.

    An empty body is ``NO_RPC``; a body that is not strict UTF-8 JSON forming an object or an
    array is ``UNPARSABLE``. Without a tool policy both are forwarded untouched.
    """
    if not body:
        return NO_RPC
    document = _decode(body)
    if isinstance(document, dict):
        return _single(document)
    if isinstance(document, list):
        tool_batch = any(
            isinstance(element, dict) and element.get("method") in TOOL_METHODS
            for element in document
        )
        return RpcInfo(
            method=None,
            id=None,
            tool=None,
            target=None,
            kind=BodyKind.ARRAY,
            tool_batch=tool_batch,
            invalid_id=any(_has_invalid_id(element) for element in document),
        )
    return UNPARSABLE


def declares_non_utf8_charset(headers: Iterable[tuple[str, str]]) -> bool:
    """True when a ``Content-Type`` header names a charset other than UTF-8 (any spelling).

    A body in another encoding may carry a tool name the UTF-8 reading never sees. An absent
    charset is fine: JSON is UTF-8 by definition.
    """
    for name, value in headers:
        if name.lower() != "content-type":
            continue
        message = Message()
        message["content-type"] = value
        charset = message.get_content_charset()
        if charset is None:
            continue
        try:
            if codecs.lookup(charset).name != "utf-8":
                return True
        except LookupError:
            return True
    return False


def error_body(
    code: int, message: str, rpc_id: str | int | None, data: dict[str, Any] | None = None
) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": rpc_id, "error": error}


def tool_error_result(
    rpc_id: str | int, text: str, meta: dict[str, Any] | None = None
) -> dict[str, Any]:
    """A ``tools/call`` result flagged ``isError``: the one failure shape every client hands to the model."""
    result: dict[str, Any] = {"content": [{"type": "text", "text": text}], "isError": True}
    if meta is not None:
        result["_meta"] = meta
    return {"jsonrpc": "2.0", "id": rpc_id, "result": result}


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
