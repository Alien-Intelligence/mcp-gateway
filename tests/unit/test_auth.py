import asyncio
import time
from typing import Any

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from mcp_gateway.auth import (
    KID_REFETCH_MIN_INTERVAL_SECONDS,
    InsufficientScopeError,
    InvalidTokenError,
    JwksUnavailableError,
    TokenVerifier,
    token_scopes,
)

from .conftest import (
    ISSUER,
    JWKS_URI,
    KID,
    PUBLIC_URL,
    FakeIdp,
    base_config_dict,
    config_from,
    mint,
)


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def make_verifier(
    idp: FakeIdp, *, clock: Clock | None = None, **auth_overrides: Any
) -> TokenVerifier:
    data = base_config_dict()
    data["auth"].update(auth_overrides)
    config = config_from(data).auth
    client = httpx.AsyncClient(transport=httpx.MockTransport(idp.handle))
    return TokenVerifier(config, client, clock=clock or Clock())


async def test_valid_token_returns_claims(idp: FakeIdp, rsa_key: rsa.RSAPrivateKey) -> None:
    claims = await make_verifier(idp).verify(mint(rsa_key))
    assert claims["sub"] == "user-1"
    assert claims["org"] == "client-x"


async def test_audience_list_in_token_matches_on_intersection(
    idp: FakeIdp, rsa_key: rsa.RSAPrivateKey
) -> None:
    token = mint(rsa_key, overrides={"aud": ["https://other.example.com", PUBLIC_URL]})
    assert (await make_verifier(idp).verify(token))["sub"] == "user-1"


@pytest.mark.parametrize(
    ("overrides", "drop", "text"),
    [
        ({"aud": "https://other.example.com/mcp"}, (), "audience"),
        ({"iss": "https://evil.example.com"}, (), "issuer"),
        ({"exp": 1}, (), "expired"),
        ({"nbf": 4_102_444_800}, (), "not yet valid"),
        ({}, ("exp",), "required claim"),
        ({}, ("iss",), "required claim"),
        ({}, ("aud",), "required claim"),
    ],
)
async def test_invalid_tokens_are_rejected(
    idp: FakeIdp,
    rsa_key: rsa.RSAPrivateKey,
    overrides: dict[str, Any],
    drop: tuple[str, ...],
    text: str,
) -> None:
    with pytest.raises(InvalidTokenError, match=text):
        await make_verifier(idp).verify(mint(rsa_key, overrides=overrides, drop=drop))


async def test_clock_skew_leeway_applies_to_exp_and_nbf(
    idp: FakeIdp, rsa_key: rsa.RSAPrivateKey
) -> None:
    now = int(time.time())
    verifier = make_verifier(idp, clock_skew_seconds=30)
    await verifier.verify(mint(rsa_key, overrides={"exp": now - 10}))
    await verifier.verify(mint(rsa_key, overrides={"nbf": now + 10}))
    with pytest.raises(InvalidTokenError):
        await verifier.verify(mint(rsa_key, overrides={"exp": now - 60}))


async def test_signature_from_another_key_is_rejected(
    idp: FakeIdp, other_rsa_key: rsa.RSAPrivateKey
) -> None:
    with pytest.raises(InvalidTokenError, match="signature"):
        await make_verifier(idp).verify(mint(other_rsa_key))


async def test_hs256_token_is_refused_even_signed_with_the_public_key(idp: FakeIdp) -> None:
    secret = "a-shared-secret-of-sufficient-length-0123456789"
    token = jwt.encode(
        {"iss": ISSUER, "aud": PUBLIC_URL, "sub": "u", "exp": 4_102_444_800},
        secret,
        algorithm="HS256",
        headers={"kid": KID},
    )
    with pytest.raises(InvalidTokenError, match="algorithm"):
        await make_verifier(idp).verify(token)


async def test_alg_none_token_is_refused(idp: FakeIdp) -> None:
    token = jwt.encode(
        {"iss": ISSUER, "aud": PUBLIC_URL, "sub": "u", "exp": 4_102_444_800},
        None,
        algorithm="none",
        headers={"kid": KID},
    )
    with pytest.raises(InvalidTokenError, match="algorithm"):
        await make_verifier(idp).verify(token)


@pytest.mark.parametrize("token", ["", "garbage", "a.b.c", "Bearer x"])
async def test_malformed_tokens_are_invalid_not_errors(idp: FakeIdp, token: str) -> None:
    with pytest.raises(InvalidTokenError):
        await make_verifier(idp).verify(token)


async def test_jwks_is_cached_between_requests(idp: FakeIdp, rsa_key: rsa.RSAPrivateKey) -> None:
    verifier = make_verifier(idp)
    for _ in range(3):
        await verifier.verify(mint(rsa_key))
    assert idp.calls == 1


async def test_cache_expires_after_ttl(idp: FakeIdp, rsa_key: rsa.RSAPrivateKey) -> None:
    clock = Clock()
    verifier = make_verifier(idp, clock=clock, jwks_cache_ttl_seconds=60)
    await verifier.verify(mint(rsa_key))
    clock.now += 59
    await verifier.verify(mint(rsa_key))
    assert idp.calls == 1
    clock.now += 2
    await verifier.verify(mint(rsa_key))
    assert idp.calls == 2


async def test_unknown_kid_triggers_one_refetch_and_finds_the_new_key(
    idp: FakeIdp, rsa_key: rsa.RSAPrivateKey, other_rsa_key: rsa.RSAPrivateKey
) -> None:
    clock = Clock()
    verifier = make_verifier(idp, clock=clock)
    await verifier.verify(mint(rsa_key))
    assert idp.calls == 1
    idp.keys["key-2"] = other_rsa_key  # the IdP rotated
    clock.now += KID_REFETCH_MIN_INTERVAL_SECONDS + 1
    claims = await verifier.verify(mint(other_rsa_key, kid="key-2"))
    assert claims["sub"] == "user-1"
    assert idp.calls == 2


async def test_unknown_kid_refetch_is_rate_limited(
    idp: FakeIdp, rsa_key: rsa.RSAPrivateKey, other_rsa_key: rsa.RSAPrivateKey
) -> None:
    clock = Clock()
    verifier = make_verifier(idp, clock=clock)
    await verifier.verify(mint(rsa_key))
    clock.now += KID_REFETCH_MIN_INTERVAL_SECONDS + 1
    for _ in range(5):
        with pytest.raises(InvalidTokenError, match="unknown"):
            await verifier.verify(mint(other_rsa_key, kid="ghost"))
    assert idp.calls == 2  # one initial fetch, one refetch; the flood did not reach the IdP
    clock.now += KID_REFETCH_MIN_INTERVAL_SECONDS + 1
    with pytest.raises(InvalidTokenError):
        await verifier.verify(mint(other_rsa_key, kid="ghost"))
    assert idp.calls == 3


async def test_concurrent_requests_share_one_fetch(
    idp: FakeIdp, rsa_key: rsa.RSAPrivateKey
) -> None:
    verifier = make_verifier(idp)
    results = await asyncio.gather(*(verifier.verify(mint(rsa_key)) for _ in range(10)))
    assert len(results) == 10
    assert idp.calls == 1


async def test_token_without_kid_uses_the_only_key(
    idp: FakeIdp, rsa_key: rsa.RSAPrivateKey
) -> None:
    claims = await make_verifier(idp).verify(mint(rsa_key, kid=None))
    assert claims["sub"] == "user-1"


async def test_jwks_http_error_is_unavailable_not_invalid(
    idp: FakeIdp, rsa_key: rsa.RSAPrivateKey
) -> None:
    idp.down = True
    with pytest.raises(JwksUnavailableError):
        await make_verifier(idp).verify(mint(rsa_key))


async def test_jwks_network_error_and_garbage_are_unavailable(rsa_key: rsa.RSAPrivateKey) -> None:
    config = config_from(base_config_dict()).auth

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    verifier = TokenVerifier(config, httpx.AsyncClient(transport=httpx.MockTransport(refuse)))
    with pytest.raises(JwksUnavailableError):
        await verifier.verify(mint(rsa_key))

    garbage = TokenVerifier(
        config,
        httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _r: httpx.Response(200, text="<html>"))
        ),
    )
    with pytest.raises(JwksUnavailableError):
        await garbage.verify(mint(rsa_key))

    empty = TokenVerifier(
        config,
        httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _r: httpx.Response(200, json={"keys": []}))
        ),
    )
    with pytest.raises(JwksUnavailableError):
        await empty.verify(mint(rsa_key))


async def test_jwks_recovers_after_an_outage(idp: FakeIdp, rsa_key: rsa.RSAPrivateKey) -> None:
    verifier = make_verifier(idp)
    idp.down = True
    with pytest.raises(JwksUnavailableError):
        await verifier.verify(mint(rsa_key))
    idp.down = False
    assert (await verifier.verify(mint(rsa_key)))["sub"] == "user-1"


async def test_jwks_fetch_uses_the_configured_uri_and_timeout(
    rsa_key: rsa.RSAPrivateKey,
) -> None:
    seen: list[httpx.Request] = []
    idp = FakeIdp({KID: rsa_key})

    def spy(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return idp.handle(request)

    config = config_from(base_config_dict()).auth
    verifier = TokenVerifier(config, httpx.AsyncClient(transport=httpx.MockTransport(spy)))
    await verifier.verify(mint(rsa_key))
    assert str(seen[0].url) == JWKS_URI
    assert seen[0].extensions["timeout"]["read"] == 5


async def test_required_scopes_from_scope_string(idp: FakeIdp, rsa_key: rsa.RSAPrivateKey) -> None:
    verifier = make_verifier(idp, required_scopes=["mcp-gateway"])
    await verifier.verify(mint(rsa_key, overrides={"scope": "openid mcp-gateway"}))


async def test_required_scopes_from_scp_list(idp: FakeIdp, rsa_key: rsa.RSAPrivateKey) -> None:
    verifier = make_verifier(idp, required_scopes=["mcp-gateway"])
    await verifier.verify(mint(rsa_key, overrides={"scp": ["mcp-gateway"]}))


async def test_missing_required_scope_is_insufficient_scope(
    idp: FakeIdp, rsa_key: rsa.RSAPrivateKey
) -> None:
    verifier = make_verifier(idp, required_scopes=["mcp-gateway", "extra"])
    with pytest.raises(InsufficientScopeError, match="extra"):
        await verifier.verify(mint(rsa_key, overrides={"scope": "openid mcp-gateway"}))


def test_token_scopes_ignores_wrong_types() -> None:
    assert token_scopes({"scope": 5, "scp": [1, "a", ""]}) == {"a"}
    assert token_scopes({"scope": "a b", "scp": "c"}) == {"a", "b", "c"}
