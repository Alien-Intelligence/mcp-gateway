"""Where cached userinfo profiles live: this process, or a Redis shared by every replica.

An entry is keyed by the access token, never by the subject: a token is the unit the IdP
validates, and a token that is revoked or expires must not be served from another token's entry.
What is stored is the ``Profile`` read from the document, not the document. Every method may
raise ``StateUnavailableError``. The Redis implementation is ``mcp_gateway.state.redis_userinfo``.
"""

import hashlib
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from mcp_gateway.identity import Profile


def token_digest(token: str) -> str:
    """The hex SHA-256 of a token: what in-process maps are keyed by, instead of the token."""
    return hashlib.sha256(token.encode()).hexdigest()


class UserinfoStore(Protocol):
    async def get(self, token: str) -> Profile | None: ...

    async def put(self, token: str, profile: Profile, ttl_seconds: float) -> None: ...


@dataclass
class _Entry:
    profile: Profile
    expires_at: float


class MemoryUserinfoStore:
    """Per-process store: bounded, least recently used evicted first."""

    def __init__(self, max_entries: int, clock: Callable[[], float] = time.monotonic) -> None:
        self._max_entries = max_entries
        self._clock = clock
        self._entries: OrderedDict[str, _Entry] = OrderedDict()

    def __len__(self) -> int:
        return len(self._entries)

    async def get(self, token: str) -> Profile | None:
        key = token_digest(token)
        entry = self._entries.get(key)
        if entry is None:
            return None
        if entry.expires_at <= self._clock():
            del self._entries[key]
            return None
        self._entries.move_to_end(key)
        return entry.profile

    async def put(self, token: str, profile: Profile, ttl_seconds: float) -> None:
        key = token_digest(token)
        self._entries[key] = _Entry(profile, self._clock() + ttl_seconds)
        self._entries.move_to_end(key)
        while len(self._entries) > self._max_entries:
            self._entries.popitem(last=False)
