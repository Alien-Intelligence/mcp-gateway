"""The process logging: what reaches stdout, and what never does."""

import logging
from collections.abc import Iterator

import httpx
import pytest

from mcp_gateway.audit import audit_logger
from mcp_gateway.logging_setup import configure_logging

HTTP_CLIENT_LOGGERS = ("httpx", "httpcore")


@pytest.fixture
def restored_logging() -> Iterator[None]:
    """``configure_logging`` rewires process-wide loggers: put every one of them back."""
    root = logging.getLogger()
    saved_handlers, saved_level = root.handlers[:], root.level
    audit_handlers = audit_logger.handlers[:]
    audit_state = (audit_logger.propagate, audit_logger.level)
    saved = {name: logging.getLogger(name).level for name in HTTP_CLIENT_LOGGERS}
    try:
        yield
    finally:
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)
        audit_logger.handlers[:] = audit_handlers
        audit_logger.propagate, audit_logger.level = audit_state
        for name, level in saved.items():
            logging.getLogger(name).setLevel(level)


async def test_http_client_request_lines_never_reach_the_log(
    restored_logging: None, capsys: pytest.CaptureFixture[str]
) -> None:
    configure_logging()
    url = "https://idp.example.com/userinfo-path-under-test"
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={}))
    async with httpx.AsyncClient(transport=transport) as client:
        await client.get(url)
    logging.getLogger("mcp_gateway.test").info("control line")
    out = capsys.readouterr().out
    assert "control line" in out
    assert "userinfo-path-under-test" not in out


def test_http_client_warnings_still_reach_the_log(
    restored_logging: None, capsys: pytest.CaptureFixture[str]
) -> None:
    configure_logging()
    logging.getLogger("httpx").warning("library warning")
    assert "library warning" in capsys.readouterr().out
