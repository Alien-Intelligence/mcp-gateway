"""Gateway configuration: one strict JSON document in one environment variable.

Every key is required. There are no field defaults, so a missing key is a startup error and
optional claim paths are written as explicit ``null``.
"""

import os
import re
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


class HmacAuthConfig(_Model):
    type: Literal["hmac"]
    # File holding the shared secret; never inline.
    secret_file: str = Field(min_length=1)


class MtlsAuthConfig(_Model):
    type: Literal["mtls"]
    client_cert_file: str = Field(min_length=1)
    client_key_file: str = Field(min_length=1)


WebhookAuthConfig = Annotated[HmacAuthConfig | MtlsAuthConfig, Field(discriminator="type")]


class WebhookResolverConfig(_Model):
    type: Literal["webhook"]
    url: str
    auth: WebhookAuthConfig
    # Private CA bundle used to verify the webhook server (both auth modes); null = system CAs.
    ca_file: str | None
    default_upstream: str = Field(min_length=1)
    # The reply carries the customer's API key, so the webhook must be https. True lifts that
    # for plain http and exists for dev and test stacks only: false in any real deployment.
    allow_insecure_http: bool
    # Per attempt, covering connect, send and read.
    timeout_seconds: float = Field(gt=0)
    max_attempts: int = Field(ge=1, le=10)
    # Wall-clock ceiling for the whole resolution: every attempt and every backoff pause.
    total_timeout_seconds: float = Field(gt=0)
    backoff_base_seconds: float = Field(gt=0)
    backoff_max_seconds: float = Field(gt=0)
    # Upper bound applied to the ttl_seconds an allow answer asks for.
    max_ttl_seconds: float = Field(gt=0)

    @field_validator("url")
    @classmethod
    def _webhook_url(cls, value: str) -> str:
        return _require_http_url(value, what="webhook url")

    @field_validator("ca_file")
    @classmethod
    def _ca_file_not_blank(cls, value: str | None) -> str | None:
        if value is not None and not value:
            raise ValueError("ca_file must be a path or null")
        return value

    @model_validator(mode="after")
    def _mtls_needs_https(self) -> Self:
        scheme = urlsplit(self.url).scheme
        if scheme != "https" and not self.allow_insecure_http:
            raise ValueError(
                "webhook url must be https (allow_insecure_http=true is for dev and test only)"
            )
        if isinstance(self.auth, MtlsAuthConfig) and scheme != "https":
            raise ValueError("mtls auth needs an https webhook url")
        if self.total_timeout_seconds <= self.timeout_seconds:
            raise ValueError("total_timeout_seconds must be > timeout_seconds")
        if self.backoff_max_seconds < self.backoff_base_seconds:
            raise ValueError("backoff_max_seconds must be >= backoff_base_seconds")
        return self


# Only these named parameters may appear in a store_sql query.
SQL_PLACEHOLDERS = frozenset({"subject", "email", "organization"})
_SQL_PARAM = re.compile(r"%\((\w*)\)s")


def validate_sql_query(query: str) -> str:
    """Accept one SELECT that uses only the allowed named parameters."""
    stripped = query.strip()
    if stripped.endswith(";"):
        stripped = stripped[:-1].rstrip()
    if not re.match(r"select\b", stripped, re.IGNORECASE):
        raise ValueError("query must be a single SELECT statement")
    if ";" in stripped:
        raise ValueError("query must be a single statement with no ';' inside")
    found = _SQL_PARAM.findall(stripped)
    if not found:
        raise ValueError("query must use at least one of %(subject)s, %(email)s, %(organization)s")
    unknown = sorted(set(found) - SQL_PLACEHOLDERS)
    if unknown:
        raise ValueError(f"query uses unsupported parameters: {', '.join(unknown)}")
    # Anything left over once named parameters and the '%%' escape are removed is a positional
    # or malformed placeholder.
    leftover = _SQL_PARAM.sub("", stripped).replace("%%", "")
    if "%" in leftover:
        raise ValueError("query may only use named %(name)s parameters (and %% for a literal %)")
    return stripped


class StoreSqlResolverConfig(_Model):
    type: Literal["store_sql"]
    # File holding the PostgreSQL DSN (it carries the password); never inline.
    dsn_file: str = Field(min_length=1)
    query: str
    default_upstream: str = Field(min_length=1)
    pool_min_size: int = Field(ge=1)
    pool_max_size: int = Field(ge=1)
    connect_timeout_seconds: float = Field(gt=0)
    statement_timeout_seconds: float = Field(gt=0)

    @field_validator("query")
    @classmethod
    def _query_shape(cls, value: str) -> str:
        return validate_sql_query(value)

    @model_validator(mode="after")
    def _pool_bounds(self) -> Self:
        if self.pool_max_size < self.pool_min_size:
            raise ValueError("pool_max_size must be >= pool_min_size")
        return self


KEY_PLACEHOLDERS = frozenset({"subject", "email", "organization"})
_KEY_BRACES = re.compile(r"\{([^{}]*)\}")


def validate_key_pattern(pattern: str) -> str:
    """Accept a pattern whose only braces are the allowed ``{name}`` placeholders."""
    found = _KEY_BRACES.findall(pattern)
    if not found:
        raise ValueError("key_pattern must use at least one of {subject}, {email}, {organization}")
    unknown = sorted(set(found) - KEY_PLACEHOLDERS)
    if unknown:
        raise ValueError(f"key_pattern uses unsupported placeholders: {', '.join(unknown)}")
    if "{" in _KEY_BRACES.sub("", pattern) or "}" in _KEY_BRACES.sub("", pattern):
        raise ValueError("key_pattern has an unbalanced brace")
    return pattern


class StoreRedisResolverConfig(_Model):
    type: Literal["store_redis"]
    # File holding the redis:// or rediss:// URL (it may carry the password); never inline.
    url_file: str = Field(min_length=1)
    key_pattern: str
    default_upstream: str = Field(min_length=1)
    connect_timeout_seconds: float = Field(gt=0)
    socket_timeout_seconds: float = Field(gt=0)
    max_connections: int = Field(ge=1)

    @field_validator("key_pattern")
    @classmethod
    def _pattern_shape(cls, value: str) -> str:
        return validate_key_pattern(value)


# `chain` members: any resolver except another chain (one level only).
LeafResolverConfig = Annotated[
    StaticResolverConfig
    | WebhookResolverConfig
    | StoreSqlResolverConfig
    | StoreRedisResolverConfig,
    Field(discriminator="type"),
]


class ChainResolverConfig(_Model):
    type: Literal["chain"]
    resolvers: list[LeafResolverConfig] = Field(min_length=2)


ResolverConfig = Annotated[
    StaticResolverConfig
    | WebhookResolverConfig
    | StoreSqlResolverConfig
    | StoreRedisResolverConfig
    | ChainResolverConfig,
    Field(discriminator="type"),
]


class ResolverCacheConfig(_Model):
    positive_ttl_seconds: float = Field(gt=0)
    # Short: a denial is re-checked soon so a freshly added mapping works quickly.
    negative_ttl_seconds: float = Field(gt=0)
    # Required with ``state.type = memory``, null with ``redis`` (entries expire by TTL there).
    max_entries: int | None = Field(ge=1)


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
    # The largest ``tools/list`` JSON response the gateway buffers to filter it. Only read when
    # ``policies.tools`` is set; a larger response is replaced by an error, never relayed.
    max_filtered_response_bytes: int = Field(gt=0)


class AuditConfig(_Model):
    enabled: bool


class OtlpConfig(_Model):
    """OTLP export of traces and metrics. Only the endpoint's address is configured here."""

    # Base URL of the collector. http/protobuf appends /v1/traces and /v1/metrics.
    endpoint: str
    protocol: Literal["grpc", "http/protobuf"]
    # File of ``key=value`` lines sent with every export (an auth header); never inline.
    headers_file: str | None
    # Lets an http endpoint carry the headers_file auth header in clear (dev and test only).
    allow_insecure_http: bool
    # Per export attempt.
    timeout_seconds: float = Field(gt=0)
    # Metric collection period; also the span batch delay.
    export_interval_seconds: float = Field(gt=0)
    service_name: str = Field(min_length=1)
    resource_attributes: dict[str, str]

    @field_validator("endpoint")
    @classmethod
    def _endpoint_url(cls, value: str) -> str:
        _require_http_url(value, what="otlp endpoint")
        parts = urlsplit(value)
        if parts.username is not None or parts.password is not None:
            raise ValueError("otlp endpoint must not carry credentials; use headers_file")
        if parts.query or parts.fragment:
            raise ValueError("otlp endpoint must not carry a query or fragment")
        return value

    @field_validator("headers_file")
    @classmethod
    def _headers_file_not_blank(cls, value: str | None) -> str | None:
        if value is not None and not value:
            raise ValueError("headers_file must be a path or null")
        return value

    @field_validator("resource_attributes")
    @classmethod
    def _attribute_keys(cls, value: dict[str, str]) -> dict[str, str]:
        if any(not key for key in value):
            raise ValueError("resource attribute keys must be non-empty")
        if "service.name" in value:
            raise ValueError("set service_name instead of the service.name resource attribute")
        return value

    @model_validator(mode="after")
    def _auth_header_needs_https(self) -> Self:
        if (
            self.headers_file is not None
            and urlsplit(self.endpoint).scheme != "https"
            and not self.allow_insecure_http
        ):
            raise ValueError(
                "otlp endpoint must be https when headers_file is set "
                "(allow_insecure_http=true is for dev and test only)"
            )
        return self

    @model_validator(mode="after")
    def _interval_covers_timeout(self) -> Self:
        if self.export_interval_seconds < self.timeout_seconds:
            raise ValueError("export_interval_seconds must be >= timeout_seconds")
        return self


class ObservabilityConfig(_Model):
    # null installs nothing: no exporter, no provider, no-op instruments.
    otlp: OtlpConfig | None


class SessionsConfig(_Model):
    """Binding of an upstream ``Mcp-Session-Id`` to the subject that created it."""

    # Required with ``state.type = memory``, null with ``redis`` (bindings expire by idle TTL
    # and the per-subject cap there).
    max_entries: int | None = Field(gt=0)
    # One subject over this cap evicts its own oldest binding, never another subject's.
    max_entries_per_subject: int = Field(gt=0)
    idle_ttl_seconds: float = Field(gt=0)

    @model_validator(mode="after")
    def _subject_cap_within_total(self) -> Self:
        if self.max_entries is not None and self.max_entries_per_subject > self.max_entries:
            raise ValueError("max_entries_per_subject must be <= max_entries")
        return self


class MemoryStateConfig(_Model):
    """Per-process state: correct for exactly one replica."""

    type: Literal["memory"]


_KEY_PREFIX = re.compile(r"[A-Za-z0-9_:.-]{1,64}")


class RedisStateConfig(_Model):
    """State shared by every replica in one Redis; any failure of it fails requests closed."""

    type: Literal["redis"]
    # File holding the redis:// or rediss:// URL (it may carry the password); never inline.
    url_file: str = Field(min_length=1)
    # Prepended to every key, so several gateways can share one Redis.
    key_prefix: str
    connect_timeout_seconds: float = Field(gt=0)
    # Ceiling for every single Redis operation, including a script and a readiness ping.
    operation_timeout_seconds: float = Field(gt=0)
    max_connections: int = Field(ge=1)
    # File holding base64 of exactly 32 random bytes: the AES-256-GCM key of cached credentials.
    encryption_key_file: str = Field(min_length=1)

    @field_validator("key_prefix")
    @classmethod
    def _prefix_shape(cls, value: str) -> str:
        if _KEY_PREFIX.fullmatch(value) is None:
            raise ValueError("key_prefix must be 1 to 64 characters of A-Z a-z 0-9 _ : . -")
        return value


StateConfig = Annotated[MemoryStateConfig | RedisStateConfig, Field(discriminator="type")]


MAX_TOOL_PATTERN_LENGTH = 256
# Non-empty, no whitespace, no control characters (a pattern is compared with tool names).
_TOOL_PATTERN = re.compile(r"[^\s\x00-\x1f\x7f]+")


def _tool_patterns(values: list[str], *, what: str) -> list[str]:
    for value in values:
        if len(value) > MAX_TOOL_PATTERN_LENGTH or _TOOL_PATTERN.fullmatch(value) is None:
            raise ValueError(
                f"{what} entries must be 1 to {MAX_TOOL_PATTERN_LENGTH} characters "
                "without whitespace or control characters"
            )
    return values


class ToolAccessConfig(_Model):
    """Which tools a caller may see and call: exact names, ``*`` as the only wildcard."""

    # null allows every tool; a list allows only the tools it matches.
    allow: list[str] | None
    # A tool matching any entry is refused, whatever ``allow`` says.
    deny: list[str]

    @field_validator("allow")
    @classmethod
    def _allow_patterns(cls, value: list[str] | None) -> list[str] | None:
        return None if value is None else _tool_patterns(value, what="allow")

    @field_validator("deny")
    @classmethod
    def _deny_patterns(cls, value: list[str]) -> list[str]:
        return _tool_patterns(value, what="deny")


class OrganizationMatchConfig(_Model):
    """Matches ``Identity.organization`` (the token's organization claim)."""

    organization: str = Field(min_length=1)


class LabelSelectorConfig(_Model):
    name: str = Field(min_length=1)
    value: str


class LabelMatchConfig(_Model):
    """Matches one of the labels the credential resolver returned."""

    label: LabelSelectorConfig


class ToolRuleConfig(ToolAccessConfig):
    match: OrganizationMatchConfig | LabelMatchConfig


class ToolsPolicyConfig(_Model):
    # Applies to every caller that matches no rule.
    default: ToolAccessConfig
    # Tried in order; the first rule that matches is the caller's policy.
    rules: list[ToolRuleConfig]


class TokenBucketConfig(_Model):
    """One token bucket: ``burst`` is the capacity (and the starting level), ``per_minute`` the refill."""

    # The caps keep one typo from announcing a multi-day wait or an effectively absent limit; the
    # refill floor is one token per 1000 minutes.
    burst: int = Field(ge=1, le=1_000_000)
    per_minute: float = Field(ge=0.001, allow_inf_nan=False)


class RateLimitsConfig(_Model):
    """Token buckets per subject and per organization; each null scope is not limited."""

    per_subject: TokenBucketConfig | None
    per_organization: TokenBucketConfig | None

    @model_validator(mode="after")
    def _at_least_one_scope(self) -> Self:
        if self.per_subject is None and self.per_organization is None:
            raise ValueError(
                "rate_limits needs per_subject or per_organization (use null to disable limiting)"
            )
        return self


class PoliciesConfig(_Model):
    # null disables rate limiting.
    rate_limits: RateLimitsConfig | None
    # Browser origins allowed on the MCP endpoint, as scheme://host[:port]. An empty list
    # refuses every request that carries an Origin header; requests without one pass.
    allowed_origins: list[str]
    # null: no tool policy, responses are relayed byte for byte and never parsed.
    tools: ToolsPolicyConfig | None

    @field_validator("allowed_origins")
    @classmethod
    def _normalised_origins(cls, value: list[str]) -> list[str]:
        normalised: list[str] = []
        for item in value:
            origin = normalize_origin(item)
            if origin is None or urlsplit(origin).scheme not in ("http", "https"):
                raise ValueError(
                    "allowed_origins entries must be http(s) origins such as "
                    "'https://app.example.com' ('null' is refused)"
                )
            normalised.append(origin)
        return normalised


class GatewayConfig(_Model):
    public_url: str
    listen: ListenConfig
    auth: AuthConfig
    resolver: ResolverConfig
    # null disables the cache.
    resolver_cache: ResolverCacheConfig | None
    upstreams: dict[str, UpstreamConfig] = Field(min_length=1)
    limits: LimitsConfig
    audit: AuditConfig
    observability: ObservabilityConfig
    sessions: SessionsConfig
    state: StateConfig
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

    @model_validator(mode="after")
    def _bounds_match_the_state_backend(self) -> Self:
        """The in-process bounds belong to ``memory`` only; none is silently ignored."""
        bounds = {"sessions.max_entries": self.sessions.max_entries}
        if self.resolver_cache is not None:
            bounds["resolver_cache.max_entries"] = self.resolver_cache.max_entries
        for name, value in bounds.items():
            if self.state.type == "memory" and value is None:
                raise ValueError(f"{name} is required when state.type is memory")
            if self.state.type == "redis" and value is not None:
                raise ValueError(f"{name} must be null when state.type is redis")
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
