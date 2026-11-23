"""The real-dev suite's providers take their gateway URL and issuer from the environment."""

import pytest
from e2e.harness import PROVIDERS, REPLICAS, provider_at

DEFAULT_URL = "http://localhost:18004/mcp"
DEFAULT_ISSUER = "http://localhost:18080/realms/alien-gateway-dev"


def test_without_overrides_a_provider_is_at_its_dev_stack_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("E2E_GATEWAY_URL_REPLICAS", raising=False)
    monkeypatch.delenv("E2E_ISSUER_REPLICAS", raising=False)
    provider = provider_at("replicas", DEFAULT_URL, DEFAULT_ISSUER, {"scope": "openid"})
    assert provider.gateway_url == DEFAULT_URL
    assert provider.issuer == DEFAULT_ISSUER
    assert provider.owns_state


def test_the_environment_replaces_the_url_and_the_issuer_of_the_named_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("E2E_GATEWAY_URL_REPLICAS", "https://mcp.example.com/mcp")
    monkeypatch.setenv("E2E_ISSUER_REPLICAS", "https://idp.example.com/realms/main")
    provider = provider_at("replicas", DEFAULT_URL, DEFAULT_ISSUER, {"scope": "openid"})
    assert provider.gateway_url == "https://mcp.example.com/mcp"
    assert provider.issuer == "https://idp.example.com/realms/main"
    assert provider.name == "replicas"
    # A deployment the harness did not start: its keys cannot be replaced from here.
    assert not provider.owns_state


def test_one_override_leaves_the_other_value_and_other_providers_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("E2E_GATEWAY_URL_HYDRA", "https://mcp.example.com/mcp")
    monkeypatch.delenv("E2E_ISSUER_HYDRA", raising=False)
    hydra = provider_at("hydra", "http://localhost:18001/mcp", "http://localhost:14444", {})
    keycloak = provider_at("keycloak", "http://localhost:18000/mcp", DEFAULT_ISSUER, {})
    assert hydra.gateway_url == "https://mcp.example.com/mcp"
    assert hydra.issuer == "http://localhost:14444"
    assert keycloak.gateway_url == "http://localhost:18000/mcp"
    assert not hydra.owns_state
    assert keycloak.owns_state


def test_the_module_level_providers_exist() -> None:
    assert {provider.name for provider in PROVIDERS} == {"keycloak", "hydra"}
    assert REPLICAS.name == "replicas"
