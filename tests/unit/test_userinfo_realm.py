"""The committed userinfo realm must be the one the generator produces, and keep its promises."""

import json
from typing import Any

import pytest
from gen_userinfo_realm import (
    GATEWAY_AUDIENCES,
    MISMATCH_SCOPE,
    MISMATCH_SUBJECT,
    REALM_NAME,
    SOURCE_PATH,
    TARGET_PATH,
    RealmError,
    main,
    render,
    transform,
)


def committed() -> dict[str, Any]:
    return json.loads(TARGET_PATH.read_text(encoding="utf-8"))


def mappers(realm: dict[str, Any], scope_name: str) -> dict[str, dict[str, Any]]:
    (scope,) = [s for s in realm["clientScopes"] if s["name"] == scope_name]
    return {m["name"]: m["config"] for m in scope["protocolMappers"]}


def test_committed_realm_is_current() -> None:
    assert TARGET_PATH.read_text(encoding="utf-8") == render(), (
        "dev/keycloak/alien-gateway-userinfo-realm.json is stale: "
        "run `uv run python scripts/gen_userinfo_realm.py`"
    )
    assert main(["--check"]) == 0


def test_identity_claims_are_kept_out_of_the_access_token_but_not_the_userinfo_document() -> None:
    realm = committed()
    assert realm["realm"] == REALM_NAME
    flipped = {
        **mappers(realm, "email"),
        **mappers(realm, "gateway-identity"),
    }
    assert set(flipped) == {"email", "email verified", "org", "groups"}
    for name, config in flipped.items():
        assert config["access.token.claim"] == "false", name
        assert config["introspection.token.claim"] == "false", name
        assert config["userinfo.token.claim"] == "true", name


def test_only_the_userinfo_gateways_are_audiences() -> None:
    audiences = {
        c["included.custom.audience"] for c in mappers(committed(), "mcp-gateway").values()
    }
    assert audiences == set(GATEWAY_AUDIENCES)


def test_the_mismatch_scope_is_optional_and_changes_only_the_userinfo_subject() -> None:
    realm = committed()
    assert MISMATCH_SCOPE in realm["defaultOptionalClientScopes"]
    assert MISMATCH_SCOPE not in realm["defaultDefaultClientScopes"]
    (config,) = mappers(realm, MISMATCH_SCOPE).values()
    assert config["claim.name"] == "sub"
    assert config["claim.value"] == MISMATCH_SUBJECT
    assert config["userinfo.token.claim"] == "true"
    for claim in ("access.token.claim", "id.token.claim", "introspection.token.claim"):
        assert config[claim] == "false", claim


def test_a_dev_realm_without_the_expected_mappers_is_refused() -> None:
    realm = json.loads(SOURCE_PATH.read_text(encoding="utf-8"))
    (scope,) = [s for s in realm["clientScopes"] if s["name"] == "gateway-identity"]
    scope["protocolMappers"] = [m for m in scope["protocolMappers"] if m["name"] != "org"]
    with pytest.raises(RealmError, match="org"):
        transform(realm)
