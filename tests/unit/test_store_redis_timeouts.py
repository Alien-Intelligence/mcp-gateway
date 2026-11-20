"""A Redis lookup is bounded by the configured timeouts (no hidden client retries)."""

import asyncio
import json
import time
from collections.abc import AsyncIterator
from contextlib import suppress
from pathlib import Path

import pytest

from mcp_gateway.config import StoreRedisResolverConfig
from mcp_gateway.identity import Identity
from mcp_gateway.resolver import RedisStoreResolver, ResolverUnavailableError

IDENTITY = Identity(subject="u", email=None, organization="o", groups=(), claims={})


@pytest.fixture
async def blackhole_port() -> AsyncIterator[int]:
    """A TCP server that accepts connections and never answers."""
    held: list[asyncio.StreamWriter] = []

    async def hold(_reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        held.append(writer)

    server = await asyncio.start_server(hold, "127.0.0.1", 0)
    yield server.sockets[0].getsockname()[1]
    for writer in held:
        writer.close()
    server.close()
    with suppress(Exception):
        await asyncio.wait_for(server.wait_closed(), timeout=1)


def resolver_for(tmp_path: Path, port: int) -> RedisStoreResolver:
    url_file = tmp_path / "url"
    url_file.write_text(f"redis://127.0.0.1:{port}/0\n")
    config = StoreRedisResolverConfig.model_validate_json(
        json.dumps(
            {
                "type": "store_redis",
                "url_file": str(url_file),
                "key_pattern": "k:{subject}",
                "default_upstream": "primary",
                "connect_timeout_seconds": 0.3,
                "socket_timeout_seconds": 0.3,
                "max_connections": 2,
            }
        ),
        strict=True,
    )
    return RedisStoreResolver(config, ["primary"])


async def test_a_dropped_connection_is_not_retried_by_the_client(tmp_path: Path) -> None:
    accepted = 0

    async def drop(_reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        nonlocal accepted
        accepted += 1
        writer.close()

    server = await asyncio.start_server(drop, "127.0.0.1", 0)
    resolver = resolver_for(tmp_path, server.sockets[0].getsockname()[1])
    try:
        with pytest.raises(ResolverUnavailableError):
            await resolver.resolve(IDENTITY)
    finally:
        await resolver.aclose()
        server.close()
    # The default redis-py client would connect four times (one try and three retries).
    assert accepted == 1


async def test_a_silent_server_fails_within_the_configured_timeouts(
    tmp_path: Path, blackhole_port: int
) -> None:
    resolver = resolver_for(tmp_path, blackhole_port)
    try:
        started = time.monotonic()
        with pytest.raises(ResolverUnavailableError):
            await resolver.resolve(IDENTITY)
        elapsed = time.monotonic() - started
    finally:
        await resolver.aclose()
    # connect + socket timeout is 0.6 s; the default client would retry three more times.
    assert elapsed < 0.9, elapsed


async def test_a_server_that_trickles_its_answer_cannot_outlast_the_deadline(
    tmp_path: Path,
) -> None:
    """Each byte arrives inside the socket timeout, so only the overall deadline can stop it."""

    async def trickle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await reader.read(1024)
            writer.write(b"$1000\r\n")
            for _ in range(1000):
                await writer.drain()
                writer.write(b"a")
                await asyncio.sleep(0.15)
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            writer.close()

    server = await asyncio.start_server(trickle, "127.0.0.1", 0)
    resolver = resolver_for(tmp_path, server.sockets[0].getsockname()[1])
    try:
        started = time.monotonic()
        with pytest.raises(ResolverUnavailableError):
            await asyncio.wait_for(resolver.resolve(IDENTITY), timeout=4)
        assert time.monotonic() - started < 1.5
    finally:
        await resolver.aclose()
        server.close()
