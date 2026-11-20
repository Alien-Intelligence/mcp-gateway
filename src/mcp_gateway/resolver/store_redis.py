"""``store_redis`` resolver: look the key up in a Redis the customer fills in.

The key is ``key_pattern`` with ``{subject}``, ``{email}`` and ``{organization}`` replaced in
one pass (never ``str.format``, so a value that itself contains braces is inserted verbatim).
An identity with no value for a placeholder the pattern uses is ``no_mapping``. The stored
value is a JSON object ``{"api_key": "...", "upstream": "<name>" | null, "labels": {...} | null}``,
parsed strictly; a missing key is ``no_mapping``.
"""

import logging
import re
from collections.abc import Collection

import redis.asyncio as aioredis
from redis.exceptions import AuthenticationError, RedisError
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError

from mcp_gateway.config import StoreRedisResolverConfig
from mcp_gateway.identity import Identity
from mcp_gateway.resolver.base import (
    Credential,
    Denied,
    ResolverError,
    ResolverUnavailableError,
    build_credential,
    parse_backend_mapping,
    read_secret_file,
)

logger = logging.getLogger(__name__)

_PLACEHOLDER = re.compile(r"\{(subject|email|organization)\}")


def render_key(pattern: str, identity: Identity) -> str | None:
    """Substitute the placeholders; None when the identity lacks a value the pattern needs."""
    values = {
        "subject": identity.subject,
        "email": identity.email,
        "organization": identity.organization,
    }
    missing = False

    def replace(match: re.Match[str]) -> str:
        nonlocal missing
        value = values[match.group(1)]
        if value is None:
            missing = True
            return ""
        return value

    rendered = _PLACEHOLDER.sub(replace, pattern)
    return None if missing else rendered


class RedisStoreResolver:
    def __init__(self, config: StoreRedisResolverConfig, upstream_names: Collection[str]) -> None:
        if config.default_upstream not in upstream_names:
            raise ResolverError("store_redis default_upstream is not a configured upstream")
        self._config = config
        self._upstream_names = frozenset(upstream_names)
        url = read_secret_file(config.url_file, what="store_redis url")
        try:
            self._client = aioredis.Redis.from_url(
                url,
                socket_connect_timeout=config.connect_timeout_seconds,
                socket_timeout=config.socket_timeout_seconds,
                max_connections=config.max_connections,
                decode_responses=True,
            )
        except (ValueError, RedisError) as exc:
            raise ResolverError("store_redis url is not a valid redis URL") from exc

    async def resolve(self, identity: Identity) -> Credential | Denied:
        key = render_key(self._config.key_pattern, identity)
        if key is None:
            return Denied("no_mapping")
        try:
            value = await self._client.get(key)
        except AuthenticationError as exc:
            raise ResolverError("store_redis authentication failed") from exc
        except (RedisConnectionError, RedisTimeoutError, TimeoutError, OSError) as exc:
            raise ResolverUnavailableError(
                f"store_redis backend unavailable ({type(exc).__name__})"
            ) from exc
        except RedisError as exc:
            raise ResolverError(f"store_redis command failed ({type(exc).__name__})") from exc
        if value is None:
            return Denied("no_mapping")
        if not isinstance(value, str):
            raise ResolverError("store_redis value is not text")
        mapping = parse_backend_mapping(value)
        return build_credential(
            source="store_redis",
            api_key=mapping.api_key,
            upstream=mapping.upstream,
            labels=mapping.labels,
            default_upstream=self._config.default_upstream,
            upstream_names=self._upstream_names,
        )

    async def invalidate(self, identity: Identity) -> None:
        """Nothing is cached here; the cache wrapper owns invalidation."""
        del identity

    async def aclose(self) -> None:
        await self._client.aclose()
