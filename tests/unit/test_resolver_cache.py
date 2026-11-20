import asyncio

import pytest
from pydantic import SecretStr

from mcp_gateway.config import ResolverCacheConfig
from mcp_gateway.identity import Identity
from mcp_gateway.resolver import CachingResolver, Credential, Denied, ResolverError
from mcp_gateway.resolver.base import CredentialResolver


def identity(subject: str = "u1", groups: tuple[str, ...] = ()) -> Identity:
    return Identity(subject=subject, email=None, organization="o", groups=groups, claims={})


def credential(ttl: float | None = None, key: str = "fake-alien-key-one") -> Credential:
    return Credential(api_key=SecretStr(key), upstream="primary", labels={}, ttl_seconds=ttl)


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class Backend:
    """A resolver with scripted answers that counts calls."""

    def __init__(self, *outcomes: Credential | Denied | Exception) -> None:
        self.outcomes = list(outcomes)
        self.calls = 0
        self.invalidated: list[Identity] = []
        self.closed = False
        self.gate: asyncio.Event | None = None

    async def resolve(self, identity: Identity) -> Credential | Denied:
        self.calls += 1
        if self.gate is not None:
            await self.gate.wait()
        outcome = self.outcomes[min(self.calls, len(self.outcomes)) - 1]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    async def invalidate(self, identity: Identity) -> None:
        self.invalidated.append(identity)

    async def aclose(self) -> None:
        self.closed = True


def build(
    backend: Backend, *, positive: float = 60, negative: float = 5, max_entries: int = 100
) -> tuple[CachingResolver, Clock]:
    clock = Clock()
    config = ResolverCacheConfig(
        positive_ttl_seconds=positive, negative_ttl_seconds=negative, max_entries=max_entries
    )
    inner: CredentialResolver = backend
    return CachingResolver(inner, config, clock), clock


async def test_positive_hit_then_expiry() -> None:
    backend = Backend(credential())
    cache, clock = build(backend, positive=60)
    await cache.resolve(identity())
    await cache.resolve(identity())
    assert backend.calls == 1
    clock.now += 61
    await cache.resolve(identity())
    assert backend.calls == 2


async def test_credential_ttl_caps_the_positive_ttl_but_never_extends_it() -> None:
    backend = Backend(credential(ttl=10), credential(ttl=9999))
    cache, clock = build(backend, positive=60)
    await cache.resolve(identity("a"))
    clock.now += 11
    await cache.resolve(identity("a"))
    assert backend.calls == 2  # 10 s from the credential beat the 60 s default
    clock.now += 61
    await cache.resolve(identity("a"))
    assert backend.calls == 3  # 9999 s did not extend the configured 60 s


async def test_denials_are_cached_briefly() -> None:
    backend = Backend(Denied("no_mapping"), credential())
    cache, clock = build(backend, positive=60, negative=5)
    assert await cache.resolve(identity()) == Denied("no_mapping")
    assert await cache.resolve(identity()) == Denied("no_mapping")
    assert backend.calls == 1
    clock.now += 6
    assert isinstance(await cache.resolve(identity()), Credential)
    assert backend.calls == 2


async def test_errors_are_never_cached() -> None:
    backend = Backend(ResolverError("boom"), credential())
    cache, _ = build(backend)
    with pytest.raises(ResolverError):
        await cache.resolve(identity())
    assert len(cache) == 0
    assert isinstance(await cache.resolve(identity()), Credential)
    assert backend.calls == 2


async def test_key_covers_subject_email_organization_and_sorted_groups() -> None:
    backend = Backend(credential())
    cache, _ = build(backend)
    await cache.resolve(identity(groups=("a", "b")))
    await cache.resolve(identity(groups=("b", "a")))
    assert backend.calls == 1
    await cache.resolve(identity(groups=("a",)))
    await cache.resolve(
        Identity(subject="u1", email="x@y", organization="o", groups=("a", "b"), claims={})
    )
    await cache.resolve(
        Identity(subject="u1", email=None, organization="p", groups=("a", "b"), claims={})
    )
    await cache.resolve(identity("u2", groups=("a", "b")))
    assert backend.calls == 5


async def test_single_flight_shares_one_backend_call() -> None:
    backend = Backend(credential())
    backend.gate = asyncio.Event()
    cache, _ = build(backend)
    tasks = [asyncio.create_task(cache.resolve(identity())) for _ in range(10)]
    await asyncio.sleep(0.05)
    assert backend.calls == 1
    backend.gate.set()
    outcomes = await asyncio.gather(*tasks)
    assert backend.calls == 1
    assert all(o is outcomes[0] for o in outcomes)


async def test_single_flight_shares_an_error_and_forgets_it() -> None:
    backend = Backend(ResolverError("down"), credential())
    backend.gate = asyncio.Event()
    cache, _ = build(backend)
    tasks = [asyncio.create_task(cache.resolve(identity())) for _ in range(3)]
    await asyncio.sleep(0.02)
    backend.gate.set()
    results = await asyncio.gather(*tasks, return_exceptions=True)
    assert all(isinstance(r, ResolverError) for r in results)
    assert backend.calls == 1
    assert isinstance(await cache.resolve(identity()), Credential)


async def test_one_waiter_leaving_does_not_cancel_the_shared_call() -> None:
    backend = Backend(credential())
    backend.gate = asyncio.Event()
    cache, _ = build(backend)
    first = asyncio.create_task(cache.resolve(identity()))
    second = asyncio.create_task(cache.resolve(identity()))
    await asyncio.sleep(0.02)
    first.cancel()
    await asyncio.sleep(0.02)
    backend.gate.set()
    assert isinstance(await second, Credential)
    assert backend.calls == 1


async def test_last_waiter_leaving_cancels_the_backend_call() -> None:
    backend = Backend(credential())
    backend.gate = asyncio.Event()
    cache, _ = build(backend)
    only = asyncio.create_task(cache.resolve(identity()))
    await asyncio.sleep(0.02)
    only.cancel()
    await asyncio.sleep(0.02)
    backend.gate = None
    assert isinstance(await cache.resolve(identity()), Credential)
    assert backend.calls == 2


async def test_a_resolve_right_after_the_last_waiter_left_starts_a_fresh_call() -> None:
    backend = Backend(credential())
    backend.gate = asyncio.Event()
    cache, _ = build(backend)
    only = asyncio.create_task(cache.resolve(identity()))
    await asyncio.sleep(0.02)
    only.cancel()
    with pytest.raises(asyncio.CancelledError):
        await only
    # The cancelled backend call has not finished unwinding yet: a joiner must not inherit it.
    backend.gate = None
    assert isinstance(await asyncio.wait_for(cache.resolve(identity()), timeout=2), Credential)
    assert backend.calls == 2


async def test_invalidate_drops_the_entry_and_tells_the_inner_resolver() -> None:
    backend = Backend(credential(key="fake-alien-key-old"), credential(key="fake-alien-key-new"))
    cache, _ = build(backend)
    first = await cache.resolve(identity())
    await cache.invalidate(identity())
    second = await cache.resolve(identity())
    assert isinstance(first, Credential) and isinstance(second, Credential)
    assert second.api_key.get_secret_value() == "fake-alien-key-new"
    assert backend.invalidated == [identity()]


async def test_invalidate_discards_a_lookup_already_in_flight() -> None:
    backend = Backend(credential(key="fake-alien-key-stale"), credential(key="fake-alien-key-fresh"))
    backend.gate = asyncio.Event()
    cache, _ = build(backend)
    stale = asyncio.create_task(cache.resolve(identity()))
    await asyncio.sleep(0.02)
    await cache.invalidate(identity())
    fresh = asyncio.create_task(cache.resolve(identity()))
    await asyncio.sleep(0.02)
    backend.gate.set()
    await asyncio.gather(stale, fresh)
    assert backend.calls == 2
    # The cached entry is the fresh one, not the stale lookup that finished later or earlier.
    cached = await cache.resolve(identity())
    assert isinstance(cached, Credential)
    assert cached.api_key.get_secret_value() == "fake-alien-key-fresh"


async def test_size_is_bounded_and_least_recently_used_goes_first() -> None:
    backend = Backend(credential())
    cache, _ = build(backend, max_entries=2)
    await cache.resolve(identity("a"))
    await cache.resolve(identity("b"))
    await cache.resolve(identity("a"))  # a is now most recent
    await cache.resolve(identity("c"))  # evicts b
    assert len(cache) == 2
    assert backend.calls == 3
    await cache.resolve(identity("a"))
    assert backend.calls == 3
    await cache.resolve(identity("b"))
    assert backend.calls == 4


async def test_aclose_closes_the_wrapped_resolver() -> None:
    backend = Backend(credential())
    cache, _ = build(backend)
    await cache.aclose()
    assert backend.closed
