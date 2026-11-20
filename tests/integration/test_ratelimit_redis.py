"""The Redis limiter against a real Redis: shared buckets, atomicity, server-side time, failure."""

import asyncio
import hashlib
import time
import uuid
from collections.abc import AsyncIterator, Callable
from pathlib import Path

import pytest
import redis.asyncio as aioredis

from mcp_gateway.config import RedisStateConfig
from mcp_gateway.ratelimit import BucketSpec, RedisRateLimiter
from mcp_gateway.state import BoundedRedis, StateUnavailableError

pytestmark = pytest.mark.integration

TIMEOUT = 2.0
# Negligible refill: one token per 100 minutes.
SLOW = 0.01


@pytest.fixture
def prefix() -> str:
    return f"p4rl:{uuid.uuid4().hex}:"


@pytest.fixture
async def client(redis_url: str, prefix: str) -> AsyncIterator[aioredis.Redis]:
    redis = aioredis.Redis.from_url(redis_url, decode_responses=True)
    yield redis
    keys = [key async for key in redis.scan_iter(match=f"{prefix}*")]
    if keys:
        await redis.delete(*keys)
    await redis.aclose()


MakeRedis = Callable[[str, float], BoundedRedis]


@pytest.fixture
async def make_redis(tmp_path: Path, prefix: str) -> AsyncIterator[MakeRedis]:
    """A state-owned bounded Redis handle for a URL and an operation timeout, closed after."""
    built: list[BoundedRedis] = []

    def make(url: str, operation_timeout: float) -> BoundedRedis:
        url_file = tmp_path / f"url-{len(built)}"
        url_file.write_text(url + "\n")
        redis = BoundedRedis(
            RedisStateConfig(
                type="redis",
                url_file=str(url_file),
                key_prefix=prefix,
                connect_timeout_seconds=1,
                operation_timeout_seconds=operation_timeout,
                max_connections=64,
                encryption_key_file=str(tmp_path / "unused-key"),
            )
        )
        built.append(redis)
        return redis

    yield make
    for redis in built:
        await redis.aclose()


@pytest.fixture
def limiter(make_redis: MakeRedis, redis_url: str, prefix: str) -> RedisRateLimiter:
    return RedisRateLimiter(make_redis(redis_url, TIMEOUT), prefix)


def subject(key: str = "u1", burst: int = 3, per_minute: float = SLOW) -> BucketSpec:
    return BucketSpec("subject", key, burst, per_minute)


def org(key: str = "o1", burst: int = 5, per_minute: float = SLOW) -> BucketSpec:
    return BucketSpec("organization", key, burst, per_minute)


async def test_two_limiters_share_their_buckets_like_two_replicas(
    make_redis: MakeRedis, redis_url: str, prefix: str
) -> None:
    replica_a = RedisRateLimiter(make_redis(redis_url, TIMEOUT), prefix)
    replica_b = RedisRateLimiter(make_redis(redis_url, TIMEOUT), prefix)
    spec = subject(burst=3)
    results = [
        (await replica_a.acquire([spec])).allowed,
        (await replica_b.acquire([spec])).allowed,
        (await replica_a.acquire([spec])).allowed,
        (await replica_b.acquire([spec])).allowed,
        (await replica_a.acquire([spec])).allowed,
    ]
    assert results == [True, True, True, False, False]


async def test_fifty_concurrent_acquires_with_burst_ten_allow_exactly_ten(
    make_redis: MakeRedis, redis_url: str, prefix: str
) -> None:
    replicas = [RedisRateLimiter(make_redis(redis_url, TIMEOUT), prefix) for _ in range(2)]
    spec = subject(burst=10)
    decisions = await asyncio.gather(*(replicas[i % 2].acquire([spec]) for i in range(50)))
    assert sum(d.allowed for d in decisions) == 10
    assert all(d.retry_after_seconds >= 1 for d in decisions if not d.allowed)


async def test_an_exhausted_organization_does_not_drain_the_subject(
    limiter: RedisRateLimiter,
) -> None:
    assert (await limiter.acquire([org("shared", burst=1)])).allowed
    for _ in range(5):
        decision = await limiter.acquire([subject("u1", burst=3), org("shared", burst=1)])
        assert not decision.allowed
        assert decision.limited_scope == "organization"
    results = [(await limiter.acquire([subject("u1", burst=3)])).allowed for _ in range(4)]
    assert results == [True, True, True, False]


async def test_an_exhausted_subject_does_not_drain_the_organization(
    limiter: RedisRateLimiter,
) -> None:
    await limiter.acquire([subject("u1", burst=1)])
    refused = await limiter.acquire([subject("u1", burst=1), org("o", burst=2)])
    assert (refused.allowed, refused.limited_scope) == (False, "subject")
    results = [(await limiter.acquire([org("o", burst=2)])).allowed for _ in range(3)]
    assert results == [True, True, False]


async def test_retry_after_is_the_slowest_bucket_rounded_up(limiter: RedisRateLimiter) -> None:
    fast = subject("u", burst=1, per_minute=60.0)  # 1 s
    slow = org("o", burst=1, per_minute=6.0)  # 10 s
    await limiter.acquire([fast, slow])
    decision = await limiter.acquire([fast, slow])
    assert decision.allowed is False
    assert decision.limited_scope == "organization"
    assert 9 <= decision.retry_after_seconds <= 10


async def test_tokens_come_back_with_time(limiter: RedisRateLimiter) -> None:
    spec = subject("refill", burst=1, per_minute=600.0)  # one token per 100 ms
    assert (await limiter.acquire([spec])).allowed
    deadline = time.monotonic() + 5
    while not (await limiter.acquire([spec])).allowed:
        assert time.monotonic() < deadline, "the bucket never refilled"
        await asyncio.sleep(0.02)


async def test_a_refused_request_reports_a_wait_after_which_it_succeeds(
    limiter: RedisRateLimiter,
) -> None:
    spec = subject("honest", burst=1, per_minute=120.0)  # one token per 500 ms
    await limiter.acquire([spec])
    decision = await limiter.acquire([spec])
    assert (decision.allowed, decision.retry_after_seconds) == (False, 1)
    deadline = time.monotonic() + decision.retry_after_seconds + 2
    while not (await limiter.acquire([spec])).allowed:
        assert time.monotonic() < deadline
        await asyncio.sleep(0.05)


async def test_idle_buckets_expire_and_start_full_again(
    limiter: RedisRateLimiter, client: aioredis.Redis, prefix: str
) -> None:
    spec = subject("ttl", burst=2, per_minute=1200.0)  # a token every 50 ms
    await limiter.acquire([spec])
    (key,) = [k async for k in client.scan_iter(match=f"{prefix}rl:s:*")]
    assert 0 < await client.pttl(key) <= 100
    deadline = time.monotonic() + 5
    while await client.exists(key):
        assert time.monotonic() < deadline, "the bucket key never expired"
        await asyncio.sleep(0.02)
    assert (await limiter.acquire([spec])).allowed


async def test_keys_hold_a_hash_and_never_the_subject_or_organization(
    limiter: RedisRateLimiter, client: aioredis.Redis, prefix: str
) -> None:
    subject_key = "https://idp.example.com\x1fvery-identifiable-subject"
    await limiter.acquire([subject(subject_key), org("very-identifiable-org")])
    keys = [key async for key in client.scan_iter(match=f"{prefix}*")]
    assert sorted(keys) == sorted(
        [
            f"{prefix}rl:s:{hashlib.sha256(subject_key.encode()).hexdigest()}",
            f"{prefix}rl:o:{hashlib.sha256(b'very-identifiable-org').hexdigest()}",
        ]
    )
    for key in keys:
        assert "identifiable" not in key
        assert "identifiable" not in str(await client.hgetall(key))


async def test_a_closed_port_raises_unavailable_within_the_timeout(
    make_redis: MakeRedis, prefix: str
) -> None:
    limiter = RedisRateLimiter(make_redis("redis://127.0.0.1:1/0", 1.5), prefix)
    started = time.monotonic()
    with pytest.raises(StateUnavailableError):
        await limiter.acquire([subject()])
    assert time.monotonic() - started < 3


async def test_a_server_that_never_answers_hits_the_operation_timeout(
    make_redis: MakeRedis, prefix: str
) -> None:
    async def silent(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await asyncio.Event().wait()

    server = await asyncio.start_server(silent, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    limiter = RedisRateLimiter(make_redis(f"redis://127.0.0.1:{port}/0", 0.3), prefix)
    started = time.monotonic()
    try:
        with pytest.raises(StateUnavailableError):
            await limiter.acquire([subject()])
        assert time.monotonic() - started < 2
    finally:
        server.close()
