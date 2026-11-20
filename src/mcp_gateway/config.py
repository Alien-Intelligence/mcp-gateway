"""Gateway configuration: one strict JSON document in one environment variable.

Every key is required. There are no field defaults, so a missing key is a startup error and
optional claim paths are written as explicit ``null``.
"""

import os
from typing import Annotated, Literal, Self
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from mcp_gateway.origin import normalize_origin

CONFIG_ENV_VAR = "MCP_GATEWAY_CONFIG"

# Asymmetric algorithms only: HS* would let anyone holding the public key forge tokens.
ALLOWED_ALGORITHMS = frozenset(
    {
        "RS256",
        "RS384",
        "RS512",
        "PS256",
        "PS384",
        "PS512",
        "ES256",
        "ES256K",
        "ES384",
        "ES512",
        "EdDSA",
    }
)


class ConfigError(Exception):
    """The gateway configuration is missing or invalid. The message never carries values."""


def _require_http_url(value: str, *, what: str) -> str:
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ValueError(f"{what} must be an absolute http(s) URL")
    return value


def _require_dotted_path(value: str, *, what: str) -> str:
    if not value or any(not segment for segment in value.split(".")):
        raise ValueError(f"{what} must be a non-empty dotted path such as 'ext.org'")
    return value


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ListenConfig(_Model):
    host: str = Field(min_length=1)
    port: int = Field(ge=1, le=65535)


class IdentityPathsConfig(_Model):
    subject: str
    email: str | None
    # Boolean claim that must be true before ``email`` is trusted; null means never trusted.
    email_verified: str | None
    organization: str | None
    groups: str | None

    @field_validator("subject")
    @classmethod
    def _subject_path(cls, value: str) -> str:
        return _require_dotted_path(value, what="identity.subject")

    @field_validator("email", "email_verified", "organization", "groups")
    @classmethod
    def _optional_path(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _require_dotted_path(value, what="identity claim path")


class AuthConfig(_Model):
    issuer: str
    jwks_uri: str
    audiences: list[str] = Field(min_length=1)
    algorithms: list[str] = Field(min_length=1)
    required_scopes: list[str]
    scopes_advertised: list[str] = Field(min_length=1)
    clock_skew_seconds: float = Field(ge=0)
    jwks_cache_ttl_seconds: float = Field(gt=0)
    # How long past the TTL a cached key set is still served when a refresh fails.
    jwks_max_stale_seconds: float = Field(ge=0)
    jwks_fetch_timeout_seconds: float = Field(gt=0)
    identity: IdentityPathsConfig

    @field_validator("issuer")
    @classmethod
    def _issuer_url(cls, value: str) -> str:
        return _require_http_url(value, what="auth.issuer")

    @field_validator("jwks_uri")
    @classmethod
    def _jwks_url(cls, value: str) -> str:
        return _require_http_url(value, what="auth.jwks_uri")

    @field_validator("algorithms")
    @classmethod
    def _asymmetric_only(cls, value: list[str]) -> list[str]:
        refused = sorted(set(value) - ALLOWED_ALGORITHMS)
        if refused:
            raise ValueError(
                f"unsupported or symmetric algorithms refused: {', '.join(refused)}; "
                f"allowed: {', '.join(sorted(ALLOWED_ALGORITHMS))}"
            )
        return value

    @field_validator("audiences", "scopes_advertised")
    @classmethod
    def _no_blank_entries(cls, value: list[str]) -> list[str]:
        if any(not item.strip() or " " in item for item in value):
            raise ValueError("entries must be non-empty and contain no spaces")
        return value

    @field_validator("required_scopes")
    @classmethod
    def _scopes_have_no_spaces(cls, value: list[str]) -> list[str]:
        if any(not item.strip() or " " in item for item in value):
            raise ValueError("entries must be non-empty and contain no spaces")
        return value


class StaticResolverConfig(_Model):
    type: Literal["static"]
    mapping_file: str = Field(min_length=1)


# Add further resolver kinds as union members; `type` discriminates.
ResolverConfig = Annotated[StaticResolverConfig, Field(discriminator="type")]


class UpstreamConfig(_Model):
    url: str
    connect_timeout_seconds: float = Field(gt=0)
    read_timeout_seconds: float = Field(gt=0)
    write_timeout_seconds: float = Field(gt=0)
    pool_timeout_seconds: float = Field(gt=0)

    @field_validator("url")
    @classmethod
    def _upstream_url(cls, value: str) -> str:
        return _require_http_url(value, what="upstream url")


class LimitsConfig(_Model):
    max_request_body_bytes: int = Field(gt=0)
    request_body_timeout_seconds: float = Field(gt=0)
    # One SSE event larger than this aborts the stream: the relay never buffers beyond one event.
    max_sse_event_bytes: int = Field(gt=0)


class AuditConfig(_Model):
    enabled: bool


class SessionsConfig(_Model):
    """Binding of an upstream ``Mcp-Session-Id`` to the subject that created it (in memory)."""

    max_entries: int = Field(gt=0)
    idle_ttl_seconds: float = Field(gt=0)


class PoliciesConfig(_Model):
    # Browser origins allowed on the MCP endpoint, as scheme://host[:port]. An empty list
    # refuses every request that carries an Origin header; requests without one pass.
    allowed_origins: list[str]

    @field_validator("allowed_origins")
    @classmethod
    def _normalised_origins(cls, value: list[str]) -> list[str]:
        normalised: list[str] = []
        for item in value:
            origin = normalize_origin(item)
            if origin is None:
                raise ValueError(
                    "allowed_origins entries must be origins such as 'https://app.example.com'"
                )
            normalised.append(origin)
        return normalised


class GatewayConfig(_Model):
    public_url: str
    listen: ListenConfig
    auth: AuthConfig
    resolver: ResolverConfig
    upstreams: dict[str, UpstreamConfig] = Field(min_length=1)
    limits: LimitsConfig
    audit: AuditConfig
    sessions: SessionsConfig
    policies: PoliciesConfig

    @field_validator("public_url")
    @classmethod
    def _public_url(cls, value: str) -> str:
        _require_http_url(value, what="public_url")
        parts = urlsplit(value)
        if parts.path in ("", "/"):
            raise ValueError("public_url must have a non-empty path such as '/mcp'")
        if parts.query or parts.fragment:
            raise ValueError("public_url must not carry a query or fragment")
        return value

    @model_validator(mode="after")
    def _audience_contains_public_url(self) -> Self:
        if self.public_url not in self.auth.audiences:
            raise ValueError("public_url must be listed in auth.audiences")
        return self

    @property
    def public_path(self) -> str:
        return urlsplit(self.public_url).path

    @property
    def public_origin(self) -> str:
        parts = urlsplit(self.public_url)
        return f"{parts.scheme}://{parts.netloc}"

    @property
    def resource_metadata_path(self) -> str:
        return f"/.well-known/oauth-protected-resource{self.public_path}"

    @property
    def resource_metadata_url(self) -> str:
        return f"{self.public_origin}{self.resource_metadata_path}"


def parse_config(raw: str) -> GatewayConfig:
    """Parse and validate a JSON config document."""
    try:
        return GatewayConfig.model_validate_json(raw, strict=True)
    except ValidationError as exc:
        # Report location and rule only: never echo input values.
        problems = "; ".join(
            f"{'.'.join(str(part) for part in err['loc']) or '<root>'}: {err['msg']}"
            for err in exc.errors(include_input=False, include_url=False)
        )
        raise ConfigError(f"invalid {CONFIG_ENV_VAR}: {problems}") from exc


def load_config() -> GatewayConfig:
    """Load the config from the environment, failing fast with a clear message."""
    raw = os.environ.get(CONFIG_ENV_VAR)
    if raw is None or not raw.strip():
        raise ConfigError(f"{CONFIG_ENV_VAR} is not set; it must hold the gateway config as JSON")
    return parse_config(raw)
