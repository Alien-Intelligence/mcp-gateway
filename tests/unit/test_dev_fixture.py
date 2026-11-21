"""The dev fixture's naming: the shared chain keeps its names, another prefix gets its own."""

import pytest
from dev_fixture import SHARED_NAME_PREFIX, Naming, client_specs, parse_args

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
