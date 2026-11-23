"""``auth.userinfo`` at the HTTP edge: the order, the answers, the cache, the telemetry."""

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind, StatusCode
from starlette.applications import Starlette

from mcp_gateway.app import create_app
from mcp_gateway.config import ConfigError
from mcp_gateway.identity import Profile
from mcp_gateway.ratelimit import ALLOWED, BucketSpec, Decision
from mcp_gateway.state import MemorySessionStore, State, StateUnavailableError
from mcp_gateway.telemetry import REQUEST_SPAN, USERINFO_SPAN
from mcp_gateway.userinfo import MemoryUserinfoStore

from .conftest import FakeIdp, FakeUpstream, base_config_dict, config_from, mint
from .test_app import RPC_BODY, bearer, call_then_disconnect, client_for
from .test_observability import attributes_of, audit_lines, metric_points, spans_named
from .userinfo_support import (
    DOCUMENT,
    FakeUserinfo,
    RecordingResolver,
    json_response,
    userinfo_dict,
    userinfo_transport,
)

LIMITS = {
    "per_subject": {"burst": 5, "per_minute": 6.0},
    "per_organization": {"burst": 1, "per_minute": 6.0},
}


class SpyLimiter:
    """Allows everything and records the buckets of every take."""

    def __init__(self, decisions: list[Decision] | None = None) -> None:
        self.takes: list[list[BucketSpec]] = []
        self._decisions = decisions or []

    async def acquire(self, buckets: Any) -> Decision:
        self.takes.append(list(buckets))
        return self._decisions.pop(0) if self._decisions else ALLOWED


class RecordingStore:
    """A memory userinfo store that records the TTLs it was given and can fail on demand."""

    def __init__(self, *, failing: tuple[str, ...] = ()) -> None:
        self.inner = MemoryUserinfoStore(100)
        self.ttls: list[float] = []
        self._failing = failing

    async def get(self, token: str) -> Profile | None:
        if "get" in self._failing:
            raise StateUnavailableError("shared state unavailable (FakeError)")
        return await self.inner.get(token)

    async def put(self, token: str, profile: Profile, ttl_seconds: float) -> None:
        self.ttls.append(ttl_seconds)
        await self.inner.put(token, profile, ttl_seconds)


def state_with_userinfo(store: Any) -> State:
    sessions = MemorySessionStore(max_entries=10, max_entries_per_subject=10, idle_ttl_seconds=600)
    return State(
        sessions=sessions, cache=None, userinfo=store, redis=None, key_prefix=None, digest=None
    )


@dataclass
class Stack:
    app: Starlette
    fake: FakeUserinfo
    resolver: RecordingResolver
    upstream: FakeUpstream
    spans: InMemorySpanExporter
    reader: InMemoryMetricReader
    key: rsa.RSAPrivateKey

    def token(self, **claims: Any) -> str:
        return mint(self.key, overrides=claims or None)

    async def post(
        self, token: str, body: Any = RPC_BODY, headers: dict[str, str] | None = None
    ) -> httpx.Response:
        async with client_for(self.app) as http:
            return await http.post("/mcp", json=body, headers={**bearer(token), **(headers or {})})


@pytest.fixture
def fake() -> FakeUserinfo:
    return FakeUserinfo()


@pytest.fixture
def stack_factory(
    idp: FakeIdp, upstream: FakeUpstream, fake: FakeUserinfo, rsa_key: rsa.RSAPrivateKey
) -> Any:
    def build(
        *,
        userinfo: dict[str, Any] | None = None,
        state: State | None = None,
        rate_limiter: Any = None,
        limits: dict[str, Any] | None = None,
        **config_overrides: Any,
    ) -> Stack:
        data = base_config_dict()
        data["auth"]["userinfo"] = userinfo_dict(**(userinfo or {}))
        data["policies"]["rate_limits"] = limits
        for dotted, value in config_overrides.items():
            node = data
            *parents, leaf = dotted.split("__")
            for part in parents:
                node = node[part]
            node[leaf] = value
        resolver = RecordingResolver()
        spans, reader = InMemorySpanExporter(), InMemoryMetricReader()
        app = create_app(
            config_from(data),
            resolver=resolver,
            http_transport=userinfo_transport(idp, upstream, fake),
            state=state,
            rate_limiter=rate_limiter,
            span_exporter=spans,
            metric_reader=reader,
        )
        return Stack(app, fake, resolver, upstream, spans, reader, rsa_key)

    return build


def error_of(response: httpx.Response) -> dict[str, Any]:
    error: dict[str, Any] = response.json()["error"]
    return error


def only_audit(caplog: pytest.LogCaptureFixture) -> dict[str, Any]:
    (line,) = audit_lines(caplog)
    return line


# --- what the identity is made of ---------------------------------------------------------------


async def test_the_request_to_the_idp_carries_the_callers_own_token(stack_factory: Any) -> None:
    stack: Stack = stack_factory()
    token = stack.token()
    assert (await stack.post(token)).status_code == 200
    (request,) = stack.fake.requests
    assert request.headers["authorization"] == f"Bearer {token}"
    assert request.headers["accept"] == "application/json"


async def test_the_identity_has_the_userinfo_values_when_the_token_carries_others(
    stack_factory: Any,
) -> None:
    stack: Stack = stack_factory()
    stack.fake.responder = lambda _r: json_response(
        {**DOCUMENT, "email": "doc@client-x.example", "org": "doc-org", "groups": ["doc-group"]}
    )
    token = stack.token(email="token@other.example", org="token-org", groups=["token-group"])
    assert (await stack.post(token)).status_code == 200
    (identity,) = stack.resolver.identities
    assert identity.subject == "user-1"
    assert identity.email == "doc@client-x.example"
    assert identity.organization == "doc-org"
    assert identity.groups == ("doc-group",)


async def test_claims_the_token_carries_and_userinfo_lacks_are_absent(stack_factory: Any) -> None:
    stack: Stack = stack_factory()
    stack.fake.responder = lambda _r: json_response({"sub": "user-1"})
    token = stack.token(email="token@other.example", org="token-org", groups=["token-group"])
    assert (await stack.post(token)).status_code == 200
    (identity,) = stack.resolver.identities
    assert (identity.email, identity.organization, identity.groups) == (None, None, ())


async def test_the_email_is_trusted_only_when_userinfo_says_it_is_verified(
    stack_factory: Any,
) -> None:
    stack: Stack = stack_factory()
    # The token claims the address is verified; only the userinfo document counts.
    stack.fake.responder = lambda _r: json_response({**DOCUMENT, "email_verified": False})
    token = stack.token(email_verified=True)
    assert (await stack.post(token)).status_code == 200
    (identity,) = stack.resolver.identities
    assert identity.email is None
    assert identity.organization == "client-x"


async def test_the_subject_is_read_from_the_token_through_the_identity_path(
    stack_factory: Any,
) -> None:
    stack: Stack = stack_factory(
        auth__identity={
            "subject": "ext.uid",
            "email": "email",
            "email_verified": "email_verified",
            "organization": "org",
            "groups": "groups",
        }
    )
    stack.fake.responder = lambda _r: json_response({**DOCUMENT, "sub": "user-1"})
    token = stack.token(ext={"uid": "uid-9"})
    assert (await stack.post(token)).status_code == 200
    (identity,) = stack.resolver.identities
    assert identity.subject == "uid-9"


# --- never called for a token that did not verify -----------------------------------------------


@pytest.mark.parametrize(
    "case", ["no-token", "garbage", "expired", "wrong-audience", "wrong-key", "missing-scope"]
)
async def test_userinfo_is_never_called_for_a_token_that_does_not_verify(
    stack_factory: Any, other_rsa_key: rsa.RSAPrivateKey, case: str
) -> None:
    extra: dict[str, Any] = (
        {"auth__required_scopes": ["mcp-gateway"]} if case == "missing-scope" else {}
    )
    stack: Stack = stack_factory(**extra)
    now = int(time.time())
    tokens = {
        "no-token": "",
        "garbage": "not-a-jwt",
        "expired": stack.token(exp=now - 600),
        "wrong-audience": stack.token(aud="https://other.example.com/mcp"),
        "wrong-key": mint(other_rsa_key),
        "missing-scope": stack.token(),
    }
    headers: dict[str, str] = {} if case == "no-token" else bearer(tokens[case])
    async with client_for(stack.app) as http:
        response = await http.post("/mcp", json=RPC_BODY, headers=headers)
    assert response.status_code in (401, 403)
    assert stack.fake.calls == 0
    assert stack.resolver.identities == []


# --- the sub check -------------------------------------------------------------------------------


async def test_a_userinfo_sub_that_is_not_the_token_sub_is_a_401_challenge(
    stack_factory: Any, caplog: pytest.LogCaptureFixture
) -> None:
    stack: Stack = stack_factory()
    stack.fake.responder = lambda _r: json_response({**DOCUMENT, "sub": "someone-else"})
    with caplog.at_level(logging.INFO, logger="mcp_gateway.audit"):
        response = await stack.post(stack.token())
    assert response.status_code == 401
    assert 'error="invalid_token"' in response.headers["www-authenticate"]
    assert (
        'error_description="userinfo does not match the token subject"'
        in (response.headers["www-authenticate"])
    )
    assert response.json()["error"] == "invalid_token"
    line = only_audit(caplog)
    assert (line["outcome"], line["reason"], line["status"]) == (
        "unauthenticated",
        "userinfo_subject_mismatch",
        401,
    )
    assert stack.resolver.identities == []
    assert stack.upstream.requests == []


async def test_a_token_without_a_sub_is_a_401_and_userinfo_is_not_called(
    stack_factory: Any, rsa_key: rsa.RSAPrivateKey
) -> None:
    stack: Stack = stack_factory(
        auth__identity={
            "subject": "ext.uid",
            "email": "email",
            "email_verified": "email_verified",
            "organization": "org",
            "groups": "groups",
        }
    )
    token = mint(rsa_key, overrides={"ext": {"uid": "uid-9"}}, drop=("sub",))
    response = await stack.post(token)
    assert response.status_code == 401
    assert (
        'error_description="token is missing a required claim"'
        in (response.headers["www-authenticate"])
    )
    assert stack.fake.calls == 0


async def test_a_userinfo_document_without_a_sub_is_a_gateway_error(
    stack_factory: Any, caplog: pytest.LogCaptureFixture
) -> None:
    stack: Stack = stack_factory()
    stack.fake.responder = lambda _r: json_response({"org": "client-x"})
    with caplog.at_level(logging.INFO, logger="mcp_gateway.audit"):
        response = await stack.post(stack.token())
    assert response.status_code == 500
    assert (error_of(response)["code"], response.json()["id"]) == (-31005, 7)
    line = only_audit(caplog)
    assert (line["outcome"], line["reason"]) == ("gateway_error", "userinfo_invalid")


# --- the IdP says no ------------------------------------------------------------------------------


@pytest.mark.parametrize("status", [401, 403])
async def test_a_401_or_403_from_the_idp_is_a_401_challenge_and_is_not_retried(
    stack_factory: Any, caplog: pytest.LogCaptureFixture, status: int
) -> None:
    stack: Stack = stack_factory(userinfo={"max_attempts": 3})
    stack.fake.responder = lambda _r: httpx.Response(status)
    with caplog.at_level(logging.INFO, logger="mcp_gateway.audit"):
        response = await stack.post(stack.token())
    assert response.status_code == 401
    assert (
        'error_description="token rejected by the identity provider"'
        in (response.headers["www-authenticate"])
    )
    assert stack.fake.calls == 1
    line = only_audit(caplog)
    assert (line["outcome"], line["reason"]) == ("unauthenticated", "userinfo_rejected")


async def test_a_claim_of_the_wrong_type_is_a_401_that_does_not_identify_a_user(
    stack_factory: Any, caplog: pytest.LogCaptureFixture
) -> None:
    stack: Stack = stack_factory()
    stack.fake.responder = lambda _r: json_response({**DOCUMENT, "org": 5})
    with caplog.at_level(logging.INFO, logger="mcp_gateway.audit"):
        response = await stack.post(stack.token())
    assert response.status_code == 401
    assert (
        'error_description="token does not identify a user"'
        in (response.headers["www-authenticate"])
    )
    line = only_audit(caplog)
    assert (line["outcome"], line["reason"]) == ("unauthenticated", "userinfo_claims")


# --- the IdP is down -----------------------------------------------------------------------------


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
async def test_a_transient_status_is_retried_then_a_503_with_retry_after(
    stack_factory: Any, caplog: pytest.LogCaptureFixture, status: int
) -> None:
    stack: Stack = stack_factory(userinfo={"max_attempts": 3})
    stack.fake.responder = lambda _r: httpx.Response(status)
    with caplog.at_level(logging.INFO, logger="mcp_gateway.audit"):
        response = await stack.post(stack.token())
    assert response.status_code == 503
    assert response.headers["retry-after"] == "5"
    assert error_of(response) == {
        "code": -31015,
        "message": "The identity provider is temporarily unavailable. Retry shortly.",
    }
    assert response.json()["id"] == 7
    assert stack.fake.calls == 3
    line = only_audit(caplog)
    assert (line["outcome"], line["status"]) == ("userinfo_unavailable", 503)
    assert stack.upstream.requests == []


async def test_a_transport_error_is_retried_then_a_503(stack_factory: Any) -> None:
    stack: Stack = stack_factory(userinfo={"max_attempts": 2})

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    stack.fake.responder = refuse
    response = await stack.post(stack.token())
    assert (response.status_code, error_of(response)["code"]) == (503, -31015)
    assert stack.fake.calls == 2


async def test_an_idp_that_never_answers_is_a_503_within_the_total_timeout(
    stack_factory: Any,
) -> None:
    stack: Stack = stack_factory(
        userinfo={"timeout_seconds": 0.05, "total_timeout_seconds": 0.3, "max_attempts": 10}
    )

    async def hang(_request: httpx.Request) -> httpx.Response:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    stack.fake.responder = hang
    started = time.monotonic()
    response = await asyncio.wait_for(stack.post(stack.token()), timeout=5)
    assert (response.status_code, error_of(response)["code"]) == (503, -31015)
    assert time.monotonic() - started < 3


async def test_a_notification_gets_the_503_without_an_id(stack_factory: Any) -> None:
    stack: Stack = stack_factory(userinfo={"max_attempts": 1})
    stack.fake.responder = lambda _r: httpx.Response(503)
    response = await stack.post(
        stack.token(), body={"jsonrpc": "2.0", "method": "notifications/initialized"}
    )
    assert response.status_code == 503
    assert response.json()["id"] is None


# --- the IdP answers something unusable -----------------------------------------------------------


def _document(content: bytes, content_type: str = "application/json") -> httpx.Response:
    return httpx.Response(200, content=content, headers={"content-type": content_type})


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(404),
        httpx.Response(204),
        _document(b'{"sub": "user-1", "padding": "' + b"x" * 70000 + b'"}'),
        _document(b'{"sub": "user-1"}', "text/html"),
        _document(b"a.b.c", "application/jwt"),
        _document(b'{"sub": "user-1", "org": "\xff"}'),
        _document(b'["user-1"]'),
        _document(b'{"sub": "user-1", "a": ' + b'{"a": ' * 200 + b"1" + b"}" * 200 + b"}"),
        _document(b"not json"),
    ],
    ids=[
        "404",
        "204",
        "too-large",
        "not-json-content-type",
        "jwt",
        "invalid-utf8",
        "array",
        "too-deep",
        "not-json",
    ],
)
async def test_an_unusable_answer_is_a_500_gateway_error_and_is_not_retried(
    stack_factory: Any, caplog: pytest.LogCaptureFixture, response: httpx.Response
) -> None:
    stack: Stack = stack_factory(userinfo={"max_attempts": 3})
    stack.fake.responder = lambda _r: response
    with caplog.at_level(logging.INFO, logger="mcp_gateway.audit"):
        answer = await stack.post(stack.token())
    assert answer.status_code == 500
    assert (error_of(answer)["code"], answer.json()["id"]) == (-31005, 7)
    assert stack.fake.calls == 1
    line = only_audit(caplog)
    assert (line["outcome"], line["reason"]) == ("gateway_error", "userinfo_invalid")
    assert stack.resolver.identities == []


async def test_a_signed_userinfo_response_is_refused_with_the_explicit_reason_in_the_log(
    stack_factory: Any, caplog: pytest.LogCaptureFixture
) -> None:
    stack: Stack = stack_factory()
    stack.fake.responder = lambda _r: _document(b"a.b.c", "application/jwt")
    with caplog.at_level(logging.ERROR, logger="mcp_gateway.app"):
        await stack.post(stack.token())
    assert (
        "signed or encrypted userinfo responses (application/jwt) are not supported" in caplog.text
    )


# --- the cache -------------------------------------------------------------------------------------


async def test_a_second_request_with_the_same_token_asks_the_idp_once(stack_factory: Any) -> None:
    stack: Stack = stack_factory()
    token = stack.token()
    assert (await stack.post(token)).status_code == 200
    assert (await stack.post(token)).status_code == 200
    assert stack.fake.calls == 1
    assert len(stack.resolver.identities) == 2


async def test_two_tokens_of_one_subject_are_two_idp_calls(stack_factory: Any) -> None:
    stack: Stack = stack_factory()
    await stack.post(stack.token(jti="one"))
    await stack.post(stack.token(jti="two"))
    assert stack.fake.calls == 2


async def test_the_cache_ttl_is_bounded_by_the_token_expiry(stack_factory: Any) -> None:
    store = RecordingStore()
    stack: Stack = stack_factory(
        state=state_with_userinfo(store), userinfo={"cache_ttl_seconds": 300}
    )
    await stack.post(stack.token(exp=int(time.time()) + 3))
    (ttl,) = store.ttls
    assert 1.5 < ttl <= 3.0


async def test_a_token_past_its_expiry_within_the_clock_skew_is_not_cached(
    stack_factory: Any,
) -> None:
    store = RecordingStore()
    stack: Stack = stack_factory(state=state_with_userinfo(store))
    token = stack.token(exp=int(time.time()) - 2)  # clock_skew_seconds is 5: still accepted
    assert (await stack.post(token)).status_code == 200
    assert (await stack.post(token)).status_code == 200
    assert store.ttls == []
    assert stack.fake.calls == 2


async def test_a_failure_is_not_cached_the_next_request_asks_again(stack_factory: Any) -> None:
    stack: Stack = stack_factory(userinfo={"max_attempts": 1})
    answers = iter([httpx.Response(503)])
    stack.fake.responder = lambda _r: next(answers, None) or json_response(DOCUMENT)
    token = stack.token()
    assert (await stack.post(token)).status_code == 503
    assert (await stack.post(token)).status_code == 200
    assert stack.fake.calls == 2


async def test_concurrent_requests_with_one_token_share_one_idp_call(stack_factory: Any) -> None:
    stack: Stack = stack_factory()
    gate = asyncio.Event()
    entered = asyncio.Event()

    async def answer(_request: httpx.Request) -> httpx.Response:
        entered.set()
        await gate.wait()
        return json_response(DOCUMENT)

    stack.fake.responder = answer
    token = stack.token()
    tasks = [asyncio.create_task(stack.post(token)) for _ in range(8)]
    await asyncio.wait_for(entered.wait(), timeout=5)
    await asyncio.sleep(0.05)
    assert stack.fake.calls == 1
    gate.set()
    responses = await asyncio.wait_for(asyncio.gather(*tasks), timeout=5)
    assert [r.status_code for r in responses] == [200] * 8
    assert stack.fake.calls == 1


# --- the order: subject bucket, userinfo, organization bucket -------------------------------------


async def test_a_subject_limited_request_never_reaches_the_idp(stack_factory: Any) -> None:
    limits = {
        "per_subject": {"burst": 1, "per_minute": 1.0},
        "per_organization": None,
    }
    stack: Stack = stack_factory(limits=limits)
    first = await stack.post(stack.token(jti="one"))
    assert first.status_code == 200
    assert stack.fake.calls == 1
    second = await stack.post(stack.token(jti="two"))  # another token: a cache miss
    assert second.headers["retry-after"]
    assert second.json()["result"]["isError"] is True
    assert stack.fake.calls == 1, "refused by the subject bucket before the identity provider"


async def test_the_organization_bucket_is_drawn_after_userinfo_with_the_userinfo_organization(
    stack_factory: Any, caplog: pytest.LogCaptureFixture
) -> None:
    stack: Stack = stack_factory(limits=LIMITS)

    def by_token(request: httpx.Request) -> httpx.Response:
        # Userinfo describes the subject the token was minted for.
        subjects = dict(tokens)
        return json_response(
            {**DOCUMENT, "sub": subjects[request.headers["authorization"].removeprefix("Bearer ")]}
        )

    tokens = {
        stack.token(sub="user-a", org="token-org-a"): "user-a",
        stack.token(sub="user-b", org="token-org-b"): "user-b",
    }
    stack.fake.responder = by_token
    first, second = tokens
    with caplog.at_level(logging.INFO, logger="mcp_gateway.audit"):
        assert (await stack.post(first)).status_code == 200
        refused = await stack.post(second)
    # The two tokens carry different organizations; userinfo gives both the same one, so the
    # second request found the organization bucket (burst 1) already empty.
    assert refused.json()["result"]["isError"] is True
    assert stack.fake.calls == 2, "userinfo was asked before the organization bucket"
    last = audit_lines(caplog)[-1]
    assert (last["outcome"], last["reason"], last["organization"]) == (
        "rate_limited",
        "organization",
        "client-x",
    )


async def test_with_userinfo_the_limiter_is_taken_twice_subject_then_organization(
    stack_factory: Any,
) -> None:
    spy = SpyLimiter()
    stack: Stack = stack_factory(limits=LIMITS, rate_limiter=spy)
    assert (await stack.post(stack.token())).status_code == 200
    subject_take, organization_take = spy.takes
    assert [b.scope for b in subject_take] == ["subject"]
    assert [(b.scope, b.key) for b in organization_take] == [("organization", "client-x")]


async def test_a_request_the_organization_bucket_refuses_has_spent_its_subject_token(
    stack_factory: Any,
) -> None:
    spy = SpyLimiter([ALLOWED, Decision(False, 4, "organization")])
    stack: Stack = stack_factory(limits=LIMITS, rate_limiter=spy)
    response = await stack.post(stack.token())
    assert response.headers["retry-after"] == "4"
    assert [[b.scope for b in take] for take in spy.takes] == [["subject"], ["organization"]]
    assert stack.resolver.identities == []


async def test_a_userinfo_without_an_organization_takes_no_second_bucket(
    stack_factory: Any,
) -> None:
    spy = SpyLimiter()
    stack: Stack = stack_factory(limits=LIMITS, rate_limiter=spy)
    stack.fake.responder = lambda _r: json_response({"sub": "user-1", "groups": ["g"]})
    assert (await stack.post(stack.token())).status_code == 200
    assert [[b.scope for b in take] for take in spy.takes] == [["subject"]]


async def test_without_userinfo_the_limiter_is_taken_once_for_both_buckets(
    idp: FakeIdp, upstream: FakeUpstream, rsa_key: rsa.RSAPrivateKey
) -> None:
    spy = SpyLimiter()
    data = base_config_dict()
    data["policies"]["rate_limits"] = LIMITS
    app = create_app(
        config_from(data),
        resolver=RecordingResolver(),
        http_transport=userinfo_transport(idp, upstream, FakeUserinfo()),
        rate_limiter=spy,
    )
    async with client_for(app) as http:
        response = await http.post("/mcp", json=RPC_BODY, headers=bearer(mint(rsa_key)))
    assert response.status_code == 200
    (take,) = spy.takes
    assert [(b.scope, b.key) for b in take] == [
        ("subject", "https://idp.example.com/realms/test\x1fuser-1"),
        ("organization", "client-x"),
    ]


# --- the client leaves, the state is down ------------------------------------------------------


async def test_a_client_that_leaves_during_userinfo_is_a_499_and_cancels_the_call(
    stack_factory: Any, caplog: pytest.LogCaptureFixture
) -> None:
    stack: Stack = stack_factory()
    cancelled = asyncio.Event()
    entered = asyncio.Event()

    async def never_answers(_request: httpx.Request) -> httpx.Response:
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise
        raise AssertionError("unreachable")

    stack.fake.responder = never_answers
    with caplog.at_level(logging.INFO, logger="mcp_gateway.audit"):
        sent, _ = await call_then_disconnect(stack.app, stack.token(), after_seconds=0.1)
    assert entered.is_set()
    await asyncio.wait_for(cancelled.wait(), timeout=2)
    assert sent[0]["status"] == 499
    line = only_audit(caplog)
    assert (line["outcome"], line["status"]) == ("client_disconnected", 499)
    assert stack.resolver.identities == []


@pytest.mark.parametrize("failing", [("get",)])
async def test_a_state_store_failure_is_a_503_state_unavailable(
    stack_factory: Any, caplog: pytest.LogCaptureFixture, failing: tuple[str, ...]
) -> None:
    stack: Stack = stack_factory(state=state_with_userinfo(RecordingStore(failing=failing)))
    with caplog.at_level(logging.INFO, logger="mcp_gateway.audit"):
        response = await stack.post(stack.token())
    assert (response.status_code, error_of(response)["code"]) == (503, -31013)
    assert response.headers["retry-after"] == "5"
    assert only_audit(caplog)["outcome"] == "state_unavailable"
    assert stack.fake.calls == 0


def test_a_state_without_a_userinfo_store_is_refused_when_userinfo_is_configured() -> None:
    data = base_config_dict()
    data["auth"]["userinfo"] = userinfo_dict()
    state = state_with_userinfo(None)
    with pytest.raises(ConfigError, match="auth.userinfo"):
        create_app(config_from(data), resolver=RecordingResolver(), state=state)


# --- spans, metrics, and what they may not carry -------------------------------------------------


async def test_a_miss_and_a_hit_leave_a_child_span_and_a_counter_without_identity(
    stack_factory: Any,
) -> None:
    stack: Stack = stack_factory()
    token = stack.token()
    await stack.post(token)
    await stack.post(token)

    first, second = spans_named(stack.spans, USERINFO_SPAN)
    assert first.kind == SpanKind.INTERNAL
    assert first.status.status_code == StatusCode.UNSET
    assert attributes_of(first) == {
        "mcp_gateway.userinfo.cache": "miss",
        "mcp_gateway.userinfo.result": "profile",
    }
    assert attributes_of(second) == {
        "mcp_gateway.userinfo.cache": "hit",
        "mcp_gateway.userinfo.result": "profile",
    }
    request_spans = spans_named(stack.spans, REQUEST_SPAN)
    assert {s.context.span_id for s in request_spans} >= {
        first.parent.span_id,
        second.parent.span_id,
    }
    points = {
        (p.attributes["cache"], p.attributes["result"]): p.value
        for p in metric_points(stack.reader, "mcp_gateway.userinfo.lookups")
    }
    assert points == {("miss", "profile"): 1, ("hit", "profile"): 1}


@pytest.mark.parametrize(
    ("responder", "result", "error"),
    [
        (lambda _r: httpx.Response(401), "rejected", False),
        (lambda _r: json_response({**DOCUMENT, "sub": "other"}), "subject_mismatch", False),
        (lambda _r: json_response({**DOCUMENT, "org": 5}), "claims_invalid", False),
        (lambda _r: httpx.Response(503), "unavailable", True),
        (lambda _r: httpx.Response(404), "invalid", True),
    ],
    ids=["rejected", "subject_mismatch", "claims_invalid", "unavailable", "invalid"],
)
async def test_every_userinfo_result_is_one_span_and_one_count(
    stack_factory: Any, responder: Any, result: str, error: bool
) -> None:
    stack: Stack = stack_factory(userinfo={"max_attempts": 1})
    stack.fake.responder = responder
    await stack.post(stack.token())
    (span,) = spans_named(stack.spans, USERINFO_SPAN)
    assert attributes_of(span)["mcp_gateway.userinfo.result"] == result
    assert (span.status.status_code == StatusCode.ERROR) is error
    (point,) = metric_points(stack.reader, "mcp_gateway.userinfo.lookups")
    assert (point.value, dict(point.attributes)) == (1, {"cache": "miss", "result": result})


async def test_state_failure_and_disconnect_have_their_own_results(stack_factory: Any) -> None:
    broken: Stack = stack_factory(state=state_with_userinfo(RecordingStore(failing=("get",))))
    await broken.post(broken.token())
    (span,) = spans_named(broken.spans, USERINFO_SPAN)
    assert attributes_of(span) == {
        "mcp_gateway.userinfo.cache": "none",
        "mcp_gateway.userinfo.result": "state_unavailable",
    }
    assert span.status.status_code == StatusCode.ERROR


async def test_a_disconnect_is_a_span_result_and_not_an_error(stack_factory: Any) -> None:
    stack: Stack = stack_factory()

    async def never_answers(_request: httpx.Request) -> httpx.Response:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    stack.fake.responder = never_answers
    await call_then_disconnect(stack.app, stack.token(), after_seconds=0.1)
    (span,) = spans_named(stack.spans, USERINFO_SPAN)
    assert attributes_of(span)["mcp_gateway.userinfo.result"] == "disconnected"
    assert span.status.status_code == StatusCode.UNSET


async def test_no_token_email_organization_or_subject_reaches_a_span_or_a_metric(
    stack_factory: Any, caplog: pytest.LogCaptureFixture
) -> None:
    stack: Stack = stack_factory(limits=LIMITS)
    secrets = {
        "email": "carol.private@client-secret.example",
        "org": "org-secret-5521",
        "sub": "subject-7f3a9c41",
    }
    stack.fake.responder = lambda _r: json_response(
        {
            "sub": secrets["sub"],
            "email": secrets["email"],
            "email_verified": True,
            "org": secrets["org"],
            "groups": ["group-secret-31"],
        }
    )
    token = stack.token(sub=secrets["sub"])
    with caplog.at_level(logging.DEBUG):
        assert (await stack.post(token)).status_code == 200
    exported = json.dumps(
        [dict(span.attributes or {}) for span in stack.spans.get_finished_spans()]
        + [
            dict(point.attributes)
            for name in ("mcp_gateway.userinfo.lookups", "mcp_gateway.requests")
            for point in metric_points(stack.reader, name)
        ]
    )
    for secret in (*secrets.values(), "group-secret-31", token, "userinfo.example.com"):
        assert secret not in exported
    audit = " ".join(r.getMessage() for r in caplog.records if r.name == "mcp_gateway.audit")
    for secret in (secrets["email"], "group-secret-31", token, "userinfo.example.com"):
        assert secret not in audit
    logs = " ".join(r.getMessage() for r in caplog.records if r.name.startswith("mcp_gateway"))
    for secret in (secrets["email"], "group-secret-31", token, "userinfo.example.com"):
        assert secret not in logs
