"""``store_sql`` resolver: look the key up in a PostgreSQL table the customer fills in.

The configured ``query`` is one SELECT using only the named parameters ``%(subject)s``,
``%(email)s`` and ``%(organization)s`` (null when the identity has none). It runs in a
read-only transaction with a statement timeout set for that transaction only (``set_config``
with ``is_local``, so nothing leaks onto a pooled or PgBouncer-shared session).

Columns read: ``api_key`` (text), ``upstream`` (text, nullable: null picks the configured
``default_upstream``) and ``labels`` (json/jsonb object of strings, nullable). No row is
``no_mapping``; more than one row is ``ambiguous_mapping``.
"""

import logging
from collections.abc import Collection
from typing import LiteralString, cast

import anyio
import psycopg
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from pydantic import ValidationError

from mcp_gateway.config import StoreSqlResolverConfig
from mcp_gateway.identity import Identity
from mcp_gateway.resolver.base import (
    BackendMapping,
    Credential,
    Denied,
    ResolverError,
    ResolverUnavailableError,
    build_credential,
    describe_validation_error,
    read_secret_file,
)

logger = logging.getLogger(__name__)

COLUMNS = ("api_key", "upstream", "labels")


class SqlStoreResolver:
    def __init__(self, config: StoreSqlResolverConfig, upstream_names: Collection[str]) -> None:
        if config.default_upstream not in upstream_names:
            raise ResolverError("store_sql default_upstream is not a configured upstream")
        self._config = config
        self._upstream_names = frozenset(upstream_names)
        # The text was validated as a single parameterised SELECT when the config was loaded.
        self._query: LiteralString = cast("LiteralString", config.query)
        dsn = read_secret_file(config.dsn_file, what="store_sql dsn")
        # Opened lazily on first use: a database that is down at boot must not stop the gateway,
        # it fails closed per request (503) until it is back.
        self._pool = AsyncConnectionPool(
            dsn,
            min_size=config.pool_min_size,
            max_size=config.pool_max_size,
            open=False,
            timeout=config.connect_timeout_seconds,
            kwargs={"connect_timeout": max(1, round(config.connect_timeout_seconds))},
        )
        self._open_lock = anyio.Lock()
        self._opened = False

    async def _ensure_open(self) -> None:
        if self._opened:
            return
        async with self._open_lock:
            if not self._opened:
                await self._pool.open(wait=False)
                self._opened = True

    async def _fetch(self, identity: Identity) -> list[dict[str, object]]:
        params = {
            "subject": identity.subject,
            "email": identity.email,
            "organization": identity.organization,
        }
        async with self._pool.connection() as conn, conn.transaction():
            # SET TRANSACTION must precede any other statement in the transaction.
            await conn.execute("SET TRANSACTION READ ONLY")
            await conn.execute(
                "SELECT set_config('statement_timeout', %s, true)",
                (str(int(self._config.statement_timeout_seconds * 1000)),),
            )
            async with conn.cursor(row_factory=dict_row) as cursor:
                await cursor.execute(self._query, params)
                return await cursor.fetchmany(2)

    async def resolve(self, identity: Identity) -> Credential | Denied:
        await self._ensure_open()
        try:
            with anyio.fail_after(
                self._config.connect_timeout_seconds + self._config.statement_timeout_seconds
            ):
                rows = await self._fetch(identity)
        except (TimeoutError, psycopg.OperationalError) as exc:
            # Includes pool checkout timeouts and statement timeouts (QueryCanceled).
            raise ResolverUnavailableError(
                f"store_sql backend unavailable ({type(exc).__name__})"
            ) from exc
        except psycopg.Error as exc:
            raise ResolverError(f"store_sql query failed ({type(exc).__name__})") from exc
        if not rows:
            return Denied("no_mapping")
        if len(rows) > 1:
            return Denied("ambiguous_mapping")
        return self._credential(rows[0])

    def _credential(self, row: dict[str, object]) -> Credential:
        missing = [name for name in COLUMNS if name not in row]
        if missing:
            raise ResolverError(f"store_sql query must return columns {', '.join(COLUMNS)}")
        try:
            mapping = BackendMapping.model_validate(
                {name: row[name] for name in COLUMNS}, strict=True
            )
        except ValidationError as exc:
            raise ResolverError(
                f"store_sql row rejected: {describe_validation_error(exc)}"
            ) from exc
        return build_credential(
            source="store_sql",
            api_key=mapping.api_key,
            upstream=mapping.upstream,
            labels=mapping.labels,
            default_upstream=self._config.default_upstream,
            upstream_names=self._upstream_names,
        )

    async def invalidate(self, identity: Identity) -> None:
        """Nothing is cached here; the cache wrapper owns invalidation."""
        del identity

    async def aclose(self) -> None:
        if self._opened:
            await self._pool.close()
