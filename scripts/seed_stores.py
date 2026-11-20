"""Load the PostgreSQL and Redis credential stores of the dev/stub stack.

Run once per stack start by the ``seed`` service. Reads ``stores.json`` and the key files from
the state directory, loads the stores, and writes the connection secrets the gateways and the
webhook service read (``pg.dsn``, ``redis.url``, ``hmac.secret``) into the connection
directory. Passwords come from the environment, never from argv. Idempotent: the table is
replaced and the Redis keys are rewritten.

The Redis key layout lives in :func:`redis_key` and must match the ``key_pattern`` of the
``store_redis`` resolver in ``compose.yaml``; the table layout must match its SQL query.
"""

import argparse
import base64
import json
import os
import secrets
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from urllib.parse import quote

import psycopg
import redis
from psycopg.types.json import Jsonb
from state_layout import KEY_FILE_MODE, STORES_FILE, write_atomic

DESCRIPTION = "Load the PostgreSQL and Redis credential stores and write the connection secrets."
POSTGRES_HOST = "postgres"
POSTGRES_USER = "gateway"
POSTGRES_DB = "gateway"
REDIS_HOST = "redis"
CONNECT_TIMEOUT_SECONDS = 10
REDIS_KEY_PREFIX = "mcp-gateway:cred:"
HA_KEY_PREFIX = "mcp-gateway:ha:"
STATE_KEY_BYTES = 32
# The port is closed: gateway-noredis must find no Redis there.
NOREDIS_URL = "redis://redis:1/0"

CREATE_TABLE = (
    "CREATE TABLE IF NOT EXISTS credentials ("
    "email text PRIMARY KEY, api_key text NOT NULL, upstream text, labels jsonb)"
)


def redis_key(email: str) -> str:
    """The Redis key of an identity; matches ``key_pattern`` ``mcp-gateway:cred:{email}``.

    The gateway percent-encodes every substituted value (``quote(value, safe="")``), so the
    store must be written the same way.
    """
    return f"{REDIS_KEY_PREFIX}{quote(email, safe='')}"


def ha_redis_key(email: str) -> str:
    """The Redis key of an identity in the replica pair's store; matches ``key_pattern``
    ``mcp-gateway:ha:{email}``."""
    return f"{HA_KEY_PREFIX}{quote(email, safe='')}"


def state_encryption_key() -> str:
    """A fresh base64 AES-256 key for the shared resolver cache."""
    return base64.b64encode(secrets.token_bytes(STATE_KEY_BYTES)).decode("ascii")


def require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"required environment variable {name} is not set")
    return value


def read_key(state_dir: Path, key_file: str) -> str:
    key = (state_dir / key_file).read_text(encoding="utf-8").strip()
    if not key:
        raise SystemExit(f"key file {key_file} is empty")
    return key


def seed_postgres(dsn: str, state_dir: Path, rows: list[dict[str, Any]]) -> None:
    with psycopg.connect(dsn, connect_timeout=CONNECT_TIMEOUT_SECONDS) as conn:
        conn.execute(CREATE_TABLE)
        conn.execute("TRUNCATE credentials")
        for row in rows:
            conn.execute(
                "INSERT INTO credentials (email, api_key, upstream, labels) VALUES (%s, %s, %s, %s)",
                (
                    row["email"],
                    read_key(state_dir, row["key_file"]),
                    row["upstream"],
                    Jsonb(row["labels"]),
                ),
            )


def _store_value(state_dir: Path, row: dict[str, Any]) -> str:
    return json.dumps(
        {
            "api_key": read_key(state_dir, row["key_file"]),
            "upstream": row["upstream"],
            "labels": row["labels"],
        }
    )


def seed_redis(
    url: str,
    state_dir: Path,
    chain_rows: list[dict[str, Any]],
    ha_rows: list[dict[str, Any]],
) -> None:
    client = redis.Redis.from_url(
        url, socket_connect_timeout=CONNECT_TIMEOUT_SECONDS, socket_timeout=CONNECT_TIMEOUT_SECONDS
    )
    try:
        for prefix in (REDIS_KEY_PREFIX, HA_KEY_PREFIX):
            for stale in client.scan_iter(match=f"{prefix}*"):
                client.delete(stale)
        for row in chain_rows:
            client.set(redis_key(row["email"]), _store_value(state_dir, row))
        for row in ha_rows:
            client.set(ha_redis_key(row["email"]), _store_value(state_dir, row))
    finally:
        client.close()


def run(state_dir: Path, conn_dir: Path) -> None:
    stores = json.loads((state_dir / STORES_FILE).read_text(encoding="utf-8"))
    if "ha" not in stores:
        raise SystemExit(f"{STORES_FILE} has no 'ha' store: run the fixture again")
    pg_password = require_env("POSTGRES_PASSWORD")
    redis_password = require_env("REDIS_PASSWORD")
    hmac_secret = require_env("WEBHOOK_HMAC_SECRET")
    dsn = f"postgresql://{POSTGRES_USER}:{pg_password}@{POSTGRES_HOST}:5432/{POSTGRES_DB}"
    url = f"redis://:{redis_password}@{REDIS_HOST}:6379/0"
    seed_postgres(dsn, state_dir, stores["postgres"])
    seed_redis(url, state_dir, stores["redis"], stores["ha"])
    write_atomic(conn_dir / "pg.dsn", dsn + "\n", KEY_FILE_MODE)
    write_atomic(conn_dir / "redis.url", url + "\n", KEY_FILE_MODE)
    write_atomic(conn_dir / "hmac.secret", hmac_secret + "\n", KEY_FILE_MODE)
    write_atomic(conn_dir / "state-redis.url", url + "\n", KEY_FILE_MODE)
    write_atomic(conn_dir / "state.key", state_encryption_key() + "\n", KEY_FILE_MODE)
    write_atomic(conn_dir / "noredis.url", NOREDIS_URL + "\n", KEY_FILE_MODE)
    print(
        f"seeded {len(stores['postgres'])} PostgreSQL rows, {len(stores['redis'])} chain Redis keys"
        f" and {len(stores['ha'])} replica-pair Redis keys",
        flush=True,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=DESCRIPTION)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--conn-dir", type=Path, required=True)
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    run(args.state_dir, args.conn_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
