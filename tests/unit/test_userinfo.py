"""The userinfo client, the cache stores and the single-flight service."""

import asyncio
import base64
import json
import logging
import math
import re
import time
from collections.abc import Coroutine
from typing import Any, cast

import httpx
import pytest
from pydantic import SecretStr

from mcp_gateway.config import UserinfoConfig
from mcp_gateway.identity import Profile
from mcp_gateway.resolver.base import Credential
from mcp_gateway.state import BoundedRedis, StateUnavailableError
from mcp_gateway.state.digest import KeyDigest
from mcp_gateway.state.redis_cache import RedisCacheStore
from mcp_gateway.state.redis_userinfo import RedisUserinfoStore
from mcp_gateway.state.sealing import Sealer
from mcp_gateway.userinfo import (
    MemoryUserinfoStore,
    UserinfoClaimsError,
    UserinfoClient,
    UserinfoRejectedError,
    UserinfoResponseError,
    UserinfoService,
    UserinfoSubjectMismatchError,
    UserinfoUnavailableError,
    token_digest,
)

from .conftest import TrackedStream
from .userinfo_support import (
    DOCUMENT,
    PATHS,
    USERINFO_URL,
    FakeUserinfo,
    json_response,
    userinfo_config,
)

TOKEN = "header.payload.signature-token-value"
SUBJECT = "user-1"
PROFILE = Profile(email="alice@client-x.example", organization="client-x", groups=("staff",))


def client_with(fake: FakeUserinfo, **overrides: Any) -> UserinfoClient:
    http = httpx.AsyncClient(transport=httpx.MockTransport(fake.handle))
    return UserinfoClient(userinfo_config(**overrides), PATHS, http)


def respond(fake: FakeUserinfo, response: httpx.Response) -> None:
    fake.responder = lambda _request: response


# --- the request -----------------------------------------------------------------------------


async def test_the_request_is_a_get_with_the_same_bearer_token_and_a_json_accept() -> None:
    fake = FakeUserinfo()
    assert await client_with(fake).fetch(TOKEN, SUBJECT) == PROFILE
    (request,) = fake.requests
    assert request.method == "GET"
    assert str(request.url) == USERINFO_URL
    assert request.headers["authorization"] == f"Bearer {TOKEN}"
    assert request.headers["accept"] == "application/json"


async def test_a_redirect_is_not_followed_and_is_an_unusable_answer() -> None:
    fake = FakeUserinfo()
    respond(fake, httpx.Response(302, headers={"location": "https://elsewhere.example.com/"}))
    with pytest.raises(UserinfoResponseError, match="status 302"):
        await client_with(fake).fetch(TOKEN, SUBJECT)
    assert fake.calls == 1


# --- the answer is a verdict about the token ---------------------------------------------------


@pytest.mark.parametrize("status", [401, 403])
async def test_a_401_or_403_from_the_idp_is_a_rejection_and_is_not_retried(status: int) -> None:
    fake = FakeUserinfo()
    respond(fake, httpx.Response(status))
    with pytest.raises(UserinfoRejectedError):
        await client_with(fake).fetch(TOKEN, SUBJECT)
    assert fake.calls == 1


@pytest.mark.parametrize("status", [404, 400, 418, 204, 201])
async def test_any_other_status_is_an_unusable_answer_and_is_not_retried(status: int) -> None:
    fake = FakeUserinfo()
    respond(fake, httpx.Response(status))
    with pytest.raises(UserinfoResponseError, match=f"status {status}"):
        await client_with(fake).fetch(TOKEN, SUBJECT)
    assert fake.calls == 1


# --- transient failures: retried, then unavailable ---------------------------------------------


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
async def test_a_transient_status_is_retried_up_to_max_attempts(status: int) -> None:
    fake = FakeUserinfo()
    respond(fake, httpx.Response(status))
    with pytest.raises(UserinfoUnavailableError, match="after 3 attempts"):
        await client_with(fake, max_attempts=3).fetch(TOKEN, SUBJECT)
    assert fake.calls == 3


async def test_a_transport_error_is_retried_up_to_max_attempts() -> None:
    fake = FakeUserinfo()

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    fake.responder = refuse
    with pytest.raises(UserinfoUnavailableError, match="ConnectError"):
        await client_with(fake, max_attempts=4).fetch(TOKEN, SUBJECT)
    assert fake.calls == 4


async def test_a_transport_timeout_is_retried() -> None:
    fake = FakeUserinfo()

    def time_out(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    fake.responder = time_out
    with pytest.raises(UserinfoUnavailableError, match="timeout"):
        await client_with(fake, max_attempts=2).fetch(TOKEN, SUBJECT)
    assert fake.calls == 2


async def test_a_body_that_breaks_midway_is_retried() -> None:
    fake = FakeUserinfo()

    def broken(_request: httpx.Request) -> httpx.Response:
        stream = TrackedStream([b'{"sub": '], fail_after=httpx.ReadError("broken"))
        return httpx.Response(200, headers={"content-type": "application/json"}, stream=stream)

    fake.responder = broken
    with pytest.raises(UserinfoUnavailableError, match="ReadError"):
        await client_with(fake, max_attempts=2).fetch(TOKEN, SUBJECT)
    assert fake.calls == 2


async def test_an_attempt_that_never_answers_is_cut_at_timeout_seconds_then_retried() -> None:
    fake = FakeUserinfo()

    async def hang(_request: httpx.Request) -> httpx.Response:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    fake.responder = hang
    started = time.monotonic()
    with pytest.raises(UserinfoUnavailableError, match="timeout"):
        await client_with(
            fake, timeout_seconds=0.05, max_attempts=2, total_timeout_seconds=5
        ).fetch(TOKEN, SUBJECT)
    assert fake.calls == 2
    assert time.monotonic() - started < 2


async def test_the_total_timeout_bounds_every_attempt_and_every_pause() -> None:
    fake = FakeUserinfo()

    async def hang(_request: httpx.Request) -> httpx.Response:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    fake.responder = hang
    started = time.monotonic()
    with pytest.raises(UserinfoUnavailableError, match="no answer within 0.3s"):
        await client_with(
            fake,
            timeout_seconds=0.1,
            total_timeout_seconds=0.3,
            max_attempts=10,
            backoff_base_seconds=0.05,
            backoff_max_seconds=0.1,
        ).fetch(TOKEN, SUBJECT)
    assert time.monotonic() - started < 2
    assert fake.calls < 10


async def test_a_transient_failure_then_a_good_answer_succeeds() -> None:
    fake = FakeUserinfo()
    answers = iter([httpx.Response(503), httpx.Response(429), json_response(DOCUMENT)])
    fake.responder = lambda _request: next(answers)
    assert await client_with(fake, max_attempts=3).fetch(TOKEN, SUBJECT) == PROFILE
    assert fake.calls == 3


# --- the body ---------------------------------------------------------------------------------


async def test_a_body_over_max_response_bytes_is_refused() -> None:
    fake = FakeUserinfo()
    respond(fake, json_response({**DOCUMENT, "padding": "x" * 200}))
    with pytest.raises(UserinfoResponseError, match="too large"):
        await client_with(fake, max_response_bytes=100).fetch(TOKEN, SUBJECT)
    assert fake.calls == 1


async def test_a_body_at_the_limit_is_accepted() -> None:
    fake = FakeUserinfo()
    response = json_response(DOCUMENT)
    size = len(response.content)
    respond(fake, response)
    assert await client_with(fake, max_response_bytes=size).fetch(TOKEN, SUBJECT) == PROFILE
    with pytest.raises(UserinfoResponseError, match="too large"):
        await client_with(fake, max_response_bytes=size - 1).fetch(TOKEN, SUBJECT)


@pytest.mark.parametrize("content_type", ["text/html", "text/plain", "application/xml", "json"])
async def test_a_content_type_other_than_json_is_refused(content_type: str) -> None:
    fake = FakeUserinfo()
    respond(
        fake,
        httpx.Response(200, content=b'{"sub": "user-1"}', headers={"content-type": content_type}),
    )
    with pytest.raises(UserinfoResponseError, match="not application/json"):
        await client_with(fake).fetch(TOKEN, SUBJECT)


async def test_a_missing_content_type_is_refused() -> None:
    fake = FakeUserinfo()
    response = httpx.Response(200, content=b'{"sub": "user-1"}')
    response.headers.pop("content-type", None)
    respond(fake, response)
    with pytest.raises(UserinfoResponseError, match="no Content-Type"):
        await client_with(fake).fetch(TOKEN, SUBJECT)


async def test_a_signed_or_encrypted_userinfo_response_is_refused_with_an_explicit_message() -> (
    None
):
    fake = FakeUserinfo()
    respond(
        fake, httpx.Response(200, content=b"a.b.c", headers={"content-type": "application/jwt"})
    )
    with pytest.raises(UserinfoResponseError, match="signed or encrypted userinfo responses"):
        await client_with(fake).fetch(TOKEN, SUBJECT)


@pytest.mark.parametrize(
    "content_type",
    ["application/json", "application/json; charset=utf-8", "Application/JSON;charset=UTF-8"],
)
async def test_json_with_utf8_parameters_is_accepted(content_type: str) -> None:
    fake = FakeUserinfo()
    respond(
        fake,
        httpx.Response(
            200,
            content=b'{"sub": "user-1", "org": "client-x"}',
            headers={"content-type": content_type},
        ),
    )
    profile = await client_with(fake).fetch(TOKEN, SUBJECT)
    assert profile.organization == "client-x"


async def test_a_charset_other_than_utf8_is_refused() -> None:
    fake = FakeUserinfo()
    respond(
        fake,
        httpx.Response(
            200,
            content=b'{"sub": "user-1"}',
            headers={"content-type": "application/json; charset=iso-8859-1"},
        ),
    )
    with pytest.raises(UserinfoResponseError, match="charset other than utf-8"):
        await client_with(fake).fetch(TOKEN, SUBJECT)


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (b'{"sub": "user-1", "org": "\xff"}', "not valid UTF-8 JSON"),
        (b"not json at all", "not valid UTF-8 JSON"),
        (b"", "not valid UTF-8 JSON"),
        (b'["sub", "user-1"]', "not a JSON object"),
        (b'"user-1"', "not a JSON object"),
        (b"null", "not a JSON object"),
        (b'{"sub": ' + b'{"a": ' * 200 + b"1" + b"}" * 200 + b"}", "not valid UTF-8 JSON"),
    ],
)
async def test_a_body_that_is_not_one_utf8_json_object_is_refused(
    body: bytes, message: str
) -> None:
    fake = FakeUserinfo()
    respond(fake, httpx.Response(200, content=body, headers={"content-type": "application/json"}))
    with pytest.raises(UserinfoResponseError, match=message):
        await client_with(fake).fetch(TOKEN, SUBJECT)
    assert fake.calls == 1


@pytest.mark.parametrize("document", [{}, {"sub": 7}, {"sub": ""}, {"sub": None}, {"sub": ["a"]}])
async def test_a_document_without_a_string_sub_is_refused(document: dict[str, Any]) -> None:
    fake = FakeUserinfo()
    respond(fake, json_response(document))
    with pytest.raises(UserinfoResponseError, match="no string sub"):
        await client_with(fake).fetch(TOKEN, SUBJECT)


async def test_a_sub_that_is_not_the_token_sub_is_a_mismatch() -> None:
    fake = FakeUserinfo()
    respond(fake, json_response({**DOCUMENT, "sub": "someone-else"}))
    with pytest.raises(UserinfoSubjectMismatchError):
        await client_with(fake).fetch(TOKEN, SUBJECT)
    assert fake.calls == 1


async def test_the_sub_is_compared_literally_not_through_the_identity_path() -> None:
    # identity.subject may name another claim; OIDC Core 5.3.4 compares the literal ``sub``.
    fake = FakeUserinfo()
    respond(fake, json_response({**DOCUMENT, "sub": "Sub-1"}))
    with pytest.raises(UserinfoSubjectMismatchError):
        await client_with(fake).fetch(TOKEN, "sub-1")


@pytest.mark.parametrize(
    "claims",
    [{"org": 5}, {"org": ["client-x"]}, {"groups": "staff"}, {"groups": [1]}, {"email": 3}],
)
async def test_a_claim_of_the_wrong_type_is_refused(claims: dict[str, Any]) -> None:
    fake = FakeUserinfo()
    respond(fake, json_response({**DOCUMENT, **claims}))
    with pytest.raises(UserinfoClaimsError):
        await client_with(fake).fetch(TOKEN, SUBJECT)


@pytest.mark.parametrize("verified", [False, None, "true", 1, "yes"])
async def test_the_email_is_trusted_only_when_the_verified_claim_is_the_boolean_true(
    verified: Any,
) -> None:
    fake = FakeUserinfo()
    respond(fake, json_response({**DOCUMENT, "email_verified": verified}))
    assert (await client_with(fake).fetch(TOKEN, SUBJECT)).email is None


async def test_an_absent_verified_claim_leaves_the_email_out() -> None:
    fake = FakeUserinfo()
    document = {k: v for k, v in DOCUMENT.items() if k != "email_verified"}
    respond(fake, json_response(document))
    assert (await client_with(fake).fetch(TOKEN, SUBJECT)).email is None


async def test_absent_optional_claims_are_absent_in_the_profile() -> None:
    fake = FakeUserinfo()
    respond(fake, json_response({"sub": SUBJECT}))
    assert await client_with(fake).fetch(TOKEN, SUBJECT) == Profile(None, None, ())


async def test_errors_and_logs_name_the_setting_never_the_url_the_token_or_the_document(
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret = "classified-document-text"
    cases: list[tuple[httpx.Response, type[Exception]]] = [
        (httpx.Response(503), UserinfoUnavailableError),
        (httpx.Response(401), UserinfoRejectedError),
        (httpx.Response(404), UserinfoResponseError),
        (json_response({"sub": SUBJECT, "org": secret, "groups": secret}), UserinfoClaimsError),
        (json_response({"sub": secret}), UserinfoSubjectMismatchError),
        (
            httpx.Response(
                200, content=secret.encode(), headers={"content-type": "application/json"}
            ),
            UserinfoResponseError,
        ),
    ]
    with caplog.at_level(logging.DEBUG, logger="mcp_gateway"):
        for response, error in cases:
            fake = FakeUserinfo()
            respond(fake, response)
            with pytest.raises(error) as caught:
                await client_with(fake, max_attempts=2).fetch(TOKEN, SUBJECT)
            text = str(caught.value)
            assert secret not in text
            assert TOKEN not in text
            assert "userinfo.example.com" not in text
    assert secret not in caplog.text
    assert TOKEN not in caplog.text
    assert "userinfo.example.com" not in caplog.text


# --- the memory store --------------------------------------------------------------------------


async def test_the_memory_store_is_keyed_by_a_digest_of_the_token_not_the_subject() -> None:
    store = MemoryUserinfoStore(10)
    await store.put("token-a", PROFILE, 60)
    assert await store.get("token-a") == PROFILE
    assert await store.get("token-b") is None
    assert token_digest("token-a") == token_digest("token-a") != token_digest("token-b")
    assert re.fullmatch(r"[0-9a-f]{64}", token_digest("token-a"))


async def test_the_memory_store_evicts_the_least_recently_used_entry() -> None:
    store = MemoryUserinfoStore(2)
    await store.put("a", PROFILE, 60)
    await store.put("b", PROFILE, 60)
    assert await store.get("a") == PROFILE  # a is now the most recent
    await store.put("c", PROFILE, 60)
    assert len(store) == 2
    assert await store.get("b") is None
    assert await store.get("a") == PROFILE
    assert await store.get("c") == PROFILE


async def test_the_memory_store_forgets_an_entry_at_its_ttl() -> None:
    now = 1000.0
    store = MemoryUserinfoStore(10, clock=lambda: now)
    await store.put("a", PROFILE, 5)
    now = 1004.9
    assert await store.get("a") == PROFILE
    now = 1005.0
    assert await store.get("a") is None
    assert len(store) == 0


# --- the service: cache, ttl bound, single flight ----------------------------------------------


class RecordingStore:
    """A memory store that records the TTLs it was given and can be broken."""

    def __init__(self, *, failing: tuple[str, ...] = ()) -> None:
        self.inner = MemoryUserinfoStore(100)
        self.ttls: list[float] = []
        self._failing = failing

    async def get(self, token: str) -> Profile | None:
        if "get" in self._failing:
            raise StateUnavailableError("shared state unavailable (FakeError)")
        return await self.inner.get(token)

    async def put(self, token: str, profile: Profile, ttl_seconds: float) -> None:
        if "put" in self._failing:
            raise StateUnavailableError("shared state unavailable (FakeError)")
        self.ttls.append(ttl_seconds)
        await self.inner.put(token, profile, ttl_seconds)


NOW = 1_800_000_000.0
FAR = NOW + 3600


def service_with(
    fake: FakeUserinfo,
    store: RecordingStore | None = None,
    **overrides: Any,
) -> tuple[UserinfoService, RecordingStore]:
    config: UserinfoConfig = userinfo_config(**overrides)
    recording = store or RecordingStore()
    http = httpx.AsyncClient(transport=httpx.MockTransport(fake.handle))
    service = UserinfoService(
        UserinfoClient(config, PATHS, http), recording, config, wall_clock=lambda: NOW
    )
    return service, recording


async def ask(
    service: UserinfoService, token: str, subject: str, expires_at: float
) -> tuple[Profile, str]:
    """The profile and the cache result the service reported for this call."""
    results: list[str] = []
    profile = await service.profile(token, subject, expires_at, on_cache_result=results.append)
    (result,) = results
    return profile, result


async def test_a_second_call_with_the_same_token_is_a_hit_and_asks_the_idp_once() -> None:
    fake = FakeUserinfo()
    service, _ = service_with(fake)
    assert await ask(service, TOKEN, SUBJECT, FAR) == (PROFILE, "miss")
    assert await ask(service, TOKEN, SUBJECT, FAR) == (PROFILE, "hit")
    assert fake.calls == 1


async def test_the_entry_lives_cache_ttl_seconds_when_the_token_outlives_it() -> None:
    service, store = service_with(FakeUserinfo(), cache_ttl_seconds=300)
    await ask(service, TOKEN, SUBJECT, FAR)
    assert store.ttls == [300]


async def test_the_entry_ttl_is_bounded_by_the_token_expiry() -> None:
    service, store = service_with(FakeUserinfo(), cache_ttl_seconds=300)
    await ask(service, TOKEN, SUBJECT, NOW + 3)
    assert store.ttls == [pytest.approx(3.0)]


@pytest.mark.parametrize("expires_at", [NOW, NOW - 1, NOW - 5])
async def test_a_token_with_no_time_left_is_not_cached(expires_at: float) -> None:
    fake = FakeUserinfo()
    service, store = service_with(fake)
    assert (await ask(service, TOKEN, SUBJECT, expires_at))[1] == "miss"
    assert (await ask(service, TOKEN, SUBJECT, expires_at))[1] == "miss"
    assert store.ttls == []
    assert fake.calls == 2


@pytest.mark.parametrize(
    ("first", "error"),
    [
        (httpx.Response(401), UserinfoRejectedError),
        (httpx.Response(503), UserinfoUnavailableError),
        (httpx.Response(404), UserinfoResponseError),
        (json_response({**DOCUMENT, "sub": "other"}), UserinfoSubjectMismatchError),
        (json_response({**DOCUMENT, "org": 5}), UserinfoClaimsError),
    ],
)
async def test_an_error_is_never_cached_a_failure_then_a_success_asks_the_idp_twice(
    first: httpx.Response, error: type[Exception]
) -> None:
    fake = FakeUserinfo()
    answers = iter([first])
    fake.responder = lambda _request: next(answers, None) or json_response(DOCUMENT)
    service, store = service_with(fake, max_attempts=1)
    with pytest.raises(error):
        await ask(service, TOKEN, SUBJECT, FAR)
    assert store.ttls == []
    assert await ask(service, TOKEN, SUBJECT, FAR) == (PROFILE, "miss")
    assert fake.calls == 2


async def test_the_cache_is_per_token_not_per_subject() -> None:
    fake = FakeUserinfo()
    service, _ = service_with(fake)
    await ask(service, "token-one", SUBJECT, FAR)
    await ask(service, "token-two", SUBJECT, FAR)
    assert fake.calls == 2
    assert sorted(fake.tokens) == ["token-one", "token-two"]


class Gated:
    """A userinfo responder held open until ``gate`` is set; records its own cancellation."""

    def __init__(self, fake: FakeUserinfo) -> None:
        self.gate = asyncio.Event()
        self.entered = asyncio.Event()
        self.cancelled = 0
        fake.responder = self.answer

    async def answer(self, _request: httpx.Request) -> httpx.Response:
        self.entered.set()
        try:
            await self.gate.wait()
        except asyncio.CancelledError:
            self.cancelled += 1
            raise
        return json_response(DOCUMENT)


async def settle() -> None:
    await asyncio.sleep(0.02)


async def test_concurrent_misses_for_one_token_share_one_idp_call() -> None:
    fake = FakeUserinfo()
    gated = Gated(fake)
    service, _ = service_with(fake)
    tasks = [asyncio.create_task(ask(service, TOKEN, SUBJECT, FAR)) for _ in range(10)]
    await gated.entered.wait()
    await settle()
    assert fake.calls == 1
    gated.gate.set()
    results = await asyncio.gather(*tasks)
    assert fake.calls == 1
    assert {cache for _, cache in results} == {"miss"}
    assert all(profile == PROFILE for profile, _ in results)


async def test_distinct_tokens_make_distinct_calls() -> None:
    fake = FakeUserinfo()
    gated = Gated(fake)
    service, _ = service_with(fake)
    tasks = [
        asyncio.create_task(ask(service, f"token-{n}", SUBJECT, FAR))
        for n in range(3)
        for _ in range(2)
    ]
    await settle()
    assert fake.calls == 3
    gated.gate.set()
    await asyncio.gather(*tasks)
    assert fake.calls == 3


async def test_an_error_reaches_every_waiter_and_is_forgotten() -> None:
    fake = FakeUserinfo()
    release = asyncio.Event()
    answers = iter([httpx.Response(401)])

    async def answer(_request: httpx.Request) -> httpx.Response:
        await release.wait()
        return next(answers, None) or json_response(DOCUMENT)

    fake.responder = answer
    service, _ = service_with(fake)
    tasks = [asyncio.create_task(ask(service, TOKEN, SUBJECT, FAR)) for _ in range(3)]
    await settle()
    release.set()
    results = await asyncio.gather(*tasks, return_exceptions=True)
    assert all(isinstance(r, UserinfoRejectedError) for r in results)
    assert fake.calls == 1
    assert (await ask(service, TOKEN, SUBJECT, FAR))[0] == PROFILE


async def test_a_waiter_leaving_does_not_cancel_the_call_for_the_others() -> None:
    fake = FakeUserinfo()
    gated = Gated(fake)
    service, _ = service_with(fake)
    first = asyncio.create_task(ask(service, TOKEN, SUBJECT, FAR))
    second = asyncio.create_task(ask(service, TOKEN, SUBJECT, FAR))
    await gated.entered.wait()
    first.cancel()
    await settle()
    assert gated.cancelled == 0
    gated.gate.set()
    assert (await second)[0] == PROFILE
    assert fake.calls == 1


async def test_the_last_waiter_leaving_cancels_the_idp_call() -> None:
    fake = FakeUserinfo()
    gated = Gated(fake)
    service, _ = service_with(fake)
    only = asyncio.create_task(ask(service, TOKEN, SUBJECT, FAR))
    await gated.entered.wait()
    only.cancel()
    await settle()
    assert gated.cancelled == 1
    gated.gate.set()
    assert (await ask(service, TOKEN, SUBJECT, FAR))[0] == PROFILE
    assert fake.calls == 2


async def test_a_call_right_after_the_last_waiter_left_starts_a_fresh_call() -> None:
    fake = FakeUserinfo()
    gated = Gated(fake)
    service, _ = service_with(fake)
    only = asyncio.create_task(ask(service, TOKEN, SUBJECT, FAR))
    await gated.entered.wait()
    only.cancel()
    with pytest.raises(asyncio.CancelledError):
        await only
    gated.gate.set()
    assert (await asyncio.wait_for(ask(service, TOKEN, SUBJECT, FAR), timeout=2))[0] == PROFILE
    assert fake.calls == 2


async def test_a_store_that_cannot_be_read_fails_closed_without_asking_the_idp() -> None:
    fake = FakeUserinfo()
    service, _ = service_with(fake, RecordingStore(failing=("get",)))
    with pytest.raises(StateUnavailableError):
        await ask(service, TOKEN, SUBJECT, FAR)
    assert fake.calls == 0


async def test_a_store_that_cannot_be_written_fails_closed() -> None:
    fake = FakeUserinfo()
    service, _ = service_with(fake, RecordingStore(failing=("put",)))
    with pytest.raises(StateUnavailableError):
        await ask(service, TOKEN, SUBJECT, FAR)


async def test_closing_the_service_cancels_the_calls_in_flight() -> None:
    fake = FakeUserinfo()
    gated = Gated(fake)
    service, _ = service_with(fake)
    waiting = asyncio.create_task(ask(service, TOKEN, SUBJECT, FAR))
    await gated.entered.wait()
    await service.aclose()
    await settle()
    assert gated.cancelled == 1
    waiting.cancel()


# --- the Redis store, against an in-memory stand-in for the client ------------------------------

KEY = bytes(range(32))
PREFIX = "gw:"


class FakeRedisClient:
    """The few commands the stores use, over a dict. ``set`` records the TTL it was given."""

    def __init__(self) -> None:
        self.data: dict[str, str] = {}
        self.px: dict[str, int] = {}

    async def get(self, key: str) -> str | None:
        return self.data.get(key)

    async def set(self, key: str, value: str, px: int | None = None) -> None:
        self.data[key] = value
        if px is not None:
            self.px[key] = px

    async def delete(self, key: str) -> None:
        self.data.pop(key, None)

    def register_script(self, _script: str) -> object:
        """The cache store registers its Lua scripts at construction; reads never run them."""
        return object()


class FakeBoundedRedis:
    def __init__(self) -> None:
        self.client = FakeRedisClient()
        self.down = False

    async def run[T](self, operation: Coroutine[Any, Any, T]) -> T:
        if self.down:
            operation.close()
            raise StateUnavailableError("shared state unavailable (FakeError)")
        return await operation


def redis_store(
    redis: FakeBoundedRedis, *, key: bytes = KEY, prefix: str = PREFIX
) -> RedisUserinfoStore:
    return RedisUserinfoStore(cast(BoundedRedis, redis), prefix, key, KeyDigest(key))


def entry_keys(redis: FakeBoundedRedis) -> list[str]:
    return list(redis.client.data)


async def test_the_redis_store_round_trips_a_profile_under_a_key_with_no_token_or_subject() -> None:
    redis = FakeBoundedRedis()
    store = redis_store(redis)
    await store.put(TOKEN, PROFILE, 30)
    (name,) = entry_keys(redis)
    assert re.fullmatch(rf"{PREFIX}u:[0-9a-f]{{64}}", name)
    assert TOKEN not in name
    assert SUBJECT not in name
    assert await store.get(TOKEN) == PROFILE
    assert await store.get("another-token") is None


async def test_the_redis_value_is_ciphertext_with_a_ttl_on_the_key() -> None:
    redis = FakeBoundedRedis()
    await redis_store(redis).put(TOKEN, PROFILE, 30.0004)
    (name,) = entry_keys(redis)
    value = redis.client.data[name]
    raw = base64.b64decode(value, validate=True)
    for secret in ("alice@client-x.example", "client-x", "staff", TOKEN, SUBJECT):
        assert secret not in value
        assert secret.encode() not in raw
    assert redis.client.px[name] == math.ceil(30.0004 * 1000)


async def test_a_tampered_value_is_dropped_as_a_miss_with_a_warning_and_no_content(
    caplog: pytest.LogCaptureFixture,
) -> None:
    redis = FakeBoundedRedis()
    store = redis_store(redis)
    await store.put(TOKEN, PROFILE, 30)
    (name,) = entry_keys(redis)
    raw = bytearray(base64.b64decode(redis.client.data[name]))
    raw[-1] ^= 0x01
    redis.client.data[name] = base64.b64encode(bytes(raw)).decode("ascii")
    with caplog.at_level(logging.DEBUG):
        assert await store.get(TOKEN) is None
    assert entry_keys(redis) == []
    (warning,) = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert (
        warning.getMessage() == "userinfo cache entry failed authentication (InvalidTag); dropped"
    )
    assert "alice@client-x.example" not in caplog.text


@pytest.mark.parametrize("garbage", ["!!! not base64 !!!", "", "AAAA"])
async def test_a_malformed_value_is_dropped_as_a_miss(garbage: str) -> None:
    redis = FakeBoundedRedis()
    store = redis_store(redis)
    await store.put(TOKEN, PROFILE, 30)
    (name,) = entry_keys(redis)
    redis.client.data[name] = garbage
    assert await store.get(TOKEN) is None
    assert entry_keys(redis) == []


async def test_a_value_sealed_by_another_key_is_a_quiet_miss_and_is_left_alone(
    caplog: pytest.LogCaptureFixture,
) -> None:
    redis = FakeBoundedRedis()
    other = redis_store(redis, key=bytes(reversed(range(32))))
    await other.put(TOKEN, PROFILE, 30)
    (name,) = entry_keys(redis)
    sealed = redis.client.data[name]
    # The digest of the key names differs per key, so the reader looks under its own name: put the
    # other key's value there.
    reader = redis_store(redis)
    await reader.put(TOKEN, PROFILE, 30)
    own_name = next(key for key in entry_keys(redis) if key != name)
    redis.client.data[own_name] = sealed
    with caplog.at_level(logging.DEBUG):
        assert await reader.get(TOKEN) is None
    assert redis.client.data[own_name] == sealed
    assert [r for r in caplog.records if r.levelno >= logging.INFO] == []


async def test_a_value_moved_to_another_prefix_does_not_authenticate() -> None:
    redis = FakeBoundedRedis()
    await redis_store(redis, prefix="gw-a:").put(TOKEN, PROFILE, 30)
    (name,) = entry_keys(redis)
    sealed = redis.client.data[name]
    reader = redis_store(redis, prefix="gw-b:")
    entry = KeyDigest(KEY)("userinfo", TOKEN)
    redis.client.data[f"gw-b:u:{entry}"] = sealed
    assert await reader.get(TOKEN) is None
    assert f"gw-b:u:{entry}" not in redis.client.data


async def test_the_sealed_expiry_is_honoured_when_redis_did_not_expire_the_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = FakeBoundedRedis()
    store = redis_store(redis)
    await store.put(TOKEN, PROFILE, 30)
    assert await store.get(TOKEN) == PROFILE
    real = time.time()
    monkeypatch.setattr("mcp_gateway.state.sealing.time.time", lambda: real + 31)
    assert await store.get(TOKEN) is None
    assert entry_keys(redis) == []


async def test_redis_failures_are_state_unavailable_for_both_operations() -> None:
    redis = FakeBoundedRedis()
    store = redis_store(redis)
    redis.down = True
    with pytest.raises(StateUnavailableError):
        await store.get(TOKEN)
    with pytest.raises(StateUnavailableError):
        await store.put(TOKEN, PROFILE, 30)


async def test_an_empty_profile_round_trips() -> None:
    redis = FakeBoundedRedis()
    store = redis_store(redis)
    await store.put(TOKEN, Profile(None, None, ()), 30)
    assert await store.get(TOKEN) == Profile(None, None, ())


# --- the sealing helper keeps the stored format byte for byte ----------------------------------

# Produced by the resolver cache code as it was before the sealing helper was factored out (key
# bytes 0..31, prefix "gw:", nonce bytes 100..111, expiry 4102444800), for the identity
# ("user-1", "alice@client-x.example", "client-x", ("g",)).
GOLDEN_ENTRY_KEY = "gw:c:e0bc0fc6b5d6c6169b72df27d2654e6ea585d7a43cf87d9e1704230c76f0675c"
GOLDEN_CREDENTIAL = (
    "Q9VPLmRlZmdoaWprbG1ubzM5tQ8XjXSkHAEtjb4ABIkro2oop06SAs6Oxy2CgZ9q8ogrpWF5/n3Qw0kNzmDm7W4r65NSLyBF2MuOu47oy6gkrrdv"
    "tC5XdqlVlmFwqAvEBlpiGIWx+aqAglOnFH0qIqxLoU5FfZRc1oKurNBoy7dDX6Il9UiJBoZNp4nqhCb3T5o2+dKc5jLa0zeWQNRgmaY="
)
GOLDEN_DENIED = (
    "Q9VPLmRlZmdoaWprbG1ubzM5tQ8XjXSkHAY6hrMADt9u4HRv6h+cHIXrjiaU/Mgp5JkpriswtyaYiwhWjye5kHovut0Ue3EazsWY7t+xi7p1IGkaLHwv"
    "iphaYGWmC4J5Tw=="
)
GOLDEN_ENTRY = GOLDEN_ENTRY_KEY.removeprefix("gw:c:")
GOLDEN_CREDENTIAL_PLAINTEXT = (
    b'{"kind":"credential","api_key":"fake-key-0123","upstream":"main","labels":{"a":"b"},'
    b'"ttl_seconds":30.0,"expires_at":4102444800.0}'
)


def test_the_sealer_produces_the_bytes_the_resolver_cache_always_produced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "mcp_gateway.state.sealing.os.urandom", lambda n: bytes(range(100, 100 + n))
    )
    sealed = Sealer(KEY, PREFIX).seal(GOLDEN_CREDENTIAL_PLAINTEXT, GOLDEN_ENTRY)
    assert sealed == GOLDEN_CREDENTIAL


def test_the_sealer_opens_a_value_written_by_the_code_before_the_refactor() -> None:
    assert Sealer(KEY, PREFIX).open(GOLDEN_CREDENTIAL, GOLDEN_ENTRY) == GOLDEN_CREDENTIAL_PLAINTEXT
    denied = Sealer(KEY, PREFIX).open(GOLDEN_DENIED, GOLDEN_ENTRY)
    assert denied is not None
    assert json.loads(denied) == {
        "kind": "denied",
        "reason": "no_mapping",
        "expires_at": 4102444800.0,
    }


async def test_the_resolver_cache_store_reads_an_entry_written_before_the_refactor() -> None:
    redis = FakeBoundedRedis()
    redis.client.data[GOLDEN_ENTRY_KEY] = GOLDEN_CREDENTIAL
    store = RedisCacheStore(cast(BoundedRedis, redis), PREFIX, KEY, KeyDigest(KEY))
    outcome = await store.get(("user-1", "alice@client-x.example", "client-x", ("g",)))
    assert isinstance(outcome, Credential)
    assert outcome.api_key == SecretStr("fake-key-0123")
    assert (outcome.upstream, dict(outcome.labels), outcome.ttl_seconds) == (
        "main",
        {"a": "b"},
        30.0,
    )
