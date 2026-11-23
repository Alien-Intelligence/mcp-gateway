"""Token-bucket algorithm, with an injected clock."""

from typing import Any, cast

import pytest

from mcp_gateway.config import RateLimitsConfig, TokenBucketConfig
from mcp_gateway.ratelimit import (
    MAX_RETRY_AFTER_SECONDS,
    BucketSpec,
    MemoryRateLimiter,
    RateLimitUnavailableError,
    RedisRateLimiter,
    bucket_specs,
    organization_bucket,
    subject_bucket,
)
from mcp_gateway.state import BoundedRedis
from mcp_gateway.state.digest import KeyDigest


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def subject(key: str = "u1", burst: int = 3, per_minute: float = 60.0) -> BucketSpec:
    return BucketSpec("subject", key, burst, per_minute)


def org(key: str = "o1", burst: int = 5, per_minute: float = 60.0) -> BucketSpec:
    return BucketSpec("organization", key, burst, per_minute)


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def limiter(clock: Clock) -> MemoryRateLimiter:
    return MemoryRateLimiter(clock)


async def test_a_bucket_starts_full_and_allows_exactly_burst_requests(
    limiter: MemoryRateLimiter,
) -> None:
    results = [(await limiter.acquire([subject()])).allowed for _ in range(4)]
    assert results == [True, True, True, False]


async def test_the_refill_gives_tokens_back_at_per_minute_over_sixty_per_second(
    limiter: MemoryRateLimiter, clock: Clock
) -> None:
    spec = subject(burst=2, per_minute=30.0)  # one token every 2 s
    await limiter.acquire([spec])
    await limiter.acquire([spec])
    assert not (await limiter.acquire([spec])).allowed
    clock.now += 1.9
    assert not (await limiter.acquire([spec])).allowed
    clock.now += 0.1
    assert (await limiter.acquire([spec])).allowed
    assert not (await limiter.acquire([spec])).allowed


async def test_the_refill_never_exceeds_the_burst(limiter: MemoryRateLimiter, clock: Clock) -> None:
    spec = subject(burst=2, per_minute=60.0)
    await limiter.acquire([spec])
    clock.now += 3600
    results = [(await limiter.acquire([spec])).allowed for _ in range(3)]
    assert results == [True, True, False]


@pytest.mark.parametrize(
    ("per_minute", "elapsed", "expected"),
    [
        (60.0, 0.0, 1),  # a whole token away, one second
        (6.0, 0.0, 10),  # one token every 10 s
        (6.0, 0.5, 10),  # 9.5 s left rounds up
        (6.0, 9.2, 1),  # 0.8 s left still announces at least one second
        (7.0, 0.0, 9),  # 60/7 = 8.57 s rounds up
    ],
)
async def test_retry_after_is_the_ceiling_of_the_wait_and_at_least_one(
    limiter: MemoryRateLimiter, clock: Clock, per_minute: float, elapsed: float, expected: int
) -> None:
    spec = subject(burst=1, per_minute=per_minute)
    assert (await limiter.acquire([spec])).allowed
    clock.now += elapsed
    decision = await limiter.acquire([spec])
    assert not decision.allowed
    assert decision.retry_after_seconds == expected
    assert decision.limited_scope == "subject"


async def test_retry_after_is_honest_waiting_that_long_succeeds(
    limiter: MemoryRateLimiter, clock: Clock
) -> None:
    spec = subject(burst=1, per_minute=7.0)
    await limiter.acquire([spec])
    decision = await limiter.acquire([spec])
    clock.now += decision.retry_after_seconds
    assert (await limiter.acquire([spec])).allowed


async def test_an_exhausted_organization_does_not_drain_the_subject(
    limiter: MemoryRateLimiter,
) -> None:
    assert (await limiter.acquire([org("shared", burst=1)])).allowed
    # Another subject of the same organization: the organization bucket refuses.
    for _ in range(5):
        decision = await limiter.acquire([subject("u1", burst=3), org("shared", burst=1)])
        assert not decision.allowed
        assert decision.limited_scope == "organization"
    # u1 kept every token: three subject-only requests pass.
    results = [(await limiter.acquire([subject("u1", burst=3)])).allowed for _ in range(4)]
    assert results == [True, True, True, False]


async def test_an_exhausted_subject_does_not_drain_the_organization(
    limiter: MemoryRateLimiter,
) -> None:
    await limiter.acquire([subject("u1", burst=1)])
    refused = await limiter.acquire([subject("u1", burst=1), org("o", burst=2)])
    assert not refused.allowed
    assert refused.limited_scope == "subject"
    results = [(await limiter.acquire([org("o", burst=2)])).allowed for _ in range(3)]
    assert results == [True, True, False]


async def test_retry_after_waits_for_the_slowest_bucket(limiter: MemoryRateLimiter) -> None:
    fast = subject("u", burst=1, per_minute=60.0)  # 1 s
    slow = org("o", burst=1, per_minute=6.0)  # 10 s
    await limiter.acquire([fast, slow])
    decision = await limiter.acquire([fast, slow])
    assert decision.retry_after_seconds == 10
    assert decision.limited_scope == "organization"


async def test_buckets_of_different_subjects_and_scopes_are_independent(
    limiter: MemoryRateLimiter,
) -> None:
    assert (await limiter.acquire([subject("a", burst=1)])).allowed
    assert (await limiter.acquire([subject("b", burst=1)])).allowed
    assert not (await limiter.acquire([subject("a", burst=1)])).allowed
    # The same key under the other scope is another bucket.
    assert (await limiter.acquire([org("a", burst=1)])).allowed


class SmallSweepLimiter(MemoryRateLimiter):
    _MIN_SWEEP_SIZE = 8


async def test_full_buckets_are_pruned_and_behave_like_fresh_ones(clock: Clock) -> None:
    limiter = SmallSweepLimiter(clock)
    for index in range(7):
        await limiter.acquire([subject(f"idle-{index}", burst=2, per_minute=60.0)])
    await limiter.acquire([subject("hot", burst=1, per_minute=0.6)])  # 100 s to refill
    assert limiter.tracked_buckets == 8
    clock.now += 5  # every idle bucket (one token spent, 1 s to refill) is full; "hot" is not
    await limiter.acquire([subject("trigger", burst=2)])  # the table reached its size: sweep
    assert limiter.tracked_buckets == 2  # "hot" and "trigger"
    # A pruned subject has its whole burst, exactly as a kept bucket that refilled would have.
    results = [(await limiter.acquire([subject("idle-0", burst=2)])).allowed for _ in range(3)]
    assert results == [True, True, False]
    # The bucket that was still refilling was not pruned.
    assert not (await limiter.acquire([subject("hot", burst=1, per_minute=0.6)])).allowed


async def test_memory_stays_bounded_under_a_stream_of_one_shot_subjects(
    limiter: MemoryRateLimiter, clock: Clock
) -> None:
    for index in range(5 * MemoryRateLimiter._MIN_SWEEP_SIZE):
        clock.now += 1.0  # each subject's single spent token is back before the next sweep
        await limiter.acquire([subject(f"u-{index}", burst=2, per_minute=60.0)])
    assert limiter.tracked_buckets <= 2 * MemoryRateLimiter._MIN_SWEEP_SIZE


def _limits(per_organization: bool) -> RateLimitsConfig:
    return RateLimitsConfig(
        per_subject=TokenBucketConfig(burst=3, per_minute=6.0),
        per_organization=TokenBucketConfig(burst=9, per_minute=12.0) if per_organization else None,
    )


def test_bucket_specs_with_an_organization_draw_from_both() -> None:
    specs = bucket_specs(_limits(True), subject_key="iss\x1fu1", organization="acme")
    assert specs == [
        BucketSpec("subject", "iss\x1fu1", 3, 6.0),
        BucketSpec("organization", "acme", 9, 12.0),
    ]


def test_a_subject_without_an_organization_is_still_limited_per_subject() -> None:
    specs = bucket_specs(_limits(True), subject_key="iss\x1fu1", organization=None)
    assert [spec.scope for spec in specs] == ["subject"]


def test_bucket_specs_skip_an_organization_scope_configured_null() -> None:
    specs = bucket_specs(_limits(False), subject_key="k", organization="o")
    assert [spec.scope for spec in specs] == ["subject"]


def test_the_two_buckets_are_available_separately_for_the_two_step_take() -> None:
    limits = _limits(True)
    assert subject_bucket(limits, subject_key="iss\x1fu1") == BucketSpec(
        "subject", "iss\x1fu1", 3, 6.0
    )
    assert organization_bucket(limits, organization="acme") == BucketSpec(
        "organization", "acme", 9, 12.0
    )
    assert organization_bucket(limits, organization=None) is None
    assert organization_bucket(_limits(False), organization="acme") is None


async def test_the_memory_limiter_caps_an_enormous_wait(limiter: MemoryRateLimiter) -> None:
    """The config floor allows 16 hours per token; the limiter itself never announces over a day."""
    spec = subject(burst=1, per_minute=0.0001)
    await limiter.acquire([spec])
    decision = await limiter.acquire([spec])
    assert (decision.allowed, decision.retry_after_seconds) == (False, MAX_RETRY_AFTER_SECONDS)


class ScriptedRedis:
    """The two members of ``BoundedRedis`` the limiter touches, answering a canned script reply."""

    def __init__(self, reply: object) -> None:
        self.reply = reply
        self.client = self

    def register_script(self, source: str) -> Any:
        return lambda keys, args: self.reply

    async def run(self, operation: object) -> object:
        return operation


def redis_limiter(reply: object) -> RedisRateLimiter:
    redis = cast(BoundedRedis, ScriptedRedis(reply))
    return RedisRateLimiter(redis, "p:", KeyDigest(bytes(32)))


async def test_the_redis_limiter_caps_an_enormous_wait() -> None:
    decision = await redis_limiter([0, 10**12, 1]).acquire([subject()])
    assert (decision.allowed, decision.retry_after_seconds) == (False, MAX_RETRY_AFTER_SECONDS)


@pytest.mark.parametrize(
    "reply",
    [
        [0, "soon", 1],
        [0, None, 1],
        [0, 1.5, 1],
        [0, True, 1],
        ["x", 1000, 1],
        [0, 1000, "1"],
        [0, 1000],
        "not a list",
    ],
)
async def test_a_script_reply_that_is_not_three_integers_is_unavailable_not_a_crash(
    reply: object,
) -> None:
    with pytest.raises(RateLimitUnavailableError):
        await redis_limiter(reply).acquire([subject()])
