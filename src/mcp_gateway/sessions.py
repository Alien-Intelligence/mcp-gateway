"""In-memory binding of upstream MCP session ids to the subject that created them.

A session id is a bearer capability on the upstream. The gateway records which subject an
upstream issued it to, and refuses (without forwarding) any other subject presenting it.
"""

import logging
import re
import time
from collections import Counter, OrderedDict
from collections.abc import Callable

logger = logging.getLogger(__name__)

# Visible ASCII only (MCP transport spec); the cap keeps one entry's memory bounded.
_SESSION_ID = re.compile(r"[\x21-\x7e]{1,512}")

type _Key = tuple[str, str]


def is_valid_session_id(value: str) -> bool:
    return _SESSION_ID.fullmatch(value) is not None


class SessionRegistry:
    """Bounded map ``(upstream, session id) -> subject key`` with an idle TTL.

    The clock is injectable so tests control time. Entries are least-recently-used ordered;
    when full, expired entries go first and then the least recently used one is evicted
    (that client gets a 404 and re-initialises). One subject holds at most
    ``max_entries_per_subject`` bindings: beyond it the subject's own oldest binding goes, so a
    single user opening sessions in a loop cannot push everyone else's out.
    """

    def __init__(
        self,
        *,
        max_entries: int,
        max_entries_per_subject: int,
        idle_ttl_seconds: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._max_entries = max_entries
        self._max_per_subject = max_entries_per_subject
        self._idle_ttl = idle_ttl_seconds
        self._clock = clock
        # Value: (subject key, last used).
        self._entries: OrderedDict[_Key, tuple[str, float]] = OrderedDict()
        self._per_subject: Counter[str] = Counter()

    def __len__(self) -> int:
        return len(self._entries)

    def bind(self, upstream: str, session_id: str, subject_key: str) -> bool:
        """Record a session id the upstream issued to ``subject_key``.

        Returns False and records nothing when the id is malformed or already bound to a
        different subject (an upstream reusing an id must never move a binding).
        """
        if not is_valid_session_id(session_id):
            logger.warning("upstream %s returned a malformed session id; not bound", upstream)
            return False
        key = (upstream, session_id)
        now = self._clock()
        current = self._live(key, now)
        if current is not None and current != subject_key:
            logger.error("upstream %s reissued a session id bound to another subject", upstream)
            return False
        if current is None:
            self._make_room_for_subject(subject_key)
            self._per_subject[subject_key] += 1
        self._entries[key] = (subject_key, now)
        self._entries.move_to_end(key)
        self._make_room(now)
        return True

    def check(self, upstream: str, session_id: str, subject_key: str) -> bool:
        """True when the session is bound to ``subject_key``; refreshes its idle timer."""
        key = (upstream, session_id)
        now = self._clock()
        owner = self._live(key, now)
        if owner is None or owner != subject_key:
            return False
        self._entries[key] = (owner, now)
        self._entries.move_to_end(key)
        return True

    def touch(self, upstream: str, session_id: str) -> None:
        """Refresh an entry's idle timer (after a long stream), whoever owns it."""
        key = (upstream, session_id)
        now = self._clock()
        owner = self._live(key, now)
        if owner is not None:
            self._entries[key] = (owner, now)
            self._entries.move_to_end(key)

    def drop(self, upstream: str, session_id: str) -> None:
        self._discard((upstream, session_id))

    def _discard(self, key: _Key) -> None:
        entry = self._entries.pop(key, None)
        if entry is None:
            return
        subject_key = entry[0]
        self._per_subject[subject_key] -= 1
        if self._per_subject[subject_key] <= 0:
            del self._per_subject[subject_key]

    def _live(self, key: _Key, now: float) -> str | None:
        entry = self._entries.get(key)
        if entry is None:
            return None
        owner, last_used = entry
        if now - last_used > self._idle_ttl:
            self._discard(key)
            return None
        return owner

    def _make_room(self, now: float) -> None:
        if len(self._entries) <= self._max_entries:
            return
        for key in [k for k, (_, last) in self._entries.items() if now - last > self._idle_ttl]:
            self._discard(key)
        while len(self._entries) > self._max_entries:
            self._discard(next(iter(self._entries)))
            logger.warning("session registry full; evicted the least recently used entry")

    def _make_room_for_subject(self, subject_key: str) -> None:
        """Before a subject's new binding: drop its own oldest ones past the per-subject cap."""
        while self._per_subject[subject_key] >= self._max_per_subject:
            oldest = next(key for key, (owner, _) in self._entries.items() if owner == subject_key)
            self._discard(oldest)
            logger.warning("subject reached its session cap; evicted its oldest binding")
