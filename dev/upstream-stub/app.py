"""Fault-injection upstream for the gateway's tests: a fake MCP server.

TEST FIXTURE ONLY. The ``/_stub/*`` control endpoints are unauthenticated: run this on
loopback or on a private compose network, never on a reachable address.

Endpoints
- ``/stateful/mcp``: a 2025-11-25 style server. ``initialize`` mints an ``Mcp-Session-Id`` bound to
  the calling key, every later request needs it (unknown, expired or foreign: 404), DELETE ends
  the session, GET opens an SSE stream of ids ``<session>-1..3`` that honours ``Last-Event-ID``
  and then stays idle.
- ``/stateless/mcp``: a 2026-07-28 style server. No sessions; GET and DELETE answer 405.
Keys come from a JSON file (``STUB_KEYS_FILE``): ``{"<key>": {"persona", "revoked",
"payment_required"}}``. A revoked key gets 401 with a ``WWW-Authenticate`` challenge, a key flagged
``payment_required`` gets 402. Raw keys never appear in anything the stub returns or records.

Tools (``tools/call`` by name)
- ``echo``: the headers received, with ``authorization`` replaced by ``persona:<name>``, and the
  arguments of the call.
- ``policy_denied_probe``: behaves like ``echo``; the tool the policy tests deny.
- ``slow_sse {count, delay_seconds, hold}``: SSE with ``count`` progress events spaced by the delay,
  then the reply; with ``hold`` it idles (no reply) until the client goes away.
- ``fail_midstream_sse {before, reply_first, declare_length, gzip}``: ``before`` whole events,
  optionally the reply, a partial event, then the TCP connection is dropped. ``declare_length``
  adds a ``Content-Length`` promising more than is sent; ``gzip`` sends the bytes gzip-encoded
  (with ``Content-Encoding: gzip``, and a declared length).
- ``fail_midstream_json``: ``Content-Length`` promises the whole body, the connection drops halfway.
- ``hang``: never answers; the client going away is recorded.
- ``status {status}``: answers that HTTP status (400 to 599).
``tools/list`` is paginated in two pages (``TOOLS_PAGE_SIZE`` tools, then the rest): a request
without ``params.cursor`` gets the first page and a ``nextCursor``, any request with one gets the
second. Each page carries ``"cacheScope": "public"`` and ``"ttlMs": 60000`` (the 2026-07-28 caching
fields, so a tool policy's rewrite of them can be seen). A request whose ``params._meta`` has
``"stub/sse": true`` gets the list as a one-event SSE stream instead of a JSON body (the gateway
forwards the body, not custom headers).
Control: ``GET /_stub/requests``, ``POST /_stub/reset``, ``POST /_stub/sessions/{id}/expire``.
"""

import json
import os
import secrets
import zlib
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import anyio
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

# No stream or wait in this fixture is unbounded: a forgotten client cannot pin a worker.
HOLD_LIMIT_SECONDS = 60.0
POLL_SECONDS = 0.05
MAX_RECORDED_REQUESTS = 10_000
SESSION_EVENTS = 3

TOOLS = [
    "echo",
    "policy_denied_probe",
    "slow_sse",
    "fail_midstream_sse",
    "fail_midstream_json",
    "hang",
    "status",
]
TOOLS_PAGE_SIZE = 3
SECOND_PAGE_CURSOR = "stub-page-2"
LIST_CACHE_TTL_MS = 60_000
SSE_REQUEST_FLAG = "stub/sse"
SSE_HEADERS = {"Content-Type": "text/event-stream", "Cache-Control": "no-cache"}


class ConnectionDroppedError(Exception):
    """Raised out of a response body so the ASGI server closes the connection mid-response."""


@dataclass(frozen=True)
class Key:
    secret: str
    persona: str
    revoked: bool
    payment_required: bool


@dataclass
class Session:
    key: str
    expired: bool = False


@dataclass
class Stub:
    keys: dict[str, Key]
    requests: list[dict[str, Any]] = field(default_factory=list)
    sessions: dict[str, Session] = field(default_factory=dict)

    def redact(self, value: str) -> str:
        for key in self.keys.values():
            value = value.replace(key.secret, f"<key:{key.persona}>")
        return value

    def record(self, request: Request, mode: str, persona: str | None) -> dict[str, Any]:
        headers = {name: self.redact(value) for name, value in request.headers.items()}
        if "authorization" in headers:
            headers["authorization"] = f"persona:{persona or 'unknown-key'}"
        entry: dict[str, Any] = {
            "seq": len(self.requests) + 1,
            "mode": mode,
            "method": request.method,
            "path": request.url.path,
            "headers": headers,
            "persona": persona,
            "rpc_method": None,
            "tool": None,
            "status": None,
            "disconnected": False,
        }
        if len(self.requests) < MAX_RECORDED_REQUESTS:
            self.requests.append(entry)
        return entry


def load_keys(path: Path) -> dict[str, Key]:
    raw = json.loads(path.read_text())
    keys: dict[str, Key] = {}
    for secret, flags in raw.items():
        persona, revoked, payment = (
            flags.get(n) for n in ("persona", "revoked", "payment_required")
        )
        if not (
            isinstance(persona, str) and isinstance(revoked, bool) and isinstance(payment, bool)
        ):
            raise ValueError(
                "every key needs a string persona and boolean revoked/payment_required"
            )
        keys[secret] = Key(secret, persona, revoked, payment)
    return keys


def rpc_result(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def rpc_error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def sse_event(payload: dict[str, Any] | None, event_id: str | None = None) -> bytes:
    lines = [f"id: {event_id}"] if event_id else []
    lines.append("event: message")
    lines.append("data: " + (json.dumps(payload, separators=(",", ":")) if payload else ""))
    return ("\n".join(lines) + "\n\n").encode()


def progress(step: int) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "method": "notifications/progress", "params": {"progress": step}}


def tool_text(payload: dict[str, Any]) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": json.dumps(payload)}], "isError": False}


async def idle_until_closed(entry: dict[str, Any]) -> None:
    """Hold the stream open, without sending, until the client goes away (bounded)."""
    with anyio.move_on_after(HOLD_LIMIT_SECONDS):
        await anyio.sleep_forever()
    entry["completed"] = True


def streaming(
    body: AsyncIterator[bytes], entry: dict[str, Any], **kwargs: Any
) -> StreamingResponse:
    """Stream ``body`` and record a disconnect when it is cancelled before finishing."""

    async def watched() -> AsyncIterator[bytes]:
        try:
            async for chunk in body:
                yield chunk
            entry["completed"] = True
        finally:
            if not entry.get("completed"):
                entry["disconnected"] = True

    return StreamingResponse(watched(), **kwargs)


class StubApp:
    def __init__(self, stub: Stub) -> None:
        self.stub = stub

    # --- auth --------------------------------------------------------------------------

    def _key_of(self, request: Request) -> Key | None:
        scheme, _, token = request.headers.get("authorization", "").partition(" ")
        return self.stub.keys.get(token.strip()) if scheme.lower() == "bearer" else None

    @staticmethod
    def _key_refusal(key: Key | None) -> Response | None:
        if key is None or key.revoked:
            return JSONResponse(
                {"error": "invalid_token"},
                status_code=401,
                headers={"WWW-Authenticate": 'Bearer realm="upstream-stub", error="invalid_token"'},
            )
        if key.payment_required:
            return JSONResponse({"error": "payment_required"}, status_code=402)
        return None

    # --- the MCP endpoint --------------------------------------------------------------

    async def mcp(self, request: Request) -> Response:
        mode = request.path_params["mode"]
        if mode not in ("stateful", "stateless"):
            return Response(status_code=404)
        key = self._key_of(request)
        entry = self.stub.record(request, mode, key.persona if key else None)
        response = await self._answer(request, mode, key, entry)
        entry["status"] = response.status_code
        return response

    async def _answer(
        self, request: Request, mode: str, key: Key | None, entry: dict[str, Any]
    ) -> Response:
        if (refusal := self._key_refusal(key)) is not None:
            return refusal
        assert key is not None
        stateful = mode == "stateful"
        if request.method == "DELETE":
            return self._delete(request, key) if stateful else _method_not_allowed()
        if request.method == "GET":
            if not stateful:
                return _method_not_allowed()
            return self._listen(request, key, entry)
        return await self._post(request, key, stateful, entry)

    def _session(self, request: Request, key: Key) -> Session | Response:
        session_id = request.headers.get("mcp-session-id")
        if session_id is None:
            return JSONResponse(rpc_error(None, -32600, "Missing session id"), status_code=400)
        session = self.stub.sessions.get(session_id)
        if session is None or session.expired or session.key != key.secret:
            return JSONResponse(rpc_error(None, -32001, "Session not found"), status_code=404)
        return session

    def _delete(self, request: Request, key: Key) -> Response:
        session = self._session(request, key)
        if isinstance(session, Response):
            return session
        session.expired = True
        return Response(status_code=200)

    def _listen(self, request: Request, key: Key, entry: dict[str, Any]) -> Response:
        session = self._session(request, key)
        if isinstance(session, Response):
            return session
        session_id = request.headers["mcp-session-id"]
        ids = [f"{session_id}-{n}" for n in range(1, SESSION_EVENTS + 1)]
        last = request.headers.get("last-event-id")
        replay = ids[ids.index(last) + 1 :] if last in ids else ids

        async def body() -> AsyncIterator[bytes]:
            for event_id in replay:
                yield sse_event(progress(ids.index(event_id) + 1), event_id)
            await idle_until_closed(entry)

        return streaming(body(), entry, headers=SSE_HEADERS)

    async def _post(
        self, request: Request, key: Key, stateful: bool, entry: dict[str, Any]
    ) -> Response:
        try:
            message = json.loads(await request.body())
        except ValueError:
            return JSONResponse(rpc_error(None, -32700, "Parse error"), status_code=400)
        if not isinstance(message, dict):
            return JSONResponse(rpc_error(None, -32600, "Invalid request"), status_code=400)
        method, request_id = message.get("method"), message.get("id")
        entry["rpc_method"] = method
        params = message.get("params")
        params = params if isinstance(params, dict) else {}

        headers: dict[str, str] = {}
        if method == "initialize" and stateful:
            session_id = secrets.token_hex(16)
            self.stub.sessions[session_id] = Session(key.secret)
            headers["Mcp-Session-Id"] = session_id
        elif stateful:
            session = self._session(request, key)
            if isinstance(session, Response):
                return session
        if request_id is None:
            return Response(status_code=202, headers=headers)
        if method == "initialize":
            result = {
                "protocolVersion": "2025-11-25",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "upstream-stub", "version": "0"},
            }
            return JSONResponse(rpc_result(request_id, result), headers=headers)
        if method == "tools/list":
            return self._tools_list(request_id, params, entry)
        if method == "tools/call":
            name = params.get("name")
            arguments = params.get("arguments")
            entry["tool"] = name if isinstance(name, str) else None
            return await self._call(
                request,
                key,
                stateful,
                entry,
                request_id,
                name,
                arguments if isinstance(arguments, dict) else {},
            )
        return JSONResponse(rpc_error(request_id, -32601, "Method not found"))

    @staticmethod
    def _tools_list(request_id: Any, params: dict[str, Any], entry: dict[str, Any]) -> Response:
        second_page = params.get("cursor") is not None
        names = TOOLS[TOOLS_PAGE_SIZE:] if second_page else TOOLS[:TOOLS_PAGE_SIZE]
        result: dict[str, Any] = {
            "tools": [{"name": n, "inputSchema": {"type": "object"}} for n in names],
            "cacheScope": "public",
            "ttlMs": LIST_CACHE_TTL_MS,
        }
        if not second_page:
            result["nextCursor"] = SECOND_PAGE_CURSOR
        meta = params.get("_meta")
        if isinstance(meta, dict) and meta.get(SSE_REQUEST_FLAG) is True:

            async def body() -> AsyncIterator[bytes]:
                yield sse_event(rpc_result(request_id, result))

            return streaming(body(), entry, headers=SSE_HEADERS)
        return JSONResponse(rpc_result(request_id, result))

    async def _call(
        self,
        request: Request,
        key: Key,
        stateful: bool,
        entry: dict[str, Any],
        request_id: Any,
        name: Any,
        arguments: dict[str, Any],
    ) -> Response:
        if name in ("echo", "policy_denied_probe"):
            echoed = {
                "mode": "stateful" if stateful else "stateless",
                "persona": key.persona,
                "headers": entry["headers"],
                "arguments": arguments,
            }
            return JSONResponse(rpc_result(request_id, tool_text(echoed)))
        if name == "slow_sse":
            return self._slow_sse(entry, request_id, arguments)
        if name == "fail_midstream_sse":
            return self._fail_sse(entry, request_id, arguments)
        if name == "fail_midstream_json":
            return self._fail_json(entry, request_id)
        if name == "hang":
            return await self._hang(request, entry)
        if name == "status":
            status = arguments.get("status")
            if not isinstance(status, int) or not 400 <= status <= 599:
                return JSONResponse(rpc_error(request_id, -32602, "status must be 400-599"))
            return JSONResponse({"error": "stub status"}, status_code=status)
        return JSONResponse(rpc_error(request_id, -32602, "Unknown tool"))

    @staticmethod
    def _slow_sse(entry: dict[str, Any], request_id: Any, arguments: dict[str, Any]) -> Response:
        count = int(arguments.get("count", 3))
        delay = float(arguments.get("delay_seconds", 0.05))
        hold = bool(arguments.get("hold", False))

        async def body() -> AsyncIterator[bytes]:
            for step in range(1, count + 1):
                yield sse_event(progress(step), str(step))
                await anyio.sleep(delay)
            if hold:
                await idle_until_closed(entry)
                return
            yield sse_event(rpc_result(request_id, tool_text({"events": count})))

        return streaming(body(), entry, headers=SSE_HEADERS)

    @staticmethod
    def _fail_sse(entry: dict[str, Any], request_id: Any, arguments: dict[str, Any]) -> Response:
        before = int(arguments.get("before", 1))
        reply_first = bool(arguments.get("reply_first", False))
        encoded = bool(arguments.get("gzip", False))
        declare_length = bool(arguments.get("declare_length", False)) or encoded
        parts = [sse_event(progress(step)) for step in range(1, before + 1)]
        if reply_first:
            parts.append(sse_event(rpc_result(request_id, tool_text({"events": before}))))
        parts.append(
            b'event: message\ndata: {"jsonrpc":"2.0","method":"notifications/progress","params":{"progress":'
        )
        headers = dict(SSE_HEADERS)
        if encoded:
            # A flushed but never finished gzip stream: a valid prefix of an encoded body.
            compressor = zlib.compressobj(wbits=31)
            parts = [compressor.compress(b"".join(parts)) + compressor.flush(zlib.Z_SYNC_FLUSH)]
            headers["Content-Encoding"] = "gzip"
        if declare_length:
            headers["Content-Length"] = str(sum(map(len, parts)) + 64)

        async def body() -> AsyncIterator[bytes]:
            for part in parts:
                yield part
            raise ConnectionDroppedError

        return streaming(body(), entry, headers=headers)

    @staticmethod
    def _fail_json(entry: dict[str, Any], request_id: Any) -> Response:
        full = json.dumps(rpc_result(request_id, tool_text({"padding": "x" * 200}))).encode()

        async def body() -> AsyncIterator[bytes]:
            yield full[: len(full) // 2]
            raise ConnectionDroppedError

        return streaming(
            body(),
            entry,
            headers={"Content-Type": "application/json", "Content-Length": str(len(full))},
        )

    @staticmethod
    async def _hang(request: Request, entry: dict[str, Any]) -> Response:
        with anyio.move_on_after(HOLD_LIMIT_SECONDS):
            while not await request.is_disconnected():
                await anyio.sleep(POLL_SECONDS)
            entry["disconnected"] = True
        return Response(status_code=504)

    # --- control -----------------------------------------------------------------------

    async def requests(self, request: Request) -> Response:
        return JSONResponse({"requests": self.stub.requests})

    async def reset(self, request: Request) -> Response:
        self.stub.requests.clear()
        self.stub.sessions.clear()
        return Response(status_code=204)

    async def expire(self, request: Request) -> Response:
        session = self.stub.sessions.get(request.path_params["session_id"])
        if session is None:
            return JSONResponse({"error": "unknown session"}, status_code=404)
        session.expired = True
        return Response(status_code=204)


def _method_not_allowed() -> Response:
    return Response("Session termination not supported", status_code=405, headers={"Allow": "POST"})


def create_app(keys_file: Path) -> Starlette:
    handlers = StubApp(Stub(load_keys(keys_file)))
    return Starlette(
        routes=[
            Route("/{mode:str}/mcp", handlers.mcp, methods=["GET", "POST", "DELETE"]),
            Route("/_stub/requests", handlers.requests, methods=["GET"]),
            Route("/_stub/reset", handlers.reset, methods=["POST"]),
            Route("/_stub/sessions/{session_id}/expire", handlers.expire, methods=["POST"]),
        ]
    )


def create_app_from_env() -> Starlette:
    return create_app(Path(os.environ["STUB_KEYS_FILE"]))
