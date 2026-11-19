import json
import os
from pathlib import Path
from typing import Any

import pytest

from mcp_gateway.identity import Identity
from mcp_gateway.resolver import Credential, Denied, ResolverError, StaticResolver

UPSTREAMS = ["primary", "other"]


def identity(
    subject: str = "u1",
    email: str | None = None,
    organization: str | None = None,
    groups: tuple[str, ...] = (),
) -> Identity:
    return Identity(
        subject=subject, email=email, organization=organization, groups=groups, claims={}
    )


def entry(
    match: dict[str, str], key_file: str, labels: dict[str, str] | None = None
) -> dict[str, Any]:
    return {
        "match": match,
        "api_key_file": key_file,
        "upstream": "primary",
        "labels": labels if labels is not None else {},
    }


def write_mapping(directory: Path, entries: list[dict[str, Any]]) -> Path:
    path = directory / "mapping.json"
    path.write_text(json.dumps({"entries": entries}))
    return path


def write_key(directory: Path, name: str, value: str) -> None:
    (directory / name).write_text(value)


def bump_mtime(path: Path) -> None:
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 5_000_000_000))


@pytest.fixture
def state(tmp_path: Path) -> Path:
    for name in ("subj", "mail", "org", "grp-a", "grp-b"):
        write_key(tmp_path, f"{name}.key", f"fake-alien-key-{name}\n")
    return tmp_path


async def test_each_match_kind_resolves_its_own_key(state: Path) -> None:
    path = write_mapping(
        state,
        [
            entry({"subject": "sub-1"}, "subj.key", {"kind": "subject"}),
            entry({"email": "a@x.example"}, "mail.key"),
            entry({"organization": "org-1"}, "org.key"),
            entry({"group": "g-a"}, "grp-a.key"),
        ],
    )
    resolver = StaticResolver(path, UPSTREAMS)

    async def key_of(ident: Identity) -> str:
        outcome = await resolver.resolve(ident)
        assert isinstance(outcome, Credential)
        return outcome.api_key.get_secret_value()

    assert await key_of(identity("sub-1")) == "fake-alien-key-subj"
    assert await key_of(identity("zz", email="a@x.example")) == "fake-alien-key-mail"
    assert await key_of(identity("zz", organization="org-1")) == "fake-alien-key-org"
    assert await key_of(identity("zz", groups=("other", "g-a"))) == "fake-alien-key-grp-a"


async def test_credential_carries_upstream_and_labels(state: Path) -> None:
    path = write_mapping(state, [entry({"subject": "s"}, "subj.key", {"client": "client-x"})])
    outcome = await StaticResolver(path, UPSTREAMS).resolve(identity("s"))
    assert isinstance(outcome, Credential)
    assert outcome.upstream == "primary"
    assert dict(outcome.labels) == {"client": "client-x"}
    assert "fake-alien-key-subj" not in repr(outcome)


async def test_precedence_subject_then_email_then_organization_then_group(state: Path) -> None:
    path = write_mapping(
        state,
        [
            entry({"group": "g"}, "grp-a.key"),
            entry({"organization": "o"}, "org.key"),
            entry({"email": "a@x.example"}, "mail.key"),
            entry({"subject": "s"}, "subj.key"),
        ],
    )
    resolver = StaticResolver(path, UPSTREAMS)
    full = identity("s", email="a@x.example", organization="o", groups=("g",))

    async def key(ident: Identity) -> str:
        outcome = await resolver.resolve(ident)
        assert isinstance(outcome, Credential)
        return outcome.api_key.get_secret_value()

    assert await key(full) == "fake-alien-key-subj"
    assert (
        await key(identity("x", email="a@x.example", organization="o", groups=("g",))) == "fake-alien-key-mail"
    )
    assert await key(identity("x", organization="o", groups=("g",))) == "fake-alien-key-org"
    assert await key(identity("x", groups=("g",))) == "fake-alien-key-grp-a"


async def test_email_match_is_case_insensitive(state: Path) -> None:
    path = write_mapping(state, [entry({"email": "Alice@Client-X.example"}, "mail.key")])
    outcome = await StaticResolver(path, UPSTREAMS).resolve(
        identity(email="alice@CLIENT-x.EXAMPLE")
    )
    assert isinstance(outcome, Credential)


async def test_subject_and_organization_matches_are_case_sensitive(state: Path) -> None:
    path = write_mapping(
        state,
        [entry({"subject": "Sub"}, "subj.key"), entry({"organization": "Org"}, "org.key")],
    )
    resolver = StaticResolver(path, UPSTREAMS)
    assert await resolver.resolve(identity("sub")) == Denied("no_mapping")
    assert await resolver.resolve(identity("x", organization="org")) == Denied("no_mapping")


async def test_several_matching_group_entries_are_ambiguous(state: Path) -> None:
    path = write_mapping(
        state, [entry({"group": "a"}, "grp-a.key"), entry({"group": "b"}, "grp-b.key")]
    )
    outcome = await StaticResolver(path, UPSTREAMS).resolve(identity(groups=("a", "b")))
    assert outcome == Denied("ambiguous_mapping")


async def test_one_group_entry_matched_by_two_groups_is_not_ambiguous(state: Path) -> None:
    path = write_mapping(
        state, [entry({"group": "a"}, "grp-a.key"), entry({"group": "b"}, "grp-b.key")]
    )
    outcome = await StaticResolver(path, UPSTREAMS).resolve(identity(groups=("a", "a", "zzz")))
    assert isinstance(outcome, Credential)


async def test_no_match_is_denied_without_fallback(state: Path) -> None:
    path = write_mapping(state, [entry({"subject": "s"}, "subj.key")])
    outcome = await StaticResolver(path, UPSTREAMS).resolve(identity("someone-else"))
    assert outcome == Denied("no_mapping")


async def test_missing_key_file_raises_without_leaking_anything(
    state: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = write_mapping(state, [entry({"subject": "s"}, "absent.key")])
    resolver = StaticResolver(path, UPSTREAMS)
    with pytest.raises(ResolverError, match="entry 0"):
        await resolver.resolve(identity("s"))
    assert "entry 0" in caplog.text


@pytest.mark.parametrize("content", ["", "  \n", "oat bad key", "oat\x00key"])
async def test_empty_or_malformed_key_file_raises(state: Path, content: str) -> None:
    write_key(state, "bad.key", content)
    path = write_mapping(state, [entry({"subject": "s"}, "bad.key")])
    with pytest.raises(ResolverError):
        await StaticResolver(path, UPSTREAMS).resolve(identity("s"))


async def test_key_is_stripped(state: Path) -> None:
    write_key(state, "pad.key", "  fake-alien-key-padded \n\n")
    path = write_mapping(state, [entry({"subject": "s"}, "pad.key")])
    outcome = await StaticResolver(path, UPSTREAMS).resolve(identity("s"))
    assert isinstance(outcome, Credential)
    assert outcome.api_key.get_secret_value() == "fake-alien-key-padded"


async def test_relative_key_path_resolves_against_the_mapping_directory(tmp_path: Path) -> None:
    nested = tmp_path / "state"
    (nested / "keys").mkdir(parents=True)
    (nested / "keys" / "k").write_text("fake-alien-key-nested")
    path = write_mapping(nested, [entry({"subject": "s"}, "keys/k")])
    outcome = await StaticResolver(path, UPSTREAMS).resolve(identity("s"))
    assert isinstance(outcome, Credential)
    assert outcome.api_key.get_secret_value() == "fake-alien-key-nested"


async def test_absolute_key_path_is_allowed(tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "abs.key").write_text("fake-alien-key-abs")
    mapping_dir = tmp_path / "mapping"
    mapping_dir.mkdir()
    path = write_mapping(mapping_dir, [entry({"subject": "s"}, str(elsewhere / "abs.key"))])
    outcome = await StaticResolver(path, UPSTREAMS).resolve(identity("s"))
    assert isinstance(outcome, Credential)
    assert outcome.api_key.get_secret_value() == "fake-alien-key-abs"


async def test_rotated_and_revoked_keys_are_picked_up_without_reload(state: Path) -> None:
    path = write_mapping(state, [entry({"subject": "s"}, "subj.key")])
    resolver = StaticResolver(path, UPSTREAMS)
    write_key(state, "subj.key", "fake-alien-key-rotated")
    rotated = await resolver.resolve(identity("s"))
    assert isinstance(rotated, Credential)
    assert rotated.api_key.get_secret_value() == "fake-alien-key-rotated"
    (state / "subj.key").unlink()
    with pytest.raises(ResolverError):
        await resolver.resolve(identity("s"))


async def test_mapping_file_is_reloaded_when_its_mtime_changes(state: Path) -> None:
    path = write_mapping(state, [entry({"subject": "s"}, "subj.key")])
    resolver = StaticResolver(path, UPSTREAMS)
    assert isinstance(await resolver.resolve(identity("s")), Credential)
    write_mapping(state, [entry({"subject": "t"}, "subj.key")])
    bump_mtime(path)
    assert await resolver.resolve(identity("s")) == Denied("no_mapping")
    assert isinstance(await resolver.resolve(identity("t")), Credential)


async def test_bad_reload_serves_nothing_and_recovers_on_fix(
    state: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = write_mapping(state, [entry({"subject": "s"}, "subj.key")])
    resolver = StaticResolver(path, UPSTREAMS)
    path.write_text('{"entries": [')
    bump_mtime(path)
    with pytest.raises(ResolverError, match="mapping file unusable"):
        await resolver.resolve(identity("s"))
    with pytest.raises(ResolverError):
        await resolver.resolve(identity("s"))
    assert "mapping file rejected" in caplog.text
    write_mapping(state, [entry({"subject": "s"}, "subj.key")])
    assert isinstance(await resolver.resolve(identity("s")), Credential)


@pytest.mark.parametrize(
    "document",
    [
        {"entries": [{"match": {}, "api_key_file": "k", "upstream": "primary", "labels": {}}]},
        {
            "entries": [
                {
                    "match": {"email": "a", "subject": "b"},
                    "api_key_file": "k",
                    "upstream": "primary",
                    "labels": {},
                }
            ]
        },
        {
            "entries": [
                {"match": {"color": "red"}, "api_key_file": "k", "upstream": "primary", "labels": {}}
            ]
        },
        {
            "entries": [
                {"match": {"email": " "}, "api_key_file": "k", "upstream": "primary", "labels": {}}
            ]
        },
        {
            "entries": [
                {"match": {"email": "a"}, "api_key_file": "k", "upstream": "nope", "labels": {}}
            ]
        },
        {
            "entries": [
                {"match": {"email": "a"}, "api_key_file": "", "upstream": "primary", "labels": {}}
            ]
        },
        {"entries": [{"match": {"email": "a"}, "api_key_file": "k", "upstream": "primary"}]},
        {"entries": [], "extra": 1},
        {"nope": []},
    ],
)
def test_invalid_mapping_documents_fail_at_load(tmp_path: Path, document: dict[str, Any]) -> None:
    path = tmp_path / "mapping.json"
    path.write_text(json.dumps(document))
    with pytest.raises(ResolverError):
        StaticResolver(path, UPSTREAMS)


def test_duplicate_match_keys_fail_at_load(state: Path) -> None:
    path = write_mapping(
        state,
        [entry({"email": "A@x.example"}, "mail.key"), entry({"email": "a@X.example"}, "subj.key")],
    )
    with pytest.raises(ResolverError, match="duplicate email"):
        StaticResolver(path, UPSTREAMS)


def test_missing_mapping_file_fails_at_load(tmp_path: Path) -> None:
    with pytest.raises(ResolverError):
        StaticResolver(tmp_path / "absent.json", UPSTREAMS)


async def test_invalidate_is_a_harmless_noop(state: Path) -> None:
    path = write_mapping(state, [entry({"subject": "s"}, "subj.key")])
    resolver = StaticResolver(path, UPSTREAMS)
    await resolver.invalidate(identity("s"))
    assert isinstance(await resolver.resolve(identity("s")), Credential)
