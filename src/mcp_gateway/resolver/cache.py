"""Optional cache in front of any resolver.

* Key: (subject, email, organization, sorted groups).
* Positive entries live ``positive_ttl_seconds``, or the credential's ``ttl_seconds`` when that
  is shorter. Denials live ``negative_ttl_seconds``. Errors are never cached.
* Single flight: concurrent misses for one identity share one backend call. The call runs in
  its own task and is cancelled only when every caller waiting for it has gone.
* Bounded: at most ``max_entries`` entries, least recently used evicted first.
* ``invalidate`` drops the entry, abandons a lookup still in flight (its answer may predate the
  reason for invalidating, so it is neither cached nor shared with later callers) and
  forwards to the wrapped resolver.
"""

import asyncio
import logging
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass

from mcp_gateway.config import ResolverCacheConfig
from mcp_gateway.identity import Identity
from mcp_gateway.resolver.base import Credential, CredentialResolver, Denied

logger = logging.getLogger(__name__)

type CacheKey = tuple[str, str | None, str | None, tuple[str, ...]]
type Outcome = Credential | Denied


def cache_key(identity: Identity) -> CacheKey:
    return (identity.subject, identity.email, identity.organization, tuple(sorted(identity.groups)))


@dataclass
class _Entry:
    outcome: Outcome
    expires_at: float


class _Flight:
    def __init__(self, task: "asyncio.Task[Outcome]") -> None:
        self.task = task
        self.waiters = 0


class CachingResolver:
    def __init__(
        self,
        inner: CredentialResolver,
        config: ResolverCacheConfig,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._inner = inner
        self._config = config
        self._clock = clock
        self._entries: OrderedDict[CacheKey, _Entry] = OrderedDict()
        self._flights: dict[CacheKey, _Flight] = {}

    def __len__(self) -> int:
        return len(self._entries)

    def _lookup(self, key: CacheKey) -> Outcome | None:
        entry = self._entries.get(key)
        if entry is None:
            return None
        if entry.expires_at <= self._clock():
            del self._entries[key]
            return None
        self._entries.move_to_end(key)
        return entry.outcome

    def _store(self, key: CacheKey, outcome: Outcome) -> None:
        if isinstance(outcome, Denied):
            ttl = self._config.negative_ttl_seconds
        else:
            ttl = self._config.positive_ttl_seconds
            if outcome.ttl_seconds is not None:
                ttl = min(ttl, outcome.ttl_seconds)
        self._entries[key] = _Entry(outcome, self._clock() + ttl)
        self._entries.move_to_end(key)
        while len(self._entries) > self._config.max_entries:
            self._entries.popitem(last=False)

    def _finished(self, key: CacheKey, flight: _Flight, task: "asyncio.Task[Outcome]") -> None:
        if self._flights.get(key) is flight:
            del self._flights[key]
            if not task.cancelled() and task.exception() is None:
                self._store(key, task.result())
        elif not task.cancelled():
            # Abandoned by invalidate or close: nobody awaits it, but the error is not lost.
            task.exception()

    def _start(self, key: CacheKey, identity: Identity) -> _Flight:
        task = asyncio.get_running_loop().create_task(self._inner.resolve(identity))
        flight = _Flight(task)
        self._flights[key] = flight
        task.add_done_callback(lambda done: self._finished(key, flight, done))
        return flight

    async def resolve(self, identity: Identity) -> Outcome:
        key = cache_key(identity)
        cached = self._lookup(key)
        if cached is not None:
            return cached
        flight = self._flights.get(key)
        if flight is None:
            flight = self._start(key, identity)
        flight.waiters += 1
        try:
            return await asyncio.shield(flight.task)
        finally:
            flight.waiters -= 1
            if flight.waiters == 0 and not flight.task.done():
                # Leave the map now: a caller arriving before the done-callback runs must start
                # a fresh call, not join one that is being cancelled.
                if self._flights.get(key) is flight:
                    del self._flights[key]
                flight.task.cancel()

    async def invalidate(self, identity: Identity) -> None:
        key = cache_key(identity)
        self._entries.pop(key, None)
        self._flights.pop(key, None)
        await self._inner.invalidate(identity)

    async def aclose(self) -> None:
        for flight in list(self._flights.values()):
            flight.task.cancel()
        self._flights.clear()
        self._entries.clear()
        await self._inner.aclose()
