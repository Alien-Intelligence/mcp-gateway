from typing import Any

import pytest

from mcp_gateway.config import IdentityPathsConfig
from mcp_gateway.identity import (
    IdentityError,
    Profile,
    assemble_identity,
    build_identity,
    read_profile,
    read_subject,
)

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


# --- the subject and the profile are read separately -----------------------------------------


def test_the_subject_is_read_through_the_configured_path() -> None:
    paths = IdentityPathsConfig(
        subject="ext.uid", email=None, email_verified=None, organization=None, groups=None
    )
    assert read_subject({"sub": "ignored", "ext": {"uid": "u9"}}, paths) == "u9"


@pytest.mark.parametrize("claims", [{}, {"sub": 5}, {"sub": ""}, {"sub": None}])
def test_a_missing_or_non_string_subject_is_refused(claims: dict[str, Any]) -> None:
    with pytest.raises(IdentityError, match="subject"):
        read_subject(claims, KEYCLOAK)


def test_the_profile_is_read_from_the_document_it_is_given_and_has_no_subject() -> None:
    profile = read_profile(
        {"email": "a@x.example", "email_verified": True, "org": "client-x", "groups": ["g"]},
        KEYCLOAK,
    )
    assert profile == Profile(email="a@x.example", organization="client-x", groups=("g",))


def test_a_profile_needs_no_subject_in_its_document() -> None:
    assert read_profile({}, KEYCLOAK) == Profile(email=None, organization=None, groups=())


@pytest.mark.parametrize(
    "document", [{"org": 5}, {"org": ["a"]}, {"groups": "g"}, {"groups": [1]}, {"groups": {}}]
)
def test_a_profile_claim_of_the_wrong_type_is_refused(document: dict[str, Any]) -> None:
    with pytest.raises(IdentityError):
        read_profile(document, KEYCLOAK)


def test_an_identity_is_assembled_from_a_subject_a_profile_and_the_token_claims() -> None:
    identity = assemble_identity(
        "u1", Profile("a@x.example", "client-x", ("g",)), {"sub": "u1", "scope": "mcp"}
    )
    assert (identity.subject, identity.email, identity.organization, identity.groups) == (
        "u1",
        "a@x.example",
        "client-x",
        ("g",),
    )
    assert identity.claims["scope"] == "mcp"


def test_build_identity_is_the_subject_and_the_profile_of_the_same_claims() -> None:
    claims = {
        "sub": "u1",
        "email": "a@x.example",
        "email_verified": True,
        "org": "client-x",
        "groups": ["g"],
    }
    assert build_identity(claims, KEYCLOAK) == assemble_identity(
        read_subject(claims, KEYCLOAK), read_profile(claims, KEYCLOAK), claims
    )
