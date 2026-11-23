"""Tool policy: config validation, the pattern matcher and rule resolution."""

import re
from types import MappingProxyType
from typing import Any

import pytest
from pydantic import SecretStr

from mcp_gateway.config import ConfigError, ToolsPolicyConfig
from mcp_gateway.identity import Identity
from mcp_gateway.resolver import Credential
from mcp_gateway.tool_policy import EffectivePolicy, ToolPattern, ToolPolicy

from .conftest import config_from
from .test_config import _parse


def policy_config(**overrides: Any) -> ToolsPolicyConfig:
    data: dict[str, Any] = {
        "default": {"allow": None, "deny": []},
        "rules": [],
        **overrides,
    }
    return ToolsPolicyConfig.model_validate(data, strict=True)


def identity(organization: str | None = "client-x") -> Identity:
    return Identity(
        subject="user-1",
        email="alice@client-x.example",
        organization=organization,
        groups=(),
        claims=MappingProxyType({}),
    )


def credential(**labels: str) -> Credential:
    return Credential(api_key=SecretStr("k"), upstream="main", labels=labels)


# --- configuration ----------------------------------------------------------------------


def test_tools_policy_is_required_and_may_be_explicit_null(config_dict: dict[str, Any]) -> None:
    assert config_from(config_dict).policies.tools is None
    del config_dict["policies"]["tools"]
    with pytest.raises(ConfigError, match="tools"):
        _parse(config_dict)


def test_a_complete_policy_loads(config_dict: dict[str, Any]) -> None:
    config_dict["policies"]["tools"] = {
        "default": {"allow": ["search", "get_*"], "deny": []},
        "rules": [
            {"match": {"organization": "client-x"}, "allow": None, "deny": ["delete_*"]},
            {"match": {"label": {"name": "tier", "value": "basic"}}, "allow": [], "deny": []},
        ],
    }
    tools = config_from(config_dict).policies.tools
    assert tools is not None
    assert len(tools.rules) == 2


@pytest.mark.parametrize(
    "mutate",
    [
        lambda t: t["default"].pop("allow"),
        lambda t: t["default"].pop("deny"),
        lambda t: t.pop("rules"),
        lambda t: t.pop("default"),
        lambda t: t["rules"][0].pop("match"),
        lambda t: t["rules"][0].pop("allow"),
        lambda t: t["rules"][0].pop("deny"),
        lambda t: t["default"].update(extra=1),
        lambda t: t["rules"][0].update(match={}),
        lambda t: t["rules"][0].update(match={"organization": ""}),
        lambda t: t["rules"][0].update(
            match={"organization": "a", "label": {"name": "n", "value": "v"}}
        ),
        lambda t: t["rules"][0].update(match={"label": {"name": "", "value": "v"}}),
        lambda t: t["rules"][0].update(match={"label": {"name": "n"}}),
        lambda t: t["default"].update(allow=[""]),
        lambda t: t["default"].update(deny=["has space"]),
        lambda t: t["default"].update(deny=["tab\there"]),
        lambda t: t["default"].update(allow=["nul\x00"]),
        lambda t: t["default"].update(allow=["x" * 257]),
        lambda t: t["default"].update(allow="search"),
        lambda t: t["default"].update(deny=None),
    ],
)
def test_invalid_policies_are_refused_at_load(config_dict: dict[str, Any], mutate: Any) -> None:
    tools: dict[str, Any] = {
        "default": {"allow": None, "deny": []},
        "rules": [{"match": {"organization": "client-x"}, "allow": None, "deny": []}],
    }
    mutate(tools)
    config_dict["policies"]["tools"] = tools
    with pytest.raises(ConfigError, match="tools"):
        _parse(config_dict)


def test_a_pattern_of_the_maximum_length_is_accepted(config_dict: dict[str, Any]) -> None:
    config_dict["policies"]["tools"] = {
        "default": {"allow": ["x" * 256], "deny": []},
        "rules": [],
    }
    assert config_from(config_dict).policies.tools is not None


def test_filtered_response_limit_is_required_and_positive(config_dict: dict[str, Any]) -> None:
    config_dict["limits"]["max_filtered_response_bytes"] = 0
    with pytest.raises(ConfigError, match="max_filtered_response_bytes"):
        _parse(config_dict)
    del config_dict["limits"]["max_filtered_response_bytes"]
    with pytest.raises(ConfigError, match="max_filtered_response_bytes"):
        _parse(config_dict)


# --- matcher ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("pattern", "name", "expected"),
    [
        ("search", "search", True),
        ("search", "searcher", False),
        ("search", "Search", False),
        ("get_*", "get_entry", True),
        ("get_*", "get_", True),
        ("get_*", "set_get_x", False),
        ("*_read", "file_read", True),
        ("*_read", "_read", True),
        ("*_read", "file_read_all", False),
        ("a*b", "ab", True),
        ("a*b", "axxb", True),
        ("a*b", "a", False),
        ("a*b", "b", False),
        ("a*a", "a", False),
        ("a*a", "aa", True),
        ("a*b*c", "a-b-c", True),
        ("a*b*c", "a-c-b", False),
        ("a*b*c", "abc", True),
        ("a*b*b", "ab", False),
        ("a*b*b", "abb", True),
        ("*", "anything", True),
        ("*", "", True),
        ("**", "x", True),
        ("a**b", "ab", True),
    ],
)
def test_wildcard_matching(pattern: str, name: str, expected: bool) -> None:
    assert ToolPattern.compile(pattern).matches(name) is expected


@pytest.mark.parametrize(
    ("pattern", "name", "expected"),
    [
        ("a.c", "abc", False),
        ("a.c", "a.c", True),
        ("a+", "aa", False),
        ("a+", "a+", True),
        ("(x)", "x", False),
        ("(x)", "(x)", True),
        ("[ab]", "a", False),
        ("[ab]", "[ab]", True),
        ("^a$", "a", False),
        ("a|b", "a", False),
        ("a|b", "a|b", True),
        ("a\\d", "a1", False),
        ("a?", "a", False),
        ("a{2}", "aa", False),
        ("a{2}", "a{2}", True),
        ("a.*b", "axxb", False),
        ("a.*b", "a.xxb", True),
    ],
)
def test_regular_expression_characters_are_literal(pattern: str, name: str, expected: bool) -> None:
    assert ToolPattern.compile(pattern).matches(name) is expected


def test_matching_a_long_name_against_many_wildcards_is_not_exponential() -> None:
    pattern = ToolPattern.compile("*".join(["a"] * 100) + "*b")
    assert pattern.matches("a" * 50_000) is False


def test_the_matcher_agrees_with_a_reference_implementation() -> None:
    patterns = ["", "a", "*", "a*", "*a", "a*a", "*a*", "a*b*a", "ab*ba", "**a**", "b*"]
    names = ["", "a", "b", "aa", "ab", "ba", "aba", "abba", "abab", "bab", "aaa"]
    for pattern in patterns:
        reference = re.compile(".*".join(re.escape(p) for p in pattern.split("*")), re.DOTALL)
        compiled = ToolPattern.compile(pattern)
        for name in names:
            assert compiled.matches(name) is (reference.fullmatch(name) is not None), (
                pattern,
                name,
            )


# --- allow / deny -----------------------------------------------------------------------


def effective(allow: list[str] | None, deny: list[str]) -> EffectivePolicy:
    return ToolPolicy(policy_config(default={"allow": allow, "deny": deny})).effective(
        identity(), credential()
    )


def test_allow_null_allows_everything() -> None:
    assert effective(None, []).allows("anything")


def test_an_allow_list_allows_only_what_it_matches() -> None:
    policy = effective(["search", "get_*"], [])
    assert [policy.allows(n) for n in ("search", "get_x", "delete_x")] == [True, True, False]


def test_an_empty_allow_list_allows_nothing() -> None:
    assert not effective([], []).allows("search")


def test_deny_wins_over_allow() -> None:
    policy = effective(["get_*"], ["get_secret"])
    assert policy.allows("get_entry") and not policy.allows("get_secret")


def test_deny_applies_when_everything_is_allowed() -> None:
    policy = effective(None, ["delete_*"])
    assert policy.allows("search") and not policy.allows("delete_all")


def test_filter_tools_keeps_allowed_definitions_whole_and_in_order() -> None:
    policy = effective(["a", "c"], [])
    tools = [{"name": "a", "x": 1}, {"name": "b"}, {"name": "c", "inputSchema": {}}]
    assert policy.filter_tools(tools) == [{"name": "a", "x": 1}, {"name": "c", "inputSchema": {}}]


def test_filter_tools_drops_what_cannot_be_checked() -> None:
    policy = effective(None, [])
    junk: list[Any] = [{"description": "no name"}, {"name": 5}, "a", None, ["a"], {"name": "ok"}]
    assert policy.filter_tools(junk) == [{"name": "ok"}]


# --- rule resolution --------------------------------------------------------------------


def rules_policy() -> ToolPolicy:
    return ToolPolicy(
        policy_config(
            default={"allow": ["default_tool"], "deny": []},
            rules=[
                {"match": {"organization": "client-x"}, "allow": ["org_tool"], "deny": []},
                {
                    "match": {"label": {"name": "tier", "value": "gold"}},
                    "allow": ["gold_tool"],
                    "deny": [],
                },
                {"match": {"organization": "client-x"}, "allow": ["shadowed"], "deny": []},
            ],
        )
    )


def test_the_default_applies_when_no_rule_matches() -> None:
    policy = rules_policy().effective(identity("other"), credential())
    assert policy.allows("default_tool") and not policy.allows("org_tool")


def test_an_organization_rule_matches_the_identity_organization() -> None:
    policy = rules_policy().effective(identity("client-x"), credential())
    assert policy.allows("org_tool") and not policy.allows("default_tool")


def test_a_caller_without_an_organization_matches_no_organization_rule() -> None:
    policy = rules_policy().effective(identity(None), credential())
    assert policy.allows("default_tool")


def test_a_label_rule_matches_the_resolver_label() -> None:
    policy = rules_policy().effective(identity("other"), credential(tier="gold"))
    assert policy.allows("gold_tool") and not policy.allows("default_tool")


@pytest.mark.parametrize("labels", [{"tier": "silver"}, {"other": "gold"}, {}])
def test_a_label_with_another_value_or_name_does_not_match(labels: dict[str, str]) -> None:
    policy = rules_policy().effective(identity("other"), credential(**labels))
    assert policy.allows("default_tool")


def test_the_first_matching_rule_wins_and_is_not_merged_with_later_ones() -> None:
    policy = rules_policy().effective(identity("client-x"), credential(tier="gold"))
    assert policy.allows("org_tool")
    assert not policy.allows("gold_tool") and not policy.allows("shadowed")
