import asyncio
import json
import time
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import redis.asyncio as aioredis

from mcp_gateway.config import StoreRedisResolverConfig
from mcp_gateway.identity import Identity
from mcp_gateway.resolver import (
    Credential,
    Denied,
    RedisStoreResolver,
    ResolverError,
    ResolverUnavailableError,
)

pytestmark = pytest.mark.integration

UPSTREAMS = ["main", "other"]


def identity(
    subject: str = "user-1", email: str | None = "alice@x.example", org: str | None = "client-x"
) -> Identity:
    return Identity(subject=subject, email=email, organization=org, groups=(), claims={})


def redis_config(url_file: Path, prefix: str, **overrides: Any) -> StoreRedisResolverConfig:
    data: dict[str, Any] = {
        "type": "store_redis",
        "url_file": str(url_file),
        "key_pattern": f"{prefix}:{{organization}}:{{subject}}",
        "default_upstream": "main",
        "connect_timeout_seconds": 1,
        "socket_timeout_seconds": 1,
        "max_connections": 4,
    }
    data.update(overrides)
    return StoreRedisResolverConfig.model_validate_json(json.dumps(data), strict=True)


@pytest.fixture
def prefix() -> str:
    # Every test owns a key prefix, so nothing here touches other data in the server.
    return f"p3test:{uuid.uuid4().hex}"


@pytest.fixture
def url_file(tmp_path: Path, redis_url: str) -> Path:
    path = tmp_path / "redis-url"
    path.write_text(redis_url + "\n")
    return path


@pytest.fixture
async def admin(redis_url: str, prefix: str) -> AsyncIterator[aioredis.Redis]:
    client = aioredis.Redis.from_url(redis_url, decode_responses=True)
    yield client
    keys = [key async for key in client.scan_iter(match=f"{prefix}:*")]
    if keys:
        await client.delete(*keys)
    await client.aclose()


async def lookup(
    url_file: Path, prefix: str, who: Identity, **overrides: Any
) -> Credential | Denied:
    resolver = RedisStoreResolver(redis_config(url_file, prefix, **overrides), UPSTREAMS)
    try:
        return await resolver.resolve(who)
    finally:
        await resolver.aclose()


async def test_allow_with_default_upstream_and_labels(
    admin: aioredis.Redis, url_file: Path, prefix: str
) -> None:
    await admin.set(
        f"{prefix}:client-x:user-1",
        json.dumps(
            {"api_key": "fake-alien-key-alice", "upstream": None, "labels": {"client": "x"}}
        ),
    )
    outcome = await lookup(url_file, prefix, identity())
    assert isinstance(outcome, Credential)
    assert outcome.api_key.get_secret_value() == "fake-alien-key-alice"
    assert outcome.upstream == "main"
    assert dict(outcome.labels) == {"client": "x"}


async def test_named_upstream_and_null_labels(
    admin: aioredis.Redis, url_file: Path, prefix: str
) -> None:
    await admin.set(
        f"{prefix}:client-x:user-1",
        json.dumps({"api_key": "fake-alien-key-a", "upstream": "other", "labels": None}),
    )
    outcome = await lookup(url_file, prefix, identity())
    assert isinstance(outcome, Credential)
    assert (outcome.upstream, dict(outcome.labels)) == ("other", {})


async def test_missing_key_is_no_mapping(url_file: Path, prefix: str) -> None:
    assert await lookup(url_file, prefix, identity()) == Denied("no_mapping")


async def test_null_placeholder_is_no_mapping_without_a_lookup(
    admin: aioredis.Redis, url_file: Path, prefix: str
) -> None:
    # A key that would match if the empty organization were substituted must not be found.
    await admin.set(
        f"{prefix}::user-1",
        json.dumps({"api_key": "fake-alien-key-wrong", "upstream": None, "labels": None}),
    )
    assert await lookup(url_file, prefix, identity(org=None)) == Denied("no_mapping")


@pytest.mark.parametrize(
    "value",
    [
        "not json",
        "[]",
        json.dumps({"api_key": "fake-alien-key-a"}),
        json.dumps({"api_key": "fake-alien-key-a", "upstream": None, "labels": None, "extra": 1}),
        json.dumps({"api_key": 5, "upstream": None, "labels": None}),
        json.dumps({"api_key": "fake-alien-key-a", "upstream": None, "labels": {"k": 1}}),
        json.dumps({"api_key": "has space", "upstream": None, "labels": None}),
        json.dumps({"api_key": "fake-alien-key-a", "upstream": "nowhere", "labels": None}),
    ],
)
async def test_malformed_values_are_resolver_errors(
    admin: aioredis.Redis, url_file: Path, prefix: str, value: str
) -> None:
    await admin.set(f"{prefix}:client-x:user-1", value)
    with pytest.raises(ResolverError) as caught:
        await lookup(url_file, prefix, identity())
    assert not isinstance(caught.value, ResolverUnavailableError)
    assert "has space" not in str(caught.value) and "fake-alien-key-a" not in str(caught.value)


async def test_a_value_of_the_wrong_redis_type_is_a_resolver_error(
    admin: aioredis.Redis, url_file: Path, prefix: str
) -> None:
    await admin.rpush(f"{prefix}:client-x:user-1", "a")
    with pytest.raises(ResolverError) as caught:
        await lookup(url_file, prefix, identity())
    assert not isinstance(caught.value, ResolverUnavailableError)


async def test_unreachable_server_is_unavailable_and_the_url_is_not_leaked(
    tmp_path: Path, prefix: str
) -> None:
    path = tmp_path / "url"
    path.write_text("redis://:hunter2-password@127.0.0.1:1/0\n")
    started = time.monotonic()
    with pytest.raises(ResolverUnavailableError) as caught:
        await lookup(path, prefix, identity())
    assert time.monotonic() - started < 3
    assert "hunter2" not in str(caught.value)


async def test_a_server_that_never_answers_times_out_as_unavailable(
    tmp_path: Path, prefix: str
) -> None:
    async def accept_and_stay_silent(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        await asyncio.sleep(10)

    server = await asyncio.start_server(accept_and_stay_silent, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    path = tmp_path / "url"
    path.write_text(f"redis://127.0.0.1:{port}/0\n")
    started = time.monotonic()
    try:
        with pytest.raises(ResolverUnavailableError):
            await lookup(path, prefix, identity(), socket_timeout_seconds=0.3)
    finally:
        server.close()
    assert time.monotonic() - started < 3


def test_missing_url_file_refuses_startup(tmp_path: Path, prefix: str) -> None:
    with pytest.raises(ResolverError, match="unreadable"):
        RedisStoreResolver(redis_config(tmp_path / "absent", prefix), UPSTREAMS)


def test_invalid_url_refuses_startup_without_leaking(tmp_path: Path, prefix: str) -> None:
    path = tmp_path / "url"
    path.write_text("not-a-redis-url-secret\n")
    with pytest.raises(ResolverError) as caught:
        RedisStoreResolver(redis_config(path, prefix), UPSTREAMS)
    assert "secret" not in str(caught.value)


def test_default_upstream_must_exist(url_file: Path, prefix: str) -> None:
    with pytest.raises(ResolverError, match="default_upstream"):
        RedisStoreResolver(redis_config(url_file, prefix, default_upstream="nowhere"), UPSTREAMS)


async def test_values_with_separators_resolve_to_their_own_encoded_key(
    url_file: Path, prefix: str, admin: aioredis.Redis
) -> None:
    mine = json.dumps({"api_key": "fake-alien-key-mine", "upstream": None, "labels": None})
    other = json.dumps({"api_key": "fake-alien-key-other", "upstream": None, "labels": None})
    # "a:b" + "c" must not read the entry written for "a" + "b:c".
    await admin.set(f"{prefix}:a%3Ab:c", mine)
    await admin.set(f"{prefix}:a:b%3Ac", other)
    first = await lookup(url_file, prefix, identity(subject="c", org="a:b"))
    second = await lookup(url_file, prefix, identity(subject="b:c", org="a"))
    assert isinstance(first, Credential) and isinstance(second, Credential)
    assert first.api_key.get_secret_value() == "fake-alien-key-mine"
    assert second.api_key.get_secret_value() == "fake-alien-key-other"
    # The raw (unencoded) spelling is not a key the resolver ever reads.
    await admin.set(f"{prefix}:a:b:c", other)
    assert await lookup(url_file, prefix, identity(subject="c", org="a:b")) == first
