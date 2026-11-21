import json
from typing import Any, cast

import pytest

from mcp_gateway.boundedjson import MAX_DEPTH, JsonTooDeepError, loads


def nested(depth: int) -> str:
    return "[" * depth + "]" * depth


def test_a_document_at_the_limit_is_read() -> None:
    assert loads(nested(MAX_DEPTH)) == json.loads(nested(MAX_DEPTH))


def test_a_document_one_level_past_the_limit_is_refused() -> None:
    with pytest.raises(JsonTooDeepError):
        loads(nested(MAX_DEPTH + 1))


def test_bytes_are_refused_because_json_would_guess_their_encoding() -> None:
    # json.loads sniffs UTF-16/32 in bytes, so a depth check run on the raw bytes can be blind to
    # what the parser then reads. The caller decodes (strict UTF-8) and hands over text.
    with pytest.raises(TypeError):
        loads(cast(Any, nested(3).encode()))


@pytest.mark.parametrize("depth", [MAX_DEPTH + 1, 500, 10_000, 1_000_000])
def test_the_refusal_does_not_depend_on_what_the_interpreter_can_read(depth: int) -> None:
    # 500 and 10 000 levels parse natively on some Python versions and raise RecursionError on
    # others; the gateway gives one answer for all of them.
    with pytest.raises(JsonTooDeepError):
        loads(nested(depth))


def test_objects_count_as_levels_too() -> None:
    body = '{"a":' * (MAX_DEPTH + 1) + "1" + "}" * (MAX_DEPTH + 1)
    with pytest.raises(JsonTooDeepError):
        loads(body)


def test_brackets_inside_strings_are_not_levels() -> None:
    text = "[" * 5_000 + '\\"' + "{" * 5_000
    assert loads(json.dumps({"k": text})) == {"k": text}


def test_many_shallow_containers_are_not_deep() -> None:
    wide = json.dumps([[1] for _ in range(10_000)])
    assert len(loads(wide)) == 10_000


def test_too_deep_is_a_value_error_like_any_unreadable_json() -> None:
    assert issubclass(JsonTooDeepError, ValueError)
