"""Userinfo profiles shared by every replica, kept in Redis with their values encrypted.

Layout (``p`` is the key prefix, ``d`` the keyed digest of ``("userinfo", access token)``):

* ``{p}u:{d}`` holds the profile (verified email, organization, groups) sealed as described in
  ``mcp_gateway.state.sealing``, with ``p || d`` as associated data and the absolute expiry sealed
  inside the plaintext. Redis also expires the entry after the TTL.

Neither the token nor any part of the profile appears in a key name or, in clear, in a value.
"""

import logging
import math
import time

from pydantic import BaseModel, ConfigDict

from mcp_gateway.identity import Profile
from mcp_gateway.state.digest import KeyDigest
from mcp_gateway.state.redis_client import BoundedRedis
from mcp_gateway.state.sealing import Sealer, read_sealed

logger = logging.getLogger(__name__)

_DIGEST_DOMAIN = "userinfo"


class _StoredProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    email: str | None
    organization: str | None
    groups: list[str]
    expires_at: float


def _serialize(profile: Profile, expires_at: float) -> bytes:
    return (
        _StoredProfile(
            email=profile.email,
            organization=profile.organization,
            groups=list(profile.groups),
            expires_at=expires_at,
        )
        .model_dump_json()
        .encode()
    )


def _deserialize(plaintext: bytes) -> tuple[Profile, float]:
    stored = _StoredProfile.model_validate_json(plaintext, strict=True)
    profile = Profile(
        email=stored.email, organization=stored.organization, groups=tuple(stored.groups)
    )
    return profile, stored.expires_at


class RedisUserinfoStore:
    def __init__(
        self, redis: BoundedRedis, prefix: str, encryption_key: bytes, digest: KeyDigest
    ) -> None:
        self._redis = redis
        self._digest = digest
        self._prefix = prefix
        self._sealer = Sealer(encryption_key, prefix)

    def _names(self, token: str) -> tuple[str, str]:
        """Entry key and the digest that is the entry's associated data."""
        entry = self._digest(_DIGEST_DOMAIN, token)
        return f"{self._prefix}u:{entry}", entry

    async def get(self, token: str) -> Profile | None:
        entry_key, entry = self._names(token)
        return await read_sealed(
            self._redis,
            self._sealer,
            entry_key,
            entry,
            _deserialize,
            what="userinfo cache entry",
            log=logger,
        )

    async def put(self, token: str, profile: Profile, ttl_seconds: float) -> None:
        entry_key, entry = self._names(token)
        sealed = self._sealer.seal(_serialize(profile, time.time() + ttl_seconds), entry)
        ttl_ms = max(1, math.ceil(ttl_seconds * 1000))
        await self._redis.run(self._redis.client.set(entry_key, sealed, px=ttl_ms))
