"""Key names are keyed digests: stable for one key, unrelated across keys, unambiguous."""

import hashlib

from mcp_gateway.state.digest import KeyDigest

KEY = bytes(range(32))


def test_a_digest_is_stable_for_one_key_and_is_not_a_plain_sha256() -> None:
    digest = KeyDigest(KEY)
    assert digest("alice") == KeyDigest(KEY)("alice")
    assert digest("alice") != hashlib.sha256(b"alice").hexdigest()
    assert len(digest("alice")) == 64


def test_another_key_names_the_same_value_differently() -> None:
    assert KeyDigest(KEY)("alice") != KeyDigest(bytes(32))("alice")


def test_parts_cannot_be_shifted_across_the_separator() -> None:
    digest = KeyDigest(KEY)
    assert digest("ab", "c") != digest("a", "bc")
    assert digest("a", "b") != digest("ab")
