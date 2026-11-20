"""Origin header policy (RFC 6454 serialisation, MCP transport DNS-rebinding rule)."""

from collections.abc import Iterable
from urllib.parse import urlsplit

_DEFAULT_PORTS = {"http": 80, "https": 443}


def normalize_origin(value: str) -> str | None:
    """Return ``scheme://host[:port]`` with scheme and host lowercased and a default port elided.

    None when ``value`` is not an origin: no scheme or host, userinfo, a path, query or
    fragment, or an invalid port. The opaque origin ``null`` (sandboxed frames, ``file:``,
    redirects) is not an origin either: it identifies no site, so no policy can name it.
    """
    if "://" not in value:
        return None
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError:
        return None
    host = parts.hostname
    if (
        not parts.scheme
        or not host
        or parts.username is not None
        or parts.password is not None
        or parts.path
        or parts.query
        or parts.fragment
    ):
        return None
    if ":" in host:
        host = f"[{host}]"
    if port is None or _DEFAULT_PORTS.get(parts.scheme) == port:
        return f"{parts.scheme}://{host}"
    return f"{parts.scheme}://{host}:{port}"


class OriginPolicy:
    def __init__(self, allowed: Iterable[str]) -> None:
        self._allowed = frozenset(allowed)

    def allows(self, header_values: list[str]) -> bool:
        """No Origin header passes (not a browser); exactly one listed origin passes."""
        if not header_values:
            return True
        if len(header_values) > 1:
            return False
        origin = normalize_origin(header_values[0].strip())
        return origin is not None and origin in self._allowed
