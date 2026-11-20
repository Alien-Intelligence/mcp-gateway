"""``chain`` resolver: try resolvers in order.

``no_mapping`` from one resolver moves on to the next. Any other denial and any error stop the
chain and are returned or raised as is, so the request fails closed.
"""

import logging
from collections.abc import Sequence

from mcp_gateway.closing import close_all
from mcp_gateway.identity import Identity
from mcp_gateway.resolver.base import Credential, CredentialResolver, Denied, ResolverError

logger = logging.getLogger(__name__)

NO_MAPPING = "no_mapping"


class ChainResolver:
    def __init__(self, resolvers: Sequence[CredentialResolver]) -> None:
        if not resolvers:
            raise ResolverError("a chain needs at least one resolver")
        self._resolvers = tuple(resolvers)

    async def resolve(self, identity: Identity) -> Credential | Denied:
        last: Denied = Denied(NO_MAPPING)
        for position, resolver in enumerate(self._resolvers):
            outcome = await resolver.resolve(identity)
            if isinstance(outcome, Denied) and outcome.reason == NO_MAPPING:
                logger.debug("chain member %d has no mapping", position)
                last = outcome
                continue
            return outcome
        return last

    async def invalidate(self, identity: Identity) -> None:
        failure: ResolverError | None = None
        for resolver in self._resolvers:
            try:
                await resolver.invalidate(identity)
            except ResolverError as exc:
                failure = failure or exc
        if failure is not None:
            raise failure

    async def aclose(self) -> None:
        await close_all(resolver.aclose for resolver in self._resolvers)
