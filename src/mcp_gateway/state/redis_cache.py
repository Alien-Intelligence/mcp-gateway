"""Resolver cache shared by every replica, kept in Redis with its values encrypted.

Layout (``p`` is the key prefix, ``d`` the keyed digest of the canonical identity key):

* ``{p}c:{d}`` holds the outcome (a credential or a denial) as
  ``base64(key id || nonce || ciphertext)``: AES-256-GCM with a random 12-byte nonce and
  ``p || d`` as associated data, so a value moved to another entry or to another gateway's
  prefix fails to decrypt. The key id (4 bytes derived from the encryption key) says which key
  sealed it, so a replica still holding the previous key leaves the new key's entries alone
  and the reverse. The absolute expiry (the writer's wall clock) is sealed inside the plaintext:
  a value replayed into Redis without its TTL is refused once past it. Redis also expires the
  entry after the TTL.
* ``{p}cg:{d}`` is the invalidation generation. ``invalidate`` deletes the entry and increments
  the generation; a lookup reads the generation before asking the backend and its answer is
  written only if the generation is unchanged (one Lua script). A lookup still running on
  another replica therefore cannot put back a credential that was just invalidated.

The identity (subject, e-mail, organization, groups) never appears in clear: it is digested
into the key names, and the API key only exists inside the ciphertext.
"""

import base64
import binascii
import logging
import math
import os
import time
from typing import Annotated, Literal

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from pydantic import BaseModel, ConfigDict, Field, SecretStr, TypeAdapter, ValidationError

from mcp_gateway.config import ConfigError
from mcp_gateway.resolver.base import Credential, Denied
from mcp_gateway.resolver.cache import CacheKey, Outcome, canonical_cache_key
from mcp_gateway.state.digest import KeyDigest
from mcp_gateway.state.redis_client import BoundedRedis, read_text_file

logger = logging.getLogger(__name__)

_NONCE_BYTES = 12
_KEY_ID_BYTES = 4
_KEY_ID_INFO = b"mcp-gateway key id"
_KEY_BYTES = 32
# The generation outlives any lookup in flight by a wide margin and is refreshed by each bump.
_GENERATION_TTL_MS = 24 * 60 * 60 * 1000

# KEYS: entry, generation. ARGV: generation the lookup started under, value, ttl ms.
_PUT = """
local current = redis.call('GET', KEYS[2])
if not current then current = '' end
if current ~= ARGV[1] then return 0 end
redis.call('SET', KEYS[1], ARGV[2], 'PX', ARGV[3])
return 1
"""

# KEYS: entry, generation. ARGV: generation ttl ms.
_INVALIDATE = """
redis.call('DEL', KEYS[1])
redis.call('INCR', KEYS[2])
redis.call('PEXPIRE', KEYS[2], ARGV[1])
return 1
"""


class _StoredCredential(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["credential"]
    api_key: str
    upstream: str
    labels: dict[str, str]
    ttl_seconds: float | None
    expires_at: float


class _StoredDenial(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["denied"]
    reason: str
    expires_at: float


_Stored = Annotated[_StoredCredential | _StoredDenial, Field(discriminator="kind")]
_STORED: TypeAdapter[_StoredCredential | _StoredDenial] = TypeAdapter(_Stored)


def load_encryption_key(path: str) -> bytes:
    """Read the base64 AES-256 key file at startup. The error never echoes its content."""
    text = read_text_file(path, what="state encryption_key_file")
    try:
        key = base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ConfigError("state encryption_key_file does not hold valid base64") from exc
    if len(key) != _KEY_BYTES:
        raise ConfigError(f"state encryption_key_file must decode to exactly {_KEY_BYTES} bytes")
    return key


def key_id(encryption_key: bytes) -> bytes:
    """A short public label of the key, derived so that it reveals nothing about the key itself."""
    return HKDF(
        algorithm=hashes.SHA256(), length=_KEY_ID_BYTES, salt=None, info=_KEY_ID_INFO
    ).derive(encryption_key)


def _serialize(outcome: Outcome, expires_at: float) -> str:
    if isinstance(outcome, Denied):
        return _StoredDenial(
            kind="denied", reason=outcome.reason, expires_at=expires_at
        ).model_dump_json()
    return _StoredCredential(
        kind="credential",
        api_key=outcome.api_key.get_secret_value(),
        upstream=outcome.upstream,
        labels=dict(outcome.labels),
        ttl_seconds=outcome.ttl_seconds,
        expires_at=expires_at,
    ).model_dump_json()


def _deserialize(raw: bytes) -> tuple[Outcome, float]:
    """The outcome and the absolute time (epoch seconds) after which it must not be used."""
    stored = _STORED.validate_json(raw, strict=True)
    if isinstance(stored, _StoredDenial):
        return Denied(stored.reason), stored.expires_at
    return (
        Credential(
            api_key=SecretStr(stored.api_key),
            upstream=stored.upstream,
            labels=stored.labels,
            ttl_seconds=stored.ttl_seconds,
        ),
        stored.expires_at,
    )


class RedisCacheStore:
    def __init__(
        self, redis: BoundedRedis, prefix: str, encryption_key: bytes, digest: KeyDigest
    ) -> None:
        self._redis = redis
        self._digest = digest
        self._prefix = prefix
        self._cipher = AESGCM(encryption_key)
        self._key_id = key_id(encryption_key)
        self._put = redis.client.register_script(_PUT)
        self._invalidate = redis.client.register_script(_INVALIDATE)

    def _names(self, key: CacheKey) -> tuple[str, str, str]:
        """Entry key, generation key and the digest that is the entry's associated data."""
        entry = self._digest(canonical_cache_key(key))
        return f"{self._prefix}c:{entry}", f"{self._prefix}cg:{entry}", entry

    def _associated_data(self, entry: str) -> bytes:
        """Bind a value to its entry and to this gateway's prefix."""
        return f"{self._prefix}\x1f{entry}".encode()

    def _seal(self, outcome: Outcome, entry: str, expires_at: float) -> str:
        nonce = os.urandom(_NONCE_BYTES)
        sealed = self._cipher.encrypt(
            nonce, _serialize(outcome, expires_at).encode(), self._associated_data(entry)
        )
        return base64.b64encode(self._key_id + nonce + sealed).decode("ascii")

    def _open(self, raw: bytes, entry: str) -> tuple[Outcome, float]:
        """Authenticate and decode ``raw`` (key id already checked). Raises on any failure."""
        body = raw[_KEY_ID_BYTES:]
        return _deserialize(
            self._cipher.decrypt(
                body[:_NONCE_BYTES], body[_NONCE_BYTES:], self._associated_data(entry)
            )
        )

    async def _drop(self, entry_key: str, why: str) -> None:
        # Never trusted, never an error for the caller: the entry goes and the caller sees a
        # miss. Nothing of the value is logged.
        logger.warning("shared cache entry %s; dropped", why)
        await self._redis.run(self._redis.client.delete(entry_key))

    async def get(self, key: CacheKey) -> Outcome | None:
        entry_key, _, entry = self._names(key)
        value = await self._redis.run(self._redis.client.get(entry_key))
        if value is None:
            return None
        try:
            raw = base64.b64decode(value, validate=True)
        except (binascii.Error, ValueError):
            raw = b""
        if len(raw) <= _KEY_ID_BYTES + _NONCE_BYTES:
            await self._drop(entry_key, "is malformed")
            return None
        if raw[:_KEY_ID_BYTES] != self._key_id:
            # Sealed by another key: a replica of another key generation (a rolling key change)
            # owns it. Not ours to read and not ours to delete.
            logger.debug("shared cache entry sealed by another key; treated as a miss")
            return None
        try:
            outcome, expires_at = self._open(raw, entry)
        except (InvalidTag, ValueError, ValidationError) as exc:
            # Our key id on a value that does not authenticate: it was altered or truncated.
            await self._drop(entry_key, f"failed authentication ({type(exc).__name__})")
            return None
        if expires_at <= time.time():
            # Redis' TTL did not do its job (a replay without TTL, a persisted dump restored
            # late): the sealed expiry is the authority.
            logger.debug("shared cache entry past its sealed expiry; dropped")
            await self._redis.run(self._redis.client.delete(entry_key))
            return None
        return outcome

    async def generation(self, key: CacheKey) -> str:
        _, generation_key, _ = self._names(key)
        value = await self._redis.run(self._redis.client.get(generation_key))
        return "" if value is None else str(value)

    async def put(
        self, key: CacheKey, outcome: Outcome, ttl_seconds: float, generation: str
    ) -> None:
        entry_key, generation_key, entry = self._names(key)
        ttl_ms = max(1, math.ceil(ttl_seconds * 1000))
        stored = await self._redis.run(
            self._put(
                keys=[entry_key, generation_key],
                args=[generation, self._seal(outcome, entry, time.time() + ttl_seconds), ttl_ms],
            )
        )
        if stored == 0:
            logger.info("a lookup finished after its entry was invalidated; answer not cached")

    async def invalidate(self, key: CacheKey) -> None:
        entry_key, generation_key, _ = self._names(key)
        await self._redis.run(
            self._invalidate(keys=[entry_key, generation_key], args=[_GENERATION_TTL_MS])
        )
