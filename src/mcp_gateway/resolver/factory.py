"""Build the configured resolver, optionally behind the cache."""

from collections.abc import Collection
from pathlib import Path

from mcp_gateway.config import (
    ChainResolverConfig,
    LeafResolverConfig,
    ResolverCacheConfig,
    ResolverConfig,
)
from mcp_gateway.resolver.base import CredentialResolver
from mcp_gateway.resolver.cache import CachingResolver
from mcp_gateway.resolver.chain import ChainResolver
from mcp_gateway.resolver.static import StaticResolver
from mcp_gateway.resolver.store_redis import RedisStoreResolver
from mcp_gateway.resolver.store_sql import SqlStoreResolver
from mcp_gateway.resolver.webhook import WebhookResolver


def _build_leaf(
    config: LeafResolverConfig, upstream_names: Collection[str], *, email_matching: bool
) -> CredentialResolver:
    match config.type:
        case "static":
            return StaticResolver(
                Path(config.mapping_file), upstream_names, email_matching=email_matching
            )
        case "webhook":
            return WebhookResolver(config, upstream_names)
        case "store_sql":
            return SqlStoreResolver(config, upstream_names)
        case "store_redis":
            return RedisStoreResolver(config, upstream_names)


def build_resolver(
    config: ResolverConfig, upstream_names: Collection[str], *, email_matching: bool
) -> CredentialResolver:
    """Construct the resolver described by ``config`` (no cache). Fails fast on bad config."""
    if isinstance(config, ChainResolverConfig):
        return ChainResolver(
            [
                _build_leaf(member, upstream_names, email_matching=email_matching)
                for member in config.resolvers
            ]
        )
    return _build_leaf(config, upstream_names, email_matching=email_matching)


def with_cache(
    resolver: CredentialResolver, config: ResolverCacheConfig | None
) -> CredentialResolver:
    """Wrap ``resolver`` in the cache, or return it unchanged when the cache is disabled."""
    return resolver if config is None else CachingResolver(resolver, config)
