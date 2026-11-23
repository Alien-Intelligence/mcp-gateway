"""Keyed digests: what Redis key names and values carry instead of identifiers.

A plain SHA-256 of a subject or a session id can be recomputed by anyone who can list the keys
and has a candidate value, which turns "is this person a user" into a lookup. The digest is an
HMAC-SHA256 under a key derived (HKDF-SHA256) from the state encryption key, so the key names
are meaningless without that key. This module is the only place key names are derived.
"""

import hashlib
import hmac

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

_SEPARATOR = "\x1f"
_KEY_NAMES_INFO = b"mcp-gateway key names"
_DIGEST_KEY_BYTES = 32


class KeyDigest:
    """Hex HMAC-SHA256 of its parts, joined by an unambiguous separator."""

    def __init__(self, encryption_key: bytes) -> None:
        self._key = HKDF(
            algorithm=hashes.SHA256(), length=_DIGEST_KEY_BYTES, salt=None, info=_KEY_NAMES_INFO
        ).derive(encryption_key)

    def __call__(self, *parts: str) -> str:
        message = _SEPARATOR.join(parts).encode("utf-8")
        return hmac.new(self._key, message, hashlib.sha256).hexdigest()
