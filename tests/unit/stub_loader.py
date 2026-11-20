"""Import the fault-injection stub (``dev/upstream-stub/app.py``, not a package) for tests."""

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

STUB_PATH = Path(__file__).resolve().parents[2] / "dev" / "upstream-stub" / "app.py"

# Fixture keys: raw secrets never appear in what the stub returns.
STUB_KEYS = {
    "fake-alien-key-alice_stub_key": {"persona": "alice", "revoked": False, "payment_required": False},
    "fake-alien-key-bob_stub_key": {"persona": "bob", "revoked": False, "payment_required": False},
    "fake-alien-key-carol_stub_key": {"persona": "carol", "revoked": False, "payment_required": False},
    "fake-alien-key-revoked_stub_key": {"persona": "revoked", "revoked": True, "payment_required": False},
    "fake-alien-key-broke_stub_key": {"persona": "broke", "revoked": False, "payment_required": True},
}


def load_stub_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("upstream_stub", STUB_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["upstream_stub"] = module
    spec.loader.exec_module(module)
    return module


def write_keys(directory: Path) -> Path:
    path = directory / "stub-keys.json"
    path.write_text(json.dumps(STUB_KEYS))
    return path
