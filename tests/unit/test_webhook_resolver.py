import asyncio
import hashlib
import hmac
import json
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from mcp_gateway.config import WebhookResolverConfig
from mcp_gateway.identity import Identity
from mcp_gateway.resolver import (
    Credential,
    Denied,
    ResolverError,
    ResolverUnavailableError,
    WebhookResolver,
)
from mcp_gateway.resolver.webhook import sign

from .conftest import FAKE_API_KEY
from .webhook_server import WebhookServer, json_responder, make_pki, slow_responder

SECRET = "s3cret-shared-with-customer"
UPSTREAMS = ["primary", "other"]
ALLOW = {
    "decision": "allow",
    "api_key": FAKE_API_KEY,
    "upstream": None,
    "labels": {"client": "client-x"},
    "ttl_seconds": None,
}


def identity(email: str | None = "alice@client-x.example") -> Identity:
    return Identity(
        subject="user-1",
        email=email,
        organization="client-x",
        groups=("staff", "eu"),
        claims={"secret_claim": "must-not-be-sent", "raw_token": "eyJ.must.not"},
    )


def webhook_config(url: str, auth: dict[str, Any], **overrides: Any) -> WebhookResolverConfig:
    data: dict[str, Any] = {
        "type": "webhook",
        "url": url,
        "auth": auth,
        "ca_file": None,
        "default_upstream": "primary",
        "allow_insecure_http": True,
        "timeout_seconds": 2,
        "total_timeout_seconds": 10,
        "max_attempts": 3,
        "backoff_base_seconds": 0.01,
        "backoff_max_seconds": 0.05,
        "max_ttl_seconds": 600,
    }
    data.update(overrides)
    return WebhookResolverConfig.model_validate_json(json.dumps(data), strict=True)


@pytest.fixture
def secret_file(tmp_path: Path) -> str:
    path = tmp_path / "hmac.secret"
    path.write_text(SECRET + "\n")
    return str(path)


@pytest.fixture
async def server() -> AsyncIterator[WebhookServer]:
    srv = WebhookServer(secret=SECRET.encode())
    srv.responders = [json_responder(ALLOW)]
    await srv.start()
    yield srv
    await srv.stop()


async def make_resolver(
    server: WebhookServer, secret_file: str, **overrides: Any
) -> AsyncIterator[WebhookResolver]:
    resolver = WebhookResolver(
        webhook_config(server.url(), {"type": "hmac", "secret_file": secret_file}, **overrides),
        UPSTREAMS,
    )
    try:
        yield resolver
    finally:
        await resolver.aclose()


@pytest.fixture
async def resolver(server: WebhookServer, secret_file: str) -> AsyncIterator[WebhookResolver]:
    async for item in make_resolver(server, secret_file):
        yield item


def test_signature_is_hmac_sha256_over_timestamp_nonce_and_body() -> None:
    expected = hmac.new(b"k", b"1700000000.abc123." + b'{"a":1}', hashlib.sha256).hexdigest()
    assert sign(b"k", 1700000000, "abc123", b'{"a":1}') == f"sha256={expected}"


async def test_every_attempt_carries_a_fresh_nonce_inside_the_signature(
    resolver: WebhookResolver, server: WebhookServer
) -> None:
    await resolver.resolve(identity())
    await resolver.resolve(identity())
    nonces = [seen.headers["x-mcp-gateway-nonce"] for seen in server.seen]
    assert len(set(nonces)) == 2
    assert all(len(nonce) == 32 and int(nonce, 16) >= 0 for nonce in nonces)
    # The server verified the signature over timestamp.nonce.body.
    assert [seen.signature_valid for seen in server.seen] == [True, True]


async def test_a_replayed_request_with_another_nonce_fails_the_signature(
    resolver: WebhookResolver, server: WebhookServer
) -> None:
    await resolver.resolve(identity())
    forged = {**server.seen[0].headers, "x-mcp-gateway-nonce": "00" * 16}
    assert server.verify_headers(forged, server.seen[0].body) is False, (
        "the nonce is part of the signed string"
    )


async def test_allow_sends_only_the_identity_and_a_valid_signature(
    resolver: WebhookResolver, server: WebhookServer
) -> None:
    outcome = await resolver.resolve(identity())
    assert isinstance(outcome, Credential)
    assert outcome.api_key.get_secret_value() == FAKE_API_KEY
    assert outcome.upstream == "primary"  # null upstream selects default_upstream
    assert dict(outcome.labels) == {"client": "client-x"}
    assert outcome.ttl_seconds is None

    (seen,) = server.seen
    assert seen.signature_valid is True
    assert json.loads(seen.body) == {
        "subject": "user-1",
        "email": "alice@client-x.example",
        "organization": "client-x",
        "groups": ["staff", "eu"],
    }
    assert b"must-not-be-sent" not in seen.body and b"eyJ" not in seen.body
    assert abs(int(seen.headers["x-mcp-gateway-timestamp"]) - time.time()) < 5


async def test_null_email_is_sent_as_null(resolver: WebhookResolver, server: WebhookServer) -> None:
    await resolver.resolve(identity(email=None))
    assert json.loads(server.seen[0].body)["email"] is None


async def test_deny_is_returned_with_its_reason(
    resolver: WebhookResolver, server: WebhookServer
) -> None:
    server.responders = [json_responder({"decision": "deny", "reason": "contract_expired"})]
    assert await resolver.resolve(identity()) == Denied("contract_expired")


async def test_named_upstream_and_ttl_are_honoured_and_capped(
    secret_file: str, server: WebhookServer
) -> None:
    server.responders = [
        json_responder({**ALLOW, "upstream": "other", "ttl_seconds": 30}),
        json_responder({**ALLOW, "ttl_seconds": 99999}),
    ]
    async for resolver in make_resolver(server, secret_file, max_ttl_seconds=600):
        first = await resolver.resolve(identity())
        second = await resolver.resolve(identity())
    assert isinstance(first, Credential) and isinstance(second, Credential)
    assert (first.upstream, first.ttl_seconds) == ("other", 30)
    assert second.ttl_seconds == 600


@pytest.mark.parametrize(
    "payload",
    [
        {"decision": "maybe"},
        {"decision": "deny"},
        {"decision": "deny", "reason": "Not A Token"},
        {**ALLOW, "extra": 1},
        {**ALLOW, "upstream": "nowhere"},
        {**ALLOW, "api_key": "has space"},
        {**ALLOW, "api_key": ""},
        {**ALLOW, "ttl_seconds": 0},
        {**ALLOW, "labels": {"k": 1}},
        {"decision": "allow", "api_key": FAKE_API_KEY},
        ["allow"],
    ],
)
async def test_malformed_answers_are_resolver_errors(
    resolver: WebhookResolver, server: WebhookServer, payload: object
) -> None:
    server.responders = [json_responder(payload)]
    with pytest.raises(ResolverError) as caught:
        await resolver.resolve(identity())
    assert not isinstance(caught.value, ResolverUnavailableError)
    assert FAKE_API_KEY not in str(caught.value)
    assert len(server.seen) == 1


async def test_retries_503_with_backoff_then_succeeds(
    resolver: WebhookResolver, server: WebhookServer
) -> None:
    server.responders = [
        json_responder({"e": 1}, 503),
        json_responder({"e": 1}, 429),
        json_responder(ALLOW),
    ]
    outcome = await resolver.resolve(identity())
    assert isinstance(outcome, Credential)
    assert len(server.seen) == 3


async def test_fails_closed_after_max_attempts(
    resolver: WebhookResolver, server: WebhookServer
) -> None:
    server.responders = [json_responder({"e": 1}, 502)]
    started = time.monotonic()
    with pytest.raises(ResolverUnavailableError):
        await resolver.resolve(identity())
    assert len(server.seen) == 3
    # Two sleeps, each at least half of the (0.01, 0.02) exponential steps.
    assert time.monotonic() - started >= 0.015


async def test_timeout_is_transient_and_bounded(secret_file: str, server: WebhookServer) -> None:
    server.responders = [slow_responder(5, ALLOW)]
    async for resolver in make_resolver(server, secret_file, timeout_seconds=0.1, max_attempts=2):
        started = time.monotonic()
        with pytest.raises(ResolverUnavailableError):
            await resolver.resolve(identity())
    assert time.monotonic() - started < 2
    assert len(server.seen) == 2


async def test_total_timeout_bounds_the_whole_retry_loop(
    secret_file: str, server: WebhookServer
) -> None:
    server.responders = [slow_responder(5, ALLOW)]
    # Ten attempts of up to 0.2 s plus pauses would take over two seconds.
    async for resolver in make_resolver(
        server,
        secret_file,
        timeout_seconds=0.2,
        total_timeout_seconds=0.5,
        max_attempts=10,
        backoff_base_seconds=0.05,
        backoff_max_seconds=0.1,
    ):
        started = time.monotonic()
        with pytest.raises(ResolverUnavailableError, match="within"):
            await resolver.resolve(identity())
    assert time.monotonic() - started < 1.2
    assert 1 <= len(server.seen) < 10


@pytest.mark.parametrize("status", [400, 401, 403, 404, 500])
async def test_other_statuses_are_not_retried(
    resolver: WebhookResolver, server: WebhookServer, status: int
) -> None:
    server.responders = [json_responder({"e": 1}, status)]
    with pytest.raises(ResolverError) as caught:
        await resolver.resolve(identity())
    assert not isinstance(caught.value, ResolverUnavailableError)
    assert len(server.seen) == 1


async def test_connection_refused_is_unavailable(secret_file: str, server: WebhookServer) -> None:
    await server.stop()
    async for resolver in make_resolver(server, secret_file, max_attempts=2):
        with pytest.raises(ResolverUnavailableError):
            await resolver.resolve(identity())


async def test_oversized_answer_is_refused(
    resolver: WebhookResolver, server: WebhookServer
) -> None:
    server.responders = [json_responder({**ALLOW, "labels": {"k": "x" * 200_000}})]
    with pytest.raises(ResolverError, match="too large"):
        await resolver.resolve(identity())


async def test_a_wrong_secret_is_rejected_by_the_server(
    server: WebhookServer, tmp_path: Path
) -> None:
    wrong = tmp_path / "wrong.secret"
    wrong.write_text("not-the-secret")
    async for resolver in make_resolver(server, str(wrong)):
        with pytest.raises(ResolverError):
            await resolver.resolve(identity())
    assert server.seen[0].signature_valid is False


def test_missing_secret_file_refuses_startup_without_leaking(tmp_path: Path) -> None:
    config = webhook_config(
        "http://127.0.0.1:1/x", {"type": "hmac", "secret_file": str(tmp_path / "absent")}
    )
    with pytest.raises(ResolverError, match="unreadable"):
        WebhookResolver(config, UPSTREAMS)


def test_empty_secret_file_refuses_startup(tmp_path: Path) -> None:
    path = tmp_path / "empty"
    path.write_text("\n")
    config = webhook_config("http://127.0.0.1:1/x", {"type": "hmac", "secret_file": str(path)})
    with pytest.raises(ResolverError, match="empty"):
        WebhookResolver(config, UPSTREAMS)


def test_default_upstream_must_exist(tmp_path: Path, secret_file: str) -> None:
    config = webhook_config(
        "http://127.0.0.1:1/x",
        {"type": "hmac", "secret_file": secret_file},
        default_upstream="nowhere",
    )
    with pytest.raises(ResolverError, match="default_upstream"):
        WebhookResolver(config, UPSTREAMS)


# --- mTLS -----------------------------------------------------------------------------


async def test_mtls_with_a_private_ca(tmp_path: Path) -> None:
    pki = make_pki(tmp_path)
    server = WebhookServer()
    server.responders = [json_responder(ALLOW)]
    await server.start(
        certfile=Path(pki.server_cert), keyfile=Path(pki.server_key), client_ca=Path(pki.ca_file)
    )
    try:
        config = webhook_config(
            server.url("https"),
            {
                "type": "mtls",
                "client_cert_file": pki.client_cert,
                "client_key_file": pki.client_key,
            },
            ca_file=pki.ca_file,
        )
        resolver = WebhookResolver(config, UPSTREAMS)
        try:
            assert isinstance(await resolver.resolve(identity()), Credential)
        finally:
            await resolver.aclose()
        assert len(server.seen) == 1
        # mTLS mode sends no HMAC headers.
        assert "x-mcp-gateway-signature" not in server.seen[0].headers
    finally:
        await server.stop()


async def test_mtls_rejects_a_client_certificate_from_an_untrusted_ca(tmp_path: Path) -> None:
    trusted = make_pki(tmp_path, "trusted")
    rogue = make_pki(tmp_path, "rogue")
    server = WebhookServer()
    server.responders = [json_responder(ALLOW)]
    await server.start(
        certfile=Path(trusted.server_cert),
        keyfile=Path(trusted.server_key),
        client_ca=Path(trusted.ca_file),
    )
    try:
        config = webhook_config(
            server.url("https"),
            {
                "type": "mtls",
                "client_cert_file": rogue.client_cert,
                "client_key_file": rogue.client_key,
            },
            ca_file=trusted.ca_file,
            max_attempts=1,
        )
        resolver = WebhookResolver(config, UPSTREAMS)
        try:
            with pytest.raises((ResolverUnavailableError, ResolverError)):
                await resolver.resolve(identity())
        finally:
            await resolver.aclose()
        assert server.seen == []
    finally:
        await server.stop()


async def test_server_certificate_from_an_unknown_ca_is_a_config_error_not_retried(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    pki = make_pki(tmp_path)
    server = WebhookServer()
    server.responders = [json_responder(ALLOW)]
    await server.start(certfile=Path(pki.server_cert), keyfile=Path(pki.server_key))
    try:
        # ca_file null: only system CAs, so the private CA is unknown.
        config = webhook_config(
            server.url("https"),
            {
                "type": "mtls",
                "client_cert_file": pki.client_cert,
                "client_key_file": pki.client_key,
            },
            max_attempts=3,
        )
        resolver = WebhookResolver(config, UPSTREAMS)
        try:
            with pytest.raises(ResolverError, match="verif") as caught:
                await resolver.resolve(identity())
            assert not isinstance(caught.value, ResolverUnavailableError)
        finally:
            await resolver.aclose()
        assert not [r for r in caplog.records if "webhook attempt" in r.getMessage()]
    finally:
        await server.stop()


def test_unreadable_tls_material_refuses_startup(tmp_path: Path) -> None:
    config = webhook_config(
        "https://127.0.0.1:1/x",
        {
            "type": "mtls",
            "client_cert_file": str(tmp_path / "no.pem"),
            "client_key_file": str(tmp_path / "no.key"),
        },
    )
    with pytest.raises(ResolverError, match="TLS material"):
        WebhookResolver(config, UPSTREAMS)


async def test_concurrent_resolves_all_complete(
    resolver: WebhookResolver, server: WebhookServer
) -> None:
    outcomes = await asyncio.gather(*(resolver.resolve(identity()) for _ in range(5)))
    assert all(isinstance(o, Credential) for o in outcomes)
