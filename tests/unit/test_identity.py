from typing import Any

import pytest

from mcp_gateway.config import IdentityPathsConfig
from mcp_gateway.identity import IdentityError, build_identity

KEYCLOAK = IdentityPathsConfig(
    subject="sub",
    email="email",
    email_verified="email_verified",
    organization="org",
    groups="groups",
)
HYDRA = IdentityPathsConfig(
    subject="sub",
    email="ext.email",
    email_verified="ext.email_verified",
    organization="ext.org",
    groups="ext.teams",
)


def test_keycloak_style_top_level_claims() -> None:
    identity = build_identity(
        {
            "sub": "u1",
            "email": "a@x.example",
            "email_verified": True,
            "org": "client-x",
            "groups": ["g1", "g2"],
        },
        KEYCLOAK,
    )
    assert identity.subject == "u1"
    assert identity.email == "a@x.example"
    assert identity.organization == "client-x"
    assert identity.groups == ("g1", "g2")


def test_hydra_style_nested_claims() -> None:
    identity = build_identity(
        {
            "sub": "u1",
            "ext": {
                "email": "a@x.example",
                "email_verified": True,
                "org": "client-y",
                "teams": ["t"],
            },
        },
        HYDRA,
    )
    assert identity.email == "a@x.example"
    assert identity.organization == "client-y"
    assert identity.groups == ("t",)


def test_absent_optional_claims_are_none_and_empty() -> None:
    identity = build_identity({"sub": "u1"}, KEYCLOAK)
    assert identity.email is None
    assert identity.organization is None
    assert identity.groups == ()


def test_paths_configured_as_null_are_never_read() -> None:
    paths = IdentityPathsConfig(
        subject="sub", email=None, email_verified=None, organization=None, groups=None
    )
    identity = build_identity({"sub": "u", "email": "a@x.example", "groups": ["g"]}, paths)
    assert identity.email is None
    assert identity.groups == ()


def test_nested_path_through_a_non_mapping_is_absent() -> None:
    identity = build_identity({"sub": "u1", "ext": "not-a-dict"}, HYDRA)
    assert identity.organization is None


def test_explicit_null_and_empty_string_count_as_absent() -> None:
    identity = build_identity({"sub": "u1", "org": None, "email": ""}, KEYCLOAK)
    assert identity.organization is None
    assert identity.email is None


@pytest.mark.parametrize("claims", [{}, {"sub": ""}, {"sub": 12}, {"sub": ["a"]}, {"sub": None}])
def test_missing_or_non_string_subject_is_an_error(claims: dict[str, Any]) -> None:
    with pytest.raises(IdentityError, match="subject"):
        build_identity(claims, KEYCLOAK)


@pytest.mark.parametrize(
    "claims",
    [
        {"sub": "u", "org": ["client-x"]},
        {"sub": "u", "org": 7},
        {"sub": "u", "email": {"a": 1}, "email_verified": True},
    ],
)
def test_wrong_scalar_types_are_errors_not_coerced(claims: dict[str, Any]) -> None:
    with pytest.raises(IdentityError):
        build_identity(claims, KEYCLOAK)


@pytest.mark.parametrize("groups", ["g1,g2", "g1", {"g": 1}, [1, 2], ["ok", 3], 5])
def test_groups_must_be_a_list_of_strings(groups: Any) -> None:
    with pytest.raises(IdentityError, match="groups"):
        build_identity({"sub": "u", "groups": groups}, KEYCLOAK)


def test_claims_are_read_only_and_not_in_repr() -> None:
    source = {"sub": "u1", "secret_claim": "v"}
    identity = build_identity(source, KEYCLOAK)
    mutable: Any = identity.claims
    with pytest.raises(TypeError):
        mutable["sub"] = "other"
    source["sub"] = "changed"
    assert identity.claims["sub"] == "u1"
    assert "secret_claim" not in repr(identity)


@pytest.mark.parametrize(
    "verified", [False, None, "true", 1, "yes"], ids=["false", "null", "str", "int", "yes"]
)
def test_email_is_untrusted_unless_the_verification_claim_is_boolean_true(verified: Any) -> None:
    claims = {"sub": "u", "email": "a@x.example", "email_verified": verified}
    assert build_identity(claims, KEYCLOAK).email is None


def test_email_is_untrusted_when_the_verification_claim_is_absent() -> None:
    assert build_identity({"sub": "u", "email": "a@x.example"}, KEYCLOAK).email is None


def test_email_is_never_trusted_when_the_verification_path_is_null() -> None:
    paths = IdentityPathsConfig(
        subject="sub", email="email", email_verified=None, organization=None, groups=None
    )
    identity = build_identity({"sub": "u", "email": "a@x.example", "email_verified": True}, paths)
    assert identity.email is None
