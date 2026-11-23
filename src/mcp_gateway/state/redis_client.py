"""One Redis client whose every operation is bounded and whose failures are one error type."""

import logging
from collections.abc import Awaitable

import anyio
import redis.asyncio as aioredis
from redis.asyncio.retry import Retry
from redis.backoff import NoBackoff
from redis.exceptions import RedisError

from mcp_gateway.config import ConfigError, RedisStateConfig
from mcp_gateway.state.errors import StateUnavailableError

logger = logging.getLogger(__name__)


def read_text_file(path: str, *, what: str) -> str:
    """Read a startup file. The error names the setting, never the content."""
    try:
        with open(path, encoding="utf-8") as handle:
            value = handle.read().strip()
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigError(f"{what}: file unreadable ({type(exc).__name__})") from exc
    if not value:
        raise ConfigError(f"{what}: file is empty")
    return value


class BoundedRedis:
    """A Redis client with a hard ceiling on every operation.

    Retries are off (redis-py retries three times with backoff by default, which would make a
    request outlast its timeouts) and every call runs under ``anyio.fail_after``. A Redis
    error, a refused connection, a timeout: all of them become ``StateUnavailableError``.
    When all ``max_connections`` are busy a call waits for one, within that same ceiling.
    """

    def __init__(self, config: RedisStateConfig) -> None:
        url = read_text_file(config.url_file, what="state url_file")
        try:
            # A blocking pool queues a command when every connection is busy; the default pool
            # would refuse it ("Too many connections") and fail a healthy replica under load.
            # The wait for a connection is bounded by the operation timeout, the same ceiling as
            # the command itself.
            self._pool = aioredis.BlockingConnectionPool.from_url(
                url,
                max_connections=config.max_connections,
                timeout=config.operation_timeout_seconds,
                socket_connect_timeout=config.connect_timeout_seconds,
                socket_timeout=config.operation_timeout_seconds,
                decode_responses=True,
                retry=Retry(NoBackoff(), 0),
            )
        except (ValueError, RedisError) as exc:
            raise ConfigError("state url_file does not hold a valid redis URL") from exc
        self.client = aioredis.Redis.from_pool(self._pool)
        # Waiting for a connection and connecting count against it too: nothing holds a request
        # longer than this.
        self._timeout = config.operation_timeout_seconds

    async def run[T](self, operation: Awaitable[T]) -> T:
        try:
            with anyio.fail_after(self._timeout):
                return await operation
        except (RedisError, TimeoutError, OSError) as exc:
            raise StateUnavailableError(f"shared state unavailable ({type(exc).__name__})") from exc

    async def ping(self) -> None:
        await self.run(self.client.ping())

    async def aclose(self) -> None:
        # The client was built from a pool it does not own, so the pool is closed explicitly.
        await self.client.aclose()
        await self._pool.aclose()
