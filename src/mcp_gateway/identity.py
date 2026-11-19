"""Identity extraction from verified JWT claims using configurable claim paths."""

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


def _optional_groups(claims: Mapping[str, Any], path: str | None) -> tuple[str, ...]:
    if path is None:
        return ()
    value = _lookup(claims, path)
    if value is _MISSING or value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise IdentityError(f"claim '{path}' (groups) must be a list of strings")
    return tuple(item for item in value if item)


def build_identity(claims: Mapping[str, Any], mapping: IdentityPathsConfig) -> Identity:
    subject = _lookup(claims, mapping.subject)
    if not isinstance(subject, str) or not subject:
        raise IdentityError(f"claim '{mapping.subject}' (subject) is missing or not a string")
    return Identity(
        subject=subject,
        email=_optional_string(claims, mapping.email, what="email"),
        organization=_optional_string(claims, mapping.organization, what="organization"),
        groups=_optional_groups(claims, mapping.groups),
        claims=MappingProxyType(dict(claims)),
    )
