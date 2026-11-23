"""Minimal webhook for the gateway's ``webhook`` resolver, standard library only.

It answers the gateway's ``POST /resolve`` with the Alien API key of the signed-in user. The
lookup is the one function to replace: here it reads a JSON file, in your system it is a
database query or a call to your user directory.

What it enforces (the contract is chapter 4 of the tutorial and docs/configuration.md):

* the call is authenticated by the gateway's HMAC signature: a timestamp inside a short window,
  a nonce never seen before inside that window, and a constant-time signature comparison;
* the answer is exactly ``{"decision": "allow", ...}`` or ``{"decision": "deny", ...}``;
* a user with no entry is a ``no_mapping`` denial, never a fallback key.

Configuration comes from the environment and has no defaults:

    WEBHOOK_HMAC_SECRET_FILE            file holding the shared secret (the gateway's ``secret_file``)
    WEBHOOK_KEYS_FILE                   JSON file, see ``load_entries``
    WEBHOOK_TIMESTAMP_WINDOW_SECONDS    accepted clock difference, for example 30
    WEBHOOK_HOST, WEBHOOK_PORT          where to listen
    WEBHOOK_TLS_CERT_FILE, WEBHOOK_TLS_KEY_FILE
                                        both set to serve https, both empty for plain http. The
                                        gateway refuses a plain http URL unless the resolver sets
                                        ``allow_insecure_http``, which is for tests only: the
                                        answer carries the API key.

Run:  python webhook_resolver.py
"""

import hashlib
import hmac
import json
import logging
import os
import re
import ssl
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

logger = logging.getLogger("webhook-resolver")

# The gateway sends at most a few hundred bytes of identity; refuse anything bigger.
MAX_BODY_BYTES = 64 * 1024
# Bound on remembered nonces. Beyond it requests are refused (fail closed), never evicted.
MAX_TRACKED_NONCES = 100_000
_NONCE = re.compile(r"[0-9a-f]{16,64}")


class NonceLedger:
    """Remembers accepted nonces for as long as a replay could still pass the time window.

    A request is accepted while its timestamp is within ``window`` of now, in either direction,
    so a copy of it can still arrive until ``now + 2 * window``. Entries are appended in time
    order, so expiry only pops from the front and memory is bounded by ``capacity``.
    """

    def __init__(
        self, window_seconds: float, capacity: int, clock: Callable[[], float] = time.time
    ) -> None:
        self._retain = 2 * window_seconds
        self._capacity = capacity
        self._clock = clock
        self._expires: OrderedDict[str, float] = OrderedDict()
        self._lock = threading.Lock()

    def accept(self, nonce: str) -> bool:
        """True the first time ``nonce`` is seen, False for a replay or when the ledger is full."""
        with self._lock:
            now = self._clock()
            while self._expires and next(iter(self._expires.values())) <= now:
                self._expires.popitem(last=False)
            if nonce in self._expires or len(self._expires) >= self._capacity:
                return False
            self._expires[nonce] = now + self._retain
            return True


@dataclass(frozen=True)
class Entry:
    """What the webhook answers for one user."""

    api_key_file: Path
    upstream: str | None
    labels: Mapping[str, str]
    ttl_seconds: float | None


@dataclass(frozen=True)
class Settings:
    hmac_secret: bytes
    keys_file: Path
    timestamp_window_seconds: float
    host: str
    port: int
    tls_cert_file: str | None
    tls_key_file: str | None


def _require(env: Mapping[str, str], name: str) -> str:
    value = env.get(name)
    if not value:
        raise RuntimeError(f"required environment variable {name} is not set")
    return value


def load_settings(env: Mapping[str, str]) -> Settings:
    secret = Path(_require(env, "WEBHOOK_HMAC_SECRET_FILE")).read_text(encoding="utf-8").strip()
    if not secret:
        raise RuntimeError("WEBHOOK_HMAC_SECRET_FILE is empty")
    cert = env.get("WEBHOOK_TLS_CERT_FILE") or None
    key = env.get("WEBHOOK_TLS_KEY_FILE") or None
    if (cert is None) != (key is None):
        raise RuntimeError("WEBHOOK_TLS_CERT_FILE and WEBHOOK_TLS_KEY_FILE go together")
    return Settings(
        hmac_secret=secret.encode(),
        keys_file=Path(_require(env, "WEBHOOK_KEYS_FILE")),
        timestamp_window_seconds=float(_require(env, "WEBHOOK_TIMESTAMP_WINDOW_SECONDS")),
        host=_require(env, "WEBHOOK_HOST"),
        port=int(_require(env, "WEBHOOK_PORT")),
        tls_cert_file=cert,
        tls_key_file=key,
    )


def load_entries(keys_file: Path) -> dict[str, Entry]:
    """Read the lookup table: verified e-mail (lower case) to entry.

    ::

        {"alice@example.com": {"api_key_file": "alice.key", "upstream": null,
                               "labels": {"client": "example-publisher"}, "ttl_seconds": 300}}

    ``api_key_file`` is relative to the directory of the JSON file. ``upstream`` is a name from
    the gateway's ``upstreams``, or null for the resolver's ``default_upstream``.
    """
    raw = json.loads(keys_file.read_text(encoding="utf-8"))
    return {
        email.casefold(): Entry(
            api_key_file=keys_file.parent / item["api_key_file"],
            upstream=item["upstream"],
            labels=item["labels"],
            ttl_seconds=item["ttl_seconds"],
        )
        for email, item in raw.items()
    }


def signature_is_valid(
    settings: Settings, headers: Mapping[str, str], body: bytes, now: float
) -> bool:
    timestamp = headers.get("x-mcp-gateway-timestamp", "")
    nonce = headers.get("x-mcp-gateway-nonce", "")
    given = headers.get("x-mcp-gateway-signature", "")
    if not timestamp.isdigit() or _NONCE.fullmatch(nonce) is None:
        return False
    if abs(now - int(timestamp)) > settings.timestamp_window_seconds:
        return False
    signed = f"{timestamp}.{nonce}.".encode() + body
    digest = hmac.new(settings.hmac_secret, signed, hashlib.sha256).hexdigest()
    # Compared as bytes so a non-ASCII header value is a failed comparison, not an exception.
    return hmac.compare_digest(given.encode(), f"sha256={digest}".encode())


def _identity(body: bytes) -> dict[str, Any] | None:
    """The identity the gateway sent, or None when the body is not that shape."""
    try:
        identity = json.loads(body)
    except ValueError:
        return None
    if not isinstance(identity, dict) or not isinstance(identity.get("subject"), str):
        return None
    return identity


def decide(
    settings: Settings,
    ledger: NonceLedger,
    headers: Mapping[str, str],
    body: bytes,
    now: float,
) -> tuple[int, dict[str, Any]]:
    """The HTTP status and JSON body to answer one ``POST /resolve``.

    ``headers`` has lower-case names. A status other than 200 is never a decision: the gateway
    treats 429, 502, 503 and 504 as "try again" and every other one as a failure.
    """
    if not signature_is_valid(settings, headers, body, now):
        logger.warning("rejected a request with a bad or stale signature")
        return 401, {"error": "invalid signature"}
    if not ledger.accept(headers["x-mcp-gateway-nonce"]):
        logger.warning("rejected a replayed nonce (or the nonce ledger is full)")
        return 401, {"error": "replayed request"}
    identity = _identity(body)
    if identity is None:
        return 400, {"error": "body is not the gateway's identity document"}
    # The gateway sends an e-mail only when the identity provider marked it verified.
    email = identity.get("email")
    entry = (
        load_entries(settings.keys_file).get(email.casefold()) if isinstance(email, str) else None
    )
    if entry is None:
        return 200, {"decision": "deny", "reason": "no_mapping"}
    api_key = entry.api_key_file.read_text(encoding="utf-8").strip()
    if not api_key:
        raise RuntimeError(f"key file {entry.api_key_file.name} is empty")
    return 200, {
        "decision": "allow",
        "api_key": api_key,
        "upstream": entry.upstream,
        "labels": dict(entry.labels),
        "ttl_seconds": entry.ttl_seconds,
    }


def make_handler(settings: Settings, ledger: NonceLedger) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def _reply(self, status: int, payload: Mapping[str, Any]) -> None:
            data = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self) -> None:
            if self.path != "/resolve":
                self._reply(404, {"error": "not found"})
                return
            length = self.headers.get("Content-Length", "")
            if not length.isdigit() or int(length) > MAX_BODY_BYTES:
                self._reply(413, {"error": "body missing or too large"})
                return
            body = self.rfile.read(int(length))
            headers = {name.lower(): value for name, value in self.headers.items()}
            try:
                status, payload = decide(settings, ledger, headers, body, time.time())
            except (OSError, RuntimeError, ValueError, KeyError, TypeError):
                # A broken lookup table or key file is our fault: answer 500, which the gateway
                # does not retry and shows its user as a gateway error, and log the cause here.
                logger.exception("could not evaluate the request against the lookup table")
                status, payload = 500, {"error": "lookup failed"}
            self._reply(status, payload)

        def log_message(self, format: str, *args: Any) -> None:
            logger.info("%s " + format, self.address_string(), *args)

    return Handler


def make_server(settings: Settings, ledger: NonceLedger | None = None) -> ThreadingHTTPServer:
    nonces = ledger or NonceLedger(settings.timestamp_window_seconds, MAX_TRACKED_NONCES)
    server = ThreadingHTTPServer((settings.host, settings.port), make_handler(settings, nonces))
    if settings.tls_cert_file is not None and settings.tls_key_file is not None:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(settings.tls_cert_file, settings.tls_key_file)
        server.socket = context.wrap_socket(server.socket, server_side=True)
    return server


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    server = make_server(load_settings(os.environ))
    server.serve_forever()


if __name__ == "__main__":
    main()
