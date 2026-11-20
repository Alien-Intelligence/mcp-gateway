"""Upstream reads that the gateway owns, and that may outlive the client that asked for them.

A read is a task that holds one upstream exchange (the wait for response headers, a body being
relayed, a body being drained). While the client is there the read feeds it. When the client
leaves, a read of a request that was already sent upstream is *detached*: it keeps going, and
discards what it reads, so the upstream finishes its work and the connection goes back to the
pool. Every other read (a listen stream, a ``DELETE``, a notification) is cancelled instead.

The reads are owned here, never by the request that started them: tracked in a set (no task is
ever left to the garbage collector), bounded in time once detached, and cancelled and awaited by
the lifespan at shutdown.
"""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

import anyio
import httpx

from mcp_gateway.telemetry import Telemetry

logger = logging.getLogger(__name__)

COMPLETED = "completed"
TIMEOUT = "timeout"
FAILED = "failed"
SHED = "shed"
SHUTDOWN = "shutdown"

# How long a cancelled read gets to close its upstream response and finish.
_SETTLE_SECONDS = 10.0


class ReadsClosedError(RuntimeError):
    """A read was started after shutdown began."""


class ReadHandle[T]:
    """One owned read: lets the request side hand it over, and the read see that it was."""

    def __init__(
        self,
        *,
        upstream: str,
        ceiling_seconds: float,
        detachable: bool,
        admit: Callable[[], bool],
    ) -> None:
        self.upstream = upstream
        self._admit = admit
        self.shed = False
        self.started = time.monotonic()
        self._ceiling_seconds = ceiling_seconds
        self._detachable = detachable
        self._detached = False
        self._scope: anyio.CancelScope | None = None
        self._task: asyncio.Task[T] | None = None

    @property
    def detached(self) -> bool:
        return self._detached

    @property
    def task(self) -> asyncio.Task[T]:
        if self._task is None:
            raise RuntimeError("the read was not started")
        return self._task

    def attach_task(self, task: asyncio.Task[T]) -> None:
        self._task = task

    def bind(self, scope: anyio.CancelScope) -> None:
        """Called by the read's own task: the scope whose deadline is the wall-clock ceiling."""
        self._scope = scope
        if self._detached:
            self._arm()

    def _arm(self) -> None:
        if self._scope is not None:
            self._scope.deadline = anyio.current_time() + self._ceiling_seconds

    def leave(self) -> None:
        """The client is gone: detach the read, or cancel it when it must not outlive the client.

        Does nothing once the read has ended.

        DETACH POINT. A client that disconnects does not cancel the upstream request it already
        sent: the read carries on, bounded by its ceiling, so the upstream finishes (and meters)
        its work and the connection returns to the pool cleanly. This is the maintainers' decision of
        2026-11-20 ("disconnect is not a cancelation"). It departs on purpose from the 2026-07-28
        MCP rule that closing the stream is the cancellation: the only cancellation the gateway
        honours is an explicit ``notifications/cancelled``, forwarded like any notification.
        """
        if self.task.done():
            return
        if not self._detachable:
            self.task.cancel()
            return
        if not self._detached:
            if not self._admit():
                # Too many reads already run on for clients that left: this one is cut instead.
                self.shed = True
                self.task.cancel()
                return
            self._detached = True
            self._arm()

    async def settled(self) -> None:
        """Wait (bounded, shielded) for a cancelled read to finish closing its response."""
        if self._detachable and not self.shed:
            return
        with anyio.CancelScope(shield=True), anyio.move_on_after(_SETTLE_SECONDS) as scope:
            await asyncio.wait({self.task})
        if scope.cancelled_caught:
            logger.error("a cancelled upstream read did not finish in time")


class DetachedReads:
    """The set of upstream reads in flight, owned by the application."""

    def __init__(self, telemetry: Telemetry) -> None:
        self._telemetry = telemetry
        self._handles: set[ReadHandle[Any]] = set()
        self._closed = False

    def __len__(self) -> int:
        return len(self._handles)

    def _detached_count(self, upstream: str) -> int:
        return sum(1 for h in self._handles if h.detached and h.upstream == upstream)

    @property
    def active(self) -> int:
        """Reads whose client left and that are still running."""
        return sum(1 for handle in self._handles if handle.detached)

    def start[T](
        self,
        *,
        upstream: str,
        ceiling_seconds: float,
        detachable: bool,
        detach_limit: int,
        body: Callable[[ReadHandle[T]], Awaitable[T]],
    ) -> ReadHandle[T]:
        """Run ``body`` as an owned task.

        ``ceiling_seconds`` is the wall-clock bound on the part of the read that happens after
        the client left (httpx's per-read timeouts still apply on top of it). At most
        ``detach_limit`` reads of one upstream run on after their client left; a read beyond that
        is cancelled instead (``shed``). ``body`` closes the upstream response itself, in every case.
        """
        if self._closed:
            raise ReadsClosedError("the upstream reads are closed")
        handle = ReadHandle[T](
            upstream=upstream,
            ceiling_seconds=ceiling_seconds,
            detachable=detachable,
            admit=lambda: self._detached_count(upstream) < detach_limit,
        )
        task = asyncio.create_task(self._run(handle, body))
        handle.attach_task(task)
        self._handles.add(handle)
        task.add_done_callback(lambda _task: self._reap(handle))
        return handle

    def _reap(self, handle: ReadHandle[Any]) -> None:
        self._handles.discard(handle)
        task = handle.task
        if not task.cancelled():
            # Retrieved: the request side got it, or _run logged it for a detached read.
            task.exception()

    async def _run[T](
        self, handle: ReadHandle[T], body: Callable[[ReadHandle[T]], Awaitable[T]]
    ) -> T:
        try:
            value = await self._supervise(handle, body)
        except asyncio.CancelledError:
            if handle.shed:
                self._finish(handle, SHED, None)
            elif handle.detached:
                self._finish(handle, SHUTDOWN, None)
            raise
        except Exception as exc:
            if handle.detached:
                timed_out = isinstance(exc, TimeoutError | httpx.TimeoutException)
                self._finish(handle, TIMEOUT if timed_out else FAILED, type(exc).__name__)
            raise
        if handle.detached:
            self._finish(handle, COMPLETED, None)
        return value

    @staticmethod
    async def _supervise[T](
        handle: ReadHandle[T], body: Callable[[ReadHandle[T]], Awaitable[T]]
    ) -> T:
        result: list[T] = []
        with anyio.CancelScope() as scope:
            handle.bind(scope)
            result.append(await body(handle))
        if scope.cancelled_caught:
            raise TimeoutError("a detached upstream read exceeded its ceiling")
        return result[0]

    def _finish(self, handle: ReadHandle[Any], result: str, reason: str | None) -> None:
        self._telemetry.record_detached(handle.upstream, result)
        logger.log(
            logging.WARNING if result == SHED else logging.INFO,
            "detached upstream read ended: upstream=%s result=%s duration=%.1fs%s",
            handle.upstream,
            result,
            time.monotonic() - handle.started,
            "" if reason is None else f" reason={reason}",
        )

    async def aclose(self, deadline_seconds: float) -> None:
        """Let the reads in progress finish for ``deadline_seconds``, then cancel what is left.

        The cancelled reads get ``deadline_seconds`` more to close their responses, so shutdown
        takes twice the deadline at most. A detached read cancelled here is counted ``shutdown``.
        """
        self._closed = True
        tasks = {handle.task for handle in self._handles}
        if not tasks:
            return
        _, unfinished = await asyncio.wait(tasks, timeout=deadline_seconds)
        for task in unfinished:
            task.cancel()
        if not unfinished:
            return
        _, pending = await asyncio.wait(unfinished, timeout=deadline_seconds)
        if pending:
            logger.error(
                "%d upstream reads did not stop within %.0f s of shutdown",
                len(pending),
                deadline_seconds,
            )
