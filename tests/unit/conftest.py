"""Shared fixtures: an RSA signing key, a fake IdP (JWKS), a fake upstream and config helpers."""

import asyncio
import copy
import json
import time
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

from mcp_gateway.config import GatewayConfig, parse_config

ISSUER = "https://idp.example.com/realms/test"
JWKS_URI = "https://idp.example.com/realms/test/certs"
PUBLIC_URL = "https://gw.example.com/mcp"
UPSTREAM_URL = "https://upstream.example.com/mcp"
KID = "key-1"
FAKE_API_KEY = "fake-alien-key-testkey_do_not_leak_0123456789"


def base_config_dict(mapping_file: str = "/unused/mapping.json") -> dict[str, Any]:
    return {
        "public_url": PUBLIC_URL,
        "listen": {"host": "0.0.0.0", "port": 8000},
        "auth": {
            "issuer": ISSUER,
            "jwks_uri": JWKS_URI,
            "audiences": [PUBLIC_URL],
            "algorithms": ["RS256"],
            "required_scopes": [],
            "scopes_advertised": ["openid", "mcp-gateway"],
            "clock_skew_seconds": 5,
            "jwks_cache_ttl_seconds": 300,
            "jwks_max_stale_seconds": 3600,
            "jwks_fetch_timeout_seconds": 5,
            "identity": {
                "subject": "sub",
                "email": "email",
                "email_verified": "email_verified",
                "organization": "org",
                "groups": "groups",
            },
        },
        "resolver": {"type": "static", "mapping_file": mapping_file},
        "resolver_cache": None,
        "upstreams": {
            "main": {
                "url": UPSTREAM_URL,
                "connect_timeout_seconds": 10,
                "read_timeout_seconds": 130,
                "write_timeout_seconds": 30,
                "pool_timeout_seconds": 10,
            }
        },
        "limits": {
            "max_request_body_bytes": 1048576,
            "request_body_timeout_seconds": 30,
            "max_sse_event_bytes": 65536,
        },
        "audit": {"enabled": True},
        "observability": {"otlp": None},
        "sessions": {"max_entries": 100, "max_entries_per_subject": 20, "idle_ttl_seconds": 600},
        "policies": {"allowed_origins": ["https://app.example.com"]},
    }


def config_from(data: dict[str, Any]) -> GatewayConfig:
    return parse_config(json.dumps(data))


@pytest.fixture
def config_dict() -> dict[str, Any]:
    return copy.deepcopy(base_config_dict())


@pytest.fixture(scope="session")
def rsa_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="session")
def other_rsa_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def jwk_for(key: rsa.RSAPrivateKey, kid: str) -> dict[str, Any]:
    jwk = json.loads(RSAAlgorithm.to_jwk(key.public_key()))
    return {**jwk, "kid": kid, "use": "sig", "alg": "RS256"}


def mint(
    key: Any,
    *,
    kid: str | None = KID,
    alg: str = "RS256",
    overrides: dict[str, Any] | None = None,
    drop: tuple[str, ...] = (),
) -> str:
    now = int(time.time())
    claims: dict[str, Any] = {
        "iss": ISSUER,
        "aud": PUBLIC_URL,
        "sub": "user-1",
        "email": "alice@client-x.example",
        "email_verified": True,
        "org": "client-x",
        "exp": now + 300,
        "iat": now,
    }
    claims.update(overrides or {})
    for name in drop:
        claims.pop(name, None)
    headers = {"kid": kid} if kid is not None else {}
    return jwt.encode(claims, key, algorithm=alg, headers=headers)


class FakeIdp:
    """Serves a mutable JWKS and counts fetches."""

    def __init__(self, keys: dict[str, rsa.RSAPrivateKey]) -> None:
        self.keys = keys
        self.calls = 0
        self.down = False

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        if self.down:
            return httpx.Response(503, json={"error": "down"})
        return httpx.Response(
            200, json={"keys": [jwk_for(key, kid) for kid, key in self.keys.items()]}
        )


class TrackedStream(httpx.AsyncByteStream):
    """Response body that records whether it was closed; optionally never ends."""

    def __init__(
        self, chunks: list[bytes], *, hang: bool = False, fail_after: Exception | None = None
    ) -> None:
        self._chunks = chunks
        self._hang = hang
        self._fail_after = fail_after
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self._chunks:
            yield chunk
        if self._fail_after is not None:
            raise self._fail_after
        if self._hang:
            await asyncio.Event().wait()

    async def aclose(self) -> None:
        self.closed = True


class _Replay(httpx.AsyncByteStream):
    """Re-streams a body that httpx already buffered when the Response was built."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    async def __aiter__(self) -> AsyncIterator[bytes]:
        async for chunk in self._inner:
            yield chunk


class FakeUpstream:
    """Records upstream requests and answers with a settable responder."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.responder: Callable[[httpx.Request], httpx.Response] = lambda _req: httpx.Response(
            200, json={"jsonrpc": "2.0", "id": 1, "result": {}}
        )

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        response = self.responder(request)
        if response.is_stream_consumed:
            # Real transports hand the proxy an unread stream; mirror that.
            response = httpx.Response(
                response.status_code, headers=response.headers, stream=_Replay(response.stream)
            )
        return response


@pytest.fixture
def idp(rsa_key: rsa.RSAPrivateKey) -> FakeIdp:
    return FakeIdp({KID: rsa_key})


@pytest.fixture
def upstream() -> FakeUpstream:
    return FakeUpstream()


@pytest.fixture
def transport(idp: FakeIdp, upstream: FakeUpstream) -> httpx.MockTransport:
    def route(request: httpx.Request) -> httpx.Response:
        if request.url.host == "idp.example.com":
            return idp.handle(request)
        if request.url.host == "upstream.example.com":
            return upstream.handle(request)
        raise httpx.ConnectError("unexpected host", request=request)

    return httpx.MockTransport(route)


@pytest.fixture
def mapping_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "state"
    directory.mkdir()
    (directory / "alice.key").write_text(f"{FAKE_API_KEY}\n")
    (directory / "mapping.json").write_text(
        json.dumps(
            {
                "entries": [
                    {
                        "match": {"email": "alice@client-x.example"},
                        "api_key_file": "alice.key",
                        "upstream": "main",
                        "labels": {"client": "client-x"},
                    }
                ]
            }
        )
    )
    return directory
