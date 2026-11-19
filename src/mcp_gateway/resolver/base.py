"""The credential resolver hook: identity in, Alien API key (or a denial) out."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from pydantic import SecretStr

from mcp_gateway.identity import Identity


class ResolverError(Exception):
    """The resolver failed (not a denial). Maps to a generic 500 for the client."""


@dataclass(frozen=True)
class Credential:
    api_key: SecretStr
    upstream: str
    labels: Mapping[str, str]


@dataclass(frozen=True)
class Denied:
    """Stable machine reason such as ``no_mapping`` or ``ambiguous_mapping``."""

    reason: str


class CredentialResolver(Protocol):
    async def resolve(self, identity: Identity) -> Credential | Denied: ...

    async def invalidate(self, identity: Identity) -> None: ...
