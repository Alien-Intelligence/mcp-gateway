"""Shared pieces of the userinfo tests: a config, a fake userinfo endpoint and a routing transport."""

import inspect
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
from pydantic import SecretStr

from mcp_gateway.config import IdentityPathsConfig, UserinfoConfig
from mcp_gateway.identity import Identity
from mcp_gateway.resolver import Credential, Denied

from .conftest import FakeIdp, FakeUpstream

USERINFO_URL = "https://userinfo.example.com/oidc/userinfo"
USERINFO_HOST = "userinfo.example.com"

PATHS = IdentityPathsConfig(
    subject="sub",
    email="email",
    email_verified="email_verified",
    organization="org",
    groups="groups",
)

DOCUMENT: dict[str, Any] = {
    "sub": "user-1",
    "email": "alice@client-x.example",
    "email_verified": True,
    "org": "client-x",
    "groups": ["staff"],
}


def userinfo_dict(**overrides: Any) -> dict[str, Any]:
    """A valid ``auth.userinfo`` object with short waits, so retries do not slow the suite."""
    data: dict[str, Any] = {
        "url": USERINFO_URL,
        "allow_insecure_http": False,
        "timeout_seconds": 2,
        "max_attempts": 3,
        "total_timeout_seconds": 10,
        "backoff_base_seconds": 0.001,
        "backoff_max_seconds": 0.002,
        "cache_ttl_seconds": 60,
        "max_response_bytes": 65536,
        "max_entries": 100,
    }
    data.update(overrides)
    return data


def userinfo_config(**overrides: Any) -> UserinfoConfig:
    return UserinfoConfig.model_validate(userinfo_dict(**overrides))


type Responder = Callable[[httpx.Request], httpx.Response | Awaitable[httpx.Response]]


def json_response(document: Any, **kwargs: Any) -> httpx.Response:
    return httpx.Response(200, json=document, **kwargs)


class FakeUserinfo:
    """Records the userinfo requests and answers with a settable responder."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.responder: Responder = lambda _request: json_response(DOCUMENT)

    @property
    def calls(self) -> int:
        return len(self.requests)

    @property
    def tokens(self) -> list[str]:
        return [r.headers["authorization"].removeprefix("Bearer ") for r in self.requests]

    async def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        answer = self.responder(request)
        return await answer if inspect.isawaitable(answer) else answer


def userinfo_transport(
    idp: FakeIdp, upstream: FakeUpstream, userinfo: FakeUserinfo
) -> httpx.MockTransport:
    async def route(request: httpx.Request) -> httpx.Response:
        if request.url.host == "idp.example.com":
            return idp.handle(request)
        if request.url.host == USERINFO_HOST:
            return await userinfo.handle(request)
        if request.url.host == "upstream.example.com":
            return upstream.handle(request)
        raise httpx.ConnectError("unexpected host", request=request)

    return httpx.MockTransport(route)


class RecordingResolver:
    """Grants every identity a credential and remembers the identities it was asked about."""

    def __init__(self) -> None:
        self.identities: list[Identity] = []

    async def resolve(self, identity: Identity) -> Credential | Denied:
        self.identities.append(identity)
        return Credential(
            api_key=SecretStr("fake-alien-key-testkey_do_not_leak_0123456789"),
            upstream="main",
            labels={},
            ttl_seconds=None,
        )

    async def invalidate(self, identity: Identity) -> None:
        return None

    async def aclose(self) -> None:
        return None
