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
import json
import os
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


def seed_redis(url: str, state_dir: Path, rows: list[dict[str, Any]]) -> None:
    client = redis.Redis.from_url(
        url, socket_connect_timeout=CONNECT_TIMEOUT_SECONDS, socket_timeout=CONNECT_TIMEOUT_SECONDS
    )
    try:
        for stale in client.scan_iter(match=f"{REDIS_KEY_PREFIX}*"):
            client.delete(stale)
        for row in rows:
            value = {
                "api_key": read_key(state_dir, row["key_file"]),
                "upstream": row["upstream"],
                "labels": row["labels"],
            }
            client.set(redis_key(row["email"]), json.dumps(value))
    finally:
        client.close()


def run(state_dir: Path, conn_dir: Path) -> None:
    stores = json.loads((state_dir / STORES_FILE).read_text(encoding="utf-8"))
    pg_password = require_env("POSTGRES_PASSWORD")
    redis_password = require_env("REDIS_PASSWORD")
    hmac_secret = require_env("WEBHOOK_HMAC_SECRET")
    dsn = f"postgresql://{POSTGRES_USER}:{pg_password}@{POSTGRES_HOST}:5432/{POSTGRES_DB}"
    url = f"redis://:{redis_password}@{REDIS_HOST}:6379/0"
    seed_postgres(dsn, state_dir, stores["postgres"])
    seed_redis(url, state_dir, stores["redis"])
    write_atomic(conn_dir / "pg.dsn", dsn + "\n", KEY_FILE_MODE)
    write_atomic(conn_dir / "redis.url", url + "\n", KEY_FILE_MODE)
    write_atomic(conn_dir / "hmac.secret", hmac_secret + "\n", KEY_FILE_MODE)
    print(
        f"seeded {len(stores['postgres'])} PostgreSQL rows and {len(stores['redis'])} Redis keys",
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
