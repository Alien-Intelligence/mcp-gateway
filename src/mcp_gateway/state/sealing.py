"""AES-256-GCM sealing of values kept in Redis, shared by every store that keeps secrets there.

A sealed value is ``base64(key id || nonce || ciphertext)``: AES-256-GCM with a random 12-byte
nonce and ``prefix || 0x1f || entry`` as associated data, so a value moved to another entry or to
another gateway's prefix fails to decrypt. The key id (4 bytes derived from the encryption key)
says which key sealed it, so a replica still holding the previous key leaves the new key's
entries alone and the reverse. The absolute expiry (the writer's wall clock) travels inside the
plaintext, which the caller serializes: a value replayed into Redis without its TTL is refused
once past it.

The format is stored data: every replica, old and new, must read what another wrote. Do not
change it without a migration.
"""

import base64
import binascii
import logging
import os
import time
from collections.abc import Callable

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from pydantic import ValidationError

from mcp_gateway.state.redis_client import BoundedRedis

_NONCE_BYTES = 12
_KEY_ID_BYTES = 4
_KEY_ID_INFO = b"mcp-gateway key id"


def key_id(encryption_key: bytes) -> bytes:
    """A short public label of the key, derived so that it reveals nothing about the key itself."""
    return HKDF(
        algorithm=hashes.SHA256(), length=_KEY_ID_BYTES, salt=None, info=_KEY_ID_INFO
    ).derive(encryption_key)


class SealedValueError(Exception):
    """A value that claims our key but is malformed or does not authenticate. It is dropped."""


class Sealer:
    """Seals and opens the values of one gateway (one key, one key prefix)."""

    def __init__(self, encryption_key: bytes, prefix: str) -> None:
        self._cipher = AESGCM(encryption_key)
        self._key_id = key_id(encryption_key)
        self._prefix = prefix

    def _associated_data(self, entry: str) -> bytes:
        """Bind a value to its entry and to this gateway's prefix."""
        return f"{self._prefix}\x1f{entry}".encode()

    def seal(self, plaintext: bytes, entry: str) -> str:
        nonce = os.urandom(_NONCE_BYTES)
        sealed = self._cipher.encrypt(nonce, plaintext, self._associated_data(entry))
        return base64.b64encode(self._key_id + nonce + sealed).decode("ascii")

    def open(self, value: str | bytes, entry: str) -> bytes | None:
        """The plaintext, None when another key sealed it, ``SealedValueError`` when it is damaged."""
        try:
            raw = base64.b64decode(value, validate=True)
        except (binascii.Error, ValueError):
            raw = b""
        if len(raw) <= _KEY_ID_BYTES + _NONCE_BYTES:
            raise SealedValueError("is malformed")
        if raw[:_KEY_ID_BYTES] != self._key_id:
            return None
        body = raw[_KEY_ID_BYTES:]
        try:
            return self._cipher.decrypt(
                body[:_NONCE_BYTES], body[_NONCE_BYTES:], self._associated_data(entry)
            )
        except InvalidTag as exc:
            raise SealedValueError(f"failed authentication ({type(exc).__name__})") from exc


async def read_sealed[T](
    redis: BoundedRedis,
    sealer: Sealer,
    entry_key: str,
    entry: str,
    decode: Callable[[bytes], tuple[T, float]],
    *,
    what: str,
    log: logging.Logger,
) -> T | None:
    """Read, authenticate and decode one sealed entry; every way it can be unusable is a miss.

    ``decode`` turns the plaintext into the stored value and its absolute expiry (epoch seconds).
    A malformed value, one that fails authentication or one that does not decode under our key is
    deleted and logged by class only (never its content). A value sealed by another key is left
    alone: a replica of another key generation owns it. A value past its sealed expiry is deleted.
    Redis failures raise ``StateUnavailableError``.
    """
    value = await redis.run(redis.client.get(entry_key))
    if value is None:
        return None
    try:
        plaintext = sealer.open(value, entry)
        if plaintext is None:
            # Sealed by another key: not ours to read and not ours to delete.
            log.debug("%s sealed by another key; treated as a miss", what)
            return None
        stored, expires_at = decode(plaintext)
    except SealedValueError as exc:
        # Our key id on a value that does not authenticate: altered or truncated.
        log.warning("%s %s; dropped", what, exc)
        await redis.run(redis.client.delete(entry_key))
        return None
    except (ValueError, ValidationError) as exc:
        # It authenticated but is not what this gateway writes.
        log.warning("%s failed authentication (%s); dropped", what, type(exc).__name__)
        await redis.run(redis.client.delete(entry_key))
        return None
    if expires_at <= time.time():
        # Redis' TTL did not do its job (a replay without TTL, a persisted dump restored late):
        # the sealed expiry is the authority.
        log.debug("%s past its sealed expiry; dropped", what)
        await redis.run(redis.client.delete(entry_key))
        return None
    return stored
