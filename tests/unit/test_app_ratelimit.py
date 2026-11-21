"""Rate limits at the HTTP edge: the three refusal shapes, the order in the pipeline, fail closed."""

import json
import logging
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from starlette.applications import Starlette

from mcp_gateway.app import create_app
from mcp_gateway.ratelimit import (
    ALLOWED,
    BucketSpec,
    Decision,
    RateLimitUnavailableError,
    Scope,
)
from mcp_gateway.resolver import Denied

from .conftest import FakeUpstream, base_config_dict, config_from, mint
from .test_app import RPC_BODY, FixedResolver, bearer, client_for

MESSAGE = "Rate limit reached for this account. Retry in {n} s."


class Refusing:
    """A limiter that refuses every request, with a fixed wait."""

    def __init__(self, retry_after: int = 7, scope: Scope = "subject") -> None:
        self.decision = Decision(False, retry_after, scope)
        self.seen: list[Sequence[BucketSpec]] = []

    async def acquire(self, buckets: Sequence[BucketSpec]) -> Decision:
        self.seen.append(list(buckets))
        return self.decision


class Broken:
    async def acquire(self, buckets: Sequence[BucketSpec]) -> Decision:
        raise RateLimitUnavailableError("TimeoutError")


class Counting:
    def __init__(self) -> None:
        self.calls = 0

    async def acquire(self, buckets: Sequence[BucketSpec]) -> Decision:
        self.calls += 1
        return ALLOWED


LIMITS = {
    "per_subject": {"burst": 2, "per_minute": 6.0},
    "per_organization": {"burst": 3, "per_minute": 6.0},
}


def make_app(
    mapping_dir: Path,
    transport: httpx.MockTransport,
    *,
    limits: dict[str, Any] | None = LIMITS,
    resolver: Any = None,
    rate_limiter: Any = None,
) -> Starlette:
    data = base_config_dict(str(mapping_dir / "mapping.json"))
    data["policies"]["rate_limits"] = limits
    return create_app(
        config_from(data), resolver=resolver, http_transport=transport, rate_limiter=rate_limiter
    )


def limited_app(mapping_dir: Path, transport: httpx.MockTransport) -> Starlette:
    """The configured in-memory limiter."""
    return make_app(mapping_dir, transport)


async def post(app: Any, token: str, body: Any = RPC_BODY, **kwargs: Any) -> httpx.Response:
    async with client_for(app) as http:
        return await http.post("/mcp", json=body, headers=bearer(token), **kwargs)


@pytest.fixture
def token(rsa_key: rsa.RSAPrivateKey) -> str:
    return mint(rsa_key)


async def test_tools_call_over_the_limit_is_a_tool_error_result_with_200(
    mapping_dir: Path, transport: httpx.MockTransport, upstream: FakeUpstream, token: str
) -> None:
    app = limited_app(mapping_dir, transport)
    for _ in range(2):
        assert (await post(app, token)).status_code == 200
    upstream.requests.clear()
    response = await post(app, token)
    assert response.status_code == 200
    retry = int(response.headers["retry-after"])
    assert retry >= 1
    assert response.json() == {
        "jsonrpc": "2.0",
        "id": 7,
        "result": {
            "content": [{"type": "text", "text": MESSAGE.format(n=retry)}],
            "isError": True,
            "_meta": {"alien.club/retryAfterSeconds": retry},
        },
    }
    assert upstream.requests == []


async def test_other_request_with_an_id_gets_a_jsonrpc_error_with_200(
    mapping_dir: Path, transport: httpx.MockTransport, upstream: FakeUpstream, token: str
) -> None:
    app = limited_app(mapping_dir, transport)
    listing = {"jsonrpc": "2.0", "id": "abc", "method": "tools/list"}
    for _ in range(2):
        await post(app, token, listing)
    upstream.requests.clear()
    response = await post(app, token, listing)
    retry = int(response.headers["retry-after"])
    assert response.status_code == 200
    assert response.json() == {
        "jsonrpc": "2.0",
        "id": "abc",
        "error": {
            "code": -31014,
            "message": MESSAGE.format(n=retry),
            "data": {"retry_after_seconds": retry},
        },
    }
    assert upstream.requests == []


@pytest.mark.parametrize(
    "send",
    [
        pytest.param(
            lambda http, headers: http.post(
                "/mcp",
                json={"jsonrpc": "2.0", "method": "notifications/initialized"},
                headers=headers,
            ),
            id="notification",
        ),
        pytest.param(
            lambda http, headers: http.post(
                "/mcp", json={"jsonrpc": "2.0", "id": 3, "result": {}}, headers=headers
            ),
            id="client-response",
        ),
        pytest.param(lambda http, headers: http.get("/mcp", headers=headers), id="get"),
        pytest.param(lambda http, headers: http.delete("/mcp", headers=headers), id="delete"),
        pytest.param(
            lambda http, headers: http.post("/mcp", content=b"not json", headers=headers),
            id="unparsable",
        ),
        pytest.param(
            lambda http, headers: http.post("/mcp", json=[RPC_BODY], headers=headers), id="batch"
        ),
    ],
)
async def test_a_request_nobody_can_answer_gets_429_with_retry_after(
    mapping_dir: Path,
    transport: httpx.MockTransport,
    upstream: FakeUpstream,
    token: str,
    send: Any,
) -> None:
    app = make_app(mapping_dir, transport, rate_limiter=Refusing(retry_after=9))
    async with client_for(app) as http:
        response = await send(http, bearer(token))
    assert response.status_code == 429
    assert response.headers["retry-after"] == "9"
    assert response.json() == {
        "jsonrpc": "2.0",
        "id": None,
        "error": {
            "code": -31014,
            "message": MESSAGE.format(n=9),
            "data": {"retry_after_seconds": 9},
        },
    }
    assert upstream.requests == []


async def test_a_limited_request_never_reaches_the_resolver(
    mapping_dir: Path, transport: httpx.MockTransport, upstream: FakeUpstream, token: str
) -> None:
    resolver = FixedResolver(Denied("no_mapping"))
    refusing = Refusing()
    app = make_app(mapping_dir, transport, resolver=resolver, rate_limiter=refusing)
    response = await post(app, token)
    # 200 with the rate-limit result, not the resolver's 403 denial.
    assert response.status_code == 200
    assert response.json()["result"]["isError"] is True
    assert resolver.calls == 0
    assert upstream.requests == []


async def test_the_limit_comes_after_the_header_checks(
    mapping_dir: Path, transport: httpx.MockTransport, token: str
) -> None:
    counting = Counting()
    app = make_app(mapping_dir, transport, rate_limiter=counting)
    async with client_for(app) as http:
        response = await http.post(
            "/mcp", json=RPC_BODY, headers={**bearer(token), "Mcp-Method": "tools/list"}
        )
    assert response.status_code == 400
    assert counting.calls == 0


async def test_unauthenticated_requests_do_not_spend_tokens(
    mapping_dir: Path, transport: httpx.MockTransport
) -> None:
    counting = Counting()
    app = make_app(mapping_dir, transport, rate_limiter=counting)
    async with client_for(app) as http:
        response = await http.post("/mcp", json=RPC_BODY)
    assert response.status_code == 401
    assert counting.calls == 0


async def test_the_audit_line_names_the_scope_that_limited(
    mapping_dir: Path,
    transport: httpx.MockTransport,
    token: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    app = make_app(
        mapping_dir,
        transport,
        rate_limiter=Refusing(scope="organization"),
    )
    with caplog.at_level(logging.INFO, logger="mcp_gateway.audit"):
        await post(app, token)
    (line,) = [json.loads(r.getMessage()) for r in caplog.records if r.name == "mcp_gateway.audit"]
    assert (line["outcome"], line["reason"], line["status"]) == (
        "rate_limited",
        "organization",
        200,
    )


async def test_per_subject_buckets_are_keyed_on_issuer_and_subject_and_org_on_organization(
    mapping_dir: Path, transport: httpx.MockTransport, token: str
) -> None:
    refusing = Refusing()
    app = make_app(mapping_dir, transport, rate_limiter=refusing)
    await post(app, token)
    (buckets,) = refusing.seen
    assert [(b.scope, b.key) for b in buckets] == [
        ("subject", "https://idp.example.com/realms/test\x1fuser-1"),
        ("organization", "client-x"),
    ]


async def test_a_subject_without_an_organization_is_limited_per_subject_only(
    mapping_dir: Path, transport: httpx.MockTransport, rsa_key: rsa.RSAPrivateKey
) -> None:
    refusing = Refusing()
    app = make_app(mapping_dir, transport, rate_limiter=refusing)
    await post(app, mint(rsa_key, drop=("org",)))
    (buckets,) = refusing.seen
    assert [b.scope for b in buckets] == ["subject"]


async def test_organization_bucket_is_shared_across_subjects_end_to_end(
    mapping_dir: Path, transport: httpx.MockTransport, rsa_key: rsa.RSAPrivateKey
) -> None:
    app = limited_app(mapping_dir, transport)
    tokens = [mint(rsa_key, overrides={"sub": f"user-{n}"}) for n in range(4)]
    statuses = []
    for t in tokens:  # organization burst is 3, each subject sends one request
        response = await post(app, t)
        statuses.append(response.json().get("result", {}).get("isError", False))
    assert statuses == [False, False, False, True]


async def test_rate_limits_null_means_no_limiting(
    mapping_dir: Path, transport: httpx.MockTransport, upstream: FakeUpstream, token: str
) -> None:
    app = make_app(mapping_dir, transport, limits=None)
    for _ in range(20):
        assert (await post(app, token)).status_code == 200
    assert len(upstream.requests) == 20


async def test_an_unavailable_store_answers_503_and_forwards_nothing(
    mapping_dir: Path,
    transport: httpx.MockTransport,
    upstream: FakeUpstream,
    token: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    resolver = FixedResolver(Denied("no_mapping"))
    app = make_app(
        mapping_dir,
        transport,
        resolver=resolver,
        rate_limiter=Broken(),
    )
    with caplog.at_level(logging.INFO, logger="mcp_gateway.audit"):
        response = await post(app, token)
    assert response.status_code == 503
    assert response.headers["retry-after"] == "5"
    assert response.json() == {
        "jsonrpc": "2.0",
        "id": 7,
        "error": {
            "code": -31013,
            "message": "The gateway's shared state is unavailable. Retry shortly.",
        },
    }
    assert resolver.calls == 0
    assert upstream.requests == []
    (line,) = [json.loads(r.getMessage()) for r in caplog.records if r.name == "mcp_gateway.audit"]
    assert (line["outcome"], line["status"]) == ("state_unavailable", 503)
