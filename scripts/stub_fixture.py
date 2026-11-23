"""Write the complete state directory of the stub target: fake keys, no Alien platform.

Idempotent: a key file that already exists is kept, everything else is rewritten. Run by the
``state-init`` service of the compose stub target, or by hand::

    uv run python scripts/stub_fixture.py --state-dir <dir>

The keys are random throwaway strings that only the upstream stub knows. One persona has a
revoked key (the stub answers 401) and one a key that needs payment (402). Keys are never
printed.
"""

import argparse
import json
import secrets
import sys
from collections.abc import Sequence
from pathlib import Path

from state_layout import (
    KEY_FILE_MODE,
    STUB_ROUTING,
    KeyedPersona,
    ensure_private_dir,
    write_atomic,
    write_layout,
)

DESCRIPTION = "Write the state directory of the compose stub target (fake keys)."
STUB_KEYS_FILE = "stub-keys.json"

PERSONAS = (
    KeyedPersona("alice", "alice@client-x.test", "client-x", "alice.key"),
    KeyedPersona("bob", "bob@client-y.test", "client-y", "bob.key"),
    KeyedPersona("carol", "carol@client-x.test", "client-x", "carol.key"),
    KeyedPersona("dave", "dave@client-x.test", "client-x", "dave.key"),
    KeyedPersona("erin", "erin@client-q.test", "client-q", "erin.key"),
    KeyedPersona("frank", "frank@client-q.test", "client-q", "frank.key"),
    KeyedPersona("revoked", "revoked@client-r.test", "client-r", "revoked.key"),
    KeyedPersona("unpaid", "unpaid@client-p.test", "client-p", "unpaid.key"),
)
REVOKED = {"revoked"}
PAYMENT_REQUIRED = {"unpaid"}


def ensure_key(state_dir: Path, persona: KeyedPersona) -> str:
    path = state_dir / persona.key_file
    if path.is_file():
        key = path.read_text(encoding="utf-8").strip()
        if key:
            return key
    key = f"fake-alien-key-stub_{persona.name}_{secrets.token_hex(16)}"
    write_atomic(path, key + "\n", KEY_FILE_MODE)
    return key


def build(state_dir: Path) -> None:
    ensure_private_dir(state_dir)
    stub_keys = {
        ensure_key(state_dir, p): {
            "persona": p.name,
            "revoked": p.name in REVOKED,
            "payment_required": p.name in PAYMENT_REQUIRED,
        }
        for p in PERSONAS
    }
    write_atomic(state_dir / STUB_KEYS_FILE, json.dumps(stub_keys, indent=2) + "\n", KEY_FILE_MODE)
    write_layout(state_dir, PERSONAS, STUB_ROUTING)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=DESCRIPTION)
    parser.add_argument("--state-dir", type=Path, required=True)
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    build(args.state_dir)
    print(f"stub state written to {args.state_dir} ({len(PERSONAS)} personas)", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
