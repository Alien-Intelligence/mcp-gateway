import copy
import json
from typing import Any

import pytest

from mcp_gateway.config import CONFIG_ENV_VAR, ConfigError, load_config, parse_config

from .conftest import PUBLIC_URL, config_from
from .userinfo_support import USERINFO_URL, userinfo_dict


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
        ("version",),
        ("public_url",),
        ("listen",),
        ("listen", "port"),
        ("auth", "issuer"),
        ("auth", "required_scopes"),
        ("auth", "clock_skew_seconds"),
        ("auth", "identity", "email"),
        ("auth", "identity", "email_verified"),
        ("auth", "userinfo"),
        ("auth", "jwks_max_stale_seconds"),
        ("resolver", "type"),
        ("upstreams", "main", "read_timeout_seconds"),
        ("limits",),
        ("limits", "request_body_timeout_seconds"),
        ("audit", "enabled"),
        ("observability",),
        ("observability", "otlp"),
        ("limits", "max_sse_event_bytes"),
        ("sessions",),
        ("sessions", "max_entries"),
        ("sessions", "max_entries_per_subject"),
        ("sessions", "idle_ttl_seconds"),
        ("state",),
        ("state", "type"),
        ("policies",),
        ("policies", "allowed_origins"),
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
    [(), ("auth",), ("auth", "identity"), ("listen",), ("upstreams", "main"), ("resolver",)],
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
        ("limits", "request_body_timeout_seconds", 0),
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
    config_dict["upstreams"]["main"][timeout_key] = 0
    with pytest.raises(ConfigError, match=timeout_key):
        _parse(config_dict)


def test_at_least_one_upstream(config_dict: dict[str, Any]) -> None:
    config_dict["upstreams"] = {}
    with pytest.raises(ConfigError, match="upstreams"):
        _parse(config_dict)


def test_upstream_url_must_be_http(config_dict: dict[str, Any]) -> None:
    config_dict["upstreams"]["main"]["url"] = "upstream.example.com/mcp"
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
    config_dict["upstreams"]["main"]["url"] = "super-secret-value"
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


def test_allowed_origins_are_normalised_at_load(config_dict: dict[str, Any]) -> None:
    config_dict["policies"]["allowed_origins"] = ["HTTPS://App.Example.com:443", "http://[::1]:80"]
    config = config_from(config_dict)
    assert config.policies.allowed_origins == ["https://app.example.com", "http://[::1]"]


@pytest.mark.parametrize(
    "bad",
    [
        "app.example.com",
        "https://app.example.com/path",
        "https://",
        "null",
        "NULL",
        "vscode-webview://abc",
        "file://localhost",
        "chrome-extension://abcdef",
    ],
)
def test_allowed_origins_reject_non_origins(config_dict: dict[str, Any], bad: str) -> None:
    config_dict["policies"]["allowed_origins"] = [bad]
    with pytest.raises(ConfigError, match="allowed_origins"):
        _parse(config_dict)


@pytest.mark.parametrize(
    ("section", "key", "value"),
    [
        ("sessions", "max_entries", 0),
        ("sessions", "max_entries_per_subject", 0),
        ("sessions", "max_entries_per_subject", 101),
        ("sessions", "idle_ttl_seconds", 0),
        ("limits", "max_sse_event_bytes", 0),
    ],
)
def test_new_numeric_limits_must_be_positive(
    config_dict: dict[str, Any], section: str, key: str, value: int
) -> None:
    config_dict[section][key] = value
    with pytest.raises(ConfigError, match=key):
        _parse(config_dict)


def test_rate_limits_null_is_accepted_and_both_scopes_parse(config_dict: dict[str, Any]) -> None:
    assert config_from(config_dict).policies.rate_limits is None
    config_dict["policies"]["rate_limits"] = {
        "per_subject": {"burst": 5, "per_minute": 12.5},
        "per_organization": None,
    }
    limits = config_from(config_dict).policies.rate_limits
    assert limits is not None
    assert limits.per_subject is not None
    assert (limits.per_subject.burst, limits.per_subject.per_minute) == (5, 12.5)
    assert limits.per_organization is None


@pytest.mark.parametrize(
    "rate_limits",
    [
        {"per_subject": None, "per_organization": None},
        {"per_subject": None, "per_organization": {"burst": 1, "per_minute": 1}},
        {"per_subject": {"burst": 0, "per_minute": 1}, "per_organization": None},
        {"per_subject": {"burst": 1, "per_minute": 0}, "per_organization": None},
        {"per_subject": {"burst": 1, "per_minute": -3}, "per_organization": None},
        {"per_subject": {"burst": 1, "per_minute": float("inf")}, "per_organization": None},
        {"per_subject": {"burst": 1, "per_minute": 0.0009}, "per_organization": None},
        {"per_subject": {"burst": 1_000_001, "per_minute": 1}, "per_organization": None},
        {
            "per_subject": {"burst": 1, "per_minute": 1},
            "per_organization": {"burst": 2_000_000, "per_minute": 1},
        },
        {"per_subject": {"burst": 1}, "per_organization": None},
        {"per_subject": {"burst": 1, "per_minute": 1}},
        {"per_subject": {"burst": 1, "per_minute": 1, "x": 1}, "per_organization": None},
    ],
)
def test_invalid_rate_limits_are_refused(
    config_dict: dict[str, Any], rate_limits: dict[str, Any]
) -> None:
    config_dict["policies"]["rate_limits"] = rate_limits
    with pytest.raises(ConfigError, match="rate_limits"):
        _parse(config_dict)


def test_a_null_per_subject_says_why_it_is_refused(config_dict: dict[str, Any]) -> None:
    config_dict["policies"]["rate_limits"] = {
        "per_subject": None,
        "per_organization": {"burst": 1, "per_minute": 1},
    }
    with pytest.raises(ConfigError, match="per_subject is required.*without an organization"):
        _parse(config_dict)


def test_per_organization_is_optional_and_needs_an_organization_claim(
    config_dict: dict[str, Any],
) -> None:
    both = {
        "per_subject": {"burst": 1, "per_minute": 1},
        "per_organization": {"burst": 2, "per_minute": 2},
    }
    config_dict["policies"]["rate_limits"] = both
    assert config_from(config_dict).policies.rate_limits is not None
    config_dict["auth"]["identity"]["organization"] = None
    with pytest.raises(ConfigError, match="per_organization.*auth.identity.organization"):
        _parse(config_dict)
    config_dict["policies"]["rate_limits"] = {**both, "per_organization": None}
    assert config_from(config_dict).policies.rate_limits is not None


def test_an_organization_tool_rule_needs_an_organization_claim(
    config_dict: dict[str, Any],
) -> None:
    config_dict["policies"]["tools"] = {
        "default": {"allow": None, "deny": []},
        "rules": [{"match": {"organization": "client-x"}, "allow": None, "deny": ["delete_*"]}],
    }
    assert config_from(config_dict).policies.tools is not None
    config_dict["auth"]["identity"]["organization"] = None
    with pytest.raises(ConfigError, match="organization.*auth.identity.organization"):
        _parse(config_dict)
    # A label rule never reads the organization.
    config_dict["policies"]["tools"]["rules"] = [
        {"match": {"label": {"name": "tier", "value": "basic"}}, "allow": None, "deny": []}
    ]
    assert config_from(config_dict).policies.tools is not None


def test_rate_limits_accept_the_documented_extremes(config_dict: dict[str, Any]) -> None:
    config_dict["policies"]["rate_limits"] = {
        "per_subject": {"burst": 1_000_000, "per_minute": 0.001},
        "per_organization": None,
    }
    assert config_from(config_dict).policies.rate_limits is not None


def test_rate_limits_key_is_required(config_dict: dict[str, Any]) -> None:
    del config_dict["policies"]["rate_limits"]
    with pytest.raises(ConfigError, match="rate_limits"):
        _parse(config_dict)


@pytest.mark.parametrize(("section", "known"), [("state", "memory, redis"), ("resolver", "static")])
def test_an_unknown_discriminator_value_is_never_echoed_in_the_error(
    config_dict: dict[str, Any], section: str, known: str
) -> None:
    """A mistyped value can be a secret pasted into the wrong key; the message lists only the choices."""
    config_dict[section]["type"] = "pasted-secret-1234"
    with pytest.raises(ConfigError) as caught:
        _parse(config_dict)
    message = str(caught.value)
    assert "pasted-secret-1234" not in message
    assert f"{section}: " in message
    for name in known.split(", "):
        assert name in message


@pytest.mark.parametrize("key", ["max_connections", "max_detached_reads"])
def test_upstream_connection_caps_are_required(config_dict: dict[str, Any], key: str) -> None:
    del config_dict["upstreams"]["main"][key]
    with pytest.raises(ConfigError, match=key):
        _parse(config_dict)


@pytest.mark.parametrize(("connections", "detached"), [(10, 0), (0, 0), (10, 10), (10, 11), (1, 1)])
def test_detached_reads_must_leave_room_in_the_connection_pool(
    config_dict: dict[str, Any], connections: int, detached: int
) -> None:
    config_dict["upstreams"]["main"]["max_connections"] = connections
    config_dict["upstreams"]["main"]["max_detached_reads"] = detached
    with pytest.raises(ConfigError, match="max_detached_reads|max_connections"):
        _parse(config_dict)


def test_detached_reads_below_the_pool_size_are_accepted(config_dict: dict[str, Any]) -> None:
    config_dict["upstreams"]["main"]["max_connections"] = 10
    config_dict["upstreams"]["main"]["max_detached_reads"] = 9
    assert config_from(config_dict).upstreams["main"].max_detached_reads == 9


# --- auth.userinfo ----------------------------------------------------------------------------


def _with_userinfo(config_dict: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    config_dict["auth"]["userinfo"] = userinfo_dict(**overrides)
    return config_dict


def test_userinfo_null_is_accepted_and_reads_nothing_from_userinfo(
    config_dict: dict[str, Any],
) -> None:
    assert config_dict["auth"]["userinfo"] is None
    assert config_from(config_dict).auth.userinfo is None


def test_a_valid_userinfo_object_is_accepted(config_dict: dict[str, Any]) -> None:
    userinfo = config_from(_with_userinfo(config_dict)).auth.userinfo
    assert userinfo is not None
    assert userinfo.url == USERINFO_URL
    assert (userinfo.max_attempts, userinfo.max_entries) == (3, 100)


@pytest.mark.parametrize("key", sorted(userinfo_dict()))
def test_every_userinfo_key_is_required(config_dict: dict[str, Any], key: str) -> None:
    _with_userinfo(config_dict)
    del config_dict["auth"]["userinfo"][key]
    with pytest.raises(ConfigError, match=f"auth.userinfo.{key}"):
        _parse(config_dict)


def test_an_unknown_userinfo_key_is_refused(config_dict: dict[str, Any]) -> None:
    _with_userinfo(config_dict, discovery=True)
    with pytest.raises(ConfigError, match="auth.userinfo.discovery"):
        _parse(config_dict)


@pytest.mark.parametrize(
    ("key", "bad"),
    [
        ("timeout_seconds", 0),
        ("timeout_seconds", -1),
        ("max_attempts", 0),
        ("max_attempts", 11),
        ("total_timeout_seconds", 0),
        ("backoff_base_seconds", 0),
        ("backoff_max_seconds", 0),
        ("cache_ttl_seconds", 0),
        ("cache_ttl_seconds", -5),
        ("max_response_bytes", 0),
        ("max_response_bytes", 1048577),
        ("max_entries", 0),
        ("max_entries", -1),
        ("allow_insecure_http", "yes"),
    ],
)
def test_userinfo_values_are_bounded(config_dict: dict[str, Any], key: str, bad: Any) -> None:
    _with_userinfo(config_dict, **{key: bad})
    with pytest.raises(ConfigError, match=f"auth.userinfo.{key}"):
        _parse(config_dict)


@pytest.mark.parametrize(
    ("key", "good"),
    [
        ("max_attempts", 1),
        ("max_attempts", 10),
        ("max_response_bytes", 1),
        ("max_response_bytes", 1048576),
        ("max_entries", 1),
    ],
)
def test_userinfo_bounds_are_inclusive(config_dict: dict[str, Any], key: str, good: Any) -> None:
    _with_userinfo(config_dict, **{key: good})
    _parse(config_dict)


def test_the_total_timeout_must_exceed_the_attempt_timeout(config_dict: dict[str, Any]) -> None:
    _with_userinfo(config_dict, timeout_seconds=3, total_timeout_seconds=3)
    with pytest.raises(ConfigError, match="total_timeout_seconds must be > timeout_seconds"):
        _parse(config_dict)


def test_the_backoff_ceiling_must_not_be_below_the_base(config_dict: dict[str, Any]) -> None:
    _with_userinfo(config_dict, backoff_base_seconds=1, backoff_max_seconds=0.5)
    with pytest.raises(ConfigError, match="backoff_max_seconds must be >= backoff_base_seconds"):
        _parse(config_dict)


@pytest.mark.parametrize(
    "url",
    [
        "not a url",
        "/relative/userinfo",
        "ftp://idp.example.com/userinfo",
        "https://",
        "https://user:secret@idp.example.com/userinfo",
        "https://user@idp.example.com/userinfo",
        "https://idp.example.com/userinfo#fragment",
    ],
)
def test_the_userinfo_url_must_be_a_plain_absolute_http_url(
    config_dict: dict[str, Any], url: str
) -> None:
    _with_userinfo(config_dict, url=url)
    with pytest.raises(ConfigError, match="auth.userinfo.url") as caught:
        _parse(config_dict)
    assert "secret" not in str(caught.value)


def test_a_plain_http_userinfo_url_needs_the_insecure_switch(config_dict: dict[str, Any]) -> None:
    _with_userinfo(config_dict, url="http://idp.example.com/userinfo")
    with pytest.raises(ConfigError, match="must be https"):
        _parse(config_dict)
    _with_userinfo(config_dict, url="http://idp.example.com/userinfo", allow_insecure_http=True)
    _parse(config_dict)


def test_https_userinfo_url_needs_no_switch_and_may_carry_a_query(
    config_dict: dict[str, Any],
) -> None:
    _with_userinfo(config_dict, url="https://idp.example.com/userinfo?tenant=a")
    _parse(config_dict)


def test_userinfo_that_nothing_reads_from_is_refused(config_dict: dict[str, Any]) -> None:
    _with_userinfo(config_dict)
    identity = config_dict["auth"]["identity"]
    identity.update(email=None, email_verified=None, organization=None, groups=None)
    with pytest.raises(ConfigError, match="nothing would be read from it"):
        _parse(config_dict)


@pytest.mark.parametrize("path", ["email", "organization", "groups"])
def test_userinfo_with_any_one_profile_path_is_accepted(
    config_dict: dict[str, Any], path: str
) -> None:
    _with_userinfo(config_dict)
    identity = config_dict["auth"]["identity"]
    identity.update(email=None, email_verified=None, organization=None, groups=None)
    identity[path] = "some.path"
    _parse(config_dict)


def test_userinfo_without_a_profile_path_is_fine_when_it_is_null(
    config_dict: dict[str, Any],
) -> None:
    config_dict["auth"]["identity"].update(
        email=None, email_verified=None, organization=None, groups=None
    )
    assert config_from(config_dict).auth.userinfo is None
