"""Close several resources without letting one failure leave the others open."""

import logging
from collections.abc import Awaitable, Callable, Iterable

logger = logging.getLogger(__name__)


async def close_all(closers: Iterable[Callable[[], Awaitable[None]]]) -> None:
    """Run every closer. Each failure is logged; the first one is re-raised after all ran."""
    first: Exception | None = None
    for close in closers:
        try:
            await close()
        except Exception as exc:  # re-raised below, once everything else is closed
            logger.error("closing a resource failed (%s)", type(exc).__name__, exc_info=exc)
            first = first or exc
    if first is not None:
        raise first
