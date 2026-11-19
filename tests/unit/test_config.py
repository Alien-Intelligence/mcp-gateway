import copy
import json
from typing import Any

import pytest

from mcp_gateway.config import CONFIG_ENV_VAR, ConfigError, load_config, parse_config

from .conftest import PUBLIC_URL, config_from


def _parse(data: dict[str, Any]) -> None:
    parse_config(json.dumps(data))


def test_valid_config_loads_and_exposes_derived_urls(config_dict: dict[str, Any]) -> None:
    config = config_from(config_dict)
    assert config.public_path == "/mcp"
    assert config.public_origin == "https://gw.example.com"
    assert (
        config.resource_metadata_url
        == "https://gw.example.com/.well-known/oauth-protected-resource/mcp"
    )


def test_optional_claim_path_may_be_explicit_null(config_dict: dict[str, Any]) -> None:
    config_dict["auth"]["identity"]["organization"] = None
    assert config_from(config_dict).auth.identity.organization is None


@pytest.mark.parametrize(
    "path",
    [
        ("public_url",),
        ("listen",),
        ("listen", "port"),
        ("auth", "issuer"),
        ("auth", "required_scopes"),
        ("auth", "clock_skew_seconds"),
        ("auth", "identity", "email"),
        ("resolver", "type"),
        ("upstreams", "primary", "read_timeout_seconds"),
        ("limits",),
        ("audit", "enabled"),
    ],
)
def test_every_key_is_required(config_dict: dict[str, Any], path: tuple[str, ...]) -> None:
    node = config_dict
    for part in path[:-1]:
        node = node[part]
    del node[path[-1]]
    with pytest.raises(ConfigError, match=path[-1]):
        _parse(config_dict)


@pytest.mark.parametrize(
    "path",
    [(), ("auth",), ("auth", "identity"), ("listen",), ("upstreams", "primary"), ("resolver",)],
)
def test_unknown_keys_are_rejected(config_dict: dict[str, Any], path: tuple[str, ...]) -> None:
    node = config_dict
    for part in path:
        node = node[part]
    node["surprise"] = 1
    with pytest.raises(ConfigError, match="surprise"):
        _parse(config_dict)


@pytest.mark.parametrize(
    "url",
    [
        "gw.example.com/mcp",
        "ftp://gw.example.com/mcp",
        "https://gw.example.com",
        "https://gw.example.com/",
        "https://gw.example.com/mcp?x=1",
        "https://gw.example.com/mcp#frag",
    ],
)
def test_public_url_rules(config_dict: dict[str, Any], url: str) -> None:
    config_dict["public_url"] = url
    config_dict["auth"]["audiences"] = [url]
    with pytest.raises(ConfigError, match="public_url"):
        _parse(config_dict)


def test_public_url_must_be_an_audience(config_dict: dict[str, Any]) -> None:
    config_dict["auth"]["audiences"] = ["https://other.example.com/mcp"]
    with pytest.raises(ConfigError, match="audiences"):
        _parse(config_dict)


def test_extra_audiences_are_fine(config_dict: dict[str, Any]) -> None:
    config_dict["auth"]["audiences"] = [PUBLIC_URL, "https://alt.example.com/mcp"]
    assert len(config_from(config_dict).auth.audiences) == 2


@pytest.mark.parametrize("algorithm", ["HS256", "HS512", "none", "NONE", "RS257"])
def test_symmetric_and_unknown_algorithms_are_refused(
    config_dict: dict[str, Any], algorithm: str
) -> None:
    config_dict["auth"]["algorithms"] = ["RS256", algorithm]
    with pytest.raises(ConfigError, match="algorithms"):
        _parse(config_dict)


@pytest.mark.parametrize("algorithm", ["RS256", "ES256", "PS384", "EdDSA"])
def test_asymmetric_algorithms_are_accepted(config_dict: dict[str, Any], algorithm: str) -> None:
    config_dict["auth"]["algorithms"] = [algorithm]
    assert config_from(config_dict).auth.algorithms == [algorithm]


@pytest.mark.parametrize(
    ("section", "key", "value"),
    [
        ("auth", "algorithms", []),
        ("auth", "scopes_advertised", []),
        ("auth", "audiences", []),
        ("auth", "jwks_cache_ttl_seconds", 0),
        ("auth", "jwks_fetch_timeout_seconds", -1),
        ("auth", "clock_skew_seconds", -1),
        ("auth", "issuer", "not a url"),
        ("auth", "jwks_uri", "/relative"),
        ("limits", "max_request_body_bytes", 0),
        ("listen", "port", 0),
        ("listen", "port", 70000),
    ],
)
def test_value_rules(config_dict: dict[str, Any], section: str, key: str, value: Any) -> None:
    config_dict[section][key] = value
    with pytest.raises(ConfigError, match=key):
        _parse(config_dict)


@pytest.mark.parametrize(
    "timeout_key",
    [
        "connect_timeout_seconds",
        "read_timeout_seconds",
        "write_timeout_seconds",
        "pool_timeout_seconds",
    ],
)
def test_upstream_timeouts_must_be_positive(config_dict: dict[str, Any], timeout_key: str) -> None:
    config_dict["upstreams"]["primary"][timeout_key] = 0
    with pytest.raises(ConfigError, match=timeout_key):
        _parse(config_dict)


def test_at_least_one_upstream(config_dict: dict[str, Any]) -> None:
    config_dict["upstreams"] = {}
    with pytest.raises(ConfigError, match="upstreams"):
        _parse(config_dict)


def test_upstream_url_must_be_http(config_dict: dict[str, Any]) -> None:
    config_dict["upstreams"]["primary"]["url"] = "upstream.example.com/mcp"
    with pytest.raises(ConfigError, match="url"):
        _parse(config_dict)


@pytest.mark.parametrize("path", ["", ".", "a..b", ".a", "a."])
def test_subject_path_must_be_dotted_path(config_dict: dict[str, Any], path: str) -> None:
    config_dict["auth"]["identity"]["subject"] = path
    with pytest.raises(ConfigError, match="subject"):
        _parse(config_dict)


def test_subject_cannot_be_null(config_dict: dict[str, Any]) -> None:
    config_dict["auth"]["identity"]["subject"] = None
    with pytest.raises(ConfigError, match="subject"):
        _parse(config_dict)


def test_nested_claim_path_is_accepted(config_dict: dict[str, Any]) -> None:
    config_dict["auth"]["identity"]["organization"] = "ext.org"
    assert config_from(config_dict).auth.identity.organization == "ext.org"


def test_unknown_resolver_type_is_rejected(config_dict: dict[str, Any]) -> None:
    config_dict["resolver"] = {"type": "webhook", "url": "https://r.example.com"}
    with pytest.raises(ConfigError, match="resolver"):
        _parse(config_dict)


def test_strict_types_no_coercion(config_dict: dict[str, Any]) -> None:
    config_dict["listen"]["port"] = "8000"
    with pytest.raises(ConfigError, match="port"):
        _parse(config_dict)


def test_invalid_json_is_a_clear_error() -> None:
    with pytest.raises(ConfigError, match=CONFIG_ENV_VAR):
        parse_config("{not json")


def test_error_never_echoes_values(config_dict: dict[str, Any]) -> None:
    config_dict["upstreams"]["primary"]["url"] = "super-secret-value"
    with pytest.raises(ConfigError) as excinfo:
        _parse(config_dict)
    assert "super-secret-value" not in str(excinfo.value)


def test_load_config_requires_the_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(CONFIG_ENV_VAR, raising=False)
    with pytest.raises(ConfigError, match="not set"):
        load_config()


def test_load_config_rejects_blank_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(CONFIG_ENV_VAR, "   ")
    with pytest.raises(ConfigError, match="not set"):
        load_config()


def test_load_config_reads_the_env_var(
    monkeypatch: pytest.MonkeyPatch, config_dict: dict[str, Any]
) -> None:
    monkeypatch.setenv(CONFIG_ENV_VAR, json.dumps(copy.deepcopy(config_dict)))
    assert load_config().public_url == PUBLIC_URL
