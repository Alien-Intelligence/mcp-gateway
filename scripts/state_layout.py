"""The gateway state directory, as the dev and stub stacks lay it out.

Both fixtures (``dev_fixture.py`` with real Alien keys, ``stub_fixture.py`` with fake ones)
write the same files, so one stack definition serves both targets::

    <persona>.key            one API key per persona (written by the fixture, mode 600)
    mapping.json             the static resolver's mapping (gateway-keycloak, gateway-hydra)
    stores.json              what ``seed_stores.py`` loads into Redis and PostgreSQL
    webhook/mapping.json     the dev webhook service's mapping, with its own copies of the keys

Connection secrets (database password, webhook HMAC secret) are not part of the state: the
seeding step writes them from the stack's environment.
"""

import json
import os
import shutil
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

STATEFUL = "stateful"
STATELESS = "stateless"
WEBHOOK_DIR = "webhook"
STORES_FILE = "stores.json"
MAPPING_FILE = "mapping.json"
KEY_FILE_MODE = 0o600

# Which resolver of gateway-chain holds each persona: alice in Redis, bob in PostgreSQL, carol
# behind the webhook. Every other persona is known to the static gateways only.
STORE_OF = {"alice": "redis", "bob": "postgres", "carol": "webhook"}
# How the static mapping recognises a persona (bob by organization keeps both kinds exercised).
STATIC_MATCH = {"bob": "organization"}


@dataclass(frozen=True)
class KeyedPersona:
    """A persona that has an API key; ``org`` is the organization claim its IdP user carries."""

    name: str
    email: str
    org: str
    key_file: str

    @property
    def labels(self) -> dict[str, str]:
        return {"client": self.org}


@dataclass(frozen=True)
class Routing:
    """Upstream name per persona: for the static mapping and for the stores (None = default)."""

    static: Mapping[str, str]
    store: Mapping[str, str | None]


# Stub target: stateful and stateless stub endpoints, spread over the personas.
STUB_ROUTING = Routing(
    static={
        "alice": STATEFUL,
        "bob": STATEFUL,
        "carol": STATELESS,
        "revoked": STATEFUL,
        "unpaid": STATEFUL,
    },
    store={"alice": STATELESS, "bob": STATEFUL, "carol": None},
)
# Dev target: the real dev MCP is stateless, and both upstream names point at it.
REAL_ROUTING = Routing(
    static=dict.fromkeys(STUB_ROUTING.static, STATELESS),
    store={"alice": STATELESS, "bob": STATELESS, "carol": None},
)


def ensure_private_dir(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.chmod(0o700)


def write_atomic(path: Path, content: str, mode: int) -> None:
    """Write ``content`` to ``path`` via a temp file in the same directory and ``os.replace``."""
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise


def _static_entry(persona: KeyedPersona, routing: Routing) -> dict[str, Any]:
    kind = STATIC_MATCH.get(persona.name, "email")
    value = persona.org if kind == "organization" else persona.email
    return {
        "match": {kind: value},
        "api_key_file": persona.key_file,
        "upstream": routing.static[persona.name],
        "labels": persona.labels,
    }


def _store_rows(personas: Sequence[KeyedPersona], routing: Routing, store: str) -> list[Any]:
    return [
        {
            "email": p.email,
            "key_file": p.key_file,
            "upstream": routing.store[p.name],
            "labels": p.labels,
        }
        for p in personas
        if STORE_OF.get(p.name) == store
    ]


def write_layout(state_dir: Path, personas: Sequence[KeyedPersona], routing: Routing) -> None:
    """Write mapping.json, stores.json and the webhook directory. Key files must exist."""
    missing = [p.key_file for p in personas if not (state_dir / p.key_file).is_file()]
    if missing:
        raise FileNotFoundError(f"key files missing from {state_dir}: {', '.join(missing)}")
    entries = [_static_entry(p, routing) for p in personas]
    write_atomic(
        state_dir / MAPPING_FILE, json.dumps({"entries": entries}, indent=2) + "\n", KEY_FILE_MODE
    )
    stores = {s: _store_rows(personas, routing, s) for s in ("redis", "postgres")}
    write_atomic(state_dir / STORES_FILE, json.dumps(stores, indent=2) + "\n", KEY_FILE_MODE)

    webhook_dir = state_dir / WEBHOOK_DIR
    ensure_private_dir(webhook_dir)
    behind_webhook = [p for p in personas if STORE_OF.get(p.name) == "webhook"]
    hook_entries: list[dict[str, Any]] = []
    for p in behind_webhook:
        shutil.copyfile(state_dir / p.key_file, webhook_dir / p.key_file)
        (webhook_dir / p.key_file).chmod(KEY_FILE_MODE)
        hook_entries.append(
            {
                "match": {"email": p.email},
                "api_key_file": p.key_file,
                "upstream": routing.store[p.name],
                "labels": p.labels,
                "ttl_seconds": None,
            }
        )
    write_atomic(
        webhook_dir / MAPPING_FILE,
        json.dumps({"entries": hook_entries}, indent=2) + "\n",
        KEY_FILE_MODE,
    )
