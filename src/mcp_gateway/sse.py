"""Server-Sent Events framing for the relay: whole events only, bounded memory.

The splitter holds at most one event's bytes. Clients dispatch an event only at its closing
blank line, so releasing whole events costs nothing, and it lets the gateway discard a
half-sent event when the upstream dies instead of handing the client a corrupt one.
"""

import json
import re
from collections.abc import Iterator
from typing import Any

from mcp_gateway.jsonrpc import UPSTREAM_STREAM_FAILED, error_body


class SseError(Exception):
    """The upstream stream is unusable. ``reason`` is a stable label, never content."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class EventTooLargeError(SseError):
    def __init__(self) -> None:
        super().__init__("event_too_large")


class EncodedStreamError(SseError):
    """The upstream compressed an event stream although the gateway asked for identity."""

    def __init__(self) -> None:
        super().__init__("encoded_event_stream")


class TruncatedEventError(SseError):
    def __init__(self) -> None:
        super().__init__("truncated_event")


_LINE_BREAK = re.compile(rb"\r\n|\r|\n")


def _data_of(event: bytes) -> bytes:
    """The joined ``data`` field values of one event (SSE field parsing, simplified)."""
    values: list[bytes] = []
    for line in event.replace(b"\r\n", b"\n").replace(b"\r", b"\n").split(b"\n"):
        if line.startswith(b"data:"):
            value = line[5:]
            values.append(value[1:] if value.startswith(b" ") else value)
    return b"\n".join(values)


def is_reply_to(event: bytes, request_id: str | int) -> bool:
    """True when the event carries the JSON-RPC response (result or error) for ``request_id``."""
    data = _data_of(event)
    if b'"result"' not in data and b'"error"' not in data:
        return False
    try:
        document: Any = json.loads(data)
    except ValueError:
        return False
    if not isinstance(document, dict) or ("result" not in document and "error" not in document):
        return False
    reply_id = document.get("id")
    return type(reply_id) is type(request_id) and reply_id == request_id


class SseEventSplitter:
    """Feed raw chunks, get whole events back. Tracks whether the reply to ``request_id`` went out."""

    def __init__(self, max_event_bytes: int, request_id: str | int | None) -> None:
        self._max = max_event_bytes
        self._request_id = request_id
        self._buffer = bytearray()
        self.reply_relayed = False
        # Where the scan for the end of the pending event resumes: the start of its first
        # unterminated line, and whether it already holds a field line. Without it every chunk
        # would rescan the whole buffer (quadratic in the event size).
        self._scan_from = 0
        self._scan_has_field = False
        # Where the search for a line break resumes inside that unterminated line, so a long
        # line is searched once however many chunks it arrives in.
        self._search_from = 0

    def feed(self, chunk: bytes) -> Iterator[bytes]:
        self._buffer.extend(chunk)
        yield from self._events(at_end=False)
        if len(self._buffer) > self._max:
            raise EventTooLargeError

    def finish(self) -> Iterator[bytes]:
        """Call when the upstream ended cleanly: trailing partial bytes mean it was cut."""
        yield from self._events(at_end=True)
        if self._buffer.strip(b"\r\n"):
            raise TruncatedEventError
        self._buffer.clear()

    def _event_end(self, *, at_end: bool) -> int | None:
        """Index just past the pending event (ending in a blank line), or None if incomplete."""
        position = self._scan_from
        has_field = self._scan_has_field
        while (located := self._line_end(position, at_end=at_end)) is not None:
            content_end, next_start = located
            if content_end == position:
                if has_field:
                    return next_start
            else:
                has_field = True
            position = next_start
            self._scan_from, self._scan_has_field = position, has_field
        return None

    def _line_end(self, start: int, *, at_end: bool) -> tuple[int, int] | None:
        """Locate the line starting at ``start``: ``(content end, next line start)``.

        None when the line is not terminated yet. A lone ``\\r`` at the very end of the buffer
        is ambiguous (it may be the first half of ``\\r\\n``) so it counts as unterminated,
        unless the upstream has ended (``at_end``). Only a CR-only server is delayed by this.
        """
        found = _LINE_BREAK.search(self._buffer, max(start, self._search_from))
        if found is None:
            # Nothing searched so far holds a break, a trailing CR included.
            self._search_from = len(self._buffer)
            return None
        if found.group() == b"\r" and found.end() == len(self._buffer) and not at_end:
            self._search_from = found.start()
            return None
        return found.start(), found.end()

    def _events(self, *, at_end: bool) -> Iterator[bytes]:
        while (end := self._event_end(at_end=at_end)) is not None:
            if end > self._max:
                raise EventTooLargeError
            event = bytes(self._buffer[:end])
            del self._buffer[:end]
            self._scan_from, self._scan_has_field = 0, False
            self._search_from = 0
            if (
                self._request_id is not None
                and not self.reply_relayed
                and is_reply_to(event, self._request_id)
            ):
                self.reply_relayed = True
            yield event

    @property
    def can_report_failure(self) -> bool:
        """A final JSON-RPC error event is meaningful only while the reply is still owed."""
        return self._request_id is not None and not self.reply_relayed


STREAM_FAILED_MESSAGE = "The service stream failed before the response was complete."


def stream_failed_event(request_id: str | int) -> bytes:
    body = error_body(UPSTREAM_STREAM_FAILED, STREAM_FAILED_MESSAGE, request_id)
    return b"event: message\ndata: " + json.dumps(body, separators=(",", ":")).encode() + b"\n\n"
