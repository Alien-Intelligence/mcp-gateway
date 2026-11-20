"""Connection settings for tests against real PostgreSQL and Redis servers.

Run with ``MCP_GATEWAY_TEST_PG_DSN`` and ``MCP_GATEWAY_TEST_REDIS_URL`` pointing at throwaway
servers. A selected integration test without them FAILS: a skipped test proves nothing.
"""

import os

import pytest

PG_ENV = "MCP_GATEWAY_TEST_PG_DSN"
REDIS_ENV = "MCP_GATEWAY_TEST_REDIS_URL"


def _required(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        pytest.fail(f"{name} must be set to run the integration tests", pytrace=False)
    return value


@pytest.fixture(scope="session")
def pg_dsn() -> str:
    return _required(PG_ENV)


@pytest.fixture(scope="session")
def redis_url() -> str:
    return _required(REDIS_ENV)
