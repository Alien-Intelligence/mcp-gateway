"""The deployment examples in deploy/ stay valid and agree with each other.

The compose configurations are loaded by the gateway's own loader, the realm template is rendered by
its own script, and the two are compared where they must match: the audience, the advertised scopes,
the identity claims and the secret file paths.
"""

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from mcp_gateway.config import parse_config

DEPLOY = Path(__file__).resolve().parents[2] / "deploy"
COMPOSE_DIR = DEPLOY / "compose"
KEYCLOAK_DIR = DEPLOY / "keycloak"
RENDER = KEYCLOAK_DIR / "render.sh"
CONFIG_EXAMPLES = sorted(COMPOSE_DIR.glob("config.*.example.json"))
SECRETS_MOUNT = "/run/mcp-gateway/secrets"
CONFIG_TARGET = "/etc/mcp-gateway/config.json"
PUBLIC_URL = "https://mcp.example.com/mcp"
RENDER_TIMEOUT_SECONDS = 30
PLACEHOLDER = re.compile(r"\$\{[A-Z][A-Z0-9_]*\}")


def _render(template: Path | None = None, **variables: str) -> subprocess.CompletedProcess[str]:
    command = ["bash", str(RENDER)] + ([str(template)] if template is not None else [])
    return subprocess.run(
        command,
        env={"PATH": os.environ["PATH"], **variables},
        capture_output=True,
        text=True,
        timeout=RENDER_TIMEOUT_SECONDS,
        check=False,
    )


def _rendered_realm(public_url: str = PUBLIC_URL) -> dict[str, Any]:
    result = _render(REALM_NAME="example-publisher", GATEWAY_PUBLIC_URL=public_url)
    assert result.returncode == 0, result.stderr
    realm = json.loads(result.stdout)
    assert isinstance(realm, dict)
    return realm


def _client_scope(realm: dict[str, Any], name: str) -> dict[str, Any]:
    (scope,) = [s for s in realm["clientScopes"] if s["name"] == name]
    return scope


def _anonymous_policy(realm: dict[str, Any], provider_id: str) -> dict[str, Any]:
    policies = realm["components"][
        "org.keycloak.services.clientregistration.policy.ClientRegistrationPolicy"
    ]
    (policy,) = [
        p for p in policies if p["providerId"] == provider_id and p["subType"] == "anonymous"
    ]
    return policy


def _file_paths(node: object) -> list[str]:
    """Every value of a ``*_file`` key, at any depth."""
    if isinstance(node, dict):
        found: list[str] = []
        for key, value in node.items():
            if key.endswith("_file") and isinstance(value, str):
                found.append(value)
            found.extend(_file_paths(value))
        return found
    if isinstance(node, list):
        return [path for item in node for path in _file_paths(item)]
    return []


def _compose() -> dict[str, Any]:
    document = yaml.safe_load((COMPOSE_DIR / "compose.yaml").read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    return document


# ---------------------------------------------------------------------------------------------
# Compose configurations
# ---------------------------------------------------------------------------------------------


def test_both_resolver_examples_exist() -> None:
    assert [p.name for p in CONFIG_EXAMPLES] == [
        "config.static.example.json",
        "config.store-redis.example.json",
    ]


@pytest.mark.parametrize("path", CONFIG_EXAMPLES, ids=lambda p: p.name)
def test_config_example_is_accepted_by_the_gateway_loader(path: Path) -> None:
    config = parse_config(path.read_text(encoding="utf-8"))
    assert config.version == 1
    assert config.state.type == "redis"
    assert config.listen.port == 8000
    expected = "static" if "static" in path.name else "store_redis"
    assert config.resolver.type == expected


@pytest.mark.parametrize("path", CONFIG_EXAMPLES, ids=lambda p: p.name)
def test_every_secret_file_of_a_config_example_is_under_the_secrets_mount(path: Path) -> None:
    paths = _file_paths(json.loads(path.read_text(encoding="utf-8")))
    assert paths, "the example names no secret file"
    assert all(p.startswith(SECRETS_MOUNT + "/") for p in paths), paths


def test_the_mapping_example_names_the_upstream_of_the_static_config() -> None:
    config = parse_config((COMPOSE_DIR / "config.static.example.json").read_text(encoding="utf-8"))
    mapping = json.loads((COMPOSE_DIR / "mapping.example.json").read_text(encoding="utf-8"))
    assert {entry["upstream"] for entry in mapping["entries"]} <= set(config.upstreams)


# ---------------------------------------------------------------------------------------------
# Compose file
# ---------------------------------------------------------------------------------------------


def test_the_gateway_service_reads_the_config_and_secrets_where_the_examples_expect() -> None:
    gateway = _compose()["services"]["gateway"]
    assert gateway["configs"] == [{"source": "gateway-config", "target": CONFIG_TARGET}]
    assert CONFIG_TARGET in gateway["entrypoint"][-1]
    assert "exec mcp-gateway" in gateway["entrypoint"][-1]
    mounts = [v for v in gateway["volumes"] if v.split(":")[-2] == SECRETS_MOUNT]
    assert len(mounts) == 1 and mounts[0].endswith(":ro"), gateway["volumes"]
    (port,) = gateway["ports"]
    assert port.endswith(":8000")


@pytest.mark.parametrize("name", ["gateway", "redis"])
def test_each_service_is_hardened(name: str) -> None:
    service = _compose()["services"][name]
    assert service["read_only"] is True
    assert service["cap_drop"] == ["ALL"]
    assert "no-new-privileges:true" in service["security_opt"]
    assert service["restart"] == "unless-stopped"
    limits = service["deploy"]["resources"]["limits"]
    assert limits["cpus"] and limits["memory"]
    assert service["healthcheck"]["test"]


def test_the_gateway_is_healthy_only_when_ready() -> None:
    gateway = _compose()["services"]["gateway"]
    assert any("/readyz" in part for part in gateway["healthcheck"]["test"])
    assert gateway["depends_on"]["redis"]["condition"] == "service_healthy"


def test_redis_is_reachable_by_the_gateway_only() -> None:
    compose = _compose()
    redis = compose["services"]["redis"]
    assert "ports" not in redis
    assert redis["networks"] == ["state"]
    assert compose["networks"]["state"]["internal"] is True
    assert "@sha256:" in redis["image"]
    command = redis["command"]
    assert command[1] == "/run/secrets/redis-conf"
    assert command[command.index("--maxmemory-policy") + 1] == "noeviction"


# ---------------------------------------------------------------------------------------------
# Realm template and render.sh
# ---------------------------------------------------------------------------------------------


def test_the_rendered_realm_has_no_placeholder_left_and_no_users_or_secrets() -> None:
    realm = _rendered_realm()
    text = json.dumps(realm)
    assert not PLACEHOLDER.search(text)
    assert realm["realm"] == "example-publisher"
    assert "users" not in realm and "clients" not in realm
    assert not re.search(r'"(secret|password|credentials)"', text)


def test_the_gateway_audience_is_a_default_scope_of_the_realm() -> None:
    realm = _rendered_realm()
    scope = _client_scope(realm, "mcp-gateway")
    audiences = [
        m["config"]["included.custom.audience"]
        for m in scope["protocolMappers"]
        if m["protocolMapper"] == "oidc-audience-mapper"
    ]
    assert audiences == [PUBLIC_URL]
    assert "mcp-gateway" in realm["defaultDefaultClientScopes"]


def test_the_realm_lifetimes_are_the_documented_production_values() -> None:
    realm = _rendered_realm()
    assert realm["accessTokenLifespan"] == 300
    assert realm["ssoSessionIdleTimeout"] == 7 * 86400
    assert realm["ssoSessionMaxLifespan"] == 30 * 86400
    assert realm["offlineSessionIdleTimeout"] == 7 * 86400
    assert realm["offlineSessionMaxLifespanEnabled"] is True
    assert realm["offlineSessionMaxLifespan"] == 30 * 86400
    assert realm["revokeRefreshToken"] is True
    assert realm["refreshTokenMaxReuse"] == 0


def test_offline_access_can_be_granted_to_registered_clients() -> None:
    realm = _rendered_realm()
    assert "offline_access" in realm["defaultOptionalClientScopes"]
    assert {"clientScope": "offline_access", "roles": ["offline_access"]} in realm["scopeMappings"]


def test_anonymous_registration_checks_redirect_hosts_not_the_sender() -> None:
    config = _anonymous_policy(_rendered_realm(), "trusted-hosts")["config"]
    assert config["host-sending-registration-request-must-match"] == ["false"]
    assert config["client-uris-must-match"] == ["true"]
    hosts = config["trusted-hosts"]
    assert len(hosts) == len(set(hosts))
    assert not any(h.startswith("*") for h in hosts), "a wildcard trusts every subdomain"


def _client_policy(realm: dict[str, Any], name: str) -> dict[str, Any]:
    (policy,) = [p for p in realm["clientPolicies"]["policies"] if p["name"] == name]
    assert policy["enabled"] is True
    return policy


def _executors(realm: dict[str, Any], policy: dict[str, Any]) -> dict[str, dict[str, str]]:
    """The executors of a policy's profiles, by name, with their configuration."""
    profiles = {p["name"]: p for p in realm["clientProfiles"]["profiles"]}
    return {
        e["executor"]: e["configuration"]
        for name in policy["profiles"]
        for e in profiles[name]["executors"]
    }


# These check the shape only. What the policies do is proven against a real Keycloak by
# tests/e2e/stub/test_keycloak_template.py (stub target, keycloak-template service).
def test_anonymously_registered_clients_must_use_pkce_and_no_implicit_or_password_grant() -> None:
    realm = _rendered_realm()
    policy = _client_policy(realm, "anonymous-registration")
    (condition,) = policy["conditions"]
    assert condition["condition"] == "client-updater-context"
    assert condition["configuration"]["update-client-source"] == ["ByAnonymous"]
    executors = _executors(realm, policy)
    assert executors == {
        "pkce-enforcer": {"auto-configure": "true"},
        # Not auto-configured: a registration asking for these grants is refused, not altered.
        "reject-implicit-grant": {"auto-configure": "false"},
        "reject-ropc-grant": {"auto-configure": "false"},
    }


def test_the_token_endpoint_grants_only_authorization_code_and_refresh_token() -> None:
    realm = _rendered_realm()
    policy = _client_policy(realm, "authorization-code-and-refresh-only")
    (condition,) = policy["conditions"]
    assert condition["condition"] == "grant-type"
    assert condition["configuration"] == {
        "is-negative-logic": "true",
        "grant_types": ["authorization_code", "refresh_token"],
    }
    assert _executors(realm, policy) == {"reject-request": {}}


def test_the_template_keycloak_of_the_stub_target_is_the_pinned_image() -> None:
    """The realm checks run on the Keycloak the README says the template is tested on."""
    root = DEPLOY.parent

    def base_image(dockerfile: Path) -> str:
        (line,) = [
            line
            for line in dockerfile.read_text(encoding="utf-8").splitlines()
            if line.startswith("FROM ")
        ]
        return line.removeprefix("FROM ").strip()

    image = base_image(root / "dev" / "keycloak" / "Dockerfile")
    assert base_image(root / "dev" / "keycloak-template" / "Dockerfile") == image
    assert f"`{image}`" in (KEYCLOAK_DIR / "README.md").read_text(encoding="utf-8")


def test_the_stub_harness_targets_the_realm_the_template_keycloak_imports() -> None:
    services = yaml.safe_load((DEPLOY.parent / "compose.stub.yaml").read_text(encoding="utf-8"))[
        "services"
    ]
    args = services["keycloak-template"]["build"]["args"]
    environment = services["harness"]["environment"]
    port = services["keycloak-template"]["ports"][0].rsplit(":", 2)[1]
    assert environment["E2E_KC_TEMPLATE_REALM"] == args["REALM_NAME"]
    assert environment["E2E_KC_TEMPLATE_AUDIENCE"] == args["GATEWAY_PUBLIC_URL"]
    assert environment["E2E_KC_TEMPLATE_URL"] == f"http://localhost:{port}"
    assert services["keycloak-template"]["environment"]["KC_HOSTNAME"] == f"http://localhost:{port}"


def test_the_identity_claims_of_the_examples_are_emitted_by_the_realm() -> None:
    realm = _rendered_realm()
    default_scopes = [_client_scope(realm, n) for n in realm["defaultDefaultClientScopes"]]
    claims = {
        m["config"]["claim.name"]
        for scope in default_scopes
        for m in scope.get("protocolMappers", [])
        if "claim.name" in m["config"]
    }
    for path in CONFIG_EXAMPLES:
        identity = parse_config(path.read_text(encoding="utf-8")).auth.identity
        for claim in (
            identity.email,
            identity.email_verified,
            identity.organization,
            identity.groups,
        ):
            assert claim in claims, (path.name, claim)


@pytest.mark.parametrize("path", CONFIG_EXAMPLES, ids=lambda p: p.name)
def test_the_examples_advertise_scopes_an_anonymous_client_may_register(path: Path) -> None:
    config = parse_config(path.read_text(encoding="utf-8"))
    realm = _rendered_realm(config.public_url)
    assignable = set(realm["defaultDefaultClientScopes"]) | set(
        realm["defaultOptionalClientScopes"]
    )
    # openid is not a Keycloak client scope: a registration that names it is refused.
    assert set(config.auth.scopes_advertised) <= assignable
    # Claude adds offline_access whenever the realm lists it; a client registered without it fails.
    assert "offline_access" in config.auth.scopes_advertised
    assert config.auth.audiences == [config.public_url]


def test_render_refuses_an_unset_variable() -> None:
    result = _render(REALM_NAME="example-publisher")
    assert result.returncode != 0
    assert result.stdout == ""
    assert "GATEWAY_PUBLIC_URL is not set" in result.stderr


@pytest.mark.parametrize(
    "url",
    ["https://mcp.example.com/", "https://mcp.example.com", "https://mcp.example.com/mcp?x=1"],
)
def test_render_refuses_a_url_that_cannot_be_a_public_url(url: str) -> None:
    result = _render(REALM_NAME="example-publisher", GATEWAY_PUBLIC_URL=url)
    assert result.returncode != 0
    assert result.stdout == ""


def test_render_refuses_a_placeholder_it_does_not_know(tmp_path: Path) -> None:
    template = tmp_path / "template.json"
    template.write_text('{"realm": "${REALM_NAME}", "other": "${NOT_A_PLACEHOLDER}"}\n')
    result = _render(template, REALM_NAME="example-publisher", GATEWAY_PUBLIC_URL=PUBLIC_URL)
    assert result.returncode != 0
    assert result.stdout == ""
    assert "${NOT_A_PLACEHOLDER}" in result.stderr


def test_render_leaves_keycloak_message_keys_alone() -> None:
    text = json.dumps(_rendered_realm())
    assert "${client_id}" in text
    assert "${rolesScopeConsentText}" in text


def test_the_cimd_policies_keep_every_template_profile_and_policy() -> None:
    """The admin API replaces whole lists, so the CIMD file must carry the template's entries."""
    realm = _rendered_realm()
    cimd = json.loads((KEYCLOAK_DIR / "cimd-client-policies.json").read_text(encoding="utf-8"))
    template_profiles = realm["clientProfiles"]["profiles"]
    template_policies = realm["clientPolicies"]["policies"]
    assert cimd["clientProfiles"]["profiles"][: len(template_profiles)] == template_profiles
    assert cimd["clientPolicies"]["policies"][: len(template_policies)] == template_policies
    (cimd_policy,) = cimd["clientPolicies"]["policies"][len(template_policies) :]
    assert cimd_policy["conditions"][0]["condition"] == "client-id-uri"
