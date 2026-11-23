"""Tool allow/deny policy: which tools a caller may see and call.

The configuration is compiled once at startup. Per request, ``ToolPolicy.effective`` picks the
first rule that matches the caller (identity organization or resolver label) and falls back to
the default; the result answers ``allows(name)`` and filters a ``tools/list`` result.

Matching is done by hand rather than by building a regular expression from operator-supplied
text: the only wildcard is ``*``, everything else is compared literally, and the cost is
bounded by pattern length times name length.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from mcp_gateway.config import (
    LabelMatchConfig,
    OrganizationMatchConfig,
    ToolAccessConfig,
    ToolsPolicyConfig,
)
from mcp_gateway.identity import Identity
from mcp_gateway.resolver import Credential

_WILDCARD = "*"


@dataclass(frozen=True)
class ToolPattern:
    """A tool name pattern: literal text, with ``*`` standing for any run of characters."""

    pieces: tuple[str, ...]

    @classmethod
    def compile(cls, pattern: str) -> "ToolPattern":
        return cls(tuple(pattern.split(_WILDCARD)))

    def matches(self, name: str) -> bool:
        first, *rest = self.pieces
        if not rest:
            return name == first
        *middle, last = rest
        if not name.startswith(first):
            return False
        position = len(first)
        for piece in middle:
            found = name.find(piece, position)
            if found < 0:
                return False
            position = found + len(piece)
        # The tail must fit after everything consumed so far.
        return len(name) - position >= len(last) and name.endswith(last)


@dataclass(frozen=True)
class EffectivePolicy:
    """The allow/deny lists that apply to one caller."""

    allow: tuple[ToolPattern, ...] | None
    deny: tuple[ToolPattern, ...]

    @classmethod
    def compile(cls, access: ToolAccessConfig) -> "EffectivePolicy":
        return cls(
            allow=None if access.allow is None else tuple(map(ToolPattern.compile, access.allow)),
            deny=tuple(map(ToolPattern.compile, access.deny)),
        )

    def allows(self, name: str) -> bool:
        if any(pattern.matches(name) for pattern in self.deny):
            return False
        return self.allow is None or any(pattern.matches(name) for pattern in self.allow)

    def filter_tools(self, tools: Sequence[Any]) -> list[Any]:
        """Keep the allowed tool definitions. One without a string ``name`` cannot be verified
        and is dropped."""
        return [
            tool
            for tool in tools
            if isinstance(tool, dict)
            and isinstance(tool.get("name"), str)
            and self.allows(tool["name"])
        ]


@dataclass(frozen=True)
class _Rule:
    match: OrganizationMatchConfig | LabelMatchConfig
    policy: EffectivePolicy

    def applies(self, identity: Identity, credential: Credential) -> bool:
        match self.match:
            case OrganizationMatchConfig(organization=organization):
                return identity.organization == organization
            case LabelMatchConfig(label=label):
                return credential.labels.get(label.name) == label.value


class ToolPolicy:
    def __init__(self, config: ToolsPolicyConfig) -> None:
        self._default = EffectivePolicy.compile(config.default)
        self._rules = tuple(
            _Rule(rule.match, EffectivePolicy.compile(rule)) for rule in config.rules
        )

    def effective(self, identity: Identity, credential: Credential) -> EffectivePolicy:
        for rule in self._rules:
            if rule.applies(identity, credential):
                return rule.policy
        return self._default
