"""Process entry point."""

import logging
import sys

import uvicorn

from mcp_gateway.app import create_app
from mcp_gateway.config import ConfigError, load_config
from mcp_gateway.logging_setup import configure_logging
from mcp_gateway.resolver import ResolverError

logger = logging.getLogger(__name__)


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
    uvicorn.run(
        app,
        host=config.listen.host,
        port=config.listen.port,
        log_config=None,
        access_log=False,
        server_header=False,
    )


if __name__ == "__main__":
    main()
