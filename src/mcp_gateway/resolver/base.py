"""The credential resolver hook: identity in, Alien API key (or a denial) out."""

import re
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from typing import Protocol

from pydantic import BaseModel, ConfigDict, SecretStr, ValidationError

from mcp_gateway.identity import Identity


class ResolverError(Exception):
    """The resolver failed (not a denial). Maps to a generic 500 for the client."""


class ResolverUnavailableError(ResolverError):
    """A backend is down or too slow after bounded retries. Maps to 503 + ``Retry-After``."""


@dataclass(frozen=True)
class Credential:
    api_key: SecretStr
    upstream: str
    labels: Mapping[str, str]
    # Resolver-requested cache lifetime (only the webhook sets it); the cache caps it.
    ttl_seconds: float | None = None


@dataclass(frozen=True)
class Denied:
    """Stable machine reason such as ``no_mapping`` or ``ambiguous_mapping``."""

    reason: str


class CredentialResolver(Protocol):
    async def resolve(self, identity: Identity) -> Credential | Denied: ...

    async def invalidate(self, identity: Identity) -> None: ...

    async def aclose(self) -> None:
        """Release connections and pools. Called once by the app lifespan."""
        ...


# A machine reason is a short token: it ends up in audit logs and is mapped to a JSON-RPC code.
_REASON = re.compile(r"[a-z][a-z0-9_]{0,63}")


def is_machine_reason(value: str) -> bool:
    return _REASON.fullmatch(value) is not None


def clean_api_key(text: str) -> str | None:
    """Return the stripped key, or None when it is empty, non-ASCII or has whitespace inside."""
    key = text.strip()
    if not key or not key.isascii() or any(ch.isspace() or not ch.isprintable() for ch in key):
        return None
    return key


def describe_validation_error(exc: ValidationError) -> str:
    """Location and rule only: a backend payload may carry a key, so never echo input."""
    return "; ".join(
        f"{'.'.join(str(p) for p in err['loc']) or '<root>'}: {err['msg']}"
        for err in exc.errors(include_input=False, include_url=False)
    )


class BackendMapping(BaseModel):
    """What a store row or value (and the webhook ``allow`` body) must look like."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    api_key: str
    upstream: str | None
    labels: dict[str, str] | None


def parse_backend_mapping(raw: str | bytes) -> BackendMapping:
    try:
        return BackendMapping.model_validate_json(raw, strict=True)
    except ValidationError as exc:
        raise ResolverError(f"mapping value rejected: {describe_validation_error(exc)}") from exc


def build_credential(
    *,
    source: str,
    api_key: str,
    upstream: str | None,
    labels: Mapping[str, str] | None,
    default_upstream: str,
    upstream_names: Collection[str],
    ttl_seconds: float | None = None,
) -> Credential:
    """Validate a backend answer the way the static resolver validates its key files."""
    key = clean_api_key(api_key)
    if key is None:
        raise ResolverError(f"{source} returned an empty or malformed api key")
    chosen = default_upstream if upstream is None else upstream
    if chosen not in upstream_names:
        raise ResolverError(f"{source} returned an upstream that is not configured")
    return Credential(
        api_key=SecretStr(key),
        upstream=chosen,
        labels=dict(labels) if labels is not None else {},
        ttl_seconds=ttl_seconds,
    )


def read_secret_file(path: str, *, what: str) -> str:
    """Read a secret file at startup. The error names the setting, never the content."""
    try:
        with open(path, encoding="utf-8") as handle:
            value = handle.read().strip()
    except (OSError, UnicodeDecodeError) as exc:
        raise ResolverError(f"{what}: file unreadable ({type(exc).__name__})") from exc
    if not value:
        raise ResolverError(f"{what}: file is empty")
    return value
