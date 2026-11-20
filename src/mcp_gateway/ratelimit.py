"""Token-bucket rate limits per subject and per organization.

A bucket holds at most ``burst`` tokens, starts full and refills ``per_minute / 60`` tokens per
second. Every authenticated request to the MCP endpoint takes one token from each applicable
bucket. The take is atomic across the buckets of one request: a token leaves all of them or none,
so an exhausted organization bucket never drains the tokens of the subject that was refused.

Two implementations share the algorithm. ``MemoryRateLimiter`` is exact for one replica;
``RedisRateLimiter`` keeps the buckets in Redis so several replicas share them.
"""

import hashlib
import math
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Literal, Protocol

from mcp_gateway.config import RateLimitsConfig
from mcp_gateway.state.errors import StateUnavailableError
from mcp_gateway.state.redis_client import BoundedRedis
from mcp_gateway.state.state import State

type Scope = Literal["subject", "organization"]

# Waiting times are computed in floating point; rounding before the ceiling stops 5.0000000001
# seconds from being announced as 6.
_ROUNDING_DIGITS = 6
_SECONDS_PER_MINUTE = 60.0


class RateLimitUnavailableError(StateUnavailableError):
    """The bucket store answered something the limiter cannot use. The request must not pass unmetered.

    A subclass of the state error so the gateway has one 503 mapping for every shared-state failure.
    """


@dataclass(frozen=True)
class BucketSpec:
    """One bucket a request draws from: who it belongs to and how it is shaped."""

    scope: Scope
    key: str
    burst: int
    per_minute: float

    @property
    def tokens_per_second(self) -> float:
        return self.per_minute / _SECONDS_PER_MINUTE


@dataclass(frozen=True)
class Decision:
    allowed: bool
    # Whole seconds (at least 1) until every bucket the request needs has a token; 0 when allowed.
    retry_after_seconds: int
    # The scope whose bucket takes longest to refill; None when allowed.
    limited_scope: Scope | None


ALLOWED = Decision(allowed=True, retry_after_seconds=0, limited_scope=None)


class RateLimiter(Protocol):
    async def acquire(self, buckets: Sequence[BucketSpec]) -> Decision: ...


def bucket_specs(
    limits: RateLimitsConfig, *, subject_key: str, organization: str | None
) -> list[BucketSpec]:
    """The buckets one request draws from.

    A subject whose token carries no organization is limited per subject only: there is no
    organization to charge.
    """
    specs: list[BucketSpec] = []
    if limits.per_subject is not None:
        specs.append(
            BucketSpec(
                "subject", subject_key, limits.per_subject.burst, limits.per_subject.per_minute
            )
        )
    if limits.per_organization is not None and organization is not None:
        specs.append(
            BucketSpec(
                "organization",
                organization,
                limits.per_organization.burst,
                limits.per_organization.per_minute,
            )
        )
    return specs


def _retry_after(wait_seconds: float) -> int:
    return max(1, math.ceil(round(wait_seconds, _ROUNDING_DIGITS)))


@dataclass
class _Level:
    tokens: float
    updated_at: float
    burst: int
    tokens_per_second: float

    def refilled(self, now: float) -> float:
        elapsed = max(0.0, now - self.updated_at)
        return min(float(self.burst), self.tokens + elapsed * self.tokens_per_second)


class MemoryRateLimiter:
    """Buckets in this process only: exact for one replica, independent per replica.

    Memory stays bounded without a knob. A bucket that has refilled to full is
    indistinguishable from one that never existed (both start full and behave identically from
    then on), so dropping it loses nothing. Full buckets are swept lazily, whenever the table has
    doubled since the last sweep, which keeps the cost per request constant on average. What
    remains is the set of buckets still refilling: subjects active within one refill window.
    """

    _MIN_SWEEP_SIZE = 1024

    def __init__(self, clock: Callable[[], float]) -> None:
        self._clock = clock
        self._levels: dict[tuple[Scope, str], _Level] = {}
        self._sweep_at = self._MIN_SWEEP_SIZE

    @property
    def tracked_buckets(self) -> int:
        return len(self._levels)

    async def acquire(self, buckets: Sequence[BucketSpec]) -> Decision:
        now = self._clock()
        self._sweep_if_due(now)
        levels = [self._level(spec, now) for spec in buckets]
        refilled = [level.refilled(now) for level in levels]
        waits = [
            (1.0 - tokens) / level.tokens_per_second if tokens < 1.0 else 0.0
            for level, tokens in zip(levels, refilled, strict=True)
        ]
        if not any(waits):
            for level, tokens in zip(levels, refilled, strict=True):
                level.tokens = tokens - 1.0
                level.updated_at = now
            return ALLOWED
        # Nothing was taken from any bucket; the refill is recomputed from the old state next time.
        slowest = max(range(len(waits)), key=waits.__getitem__)
        return Decision(False, _retry_after(waits[slowest]), buckets[slowest].scope)

    def _level(self, spec: BucketSpec, now: float) -> _Level:
        key = (spec.scope, spec.key)
        level = self._levels.get(key)
        if level is None:
            level = _Level(float(spec.burst), now, spec.burst, spec.tokens_per_second)
            self._levels[key] = level
        return level

    def _sweep_if_due(self, now: float) -> None:
        if len(self._levels) < self._sweep_at:
            return
        self._levels = {
            key: level for key, level in self._levels.items() if level.refilled(now) < level.burst
        }
        self._sweep_at = max(self._MIN_SWEEP_SIZE, 2 * len(self._levels))


# One script for all the buckets of a request, so the check and the take are one atomic step on
# the server. Time comes from the Redis server (TIME), never from a replica, so replicas with
# skewed clocks agree. KEYS are the bucket keys; ARGV holds, per key, the burst and the refill in
# tokens per millisecond. A bucket is a hash {tokens, ts}; a missing key is a full bucket. The
# key expires when the bucket would be full again, so idle buckets cost nothing.
# Returns {allowed (0/1), wait_ms, index of the slowest bucket (1-based, 0 when allowed)}.
_ACQUIRE_LUA = """
local time = redis.call('TIME')
local now_ms = tonumber(time[1]) * 1000 + math.floor(tonumber(time[2]) / 1000)
local count = #KEYS
local tokens = {}
local wait_ms = 0
local slowest = 0
for i = 1, count do
  local burst = tonumber(ARGV[2 * i - 1])
  local rate = tonumber(ARGV[2 * i])
  local stored = redis.call('HMGET', KEYS[i], 'tokens', 'ts')
  local level = burst
  if stored[1] then
    local elapsed = math.max(0, now_ms - tonumber(stored[2]))
    level = math.min(burst, tonumber(stored[1]) + elapsed * rate)
  end
  tokens[i] = level
  if level < 1 then
    local wait = math.ceil((1 - level) / rate)
    if wait > wait_ms then
      wait_ms = wait
      slowest = i
    end
  end
end
if slowest > 0 then
  return {0, wait_ms, slowest}
end
for i = 1, count do
  local burst = tonumber(ARGV[2 * i - 1])
  local rate = tonumber(ARGV[2 * i])
  local left = tokens[i] - 1
  redis.call('HSET', KEYS[i], 'tokens', string.format('%.9f', left), 'ts', now_ms)
  redis.call('PEXPIRE', KEYS[i], math.max(1, math.ceil((burst - left) / rate)))
end
return {1, 0, 0}
"""


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class RedisRateLimiter:
    """Buckets in Redis, shared by every replica that uses the same ``key_prefix``.

    The Redis handle is the state backend's: it is built and closed by the state, and every call
    goes through its bounded ``run`` so the limiter shares the state's timeout and error mapping.
    Keys hold a hash of the subject or organization, never the value. A failure raises
    ``StateUnavailableError`` so the caller fails closed instead of letting traffic through
    unmetered.
    """

    _SCOPE_TAG: dict[Scope, str] = {"subject": "s", "organization": "o"}

    def __init__(self, redis: BoundedRedis, key_prefix: str) -> None:
        self._redis = redis
        self._script = redis.client.register_script(_ACQUIRE_LUA)
        self._prefix = key_prefix

    def _key(self, spec: BucketSpec) -> str:
        return f"{self._prefix}rl:{self._SCOPE_TAG[spec.scope]}:{_digest(spec.key)}"

    async def acquire(self, buckets: Sequence[BucketSpec]) -> Decision:
        keys = [self._key(spec) for spec in buckets]
        args: list[str] = []
        for spec in buckets:
            args.extend((str(spec.burst), repr(spec.tokens_per_second / 1000.0)))
        reply = await self._redis.run(self._script(keys=keys, args=args))
        return self._decision(reply, buckets)

    @staticmethod
    def _decision(reply: object, buckets: Sequence[BucketSpec]) -> Decision:
        if not (isinstance(reply, list) and len(reply) == 3):
            raise RateLimitUnavailableError("unexpected reply from the rate limit script")
        allowed, wait_ms, slowest = (int(item) for item in reply)
        if allowed == 1:
            return ALLOWED
        if not 1 <= slowest <= len(buckets):
            raise RateLimitUnavailableError("unexpected reply from the rate limit script")
        return Decision(False, _retry_after(wait_ms / 1000.0), buckets[slowest - 1].scope)


def build_rate_limiter(config: RateLimitsConfig | None, state: State) -> RateLimiter | None:
    """The limiter ``policies.rate_limits`` asks for, on the same backend as the rest of the state.

    Redis state: one set of buckets shared by every replica, reached through the state's bounded
    client. Memory state: per-process buckets, exact for a single replica only.
    """
    if config is None:
        return None
    if state.redis is None:
        return MemoryRateLimiter(clock=time.monotonic)
    if state.key_prefix is None:
        raise ValueError("a state with a Redis client must carry its key prefix")
    return RedisRateLimiter(state.redis, state.key_prefix)
