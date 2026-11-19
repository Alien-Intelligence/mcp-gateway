"""JWT verification against a cached remote JWKS."""

import asyncio
import logging
import time
from collections.abc import Callable, Mapping
from typing import Any

import httpx
import jwt
from jwt import PyJWK, PyJWKSet

from mcp_gateway.config import AuthConfig

logger = logging.getLogger(__name__)

# Minimum spacing between JWKS fetches triggered by an unknown kid.
KID_REFETCH_MIN_INTERVAL_SECONDS = 10.0

_DESCRIPTIONS: dict[type[jwt.PyJWTError], str] = {
    jwt.ExpiredSignatureError: "token expired",
    jwt.ImmatureSignatureError: "token not yet valid",
    jwt.InvalidAudienceError: "token audience does not match this resource",
    jwt.InvalidIssuerError: "token issuer is not trusted",
    jwt.MissingRequiredClaimError: "token is missing a required claim",
    jwt.InvalidSignatureError: "token signature is invalid",
    jwt.InvalidAlgorithmError: "token algorithm is not accepted",
}


class AuthError(Exception):
    def __init__(self, description: str) -> None:
        super().__init__(description)
        self.description = description


class InvalidTokenError(AuthError):
    """The token is not acceptable (401 invalid_token)."""


class InsufficientScopeError(AuthError):
    """The token is valid but lacks required scopes (403 insufficient_scope)."""


class JwksUnavailableError(AuthError):
    """The key set could not be fetched (503, never 401: re-auth would not help)."""


def token_scopes(claims: Mapping[str, Any]) -> frozenset[str]:
    """Scopes from ``scope`` (space-separated string) and/or ``scp`` (list of strings)."""
    scopes: set[str] = set()
    scope = claims.get("scope")
    if isinstance(scope, str):
        scopes.update(scope.split())
    scp = claims.get("scp")
    if isinstance(scp, str):
        scopes.update(scp.split())
    elif isinstance(scp, list):
        scopes.update(item for item in scp if isinstance(item, str) and item)
    return frozenset(scopes)


class TokenVerifier:
    def __init__(
        self,
        config: AuthConfig,
        client: httpx.AsyncClient,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._config = config
        self._client = client
        self._clock = clock
        self._lock = asyncio.Lock()
        self._keys: dict[str, PyJWK] = {}
        self._unkeyed: list[PyJWK] = []
        self._fetched_at: float | None = None
        self._last_attempt: float | None = None
        self._fetch_seq = 0
        self._last_fetch_ok = True

    async def verify(self, token: str) -> dict[str, Any]:
        """Return the verified claims or raise an ``AuthError`` subclass."""
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError as exc:
            raise InvalidTokenError("token is malformed") from exc
        alg = header.get("alg")
        if not isinstance(alg, str) or alg not in self._config.algorithms:
            raise InvalidTokenError("token algorithm is not accepted")
        kid = header.get("kid")
        key = await self._key_for(kid if isinstance(kid, str) else None)
        try:
            claims: dict[str, Any] = jwt.decode(
                token,
                key=key.key,
                algorithms=[alg],
                audience=self._config.audiences,
                issuer=self._config.issuer,
                leeway=self._config.clock_skew_seconds,
                options={"require": ["exp", "iss", "aud"]},
            )
        except jwt.PyJWTError as exc:
            description = next(
                (text for kind, text in _DESCRIPTIONS.items() if isinstance(exc, kind)),
                "token is invalid",
            )
            raise InvalidTokenError(description) from exc
        self._check_scopes(claims)
        return claims

    def _check_scopes(self, claims: Mapping[str, Any]) -> None:
        required = self._config.required_scopes
        missing = [scope for scope in required if scope not in token_scopes(claims)]
        if missing:
            raise InsufficientScopeError(f"missing required scope: {' '.join(missing)}")

    def _age(self) -> float | None:
        return None if self._fetched_at is None else self._clock() - self._fetched_at

    def _expired(self) -> bool:
        age = self._age()
        return age is None or age >= self._config.jwks_cache_ttl_seconds

    def _stale_but_usable(self) -> bool:
        """A key set that outlived its TTL but is still inside the stale-serving window."""
        age = self._age()
        limit = self._config.jwks_cache_ttl_seconds + self._config.jwks_max_stale_seconds
        return age is not None and age < limit

    def _may_fetch(self) -> bool:
        last = self._last_attempt
        return last is None or self._clock() - last >= KID_REFETCH_MIN_INTERVAL_SECONDS

    def _lookup(self, kid: str | None) -> PyJWK | None:
        if kid is not None:
            return self._keys.get(kid)
        everything = [*self._keys.values(), *self._unkeyed]
        return everything[0] if len(everything) == 1 else None

    async def _ensure_fresh(self) -> None:
        """Refresh an expired key set; fall back to the cached one while it is usable.

        With a cached set in hand, fetches are spaced by the minimum interval so an IdP outage
        costs one fetch per interval, not one per request. With nothing cached there is no
        alternative to a fetch.
        """
        if not self._expired():
            return
        have_cache = self._fetched_at is not None
        if have_cache and not self._may_fetch():
            if self._stale_but_usable():
                return
            raise JwksUnavailableError("identity provider keys are unavailable")
        try:
            await self._refresh(self._fetch_seq)
        except JwksUnavailableError:
            if not self._stale_but_usable():
                raise
            logger.warning(
                "JWKS refresh failed; serving the cached key set (age %.0f s)", self._age() or 0.0
            )

    async def _key_for(self, kid: str | None) -> PyJWK:
        await self._ensure_fresh()
        key = self._lookup(kid)
        if key is not None:
            return key
        if kid is not None and self._may_fetch():
            await self._refresh(self._fetch_seq)
            key = self._lookup(kid)
            if key is not None:
                return key
        raise InvalidTokenError("token signing key is unknown")

    async def _refresh(self, seen_seq: int) -> None:
        """Fetch the JWKS once for all concurrent callers that saw the same generation."""
        async with self._lock:
            if self._fetch_seq != seen_seq:
                # Another caller fetched while we waited: share its outcome.
                if not self._last_fetch_ok:
                    raise JwksUnavailableError("identity provider keys are unavailable")
                return
            self._last_attempt = self._clock()
            try:
                keyset = await self._fetch()
            except JwksUnavailableError:
                self._last_fetch_ok = False
                self._fetch_seq += 1
                raise
            self._keys = {k.key_id: k for k in keyset.keys if k.key_id}
            self._unkeyed = [k for k in keyset.keys if not k.key_id]
            self._fetched_at = self._clock()
            self._last_fetch_ok = True
            self._fetch_seq += 1

    async def _fetch(self) -> PyJWKSet:
        try:
            response = await self._client.get(
                self._config.jwks_uri, timeout=self._config.jwks_fetch_timeout_seconds
            )
            response.raise_for_status()
            document = response.json()
            if not isinstance(document, dict):
                raise ValueError("JWKS document is not a JSON object")
            return PyJWKSet.from_dict(document)
        except (httpx.HTTPError, ValueError, jwt.PyJWTError) as exc:
            logger.error("JWKS fetch failed (%s)", type(exc).__name__)
            raise JwksUnavailableError("identity provider keys are unavailable") from exc
