"""``DetachedReads``: owned upstream reads, the ceiling after the client left, and shutdown."""

import asyncio
import logging
from typing import Any

import anyio
import httpx
import pytest
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from mcp_gateway.config import ObservabilityConfig
from mcp_gateway.detached import DetachedReads, ReadHandle
from mcp_gateway.telemetry import build_telemetry

from .conftest import until
from .test_observability import metric_points

CEILING = 0.2


class Rig:
    """Reads over a real in-memory metric reader, with a body that records how it ended."""

    def __init__(self) -> None:
        self.reader = InMemoryMetricReader()
        telemetry = build_telemetry(
            ObservabilityConfig.model_validate({"otlp": None}),
            span_exporter=None,
            metric_reader=self.reader,
        )
        self.telemetry = telemetry
        self.reads = DetachedReads(telemetry)
        telemetry.observe_detached(lambda: self.reads.active)
        self.closed = False

    def counted(self) -> dict[str, int]:
        return {
            point.attributes["result"]: point.value
            for point in metric_points(self.reader, "mcp_gateway.upstream.detached")
        }

    def active(self) -> int:
        (point,) = metric_points(self.reader, "mcp_gateway.upstream.detached.active")
        return point.value

    async def start(
        self, body: Any, *, detachable: bool = True, ceiling: float = CEILING
    ) -> ReadHandle[Any]:
        """Start a read and wait until its body is running."""
        running = asyncio.Event()

        async def run(handle: ReadHandle[Any]) -> Any:
            running.set()
            try:
                return await body(handle)
            finally:
                self.closed = True

        handle = self.reads.start(
            upstream="main",
            ceiling_seconds=ceiling,
            detachable=detachable,
            detach_limit=100,
            body=run,
        )
        await running.wait()
        return handle


@pytest.fixture
def rig() -> Rig:
    return Rig()


async def test_a_read_that_ends_while_attached_is_not_counted(rig: Rig) -> None:
    async def body(handle: ReadHandle[Any]) -> str:
        return "answer"

    read = await rig.start(body)
    assert await read.task == "answer"
    await until(lambda: len(rig.reads) == 0)
    assert rig.counted() == {}
    assert rig.closed


async def test_a_detached_read_that_ends_is_counted_completed_and_logged(
    rig: Rig, caplog: pytest.LogCaptureFixture
) -> None:
    go = asyncio.Event()

    async def body(handle: ReadHandle[Any]) -> None:
        await go.wait()

    with caplog.at_level(logging.INFO, logger="mcp_gateway.detached"):
        read = await rig.start(body)
        read.leave()
        assert rig.reads.active == 1
        assert rig.active() == 1
        go.set()
        await read.task
        await until(lambda: len(rig.reads) == 0)
    assert rig.counted() == {"completed": 1}
    assert rig.active() == 0
    (line,) = [r.getMessage() for r in caplog.records if r.name == "mcp_gateway.detached"]
    assert "upstream=main" in line
    assert "result=completed" in line
    assert "duration=" in line


async def test_the_ceiling_closes_a_read_that_never_finishes_and_counts_it_a_timeout(
    rig: Rig,
) -> None:
    async def body(handle: ReadHandle[Any]) -> None:
        await asyncio.Event().wait()

    read = await rig.start(body)
    read.leave()
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(read.task, timeout=5)
    await until(lambda: len(rig.reads) == 0)
    assert rig.counted() == {"timeout": 1}
    assert rig.closed


async def test_the_ceiling_starts_when_the_client_leaves_not_before(rig: Rig) -> None:
    async def body(handle: ReadHandle[Any]) -> str:
        await anyio.sleep(CEILING * 2)
        return "done"

    read = await rig.start(body)
    # Attached for longer than the ceiling: no wall-clock bound applies to an attached read.
    await anyio.sleep(CEILING * 1.2)
    assert not read.task.done()
    assert await read.task == "done"
    assert rig.counted() == {}


@pytest.mark.parametrize(
    ("error", "result"),
    [
        (httpx.ReadError("reset"), "failed"),
        (httpx.ReadTimeout("slow"), "timeout"),
        (RuntimeError("bug"), "failed"),
    ],
)
async def test_a_detached_read_that_raises_is_counted_by_what_it_raised(
    rig: Rig, error: Exception, result: str
) -> None:
    go = asyncio.Event()

    async def body(handle: ReadHandle[Any]) -> None:
        await go.wait()
        raise error

    read = await rig.start(body)
    read.leave()
    go.set()
    with pytest.raises(type(error)):
        await read.task
    await until(lambda: len(rig.reads) == 0)
    assert rig.counted() == {result: 1}
    assert rig.closed


async def test_a_read_that_must_not_outlive_the_client_is_cancelled_and_not_counted(
    rig: Rig,
) -> None:
    async def body(handle: ReadHandle[Any]) -> None:
        await asyncio.Event().wait()

    read = await rig.start(body, detachable=False)
    read.leave()
    await read.settled()
    assert read.task.cancelled()
    assert rig.closed
    await until(lambda: len(rig.reads) == 0)
    assert rig.counted() == {}


async def test_leaving_a_read_that_already_ended_changes_nothing(rig: Rig) -> None:
    async def body(handle: ReadHandle[Any]) -> str:
        return "answer"

    read = await rig.start(body)
    await read.task
    read.leave()
    assert not read.detached
    assert rig.counted() == {}


async def test_closing_cancels_what_is_left_and_awaits_it(rig: Rig) -> None:
    async def body(handle: ReadHandle[Any]) -> None:
        await asyncio.Event().wait()

    detached = await rig.start(body)
    detached.leave()
    attached = await rig.start(body)
    assert len(rig.reads) == 2
    await rig.reads.aclose(5)
    assert len(rig.reads) == 0
    assert detached.task.cancelled()
    assert attached.task.cancelled()
    assert rig.counted() == {"failed": 1}, "only the one whose client left is a detached read"


async def test_closing_gives_up_on_a_read_that_will_not_stop_at_its_deadline(
    rig: Rig, caplog: pytest.LogCaptureFixture
) -> None:
    release = asyncio.Event()

    async def stubborn(handle: ReadHandle[Any]) -> None:
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                continue  # a read that ignores its cancellation

    read = await rig.start(stubborn)
    started = asyncio.get_running_loop().time()
    with caplog.at_level(logging.ERROR, logger="mcp_gateway.detached"):
        await rig.reads.aclose(0.1)
    assert asyncio.get_running_loop().time() - started < 2
    assert "did not stop" in caplog.text
    release.set()
    await read.task


async def test_nothing_starts_after_closing(rig: Rig) -> None:
    await rig.reads.aclose(1)

    async def body(handle: ReadHandle[Any]) -> None:
        return None

    with pytest.raises(RuntimeError, match="closed"):
        await rig.start(body)
