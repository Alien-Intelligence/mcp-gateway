"""The shared state against a real Redis: two ``State`` objects on one server are two replicas."""

import asyncio
import base64
import hashlib
import logging
import os
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path

import anyio
import httpx
import pytest
import redis.asyncio as aioredis
from pydantic import SecretStr
from unit.conftest import base_config_dict, config_from
from unit.test_app import client_for

from mcp_gateway.app import create_app
from mcp_gateway.config import RateLimitsConfig, ResolverCacheConfig, TokenBucketConfig
from mcp_gateway.identity import Identity
from mcp_gateway.ratelimit import BucketSpec, build_rate_limiter
from mcp_gateway.resolver import CachingResolver, Credential, Denied
from mcp_gateway.resolver.cache import cache_key, canonical_cache_key
from mcp_gateway.state import State, StateUnavailableError, build_state
from mcp_gateway.state.redis_cache import RedisCacheStore

pytestmark = pytest.mark.integration

UP = "main"
CACHE_CONFIG = {"positive_ttl_seconds": 60, "negative_ttl_seconds": 5, "max_entries": None}
API_KEY = "fake-api-key-do-not-leak-0123456789"
SUBJECT = "alice-subject-id"
EMAIL = "alice@client-x.example"
SESSION = "session-id-do-not-leak"
ISSUER = "https://idp.example.com/realms/test"
DEADLINE_SECONDS = 10.0

type MakeState = Callable[..., State]


def identity(subject: str = SUBJECT) -> Identity:
    return Identity(subject=subject, email=EMAIL, organization="client-x", groups=("g",), claims={})


def credential(ttl: float | None = None) -> Credential:
    return Credential(
        api_key=SecretStr(API_KEY), upstream=UP, labels={"client": "x"}, ttl_seconds=ttl
    )


def subject_key(subject: str = SUBJECT) -> str:
    return f"{ISSUER}\x1f{subject}"


@pytest.fixture
def prefix() -> str:
    # Every test owns a key prefix, so nothing here touches other data in the server.
    return f"p4test:{uuid.uuid4().hex}:"


@pytest.fixture
async def admin(redis_url: str, prefix: str) -> AsyncIterator[aioredis.Redis]:
    client = aioredis.Redis.from_url(redis_url, decode_responses=True)
    yield client
    keys = [key async for key in client.scan_iter(match=f"{prefix}*")]
    if keys:
        await client.delete(*keys)
    await client.aclose()


@pytest.fixture
async def make_state(tmp_path: Path, redis_url: str, prefix: str) -> AsyncIterator[MakeState]:
    """Build a replica. ``key`` is its encryption key; replicas that share Redis share it."""
    built: list[State] = []
    shared_key = os.urandom(32)

    def make(
        *,
        url: str | None = None,
        key: bytes | None = None,
        idle_ttl: float = 600,
        per_subject: int = 20,
        operation_timeout: float = 2,
        max_connections: int = 4,
    ) -> State:
        index = len(built)
        (tmp_path / f"url-{index}").write_text((url or redis_url) + "\n")
        (tmp_path / f"key-{index}").write_text(base64.b64encode(key or shared_key).decode() + "\n")
        data = base_config_dict()
        data["resolver_cache"] = CACHE_CONFIG
        data["sessions"] = {
            "max_entries": None,
            "max_entries_per_subject": per_subject,
            "idle_ttl_seconds": idle_ttl,
        }
        data["state"] = {
            "type": "redis",
            "url_file": str(tmp_path / f"url-{index}"),
            "key_prefix": prefix,
            "connect_timeout_seconds": 1,
            "operation_timeout_seconds": operation_timeout,
            "max_connections": max_connections,
            "encryption_key_file": str(tmp_path / f"key-{index}"),
        }
        state = build_state(config_from(data))
        built.append(state)
        return state

    yield make
    for state in built:
        await state.aclose()


async def eventually(check: Callable[[], Awaitable[bool]], what: str) -> None:
    """Poll until ``check`` holds; fail with ``what`` at the deadline (never a fixed sleep)."""
    with anyio.move_on_after(DEADLINE_SECONDS) as scope:
        while not await check():
            await asyncio.sleep(0.02)
    assert not scope.cancelled_caught, f"timed out waiting for: {what}"


async def next_millisecond(admin: aioredis.Redis) -> None:
    """Wait until the server clock has moved on, so two bindings never share a score."""
    seconds, micros = await admin.time()
    start = seconds * 1000 + micros // 1000

    async def moved() -> bool:
        seconds, micros = await admin.time()
        return seconds * 1000 + micros // 1000 > start

    await eventually(moved, "the server clock to advance")


class Backend:
    """A resolver that counts calls and can be held open."""

    def __init__(self, outcome: Credential | Denied) -> None:
        self.outcome = outcome
        self.calls = 0
        self.entered = asyncio.Event()
        self.gate: asyncio.Event | None = None

    async def resolve(self, identity: Identity) -> Credential | Denied:
        self.calls += 1
        self.entered.set()
        if self.gate is not None:
            await self.gate.wait()
        return self.outcome

    async def invalidate(self, identity: Identity) -> None:
        return None

    async def aclose(self) -> None:
        return None


def cached(state: State, backend: Backend) -> CachingResolver:
    assert state.cache is not None
    return CachingResolver(backend, ResolverCacheConfig.model_validate(CACHE_CONFIG), state.cache)


def store_with_key(state: State, key: bytes) -> RedisCacheStore:
    """A cache store on the state's Redis, prefix and key-name digest, sealing with ``key``.

    Key names derive from the encryption key, so two replicas with different keys never meet on
    the same name. Sharing the digest and changing only the sealing key is how a test puts a
    value sealed by another key where this replica looks.
    """
    assert state.redis is not None and state.key_prefix is not None and state.digest is not None
    return RedisCacheStore(state.redis, state.key_prefix, key, state.digest)


def resolver_on(store: RedisCacheStore, backend: Backend) -> CachingResolver:
    return CachingResolver(backend, ResolverCacheConfig.model_validate(CACHE_CONFIG), store)


# --- sessions ---------------------------------------------------------------------------


async def test_a_session_bound_on_one_replica_is_honoured_on_the_other(
    make_state: MakeState,
) -> None:
    a, b = make_state(), make_state()
    assert await a.sessions.bind(UP, SESSION, subject_key())
    assert await b.sessions.check(UP, SESSION, subject_key())
    assert await a.sessions.check(UP, SESSION, subject_key())


async def test_another_subject_is_refused_on_both_replicas(make_state: MakeState) -> None:
    a, b = make_state(), make_state()
    await a.sessions.bind(UP, SESSION, subject_key())
    other = subject_key("mallory")
    assert not await a.sessions.check(UP, SESSION, other)
    assert not await b.sessions.check(UP, SESSION, other)
    assert await b.sessions.check(UP, SESSION, subject_key()), "the owner is unaffected"


async def test_an_unknown_session_and_another_upstream_are_refused(make_state: MakeState) -> None:
    a = make_state()
    await a.sessions.bind(UP, SESSION, subject_key())
    assert not await a.sessions.check(UP, "never-bound", subject_key())
    assert not await a.sessions.check("other", SESSION, subject_key())


async def test_an_id_bound_to_someone_else_is_never_moved_across_replicas(
    make_state: MakeState,
) -> None:
    a, b = make_state(), make_state()
    assert await a.sessions.bind(UP, SESSION, subject_key())
    assert not await b.sessions.bind(UP, SESSION, subject_key("mallory"))
    assert await b.sessions.check(UP, SESSION, subject_key())
    assert not await b.sessions.check(UP, SESSION, subject_key("mallory"))
    assert await b.sessions.bind(UP, SESSION, subject_key()), "the owner may rebind"


async def test_malformed_session_ids_are_not_bound(
    make_state: MakeState, admin: aioredis.Redis, prefix: str
) -> None:
    a = make_state()
    for bad in ("has space", "", "x" * 513):
        assert not await a.sessions.bind(UP, bad, subject_key())
    assert [key async for key in admin.scan_iter(match=f"{prefix}*")] == []


async def test_a_drop_on_one_replica_is_seen_on_the_other(make_state: MakeState) -> None:
    a, b = make_state(), make_state()
    await a.sessions.bind(UP, SESSION, subject_key())
    assert await b.sessions.check(UP, SESSION, subject_key())
    await a.sessions.drop(UP, SESSION)
    assert not await b.sessions.check(UP, SESSION, subject_key())
    await a.sessions.drop(UP, "never-bound")


async def test_a_subject_over_its_cap_loses_only_its_own_oldest_binding(
    make_state: MakeState, admin: aioredis.Redis
) -> None:
    a, b = make_state(per_subject=2), make_state(per_subject=2)
    await a.sessions.bind(UP, "bob-1", subject_key("bob"))
    for index in range(4):
        await next_millisecond(admin)
        replica = a if index % 2 == 0 else b
        await replica.sessions.bind(UP, f"alice-{index}", subject_key())
    assert await a.sessions.check(UP, "bob-1", subject_key("bob")), "another subject is untouched"
    held = [await a.sessions.check(UP, f"alice-{i}", subject_key()) for i in range(4)]
    assert held == [False, False, True, True]


async def test_rebinding_and_dropping_keep_the_cap_count_right(
    make_state: MakeState, admin: aioredis.Redis
) -> None:
    a = make_state(per_subject=2)
    await a.sessions.bind(UP, "a1", subject_key())
    await a.sessions.bind(UP, "a2", subject_key())
    assert await a.sessions.bind(UP, "a2", subject_key())
    assert await a.sessions.check(UP, "a1", subject_key()), "a rebind is not a third binding"
    await a.sessions.drop(UP, "a1")
    await next_millisecond(admin)
    await a.sessions.bind(UP, "a3", subject_key())
    assert await a.sessions.check(UP, "a2", subject_key())
    assert await a.sessions.check(UP, "a3", subject_key())


async def test_an_idle_binding_expires_and_use_keeps_one_alive(
    make_state: MakeState, admin: aioredis.Redis, prefix: str
) -> None:
    a, b = make_state(idle_ttl=1.0), make_state(idle_ttl=1.0)
    await a.sessions.bind(UP, "idle", subject_key())
    await a.sessions.bind(UP, "busy", subject_key())

    # Use on the other replica refreshes the shared expiry well past the original one.
    started = time.monotonic()
    while time.monotonic() - started < 1.6:
        assert await b.sessions.check(UP, "busy", subject_key())
        await asyncio.sleep(0.2)

    async def idle_binding_gone() -> bool:
        return not await a.sessions.check(UP, "idle", subject_key())

    await eventually(idle_binding_gone, "the idle binding to expire")
    assert await a.sessions.check(UP, "busy", subject_key())


async def test_touch_refreshes_a_binding_whoever_owns_it(make_state: MakeState) -> None:
    a = make_state(idle_ttl=1.0)
    await a.sessions.bind(UP, SESSION, subject_key())
    started = time.monotonic()
    while time.monotonic() - started < 1.6:
        await a.sessions.touch(UP, SESSION)
        await asyncio.sleep(0.2)
    assert await a.sessions.check(UP, SESSION, subject_key())
    await a.sessions.touch(UP, "never-bound")


# --- resolver cache ---------------------------------------------------------------------


async def test_an_entry_written_through_one_replica_is_a_hit_on_the_other(
    make_state: MakeState,
) -> None:
    backend_a, backend_b = Backend(credential()), Backend(credential())
    cache_a, cache_b = cached(make_state(), backend_a), cached(make_state(), backend_b)
    first, how_first = await cache_a.resolve_detailed(identity())
    second, how_second = await cache_b.resolve_detailed(identity())
    assert (how_first, how_second) == ("miss", "hit")
    assert (backend_a.calls, backend_b.calls) == (1, 0)
    assert isinstance(second, Credential)
    assert second.api_key.get_secret_value() == API_KEY
    assert (second.upstream, dict(second.labels)) == (UP, {"client": "x"})
    assert first == second


async def test_a_denial_is_shared_and_keeps_its_reason(make_state: MakeState) -> None:
    cache_a = cached(make_state(), Backend(Denied("no_mapping")))
    backend_b = Backend(credential())
    cache_b = cached(make_state(), backend_b)
    await cache_a.resolve(identity())
    outcome, how = await cache_b.resolve_detailed(identity())
    assert (outcome, how) == (Denied("no_mapping"), "negative")
    assert backend_b.calls == 0


async def test_an_entry_lives_as_long_as_the_shortest_ttl(
    make_state: MakeState, admin: aioredis.Redis, prefix: str
) -> None:
    await cached(make_state(), Backend(credential(ttl=30))).resolve(identity())
    (entry,) = [key async for key in admin.scan_iter(match=f"{prefix}c:*")]
    assert 0 < await admin.pttl(entry) <= 30_000


async def test_an_invalidation_on_one_replica_is_a_miss_on_the_other(
    make_state: MakeState,
) -> None:
    cache_a = cached(make_state(), Backend(credential()))
    backend_b = Backend(credential())
    cache_b = cached(make_state(), backend_b)
    await cache_a.resolve(identity())
    assert (await cache_b.resolve_detailed(identity()))[1] == "hit"
    await cache_a.invalidate(identity())
    _, how = await cache_b.resolve_detailed(identity())
    assert how == "miss"
    assert backend_b.calls == 1


async def test_an_invalidation_cannot_be_undone_by_a_lookup_in_flight_elsewhere(
    make_state: MakeState,
) -> None:
    state_a, state_b = make_state(), make_state()
    cache_a = cached(state_a, Backend(credential()))
    slow = Backend(credential())
    slow.gate = asyncio.Event()
    cache_b = cached(state_b, slow)

    lookup = asyncio.create_task(cache_b.resolve(identity()))
    with anyio.fail_after(DEADLINE_SECONDS):
        await slow.entered.wait()
    await cache_a.invalidate(identity())
    slow.gate.set()
    answer = await lookup
    assert isinstance(answer, Credential), "the caller that asked still gets its answer"

    assert state_a.cache is not None
    assert await state_a.cache.get(cache_key(identity())) is None, (
        "the late answer must not put back an invalidated credential"
    )


async def test_the_generation_expires_but_still_fences_after_an_invalidation(
    make_state: MakeState, admin: aioredis.Redis, prefix: str
) -> None:
    cache = cached(make_state(), Backend(credential()))
    await cache.invalidate(identity())
    (generation,) = [key async for key in admin.scan_iter(match=f"{prefix}cg:*")]
    assert await admin.get(generation) == "1"
    assert 0 < await admin.pttl(generation) <= 24 * 60 * 60 * 1000


async def test_an_entry_sealed_by_another_key_is_a_quiet_miss_and_is_kept(
    make_state: MakeState,
    admin: aioredis.Redis,
    prefix: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """During a rolling key change the other generation still reads its own entries."""
    state = make_state()
    writer = store_with_key(state, os.urandom(32))
    await resolver_on(writer, Backend(credential())).resolve(identity())
    (entry,) = [key async for key in admin.scan_iter(match=f"{prefix}c:*")]
    sealed = await admin.get(entry)

    backend = Backend(credential())
    reader = store_with_key(state, os.urandom(32))
    with caplog.at_level(logging.DEBUG, logger="mcp_gateway.state.redis_cache"):
        outcome = await reader.get(cache_key(identity()))
    assert outcome is None
    assert await admin.get(entry) == sealed, "the other key's entry must not be deleted"
    assert [r for r in caplog.records if r.levelno >= logging.INFO] == []
    assert API_KEY not in caplog.text
    # The writer's own key still reads it.
    assert await writer.get(cache_key(identity())) is not None
    assert backend.calls == 0


async def test_a_tampered_entry_under_our_key_is_deleted_with_a_warning(
    make_state: MakeState,
    admin: aioredis.Redis,
    prefix: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    state = make_state()
    await cached(state, Backend(credential())).resolve(identity())
    (entry,) = [key async for key in admin.scan_iter(match=f"{prefix}c:*")]
    raw = bytearray(base64.b64decode(str(await admin.get(entry))))
    raw[-1] ^= 0x01
    await admin.set(entry, base64.b64encode(bytes(raw)).decode("ascii"))

    assert state.cache is not None
    with caplog.at_level(logging.DEBUG, logger="mcp_gateway.state.redis_cache"):
        assert await state.cache.get(cache_key(identity())) is None
    assert await admin.exists(entry) == 0
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert warnings[0].getMessage().startswith("shared cache entry failed authentication")
    assert API_KEY not in caplog.text


async def test_garbage_in_an_entry_is_a_miss_and_is_deleted(
    make_state: MakeState, admin: aioredis.Redis, prefix: str
) -> None:
    state = make_state()
    await cached(state, Backend(credential())).resolve(identity())
    (entry,) = [key async for key in admin.scan_iter(match=f"{prefix}c:*")]
    await admin.set(entry, "!!! not base64 !!!")
    assert state.cache is not None
    assert await state.cache.get(cache_key(identity())) is None
    assert await admin.exists(entry) == 0


async def test_a_replayed_ciphertext_past_its_expiry_is_a_miss(
    make_state: MakeState, admin: aioredis.Redis, prefix: str
) -> None:
    """Redis' TTL is not the only clock: the expiry rides inside the sealed value."""
    state = make_state()
    await cached(state, Backend(credential(ttl=0.3))).resolve(identity())
    (entry,) = [key async for key in admin.scan_iter(match=f"{prefix}c:*")]
    sealed = await admin.get(entry)
    assert sealed is not None

    async def expired() -> bool:
        return await admin.exists(entry) == 0

    await eventually(expired, "the entry to expire in Redis")
    await admin.set(entry, sealed)  # replayed with no TTL at all
    assert state.cache is not None
    assert await state.cache.get(cache_key(identity())) is None
    assert await admin.exists(entry) == 0, "an expired value is not kept"


async def test_a_ciphertext_copied_under_another_key_prefix_is_a_miss(
    make_state: MakeState, admin: aioredis.Redis, prefix: str
) -> None:
    """Two gateways on one Redis and one key file must not be able to read each other's entries."""
    key = os.urandom(32)
    state = make_state(key=key)
    await cached(state, Backend(credential())).resolve(identity())
    (entry,) = [key async for key in admin.scan_iter(match=f"{prefix}c:*")]
    sealed = await admin.get(entry)
    assert sealed is not None and state.redis is not None and state.digest is not None
    other_prefix = f"{prefix}other:"
    other = RedisCacheStore(state.redis, other_prefix, key, state.digest)

    copied = entry.replace(prefix, other_prefix, 1)
    await admin.set(copied, sealed)
    assert await other.get(cache_key(identity())) is None
    # Positive control: the same store reads an entry it sealed itself.
    generation = await other.generation(cache_key(identity()))
    await other.put(cache_key(identity()), credential(), 30.0, generation)
    assert await other.get(cache_key(identity())) is not None


# --- what Redis holds -------------------------------------------------------------------


async def test_redis_holds_no_identifier_and_no_api_key_in_clear(
    make_state: MakeState, admin: aioredis.Redis, prefix: str
) -> None:
    a = make_state()
    await a.sessions.bind(UP, SESSION, subject_key())
    await a.sessions.check(UP, SESSION, subject_key())
    await cached(a, Backend(credential())).resolve(identity())
    await cached(a, Backend(credential())).invalidate(identity())
    await cached(a, Backend(credential())).resolve(identity())

    dump: list[str] = []
    async for key in admin.scan_iter(match=f"{prefix}*"):
        dump.append(key)
        kind = await admin.type(key)
        if kind == "string":
            dump.append(str(await admin.get(key)))
        elif kind == "zset":
            dump.extend(member for member, _ in await admin.zrange(key, 0, -1, withscores=True))
    assert len(dump) >= 6, "the scan must have found the bindings, the cache entry and the sets"
    everything = "\n".join(dump)
    for secret in (API_KEY, SUBJECT, EMAIL, SESSION, ISSUER, "client-x", UP + "\x1f"):
        assert secret not in everything
    assert {key.split(":")[-2] for key in dump if key.startswith(prefix)} <= {"s", "ss", "c", "cg"}


async def test_key_names_are_keyed_digests_never_a_plain_sha256_of_an_identifier(
    make_state: MakeState, admin: aioredis.Redis, prefix: str
) -> None:
    """A plain hash of a known subject or session id could be recomputed by anyone who can list keys."""

    def plain(*parts: str) -> str:
        return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()

    state = make_state()
    await state.sessions.bind(UP, SESSION, subject_key())
    await cached(state, Backend(credential())).resolve(identity())
    limits = RateLimitsConfig(
        per_subject=TokenBucketConfig(burst=5, per_minute=60.0),
        per_organization=TokenBucketConfig(burst=5, per_minute=60.0),
    )
    limiter = build_rate_limiter(limits, state)
    assert limiter is not None
    await limiter.acquire(
        [
            BucketSpec("subject", subject_key(), 5, 60.0),
            BucketSpec("organization", "client-x", 5, 60.0),
        ]
    )

    names = [key async for key in admin.scan_iter(match=f"{prefix}*")]
    guessable = {
        plain(UP, SESSION),
        plain(subject_key()),
        plain(canonical_cache_key(cache_key(identity()))),
        plain("client-x"),
    }
    assert len(names) == 5, "binding, subject set, cache entry and two buckets"
    for name in names:
        assert name.rsplit(":", 1)[-1] not in guessable, name


# --- failure ----------------------------------------------------------------------------


async def test_a_closed_port_raises_state_unavailable_within_the_timeout(
    make_state: MakeState,
) -> None:
    dead = make_state(url="redis://127.0.0.1:1/0", operation_timeout=1)
    started = time.monotonic()
    with pytest.raises(StateUnavailableError) as caught:
        await dead.sessions.check(UP, SESSION, subject_key())
    with pytest.raises(StateUnavailableError):
        await dead.sessions.bind(UP, SESSION, subject_key())
    with pytest.raises(StateUnavailableError):
        await cached(dead, Backend(credential())).resolve(identity())
    with pytest.raises(StateUnavailableError):
        await dead.ping()
    assert time.monotonic() - started < 4
    assert SESSION not in str(caught.value) and SUBJECT not in str(caught.value)


async def test_a_command_that_outlasts_the_operation_timeout_raises_state_unavailable(
    make_state: MakeState, admin: aioredis.Redis
) -> None:
    state = make_state(operation_timeout=0.5)
    await state.ping()
    await admin.execute_command("CLIENT", "PAUSE", 3000, "ALL")
    try:
        started = time.monotonic()
        with pytest.raises(StateUnavailableError):
            await state.ping()
        with pytest.raises(StateUnavailableError):
            await state.sessions.check(UP, SESSION, subject_key())
        assert time.monotonic() - started < 2.5
    finally:
        await admin.execute_command("CLIENT", "UNPAUSE")


async def test_the_state_recovers_when_redis_answers_again(
    make_state: MakeState, admin: aioredis.Redis
) -> None:
    state = make_state(operation_timeout=0.5)
    await admin.execute_command("CLIENT", "PAUSE", 3000, "ALL")
    try:
        with pytest.raises(StateUnavailableError):
            await state.ping()
    finally:
        await admin.execute_command("CLIENT", "UNPAUSE")
    await state.ping()
    assert await state.sessions.bind(UP, SESSION, subject_key())


def test_the_redis_client_and_prefix_are_exposed_for_other_features(
    make_state: MakeState, prefix: str
) -> None:
    state = make_state()
    assert state.redis is not None
    assert state.key_prefix == prefix


async def test_the_inner_resolver_is_not_called_when_the_cache_is_down(
    make_state: MakeState,
) -> None:
    backend = Backend(credential())
    dead = cached(make_state(url="redis://127.0.0.1:1/0", operation_timeout=0.5), backend)
    with pytest.raises(StateUnavailableError):
        await dead.resolve(identity())
    assert backend.calls == 0


# --- the connection pool ----------------------------------------------------------------


async def test_commands_beyond_the_pool_size_wait_for_a_connection(
    make_state: MakeState, prefix: str
) -> None:
    state = make_state(max_connections=2, operation_timeout=5)
    redis = state.redis
    assert redis is not None
    replies = await asyncio.gather(
        *(redis.run(redis.client.blpop([f"{prefix}missing"], timeout=0.1)) for _ in range(6))
    )
    assert replies == [None] * 6


async def test_readyz_waits_for_a_connection_while_the_pool_is_saturated(
    make_state: MakeState, admin: aioredis.Redis, prefix: str
) -> None:
    state = make_state(max_connections=2, operation_timeout=5)
    redis = state.redis
    assert redis is not None
    data = base_config_dict()
    app = create_app(
        config_from(data),
        resolver=Backend(credential()),
        http_transport=httpx.MockTransport(lambda request: httpx.Response(500)),
        state=state,
    )
    holders = [
        asyncio.create_task(redis.run(redis.client.blpop([f"{prefix}held"], timeout=1.0)))
        for _ in range(2)
    ]

    async def pool_is_saturated() -> bool:
        listing = await admin.execute_command("CLIENT", "LIST")
        return str(listing).count("cmd=blpop") >= 2

    await eventually(pool_is_saturated, "both pooled connections to block")
    async with client_for(app) as http:
        response = await http.get("/readyz")
    assert (response.status_code, response.json()) == (200, {"status": "ready"})
    await asyncio.gather(*holders)


# --- keys of the wrong type -------------------------------------------------------------


async def only_key(admin: aioredis.Redis, pattern: str) -> str:
    (key,) = [key async for key in admin.scan_iter(match=pattern)]
    return key


async def replace_with_wrong_type(admin: aioredis.Redis, key: str, kind: str) -> None:
    """Leave something else under ``key``: a string where a structure is read, or a hash."""
    await admin.delete(key)
    if kind == "string":
        await admin.set(key, "not a structure")
    else:
        await admin.hset(key, "field", "value")


async def test_a_session_set_of_the_wrong_type_is_state_unavailable_not_a_crash(
    make_state: MakeState, admin: aioredis.Redis, prefix: str
) -> None:
    state = make_state()
    await state.sessions.bind(UP, SESSION, subject_key())
    await replace_with_wrong_type(admin, await only_key(admin, f"{prefix}ss:*"), "string")
    with pytest.raises(StateUnavailableError):
        await state.sessions.check(UP, SESSION, subject_key())
    with pytest.raises(StateUnavailableError):
        await state.sessions.bind(UP, "another-session", subject_key())


async def test_a_session_binding_of_the_wrong_type_is_state_unavailable(
    make_state: MakeState, admin: aioredis.Redis, prefix: str
) -> None:
    state = make_state()
    await state.sessions.bind(UP, SESSION, subject_key())
    await replace_with_wrong_type(admin, await only_key(admin, f"{prefix}s:*"), "hash")
    with pytest.raises(StateUnavailableError):
        await state.sessions.check(UP, SESSION, subject_key())


async def test_a_cache_generation_of_the_wrong_type_is_state_unavailable(
    make_state: MakeState, admin: aioredis.Redis, prefix: str
) -> None:
    state = make_state()
    backend = Backend(credential())
    await cached(state, backend).invalidate(identity())
    await replace_with_wrong_type(admin, await only_key(admin, f"{prefix}cg:*"), "hash")
    with pytest.raises(StateUnavailableError):
        await cached(state, backend).resolve(identity())
    assert backend.calls == 0, "the backend is not asked around a broken cache"


async def test_a_rate_limit_bucket_of_the_wrong_type_is_state_unavailable(
    make_state: MakeState, admin: aioredis.Redis, prefix: str
) -> None:
    state = make_state()
    limits = RateLimitsConfig(
        per_subject=TokenBucketConfig(burst=5, per_minute=60.0), per_organization=None
    )
    limiter = build_rate_limiter(limits, state)
    assert limiter is not None
    spec = BucketSpec("subject", subject_key(), 5, 60.0)
    assert (await limiter.acquire([spec])).allowed
    await replace_with_wrong_type(admin, await only_key(admin, f"{prefix}rl:s:*"), "string")
    with pytest.raises(StateUnavailableError):
        await limiter.acquire([spec])
