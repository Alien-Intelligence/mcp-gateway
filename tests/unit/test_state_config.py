"""The ``state`` config: a required explicit choice, and bounds that belong to one backend."""

import base64
import json
from pathlib import Path
from typing import Any

import pytest

from mcp_gateway.config import ConfigError, parse_config
from mcp_gateway.state import build_state
from mcp_gateway.state.redis_cache import load_encryption_key
from mcp_gateway.state.redis_userinfo import RedisUserinfoStore
from mcp_gateway.userinfo import MemoryUserinfoStore

from .conftest import base_config_dict, config_from
from .userinfo_support import userinfo_dict

REDIS_STATE: dict[str, Any] = {
    "type": "redis",
    "url_file": "/run/secrets/state-url",
    "key_prefix": "mcp-gateway:",
    "connect_timeout_seconds": 1,
    "operation_timeout_seconds": 1,
    "max_connections": 8,
    "encryption_key_file": "/run/secrets/state-key",
}
CACHE = {"positive_ttl_seconds": 60, "negative_ttl_seconds": 5, "max_entries": None}


def redis_config(**state_overrides: Any) -> dict[str, Any]:
    data = base_config_dict()
    data["state"] = {**REDIS_STATE, **state_overrides}
    data["sessions"]["max_entries"] = None
    return data


def rejects(data: dict[str, Any], *fragments: str) -> None:
    with pytest.raises(ConfigError) as caught:
        config_from(data)
    for fragment in fragments:
        assert fragment in str(caught.value)


def test_memory_is_valid_with_the_bounds_set() -> None:
    config = config_from(base_config_dict())
    assert config.state.type == "memory"
    assert config.sessions.max_entries == 100


def test_redis_is_valid_with_null_bounds_and_a_cache() -> None:
    data = redis_config()
    data["resolver_cache"] = CACHE
    config = config_from(data)
    assert config.state.type == "redis"
    assert config.sessions.max_entries is None
    assert config.resolver_cache is not None
    assert config.resolver_cache.max_entries is None


def test_redis_is_valid_with_the_cache_off() -> None:
    assert config_from(redis_config()).resolver_cache is None


def test_memory_refuses_a_null_session_bound() -> None:
    data = base_config_dict()
    data["sessions"]["max_entries"] = None
    rejects(data, "sessions.max_entries is required when state.type is memory")


def test_memory_refuses_a_null_cache_bound_when_the_cache_is_on() -> None:
    data = base_config_dict()
    data["resolver_cache"] = CACHE
    rejects(data, "resolver_cache.max_entries is required when state.type is memory")


def test_redis_refuses_a_session_bound() -> None:
    data = redis_config()
    data["sessions"]["max_entries"] = 100
    rejects(data, "sessions.max_entries must be null when state.type is redis")


def test_redis_refuses_a_cache_bound() -> None:
    data = redis_config()
    data["resolver_cache"] = {**CACHE, "max_entries": 10}
    rejects(data, "resolver_cache.max_entries must be null when state.type is redis")


def test_the_per_subject_cap_needs_no_total_with_redis() -> None:
    data = redis_config()
    data["sessions"]["max_entries_per_subject"] = 1000
    assert config_from(data).sessions.max_entries_per_subject == 1000


def test_the_per_subject_cap_stays_within_the_total_with_memory() -> None:
    data = base_config_dict()
    data["sessions"]["max_entries_per_subject"] = 101
    rejects(data, "max_entries_per_subject must be <= max_entries")


@pytest.mark.parametrize("missing", sorted(REDIS_STATE))
def test_every_redis_key_is_required(missing: str) -> None:
    data = redis_config()
    del data["state"][missing]
    rejects(data, f"state.redis.{missing}" if missing != "type" else "state")


@pytest.mark.parametrize(
    ("field", "bad"),
    [
        ("connect_timeout_seconds", 0),
        ("operation_timeout_seconds", 0),
        ("operation_timeout_seconds", -1),
        ("max_connections", 0),
        ("url_file", ""),
        ("encryption_key_file", ""),
        ("key_prefix", ""),
        ("key_prefix", "has space"),
        ("key_prefix", "a" * 65),
        ("key_prefix", "ünï"),
        ("key_prefix", "a{b}"),
    ],
)
def test_redis_values_are_bounded(field: str, bad: Any) -> None:
    rejects(redis_config(**{field: bad}), field)


def test_an_unknown_type_and_unknown_keys_are_refused() -> None:
    rejects(redis_config(type="sqlite"), "state")
    rejects(redis_config(extra=1), "extra")
    data = base_config_dict()
    data["state"] = {"type": "memory", "key_prefix": "x"}
    rejects(data, "key_prefix")


def test_a_missing_state_key_is_a_startup_error() -> None:
    data = base_config_dict()
    del data["state"]
    with pytest.raises(ConfigError, match="state"):
        parse_config(json.dumps(data))


# --- the encryption key file -----------------------------------------------------------


def write(tmp_path: Path, content: str) -> str:
    path = tmp_path / "key"
    path.write_text(content)
    return str(path)


def test_a_valid_key_file_loads(tmp_path: Path) -> None:
    key = bytes(range(32))
    assert load_encryption_key(write(tmp_path, base64.b64encode(key).decode() + "\n")) == key


@pytest.mark.parametrize(
    "content",
    [
        base64.b64encode(b"s3cret-but-too-short").decode(),
        base64.b64encode(bytes(33)).decode(),
        "not*base64*at*all-s3cretvalue",
        "",
    ],
)
def test_a_bad_key_file_is_refused_without_echoing_its_content(
    tmp_path: Path, content: str
) -> None:
    with pytest.raises(ConfigError) as caught:
        load_encryption_key(write(tmp_path, content))
    assert "s3cret" not in str(caught.value)
    assert content == "" or content not in str(caught.value)


def test_a_missing_key_file_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="unreadable"):
        load_encryption_key(str(tmp_path / "absent"))


# --- auth.userinfo.max_entries follows the state backend ------------------------------------------


def _userinfo(max_entries: int | None) -> dict[str, Any]:
    return userinfo_dict(max_entries=max_entries)


def test_memory_requires_the_userinfo_bound() -> None:
    data = base_config_dict()
    data["auth"]["userinfo"] = _userinfo(None)
    rejects(data, "auth.userinfo.max_entries is required when state.type is memory")


def test_memory_accepts_the_userinfo_bound() -> None:
    data = base_config_dict()
    data["auth"]["userinfo"] = _userinfo(50)
    assert config_from(data).auth.userinfo is not None


def test_redis_refuses_a_userinfo_bound() -> None:
    data = redis_config()
    data["auth"]["userinfo"] = _userinfo(50)
    rejects(data, "auth.userinfo.max_entries must be null when state.type is redis")


def test_redis_accepts_a_null_userinfo_bound() -> None:
    data = redis_config()
    data["auth"]["userinfo"] = _userinfo(None)
    assert config_from(data).auth.userinfo is not None


def test_a_null_userinfo_has_no_bound_to_check_with_either_backend() -> None:
    config_from(base_config_dict())
    config_from(redis_config())


def test_the_memory_state_carries_a_userinfo_store_only_when_userinfo_is_configured() -> None:
    off = build_state(config_from(base_config_dict()))
    assert off.userinfo is None
    data = base_config_dict()
    data["auth"]["userinfo"] = _userinfo(50)
    assert isinstance(build_state(config_from(data)).userinfo, MemoryUserinfoStore)


async def test_the_redis_state_carries_a_userinfo_store_only_when_userinfo_is_configured(
    tmp_path: Path,
) -> None:
    (tmp_path / "url").write_text("redis://127.0.0.1:1/0\n")
    (tmp_path / "key").write_text(base64.b64encode(bytes(range(32))).decode() + "\n")
    state_config = {
        **REDIS_STATE,
        "url_file": str(tmp_path / "url"),
        "encryption_key_file": str(tmp_path / "key"),
    }
    off = redis_config()
    off["state"] = state_config
    on = redis_config()
    on["state"] = state_config
    on["auth"]["userinfo"] = _userinfo(None)
    off_state, on_state = build_state(config_from(off)), build_state(config_from(on))
    try:
        assert off_state.userinfo is None
        assert isinstance(on_state.userinfo, RedisUserinfoStore)
    finally:
        await off_state.aclose()
        await on_state.aclose()
