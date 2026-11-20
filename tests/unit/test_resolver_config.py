import copy
import json
from typing import Any

import pytest

from mcp_gateway.config import ConfigError, parse_config, validate_key_pattern, validate_sql_query
from mcp_gateway.identity import Identity
from mcp_gateway.resolver.store_redis import render_key

from .conftest import base_config_dict

WEBHOOK: dict[str, Any] = {
    "type": "webhook",
    "url": "https://erp.example.com/resolve",
    "auth": {"type": "hmac", "secret_file": "/run/secrets/hmac"},
    "ca_file": None,
    "default_upstream": "primary",
    "allow_insecure_http": False,
    "timeout_seconds": 3,
    "total_timeout_seconds": 20,
    "max_attempts": 3,
    "backoff_base_seconds": 0.2,
    "backoff_max_seconds": 2,
    "max_ttl_seconds": 600,
}
SQL: dict[str, Any] = {
    "type": "store_sql",
    "dsn_file": "/run/secrets/dsn",
    "query": "SELECT api_key, upstream, labels FROM mapping WHERE email = %(email)s",
    "default_upstream": "primary",
    "pool_min_size": 1,
    "pool_max_size": 4,
    "connect_timeout_seconds": 3,
    "statement_timeout_seconds": 2,
}
REDIS: dict[str, Any] = {
    "type": "store_redis",
    "url_file": "/run/secrets/redis",
    "key_pattern": "mcp:org:{organization}:{subject}",
    "default_upstream": "primary",
    "connect_timeout_seconds": 2,
    "socket_timeout_seconds": 2,
    "max_connections": 8,
}
STATIC: dict[str, Any] = {"type": "static", "mapping_file": "/m.json"}


def parse(resolver: dict[str, Any], **top: Any) -> None:
    data = copy.deepcopy(base_config_dict())
    data["resolver"] = copy.deepcopy(resolver)
    data.update(top)
    parse_config(json.dumps(data))


@pytest.mark.parametrize("resolver", [STATIC, WEBHOOK, SQL, REDIS])
def test_each_resolver_type_loads(resolver: dict[str, Any]) -> None:
    parse(resolver)


def test_chain_of_leaves_loads() -> None:
    parse({"type": "chain", "resolvers": [REDIS, WEBHOOK]})


def test_chains_do_not_nest() -> None:
    inner = {"type": "chain", "resolvers": [REDIS, WEBHOOK]}
    with pytest.raises(ConfigError):
        parse({"type": "chain", "resolvers": [inner, WEBHOOK]})


def test_chain_needs_two_members() -> None:
    with pytest.raises(ConfigError, match="resolvers"):
        parse({"type": "chain", "resolvers": [REDIS]})


def test_unknown_resolver_type_is_refused() -> None:
    with pytest.raises(ConfigError):
        parse({"type": "alien-provisioner"})


def test_resolver_cache_is_required_and_may_be_null() -> None:
    data = copy.deepcopy(base_config_dict())
    del data["resolver_cache"]
    with pytest.raises(ConfigError, match="resolver_cache"):
        parse_config(json.dumps(data))
    parse(STATIC, resolver_cache=None)
    parse(
        STATIC,
        resolver_cache={
            "positive_ttl_seconds": 60,
            "negative_ttl_seconds": 5,
            "max_entries": 1000,
        },
    )


@pytest.mark.parametrize("field", ["positive_ttl_seconds", "negative_ttl_seconds", "max_entries"])
@pytest.mark.parametrize("bad", [0, -1])
def test_cache_bounds_are_positive(field: str, bad: int) -> None:
    cache = {"positive_ttl_seconds": 60, "negative_ttl_seconds": 5, "max_entries": 10}
    cache[field] = bad
    with pytest.raises(ConfigError, match=field):
        parse(STATIC, resolver_cache=cache)


@pytest.mark.parametrize(
    ("resolver", "field"),
    [(WEBHOOK, k) for k in WEBHOOK if k not in ("type",)]
    + [(SQL, k) for k in SQL if k != "type"]
    + [(REDIS, k) for k in REDIS if k != "type"],
)
def test_every_resolver_key_is_required(resolver: dict[str, Any], field: str) -> None:
    broken = {k: v for k, v in resolver.items() if k != field}
    with pytest.raises(ConfigError, match=field):
        parse(broken)


@pytest.mark.parametrize(
    ("resolver", "field", "bad"),
    [
        (WEBHOOK, "timeout_seconds", 0),
        (WEBHOOK, "max_attempts", 0),
        (WEBHOOK, "max_attempts", 11),
        (WEBHOOK, "backoff_base_seconds", 0),
        (WEBHOOK, "backoff_max_seconds", 0.1),  # below the base
        (WEBHOOK, "max_ttl_seconds", -5),
        (WEBHOOK, "url", "ftp://x"),
        (WEBHOOK, "ca_file", ""),
        (SQL, "pool_min_size", 0),
        (SQL, "pool_max_size", 0),
        (SQL, "connect_timeout_seconds", 0),
        (SQL, "statement_timeout_seconds", 0),
        (REDIS, "connect_timeout_seconds", 0),
        (REDIS, "socket_timeout_seconds", -1),
        (REDIS, "max_connections", 0),
    ],
)
def test_bounds_are_enforced(resolver: dict[str, Any], field: str, bad: Any) -> None:
    with pytest.raises(ConfigError):
        parse({**resolver, field: bad})


def test_unknown_keys_are_refused() -> None:
    with pytest.raises(ConfigError, match="surprise"):
        parse({**WEBHOOK, "surprise": 1})
    with pytest.raises(ConfigError, match="inline_secret"):
        parse({**WEBHOOK, "auth": {"type": "hmac", "secret_file": "/s", "inline_secret": "x"}})


def test_secrets_cannot_be_given_inline() -> None:
    with pytest.raises(ConfigError, match="secret_file"):
        parse({**WEBHOOK, "auth": {"type": "hmac", "secret": "inline"}})
    with pytest.raises(ConfigError, match="dsn_file"):
        parse({**{k: v for k, v in SQL.items() if k != "dsn_file"}, "dsn": "postgresql://x"})


def test_plain_http_webhook_needs_the_explicit_dev_flag() -> None:
    plain = {**WEBHOOK, "url": "http://webhook-resolver:8080/resolve"}
    with pytest.raises(ConfigError, match="https"):
        parse(plain)
    with pytest.raises(ConfigError, match="https"):
        parse({**plain, "url": "http://127.0.0.1:9/resolve"})  # loopback gets no exemption
    parse({**plain, "allow_insecure_http": True})
    parse(WEBHOOK)


@pytest.mark.parametrize("total", [3, 2.9, 0])
def test_total_timeout_must_exceed_the_attempt_timeout(total: float) -> None:
    with pytest.raises(ConfigError, match="total_timeout_seconds"):
        parse({**WEBHOOK, "total_timeout_seconds": total})


def test_allow_insecure_http_is_required() -> None:
    with pytest.raises(ConfigError, match="allow_insecure_http"):
        parse({k: v for k, v in WEBHOOK.items() if k != "allow_insecure_http"})


def test_mtls_needs_https_and_both_files() -> None:
    mtls = {"type": "mtls", "client_cert_file": "/c", "client_key_file": "/k"}
    parse({**WEBHOOK, "auth": mtls})  # https url
    with pytest.raises(ConfigError, match="https"):
        parse(
            {
                **WEBHOOK,
                "auth": mtls,
                "url": "http://erp.example.com/r",
                "allow_insecure_http": True,
            }
        )
    with pytest.raises(ConfigError, match="client_key_file"):
        parse({**WEBHOOK, "auth": {"type": "mtls", "client_cert_file": "/c"}})


def test_pool_max_below_min_is_refused() -> None:
    with pytest.raises(ConfigError, match="pool_max_size"):
        parse({**SQL, "pool_min_size": 5, "pool_max_size": 2})


def test_errors_never_echo_values() -> None:
    with pytest.raises(ConfigError) as caught:
        parse({**WEBHOOK, "auth": {"type": "hmac", "secret_file": ""}, "url": "not-a-url"})
    assert "not-a-url" not in str(caught.value)


# --- SQL query validation -----------------------------------------------------------------


@pytest.mark.parametrize(
    "query",
    [
        "SELECT api_key, upstream, labels FROM t WHERE subject = %(subject)s",
        "select api_key from t where email = %(email)s and org = %(organization)s;",
        "SELECT api_key FROM t WHERE name LIKE 'a%%' AND s = %(subject)s",
    ],
)
def test_valid_queries(query: str) -> None:
    assert validate_sql_query(query)


@pytest.mark.parametrize(
    "query",
    [
        "SELECT api_key FROM t",  # no parameter at all
        "SELECT api_key FROM t WHERE x = %(groups)s",
        "SELECT api_key FROM t WHERE x = %(subject)s AND y = %(token)s",
        "SELECT api_key FROM t WHERE x = %s",
        "SELECT api_key FROM t WHERE x = %(subject)",
        "SELECT api_key FROM t WHERE x LIKE 'a%' AND y = %(subject)s",
        "SELECT api_key FROM t WHERE x = %(subject)s; DROP TABLE t",
        "SELECT 1; SELECT 2 WHERE %(subject)s = ''",
        "DELETE FROM t WHERE x = %(subject)s",
        "UPDATE t SET api_key = 'x' WHERE x = %(subject)s",
        "WITH d AS (DELETE FROM t RETURNING *) SELECT * FROM d WHERE %(subject)s = ''",
        "",
    ],
)
def test_invalid_queries(query: str) -> None:
    with pytest.raises(ValueError, match="query"):
        validate_sql_query(query)


def test_query_is_validated_at_config_load() -> None:
    with pytest.raises(ConfigError, match="query"):
        parse({**SQL, "query": "SELECT api_key FROM t WHERE x = %(groups)s"})


# --- Redis key pattern --------------------------------------------------------------------


def ident(email: str | None = "a@b.c", organization: str | None = "org") -> Identity:
    return Identity(subject="u{1}", email=email, organization=organization, groups=(), claims={})


@pytest.mark.parametrize(
    "pattern",
    ["{subject}", "mcp:{organization}:{subject}", "k:{email}"],
)
def test_valid_key_patterns(pattern: str) -> None:
    assert validate_key_pattern(pattern) == pattern


@pytest.mark.parametrize(
    "pattern",
    [
        "static-key",
        "{subject}{",
        "}{subject}",
        "{groups}",
        "{subject!r}",
        "{0}",
        "{}",
        "{{subject}}",
    ],
)
def test_invalid_key_patterns(pattern: str) -> None:
    with pytest.raises(ValueError, match="key_pattern"):
        validate_key_pattern(pattern)


def test_key_pattern_is_validated_at_config_load() -> None:
    with pytest.raises(ConfigError, match="key_pattern"):
        parse({**REDIS, "key_pattern": "{claims}"})


def test_render_substitutes_in_one_pass() -> None:
    # A value that looks like a placeholder is inserted verbatim, not expanded again.
    identity = Identity(subject="{email}", email="a@b.c", organization="org", groups=(), claims={})
    assert render_key("k:{subject}:{organization}", identity) == "k:{email}:org"


def test_render_returns_none_when_a_used_placeholder_is_null() -> None:
    assert render_key("k:{subject}:{organization}", ident(organization=None)) is None
    assert render_key("k:{subject}", ident(organization=None)) == "k:u{1}"
