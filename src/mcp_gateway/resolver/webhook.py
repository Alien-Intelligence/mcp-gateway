"""Webhook resolver: ``POST`` the identity to a customer endpoint, get a decision back.

Request: ``POST <url>`` with a compact JSON body holding only the identity the gateway
verified, never the raw token claims and never the token::

    {"subject": "...", "email": "..." | null, "organization": "..." | null, "groups": ["..."]}

Authentication of the gateway is one of:

* ``hmac``: headers ``X-MCP-Gateway-Timestamp`` (unix seconds) and
  ``X-MCP-Gateway-Signature: sha256=<hex>`` where the digest is
  ``HMAC-SHA256(secret, f"{timestamp}.{body}")`` over the exact body bytes. The receiver must
  compare in constant time and reject timestamps outside a short window.
* ``mtls``: the gateway presents a client certificate; the receiver verifies it.

Response contract, HTTP 200 with a JSON body, either::

    {"decision": "allow", "api_key": "oat_...", "upstream": "<name>" | null,
     "labels": {"k": "v"}, "ttl_seconds": <number> | null}
    {"decision": "deny", "reason": "<machine_reason>"}

``upstream: null`` selects the configured ``default_upstream``; a name must be a configured
upstream. ``ttl_seconds`` asks the cache to keep the answer that long, capped by
``max_ttl_seconds``. ``reason`` is a lower-case token (``a-z0-9_``) used in audit logs.
Anything else (other status, extra or missing keys, wrong types) is a resolver error.

Failure handling: each attempt has a timeout. Connection errors, timeouts, 429, 502, 503 and
504 are retried with exponential backoff and jitter up to ``max_attempts``; after the last one
the resolver raises ``ResolverUnavailableError`` (the request fails closed). Any other
status is a ``ResolverError`` immediately.
"""

import hmac
import json
import logging
import random
import ssl
import time
from collections.abc import Collection
from hashlib import sha256
from typing import Annotated, Literal

import anyio
import httpx
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from mcp_gateway.config import HmacAuthConfig, MtlsAuthConfig, WebhookResolverConfig
from mcp_gateway.identity import Identity
from mcp_gateway.resolver.base import (
    Credential,
    Denied,
    ResolverError,
    ResolverUnavailableError,
    build_credential,
    describe_validation_error,
    is_machine_reason,
    read_secret_file,
)

logger = logging.getLogger(__name__)

TIMESTAMP_HEADER = "X-MCP-Gateway-Timestamp"
SIGNATURE_HEADER = "X-MCP-Gateway-Signature"
# Statuses worth another attempt; everything else non-200 is a verdict about the request.
RETRYABLE_STATUSES = frozenset({429, 502, 503, 504})
# A decision is a few hundred bytes; refuse anything that looks like a runaway body.
MAX_RESPONSE_BYTES = 64 * 1024


def sign(secret: bytes, timestamp: int, body: bytes) -> str:
    """The ``X-MCP-Gateway-Signature`` value for ``body`` sent at ``timestamp``."""
    digest = hmac.new(secret, f"{timestamp}.".encode() + body, sha256).hexdigest()
    return f"sha256={digest}"


class _Allow(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    decision: Literal["allow"]
    api_key: str
    upstream: str | None
    labels: dict[str, str]
    ttl_seconds: float | None = Field(gt=0)


class _Deny(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    decision: Literal["deny"]
    reason: str


_DECISION: TypeAdapter[_Allow | _Deny] = TypeAdapter(
    Annotated[_Allow | _Deny, Field(discriminator="decision")]
)


class _TransientError(Exception):
    """One attempt failed in a way that may pass; ``why`` carries no payload."""

    def __init__(self, why: str) -> None:
        super().__init__(why)
        self.why = why


def _request_body(identity: Identity) -> bytes:
    return json.dumps(
        {
            "subject": identity.subject,
            "email": identity.email,
            "organization": identity.organization,
            "groups": list(identity.groups),
        },
        separators=(",", ":"),
    ).encode()


def _ssl_context(config: WebhookResolverConfig) -> ssl.SSLContext:
    try:
        context = ssl.create_default_context(cafile=config.ca_file)
        if isinstance(config.auth, MtlsAuthConfig):
            context.load_cert_chain(config.auth.client_cert_file, config.auth.client_key_file)
    except (OSError, ssl.SSLError) as exc:
        raise ResolverError(f"webhook TLS material unusable ({type(exc).__name__})") from exc
    return context


class WebhookResolver:
    def __init__(self, config: WebhookResolverConfig, upstream_names: Collection[str]) -> None:
        if config.default_upstream not in upstream_names:
            raise ResolverError("webhook default_upstream is not a configured upstream")
        self._config = config
        self._upstream_names = frozenset(upstream_names)
        self._secret: bytes | None = None
        if isinstance(config.auth, HmacAuthConfig):
            self._secret = read_secret_file(
                config.auth.secret_file, what="webhook hmac secret"
            ).encode()
        self._client = httpx.AsyncClient(
            verify=_ssl_context(config),
            timeout=config.timeout_seconds,
            follow_redirects=False,
        )

    def _headers(self, body: bytes) -> dict[str, str]:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self._secret is not None:
            timestamp = int(time.time())
            headers[TIMESTAMP_HEADER] = str(timestamp)
            headers[SIGNATURE_HEADER] = sign(self._secret, timestamp, body)
        return headers

    def _backoff(self, attempt: int) -> float:
        """Equal jitter: half the exponential step is fixed, half random."""
        step = min(
            self._config.backoff_max_seconds, self._config.backoff_base_seconds * 2 ** (attempt - 1)
        )
        return step / 2 + random.uniform(0, step / 2)

    async def _attempt(self, body: bytes) -> bytes:
        """One POST. Returns the body of a 200; raises ``_TransientError`` or ``ResolverError``."""
        try:
            with anyio.fail_after(self._config.timeout_seconds):
                async with self._client.stream(
                    "POST", self._config.url, content=body, headers=self._headers(body)
                ) as response:
                    if response.status_code in RETRYABLE_STATUSES:
                        raise _TransientError(f"status {response.status_code}")
                    if response.status_code != 200:
                        raise ResolverError(f"webhook answered status {response.status_code}")
                    received = bytearray()
                    async for chunk in response.aiter_bytes():
                        received.extend(chunk)
                        if len(received) > MAX_RESPONSE_BYTES:
                            raise ResolverError("webhook answer is too large")
                    return bytes(received)
        except (TimeoutError, httpx.TimeoutException) as exc:
            raise _TransientError("timeout") from exc
        except httpx.TransportError as exc:
            raise _TransientError(type(exc).__name__) from exc
        except httpx.HTTPError as exc:
            raise ResolverError(f"webhook request failed ({type(exc).__name__})") from exc

    async def _post(self, body: bytes) -> bytes:
        attempts = self._config.max_attempts
        why = "no attempt made"
        for attempt in range(1, attempts + 1):
            try:
                return await self._attempt(body)
            except _TransientError as exc:
                why = exc.why
                logger.warning("webhook attempt %d/%d failed (%s)", attempt, attempts, why)
            if attempt < attempts:
                await anyio.sleep(self._backoff(attempt))
        raise ResolverUnavailableError(f"webhook unavailable after {attempts} attempts ({why})")

    def _decide(self, raw: bytes) -> Credential | Denied:
        try:
            answer = _DECISION.validate_json(raw, strict=True)
        except ValidationError as exc:
            raise ResolverError(
                f"webhook answer rejected: {describe_validation_error(exc)}"
            ) from exc
        if isinstance(answer, _Deny):
            if not is_machine_reason(answer.reason):
                raise ResolverError("webhook denial reason is not a machine reason")
            return Denied(answer.reason)
        ttl = answer.ttl_seconds
        return build_credential(
            source="webhook",
            api_key=answer.api_key,
            upstream=answer.upstream,
            labels=answer.labels,
            default_upstream=self._config.default_upstream,
            upstream_names=self._upstream_names,
            ttl_seconds=None if ttl is None else min(ttl, self._config.max_ttl_seconds),
        )

    async def resolve(self, identity: Identity) -> Credential | Denied:
        return self._decide(await self._post(_request_body(identity)))

    async def invalidate(self, identity: Identity) -> None:
        """Nothing is cached here; the cache wrapper owns invalidation."""
        del identity

    async def aclose(self) -> None:
        await self._client.aclose()
