"""Resolver cache shared by every replica, kept in Redis with its values encrypted.

Layout (``p`` is the key prefix, ``d`` the keyed digest of the canonical identity key):

* ``{p}c:{d}`` holds the outcome (a credential or a denial), sealed as described in
  ``mcp_gateway.state.sealing`` (AES-256-GCM, ``p || d`` as associated data, the absolute expiry
  sealed inside the plaintext). Redis also expires the entry after the TTL.
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
import time
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, TypeAdapter

from mcp_gateway.config import ConfigError
from mcp_gateway.resolver.base import Credential, Denied
from mcp_gateway.resolver.cache import CacheKey, Outcome, canonical_cache_key
from mcp_gateway.state.digest import KeyDigest
from mcp_gateway.state.redis_client import BoundedRedis, read_text_file
from mcp_gateway.state.sealing import Sealer, read_sealed

logger = logging.getLogger(__name__)

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
        self._sealer = Sealer(encryption_key, prefix)
        self._put = redis.client.register_script(_PUT)
        self._invalidate = redis.client.register_script(_INVALIDATE)

    def _names(self, key: CacheKey) -> tuple[str, str, str]:
        """Entry key, generation key and the digest that is the entry's associated data."""
        entry = self._digest(canonical_cache_key(key))
        return f"{self._prefix}c:{entry}", f"{self._prefix}cg:{entry}", entry

    async def get(self, key: CacheKey) -> Outcome | None:
        entry_key, _, entry = self._names(key)
        return await read_sealed(
            self._redis,
            self._sealer,
            entry_key,
            entry,
            _deserialize,
            what="shared cache entry",
            log=logger,
        )

    async def generation(self, key: CacheKey) -> str:
        _, generation_key, _ = self._names(key)
        value = await self._redis.run(self._redis.client.get(generation_key))
        return "" if value is None else str(value)

    async def put(
        self, key: CacheKey, outcome: Outcome, ttl_seconds: float, generation: str
    ) -> None:
        entry_key, generation_key, entry = self._names(key)
        ttl_ms = max(1, math.ceil(ttl_seconds * 1000))
        sealed = self._sealer.seal(_serialize(outcome, time.time() + ttl_seconds).encode(), entry)
        stored = await self._redis.run(
            self._put(
                keys=[entry_key, generation_key],
                args=[generation, sealed, ttl_ms],
            )
        )
        if stored == 0:
            logger.info("a lookup finished after its entry was invalidated; answer not cached")

    async def invalidate(self, key: CacheKey) -> None:
        entry_key, generation_key, _ = self._names(key)
        await self._redis.run(
            self._invalidate(keys=[entry_key, generation_key], args=[_GENERATION_TTL_MS])
        )
