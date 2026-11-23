"""End-to-end fixtures: settings from the environment and the per-run evidence file."""

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from e2e.harness import DirectJobControl, Settings, load_settings


@pytest.fixture(scope="session")
def settings() -> Settings:
    return load_settings()


class EvidenceLog:
    """One JSON document per run: what each test did and saw. Never tokens or keys."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._started = datetime.now(UTC).isoformat(timespec="seconds")
        self.entries: list[dict[str, Any]] = []

    def flush(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        document = {"started": self._started, "entries": self.entries}
        self._path.write_text(json.dumps(document, indent=2, default=str) + "\n", encoding="utf-8")


@pytest.fixture(scope="session")
def evidence_log(settings: Settings) -> EvidenceLog:
    return EvidenceLog(settings.run_dir / "evidence.json")


@pytest.fixture
def evidence(request: pytest.FixtureRequest, evidence_log: EvidenceLog) -> Iterator[dict[str, Any]]:
    entry: dict[str, Any] = {"test": request.node.nodeid}
    evidence_log.entries.append(entry)
    try:
        yield entry
    finally:
        evidence_log.flush()


@pytest.fixture(scope="session")
def direct_job_control(settings: Settings) -> DirectJobControl:
    return DirectJobControl(settings)
