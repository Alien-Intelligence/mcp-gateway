"""The setup tutorial's examples must stay valid.

Every configuration the tutorial shows is loaded with the gateway's own loader, the static mapping
with the static resolver, every webhook answer with the webhook resolver, and the webhook example
runs against the real ``WebhookResolver``. A fence of JSON in the tutorial must say what it is, so
that a new example cannot be added without being checked.

Markers (an HTML comment on the line before the fence):

* ``<!-- config-fragment: a.b [base=ha] -->``: the value of key ``a.b`` of ``examples/config-single.json``
  (or of ``config-ha.json`` with ``base=ha``)
* ``<!-- static-mapping -->``: a mapping file of the ``static`` resolver
* ``<!-- webhook-answer -->``: an answer of a webhook
* ``<!-- not-config -->``: anything else (an API body, a request, a log line)
"""

import asyncio
import copy
import importlib.util
import json
import re
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from mcp_gateway.config import parse_config
from mcp_gateway.identity import Identity
from mcp_gateway.resolver import Credential, Denied, ResolverError, StaticResolver, WebhookResolver

from .conftest import FAKE_API_KEY
from .test_webhook_resolver import SECRET, UPSTREAMS, identity, webhook_config
from .webhook_server import WebhookServer, json_responder

TUTORIAL = Path(__file__).resolve().parents[2] / "docs" / "tutorial"
EXAMPLES = TUTORIAL / "examples"
CHAPTERS = sorted((TUTORIAL / "en").glob("*.md"))
MARKER = re.compile(
    r"<!-- (config-fragment: [\w.]+(?: base=ha)?|static-mapping|webhook-answer|not-config) -->"
)


@dataclass(frozen=True)
class Fence:
    chapter: str
    line: int
    marker: str | None
    text: str


def json_fences(path: Path) -> Iterator[Fence]:
    lines = path.read_text(encoding="utf-8").splitlines()
    for index, line in enumerate(lines):
        if line.strip() != "```json":
            continue
        end = next(i for i in range(index + 1, len(lines)) if lines[i].strip() == "```")
        found = MARKER.fullmatch(lines[index - 1].strip()) if index else None
        yield Fence(
            path.name,
            index + 1,
            found.group(1) if found else None,
            "\n".join(lines[index + 1 : end]),
        )


ALL_FENCES = [fence for chapter in CHAPTERS for fence in json_fences(chapter)]


def load_example(name: str) -> dict[str, Any]:
    return json.loads((EXAMPLES / name).read_text(encoding="utf-8"))


def fence_id(fence: Fence) -> str:
    return f"{fence.chapter}:{fence.line}"


def test_the_tutorial_has_every_chapter() -> None:
    assert [chapter.name for chapter in CHAPTERS] == [
        "01-what-it-is.md",
        "02-prerequisites.md",
        "03-alien-side.md",
        "04-resolvers.md",
        "05-deploying.md",
        "06-oauth-server.md",
        "07-clients.md",
        "08-operations.md",
        "09-troubleshooting.md",
        "README.md",
    ]


@pytest.mark.parametrize("name", sorted(path.name for path in EXAMPLES.glob("config-*.json")))
def test_every_example_configuration_is_accepted_by_the_loader(name: str) -> None:
    parse_config((EXAMPLES / name).read_text(encoding="utf-8"))


def test_the_examples_cover_a_single_replica_and_a_shared_state_pair() -> None:
    single = parse_config((EXAMPLES / "config-single.json").read_text(encoding="utf-8"))
    pair = parse_config((EXAMPLES / "config-ha.json").read_text(encoding="utf-8"))
    assert single.state.type == "memory"
    assert pair.state.type == "redis"
    assert pair.resolver.type == "webhook"


@pytest.mark.parametrize("fence", ALL_FENCES, ids=fence_id)
def test_every_json_fence_in_the_tutorial_says_what_it_is(fence: Fence) -> None:
    assert fence.marker is not None, (
        f"{fence.chapter}:{fence.line}: a json fence needs a marker comment on the line before it"
    )
    json.loads(fence.text)


@pytest.mark.parametrize(
    "fence",
    [f for f in ALL_FENCES if f.marker is not None and f.marker.startswith("config-fragment")],
    ids=fence_id,
)
def test_a_configuration_fragment_in_the_text_is_accepted_in_place(fence: Fence) -> None:
    assert fence.marker is not None
    _, _, spec = fence.marker.partition(": ")
    path, _, option = spec.partition(" ")
    base = copy.deepcopy(
        load_example("config-ha.json" if option == "base=ha" else "config-single.json")
    )
    target = base
    *parents, last = path.split(".")
    for part in parents:
        target = target[part]
    target[last] = json.loads(fence.text)
    parse_config(json.dumps(base))


def test_the_tutorial_uses_every_kind_of_checked_fence() -> None:
    markers = {f.marker for f in ALL_FENCES}
    assert {"static-mapping", "webhook-answer", "not-config"} <= markers
    assert any(m is not None and m.startswith("config-fragment: resolver") for m in markers)


@pytest.mark.parametrize(
    "text",
    [
        *(f.text for f in ALL_FENCES if f.marker == "static-mapping"),
        (EXAMPLES / "static-mapping.json").read_text(encoding="utf-8"),
    ],
)
def test_a_static_mapping_example_is_accepted_by_the_static_resolver(
    text: str, tmp_path: Path
) -> None:
    mapping = tmp_path / "mapping.json"
    mapping.write_text(text, encoding="utf-8")
    for entry in json.loads(text)["entries"]:
        key_file = tmp_path / entry["api_key_file"]
        key_file.parent.mkdir(parents=True, exist_ok=True)
        key_file.write_text(FAKE_API_KEY + "\n", encoding="utf-8")
    resolver = StaticResolver(mapping, ["main"], email_matching=True)
    first = json.loads(text)["entries"][0]["match"]
    ((kind, value),) = first.items()
    who = Identity(
        subject=value if kind == "subject" else "someone",
        email=value if kind == "email" else None,
        organization=value if kind == "organization" else None,
        groups=(value,) if kind == "group" else (),
        claims={},
    )
    outcome = asyncio.run(resolver.resolve(who))
    assert isinstance(outcome, Credential)


@pytest.mark.parametrize("answer", [f.text for f in ALL_FENCES if f.marker == "webhook-answer"])
async def test_a_webhook_answer_in_the_text_is_accepted_by_the_webhook_resolver(
    answer: str, tmp_path: Path
) -> None:
    secret = tmp_path / "hmac"
    secret.write_text(SECRET)
    server = WebhookServer(secret=SECRET.encode())
    server.responders = [json_responder(json.loads(answer))]
    await server.start()
    resolver = WebhookResolver(
        webhook_config(server.url(), {"type": "hmac", "secret_file": str(secret)}), UPSTREAMS
    )
    try:
        outcome = await resolver.resolve(identity())
        assert isinstance(outcome, Credential | Denied)
    finally:
        await resolver.aclose()
        await server.stop()


# --- the webhook example, against the real WebhookResolver ---------------------------------


def load_module(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, EXAMPLES / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


webhook = load_module("webhook_resolver")
probe = load_module("resolve_probe")


@pytest.fixture
def example_settings(tmp_path: Path) -> Any:
    keys = tmp_path / "keys"
    keys.mkdir()
    (keys / "alice.key").write_text(FAKE_API_KEY + "\n")
    (keys / "bob.key").write_text(FAKE_API_KEY + "\n")
    (keys / "keys.json").write_text((EXAMPLES / "webhook-keys.json").read_text())
    secret = tmp_path / "hmac"
    secret.write_text(SECRET + "\n")
    return webhook.load_settings(
        {
            "WEBHOOK_HMAC_SECRET_FILE": str(secret),
            "WEBHOOK_KEYS_FILE": str(keys / "keys.json"),
            "WEBHOOK_TIMESTAMP_WINDOW_SECONDS": "30",
            "WEBHOOK_HOST": "127.0.0.1",
            "WEBHOOK_PORT": "0",
        }
    )


@pytest.fixture
def example_url(example_settings: Any) -> Iterator[str]:
    server = webhook.make_server(example_settings)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/resolve"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=10)


async def test_the_webhook_example_allows_a_mapped_user_and_denies_the_others(
    example_url: str, tmp_path: Path
) -> None:
    secret = tmp_path / "gateway-hmac"
    secret.write_text(SECRET)
    resolver = WebhookResolver(
        webhook_config(example_url, {"type": "hmac", "secret_file": str(secret)}), UPSTREAMS
    )
    try:
        allowed = await resolver.resolve(identity(email="Alice@Example.com"))
        assert isinstance(allowed, Credential)
        assert allowed.api_key.get_secret_value() == FAKE_API_KEY
        assert allowed.upstream == "main"
        assert allowed.ttl_seconds == 300
        assert allowed.labels == {"client": "example-publisher"}
        assert await resolver.resolve(identity(email="nobody@example.com")) == Denied("no_mapping")
        assert await resolver.resolve(identity(email=None)) == Denied("no_mapping")
    finally:
        await resolver.aclose()


async def test_the_webhook_example_refuses_a_wrong_secret(example_url: str, tmp_path: Path) -> None:
    secret = tmp_path / "wrong"
    secret.write_text("not-the-secret")
    resolver = WebhookResolver(
        webhook_config(example_url, {"type": "hmac", "secret_file": str(secret)}), UPSTREAMS
    )
    try:
        with pytest.raises(ResolverError, match="status 401"):
            await resolver.resolve(identity())
    finally:
        await resolver.aclose()


def test_the_webhook_example_refuses_a_replayed_request(example_url: str) -> None:
    body = json.dumps(
        {"subject": "s", "email": "alice@example.com", "organization": None, "groups": []},
        separators=(",", ":"),
    ).encode()
    headers = probe.signed_headers(SECRET.encode(), body, time.time())

    def post() -> int:
        request = urllib.request.Request(example_url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status
        except urllib.error.HTTPError as exc:
            return exc.code

    assert (post(), post()) == (200, 401)


def test_the_probe_reports_an_allow_without_printing_the_key(
    example_url: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    secret = tmp_path / "probe-hmac"
    secret.write_text(SECRET)
    status = probe.main(
        ["--url", example_url, "--secret-file", str(secret), "--email", "alice@example.com"]
    )
    out = capsys.readouterr().out
    assert status == 0
    assert "<redacted>" in out
    assert FAKE_API_KEY not in out


def test_the_probe_exits_nonzero_for_a_refused_signature(
    example_url: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    secret = tmp_path / "bad"
    secret.write_text("not-the-secret")
    status = probe.main(["--url", example_url, "--secret-file", str(secret), "--email", "a@b.c"])
    assert status == 1
    assert "HTTP 401" in capsys.readouterr().out
