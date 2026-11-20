import json
import re

import pytest

from mcp_gateway import sse as sse_module
from mcp_gateway.sse import (
    EventTooLargeError,
    SseEventSplitter,
    TruncatedEventError,
    data_of,
    is_reply_to,
    stream_failed_event,
)

REPLY = b'event: message\ndata: {"jsonrpc":"2.0","id":7,"result":{}}\n\n'


def drain(splitter: SseEventSplitter, chunks: list[bytes]) -> list[bytes]:
    events: list[bytes] = []
    for chunk in chunks:
        events.extend(splitter.feed(chunk))
    return events


def test_whole_events_only_split_across_arbitrary_chunks() -> None:
    stream = b"id: 1\ndata: a\n\n: keep-alive\n\nid: 2\ndata: b\n\n"
    for size in (1, 2, 3, 7, len(stream)):
        splitter = SseEventSplitter(1024, None)
        chunks = [stream[i : i + size] for i in range(0, len(stream), size)]
        events = drain(splitter, chunks)
        assert events == [b"id: 1\ndata: a\n\n", b": keep-alive\n\n", b"id: 2\ndata: b\n\n"]
        assert list(splitter.finish()) == []


@pytest.mark.parametrize("terminator", [b"\r\n", b"\r", b"\n"])
def test_every_line_ending_is_recognised(terminator: bytes) -> None:
    event = b"data: x" + terminator + terminator
    splitter = SseEventSplitter(1024, None)
    events = drain(splitter, [event[:-1], event[-1:]])
    # A trailing lone CR could still become CRLF, so it is released when the upstream ends.
    assert events + list(splitter.finish()) == [event]


def test_crlf_split_between_chunks_is_not_a_blank_line() -> None:
    splitter = SseEventSplitter(1024, None)
    # "data: x\r" then "\n" is one line break, so the event is not finished yet.
    assert drain(splitter, [b"data: x\r"]) == []
    assert drain(splitter, [b"\n"]) == []
    assert drain(splitter, [b"\r\n"]) == [b"data: x\r\n\r\n"]


def test_partial_event_is_held_back() -> None:
    splitter = SseEventSplitter(1024, None)
    assert drain(splitter, [b"data: half"]) == []
    with pytest.raises(TruncatedEventError):
        assert list(splitter.finish()) == []


def test_clean_end_after_whole_events_is_fine() -> None:
    splitter = SseEventSplitter(1024, None)
    drain(splitter, [b"data: a\n\n"])
    assert list(splitter.finish()) == []


def test_event_over_the_cap_is_refused_even_when_incomplete() -> None:
    splitter = SseEventSplitter(16, None)
    with pytest.raises(EventTooLargeError):
        drain(splitter, [b"data: " + b"x" * 32])


def test_complete_event_over_the_cap_is_refused() -> None:
    splitter = SseEventSplitter(16, None)
    with pytest.raises(EventTooLargeError):
        drain(splitter, [b"data: " + b"x" * 32 + b"\n\n"])


def test_reply_tracking_follows_the_request_id() -> None:
    splitter = SseEventSplitter(1024, 7)
    progress = b'data: {"jsonrpc":"2.0","method":"notifications/progress"}\n\n'
    drain(splitter, [progress])
    assert not splitter.reply_relayed
    assert splitter.can_report_failure
    drain(splitter, [REPLY])
    assert splitter.reply_relayed
    assert not splitter.can_report_failure


def test_no_request_id_means_no_failure_report() -> None:
    splitter = SseEventSplitter(1024, None)
    assert not splitter.can_report_failure


@pytest.mark.parametrize(
    ("event", "request_id", "expected"),
    [
        (b'data: {"jsonrpc":"2.0","id":7,"result":{}}\n\n', 7, True),
        (b'data: {"jsonrpc":"2.0","id":7,"error":{"code":1,"message":"m"}}\n\n', 7, True),
        (b'data: {"jsonrpc":"2.0","id":"7","result":{}}\n\n', 7, False),
        (b'data: {"jsonrpc":"2.0","id":8,"result":{}}\n\n', 7, False),
        (b'data: {"jsonrpc":"2.0","method":"x","params":{"result":1}}\n\n', 7, False),
        (b'data: {"jsonrpc":"2.0","id":7,\ndata: "result":{}}\n\n', 7, True),
        (b"data: not json result\n\n", 7, False),
        (b'data: {"jsonrpc":"2.0","id":true,"result":{}}\n\n', 1, False),
    ],
)
def test_is_reply_to(event: bytes, request_id: int, expected: bool) -> None:
    assert is_reply_to(event, request_id) is expected


def test_failure_event_is_a_whole_jsonrpc_error_message_event() -> None:
    event = stream_failed_event(7)
    assert event.startswith(b"event: message\ndata: ")
    assert event.endswith(b"\n\n")
    body = json.loads(event.split(b"data: ", 1)[1])
    assert body == {
        "jsonrpc": "2.0",
        "id": 7,
        "error": {
            "code": -32006,
            "message": "The service stream failed before the response was complete.",
        },
    }


class CountingPattern:
    """Stands in for the line-break regex and counts how often the buffer is scanned."""

    def __init__(self, inner: re.Pattern[bytes]) -> None:
        self._inner = inner
        self.searches = 0

    def search(self, buffer: bytes | bytearray, start: int = 0) -> re.Match[bytes] | None:
        self.searches += 1
        return self._inner.search(buffer, start)


def test_scanning_a_long_event_is_linear_not_quadratic(monkeypatch: pytest.MonkeyPatch) -> None:
    counter = CountingPattern(sse_module._LINE_BREAK)
    monkeypatch.setattr(sse_module, "_LINE_BREAK", counter)
    lines = 2000
    stream = b"data: x\n" * lines + b"\n"
    splitter = SseEventSplitter(len(stream) + 1, None)
    # Chunks that end mid-line, so every feed has an unterminated tail to resume from.
    chunks = [stream[i : i + 11] for i in range(0, len(stream), 11)]
    events = drain(splitter, chunks)
    assert events == [stream]
    assert counter.searches < 3 * (lines + len(chunks))


def test_mixed_line_endings_split_the_same_at_every_chunk_size() -> None:
    stream = b"id: 1\r\ndata: a\r\n\r\nid: 2\rdata: b\r\r: c\n\nid: 3\ndata: d\n\n"
    expected = [
        b"id: 1\r\ndata: a\r\n\r\n",
        b"id: 2\rdata: b\r\r",
        b": c\n\n",
        b"id: 3\ndata: d\n\n",
    ]
    for size in (1, 2, 3, 5, len(stream)):
        splitter = SseEventSplitter(1024, None)
        chunks = [stream[i : i + size] for i in range(0, len(stream), size)]
        events = drain(splitter, chunks)
        events.extend(splitter.finish())
        assert events == expected, size


class _CountingLineBreak:
    """Stands in for the line-break pattern and counts the bytes every search looks at."""

    def __init__(self, real: re.Pattern[bytes]) -> None:
        self._real = real
        self.scanned = 0

    def search(self, buffer: bytes | bytearray, start: int) -> re.Match[bytes] | None:
        self.scanned += len(buffer) - start
        return self._real.search(buffer, start)


def test_one_long_unterminated_line_is_scanned_once_not_per_chunk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    counter = _CountingLineBreak(sse_module._LINE_BREAK)
    monkeypatch.setattr(sse_module, "_LINE_BREAK", counter)
    size = 8192
    stream = b"data: " + b"x" * size + b"\n\n"
    splitter = SseEventSplitter(1024 * 1024, None)
    events = drain(splitter, [stream[i : i + 1] for i in range(len(stream))])
    assert events == [stream]
    # A rescan from the line start would look at ~size**2 / 2 (33 million) bytes.
    assert counter.scanned < 4 * len(stream)


def test_a_byte_order_mark_does_not_hide_the_reply_from_the_tracking() -> None:
    splitter = SseEventSplitter(1024, 5)
    reply = b'\xef\xbb\xbfdata: {"jsonrpc":"2.0","id":5,"result":{}}\n\n'
    assert list(splitter.feed(reply)) == [reply]
    assert splitter.reply_relayed is True


def test_a_data_line_without_a_colon_is_an_empty_field_value() -> None:
    assert data_of(b"data\ndata: {}\n\n") == b"\n{}"


def test_a_deeply_nested_event_is_not_a_reply_and_does_not_crash_the_relay() -> None:
    nested = b'data: {"result":' + b"[" * 100_000 + b"]" * 100_000 + b"}\n\n"
    splitter = SseEventSplitter(1 << 22, 5)
    assert list(splitter.feed(nested)) == [nested]
    assert splitter.reply_relayed is False
