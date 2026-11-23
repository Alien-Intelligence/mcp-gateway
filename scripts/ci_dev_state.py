"""Write the gateway state directory of the CI real-dev run from the keys CI provides.

``scripts/dev_fixture.py`` builds the dev state on a developer machine by calling the Alien
platform. CI does not run it: it receives the fixture's facts (``fixture.json``) and one key file
per persona as CI variables, and this script lays them out exactly as ``dev_fixture.py`` does
(``state_layout.py``), into the stack's state volume::

    python scripts/ci_dev_state.py --state-dir /run/gateway/state --secrets-dir /run/secrets

Each key file is looked up in ``--secrets-dir`` by the persona's name (``alice``, ``bob``, ...)
and written under the file name ``fixture.json`` gives it. Keys are never printed.
"""

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from state_layout import (
    KEY_FILE_MODE,
    REAL_ROUTING,
    KeyedPersona,
    ensure_private_dir,
    write_atomic,
    write_layout,
)

DESCRIPTION = "Write the real-dev gateway state directory from CI-provided keys."
FIXTURE_FILE = "fixture.json"
FIXTURE_SECRET = "fixture"
KEY_SECRET_SUFFIX = "_key"


def persona_of(client: dict[str, Any]) -> KeyedPersona:
    """The persona of a ``fixture.json`` client: named by its IdP address, labelled by client."""
    email = client["email"]
    return KeyedPersona(email.split("@", 1)[0], email, client["name"], client["key_file"])


def build(state_dir: Path, secrets_dir: Path) -> list[str]:
    """Write the state and return the persona names that were laid out."""
    fixture_text = (secrets_dir / FIXTURE_SECRET).read_text(encoding="utf-8")
    fixture = json.loads(fixture_text)
    personas = [persona_of(client) for client in fixture["clients"]]
    ensure_private_dir(state_dir)
    for persona in personas:
        key = (secrets_dir / f"{persona.name}{KEY_SECRET_SUFFIX}").read_text(encoding="utf-8")
        if not key.strip():
            raise SystemExit(f"the key of {persona.name} is empty")
        write_atomic(state_dir / persona.key_file, key.strip() + "\n", KEY_FILE_MODE)
    write_atomic(state_dir / FIXTURE_FILE, fixture_text, KEY_FILE_MODE)
    write_layout(state_dir, personas, REAL_ROUTING)
    return [persona.name for persona in personas]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=DESCRIPTION)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--secrets-dir", type=Path, required=True)
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    names = build(args.state_dir, args.secrets_dir)
    print(f"real-dev state written to {args.state_dir} ({', '.join(names)})", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
