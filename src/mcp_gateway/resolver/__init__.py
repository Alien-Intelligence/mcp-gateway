"""Credential resolvers."""

from mcp_gateway.resolver.base import (
    Credential,
    CredentialResolver,
    Denied,
    ResolverError,
    ResolverUnavailableError,
)
from mcp_gateway.resolver.cache import CacheResult, CacheStore, CachingResolver, MemoryCacheStore
from mcp_gateway.resolver.chain import ChainResolver
from mcp_gateway.resolver.factory import build_resolver, with_cache
from mcp_gateway.resolver.static import StaticResolver
from mcp_gateway.resolver.store_redis import RedisStoreResolver
from mcp_gateway.resolver.store_sql import SqlStoreResolver
from mcp_gateway.resolver.webhook import WebhookResolver

__all__ = [
    "CacheResult",
    "CacheStore",
    "CachingResolver",
    "ChainResolver",
    "Credential",
    "CredentialResolver",
    "Denied",
    "MemoryCacheStore",
    "RedisStoreResolver",
    "ResolverError",
    "ResolverUnavailableError",
    "SqlStoreResolver",
    "StaticResolver",
    "WebhookResolver",
    "build_resolver",
    "with_cache",
]
