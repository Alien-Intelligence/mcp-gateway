"""The userinfo lookup with a cache and single flight, in front of the client.

* One entry per access token, living ``cache_ttl_seconds`` or until the token's own ``exp`` when
  that comes first. A token with no time left is never cached. Errors are never cached.
* Single flight, per process: concurrent misses for one token share one IdP call. The call runs
  in its own task and is cancelled only when every caller waiting for it has gone. Replicas do not
  coordinate their flights.
* A store failure (``StateUnavailableError``) propagates: the IdP is never asked around a broken
  cache, and a lookup is never answered without recording it where other replicas will look.
"""

import asyncio
import logging
import time
from collections.abc import Callable
from typing import Literal

from mcp_gateway.config import UserinfoConfig
from mcp_gateway.identity import Profile
from mcp_gateway.userinfo.client import UserinfoClient
from mcp_gateway.userinfo.store import UserinfoStore, token_digest

logger = logging.getLogger(__name__)

type CacheResult = Literal["hit", "miss"]


class _Flight:
    task: "asyncio.Task[Profile]"

    def __init__(self) -> None:
        self.waiters = 0


class UserinfoService:
    def __init__(
        self,
        client: UserinfoClient,
        store: UserinfoStore,
        config: UserinfoConfig,
        *,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        self._client = client
        self._store = store
        self._config = config
        # The token's ``exp`` is a wall-clock epoch time, so the remaining life is measured on one.
        self._wall_clock = wall_clock
        self._flights: dict[str, _Flight] = {}

    def _ttl(self, token_expires_at: float) -> float:
        return min(self._config.cache_ttl_seconds, token_expires_at - self._wall_clock())

    async def _fill(self, token: str, token_subject: str, token_expires_at: float) -> Profile:
        profile = await self._client.fetch(token, token_subject)
        ttl = self._ttl(token_expires_at)
        if ttl > 0:
            await self._store.put(token, profile, ttl)
        return profile

    def _finished(self, key: str, flight: _Flight, task: "asyncio.Task[Profile]") -> None:
        if self._flights.get(key) is flight:
            del self._flights[key]
        if not task.cancelled():
            # Abandoned by its last waiter or by close: nobody awaits it, but the error is not lost.
            task.exception()

    def _start(self, key: str, token: str, token_subject: str, expires_at: float) -> _Flight:
        flight = _Flight()
        flight.task = asyncio.get_running_loop().create_task(
            self._fill(token, token_subject, expires_at)
        )
        self._flights[key] = flight
        flight.task.add_done_callback(lambda done: self._finished(key, flight, done))
        return flight

    async def profile(
        self,
        token: str,
        token_subject: str,
        token_expires_at: float,
        *,
        on_cache_result: Callable[[CacheResult], None],
    ) -> Profile:
        """The profile of ``token``'s user.

        ``token_subject`` and ``token_expires_at`` (epoch seconds) are the ``sub`` and ``exp`` of
        the verified token. ``on_cache_result`` is told whether the cache answered as soon as that
        is known, so a caller can report it even when the lookup then fails. A caller that joins a
        lookup already in flight is a miss: the IdP was asked.
        """
        cached = await self._store.get(token)
        if cached is not None:
            on_cache_result("hit")
            return cached
        on_cache_result("miss")
        key = token_digest(token)
        flight = self._flights.get(key)
        if flight is None:
            flight = self._start(key, token, token_subject, token_expires_at)
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

    async def aclose(self) -> None:
        for flight in list(self._flights.values()):
            flight.task.cancel()
        self._flights.clear()
