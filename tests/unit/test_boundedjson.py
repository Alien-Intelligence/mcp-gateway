import json

import pytest

from mcp_gateway.boundedjson import MAX_DEPTH, JsonTooDeepError, loads


def nested(depth: int) -> bytes:
    return b"[" * depth + b"]" * depth


@pytest.mark.parametrize("make", [lambda b: b, lambda b: b.decode()], ids=["bytes", "str"])
def test_a_document_at_the_limit_is_read(make) -> None:
    assert loads(make(nested(MAX_DEPTH))) == json.loads(nested(MAX_DEPTH))


@pytest.mark.parametrize("make", [lambda b: b, lambda b: b.decode()], ids=["bytes", "str"])
def test_a_document_one_level_past_the_limit_is_refused(make) -> None:
    with pytest.raises(JsonTooDeepError):
        loads(make(nested(MAX_DEPTH + 1)))


@pytest.mark.parametrize("depth", [MAX_DEPTH + 1, 500, 10_000, 1_000_000])
def test_the_refusal_does_not_depend_on_what_the_interpreter_can_read(depth: int) -> None:
    # 500 and 10 000 levels parse natively on some Python versions and raise RecursionError on
    # others; the gateway gives one answer for all of them.
    with pytest.raises(JsonTooDeepError):
        loads(nested(depth))


def test_objects_count_as_levels_too() -> None:
    body = b'{"a":' * (MAX_DEPTH + 1) + b"1" + b"}" * (MAX_DEPTH + 1)
    with pytest.raises(JsonTooDeepError):
        loads(body)


def test_brackets_inside_strings_are_not_levels() -> None:
    text = "[" * 5_000 + '\\"' + "{" * 5_000
    assert loads(json.dumps({"k": text})) == {"k": text}
    assert loads(json.dumps({"k": text}).encode()) == {"k": text}


def test_many_shallow_containers_are_not_deep() -> None:
    wide = json.dumps([[1] for _ in range(10_000)])
    assert len(loads(wide)) == 10_000


def test_too_deep_is_a_value_error_like_any_unreadable_json() -> None:
    assert issubclass(JsonTooDeepError, ValueError)
