"""A real HTTP(S) server on 127.0.0.1 for webhook tests, plus a throwaway PKI."""

import asyncio
import datetime
import hashlib
import hmac
import ipaddress
import ssl
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

import uvicorn
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

Responder = Callable[[Request], Awaitable[Response]]


@dataclass
class Seen:
    body: bytes
    headers: dict[str, str]
    signature_valid: bool | None
    peer_cert_subject: str | None = None


@dataclass
class WebhookServer:
    """Records every request; answers from ``responders`` (one per request, last repeats)."""

    secret: bytes | None = None
    seen: list[Seen] = field(default_factory=list)
    responders: list[Responder] = field(default_factory=list)
    port: int = 0
    _server: uvicorn.Server | None = None
    _task: "asyncio.Task[None] | None" = None

    def verify_headers(self, headers: Mapping[str, str], body: bytes) -> bool | None:
        if self.secret is None:
            return None
        timestamp = headers.get("x-mcp-gateway-timestamp", "")
        nonce = headers.get("x-mcp-gateway-nonce", "")
        given = headers.get("x-mcp-gateway-signature", "")
        expected = (
            "sha256="
            + hmac.new(
                self.secret, f"{timestamp}.{nonce}.".encode() + body, hashlib.sha256
            ).hexdigest()
        )
        fresh = timestamp.isdigit() and abs(time.time() - int(timestamp)) < 30
        return bool(nonce) and fresh and hmac.compare_digest(given, expected)

    async def _handle(self, request: Request) -> Response:
        body = await request.body()
        valid = self.verify_headers(request.headers, body)
        self.seen.append(Seen(body, dict(request.headers), valid))
        if valid is False:
            return JSONResponse({"error": "bad signature"}, status_code=401)
        index = min(len(self.seen) - 1, len(self.responders) - 1)
        return await self.responders[index](request)

    async def start(
        self,
        *,
        certfile: Path | None = None,
        keyfile: Path | None = None,
        client_ca: Path | None = None,
    ) -> None:
        app = Starlette(routes=[Route("/resolve", self._handle, methods=["POST"])])
        config = uvicorn.Config(
            app,
            host="127.0.0.1",
            port=0,
            log_level="warning",
            ssl_certfile=str(certfile) if certfile else None,
            ssl_keyfile=str(keyfile) if keyfile else None,
            ssl_ca_certs=str(client_ca) if client_ca else None,
            ssl_cert_reqs=ssl.CERT_REQUIRED if client_ca else ssl.CERT_NONE,
        )
        self._server = uvicorn.Server(config)
        self._task = asyncio.create_task(self._server.serve())
        async with asyncio.timeout(10):
            while not self._server.started:
                await asyncio.sleep(0.01)
        self.port = self._server.servers[0].sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self._server is not None and self._task is not None:
            # A test may leave a deliberately slow request in flight: do not wait for it.
            self._server.force_exit = True
            self._server.should_exit = True
            await asyncio.wait_for(self._task, timeout=10)

    def url(self, scheme: str = "http") -> str:
        return f"{scheme}://127.0.0.1:{self.port}/resolve"


def json_responder(payload: object, status: int = 200) -> Responder:
    async def respond(request: Request) -> Response:
        return JSONResponse(payload, status_code=status)

    return respond


def slow_responder(seconds: float, payload: object) -> Responder:
    async def respond(request: Request) -> Response:
        await asyncio.sleep(seconds)
        return JSONResponse(payload)

    return respond


# --- throwaway PKI ----------------------------------------------------------------------


def _name(common_name: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])


def _write(path: Path, data: bytes) -> str:
    path.write_bytes(data)
    return str(path)


@dataclass(frozen=True)
class Pki:
    ca_file: str
    server_cert: str
    server_key: str
    client_cert: str
    client_key: str


class _Ca:
    def __init__(self, common_name: str) -> None:
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.name = _name(common_name)
        now = datetime.datetime.now(datetime.UTC)
        self.cert = (
            x509.CertificateBuilder()
            .subject_name(self.name)
            .issuer_name(self.name)
            .public_key(self.key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(minutes=5))
            .not_valid_after(now + datetime.timedelta(days=1))
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .sign(self.key, hashes.SHA256())
        )

    def issue(self, common_name: str, *, server: bool) -> tuple[bytes, bytes]:
        key = ec.generate_private_key(ec.SECP256R1())
        now = datetime.datetime.now(datetime.UTC)
        builder = (
            x509.CertificateBuilder()
            .subject_name(_name(common_name))
            .issuer_name(self.name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(minutes=5))
            .not_valid_after(now + datetime.timedelta(days=1))
            .add_extension(
                x509.ExtendedKeyUsage(
                    [ExtendedKeyUsageOID.SERVER_AUTH if server else ExtendedKeyUsageOID.CLIENT_AUTH]
                ),
                critical=False,
            )
        )
        if server:
            builder = builder.add_extension(
                x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
                critical=False,
            )
        cert = builder.sign(self.key, hashes.SHA256())
        return (
            cert.public_bytes(serialization.Encoding.PEM),
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            ),
        )

    def pem(self) -> bytes:
        return self.cert.public_bytes(serialization.Encoding.PEM)


def make_pki(directory: Path, name: str = "ca") -> Pki:
    """A CA, a 127.0.0.1 server certificate and a client certificate, all written to files."""
    ca = _Ca(f"{name}-test-ca")
    server_cert, server_key = ca.issue("localhost", server=True)
    client_cert, client_key = ca.issue("gateway", server=False)
    return Pki(
        ca_file=_write(directory / f"{name}-ca.pem", ca.pem()),
        server_cert=_write(directory / f"{name}-server.pem", server_cert),
        server_key=_write(directory / f"{name}-server.key", server_key),
        client_cert=_write(directory / f"{name}-client.pem", client_cert),
        client_key=_write(directory / f"{name}-client.key", client_key),
    )
