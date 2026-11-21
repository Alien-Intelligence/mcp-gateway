"""The dev fixture: its naming (the shared chain keeps its names, another prefix gets its own)
and the order of the sponsor side against a fake that enforces the backend's rules."""

import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest
from dev_fixture import (
    SHARED_NAME_PREFIX,
    AlienApiError,
    ApiResult,
    Config,
    FixtureApi,
    Naming,
    Tally,
    client_specs,
    parse_args,
    prepare_group,
)

REQUIRED = [
    "--sponsor-key-file",
    "sponsor.key",
    "--api-base",
    "https://api.example.com",
    "--mcp-url",
    "https://mcp.example.com/mcp",
    "--sponsor-org-id",
    "3",
    "--cluster-id",
    "12",
    "--dataset-id",
    "345",
    "--state-dir",
    "state",
]


def test_the_default_prefix_keeps_the_names_the_shared_fixture_created() -> None:
    # Organizations and users cannot be deleted on Alien: a renamed object would be created
    # again next to the existing one.
    naming = parse_args(REQUIRED).naming
    assert naming.prefix == SHARED_NAME_PREFIX
    assert naming.grant == "mcp-gateway-e2e-dataset-read"
    assert naming.group == "mcp-gateway-e2e-sponsored"
    assert naming.token == "mcp-gateway-e2e"
    specs = {s.persona: s for s in client_specs(naming)}
    assert specs["alice"].org_name == "mcp-gateway-e2e-managed-client-x"
    assert specs["alice"].alien_email == "alice@managed-client-x.test"
    assert specs["bob"].org_name == "mcp-gateway-e2e-managed-client-y"
    assert specs["bob"].alien_email == "bob@managed-client-y.test"
    assert specs["carol"].alien_email == "carol@managed-client-x.test"


def test_another_prefix_names_every_object_and_address_apart() -> None:
    naming = parse_args([*REQUIRED, "--name-prefix", "fixture-check-1"]).naming
    shared = Naming(SHARED_NAME_PREFIX)
    specs = client_specs(naming)
    shared_specs = client_specs(shared)
    assert naming.grant == "fixture-check-1-dataset-read"
    assert naming.group == "fixture-check-1-sponsored"
    assert naming.token == "fixture-check-1"
    assert {s.org_name for s in specs}.isdisjoint({s.org_name for s in shared_specs})
    # Alien e-mails are unique across the platform.
    assert {s.alien_email for s in specs}.isdisjoint({s.alien_email for s in shared_specs})
    assert [s.alien_email for s in specs] == [
        "alice@fixture-check-1.managed-client-x.test",
        "bob@fixture-check-1.managed-client-y.test",
        "carol@fixture-check-1.managed-client-x.test",
    ]
    # The identity-provider side does not change: the dev stack's IdP users stay the same.
    assert [s.email for s in specs] == [s.email for s in shared_specs]


@pytest.mark.parametrize("prefix", ["", "Upper", "-leading", "has space", "a" * 41, "dot.ted"])
def test_an_invalid_prefix_is_refused_before_any_call(prefix: str) -> None:
    with pytest.raises(SystemExit) as refused:
        parse_args([*REQUIRED, "--name-prefix", prefix])
    assert refused.value.code == 2


class FakeSponsorSide(FixtureApi):
    """The group, grant and managed-organization routes of the backend, with its two ordering
    rules: a group is sponsored only once it holds a grant, and a managed organization is
    created only in a sponsored group (409 GROUP_NOT_SPONSORED otherwise)."""

    def __init__(self) -> None:  # no HTTP client: every call is answered here
        self.grants: list[dict[str, Any]] = []
        self.groups: dict[int, dict[str, Any]] = {}
        self.orgs: dict[int, dict[str, Any]] = {}
        self.calls: list[str] = []
        self._next_id = 100

    def close(self) -> None:
        pass

    def _new_id(self) -> int:
        self._next_id += 1
        return self._next_id

    def request(
        self,
        method: str,
        path: str,
        *,
        org_id: int | None = None,
        json_body: Mapping[str, Any] | None = None,
        params: Mapping[str, Any] | None = None,
        handled: Sequence[int] = (),
    ) -> ApiResult:
        self.calls.append(f"{method} {path}")
        body = dict(json_body or {})
        if (method, path) == ("GET", "/grants"):
            return ApiResult(200, list(self.grants), None)
        if (method, path) == ("POST", "/grants"):
            grant = {"id": self._new_id(), "name": body["name"]}
            self.grants.append(grant)
            return ApiResult(200, grant, None)
        if (method, path) == ("GET", "/groups"):
            return ApiResult(200, [dict(g) for g in self.groups.values()], None)
        if (method, path) == ("POST", "/groups"):
            group = {"id": self._new_id(), "name": body["name"], "isSponsored": False}
            self.groups[group["id"]] = {**group, "grants": [], "orgs": []}
            return ApiResult(200, {"status": "created", "group": group}, None)
        match = re.fullmatch(r"/groups/(\d+)(/.*)?", path)
        assert match is not None, f"unexpected call {method} {path}"
        group = self.groups[int(match.group(1))]
        tail = match.group(2) or ""
        if (method, tail) == ("GET", ""):
            return ApiResult(200, {"id": group["id"], "isSponsored": group["isSponsored"]}, None)
        if (method, tail) == ("PATCH", ""):
            if body.get("isSponsored") is True and not group["grants"]:
                raise AlienApiError(409, method, path, "a sponsored group needs a cluster grant")
            group["isSponsored"] = body["isSponsored"]
            return ApiResult(200, {"status": "updated"}, None)
        if (method, tail) == ("GET", "/grants"):
            return ApiResult(200, [{"id": g} for g in group["grants"]], None)
        if (method, tail) == ("POST", "/grants"):
            group["grants"].extend(body["grantIds"])
            return ApiResult(200, {"attached": body["grantIds"]}, None)
        if (method, tail) == ("GET", "/organizations"):
            return ApiResult(200, [self.orgs[o] for o in group["orgs"]], None)
        if (method, tail) == ("POST", "/organizations"):
            group["orgs"].extend(body["organizationIds"])
            return ApiResult(200, {"attached": body["organizationIds"]}, None)
        if (method, tail) == ("POST", "/organizations/managed"):
            if not group["isSponsored"]:
                raise AlienApiError(409, method, path, '{"error":{"name":"GROUP_NOT_SPONSORED"}}')
            org = {"id": self._new_id(), "name": body["name"], "accountType": "managed"}
            self.orgs[org["id"]] = org
            group["orgs"].append(org["id"])
            return ApiResult(200, org, None)
        raise AssertionError(f"unexpected call {method} {path}")


def fixture_config(tmp_path: Path) -> Config:
    return Config(
        sponsor_key_file=tmp_path / "sponsor.key",
        api_base="https://api.example.com",
        mcp_url="https://mcp.example.com/mcp",
        sponsor_org_id=3,
        cluster_id=12,
        dataset_id=345,
        state_dir=tmp_path / "state",
        rotate=False,
        probe_forged_headers=False,
        naming=Naming("fixture-check-1"),
    )


def test_a_fresh_chain_is_sponsored_before_its_managed_organizations(tmp_path: Path) -> None:
    api = FakeSponsorSide()
    cfg = fixture_config(tmp_path)
    tally = Tally()

    group_id, grant_id, orgs = prepare_group(api, cfg, client_specs(cfg.naming), tally)

    group = api.groups[group_id]
    assert group["isSponsored"] is True
    assert group["grants"] == [grant_id]
    # alice and carol share client-x: two organizations for three personas, both attached once.
    assert [o["name"] for o in orgs] == [
        "fixture-check-1-managed-client-x",
        "fixture-check-1-managed-client-y",
        "fixture-check-1-managed-client-x",
    ]
    assert sorted(group["orgs"]) == sorted({o["id"] for o in orgs})
    sponsored_at = api.calls.index(f"PATCH /groups/{group_id}")
    first_org_at = api.calls.index(f"POST /groups/{group_id}/organizations/managed")
    assert api.calls.index(f"POST /groups/{group_id}/grants") < sponsored_at < first_org_at


def test_a_second_run_creates_nothing(tmp_path: Path) -> None:
    api = FakeSponsorSide()
    cfg = fixture_config(tmp_path)
    first = prepare_group(api, cfg, client_specs(cfg.naming), Tally())

    tally = Tally()
    again = prepare_group(api, cfg, client_specs(cfg.naming), tally)

    assert again == first
    assert tally.created == []


def test_a_run_that_stopped_before_sponsoring_is_completed(tmp_path: Path) -> None:
    # What a run of the old order left behind: the grant and the group exist, the group holds no
    # grant and is not sponsored.
    api = FakeSponsorSide()
    cfg = fixture_config(tmp_path)
    grant = api.request("POST", "/grants", json_body={"name": cfg.naming.grant}).data
    created = api.request("POST", "/groups", json_body={"name": cfg.naming.group}).data
    tally = Tally()

    group_id, grant_id, _ = prepare_group(api, cfg, client_specs(cfg.naming), tally)

    assert (group_id, grant_id) == (created["group"]["id"], grant["id"])
    assert api.groups[group_id]["isSponsored"] is True
    assert (len(api.grants), len(api.groups)) == (1, 1)
