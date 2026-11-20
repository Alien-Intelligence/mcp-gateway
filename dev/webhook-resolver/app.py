"""Development webhook for the gateway's ``webhook`` resolver (a stand-in for a customer's ERP).

It answers from a mapping file in ``WEBHOOK_STATE_DIR/mapping.json``, re-read on every request
so the state can change while the stack runs::

    {"entries": [{"match": {"email": "alice@x.example"}, "api_key_file": "alice.key",
                  "upstream": "primary" | null, "labels": {"client": "x"}, "ttl_seconds": 30 | null}]}

``match`` has exactly one of subject/email/organization/group; lookup order is subject, email,
organization, group. No match is ``{"decision": "deny", "reason": "no_mapping"}``.

Authentication of the caller is configured by environment, with no defaults:

* ``WEBHOOK_HMAC_SECRET_FILE``: verify ``X-MCP-Gateway-Signature`` (constant-time compare)
  and require ``X-MCP-Gateway-Timestamp`` within ``WEBHOOK_TIMESTAMP_WINDOW_SECONDS``.
* ``WEBHOOK_TLS_CERT_FILE`` + ``WEBHOOK_TLS_KEY_FILE`` (+ ``WEBHOOK_TLS_CLIENT_CA_FILE`` to
  require and verify a client certificate, i.e. mTLS).

At least one of HMAC or a client CA must be configured: this service never runs open.
"""

import hashlib
import hmac
import json
import logging
import os
import ssl
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

logger = logging.getLogger("webhook-resolver")

MATCH_ORDER = ("subject", "email", "organization", "group")


@dataclass(frozen=True)
class Settings:
    state_dir: Path
    hmac_secret: bytes | None
    timestamp_window_seconds: float
    tls_cert_file: str | None
    tls_key_file: str | None
    tls_client_ca_file: str | None
    host: str
    port: int


def _require(env: Mapping[str, str], name: str) -> str:
    value = env.get(name)
    if not value:
        raise RuntimeError(f"required environment variable {name} is not set")
    return value


def load_settings(env: Mapping[str, str]) -> Settings:
    secret_file = env.get("WEBHOOK_HMAC_SECRET_FILE") or None
    client_ca = env.get("WEBHOOK_TLS_CLIENT_CA_FILE") or None
    if secret_file is None and client_ca is None:
        raise RuntimeError(
            "set WEBHOOK_HMAC_SECRET_FILE and/or WEBHOOK_TLS_CLIENT_CA_FILE: refusing to run open"
        )
    cert = env.get("WEBHOOK_TLS_CERT_FILE") or None
    key = env.get("WEBHOOK_TLS_KEY_FILE") or None
    if (cert is None) != (key is None):
        raise RuntimeError("WEBHOOK_TLS_CERT_FILE and WEBHOOK_TLS_KEY_FILE go together")
    if client_ca is not None and cert is None:
        raise RuntimeError("WEBHOOK_TLS_CLIENT_CA_FILE needs WEBHOOK_TLS_CERT_FILE/KEY_FILE")
    secret = Path(secret_file).read_text(encoding="utf-8").strip() if secret_file else None
    if secret_file is not None and not secret:
        raise RuntimeError("WEBHOOK_HMAC_SECRET_FILE is empty")
    return Settings(
        state_dir=Path(_require(env, "WEBHOOK_STATE_DIR")),
        hmac_secret=secret.encode() if secret else None,
        timestamp_window_seconds=float(_require(env, "WEBHOOK_TIMESTAMP_WINDOW_SECONDS")),
        tls_cert_file=cert,
        tls_key_file=key,
        tls_client_ca_file=client_ca,
        host=_require(env, "WEBHOOK_HOST"),
        port=int(_require(env, "WEBHOOK_PORT")),
    )


def signature_is_valid(settings: Settings, headers: Mapping[str, str], body: bytes) -> bool:
    if settings.hmac_secret is None:
        return True  # mTLS-only: uvicorn already verified the client certificate
    timestamp = headers.get("x-mcp-gateway-timestamp", "")
    given = headers.get("x-mcp-gateway-signature", "")
    if not timestamp.isdigit():
        return False
    if abs(time.time() - int(timestamp)) > settings.timestamp_window_seconds:
        return False
    digest = hmac.new(settings.hmac_secret, f"{timestamp}.".encode() + body, hashlib.sha256)
    return hmac.compare_digest(given, f"sha256={digest.hexdigest()}")


def _find_entry(
    entries: list[dict[str, Any]], identity: Mapping[str, Any]
) -> dict[str, Any] | None:
    wanted: dict[str, list[str]] = {
        "subject": [identity["subject"]],
        "email": [identity["email"]] if identity.get("email") else [],
        "organization": [identity["organization"]] if identity.get("organization") else [],
        "group": list(identity.get("groups", [])),
    }
    for kind in MATCH_ORDER:
        for entry in entries:
            ((entry_kind, value),) = entry["match"].items()
            if entry_kind == kind and value in wanted[kind]:
                return entry
    return None


def create_app(settings: Settings) -> Starlette:
    async def resolve(request: Request) -> Response:
        body = await request.body()
        if not signature_is_valid(settings, request.headers, body):
            logger.warning("rejected a request with a bad or stale signature")
            return JSONResponse({"error": "invalid signature"}, status_code=401)
        try:
            identity = json.loads(body)
            mapping = json.loads((settings.state_dir / "mapping.json").read_text(encoding="utf-8"))
            entry = _find_entry(mapping["entries"], identity)
        except (ValueError, KeyError, TypeError, OSError):
            logger.exception("could not evaluate the request against the mapping")
            return JSONResponse({"error": "bad request or mapping"}, status_code=400)
        if entry is None:
            return JSONResponse({"decision": "deny", "reason": "no_mapping"})
        api_key = (settings.state_dir / entry["api_key_file"]).read_text(encoding="utf-8").strip()
        return JSONResponse(
            {
                "decision": "allow",
                "api_key": api_key,
                "upstream": entry.get("upstream"),
                "labels": entry.get("labels", {}),
                "ttl_seconds": entry.get("ttl_seconds"),
            }
        )

    async def healthz(request: Request) -> Response:
        return JSONResponse({"status": "ok"})

    return Starlette(
        routes=[
            Route("/healthz", healthz, methods=["GET"]),
            Route("/resolve", resolve, methods=["POST"]),
        ]
    )


def server_config(app: Starlette, settings: Settings) -> uvicorn.Config:
    return uvicorn.Config(
        app,
        host=settings.host,
        port=settings.port,
        log_level="info",
        ssl_certfile=settings.tls_cert_file,
        ssl_keyfile=settings.tls_key_file,
        ssl_ca_certs=settings.tls_client_ca_file,
        ssl_cert_reqs=ssl.CERT_REQUIRED if settings.tls_client_ca_file else ssl.CERT_NONE,
    )


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = load_settings(os.environ)
    uvicorn.Server(server_config(create_app(settings), settings)).run()


if __name__ == "__main__":
    main()
