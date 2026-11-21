"""JSON parsing with a nesting limit of our own.

``json.loads`` answers a deeply nested document with ``RecursionError`` on one interpreter and
with a document on the next: CPython 3.14 reads 10 000 levels that 3.12 refuses, and the limit
moves again between releases. The gateway reads request bodies and upstream replies it does not
control, so the answer must not depend on the interpreter: a document nested deeper than
``MAX_DEPTH`` is refused before it is parsed, the same on every version.
"""

import json
import re
from typing import Any

# Far above any real JSON-RPC message (a tool input schema nests a few dozen levels at most) and
# far below the depth at which any supported interpreter's own parser gives up.
MAX_DEPTH = 128

# A string literal (skipped, whatever brackets it holds) or one structural bracket.
_TOKEN = re.compile(r'"(?:[^"\\]|\\.)*+"|[\[\]{}]', re.DOTALL)


class JsonTooDeepError(ValueError):
    """The document nests deeper than ``MAX_DEPTH``. A ``ValueError`` like any other unreadable JSON."""


def _too_deep(data: str | bytes) -> bool:
    # Latin-1 maps every byte to one character and leaves ASCII alone; the bytes of a multi-byte
    # UTF-8 sequence are all >= 0x80, so none of them can pass for a quote, a backslash or a bracket.
    text = data if isinstance(data, str) else data.decode("latin-1")
    # Cheap exit for the usual body: it cannot nest deeper than it has openers.
    if text.count("[") + text.count("{") <= MAX_DEPTH:
        return False
    depth = 0
    for match in _TOKEN.finditer(text):
        token = match.group()
        if token in ("[", "{"):
            depth += 1
            if depth > MAX_DEPTH:
                return True
        elif token in ("]", "}"):
            depth -= 1
    return False


def loads(data: str | bytes) -> Any:
    """``json.loads``, refusing (``JsonTooDeepError``) a document nested deeper than ``MAX_DEPTH``."""
    if _too_deep(data):
        raise JsonTooDeepError(f"JSON nested deeper than {MAX_DEPTH} levels")
    return json.loads(data)
