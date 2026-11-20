"""State stores that fail on demand, to prove the gateway fails closed."""

from mcp_gateway.resolver import MemoryCacheStore
from mcp_gateway.resolver.cache import CacheKey, Outcome
from mcp_gateway.state import MemorySessionStore, State, StateUnavailableError


class BrokenSessions:
    """A session store whose listed operations raise ``StateUnavailableError``."""

    def __init__(self, *failing: str) -> None:
        self._failing = set(failing)
        self._inner = MemorySessionStore(
            max_entries=10, max_entries_per_subject=10, idle_ttl_seconds=600
        )
        self.calls: list[str] = []

    def _enter(self, name: str) -> None:
        self.calls.append(name)
        if name in self._failing:
            raise StateUnavailableError("shared state unavailable (FakeError)")

    async def bind(self, upstream: str, session_id: str, subject_key: str) -> bool:
        self._enter("bind")
        return await self._inner.bind(upstream, session_id, subject_key)

    async def check(self, upstream: str, session_id: str, subject_key: str) -> bool:
        self._enter("check")
        return await self._inner.check(upstream, session_id, subject_key)

    async def touch(self, upstream: str, session_id: str) -> None:
        self._enter("touch")
        await self._inner.touch(upstream, session_id)

    async def drop(self, upstream: str, session_id: str) -> None:
        self._enter("drop")
        await self._inner.drop(upstream, session_id)


class BrokenCache:
    """A cache store that is down for every operation."""

    async def get(self, key: CacheKey) -> Outcome | None:
        raise StateUnavailableError("shared state unavailable (FakeError)")

    async def generation(self, key: CacheKey) -> str:
        raise StateUnavailableError("shared state unavailable (FakeError)")

    async def put(
        self, key: CacheKey, outcome: Outcome, ttl_seconds: float, generation: str
    ) -> None:
        raise StateUnavailableError("shared state unavailable (FakeError)")

    async def invalidate(self, key: CacheKey) -> None:
        raise StateUnavailableError("shared state unavailable (FakeError)")


def state_with(*failing: str, cache_down: bool = False) -> tuple[State, BrokenSessions]:
    """A memory-like state whose session operations named in ``failing`` raise."""
    sessions = BrokenSessions(*failing)
    cache = BrokenCache() if cache_down else MemoryCacheStore(100)
    return State(sessions=sessions, cache=cache, redis=None, key_prefix=None), sessions
