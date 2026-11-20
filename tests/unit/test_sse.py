import json

import pytest

from mcp_gateway.sse import (
    EventTooLargeError,
    SseEventSplitter,
    TruncatedEventError,
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
