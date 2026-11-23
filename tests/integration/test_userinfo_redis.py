"""The userinfo cache against a real Redis: two ``State`` objects on one server are two replicas."""

import asyncio
import base64
import os
import re
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any

import anyio
import pytest
import redis.asyncio as aioredis
from cryptography.hazmat.primitives.asymmetric import rsa
from unit.conftest import KID, FakeIdp, FakeUpstream, base_config_dict, config_from, mint
from unit.test_app import RPC_BODY, bearer, client_for
from unit.userinfo_support import (
    DOCUMENT,
    FakeUserinfo,
    RecordingResolver,
    json_response,
    userinfo_dict,
    userinfo_transport,
)

from mcp_gateway.app import create_app
from mcp_gateway.identity import Profile
from mcp_gateway.state import State, StateUnavailableError, build_state

pytestmark = pytest.mark.integration

TOKEN = "header.payload.signature-integration-token"
SUBJECT = "alice-subject-id"
EMAIL = "alice@client-x.example"
ORGANIZATION = "client-x"
GROUP = "group-integration-7"
PROFILE = Profile(email=EMAIL, organization=ORGANIZATION, groups=(GROUP,))
DEADLINE_SECONDS = 10.0

type MakeState = Callable[..., State]


@pytest.fixture(scope="session")
def rsa_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def idp(rsa_key: rsa.RSAPrivateKey) -> FakeIdp:
    return FakeIdp({KID: rsa_key})


@pytest.fixture
def upstream() -> FakeUpstream:
    return FakeUpstream()


@pytest.fixture
def prefix() -> str:
    # Every test owns a key prefix, so nothing here touches other data in the server.
    return f"uitest:{uuid.uuid4().hex}:"


@pytest.fixture
async def admin(redis_url: str, prefix: str) -> AsyncIterator[aioredis.Redis]:
    client = aioredis.Redis.from_url(redis_url, decode_responses=True)
    yield client
    keys = [key async for key in client.scan_iter(match=f"{prefix}*")]
    if keys:
        await client.delete(*keys)
    await client.aclose()


def replica_config(
    tmp_path: Path, redis_url: str, prefix: str, key: bytes, index: int, operation_timeout: float
) -> dict[str, Any]:
    (tmp_path / f"url-{index}").write_text(redis_url + "\n")
    (tmp_path / f"key-{index}").write_text(base64.b64encode(key).decode() + "\n")
    data = base_config_dict()
    data["auth"]["userinfo"] = userinfo_dict(max_entries=None)
    data["sessions"] = {"max_entries": None, "max_entries_per_subject": 20, "idle_ttl_seconds": 600}
    data["state"] = {
        "type": "redis",
        "url_file": str(tmp_path / f"url-{index}"),
        "key_prefix": prefix,
        "connect_timeout_seconds": 1,
        "operation_timeout_seconds": operation_timeout,
        "max_connections": 4,
        "encryption_key_file": str(tmp_path / f"key-{index}"),
    }
    return data


@pytest.fixture
async def make_state(tmp_path: Path, redis_url: str, prefix: str) -> AsyncIterator[MakeState]:
    """Build a replica. ``key`` is its encryption key; replicas that share Redis share it."""
    built: list[State] = []
    shared_key = os.urandom(32)

    def make(
        *, url: str | None = None, key: bytes | None = None, operation_timeout: float = 2
    ) -> State:
        data = replica_config(
            tmp_path, url or redis_url, prefix, key or shared_key, len(built), operation_timeout
        )
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


async def entry_keys(admin: aioredis.Redis, prefix: str) -> list[str]:
    return [key async for key in admin.scan_iter(match=f"{prefix}u:*")]


def store_of(state: State) -> Any:
    assert state.userinfo is not None
    return state.userinfo


async def test_a_profile_round_trips_and_a_second_replica_reads_it(make_state: MakeState) -> None:
    writer, reader = store_of(make_state()), store_of(make_state())
    assert await writer.get(TOKEN) is None
    await writer.put(TOKEN, PROFILE, 60)
    assert await reader.get(TOKEN) == PROFILE
    assert await reader.get("another-token") is None


async def test_the_key_is_prefix_u_and_a_digest_with_no_token_or_subject(
    make_state: MakeState, admin: aioredis.Redis, prefix: str
) -> None:
    await store_of(make_state()).put(TOKEN, PROFILE, 60)
    (name,) = await entry_keys(admin, prefix)
    assert re.fullmatch(rf"{re.escape(prefix)}u:[0-9a-f]{{64}}", name)
    for secret in (TOKEN, SUBJECT, EMAIL, ORGANIZATION, "signature-integration-token"):
        assert secret not in name
    assert [key async for key in admin.scan_iter(match=f"{prefix}*")] == [name]


async def test_the_stored_value_is_ciphertext(
    make_state: MakeState, admin: aioredis.Redis, prefix: str
) -> None:
    await store_of(make_state()).put(TOKEN, PROFILE, 60)
    (name,) = await entry_keys(admin, prefix)
    value = str(await admin.get(name))
    raw = base64.b64decode(value, validate=True)
    for secret in (EMAIL, ORGANIZATION, GROUP, SUBJECT, TOKEN, "client-x"):
        assert secret not in value
        assert secret.encode() not in raw


async def test_the_key_carries_a_ttl_no_longer_than_the_one_asked(
    make_state: MakeState, admin: aioredis.Redis, prefix: str
) -> None:
    await store_of(make_state()).put(TOKEN, PROFILE, 30)
    (name,) = await entry_keys(admin, prefix)
    remaining = await admin.pttl(name)
    assert 0 < remaining <= 30_000


async def test_a_tampered_value_is_dropped_as_a_miss(
    make_state: MakeState, admin: aioredis.Redis, prefix: str
) -> None:
    store = store_of(make_state())
    await store.put(TOKEN, PROFILE, 60)
    (name,) = await entry_keys(admin, prefix)
    raw = bytearray(base64.b64decode(str(await admin.get(name))))
    raw[-1] ^= 0x01
    await admin.set(name, base64.b64encode(bytes(raw)).decode("ascii"))
    assert await store.get(TOKEN) is None
    assert await admin.exists(name) == 0


async def test_garbage_in_an_entry_is_a_miss_and_is_deleted(
    make_state: MakeState, admin: aioredis.Redis, prefix: str
) -> None:
    store = store_of(make_state())
    await store.put(TOKEN, PROFILE, 60)
    (name,) = await entry_keys(admin, prefix)
    await admin.set(name, "!!! not base64 !!!")
    assert await store.get(TOKEN) is None
    assert await admin.exists(name) == 0


async def test_a_value_sealed_by_another_key_is_a_miss_and_is_left_alone(
    make_state: MakeState, admin: aioredis.Redis, prefix: str
) -> None:
    writer = store_of(make_state(key=os.urandom(32)))
    await writer.put(TOKEN, PROFILE, 60)
    (writer_name,) = await entry_keys(admin, prefix)
    sealed = str(await admin.get(writer_name))
    reader = store_of(make_state(key=os.urandom(32)))
    await reader.put(TOKEN, PROFILE, 60)
    (reader_name,) = [name for name in await entry_keys(admin, prefix) if name != writer_name]
    await admin.set(reader_name, sealed)
    assert await reader.get(TOKEN) is None
    assert await admin.get(reader_name) == sealed, "the other key's entry must not be deleted"
    assert await writer.get(TOKEN) == PROFILE


async def test_the_sealed_expiry_is_honoured_when_redis_kept_the_entry_alive(
    make_state: MakeState, admin: aioredis.Redis, prefix: str
) -> None:
    store = store_of(make_state())
    await store.put(TOKEN, PROFILE, 1)
    (name,) = await entry_keys(admin, prefix)
    # A replay without its TTL: Redis would keep the value for ever, the sealed expiry must not.
    await admin.set(name, str(await admin.get(name)))
    assert await admin.pttl(name) == -1

    async def refused() -> bool:
        return await store.get(TOKEN) is None

    await eventually(refused, "the sealed expiry to pass")
    assert await admin.exists(name) == 0


async def test_an_unreachable_redis_is_state_unavailable_for_both_operations(
    make_state: MakeState,
) -> None:
    store = store_of(make_state(url="redis://127.0.0.1:1/0", operation_timeout=1))
    with pytest.raises(StateUnavailableError):
        await store.get(TOKEN)
    with pytest.raises(StateUnavailableError):
        await store.put(TOKEN, PROFILE, 60)


async def test_two_replicas_share_one_idp_call_per_token_through_the_shared_cache(
    tmp_path: Path,
    redis_url: str,
    prefix: str,
    admin: aioredis.Redis,
    idp: FakeIdp,
    upstream: FakeUpstream,
    rsa_key: rsa.RSAPrivateKey,
) -> None:
    fake = FakeUserinfo()
    fake.responder = lambda _r: json_response(DOCUMENT)
    key = os.urandom(32)
    resolvers = [RecordingResolver(), RecordingResolver()]
    apps = [
        create_app(
            config_from(replica_config(tmp_path, redis_url, prefix, key, index, 2)),
            resolver=resolvers[index],
            http_transport=userinfo_transport(idp, upstream, fake),
        )
        for index in range(2)
    ]
    token = mint(rsa_key)
    for app in apps:
        async with client_for(app) as http:
            response = await http.post("/mcp", json=RPC_BODY, headers=bearer(token))
        assert response.status_code == 200
    assert fake.calls == 1, "the second replica must be served from the shared cache"
    assert [r.identities[0].email for r in resolvers] == ["alice@client-x.example"] * 2
    (name,) = await entry_keys(admin, prefix)
    value = str(await admin.get(name))
    assert "alice@client-x.example" not in value
    assert token not in value
