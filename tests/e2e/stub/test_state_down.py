"""A gateway whose shared state (Redis) is unreachable fails closed: it is alive but not ready,
and it answers 503 without forwarding anything. Real user, real token, the stub as the upstream."""

from typing import Any

import httpx
import pytest

from e2e.harness import initialize_payload, obtain_grant
from e2e.stub.stack import (
    KEYCLOAK,
    NOREDIS,
    PERSONAS,
    STATE_UNAVAILABLE,
    StubControl,
    StubSettings,
    error_code,
    post,
)

pytestmark = pytest.mark.e2e_stub

NOREDIS_ORIGIN = "http://127.0.0.1:18007"


async def test_a_gateway_that_lost_its_state_refuses_requests_and_readiness(
    settings: StubSettings,
    http: httpx.AsyncClient,
    stub: StubControl,
    evidence: dict[str, Any],
) -> None:
    alice = PERSONAS["alice"]
    # Positive control: the same user, in the same way, is served by a gateway with its state.
    served = await post(
        http, KEYCLOAK, (await obtain_grant(settings, KEYCLOAK, alice)).token, initialize_payload()
    )
    assert served.status_code == 200, served.text[:300]
    seen = len(await stub.requests())

    token = (await obtain_grant(settings, NOREDIS, alice)).token
    refused = await post(http, NOREDIS, token, initialize_payload())
    assert refused.status_code == 503
    assert error_code(refused) == STATE_UNAVAILABLE
    assert int(refused.headers["retry-after"]) > 0
    assert len(await stub.requests()) == seen, "nothing is forwarded without the state"

    assert (await http.get(f"{NOREDIS_ORIGIN}/readyz")).status_code == 503
    alive = await http.get(f"{NOREDIS_ORIGIN}/healthz")
    assert alive.status_code == 200
    evidence.update(code=STATE_UNAVAILABLE, readyz=503, healthz=alive.status_code)
