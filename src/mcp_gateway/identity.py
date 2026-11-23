"""Identity extraction using configurable claim paths.

The subject is always read from the verified access token. The profile (verified email,
organization, groups) is read from one document: the same token claims, or, when the gateway
calls the OIDC userinfo endpoint, the userinfo document and nothing else. The two are never
merged.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from mcp_gateway.config import IdentityPathsConfig


class IdentityError(Exception):
    """The token claims cannot yield a usable identity."""


@dataclass(frozen=True)
class Identity:
    subject: str
    email: str | None
    organization: str | None
    groups: tuple[str, ...]
    claims: Mapping[str, Any] = field(repr=False, compare=False)


@dataclass(frozen=True)
class Profile:
    """What is read from one claims document besides the subject."""

    email: str | None
    organization: str | None
    groups: tuple[str, ...]


_MISSING = object()


def _lookup(claims: Mapping[str, Any], path: str) -> Any:
    """Read a dotted path: ``ext.org`` is ``claims["ext"]["org"]``."""
    node: Any = claims
    for segment in path.split("."):
        if not isinstance(node, Mapping) or segment not in node:
            return _MISSING
        node = node[segment]
    return node


def _optional_string(claims: Mapping[str, Any], path: str | None, *, what: str) -> str | None:
    if path is None:
        return None
    value = _lookup(claims, path)
    if value is _MISSING or value is None:
        return None
    if not isinstance(value, str):
        raise IdentityError(f"claim '{path}' ({what}) must be a string")
    return value or None


def _verified_email(claims: Mapping[str, Any], mapping: IdentityPathsConfig) -> str | None:
    """The email claim, only when the verification claim is the boolean ``true``."""
    if mapping.email_verified is None:
        return None
    if _lookup(claims, mapping.email_verified) is not True:
        return None
    return _optional_string(claims, mapping.email, what="email")


def _optional_groups(claims: Mapping[str, Any], path: str | None) -> tuple[str, ...]:
    if path is None:
        return ()
    value = _lookup(claims, path)
    if value is _MISSING or value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise IdentityError(f"claim '{path}' (groups) must be a list of strings")
    return tuple(item for item in value if item)


def read_subject(claims: Mapping[str, Any], mapping: IdentityPathsConfig) -> str:
    subject = _lookup(claims, mapping.subject)
    if not isinstance(subject, str) or not subject:
        raise IdentityError(f"claim '{mapping.subject}' (subject) is missing or not a string")
    return subject


def read_profile(document: Mapping[str, Any], mapping: IdentityPathsConfig) -> Profile:
    """Email (only when verified), organization and groups from ``document``."""
    return Profile(
        email=_verified_email(document, mapping),
        organization=_optional_string(document, mapping.organization, what="organization"),
        groups=_optional_groups(document, mapping.groups),
    )


def assemble_identity(subject: str, profile: Profile, claims: Mapping[str, Any]) -> Identity:
    """The identity of ``subject``; ``claims`` are the verified token claims."""
    return Identity(
        subject=subject,
        email=profile.email,
        organization=profile.organization,
        groups=profile.groups,
        claims=MappingProxyType(dict(claims)),
    )


def build_identity(claims: Mapping[str, Any], mapping: IdentityPathsConfig) -> Identity:
    """The identity of a token that carries every claim itself (no userinfo call)."""
    return assemble_identity(read_subject(claims, mapping), read_profile(claims, mapping), claims)
