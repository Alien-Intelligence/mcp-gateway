"""Fixtures of the stub-target suite: settings, an HTTP client, the stub control and evidence."""

import json
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from e2e.stub.stack import HTTP_TIMEOUT, StubControl, StubSettings, load_stub_settings


@pytest.fixture(scope="session")
def settings() -> StubSettings:
    return load_stub_settings()


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
def evidence_log(settings: StubSettings) -> EvidenceLog:
    return EvidenceLog(settings.run_dir / "evidence-stub.json")


@pytest.fixture
def evidence(request: pytest.FixtureRequest, evidence_log: EvidenceLog) -> Iterator[dict[str, Any]]:
    entry: dict[str, Any] = {"test": request.node.nodeid}
    evidence_log.entries.append(entry)
    try:
        yield entry
    finally:
        evidence_log.flush()


@pytest.fixture
async def http() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        yield client


@pytest.fixture
async def stub(http: httpx.AsyncClient) -> StubControl:
    """The stub's control endpoints, with the request log and sessions emptied for this test."""
    control = StubControl(http)
    await control.reset()
    return control
