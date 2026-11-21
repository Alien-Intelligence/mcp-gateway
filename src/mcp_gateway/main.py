"""Process entry point."""

import logging
import socket
import sys
from collections.abc import Callable

import uvicorn

from mcp_gateway.app import create_app
from mcp_gateway.config import ConfigError, load_config
from mcp_gateway.logging_setup import configure_logging
from mcp_gateway.resolver import ResolverError
from mcp_gateway.shutdown import in_flight_grace_seconds, stop_budget_seconds

logger = logging.getLogger(__name__)


class GatewayServer(uvicorn.Server):
    """uvicorn, told to end the listen streams as soon as it is asked to stop.

    uvicorn waits for every open connection before it lets the application shut down, and a
    listen stream is open until somebody closes it: without this, one idle stream would hold the
    whole stop until the supervisor's grace period ran out and the process was killed before its
    shutdown (flush of the telemetry, detached reads) ran.
    """

    def __init__(self, config: uvicorn.Config, begin_drain: Callable[[], None]) -> None:
        super().__init__(config)
        self._begin_drain = begin_drain

    async def shutdown(self, sockets: list[socket.socket] | None = None) -> None:
        self._begin_drain()
        await super().shutdown(sockets)


def main() -> None:
    configure_logging()
    try:
        config = load_config()
        app = create_app(config)
    except (ConfigError, ResolverError) as exc:
        logger.error("startup refused: %s", exc)
        sys.exit(1)
    logger.info(
        "gateway starting: public_url=%s issuer=%s upstreams=%s",
        config.public_url,
        config.auth.issuer,
        ",".join(sorted(config.upstreams)),
    )
    logger.info(
        "stop sequence: in-flight requests %d s, then the application's shutdown (at most %.0f s in all)",
        in_flight_grace_seconds(config),
        stop_budget_seconds(config),
    )
    server = GatewayServer(
        uvicorn.Config(
            app,
            host=config.listen.host,
            port=config.listen.port,
            log_config=None,
            access_log=False,
            server_header=False,
            # Bounded: requests still running when it ends are cancelled, then the lifespan runs.
            timeout_graceful_shutdown=in_flight_grace_seconds(config),
        ),
        app.state.begin_drain,
    )
    server.run()


if __name__ == "__main__":
    main()
