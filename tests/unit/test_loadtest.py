"""The load tooling, with no network: statistics, the SSE reader, error classification, the
worker loop over a mock transport, the output files and the container-stats summariser."""

import asyncio
import csv
import json
from collections.abc import AsyncIterator, Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from e2e.harness import Grant, Persona, Provider
from load import loadtest, stats_summary

START = datetime(2026, 1, 1, tzinfo=UTC)


def sample(
    target: str = "gateway",
    *,
    phase: str = "call",
    total: float | None = 1.0,
    first: float | None = 0.1,
    reason: str | None = None,
    start: float = 1000.0,
) -> loadtest.Sample:
    return loadtest.Sample(
        target=target,
        persona="alice",
        worker=0,
        phase=phase,
        start_unix=start,
        headers_seconds=0.01,
        first_event_seconds=first,
        total_seconds=total,
        status=200,
        rpc_error=None,
        tool_error=False,
        events=3,
        ok=reason is None,
        failure_reason=reason,
    )


# --- statistics ------------------------------------------------------------------------------


def test_percentile_interpolates_between_ranks() -> None:
    values = [4.0, 1.0, 3.0, 2.0]
    assert loadtest.percentile(values, 0) == 1.0
    assert loadtest.percentile(values, 50) == pytest.approx(2.5)
    assert loadtest.percentile(values, 100) == 4.0
    assert loadtest.percentile([1, 2, 3, 4, 5], 95) == pytest.approx(4.8)
    assert loadtest.percentile([7.0], 99) == 7.0


def test_percentile_refuses_nothing_to_measure_and_bad_rank() -> None:
    with pytest.raises(ValueError, match="no values"):
        loadtest.percentile([], 50)
    with pytest.raises(ValueError, match="outside"):
        loadtest.percentile([1.0], 101)


def test_distribution_reports_percentiles_and_max() -> None:
    result = loadtest.distribution([float(i) for i in range(1, 101)])
    assert result is not None
    assert result["p50"] == pytest.approx(50.5)
    assert result["p99"] == pytest.approx(99.01)
    assert result["max"] == 100.0
    assert loadtest.distribution([]) is None


def test_overhead_is_the_difference_per_percentile() -> None:
    gateway = {"p50": 1.2, "p95": 2.0, "p99": 3.5, "max": 9.0}
    direct = {"p50": 1.0, "p95": 1.5, "p99": 2.0, "max": 4.0}
    assert loadtest.overhead(gateway, direct) == pytest.approx({"p50": 0.2, "p95": 0.5, "p99": 1.5})
    assert loadtest.overhead(None, direct) is None
    assert loadtest.overhead(gateway, None) is None


# --- SSE and replies -------------------------------------------------------------------------


def test_sse_parser_handles_split_chunks_crlf_and_comments() -> None:
    parser = loadtest.SseParser()
    assert parser.feed(b'event: message\r\ndata: {"a"') == []
    assert parser.feed(b":1}\r\n\r\n: ping\n\nid: 2\ndata: x\ndata: y\n") == ['{"a":1}']
    assert parser.feed(b"\n") == ["x\ny"]


def test_sse_parser_decodes_multibyte_characters_split_across_chunks() -> None:
    parser = loadtest.SseParser()
    encoded = "data: é\n\n".encode()
    assert parser.feed(encoded[:7]) == []
    assert parser.feed(encoded[7:]) == ["é"]


def test_reply_of_recognises_result_error_and_tool_error() -> None:
    assert loadtest.reply_of({"id": 3, "result": {}}, 3) == loadtest.Reply(None, False)
    assert loadtest.reply_of({"id": 3, "result": {"isError": True}}, 3) == loadtest.Reply(
        None, True
    )
    assert loadtest.reply_of({"id": 3, "error": {"code": -32601}}, 3) == loadtest.Reply(
        -32601, False
    )
    # a progress notification, another id, and junk are not the reply
    assert loadtest.reply_of({"method": "notifications/progress"}, 3) is None
    assert loadtest.reply_of({"id": 4, "result": {}}, 3) is None
    assert loadtest.reply_of(["id"], 3) is None


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        (loadtest.Exchange(events=4, reply=loadtest.Reply(None, False)), None),
        (loadtest.Exchange(failure="timeout"), "timeout"),
        (loadtest.Exchange(failure="http_502"), "http_502"),
        (loadtest.Exchange(events=4, reply=loadtest.Reply(-32000, False)), "rpc_error"),
        (loadtest.Exchange(events=4, reply=loadtest.Reply(None, True)), "tool_error"),
        (loadtest.Exchange(events=3, reply=loadtest.Reply(None, False)), "event_count"),
    ],
)
def test_classify(result: loadtest.Exchange, expected: str | None) -> None:
    assert loadtest.classify(result, expected_events=4) == expected


# --- the exchange and the workers over a mock transport ----------------------------------------


def sse_body(request_id: int, count: int) -> list[bytes]:
    events = [
        f'event: message\ndata: {{"jsonrpc":"2.0","method":"notifications/progress",'
        f'"params":{{"progress":{n}}}}}\n\n'.encode()
        for n in range(1, count + 1)
    ]
    reply = {"jsonrpc": "2.0", "id": request_id, "result": {"content": [], "isError": False}}
    events.append(f"event: message\ndata: {json.dumps(reply)}\n\n".encode())
    return events


class Upstream:
    """Behaves like the stub: a session id on initialize, SSE for tools/call; can be told to
    fail the calls."""

    def __init__(self, *, call_status: int = 200, count: int = 2) -> None:
        self.call_status = call_status
        self.count = count
        self.seen: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        message = json.loads(request.content)
        method = message.get("method")
        if method == "initialize":
            body = {"jsonrpc": "2.0", "id": message["id"], "result": {}}
            return httpx.Response(200, json=body, headers={"Mcp-Session-Id": "sess-1"})
        if "id" not in message:
            return httpx.Response(202)
        if self.call_status != 200:
            return httpx.Response(self.call_status, json={"error": "nope"})

        async def chunks() -> AsyncIterator[bytes]:
            for chunk in sse_body(message["id"], self.count):
                yield chunk

        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=chunks())


class FixedAuth:
    def __init__(self) -> None:
        self.primed: list[str] = []

    async def header(self, persona: str) -> str:
        return f"Bearer key-of-{persona}"

    async def prime(self, personas: Sequence[str]) -> None:
        self.primed.extend(personas)

    @property
    def grants_obtained(self) -> int:
        return 0


def workload(**overrides: Any) -> loadtest.Workload:
    values: dict[str, Any] = {
        "personas": ("alice", "bob"),
        "concurrency": 3,
        "requests_per_worker": 2,
        "warmup_requests": 1,
        "tool": "slow_sse",
        "arguments": {"count": 2, "delay_seconds": 0.0},
        "expected_events": 3,
        "timeout_seconds": 5.0,
    }
    values.update(overrides)
    return loadtest.Workload(**values)


def clients_of(handler: Callable[[httpx.Request], httpx.Response]) -> loadtest.ClientFactory:
    """A factory of clients over a mock transport: what each worker opens in these tests."""
    return lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def run(upstream: Upstream, load: loadtest.Workload) -> list[loadtest.Sample]:
    return await loadtest.run_target(
        clients_of(upstream), "gateway", "http://up/mcp", FixedAuth(), load
    )


async def test_workers_run_the_workload_and_carry_the_session() -> None:
    upstream = Upstream()
    samples = await run(upstream, workload())
    calls = [s for s in samples if s.phase == "call"]
    assert len(calls) == 3 * 2
    assert len([s for s in samples if s.phase == "warmup"]) == 3
    assert all(s.ok and s.events == 3 and s.first_event_seconds is not None for s in samples)
    assert {s.persona for s in samples} == {"alice", "bob"}
    tool_calls = [r for r in upstream.seen if json.loads(r.content).get("method") == "tools/call"]
    assert len(tool_calls) == 9
    assert {r.headers["mcp-session-id"] for r in tool_calls} == {"sess-1"}
    assert {r.headers["authorization"] for r in tool_calls} == {
        "Bearer key-of-alice",
        "Bearer key-of-bob",
    }
    assert all("text/event-stream" in r.headers["accept"] for r in upstream.seen)


class Clock:
    def __init__(self, now: float) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class GrantIssuer:
    """Stands in for the OAuth journey: issues tokens of a fixed lifetime, stamped by the clock,
    and records the order and the overlap of the grants."""

    def __init__(self, clock: Clock, lifetime: float, log: list[str] | None = None) -> None:
        self.clock = clock
        self.lifetime = lifetime
        self.personas: list[str] = []
        self.running = 0
        self.max_running = 0
        self.log = log if log is not None else []

    async def __call__(self, persona: str) -> Grant:
        self.running += 1
        self.max_running = max(self.max_running, self.running)
        await asyncio.sleep(0)
        self.running -= 1
        self.personas.append(persona)
        self.log.append(f"grant:{persona}")
        return grant_with_claims(
            persona,
            {"iat": self.clock.now, "exp": self.clock.now + self.lifetime},
            token=f"token-{len(self.personas)}",
        )


def grant_with_claims(persona: str, claims: dict[str, Any], token: str = "t") -> Grant:
    return Grant(
        provider=Provider(
            name="p", gateway_url="http://gw/mcp", issuer="http://idp", wrong_audience={}
        ),
        persona=Persona(
            name=persona, email=f"{persona}@x", org_id=None, user_id=None, key_file=None
        ),
        token=token,
        claims=claims,
        challenge={},
    )


@pytest.mark.parametrize(("lifetime", "margin"), [(60.0, 12.0), (3600.0, 720.0), (10.0, 5.0)])
async def test_the_refresh_margin_is_relative_to_the_token_lifetime(
    lifetime: float, margin: float
) -> None:
    clock = Clock(1_000_000.0)
    issuer = GrantIssuer(clock, lifetime)
    auth = loadtest.OAuthAuth(issuer, clock)
    first = await auth.header("alice")
    # Right after obtaining it, and while more than the margin is left: no new login.
    assert await auth.header("alice") == first
    clock.now += lifetime - margin - 0.5
    assert await auth.header("alice") == first
    assert auth.grants_obtained == 1
    # Less than the margin left: a new token.
    clock.now += 1.0
    assert await auth.header("alice") != first
    assert auth.grants_obtained == 2


async def test_each_persona_has_its_own_token_and_grant() -> None:
    clock = Clock(1_000_000.0)
    issuer = GrantIssuer(clock, 3600.0)
    auth = loadtest.OAuthAuth(issuer, clock)
    assert await auth.header("alice") != await auth.header("bob")
    assert issuer.personas == ["alice", "bob"]


@pytest.mark.parametrize(
    "claims",
    [
        {"exp": 2_000_000},
        {"iat": 1_000_000},
        {"iat": "1000000", "exp": 2_000_000},
        {"iat": 1_000_000, "exp": None},
        {"iat": 1_000_000, "exp": 1_000_000},
        {"iat": 1_000_000, "exp": 999_000},
    ],
)
async def test_a_token_without_a_usable_lifetime_is_refused(claims: dict[str, Any]) -> None:
    async def fetch(persona: str) -> Grant:
        return grant_with_claims(persona, claims)

    auth = loadtest.OAuthAuth(fetch, Clock(1_000_000.0))
    with pytest.raises(ValueError, match="exp|iat|lifetime"):
        await auth.header("alice")
    assert auth.grants_obtained == 0


async def test_prime_obtains_one_grant_per_persona_sequentially() -> None:
    clock = Clock(1_000_000.0)
    issuer = GrantIssuer(clock, 60.0)
    auth = loadtest.OAuthAuth(issuer, clock)
    await auth.prime(["alice", "bob", "carol"])
    assert issuer.personas == ["alice", "bob", "carol"]
    assert issuer.max_running == 1
    assert auth.grants_obtained == 3


async def test_run_target_logs_every_persona_in_before_the_first_request() -> None:
    clock = Clock(1_000_000.0)
    log: list[str] = []
    issuer = GrantIssuer(clock, 3600.0, log)
    auth = loadtest.OAuthAuth(issuer, clock)
    upstream = Upstream()

    def recording(request: httpx.Request) -> httpx.Response:
        log.append("request")
        return upstream(request)

    samples = await loadtest.run_target(
        clients_of(recording), "gateway", "http://up/mcp", auth, workload(concurrency=4)
    )
    assert all(s.ok for s in samples)
    # One grant per persona, all done before the workers made any request, and no grant
    # afterwards: the measurement contains no login.
    assert log[:2] == ["grant:alice", "grant:bob"]
    assert log.count("request") == len(upstream.seen) > 0
    assert [entry for entry in log if entry.startswith("grant")] == log[:2]
    assert auth.grants_obtained == 2


async def test_every_worker_owns_its_client_and_its_connection_pool() -> None:
    """Workers sharing one client share one httpcore pool, which scans all its connections on
    every request: at a few hundred idle connections that stalls the event loop for seconds and
    shows up as a server wait (the direct baseline's time to headers)."""
    upstream = Upstream()
    opened: list[httpx.AsyncClient] = []

    def open_client() -> httpx.AsyncClient:
        opened.append(httpx.AsyncClient(transport=httpx.MockTransport(upstream)))
        return opened[-1]

    await loadtest.run_target(open_client, "gateway", "http://up/mcp", FixedAuth(), workload())
    assert len(opened) == 3
    assert all(client.is_closed for client in opened)


async def test_run_target_primes_the_auth_with_the_workload_personas() -> None:
    auth = FixedAuth()
    await loadtest.run_target(clients_of(Upstream()), "gateway", "http://up/mcp", auth, workload())
    assert auth.primed == ["alice", "bob"]


async def test_a_wrong_event_count_is_an_error() -> None:
    samples = await run(Upstream(count=5), workload(warmup_requests=0))
    assert {s.failure_reason for s in samples} == {"event_count"}


async def test_an_upstream_error_status_is_an_error() -> None:
    samples = await run(Upstream(call_status=502), workload(warmup_requests=0))
    assert {s.failure_reason for s in samples} == {"http_502"}
    assert all(s.status == 502 for s in samples)


async def test_a_refused_handshake_counts_every_call_of_the_worker_as_an_error() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "invalid_token"})

    samples = await loadtest.run_target(
        clients_of(refuse), "gateway", "http://up/mcp", FixedAuth(), workload(concurrency=2)
    )
    calls = [s for s in samples if s.phase == "call"]
    assert len(calls) == 4
    assert {s.failure_reason for s in calls} == {"initialize_failed:http_401"}
    assert loadtest.summarise_target(samples)["error_rate"] == 1.0


async def test_a_request_past_the_timeout_is_recorded_as_a_timeout() -> None:
    async def stalled() -> AsyncIterator[bytes]:
        yield b"event: message\ndata: {}\n\n"
        await asyncio.sleep(30)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=stalled())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await loadtest.exchange(
            client, "http://up/mcp", {}, {"id": 2, "method": "tools/call"}, timeout_seconds=0.2
        )
    assert result.failure == "timeout"
    assert result.first_event_seconds is not None
    assert loadtest.classify(result, 3) == "timeout"


# --- summaries and files -----------------------------------------------------------------------


def test_summarise_target_excludes_warmup_and_counts_errors_by_reason() -> None:
    samples = [
        sample(phase="warmup", total=99.0),
        sample(total=1.0, start=100.0),
        sample(total=2.0, start=100.5),
        sample(total=None, first=None, reason="timeout", start=101.0),
        sample(total=None, first=None, reason="http_502", start=101.0),
    ]
    stats = loadtest.summarise_target(samples)
    assert stats["count"] == 4
    assert stats["errors"] == 2
    assert stats["errors_by_reason"] == {"http_502": 1, "timeout": 1}
    assert stats["error_rate"] == 0.5
    assert stats["total_seconds"]["max"] == 2.0
    assert stats["window_seconds"] == pytest.approx(2.5)
    assert stats["throughput_per_second"] == pytest.approx(2 / 2.5)


def test_build_summary_computes_overhead_against_the_baseline_only() -> None:
    per_target = {
        "direct": [sample("direct", total=1.0), sample("direct", total=1.0)],
        "gateway": [
            sample("gateway", total=1.25, first=0.3),
            sample("gateway", total=1.25, first=0.3),
        ],
    }
    summary = loadtest.build_summary(
        per_target, "direct", {"k": 1}, START, START, "abc123", {"direct": 0, "gateway": 4}
    )
    assert list(summary["overhead"]) == ["gateway"]
    assert summary["overhead"]["gateway"]["total_seconds"]["p95"] == pytest.approx(0.25)
    assert summary["overhead"]["gateway"]["first_event_seconds"]["p50"] == pytest.approx(0.2)
    assert summary["overhead"]["gateway"]["baseline"] == "direct"
    assert "not a per-request pairing" in summary["overhead_note"]
    assert summary["git_commit"] == "abc123"
    assert summary["started_utc"] == "2026-01-01T00:00:00+00:00"
    assert summary["oauth_grants"] == {"direct": 0, "gateway": 4}
    assert loadtest.build_summary(per_target, None, {}, START, START, None, {})["overhead"] == {}


def test_write_raw_csv_has_one_row_per_sample(tmp_path: Path) -> None:
    path = tmp_path / "raw-gateway.csv"
    loadtest.write_raw_csv(path, [sample(), sample(total=None, first=None, reason="timeout")])
    rows = list(csv.DictReader(path.open(encoding="utf-8")))
    assert tuple(rows[0]) == loadtest.CSV_COLUMNS
    assert [r["ok"] for r in rows] == ["True", "False"]
    assert rows[1]["total_seconds"] == ""
    assert rows[1]["failure_reason"] == "timeout"


def test_format_table_prints_targets_overhead_and_the_caveat() -> None:
    per_target = {"direct": [sample("direct")], "gateway": [sample("gateway", total=1.5)]}
    text = loadtest.format_table(
        loadtest.build_summary(per_target, "direct", {}, START, START, None, {})
    )
    assert "direct" in text
    assert "overhead gateway vs direct" in text
    assert "500.0" in text
    assert "difference of two distributions" in text


# --- arguments -----------------------------------------------------------------------------------


def test_parse_targets_pairs_urls_with_authentication() -> None:
    targets = loadtest.parse_targets(
        ["gateway=http://g/mcp", "direct=http://d/mcp"],
        ["gateway=oauth:http://idp/realms/x", "direct=stub-key"],
    )
    assert [(t.name, t.url, t.auth) for t in targets] == [
        ("gateway", "http://g/mcp", "oauth:http://idp/realms/x"),
        ("direct", "http://d/mcp", "stub-key"),
    ]


@pytest.mark.parametrize(
    ("urls", "auths", "message"),
    [
        (["gateway=http://g"], [], "same targets"),
        (["gateway=http://g"], ["other=stub-key"], "same targets"),
        (["gateway=http://g"], ["gateway=basic"], "expected 'stub-key'"),
        (["gateway"], ["gateway=stub-key"], "name=value"),
        (["a=http://g", "a=http://h"], ["a=stub-key"], "twice"),
    ],
)
def test_parse_targets_refuses_bad_pairings(
    urls: list[str], auths: list[str], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        loadtest.parse_targets(urls, auths)


def test_every_measurement_parameter_is_required() -> None:
    with pytest.raises(SystemExit):
        loadtest.build_parser().parse_args(
            ["--target-url", "a=http://x", "--target-auth", "a=stub-key"]
        )


def test_main_refuses_an_unknown_persona(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    argv = [
        "--target-url",
        "d=http://x/mcp",
        "--target-auth",
        "d=stub-key",
        "--personas",
        "nobody",
        "--state-dir",
        str(tmp_path),
        "--concurrency",
        "1",
        "--requests-per-worker",
        "1",
        "--warmup-requests",
        "0",
        "--tool",
        "echo",
        "--arguments-json",
        "{}",
        "--expected-events",
        "1",
        "--timeout-seconds",
        "5",
        "--max-run-seconds",
        "10",
        "--out-dir",
        str(tmp_path),
    ]
    with pytest.raises(SystemExit):
        loadtest.main(argv)
    assert "unknown personas" in capsys.readouterr().err


# --- container stats ---------------------------------------------------------------------------------


def stats_line(name: str, cpu: str, memory: str) -> str:
    stats = {"Name": name, "CPUPerc": cpu, "MemUsage": memory}
    return json.dumps({"ts": "2026-01-01T00:00:00Z", "stats": stats})


def test_stats_units_are_converted_to_mib() -> None:
    assert stats_summary.parse_memory_mib("45.6MiB / 7.7GiB") == pytest.approx(45.6)
    assert stats_summary.parse_memory_mib("1GiB / 2GiB") == 1024.0
    assert stats_summary.parse_memory_mib("1048576B / 1GiB") == 1.0
    assert stats_summary.parse_memory_mib("2MB / 1GB") == pytest.approx(2e6 / 1024**2)
    assert stats_summary.parse_cpu_percent("12.5%") == 12.5
    with pytest.raises(ValueError, match="unknown memory unit"):
        stats_summary.parse_memory_mib("3XiB / 1GiB")


def test_stats_summary_has_peak_and_mean_per_container(tmp_path: Path) -> None:
    source = tmp_path / "stats.jsonl"
    source.write_text(
        "\n".join(
            [
                stats_line("gw-a", "10.0%", "100MiB / 1GiB"),
                stats_line("gw-b", "5.0%", "50MiB / 1GiB"),
                stats_line("gw-a", "30.0%", "300MiB / 1GiB"),
                "",
            ]
        ),
        encoding="utf-8",
    )
    target = tmp_path / "summary.json"
    assert stats_summary.main([str(source), str(target)]) == 0
    containers = json.loads(target.read_text(encoding="utf-8"))["containers"]
    assert containers["gw-a"] == {
        "samples": 2,
        "cpu_percent_peak": 30.0,
        "cpu_percent_mean": 20.0,
        "memory_mib_peak": 300.0,
        "memory_mib_mean": 200.0,
    }
    assert containers["gw-b"]["samples"] == 1


def test_stats_summary_refuses_a_malformed_line_and_an_empty_file(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="line 1"):
        stats_summary.collect(["{not json"])
    with pytest.raises(ValueError, match="line 2"):
        stats_summary.collect([stats_line("a", "1%", "1MiB / 2MiB"), '{"stats": {}}'])
    empty = tmp_path / "empty.jsonl"
    empty.write_text("", encoding="utf-8")
    assert stats_summary.main([str(empty), str(tmp_path / "out.json")]) == 1
