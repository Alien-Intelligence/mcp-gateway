"""The configured state backend: session store, cache store and a readiness probe."""

import time
from collections.abc import Callable
from dataclasses import dataclass

from mcp_gateway.config import ConfigError, GatewayConfig, MemoryStateConfig, RedisStateConfig
from mcp_gateway.resolver.cache import CacheStore, MemoryCacheStore
from mcp_gateway.state.redis_cache import RedisCacheStore, load_encryption_key
from mcp_gateway.state.redis_client import BoundedRedis
from mcp_gateway.state.redis_sessions import RedisSessionStore
from mcp_gateway.state.sessions import MemorySessionStore, SessionStore


@dataclass(frozen=True)
class State:
    """Everything that must be shared when the gateway runs as several replicas.

    ``redis`` and ``key_prefix`` are None with the memory backend. Features that need the same
    Redis (rate limiting) take the client and prefix from here instead of opening their own.
    ``cache`` is None when the resolver cache is disabled.
    """

    sessions: SessionStore
    cache: CacheStore | None
    redis: BoundedRedis | None
    key_prefix: str | None

    async def ping(self) -> None:
        """Raise ``StateUnavailableError`` when the backend does not answer in time."""
        if self.redis is not None:
            await self.redis.ping()

    async def aclose(self) -> None:
        if self.redis is not None:
            await self.redis.aclose()


def _bound(value: int | None, name: str) -> int:
    # GatewayConfig refuses a memory config without the bound; this only narrows the type.
    if value is None:
        raise ConfigError(f"{name} is required when state.type is memory")
    return value


def _memory(config: GatewayConfig, clock: Callable[[], float]) -> State:
    cache: CacheStore | None = None
    if config.resolver_cache is not None:
        cache = MemoryCacheStore(
            _bound(config.resolver_cache.max_entries, "resolver_cache.max_entries")
        )
    return State(
        sessions=MemorySessionStore(
            max_entries=_bound(config.sessions.max_entries, "sessions.max_entries"),
            max_entries_per_subject=config.sessions.max_entries_per_subject,
            idle_ttl_seconds=config.sessions.idle_ttl_seconds,
            clock=clock,
        ),
        cache=cache,
        redis=None,
        key_prefix=None,
    )


def _redis(config: GatewayConfig, state: RedisStateConfig) -> State:
    key = load_encryption_key(state.encryption_key_file)
    redis = BoundedRedis(state)
    prefix = state.key_prefix
    return State(
        sessions=RedisSessionStore(redis, prefix, config.sessions),
        cache=None if config.resolver_cache is None else RedisCacheStore(redis, prefix, key),
        redis=redis,
        key_prefix=prefix,
    )


def build_state(
    config: GatewayConfig, *, session_clock: Callable[[], float] = time.monotonic
) -> State:
    """Build the backend ``config.state`` names. Fails fast on an unreadable URL or key file."""
    match config.state:
        case MemoryStateConfig():
            return _memory(config, session_clock)
        case RedisStateConfig() as redis_config:
            return _redis(config, redis_config)
