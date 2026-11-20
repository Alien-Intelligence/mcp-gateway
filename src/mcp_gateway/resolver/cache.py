"""Optional cache in front of any resolver.

* Key: (subject, email, organization, sorted groups).
* Positive entries live ``positive_ttl_seconds``, or the credential's ``ttl_seconds`` when that
  is shorter. Denials live ``negative_ttl_seconds``. Errors are never cached.
* Where entries live is a ``CacheStore``: this process (``MemoryCacheStore``, bounded, least
  recently used evicted first) or a Redis shared by all replicas. A store failure
  (``StateUnavailableError``) propagates: the inner resolver is never called around a broken
  cache, because the entry it would skip may have been invalidated.
* Single flight, per process: concurrent misses for one identity share one backend call. The
  call runs in its own task and is cancelled only when every caller waiting for it has gone.
  Replicas do not coordinate their flights; two replicas missing at once each ask the backend.
* ``invalidate`` drops the entry, abandons a lookup still in flight in this process (its answer
  may predate the reason for invalidating, so it is neither cached nor shared with later
  callers) and forwards to the wrapped resolver. A lookup in flight on another replica is
  fenced by the store's generation token: the store refuses its late ``put``.
"""

import asyncio
import json
import logging
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, Protocol

from mcp_gateway.config import ResolverCacheConfig
from mcp_gateway.identity import Identity
from mcp_gateway.resolver.base import Credential, CredentialResolver, Denied

logger = logging.getLogger(__name__)

type CacheKey = tuple[str, str | None, str | None, tuple[str, ...]]
type Outcome = Credential | Denied
type CacheResult = Literal["hit", "negative", "miss"]


def cache_key(identity: Identity) -> CacheKey:
    return (identity.subject, identity.email, identity.organization, tuple(sorted(identity.groups)))


def canonical_cache_key(key: CacheKey) -> str:
    """One stable text per identity (what a shared store digests into a key name)."""
    return json.dumps(list(key), ensure_ascii=False, separators=(",", ":"))


class CacheStore(Protocol):
    """Where cached outcomes live. Every method may raise ``StateUnavailableError``."""

    async def get(self, key: CacheKey) -> Outcome | None: ...

    async def generation(self, key: CacheKey) -> str:
        """An opaque token that changes whenever the entry is invalidated.

        Read it BEFORE asking the backend and hand it back to ``put``.
        """
        ...

    async def put(
        self, key: CacheKey, outcome: Outcome, ttl_seconds: float, generation: str
    ) -> None:
        """Store the outcome unless the entry was invalidated since ``generation`` was read."""
        ...

    async def invalidate(self, key: CacheKey) -> None: ...


@dataclass
class _Entry:
    outcome: Outcome
    expires_at: float


class MemoryCacheStore:
    """Per-process store: correct for one replica only."""

    def __init__(self, max_entries: int, clock: Callable[[], float] = time.monotonic) -> None:
        self._max_entries = max_entries
        self._clock = clock
        self._entries: OrderedDict[CacheKey, _Entry] = OrderedDict()

    def __len__(self) -> int:
        return len(self._entries)

    async def get(self, key: CacheKey) -> Outcome | None:
        entry = self._entries.get(key)
        if entry is None:
            return None
        if entry.expires_at <= self._clock():
            del self._entries[key]
            return None
        self._entries.move_to_end(key)
        return entry.outcome

    async def generation(self, key: CacheKey) -> str:
        # Nothing outside this process can invalidate an entry; the flight map covers the rest.
        return ""

    async def put(
        self, key: CacheKey, outcome: Outcome, ttl_seconds: float, generation: str
    ) -> None:
        self._entries[key] = _Entry(outcome, self._clock() + ttl_seconds)
        self._entries.move_to_end(key)
        while len(self._entries) > self._max_entries:
            self._entries.popitem(last=False)

    async def invalidate(self, key: CacheKey) -> None:
        self._entries.pop(key, None)


class _Flight:
    task: "asyncio.Task[Outcome]"

    def __init__(self) -> None:
        self.waiters = 0


class CachingResolver:
    def __init__(
        self, inner: CredentialResolver, config: ResolverCacheConfig, store: CacheStore
    ) -> None:
        self._inner = inner
        self._config = config
        self._store = store
        self._flights: dict[CacheKey, _Flight] = {}

    @property
    def store(self) -> CacheStore:
        return self._store

    def _ttl(self, outcome: Outcome) -> float:
        if isinstance(outcome, Denied):
            return self._config.negative_ttl_seconds
        ttl = self._config.positive_ttl_seconds
        if outcome.ttl_seconds is not None:
            ttl = min(ttl, outcome.ttl_seconds)
        return ttl

    async def _fill(self, key: CacheKey, identity: Identity, flight: _Flight) -> Outcome:
        # The token is read before the backend is asked: an invalidation (here or on another
        # replica) that lands while the backend works makes the store refuse the answer.
        generation = await self._store.generation(key)
        outcome = await self._inner.resolve(identity)
        if self._flights.get(key) is flight:
            await self._store.put(key, outcome, self._ttl(outcome), generation)
        return outcome

    def _finished(self, key: CacheKey, flight: _Flight, task: "asyncio.Task[Outcome]") -> None:
        if self._flights.get(key) is flight:
            del self._flights[key]
        if not task.cancelled():
            # Abandoned by invalidate or close: nobody awaits it, but the error is not lost.
            task.exception()

    def _start(self, key: CacheKey, identity: Identity) -> _Flight:
        flight = _Flight()
        flight.task = asyncio.get_running_loop().create_task(self._fill(key, identity, flight))
        self._flights[key] = flight
        flight.task.add_done_callback(lambda done: self._finished(key, flight, done))
        return flight

    async def resolve(self, identity: Identity) -> Outcome:
        outcome, _ = await self.resolve_detailed(identity)
        return outcome

    async def resolve_detailed(self, identity: Identity) -> tuple[Outcome, CacheResult]:
        """Resolve, and say how the cache answered: hit, negative (a cached denial) or miss.

        A caller that joins a lookup already in flight is a miss: the backend was asked.
        """
        key = cache_key(identity)
        cached = await self._store.get(key)
        if cached is not None:
            return cached, "negative" if isinstance(cached, Denied) else "hit"
        flight = self._flights.get(key)
        if flight is None:
            flight = self._start(key, identity)
        flight.waiters += 1
        try:
            return await asyncio.shield(flight.task), "miss"
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
        self._flights.pop(key, None)
        try:
            await self._store.invalidate(key)
        finally:
            # The inner resolver's own cache must forget the identity even when the shared
            # store is down: the store error still reaches the caller, but a revoked credential
            # is not kept alive behind it.
            await self._inner.invalidate(identity)

    async def aclose(self) -> None:
        for flight in list(self._flights.values()):
            flight.task.cancel()
        self._flights.clear()
        await self._inner.aclose()
