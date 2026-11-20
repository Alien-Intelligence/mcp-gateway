"""SHA-256 digests: what Redis key names and values carry instead of identifiers."""

import hashlib

_SEPARATOR = "\x1f"


def digest(*parts: str) -> str:
    """Hex SHA-256 of the parts joined by an unambiguous separator."""
    return hashlib.sha256(_SEPARATOR.join(parts).encode("utf-8")).hexdigest()
