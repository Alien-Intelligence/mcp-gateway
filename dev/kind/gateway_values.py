"""Write the Helm values of the kind install: the dev stack's `gateway-keycloak` service (static
resolver, tool policy, per-subject rate limit) as two replicas sharing their state in Redis.

Usage: gateway_values.py <image tag> <upstream MCP URL> > values.yaml

The public URL and the issuer are the host's loopback ports published by kind-config.yaml.
"""

import json
import sys

PUBLIC_URL = "http://localhost:38000/mcp"
ISSUER = "http://localhost:38080/realms/alien-gateway-dev"
JWKS_URI = "http://keycloak:8080/realms/alien-gateway-dev/protocol/openid-connect/certs"


def upstream(url: str) -> dict[str, object]:
    return {
        "url": url,
        "connect_timeout_seconds": 10,
        "read_timeout_seconds": 130,
        "write_timeout_seconds": 30,
        "pool_timeout_seconds": 10,
        "max_connections": 200,
        "max_detached_reads": 50,
    }


def values(image_tag: str, upstream_url: str) -> dict[str, object]:
    config = {
        "version": 1,
        "public_url": PUBLIC_URL,
        "listen": {"host": "0.0.0.0", "port": 8000},
        "auth": {
            "issuer": ISSUER,
            "jwks_uri": JWKS_URI,
            "audiences": [PUBLIC_URL],
            "algorithms": ["RS256"],
            "required_scopes": [],
            "scopes_advertised": ["openid", "mcp-gateway"],
            "clock_skew_seconds": 5,
            "jwks_cache_ttl_seconds": 300,
            "jwks_max_stale_seconds": 3600,
            "jwks_fetch_timeout_seconds": 5,
            "identity": {
                "subject": "sub",
                "email": "email",
                "email_verified": "email_verified",
                "organization": "org",
                "groups": "groups",
            },
            "userinfo": None,
        },
        "resolver": {"type": "static", "mapping_file": "/run/gateway/state/mapping.json"},
        "resolver_cache": {
            "positive_ttl_seconds": 60,
            "negative_ttl_seconds": 1,
            "max_entries": None,
        },
        "upstreams": {"stateful": upstream(upstream_url), "stateless": upstream(upstream_url)},
        "limits": {
            "max_request_body_bytes": 1048576,
            "request_body_timeout_seconds": 30,
            "max_sse_event_bytes": 1048576,
            "max_filtered_response_bytes": 4194304,
        },
        "audit": {"enabled": True},
        "observability": {"otlp": None},
        "sessions": {"max_entries": None, "max_entries_per_subject": 50, "idle_ttl_seconds": 600},
        "state": {
            "type": "redis",
            "url_file": "/run/gateway/secrets/state-redis-url",
            "key_prefix": "mcp-gateway:state:",
            "connect_timeout_seconds": 2,
            "operation_timeout_seconds": 2,
            "max_connections": 32,
            "encryption_key_file": "/run/gateway/secrets/state-encryption-key",
        },
        "policies": {
            "allowed_origins": ["https://claude.ai", "http://localhost:6274"],
            "tools": {
                "default": {
                    "allow": None,
                    "deny": ["datacluster_keyword_search", "policy_denied_probe"],
                },
                "rules": [
                    {
                        "match": {"label": {"name": "client", "value": "client-y"}},
                        "allow": ["datacluster_get_dataset", "datacluster_list_datasets", "echo"],
                        "deny": [],
                    }
                ],
            },
            "rate_limits": {
                "per_subject": {"burst": 40, "per_minute": 20},
                "per_organization": None,
            },
        },
    }
    return {
        "image": {"repository": "mcp-gateway", "tag": image_tag, "pullPolicy": "IfNotPresent"},
        "replicaCount": 2,
        "service": {"type": "NodePort", "port": 80, "nodePort": 30000},
        "config": config,
        "secretMounts": [
            {
                "name": "secrets",
                "secretName": "gateway-secrets",
                "mountPath": "/run/gateway/secrets",
            },
            {"name": "state", "secretName": "gateway-state", "mountPath": "/run/gateway/state"},
        ],
    }


def main() -> None:
    if len(sys.argv) != 3:
        sys.exit("usage: gateway_values.py <image tag> <upstream MCP URL>")
    # JSON is YAML: helm reads it as a values file.
    json.dump(values(sys.argv[1], sys.argv[2]), sys.stdout, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
