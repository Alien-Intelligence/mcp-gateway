"""The dev webhook service driven by the real WebhookResolver over real sockets."""

import asyncio
import importlib.util
import json
import time
from collections.abc import AsyncIterator
from pathlib import Path
from types import ModuleType

import httpx
import pytest
import uvicorn

from mcp_gateway.resolver import Credential, Denied, ResolverError, WebhookResolver
from mcp_gateway.resolver.webhook import sign

from .conftest import FAKE_API_KEY
from .test_webhook_resolver import SECRET, UPSTREAMS, identity, webhook_config
from .webhook_server import make_pki

APP_PATH = Path(__file__).parents[2] / "dev" / "webhook-resolver" / "app.py"


def load_service() -> ModuleType:
    spec = importlib.util.spec_from_file_location("dev_webhook_resolver", APP_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


service = load_service()


@pytest.fixture
def state_dir(tmp_path: Path) -> Path:
    state = tmp_path / "state"
    state.mkdir()
    (state / "alice.key").write_text(FAKE_API_KEY + "\n")
    (state / "mapping.json").write_text(
        json.dumps(
            {
                "entries": [
                    {
                        "match": {"email": "alice@client-x.example"},
                        "api_key_file": "alice.key",
                        "upstream": "other",
                        "labels": {"client": "x"},
                        "ttl_seconds": 30,
                    }
                ]
            }
        )
    )
    secret = tmp_path / "hmac"
    secret.write_text(SECRET)
    return state


def env_for(state: Path, **extra: str) -> dict[str, str]:
    return {
        "WEBHOOK_STATE_DIR": str(state),
        "WEBHOOK_TIMESTAMP_WINDOW_SECONDS": "30",
        "WEBHOOK_HOST": "127.0.0.1",
        "WEBHOOK_PORT": "0",
        **extra,
    }


async def serve(config: uvicorn.Config) -> tuple[uvicorn.Server, "asyncio.Task[None]", int]:
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    async with asyncio.timeout(10):
        while not server.started:
            await asyncio.sleep(0.01)
    return server, task, server.servers[0].sockets[0].getsockname()[1]


@pytest.fixture
async def hmac_port(state_dir: Path) -> AsyncIterator[int]:
    settings = service.load_settings(
        env_for(state_dir, WEBHOOK_HMAC_SECRET_FILE=str(state_dir.parent / "hmac"))
    )
    server, task, port = await serve(service.server_config(service.create_app(settings), settings))
    yield port
    server.should_exit = True
    await task


async def test_allow_and_no_mapping_over_hmac(hmac_port: int, tmp_path: Path) -> None:
    secret = tmp_path / "hmac-client"
    secret.write_text(SECRET)
    config = webhook_config(
        f"http://127.0.0.1:{hmac_port}/resolve", {"type": "hmac", "secret_file": str(secret)}
    )
    resolver = WebhookResolver(config, UPSTREAMS)
    try:
        outcome = await resolver.resolve(identity())
        assert isinstance(outcome, Credential)
        assert (outcome.upstream, outcome.ttl_seconds) == ("other", 30)
        assert outcome.api_key.get_secret_value() == FAKE_API_KEY
        assert await resolver.resolve(identity(email="nobody@x.example")) == Denied("no_mapping")
    finally:
        await resolver.aclose()


async def test_stats_count_signed_and_rejected_resolve_calls(
    hmac_port: int, tmp_path: Path
) -> None:
    async def stats() -> dict[str, int]:
        async with httpx.AsyncClient(timeout=5) as client:
            return (await client.get(f"http://127.0.0.1:{hmac_port}/_stats")).json()

    assert await stats() == {"resolve_requests": 0, "rejected_requests": 0}
    secrets = {"good": SECRET, "bad": "not-the-secret"}
    for name, secret_value in secrets.items():
        secret = tmp_path / name
        secret.write_text(secret_value)
        config = webhook_config(
            f"http://127.0.0.1:{hmac_port}/resolve", {"type": "hmac", "secret_file": str(secret)}
        )
        resolver = WebhookResolver(config, UPSTREAMS)
        try:
            if name == "good":
                await resolver.resolve(identity())
            else:
                with pytest.raises(ResolverError):
                    await resolver.resolve(identity())
        finally:
            await resolver.aclose()
    assert await stats() == {"resolve_requests": 1, "rejected_requests": 1}


async def test_a_wrong_secret_is_refused(hmac_port: int, tmp_path: Path) -> None:
    secret = tmp_path / "wrong"
    secret.write_text("not-the-secret")
    config = webhook_config(
        f"http://127.0.0.1:{hmac_port}/resolve", {"type": "hmac", "secret_file": str(secret)}
    )
    resolver = WebhookResolver(config, UPSTREAMS)
    try:
        with pytest.raises(ResolverError, match="401"):
            await resolver.resolve(identity())
    finally:
        await resolver.aclose()


async def test_mtls_mode(state_dir: Path, tmp_path: Path) -> None:
    pki = make_pki(tmp_path)
    settings = service.load_settings(
        env_for(
            state_dir,
            WEBHOOK_TLS_CERT_FILE=pki.server_cert,
            WEBHOOK_TLS_KEY_FILE=pki.server_key,
            WEBHOOK_TLS_CLIENT_CA_FILE=pki.ca_file,
        )
    )
    server, task, port = await serve(service.server_config(service.create_app(settings), settings))
    config = webhook_config(
        f"https://127.0.0.1:{port}/resolve",
        {"type": "mtls", "client_cert_file": pki.client_cert, "client_key_file": pki.client_key},
        ca_file=pki.ca_file,
    )
    resolver = WebhookResolver(config, UPSTREAMS)
    try:
        assert isinstance(await resolver.resolve(identity()), Credential)
    finally:
        await resolver.aclose()
        server.should_exit = True
        await task


def test_it_refuses_to_run_without_authentication(state_dir: Path) -> None:
    with pytest.raises(RuntimeError, match="refusing to run open"):
        service.load_settings(env_for(state_dir))


def test_secrets_and_settings_have_no_defaults(state_dir: Path) -> None:
    env = env_for(state_dir, WEBHOOK_HMAC_SECRET_FILE=str(state_dir.parent / "hmac"))
    for name in ("WEBHOOK_STATE_DIR", "WEBHOOK_TIMESTAMP_WINDOW_SECONDS", "WEBHOOK_PORT"):
        broken = {k: v for k, v in env.items() if k != name}
        with pytest.raises(RuntimeError, match=name):
            service.load_settings(broken)


# --- replay protection -------------------------------------------------------------------


def signed_headers(timestamp: int, nonce: str, body: bytes) -> dict[str, str]:
    return {
        "X-MCP-Gateway-Timestamp": str(timestamp),
        "X-MCP-Gateway-Nonce": nonce,
        "X-MCP-Gateway-Signature": sign(SECRET.encode(), timestamp, nonce, body),
    }


@pytest.fixture
async def replay_client(state_dir: Path) -> AsyncIterator[httpx.AsyncClient]:
    settings = service.load_settings(
        env_for(state_dir, WEBHOOK_HMAC_SECRET_FILE=str(state_dir.parent / "hmac"))
    )
    app = service.create_app(settings)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://webhook"
    ) as client:
        yield client


BODY = json.dumps(
    {"subject": "u", "email": "alice@client-x.example", "organization": None}
).encode()
NONCE_A = "a" * 32
NONCE_B = "b" * 32


async def test_a_replayed_request_is_refused(replay_client: httpx.AsyncClient) -> None:
    headers = signed_headers(int(time.time()), NONCE_A, BODY)
    assert (await replay_client.post("/resolve", content=BODY, headers=headers)).status_code == 200
    again = await replay_client.post("/resolve", content=BODY, headers=headers)
    assert again.status_code == 401
    other = signed_headers(int(time.time()), NONCE_B, BODY)
    assert (await replay_client.post("/resolve", content=BODY, headers=other)).status_code == 200


async def test_a_request_without_a_usable_nonce_is_refused(
    replay_client: httpx.AsyncClient,
) -> None:
    now = int(time.time())
    for nonce in ("", "not hex!", "ab"):
        headers = signed_headers(now, nonce, BODY)
        response = await replay_client.post("/resolve", content=BODY, headers=headers)
        assert response.status_code == 401, nonce


async def test_a_non_ascii_signature_is_401_not_a_server_error(
    replay_client: httpx.AsyncClient,
) -> None:
    headers = signed_headers(int(time.time()), NONCE_A, BODY)
    raw = [(k.lower().encode(), v.encode()) for k, v in headers.items()]
    raw = [(k, "sha256=é".encode("latin-1") if k.endswith(b"signature") else v) for k, v in raw]
    response = await replay_client.post("/resolve", content=BODY, headers=raw)
    assert response.status_code == 401


def test_the_nonce_ledger_forgets_after_the_replay_window_and_is_bounded() -> None:
    now = [1000.0]
    ledger = service.NonceLedger(30, capacity=2, clock=lambda: now[0])
    assert ledger.accept("n1")
    assert not ledger.accept("n1")
    assert ledger.accept("n2")
    assert not ledger.accept("n3"), "a full ledger refuses instead of evicting"
    now[0] += 61  # past 2 * window: n1 and n2 could no longer pass the timestamp check
    assert ledger.accept("n3")
    assert ledger.accept("n1")
