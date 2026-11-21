"""Call a webhook resolver the way the gateway does and print what it answered.

Use it to test your own webhook before the gateway does:

    python resolve_probe.py --url https://keys.example.com/resolve \
        --secret-file ./webhook-hmac --email alice@example.com

It signs the request exactly as the gateway does (headers X-MCP-Gateway-Timestamp,
X-MCP-Gateway-Nonce and X-MCP-Gateway-Signature, a signature over "<timestamp>.<nonce>.<body>")
and prints the status and the JSON answer with the API key replaced by ``<redacted>``.
Exit status 0 for an answer the gateway accepts (an allow or a deny), 1 otherwise.
"""

import argparse
import hashlib
import hmac
import json
import secrets
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Sequence
from pathlib import Path

TIMEOUT_SECONDS = 10


def signed_headers(secret: bytes, body: bytes, now: float) -> dict[str, str]:
    timestamp = str(int(now))
    nonce = secrets.token_hex(16)
    digest = hmac.new(secret, f"{timestamp}.{nonce}.".encode() + body, hashlib.sha256)
    return {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-MCP-Gateway-Timestamp": timestamp,
        "X-MCP-Gateway-Nonce": nonce,
        "X-MCP-Gateway-Signature": f"sha256={digest.hexdigest()}",
    }


def accepted_by_the_gateway(status: int, answer: object) -> bool:
    """True for the two answers the gateway's contract allows: an allow or a deny with HTTP 200."""
    if status != 200 or not isinstance(answer, dict):
        return False
    if answer.get("decision") == "allow":
        return set(answer) == {"decision", "api_key", "upstream", "labels", "ttl_seconds"}
    if answer.get("decision") == "deny":
        return set(answer) == {"decision", "reason"}
    return False


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Call a webhook resolver like the gateway does.")
    parser.add_argument("--url", required=True)
    parser.add_argument("--secret-file", type=Path, required=True)
    parser.add_argument("--email", required=True, help="the verified e-mail to look up")
    parser.add_argument("--subject", default="probe-subject")
    parser.add_argument("--organization", default=None)
    args = parser.parse_args(argv)

    secret = args.secret_file.read_text(encoding="utf-8").strip().encode()
    identity = {
        "subject": args.subject,
        "email": args.email,
        "organization": args.organization,
        "groups": [],
    }
    body = json.dumps(identity, separators=(",", ":")).encode()
    request = urllib.request.Request(
        args.url, data=body, headers=signed_headers(secret, body, time.time()), method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            status, raw = response.status, response.read()
    except urllib.error.HTTPError as exc:
        status, raw = exc.code, exc.read()
    try:
        answer = json.loads(raw)
    except ValueError:
        print(f"HTTP {status}, body is not JSON")
        return 1
    shown = (
        {**answer, "api_key": "<redacted>"}
        if isinstance(answer, dict) and "api_key" in answer
        else answer
    )
    print(f"HTTP {status} {json.dumps(shown)}")
    return 0 if accepted_by_the_gateway(status, answer) else 1


if __name__ == "__main__":
    sys.exit(main())
