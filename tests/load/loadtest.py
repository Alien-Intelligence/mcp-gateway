"""SSE load client: N workers, each holding one long-lived stream at a time, against one or more
targets (a gateway, the upstream stub directly), the same workload run target after target.

It measures what the gateway adds to a stream: the percentiles of the time to the first event and
of the total time per request, per target, and the overhead (gateway minus the direct baseline,
percentile by percentile). Container CPU and memory are sampled separately
(``scripts/loadtest_stats.sh``).

    python -m load.loadtest --help      (from the tests/ directory, with src/ and scripts/ on
                                         PYTHONPATH, as the e2e harness is imported)

Every measurement parameter is an explicit, required argument: nothing here has a default that
changes what is measured. The users' password (OAuth login) comes from the environment
(``E2E_USER_PASSWORD``), never from an argument. Tokens and keys are never printed or written.
"""

import argparse
import asyncio
import codecs
import csv
import dataclasses
import json
import math
import os
import sys
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

import httpx
from e2e.harness import MCP_ACCEPT, MCP_PROTOCOL_VERSION, Grant, Provider, obtain_grant
from e2e.stub.stack import PERSONAS

# Opens the client of one worker (an async context manager).
ClientFactory = Callable[[], httpx.AsyncClient]

PASSWORD_VARIABLE = "E2E_USER_PASSWORD"
GIT_COMMIT_VARIABLE = "LOADTEST_GIT_COMMIT"
PERCENTILES = (50.0, 95.0, 99.0)
# A token is re-obtained when less than max(MIN, FRACTION x its own lifetime) is left, so no
# request starts with a stale one. The margin follows the token's lifetime: a fixed margin as long
# as the lifetime itself would log in again on every request.
TOKEN_REFRESH_MIN_MARGIN_SECONDS = 5.0
TOKEN_REFRESH_LIFETIME_FRACTION = 0.2
OVERHEAD_NOTE = (
    "overhead = percentile of the target minus the same percentile of the baseline target. It is "
    "a difference of two distributions measured one after the other, not a per-request pairing."
)
CSV_COLUMNS = (
    "target",
    "persona",
    "worker",
    "phase",
    "start_unix",
    "headers_seconds",
    "first_event_seconds",
    "total_seconds",
    "status",
    "rpc_error",
    "tool_error",
    "events",
    "ok",
    "failure_reason",
)


# ---------------------------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------------------------


def percentile(values: Sequence[float], q: float) -> float:
    """The ``q``-th percentile (0 to 100) by linear interpolation between closest ranks."""
    if not values:
        raise ValueError("percentile of no values")
    if not 0.0 <= q <= 100.0:
        raise ValueError(f"percentile {q} outside 0..100")
    ordered = sorted(values)
    position = (len(ordered) - 1) * q / 100.0
    lower = math.floor(position)
    upper = math.ceil(position)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def distribution(values: Sequence[float]) -> dict[str, float] | None:
    """p50, p95, p99 and max in seconds; ``None`` when there is nothing to describe."""
    if not values:
        return None
    result = {f"p{q:g}": percentile(values, q) for q in PERCENTILES}
    result["max"] = max(values)
    return result


def overhead(
    target: dict[str, float] | None, baseline: dict[str, float] | None
) -> dict[str, float] | None:
    """Target minus baseline for each reported percentile (not the max); ``None`` if either side
    has no data."""
    if target is None or baseline is None:
        return None
    return {f"p{q:g}": target[f"p{q:g}"] - baseline[f"p{q:g}"] for q in PERCENTILES}


# ---------------------------------------------------------------------------------------------
# One exchange: a POST whose answer is JSON or an SSE stream ending in the reply
# ---------------------------------------------------------------------------------------------


class SseParser:
    """Incremental server-sent-events parser: bytes in, complete events out.

    An event is the data lines (joined by newlines) of one blank-line-terminated block; blocks
    made of comments only are not events.
    """

    def __init__(self) -> None:
        self._decoder = codecs.getincrementaldecoder("utf-8")()
        self._buffer = ""

    def feed(self, chunk: bytes) -> list[str]:
        self._buffer += self._decoder.decode(chunk)
        self._buffer = self._buffer.replace("\r\n", "\n")
        *blocks, self._buffer = self._buffer.split("\n\n")
        events = []
        for block in blocks:
            fields = [line for line in block.split("\n") if line and not line.startswith(":")]
            if fields:
                data = [line[5:].removeprefix(" ") for line in fields if line.startswith("data:")]
                events.append("\n".join(data))
        return events


@dataclass(frozen=True)
class Reply:
    """What a JSON-RPC response says: a protocol error code, a tool error flag, both optional."""

    rpc_error: int | None
    tool_error: bool


def reply_of(message: object, request_id: int) -> Reply | None:
    """The reply to ``request_id`` if ``message`` is it (a JSON-RPC response), else ``None``."""
    if not isinstance(message, dict) or message.get("id") != request_id:
        return None
    error = message.get("error")
    if isinstance(error, dict):
        code = error.get("code")
        return Reply(code if isinstance(code, int) else 0, False)
    result = message.get("result")
    if isinstance(result, dict):
        return Reply(None, result.get("isError") is True)
    return None


def _json_or_none(text: str) -> object:
    try:
        return json.loads(text)
    except ValueError:
        return None


@dataclass
class Exchange:
    status: int | None = None
    headers_seconds: float | None = None
    first_event_seconds: float | None = None
    total_seconds: float | None = None
    events: int = 0
    reply: Reply | None = None
    session_id: str | None = None
    failure: str | None = None


async def exchange(
    client: httpx.AsyncClient,
    url: str,
    headers: dict[str, str],
    payload: dict[str, Any],
    *,
    timeout_seconds: float,
) -> Exchange:
    """POST ``payload`` and read the whole answer, bounded by ``timeout_seconds`` in total.

    Never raises for a network or protocol failure: the failure is the result, so a run records
    it and goes on. The timings are seconds since the request was sent.
    """
    result = Exchange()
    start = time.perf_counter()
    try:
        async with asyncio.timeout(timeout_seconds):
            await _read_exchange(client, url, headers, payload, start, result)
    except (TimeoutError, httpx.TimeoutException):
        result.failure = "timeout"
    except httpx.HTTPError as exc:
        result.failure = f"transport_error:{type(exc).__name__}"
    result.total_seconds = time.perf_counter() - start
    return result


async def _read_exchange(
    client: httpx.AsyncClient,
    url: str,
    headers: dict[str, str],
    payload: dict[str, Any],
    start: float,
    result: Exchange,
) -> None:
    request_id = payload["id"]
    async with client.stream("POST", url, json=payload, headers=headers) as response:
        result.headers_seconds = time.perf_counter() - start
        result.status = response.status_code
        result.session_id = response.headers.get("mcp-session-id")
        if response.status_code != 200:
            await response.aread()
            result.failure = f"http_{response.status_code}"
            return
        if response.headers.get("content-type", "").startswith("text/event-stream"):
            parser = SseParser()
            async for chunk in response.aiter_raw():
                for data in parser.feed(chunk):
                    result.events += 1
                    if result.first_event_seconds is None:
                        result.first_event_seconds = time.perf_counter() - start
                    reply = reply_of(_json_or_none(data), request_id)
                    if reply is not None:
                        result.reply = reply
            if result.reply is None:
                result.failure = "no_reply"
            return
        body = await response.aread()
        result.events = 1
        result.first_event_seconds = time.perf_counter() - start
        result.reply = reply_of(_json_or_none(body.decode("utf-8", errors="replace")), request_id)
        if result.reply is None:
            result.failure = "no_reply"


def classify(result: Exchange, expected_events: int) -> str | None:
    """The failure reason of a tools/call exchange, ``None`` when it counts as a success."""
    if result.failure is not None:
        return result.failure
    if result.reply is not None and result.reply.rpc_error is not None:
        return "rpc_error"
    if result.reply is not None and result.reply.tool_error:
        return "tool_error"
    if result.events != expected_events:
        return "event_count"
    return None


# ---------------------------------------------------------------------------------------------
# Authentication of each target
# ---------------------------------------------------------------------------------------------


class AuthSource(Protocol):
    async def header(self, persona: str) -> str:
        """The ``Authorization`` header value for ``persona``, fresh enough to start a request."""
        ...

    async def prime(self, personas: Sequence[str]) -> None:
        """Obtain every persona's first credential, one after the other, before any worker starts,
        so the measurement never includes a login and the workers never race to obtain one."""
        ...

    @property
    def grants_obtained(self) -> int:
        """How many OAuth grants (full logins) this source has run so far."""
        ...


@dataclass(frozen=True)
class PasswordSettings:
    user_password: str = field(repr=False)


class StubKeyAuth:
    """The persona's stub API key from the state directory, sent straight to the upstream."""

    def __init__(self, state_dir: Path, personas: Sequence[str]) -> None:
        self._keys = {
            name: (state_dir / f"{name}.key").read_text(encoding="utf-8").strip()
            for name in personas
        }
        for name, key in self._keys.items():
            if not key:
                raise ValueError(f"the key file of {name} is empty")

    async def header(self, persona: str) -> str:
        return f"Bearer {self._keys[persona]}"

    async def prime(self, personas: Sequence[str]) -> None:
        missing = [name for name in personas if name not in self._keys]
        if missing:
            raise ValueError(f"no stub key loaded for {missing}")

    @property
    def grants_obtained(self) -> int:
        return 0


@dataclass(frozen=True)
class CachedToken:
    token: str = field(repr=False)
    expires_at: float
    refresh_margin: float


def token_refresh_margin(lifetime_seconds: float) -> float:
    return max(TOKEN_REFRESH_MIN_MARGIN_SECONDS, TOKEN_REFRESH_LIFETIME_FRACTION * lifetime_seconds)


def _numeric_claim(claims: dict[str, Any], name: str) -> float:
    value = claims.get(name)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(
            f"the access token has no numeric {name!r} claim; the refresh margin is relative to "
            "the token's own lifetime (exp - iat), so both are required"
        )
    return float(value)


def cache_token(grant: Grant) -> CachedToken:
    """The token with its expiry and the margin derived from its own lifetime (``exp - iat``)."""
    expires_at = _numeric_claim(grant.claims, "exp")
    lifetime = expires_at - _numeric_claim(grant.claims, "iat")
    if lifetime <= 0:
        raise ValueError(f"the access token's lifetime (exp - iat) is {lifetime:g} s, not positive")
    return CachedToken(grant.token, expires_at, token_refresh_margin(lifetime))


class OAuthAuth:
    """Real OAuth tokens (DCR, PKCE, the provider's login form) obtained through the e2e
    harness once per persona, and obtained again when one is about to expire.

    ``fetch_grant`` runs one full grant for a persona; ``clock`` is the wall clock (unix seconds,
    as the token's ``exp``), both injected so the refresh logic is testable without a provider.
    """

    def __init__(
        self,
        fetch_grant: Callable[[str], Awaitable[Grant]],
        clock: Callable[[], float],
    ) -> None:
        self._fetch_grant = fetch_grant
        self._clock = clock
        self._tokens: dict[str, CachedToken] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._grants = 0

    @property
    def grants_obtained(self) -> int:
        return self._grants

    async def prime(self, personas: Sequence[str]) -> None:
        for persona in personas:
            await self.header(persona)

    async def header(self, persona: str) -> str:
        lock = self._locks.setdefault(persona, asyncio.Lock())
        async with lock:
            cached = self._tokens.get(persona)
            if cached is None or cached.expires_at - self._clock() < cached.refresh_margin:
                cached = cache_token(await self._fetch_grant(persona))
                self._grants += 1
                self._tokens[persona] = cached
        return f"Bearer {cached.token}"


# ---------------------------------------------------------------------------------------------
# The workload
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Workload:
    personas: tuple[str, ...]
    concurrency: int
    requests_per_worker: int
    warmup_requests: int
    tool: str
    arguments: dict[str, Any]
    expected_events: int
    timeout_seconds: float


@dataclass(frozen=True)
class Sample:
    """One request as it is written to the raw CSV."""

    target: str
    persona: str
    worker: int
    phase: str
    start_unix: float
    headers_seconds: float | None
    first_event_seconds: float | None
    total_seconds: float | None
    status: int | None
    rpc_error: int | None
    tool_error: bool
    events: int
    ok: bool
    failure_reason: str | None


class StartGate:
    """Holds every worker after its handshake until all have finished theirs, so the measured
    calls start together and N workers really are N concurrent streams."""

    def __init__(self, workers: int) -> None:
        self._pending = workers
        self._open = asyncio.Event()

    async def arrive(self) -> None:
        self._pending -= 1
        if self._pending == 0:
            self._open.set()
        await self._open.wait()


def _headers(authorization: str, session_id: str | None) -> dict[str, str]:
    headers = {
        "Authorization": authorization,
        "Accept": MCP_ACCEPT,
        "Content-Type": "application/json",
        "MCP-Protocol-Version": MCP_PROTOCOL_VERSION,
    }
    if session_id is not None:
        headers["Mcp-Session-Id"] = session_id
    return headers


def _initialize(request_id: int) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "initialize",
        "params": {
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "mcp-gateway-loadtest", "version": "0"},
        },
    }


def _sample(
    target: str,
    persona: str,
    worker: int,
    phase: str,
    start_unix: float,
    result: Exchange,
    reason: str | None,
) -> Sample:
    return Sample(
        target=target,
        persona=persona,
        worker=worker,
        phase=phase,
        start_unix=start_unix,
        headers_seconds=result.headers_seconds,
        first_event_seconds=result.first_event_seconds,
        total_seconds=result.total_seconds,
        status=result.status,
        rpc_error=result.reply.rpc_error if result.reply else None,
        tool_error=result.reply.tool_error if result.reply else False,
        events=result.events,
        ok=reason is None,
        failure_reason=reason,
    )


def _refused_sample(target: str, persona: str, worker: int, phase: str, reason: str) -> Sample:
    return Sample(
        target=target,
        persona=persona,
        worker=worker,
        phase=phase,
        start_unix=time.time(),
        headers_seconds=None,
        first_event_seconds=None,
        total_seconds=None,
        status=None,
        rpc_error=None,
        tool_error=False,
        events=0,
        ok=False,
        failure_reason=reason,
    )


async def _handshake(
    client: httpx.AsyncClient, url: str, auth: AuthSource, persona: str, workload: Workload
) -> tuple[str | None, str | None]:
    """``initialize`` then ``notifications/initialized``: (session id, failure reason)."""
    authorization = await auth.header(persona)
    opened = await exchange(
        client,
        url,
        _headers(authorization, None),
        _initialize(1),
        timeout_seconds=workload.timeout_seconds,
    )
    if opened.failure is not None:
        return None, f"initialize_failed:{opened.failure}"
    if opened.reply is None or opened.reply.rpc_error is not None:
        return None, "initialize_failed:rpc_error"
    notified = await _notify_initialized(
        client, url, _headers(authorization, opened.session_id), workload.timeout_seconds
    )
    if notified is not None:
        return None, f"initialize_failed:{notified}"
    return opened.session_id, None


async def _notify_initialized(
    client: httpx.AsyncClient, url: str, headers: dict[str, str], timeout_seconds: float
) -> str | None:
    """POST the initialized notification; the failure reason, ``None`` on the expected 202."""
    payload = {"jsonrpc": "2.0", "method": "notifications/initialized"}
    try:
        async with asyncio.timeout(timeout_seconds):
            response = await client.post(url, json=payload, headers=headers)
    except (TimeoutError, httpx.TimeoutException):
        return "timeout"
    except httpx.HTTPError as exc:
        return f"transport_error:{type(exc).__name__}"
    return None if response.status_code in (200, 202) else f"http_{response.status_code}"


async def _worker(
    index: int,
    open_client: ClientFactory,
    target: str,
    url: str,
    auth: AuthSource,
    workload: Workload,
    gate: StartGate,
    samples: list[Sample],
) -> None:
    persona = workload.personas[index % len(workload.personas)]
    async with open_client() as client:
        await _run_worker(index, client, target, url, auth, workload, gate, samples, persona)


async def _run_worker(
    index: int,
    client: httpx.AsyncClient,
    target: str,
    url: str,
    auth: AuthSource,
    workload: Workload,
    gate: StartGate,
    samples: list[Sample],
    persona: str,
) -> None:
    session_id, refused = await _handshake(client, url, auth, persona, workload)
    await gate.arrive()
    phases = ["warmup"] * workload.warmup_requests + ["call"] * workload.requests_per_worker
    if refused is not None:
        # The calls this worker could not make count as errors, so a dead worker cannot make
        # the error rate look better.
        for phase in phases:
            samples.append(_refused_sample(target, persona, index, phase, refused))
        return
    for number, phase in enumerate(phases, start=2):
        payload = {
            "jsonrpc": "2.0",
            "id": number,
            "method": "tools/call",
            "params": {"name": workload.tool, "arguments": workload.arguments},
        }
        authorization = await auth.header(persona)
        start_unix = time.time()
        result = await exchange(
            client,
            url,
            _headers(authorization, session_id),
            payload,
            timeout_seconds=workload.timeout_seconds,
        )
        reason = classify(result, workload.expected_events)
        samples.append(_sample(target, persona, index, phase, start_unix, result, reason))


async def run_target(
    open_client: ClientFactory, name: str, url: str, auth: AuthSource, workload: Workload
) -> list[Sample]:
    """Run the whole workload against one target and return every sample (warmup included).

    Every worker opens its own client, so it owns its connection: see ``make_client``."""
    # Every persona's first token before the workers start: the measurement has no login in it.
    await auth.prime(workload.personas)
    samples: list[Sample] = []
    gate = StartGate(workload.concurrency)
    await asyncio.gather(
        *(
            _worker(index, open_client, name, url, auth, workload, gate, samples)
            for index in range(workload.concurrency)
        )
    )
    return samples


# ---------------------------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------------------------


def summarise_target(samples: Sequence[Sample]) -> dict[str, Any]:
    """Statistics of the measured calls (warmup excluded).

    The percentiles describe the successful requests: an error has no meaningful latency, it is
    counted in the error rate and the error reasons instead.
    """
    calls = [s for s in samples if s.phase == "call"]
    good = [s for s in calls if s.ok]
    reasons: dict[str, int] = {}
    for sample in calls:
        if sample.failure_reason is not None:
            reasons[sample.failure_reason] = reasons.get(sample.failure_reason, 0) + 1
    totals = [s.total_seconds for s in good if s.total_seconds is not None]
    firsts = [s.first_event_seconds for s in good if s.first_event_seconds is not None]
    window = None
    throughput = None
    if good:
        began = min(s.start_unix for s in good)
        ended = max(s.start_unix + (s.total_seconds or 0.0) for s in good)
        window = ended - began
        throughput = len(good) / window if window > 0 else None
    return {
        "count": len(calls),
        "ok": len(good),
        "errors": len(calls) - len(good),
        "errors_by_reason": dict(sorted(reasons.items())),
        "error_rate": (len(calls) - len(good)) / len(calls) if calls else None,
        "total_seconds": distribution(totals),
        "first_event_seconds": distribution(firsts),
        "window_seconds": window,
        "throughput_per_second": throughput,
    }


def build_summary(
    per_target: dict[str, list[Sample]],
    baseline: str | None,
    parameters: dict[str, Any],
    started: datetime,
    ended: datetime,
    git_commit: str | None,
    oauth_grants: dict[str, int],
) -> dict[str, Any]:
    """``oauth_grants``: per target, how many full OAuth logins the run made. More than one per
    persona means tokens expired during the run and were obtained again."""
    targets = {name: summarise_target(samples) for name, samples in per_target.items()}
    overheads: dict[str, Any] = {}
    if baseline is not None:
        for name, stats in targets.items():
            if name == baseline:
                continue
            overheads[name] = {
                "baseline": baseline,
                "total_seconds": overhead(
                    stats["total_seconds"], targets[baseline]["total_seconds"]
                ),
                "first_event_seconds": overhead(
                    stats["first_event_seconds"], targets[baseline]["first_event_seconds"]
                ),
            }
    return {
        "parameters": parameters,
        "started_utc": started.isoformat(timespec="seconds"),
        "ended_utc": ended.isoformat(timespec="seconds"),
        "git_commit": git_commit,
        "overhead_note": OVERHEAD_NOTE,
        "oauth_grants": oauth_grants,
        "targets": targets,
        "overhead": overheads,
    }


def write_raw_csv(path: Path, samples: Sequence[Sample]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(CSV_COLUMNS)
        for sample in samples:
            row = dataclasses.asdict(sample)
            writer.writerow(["" if row[c] is None else row[c] for c in CSV_COLUMNS])


def _ms(value: float | None) -> str:
    return "-" if value is None else f"{value * 1000:.1f}"


def format_table(summary: dict[str, Any]) -> str:
    lines = [
        f"{'target':<14}{'calls':>7}{'errors':>8}{'err%':>7}"
        f"{'p50 ms':>10}{'p95 ms':>10}{'p99 ms':>10}{'max ms':>10}"
        f"{'ttfe p50':>10}{'ttfe p95':>10}{'ttfe p99':>10}{'req/s':>9}"
    ]
    for name, stats in summary["targets"].items():
        total = stats["total_seconds"] or {}
        first = stats["first_event_seconds"] or {}
        rate = stats["error_rate"]
        throughput = stats["throughput_per_second"]
        lines.append(
            f"{name:<14}{stats['count']:>7}{stats['errors']:>8}"
            f"{'-' if rate is None else f'{rate * 100:.1f}':>7}"
            f"{_ms(total.get('p50')):>10}{_ms(total.get('p95')):>10}{_ms(total.get('p99')):>10}"
            f"{_ms(total.get('max')):>10}"
            f"{_ms(first.get('p50')):>10}{_ms(first.get('p95')):>10}{_ms(first.get('p99')):>10}"
            f"{'-' if throughput is None else f'{throughput:.1f}':>9}"
        )
    for name, values in summary["overhead"].items():
        total = values["total_seconds"] or {}
        first = values["first_event_seconds"] or {}
        lines.append(
            f"overhead {name} vs {values['baseline']} (ms): "
            f"total p50/p95/p99 {_ms(total.get('p50'))}/{_ms(total.get('p95'))}/"
            f"{_ms(total.get('p99'))}; first event p50/p95/p99 "
            f"{_ms(first.get('p50'))}/{_ms(first.get('p95'))}/{_ms(first.get('p99'))}"
        )
    if summary["overhead"]:
        lines.append(f"note: {summary['overhead_note']}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Target:
    name: str
    url: str
    auth: str


def _pairs(values: Sequence[str], option: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for value in values:
        name, separator, rest = value.partition("=")
        if not separator or not name or not rest:
            raise ValueError(f"{option} expects name=value, got {value!r}")
        if name in result:
            raise ValueError(f"{option} names {name!r} twice")
        result[name] = rest
    return result


def parse_targets(urls: Sequence[str], auths: Sequence[str]) -> list[Target]:
    """Pair each ``--target-url name=url`` with its ``--target-auth name=oauth:<issuer>`` or
    ``name=stub-key``; a target without an authentication, or the reverse, is refused."""
    url_by_name = _pairs(urls, "--target-url")
    auth_by_name = _pairs(auths, "--target-auth")
    if set(url_by_name) != set(auth_by_name):
        raise ValueError("--target-url and --target-auth must name the same targets")
    for name, auth in auth_by_name.items():
        if auth != "stub-key" and not auth.startswith("oauth:"):
            raise ValueError(f"--target-auth {name}: expected 'stub-key' or 'oauth:<issuer>'")
    return [Target(name, url_by_name[name], auth_by_name[name]) for name in url_by_name]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run N concurrent long-lived SSE streams against each target in turn."
    )
    add = parser.add_argument
    add("--target-url", action="append", required=True, metavar="NAME=URL")
    add(
        "--target-auth",
        action="append",
        required=True,
        metavar="NAME=oauth:ISSUER|stub-key",
        help="oauth:<issuer> logs in through the issuer (password in E2E_USER_PASSWORD); "
        "stub-key sends the persona's key from --state-dir",
    )
    add("--baseline-target", help="target the overhead is computed against; omit for no overhead")
    add("--personas", nargs="+", required=True)
    add("--state-dir", type=Path, help="directory of <persona>.key files (stub-key targets)")
    add("--concurrency", type=int, required=True)
    add("--requests-per-worker", type=int, required=True)
    add("--warmup-requests", type=int, required=True)
    add("--tool", required=True)
    add("--arguments-json", required=True)
    add("--expected-events", type=int, required=True, help="events of one reply stream")
    add("--timeout-seconds", type=float, required=True, help="per request, in total")
    add("--max-run-seconds", type=float, required=True, help="wall-clock ceiling of the run")
    add("--out-dir", type=Path, required=True)
    return parser


def _validate(args: argparse.Namespace) -> tuple[list[Target], Workload]:
    targets = parse_targets(args.target_url, args.target_auth)
    names = {target.name for target in targets}
    if args.baseline_target is not None and args.baseline_target not in names:
        raise ValueError(f"--baseline-target {args.baseline_target!r} is not a target")
    unknown = [p for p in args.personas if p not in PERSONAS]
    if unknown:
        raise ValueError(f"unknown personas {unknown}; known: {sorted(PERSONAS)}")
    for option in ("concurrency", "requests_per_worker", "expected_events"):
        if getattr(args, option) < 1:
            raise ValueError(f"--{option.replace('_', '-')} must be at least 1")
    if args.warmup_requests < 0:
        raise ValueError("--warmup-requests must not be negative")
    for option in ("timeout_seconds", "max_run_seconds"):
        if getattr(args, option) <= 0:
            raise ValueError(f"--{option.replace('_', '-')} must be positive")
    arguments = json.loads(args.arguments_json)
    if not isinstance(arguments, dict):
        raise ValueError("--arguments-json must be a JSON object")
    if any(t.auth == "stub-key" for t in targets) and args.state_dir is None:
        raise ValueError("a stub-key target needs --state-dir")
    if any(t.auth.startswith("oauth:") for t in targets) and not os.environ.get(PASSWORD_VARIABLE):
        raise ValueError(f"an oauth target needs {PASSWORD_VARIABLE} in the environment")
    workload = Workload(
        personas=tuple(args.personas),
        concurrency=args.concurrency,
        requests_per_worker=args.requests_per_worker,
        warmup_requests=args.warmup_requests,
        tool=args.tool,
        arguments=arguments,
        expected_events=args.expected_events,
        timeout_seconds=args.timeout_seconds,
    )
    return targets, workload


def make_auth(target: Target, workload: Workload, state_dir: Path | None) -> AuthSource:
    if target.auth == "stub-key":
        assert state_dir is not None  # checked by _validate
        return StubKeyAuth(state_dir, workload.personas)
    provider = Provider(
        name=target.name,
        gateway_url=target.url,
        issuer=target.auth.removeprefix("oauth:"),
        wrong_audience={},
    )
    settings = PasswordSettings(os.environ[PASSWORD_VARIABLE])

    async def fetch_grant(persona: str) -> Grant:
        return await obtain_grant(settings, provider, PERSONAS[persona])

    return OAuthAuth(fetch_grant, time.time)


def make_client(timeout_seconds: float) -> httpx.AsyncClient:
    """The client of ONE worker: a pool of a single connection, kept alive between its requests.

    The workers must not share a client. httpcore re-scans its whole pool on every request and
    every response end, and that scan is quadratic in the number of pooled connections: with
    about 200 idle keep-alive connections one scan round after a burst of streams ending together
    costs seconds of CPU on the event loop, so the requests that follow wait for the client, not
    for the server (the time to headers of the direct baseline then reads one stream length too
    long). One pool of one connection per worker has nothing to scan.
    """
    limits = httpx.Limits(max_connections=1, max_keepalive_connections=1)
    return httpx.AsyncClient(
        limits=limits, timeout=httpx.Timeout(timeout_seconds, connect=min(timeout_seconds, 10.0))
    )


async def run_all(
    targets: Sequence[Target],
    workload: Workload,
    state_dir: Path | None,
    out_dir: Path,
    *,
    baseline: str | None,
    parameters: dict[str, Any],
    git_commit: str | None,
) -> dict[str, Any]:
    """The targets one after the other, the same workload each; raw CSVs and the summary written
    to ``out_dir``."""
    started = datetime.now(UTC)
    per_target: dict[str, list[Sample]] = {}
    oauth_grants: dict[str, int] = {}
    for target in targets:
        auth = make_auth(target, workload, state_dir)
        print(f"== {target.name}: {workload.concurrency} workers", flush=True)
        samples = await run_target(
            lambda: make_client(workload.timeout_seconds), target.name, target.url, auth, workload
        )
        per_target[target.name] = samples
        oauth_grants[target.name] = auth.grants_obtained
        write_raw_csv(out_dir / f"raw-{target.name}.csv", samples)
    summary = build_summary(
        per_target, baseline, parameters, started, datetime.now(UTC), git_commit, oauth_grants
    )
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        targets, workload = _validate(args)
    except ValueError as exc:
        parser.error(str(exc))
    args.out_dir.mkdir(parents=True, exist_ok=True)
    parameters = {
        "targets": {t.name: {"url": t.url, "auth": t.auth.split(":", 1)[0]} for t in targets},
        "baseline_target": args.baseline_target,
        "personas": list(workload.personas),
        "concurrency": workload.concurrency,
        "requests_per_worker": workload.requests_per_worker,
        "warmup_requests": workload.warmup_requests,
        "tool": workload.tool,
        "arguments": workload.arguments,
        "expected_events": workload.expected_events,
        "timeout_seconds": workload.timeout_seconds,
        "max_run_seconds": args.max_run_seconds,
    }
    try:
        summary = asyncio.run(
            asyncio.wait_for(
                run_all(
                    targets,
                    workload,
                    args.state_dir,
                    args.out_dir,
                    baseline=args.baseline_target,
                    parameters=parameters,
                    git_commit=os.environ.get(GIT_COMMIT_VARIABLE) or None,
                ),
                timeout=args.max_run_seconds,
            )
        )
    except TimeoutError:
        print(f"the run exceeded --max-run-seconds {args.max_run_seconds:g}", file=sys.stderr)
        return 3
    print(format_table(summary))
    return 0


if __name__ == "__main__":
    sys.exit(main())
