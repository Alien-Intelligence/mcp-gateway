"""Static resolver: a JSON mapping file of identity matchers to API key files.

The mapping file is re-parsed when its (mtime, size) changes. Key files are read at resolve
time so a rotated or revoked key takes effect without a restart. Both are a few bytes, so the
key read goes through ``asyncio.to_thread`` and the mapping stat/parse is done inline (one
``stat`` per request; a parse only on change).
"""

import asyncio
import logging
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, field_validator

from mcp_gateway.identity import Identity
from mcp_gateway.resolver.base import Credential, Denied, ResolverError

logger = logging.getLogger(__name__)

MatchKind = Literal["subject", "email", "organization", "group"]


class _Entry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    match: dict[MatchKind, str]
    api_key_file: str = Field(min_length=1)
    upstream: str = Field(min_length=1)
    labels: dict[str, str]

    @field_validator("match")
    @classmethod
    def _exactly_one_non_empty(cls, value: dict[MatchKind, str]) -> dict[MatchKind, str]:
        if len(value) != 1:
            raise ValueError("match must have exactly one of subject/email/organization/group")
        if not next(iter(value.values())).strip():
            raise ValueError("match value must not be empty")
        return value


class _MappingFile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    entries: list[_Entry]


@dataclass(frozen=True)
class _Rule:
    index: int
    key_path: Path
    upstream: str
    labels: Mapping[str, str]


@dataclass(frozen=True)
class _Index:
    subject: Mapping[str, _Rule]
    email: Mapping[str, _Rule]
    organization: Mapping[str, _Rule]
    group: Mapping[str, _Rule]


def _normalise(kind: MatchKind, value: str) -> str:
    value = value.strip()
    return value.casefold() if kind == "email" else value


def _build_index(raw: str, base_dir: Path, upstream_names: Collection[str]) -> _Index:
    parsed = _MappingFile.model_validate_json(raw, strict=True)
    tables: dict[MatchKind, dict[str, _Rule]] = {
        "subject": {},
        "email": {},
        "organization": {},
        "group": {},
    }
    for index, entry in enumerate(parsed.entries):
        if entry.upstream not in upstream_names:
            raise ValueError(f"entries.{index}: upstream is not a configured upstream")
        ((kind, value),) = entry.match.items()
        key = _normalise(kind, value)
        if key in tables[kind]:
            raise ValueError(f"entries.{index}: duplicate {kind} match")
        # Path(base) / absolute -> absolute, so absolute key paths are allowed.
        tables[kind][key] = _Rule(
            index=index,
            key_path=base_dir / entry.api_key_file,
            upstream=entry.upstream,
            labels=dict(entry.labels),
        )
    return _Index(
        subject=tables["subject"],
        email=tables["email"],
        organization=tables["organization"],
        group=tables["group"],
    )


def _describe(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        return "; ".join(
            f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}"
            for err in exc.errors(include_input=False, include_url=False)
        )
    return str(exc)


class StaticResolver:
    def __init__(self, mapping_file: Path, upstream_names: Collection[str]) -> None:
        self._mapping_file = mapping_file
        self._upstream_names = frozenset(upstream_names)
        self._signature: tuple[int, int] | None = None
        self._index: _Index | None = None
        self._load_error: str | None = None
        # Fail fast at startup: a missing or broken mapping file must not boot a gateway.
        self._load()
        if self._index is None:
            raise ResolverError(f"mapping file rejected: {self._load_error}")

    def _load(self) -> None:
        try:
            stat = self._mapping_file.stat()
            signature = (stat.st_mtime_ns, stat.st_size)
            raw = self._mapping_file.read_text(encoding="utf-8")
            index = _build_index(raw, self._mapping_file.parent, self._upstream_names)
        except (OSError, ValueError) as exc:
            # ValidationError and JSON errors are ValueError subclasses.
            self._index = None
            self._load_error = _describe(exc)
            self._signature = self._safe_signature()
            logger.error("static resolver mapping file rejected: %s", self._load_error)
            return
        self._index = index
        self._load_error = None
        self._signature = signature
        logger.info("static resolver mapping loaded: %d rules", _count(index))

    def _safe_signature(self) -> tuple[int, int] | None:
        try:
            stat = self._mapping_file.stat()
        except OSError:
            return None
        return (stat.st_mtime_ns, stat.st_size)

    def _current_index(self) -> _Index:
        if self._safe_signature() != self._signature:
            self._load()
        if self._index is None:
            raise ResolverError(f"mapping file unusable: {self._load_error}")
        return self._index

    def _match(self, identity: Identity) -> _Rule | Denied:
        index = self._current_index()
        rule = index.subject.get(_normalise("subject", identity.subject))
        if rule is None and identity.email is not None:
            rule = index.email.get(_normalise("email", identity.email))
        if rule is None and identity.organization is not None:
            rule = index.organization.get(_normalise("organization", identity.organization))
        if rule is not None:
            return rule
        group_rules = {
            found.index: found
            for group in identity.groups
            if (found := index.group.get(_normalise("group", group))) is not None
        }
        if len(group_rules) > 1:
            return Denied("ambiguous_mapping")
        if group_rules:
            return next(iter(group_rules.values()))
        return Denied("no_mapping")

    async def resolve(self, identity: Identity) -> Credential | Denied:
        rule = self._match(identity)
        if isinstance(rule, Denied):
            return rule
        key = await self._read_key(rule)
        return Credential(api_key=SecretStr(key), upstream=rule.upstream, labels=rule.labels)

    async def _read_key(self, rule: _Rule) -> str:
        try:
            text = await asyncio.to_thread(rule.key_path.read_text, encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            logger.error(
                "api key file unreadable for mapping entry %d (%s)", rule.index, type(exc).__name__
            )
            raise ResolverError(f"api key file unreadable for entry {rule.index}") from exc
        key = text.strip()
        if not key or any(ch.isspace() or not ch.isprintable() for ch in key):
            logger.error("api key file empty or malformed for mapping entry %d", rule.index)
            raise ResolverError(f"api key file empty or malformed for entry {rule.index}")
        return key

    async def invalidate(self, identity: Identity) -> None:
        """Nothing is cached (keys are read per request), so there is nothing to drop."""
        del identity


def _count(index: _Index) -> int:
    return len(index.subject) + len(index.email) + len(index.organization) + len(index.group)


__all__ = ["StaticResolver"]
