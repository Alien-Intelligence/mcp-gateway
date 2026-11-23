"""What stopping the gateway guarantees, and the one bound that follows from the configuration.

On SIGTERM, in this order:

1. The server stops accepting connections and the listen streams (GET) are ended cleanly: a
   client reconnects, which is how the protocol answers a server that closes a stream.
2. Exchanges that have a reply to deliver (a ``tools/call`` and any other request) get
   ``in_flight_grace_seconds`` to finish: the longest ``read_timeout_seconds`` of the upstreams,
   the time the gateway is willing to wait on one of them anyway. What is still running then is
   cancelled; a request that was sent upstream carries on as a detached read.
3. The application's own shutdown: detached reads get ``DETACHED_READS_SHUTDOWN_SECONDS`` to
   finish and as long again to close once cancelled, then the resolver, the shared state, the HTTP
   clients are closed and the telemetry is flushed (bounded by the exporter timeout).

``stop_budget_seconds`` adds the three up. A supervisor (Kubernetes' grace period, Compose's
``stop_grace_period``) must allow at least that, or it kills the process before step 3 ran.
"""

import math

from mcp_gateway.config import GatewayConfig
from mcp_gateway.telemetry import SHUTDOWN_SLACK_SECONDS

# How long the lifespan waits for upstream reads to finish, and again for those it cancelled to
# close their responses.
DETACHED_READS_SHUTDOWN_SECONDS = 10.0

# uvicorn pauses this long after asking the connections to close.
_SERVER_SETTLE_SECONDS = 0.1

# Providers that flush on shutdown, each with its own bound (traces, then metrics).
_TELEMETRY_PROVIDERS = 2


def in_flight_grace_seconds(config: GatewayConfig) -> int:
    """How long requests in flight may keep running after the stop began (step 2)."""
    return math.ceil(max(upstream.read_timeout_seconds for upstream in config.upstreams.values()))


def application_shutdown_seconds(config: GatewayConfig) -> float:
    """The longest the application's own shutdown takes (step 3)."""
    telemetry = 0.0
    otlp = config.observability.otlp
    if otlp is not None:
        telemetry = _TELEMETRY_PROVIDERS * (otlp.timeout_seconds + SHUTDOWN_SLACK_SECONDS)
    return 2 * DETACHED_READS_SHUTDOWN_SECONDS + telemetry


def stop_budget_seconds(config: GatewayConfig) -> float:
    """The longest the process takes to stop by itself after SIGTERM."""
    return (
        in_flight_grace_seconds(config)
        + _SERVER_SETTLE_SECONDS
        + application_shutdown_seconds(config)
    )
