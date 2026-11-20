import asyncio
import json
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import psycopg
import pytest

from mcp_gateway.config import StoreSqlResolverConfig
from mcp_gateway.identity import Identity
from mcp_gateway.resolver import (
    Credential,
    Denied,
    ResolverError,
    ResolverUnavailableError,
    SqlStoreResolver,
)

pytestmark = pytest.mark.integration

UPSTREAMS = ["main", "other"]
DEFAULT_QUERY = (
    "SELECT api_key, upstream, labels FROM p3_mapping "
    "WHERE email = %(email)s OR subject = %(subject)s OR organization = %(organization)s"
)


def identity(
    subject: str = "user-1", email: str | None = "alice@x.example", org: str | None = "client-x"
) -> Identity:
    return Identity(subject=subject, email=email, organization=org, groups=(), claims={})


def sql_config(dsn_file: Path, **overrides: Any) -> StoreSqlResolverConfig:
    data: dict[str, Any] = {
        "type": "store_sql",
        "dsn_file": str(dsn_file),
        "query": DEFAULT_QUERY,
        "default_upstream": "main",
        "pool_min_size": 1,
        "pool_max_size": 2,
        "connect_timeout_seconds": 2,
        "statement_timeout_seconds": 2,
    }
    data.update(overrides)
    return StoreSqlResolverConfig.model_validate_json(json.dumps(data), strict=True)


@pytest.fixture
async def table(pg_dsn: str) -> AsyncIterator[None]:
    async with await psycopg.AsyncConnection.connect(pg_dsn, autocommit=True) as conn:
        await conn.execute("DROP TABLE IF EXISTS p3_mapping")
        await conn.execute(
            "CREATE TABLE p3_mapping (subject text, email text, organization text, "
            "api_key text, upstream text, labels jsonb)"
        )
        await conn.execute(
            "INSERT INTO p3_mapping VALUES "
            "('s1', 'alice@x.example', NULL, 'fake-alien-key-alice', NULL, '{\"client\": \"x\"}'), "
            "('s2', 'bob@x.example', NULL, 'fake-alien-key-bob', 'other', NULL), "
            "('s3', 'dup@x.example', NULL, 'fake-alien-key-d1', NULL, NULL), "
            "('s4', 'dup@x.example', NULL, 'fake-alien-key-d2', NULL, NULL), "
            "('s5', 'bad@x.example', NULL, 'fake-alien-key-bad', 'nowhere', NULL), "
            "('s6', 'space@x.example', NULL, 'has space', NULL, NULL), "
            "('s7', 'labels@x.example', NULL, 'fake-alien-key-l', NULL, '{\"k\": 1}')"
        )
        yield
        await conn.execute("DROP TABLE IF EXISTS p3_mapping")


@pytest.fixture
def dsn_file(tmp_path: Path, pg_dsn: str) -> Path:
    path = tmp_path / "dsn"
    path.write_text(pg_dsn + "\n")
    return path


async def resolver_for(dsn_file: Path, **overrides: Any) -> SqlStoreResolver:
    return SqlStoreResolver(sql_config(dsn_file, **overrides), UPSTREAMS)


@pytest.mark.usefixtures("table")
async def test_allow_with_default_upstream_and_labels(dsn_file: Path) -> None:
    resolver = await resolver_for(dsn_file)
    try:
        outcome = await resolver.resolve(identity(subject="nobody", org=None))
    finally:
        await resolver.aclose()
    assert isinstance(outcome, Credential)
    assert outcome.api_key.get_secret_value() == "fake-alien-key-alice"
    assert outcome.upstream == "main"
    assert dict(outcome.labels) == {"client": "x"}
    assert outcome.ttl_seconds is None


@pytest.mark.usefixtures("table")
async def test_named_upstream_and_null_labels(dsn_file: Path) -> None:
    resolver = await resolver_for(dsn_file)
    try:
        outcome = await resolver.resolve(identity(subject="x", email="bob@x.example", org=None))
    finally:
        await resolver.aclose()
    assert isinstance(outcome, Credential)
    assert (outcome.upstream, dict(outcome.labels)) == ("other", {})


@pytest.mark.usefixtures("table")
async def test_no_row_is_no_mapping_and_null_parameters_match_nothing(dsn_file: Path) -> None:
    resolver = await resolver_for(dsn_file)
    try:
        assert await resolver.resolve(identity("nobody", "nobody@x", "none")) == Denied(
            "no_mapping"
        )
        # NULL = NULL is not true, so an identity with no email and no org matches no row.
        assert await resolver.resolve(identity("nobody", None, None)) == Denied("no_mapping")
    finally:
        await resolver.aclose()


@pytest.mark.usefixtures("table")
async def test_several_rows_are_ambiguous(dsn_file: Path) -> None:
    resolver = await resolver_for(dsn_file)
    try:
        outcome = await resolver.resolve(identity("x", "dup@x.example", None))
    finally:
        await resolver.aclose()
    assert outcome == Denied("ambiguous_mapping")


@pytest.mark.usefixtures("table")
async def test_values_are_bound_never_interpolated(dsn_file: Path) -> None:
    resolver = await resolver_for(dsn_file)
    try:
        hostile = "x' OR '1'='1"
        assert await resolver.resolve(identity(hostile, hostile, hostile)) == Denied("no_mapping")
    finally:
        await resolver.aclose()


@pytest.mark.usefixtures("table")
@pytest.mark.parametrize("email", ["bad@x.example", "space@x.example", "labels@x.example"])
async def test_bad_rows_are_resolver_errors_without_the_key(dsn_file: Path, email: str) -> None:
    resolver = await resolver_for(dsn_file)
    try:
        with pytest.raises(ResolverError) as caught:
            await resolver.resolve(identity("x", email, None))
    finally:
        await resolver.aclose()
    assert not isinstance(caught.value, ResolverUnavailableError)
    assert "oat_" not in str(caught.value) and "has space" not in str(caught.value)


@pytest.mark.usefixtures("table")
async def test_missing_columns_are_a_resolver_error(dsn_file: Path) -> None:
    resolver = await resolver_for(
        dsn_file,
        query="SELECT api_key FROM p3_mapping WHERE email = %(email)s",
    )
    try:
        with pytest.raises(ResolverError, match="columns"):
            await resolver.resolve(identity())
    finally:
        await resolver.aclose()


@pytest.mark.usefixtures("table")
async def test_a_broken_query_is_a_resolver_error_not_unavailable(dsn_file: Path) -> None:
    resolver = await resolver_for(
        dsn_file, query="SELECT api_key, upstream, labels FROM nope WHERE a = %(subject)s"
    )
    try:
        with pytest.raises(ResolverError) as caught:
            await resolver.resolve(identity())
    finally:
        await resolver.aclose()
    assert not isinstance(caught.value, ResolverUnavailableError)


@pytest.mark.usefixtures("table")
async def test_the_transaction_is_read_only_and_the_timeout_is_transaction_local(
    dsn_file: Path,
) -> None:
    # One pooled connection reused for every call: settings must not stick to the session.
    resolver = await resolver_for(
        dsn_file,
        pool_max_size=1,
        query=(
            "SELECT current_setting('transaction_read_only') AS api_key, NULL AS upstream, "
            "NULL AS labels WHERE %(subject)s::text IS NOT NULL"
        ),
        statement_timeout_seconds=7,
    )
    try:
        outcome = await resolver.resolve(identity())
        assert isinstance(outcome, Credential)
        assert outcome.api_key.get_secret_value() == "on"
    finally:
        await resolver.aclose()
    timeout_probe = await resolver_for(
        dsn_file,
        pool_max_size=1,
        query=(
            "SELECT current_setting('statement_timeout') AS api_key, NULL AS upstream, "
            "NULL AS labels WHERE %(subject)s::text IS NOT NULL"
        ),
        statement_timeout_seconds=7,
    )
    try:
        outcome = await timeout_probe.resolve(identity())
        assert isinstance(outcome, Credential)
        assert outcome.api_key.get_secret_value() == "7s"
    finally:
        await timeout_probe.aclose()


@pytest.mark.usefixtures("table")
async def test_writes_are_refused_by_the_read_only_transaction(dsn_file: Path) -> None:
    resolver = await resolver_for(
        dsn_file,
        query=(
            "SELECT nextval('p3_seq')::text AS api_key, NULL AS upstream, NULL AS labels "
            "WHERE %(subject)s::text IS NOT NULL"
        ),
    )
    try:
        async with await psycopg.AsyncConnection.connect(
            dsn_file.read_text().strip(), autocommit=True
        ) as conn:
            await conn.execute("CREATE SEQUENCE IF NOT EXISTS p3_seq")
            try:
                with pytest.raises(ResolverError, match="ReadOnlySqlTransaction"):
                    await resolver.resolve(identity())
            finally:
                await conn.execute("DROP SEQUENCE IF EXISTS p3_seq")
    finally:
        await resolver.aclose()


@pytest.mark.usefixtures("table")
async def test_statement_timeout_is_unavailable_and_bounded(dsn_file: Path) -> None:
    resolver = await resolver_for(
        dsn_file,
        query=(
            "SELECT pg_sleep(5)::text AS api_key, NULL AS upstream, NULL AS labels "
            "WHERE %(subject)s::text IS NOT NULL"
        ),
        statement_timeout_seconds=0.3,
    )
    started = time.monotonic()
    try:
        with pytest.raises(ResolverUnavailableError):
            await resolver.resolve(identity())
    finally:
        await resolver.aclose()
    assert time.monotonic() - started < 3


async def test_an_unreachable_database_is_unavailable_and_the_dsn_is_not_leaked(
    tmp_path: Path,
) -> None:
    dsn = tmp_path / "dsn"
    dsn.write_text("postgresql://u:hunter2-password@127.0.0.1:1/db\n")
    resolver = await resolver_for(dsn, connect_timeout_seconds=1)
    started = time.monotonic()
    try:
        with pytest.raises(ResolverUnavailableError) as caught:
            await resolver.resolve(identity())
    finally:
        await resolver.aclose()
    assert time.monotonic() - started < 4
    assert "hunter2" not in str(caught.value)


@pytest.mark.usefixtures("table")
async def test_many_sequential_and_concurrent_lookups_share_a_small_pool(dsn_file: Path) -> None:
    resolver = await resolver_for(dsn_file)
    try:
        outcomes = await asyncio.gather(*(resolver.resolve(identity()) for _ in range(20)))
    finally:
        await resolver.aclose()
    assert all(isinstance(o, Credential) for o in outcomes)


def test_missing_dsn_file_refuses_startup(tmp_path: Path) -> None:
    with pytest.raises(ResolverError, match="unreadable"):
        SqlStoreResolver(sql_config(tmp_path / "absent"), UPSTREAMS)


def test_default_upstream_must_exist(dsn_file: Path) -> None:
    with pytest.raises(ResolverError, match="default_upstream"):
        SqlStoreResolver(sql_config(dsn_file, default_upstream="nowhere"), UPSTREAMS)


@pytest.mark.usefixtures("table")
async def test_no_statement_is_ever_prepared_on_the_server(dsn_file: Path) -> None:
    # One connection, so every lookup reuses the same server session.
    resolver = await resolver_for(dsn_file, pool_min_size=1, pool_max_size=1)
    try:
        for _ in range(12):  # psycopg would auto-prepare from the sixth execution on
            assert isinstance(
                await resolver.resolve(identity(subject="nobody", org=None)), Credential
            )
        async with resolver._pool.connection() as conn:
            cursor = await conn.execute("SELECT count(*) FROM pg_prepared_statements")
            row = await cursor.fetchone()
        assert row == (0,)
    finally:
        await resolver.aclose()


@pytest.mark.usefixtures("table")
async def test_a_connection_dropped_while_idle_is_replaced_at_checkout(
    tmp_path: Path, pg_dsn: str
) -> None:
    # A named application lets the test kill the pool's connections and nothing else.
    named = tmp_path / "named-dsn"
    named.write_text(f"{pg_dsn}{'&' if '?' in pg_dsn else '?'}application_name=p3pool\n")
    resolver = await resolver_for(named, pool_min_size=1, pool_max_size=1)
    try:
        assert isinstance(await resolver.resolve(identity(subject="nobody", org=None)), Credential)
        async with await psycopg.AsyncConnection.connect(pg_dsn, autocommit=True) as admin:
            await admin.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE pid <> pg_backend_pid() AND datname = current_database() "
                "AND application_name = 'p3pool'"
            )
        await asyncio.sleep(0.2)
        assert isinstance(await resolver.resolve(identity(subject="nobody", org=None)), Credential)
    finally:
        await resolver.aclose()
