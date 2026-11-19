"""Credential resolvers."""

from mcp_gateway.resolver.base import Credential, CredentialResolver, Denied, ResolverError
from mcp_gateway.resolver.static import StaticResolver

__all__ = ["Credential", "CredentialResolver", "Denied", "ResolverError", "StaticResolver"]
