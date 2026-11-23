"""The OIDC userinfo request (OIDC Core 5.3): one bearer-authenticated GET, validated.

``GET <auth.userinfo.url>`` with the caller's own access token and ``Accept: application/json``.
The reply must be a 200 whose media type is ``application/json`` (UTF-8) and whose body is one
JSON object with a string ``sub`` equal to the ``sub`` of the verified token (OIDC Core 5.3.4).
What the gateway reads from it (verified email, organization, groups) is a ``Profile``.

Failure handling, one class per answer the gateway gives:

* ``UserinfoUnavailableError``: a timeout, a transport error, status 429 or any 5xx, still failing
  after ``max_attempts`` attempts (exponential backoff with equal jitter) or after
  ``total_timeout_seconds`` for everything. The caller fails closed with a 503.
* ``UserinfoRejectedError``: status 401 or 403. The IdP no longer accepts the token. Not retried.
* ``UserinfoSubjectMismatchError``: the document describes another subject. Not retried.
* ``UserinfoClaimsError``: a claim the identity paths name has the wrong type. Not retried.
* ``UserinfoResponseError``: a server certificate that fails verification, any other status, a body that is too large, not UTF-8, not JSON, not an
  object, a signed or encrypted (``application/jwt``) reply, a missing ``sub``. Not retried.

Messages and logs name the setting and the failure class, never the URL, the token or the document.
"""

import logging
import random
from collections.abc import Mapping
from email.message import Message
from typing import Any

import anyio
import httpx

from mcp_gateway import boundedjson
from mcp_gateway.config import IdentityPathsConfig, UserinfoConfig
from mcp_gateway.identity import IdentityError, Profile, read_profile
from mcp_gateway.jsonrpc import declares_non_utf8_charset
from mcp_gateway.tls import is_certificate_failure

logger = logging.getLogger(__name__)

# Statuses worth another attempt: the IdP is overloaded or broken, not answering about the token.
RETRYABLE_STATUS = 429
SERVER_ERROR_FIRST = 500
REJECTED_STATUSES = frozenset({401, 403})
JSON_MEDIA_TYPE = "application/json"
JWT_MEDIA_TYPE = "application/jwt"


class UserinfoError(Exception):
    """Base of every userinfo failure. The message carries no URL, token or document content."""


class UserinfoUnavailableError(UserinfoError):
    """The IdP could not be reached or kept failing: retry later (503)."""


class UserinfoRejectedError(UserinfoError):
    """The IdP answered 401 or 403 for this token."""


class UserinfoSubjectMismatchError(UserinfoError):
    """The userinfo ``sub`` is not the token's ``sub``."""


class UserinfoClaimsError(UserinfoError):
    """A claim read from the userinfo document has the wrong type."""


class UserinfoResponseError(UserinfoError):
    """The IdP answered something the gateway cannot use (500)."""


class _TransientError(Exception):
    """One attempt failed in a way that may pass; ``why`` carries no payload."""

    def __init__(self, why: str) -> None:
        super().__init__(why)
        self.why = why


def _is_transient_status(status: int) -> bool:
    return status == RETRYABLE_STATUS or status >= SERVER_ERROR_FIRST


def _check_media_type(headers: httpx.Headers) -> None:
    declared = headers.get("content-type")
    if declared is None:
        raise UserinfoResponseError("userinfo response has no Content-Type")
    message = Message()
    message["content-type"] = declared
    media_type = message.get_content_type()
    if media_type == JWT_MEDIA_TYPE:
        raise UserinfoResponseError(
            "signed or encrypted userinfo responses (application/jwt) are not supported"
        )
    if media_type != JSON_MEDIA_TYPE:
        raise UserinfoResponseError("userinfo response is not application/json")
    if declares_non_utf8_charset(headers.items()):
        raise UserinfoResponseError("userinfo response declares a charset other than utf-8")


def _parse_document(raw: bytes) -> dict[str, Any]:
    try:
        document = boundedjson.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise UserinfoResponseError(
            f"userinfo response is not valid UTF-8 JSON ({type(exc).__name__})"
        ) from exc
    if not isinstance(document, dict):
        raise UserinfoResponseError("userinfo response is not a JSON object")
    return document


class UserinfoClient:
    def __init__(
        self,
        config: UserinfoConfig,
        mapping: IdentityPathsConfig,
        client: httpx.AsyncClient,
    ) -> None:
        self._config = config
        self._mapping = mapping
        self._client = client

    async def fetch(self, token: str, token_subject: str) -> Profile:
        """The profile of the token's user, from the IdP, or a ``UserinfoError``.

        ``token_subject`` is the literal ``sub`` of the verified token.
        """
        document = await self._document(token)
        sub = document.get("sub")
        if not isinstance(sub, str) or not sub:
            raise UserinfoResponseError("userinfo response has no string sub")
        if sub != token_subject:
            raise UserinfoSubjectMismatchError("userinfo sub is not the token sub")
        try:
            return read_profile(document, self._mapping)
        except IdentityError as exc:
            # Names the configured claim path, never a value.
            raise UserinfoClaimsError(f"userinfo claims rejected: {exc}") from exc

    async def _document(self, token: str) -> dict[str, Any]:
        try:
            with anyio.fail_after(self._config.total_timeout_seconds):
                return _parse_document(await self._retrying(token))
        except TimeoutError as exc:
            raise UserinfoUnavailableError(
                f"auth.userinfo unavailable: no answer within {self._config.total_timeout_seconds}s"
            ) from exc

    def _backoff(self, attempt: int) -> float:
        """Equal jitter: half the exponential step is fixed, half random."""
        step = min(
            self._config.backoff_max_seconds, self._config.backoff_base_seconds * 2 ** (attempt - 1)
        )
        return step / 2 + random.uniform(0, step / 2)

    async def _retrying(self, token: str) -> bytes:
        attempts = self._config.max_attempts
        why = "no attempt made"
        for attempt in range(1, attempts + 1):
            try:
                return await self._attempt(token)
            except _TransientError as exc:
                why = exc.why
                logger.warning("auth.userinfo attempt %d/%d failed (%s)", attempt, attempts, why)
            if attempt < attempts:
                await anyio.sleep(self._backoff(attempt))
        raise UserinfoUnavailableError(
            f"auth.userinfo unavailable after {attempts} attempts ({why})"
        )

    async def _attempt(self, token: str) -> bytes:
        """One GET. Returns the body of a 200; raises ``_TransientError`` or a ``UserinfoError``."""
        headers: Mapping[str, str] = {
            "Authorization": f"Bearer {token}",
            "Accept": JSON_MEDIA_TYPE,
        }
        try:
            with anyio.fail_after(self._config.timeout_seconds):
                async with self._client.stream(
                    "GET", self._config.url, headers=headers
                ) as response:
                    return await self._read(response)
        except (TimeoutError, httpx.TimeoutException) as exc:
            raise _TransientError("timeout") from exc
        except httpx.TransportError as exc:
            if is_certificate_failure(exc):
                # Retrying cannot fix a certificate the gateway does not trust.
                raise UserinfoResponseError(
                    "auth.userinfo TLS certificate verification failed"
                ) from exc
            raise _TransientError(type(exc).__name__) from exc
        except httpx.HTTPError as exc:
            raise UserinfoResponseError(
                f"auth.userinfo request failed ({type(exc).__name__})"
            ) from exc

    async def _read(self, response: httpx.Response) -> bytes:
        status = response.status_code
        if _is_transient_status(status):
            raise _TransientError(f"status {status}")
        if status in REJECTED_STATUSES:
            raise UserinfoRejectedError(f"auth.userinfo answered status {status}")
        if status != 200:
            raise UserinfoResponseError(f"auth.userinfo answered status {status}")
        _check_media_type(response.headers)
        received = bytearray()
        async for chunk in response.aiter_bytes():
            received.extend(chunk)
            if len(received) > self._config.max_response_bytes:
                raise UserinfoResponseError("userinfo response is too large")
        return bytes(received)
