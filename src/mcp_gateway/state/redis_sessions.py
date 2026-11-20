"""Session bindings shared by every replica, kept in Redis.

Layout (``p`` is the configured key prefix; nothing below is a session id, a subject, an
issuer or an e-mail, only SHA-256 digests):

* ``{p}s:{sha256(upstream \\x1f session id)}`` holds ``sha256(subject key)`` and expires after
  the idle TTL. It is the binding.
* ``{p}ss:{sha256(subject key)}`` is a sorted set of that subject's binding digests, scored by
  last use in milliseconds (the Redis server clock, so replicas never disagree). It enforces
  ``max_entries_per_subject`` by evicting the subject's own oldest binding.

``bind``, ``check`` and ``drop`` are each one Lua script, so two replicas cannot race a binding
or the cap. The scripts read the set's key from a stored digest, so the layout assumes one
Redis instance (or a primary with replicas), not a sharded Redis Cluster.
"""

import logging

from mcp_gateway.config import SessionsConfig
from mcp_gateway.state.digest import digest
from mcp_gateway.state.redis_client import BoundedRedis
from mcp_gateway.state.sessions import is_valid_session_id

logger = logging.getLogger(__name__)

# KEYS[1] binding; ARGV: prefix, session digest, subject digest, idle ttl ms, per-subject cap.
# Returns 0 (refused: another subject owns it), 1 (bound) or 2 (bound, evicted an old one).
_BIND = """
local now = redis.call('TIME')
now = now[1] * 1000 + math.floor(now[2] / 1000)
local ttl = tonumber(ARGV[4])
local owner = redis.call('GET', KEYS[1])
if owner and owner ~= ARGV[3] then return 0 end
local set = ARGV[1] .. 'ss:' .. ARGV[3]
local evicted = 0
if not owner then
  redis.call('ZREMRANGEBYSCORE', set, '-inf', now - ttl)
  while redis.call('ZCARD', set) >= tonumber(ARGV[5]) do
    local oldest = redis.call('ZRANGE', set, 0, 0)[1]
    redis.call('ZREM', set, oldest)
    redis.call('DEL', ARGV[1] .. 's:' .. oldest)
    evicted = 1
  end
end
redis.call('SET', KEYS[1], ARGV[3], 'PX', ttl)
redis.call('ZADD', set, now, ARGV[2])
redis.call('PEXPIRE', set, ttl)
return 1 + evicted
"""

# KEYS[1] binding; ARGV: prefix, session digest, subject digest ("" = any owner), idle ttl ms.
# Returns 1 when the binding exists (and belongs to the subject, if given) and was refreshed.
_REFRESH = """
local owner = redis.call('GET', KEYS[1])
if not owner then return 0 end
if ARGV[3] ~= '' and owner ~= ARGV[3] then return 0 end
local now = redis.call('TIME')
now = now[1] * 1000 + math.floor(now[2] / 1000)
local ttl = tonumber(ARGV[4])
local set = ARGV[1] .. 'ss:' .. owner
redis.call('PEXPIRE', KEYS[1], ttl)
redis.call('ZADD', set, now, ARGV[2])
redis.call('PEXPIRE', set, ttl)
return 1
"""

# KEYS[1] binding; ARGV: prefix, session digest.
_DROP = """
local owner = redis.call('GET', KEYS[1])
if not owner then return 0 end
redis.call('ZREM', ARGV[1] .. 'ss:' .. owner, ARGV[2])
redis.call('DEL', KEYS[1])
return 1
"""


class RedisSessionStore:
    def __init__(self, redis: BoundedRedis, prefix: str, config: SessionsConfig) -> None:
        self._redis = redis
        self._prefix = prefix
        self._ttl_ms = max(1, round(config.idle_ttl_seconds * 1000))
        self._cap = config.max_entries_per_subject
        self._bind = redis.client.register_script(_BIND)
        self._refresh = redis.client.register_script(_REFRESH)
        self._drop = redis.client.register_script(_DROP)

    def _binding(self, upstream: str, session_id: str) -> tuple[str, str]:
        session = digest(upstream, session_id)
        return f"{self._prefix}s:{session}", session

    async def bind(self, upstream: str, session_id: str, subject_key: str) -> bool:
        if not is_valid_session_id(session_id):
            logger.warning("upstream %s returned a malformed session id; not bound", upstream)
            return False
        key, session = self._binding(upstream, session_id)
        result = await self._redis.run(
            self._bind(
                keys=[key],
                args=[self._prefix, session, digest(subject_key), self._ttl_ms, self._cap],
            )
        )
        if result == 0:
            logger.error("upstream %s reissued a session id bound to another subject", upstream)
            return False
        if result == 2:
            logger.warning("subject reached its session cap; evicted its oldest binding")
        return True

    async def check(self, upstream: str, session_id: str, subject_key: str) -> bool:
        return await self._refresh_binding(upstream, session_id, digest(subject_key))

    async def touch(self, upstream: str, session_id: str) -> None:
        await self._refresh_binding(upstream, session_id, "")

    async def _refresh_binding(self, upstream: str, session_id: str, subject: str) -> bool:
        key, session = self._binding(upstream, session_id)
        result = await self._redis.run(
            self._refresh(keys=[key], args=[self._prefix, session, subject, self._ttl_ms])
        )
        return result == 1

    async def drop(self, upstream: str, session_id: str) -> None:
        key, session = self._binding(upstream, session_id)
        await self._redis.run(self._drop(keys=[key], args=[self._prefix, session]))
