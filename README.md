# Alien MCP Gateway

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

An OAuth-protected front door for Alien MCP servers that you host on your own domain. MCP clients (Claude, ChatGPT, Microsoft Copilot Studio, VS Code, Cursor) sign in with **your own OAuth server**, for example Keycloak. The gateway checks the token, maps the verified identity to an Alien API key through a pluggable credential resolver, and streams the MCP traffic to Alien unchanged. Your users sign in with their usual accounts, and Alien sees one billed user per API key.

```
MCP client ──OAuth 2.1 + PKCE──> your OAuth server (token aud = the gateway URL)
    │
    └─ Bearer JWT ─> gateway: check the JWT (JWKS, iss, aud, exp/nbf)
                             identity = claims (subject, verified email, organization, groups)
                             resolver(identity) -> Alien API key, or 403
                             drop the client's credentials and forgeable headers, add the key
                             stream to the Alien MCP server, stream the answer back untouched
```

## Who it is for

A publisher, or any organization, that sponsors access to Alien MCP servers for its own users and wants them to sign in with the accounts they already have, on an MCP endpoint under the organization's own domain. You run the gateway and an OAuth server; Alien keeps the data, the MCP servers and the metering.

## Features

- OAuth 2.1 resource server: local JWT validation (JWKS, issuer, audience, expiry, optional required scopes), Protected Resource Metadata (RFC 9728), works with any server that meets the [prerequisites](#prerequisites-your-oauth-server).
- Five credential resolvers (`static`, `webhook`, `store_sql`, `store_redis`, `chain`), an optional cache, and an explicit denial for a user without a mapping: there is never a fallback key.
- Streaming reverse proxy for MCP over HTTP, Server-Sent Events included, with allow-listed headers; the client's credentials never reach Alien.
- Per-caller tool allow and deny lists, rate limits per subject and per organization, and browser-origin checks.
- Several replicas with shared state in Redis (sessions, credential cache, rate-limit buckets); readiness and liveness probes.
- OpenTelemetry traces and metrics, and one JSON audit line per request that never carries content, tokens or keys.
- A hardened container image (non-root, read-only filesystem, no capabilities), a Helm chart, a Docker Compose example and a Keycloak realm template.

## Quick start

The [setup tutorial](docs/tutorial/en/README.md) takes you from nothing to a working deployment. The short path:

- **Kubernetes:** the [Helm chart](charts/mcp-gateway/README.md) and [tutorial chapter 5](docs/tutorial/en/05-deploying.md).
- **One host:** the [Docker Compose example](deploy/compose/README.md) (gateway, Redis, TLS reverse proxy settings).
- **Your OAuth server:** the [Keycloak realm template](deploy/keycloak/README.md) and [tutorial chapter 6](docs/tutorial/en/06-oauth-server.md); a checklist for other servers is in the same chapter.
- **The Alien side** (organizations, users and keys): [tutorial chapter 3](docs/tutorial/en/03-alien-side.md).

## Documentation

| Document | What it covers |
|---|---|
| [Tutorial (English)](docs/tutorial/en/README.md) | Setup, step by step, in nine chapters. |
| [Tutorial (French)](docs/tutorial/fr/README.md) | The same tutorial in French (Tutoriel d'installation). |
| [Configuration reference](docs/configuration.md) | Every key of `MCP_GATEWAY_CONFIG`, with bounds and a complete example |
| [Behaviour overview](docs/overview.md) | Sessions, state, policies, resolvers, observability and the audit line |
| [Wire protocol](docs/protocol.md) | HTTP statuses, JSON-RPC error codes, `_meta` keys, challenges and headers |
| [Helm chart](charts/mcp-gateway/README.md) | Chart values and the high-availability shape |
| [Docker Compose example](deploy/compose/README.md) | A hardened single-host deployment |
| [Keycloak realm template](deploy/keycloak/README.md) | The identity provider side, with token lifetimes and trusted hosts |
| [Development](docs/development.md) | The development stack, test suites, load test and CI |

## How it works

The diagram above is the whole path. In detail:

- The gateway is an OAuth **resource server** only. It issues no tokens and registers no clients: your OAuth server does that.
- It publishes Protected Resource Metadata (RFC 9728) at the path-specific URL only (`/.well-known/oauth-protected-resource/mcp` for a `/mcp` endpoint), with `resource` equal to `public_url`. The origin-level URL answers `404`: RFC 9728 requires `resource` to match the URL the document was fetched for.
- A request without a valid token gets `401` with `WWW-Authenticate: Bearer resource_metadata="…", scope="…"`.
- A signed-in user with no mapping gets `403` with a JSON-RPC error (`-31010`) and no challenge.
- If Alien rejects the mapped key (`401` or `402`), the client gets a JSON-RPC error (`-31001` / `-31002`), never an OAuth challenge, so it does not loop on re-authentication.
- Inbound headers are allow-listed. `Authorization`, `Cookie`, `X-Oauth-Access-Token`, `X-Job-Id`, `X-Organization-Id`, `X-Mcp-Config-Id` and `X-Forwarded-*` never reach Alien.
- One JSON audit line per request: subject, organization, labels, method, tool, status, outcome, latency. Never content, tokens or keys.

## Prerequisites: your OAuth server

The gateway works with any OAuth server that provides all of the following:

1. **Discovery:** RFC 8414 authorization-server metadata or OpenID Connect discovery, advertising `S256` in `code_challenge_methods_supported`.
2. **JWT access tokens** signed with an asymmetric key (RS256, ES256, PS256 or EdDSA) and a published JWKS. The gateway validates tokens locally; it does not use token introspection. If the access token carries only `sub`, `auth.userinfo` makes it read the other claims from the provider's OIDC userinfo endpoint; opaque access tokens (Google) cannot be used either way.
3. **The gateway URL as audience:** the access token's `aud` must contain the gateway's public URL. Keycloak does this with a client scope carrying an Audience mapper (the [realm template](deploy/keycloak/) sets it up); Ory Hydra does it in the consent step (`grant_access_token_audience`).
4. **Client registration that MCP clients can use:** anonymous Dynamic Client Registration (RFC 7591), Client ID Metadata Documents, or clients you pre-register for each MCP client.
5. **Identity claims**: a stable subject in the access token, and the claims you map on (`email` with `email_verified`, an organization claim, groups) in the access token or at the userinfo endpoint. Claim paths are configurable, including nested ones such as `ext.org`.

Known incompatibility: authentik 2026.8 dynamic client registration requires a pre-issued bearer token, and the clients it creates sign tokens with their own secret (HS256) under a per-application issuer, so a resource server cannot verify them through a JWKS. With authentik, pre-register the MCP clients instead.

## Container image

The release flow builds one image for `linux/amd64` and `linux/arm64` and pushes it to the project's own registry (`.gitlab-ci.yml`, internal release flow). Tags: `<version>` from `main`, `<version>-rc` from `staging`, `<version>-dev` from `dev`, plus the moving tags `latest`, `latest-staging` and `latest-dev`. Releases from `main` are also published at **`ghcr.io/alien-intelligence/mcp-gateway`** with the tags `X.Y.Z`, `X.Y`, `X` and `latest`: the same digest as the release, copied after its vulnerability scan, never rebuilt. Pin deployments to a version, or better to the digest. You can also build the image yourself:

```sh
docker buildx build \
  --build-arg VERSION="$(sed -n 's/^version = "\(.*\)"$/\1/p' pyproject.toml | head -n 1)" \
  --build-arg VCS_REF="$(git rev-parse HEAD)" \
  --build-arg BUILD_DATE="$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --tag mcp-gateway:local --load .
```

The three build arguments are required: the build refuses to run without them. Each image carries an SBOM and a provenance attestation, and the OCI labels `source`, `version`, `revision` and `created`.

The image runs as uid 1000 with no writable path: the gateway keeps its state in memory or in Redis and logs to stdout. Run it the way it is tested, read-only and without capabilities:

```bash
docker run --read-only --cap-drop ALL --security-opt no-new-privileges \
  -e MCP_GATEWAY_CONFIG="$(cat config.json)" \
  -v "$PWD/secrets:/run/secrets:ro" \
  -p 8000:8000 ghcr.io/alien-intelligence/mcp-gateway:<version>

curl http://localhost:8000/healthz   # {"status":"ok"}: the process is up
curl http://localhost:8000/readyz    # {"status":"ready"}: the shared state answers; 503 drains the replica
```

The image's `HEALTHCHECK` is a real GET of `/healthz` on the port in `MCP_GATEWAY_CONFIG`, so `docker ps` shows `healthy` once the gateway serves. In Kubernetes use `/healthz` for the liveness probe and `/readyz` for the readiness probe, and set `readOnlyRootFilesystem: true`.

Build it yourself (the version, revision and date are required build arguments, a build without them fails):

```bash
docker build --build-arg VERSION=$(sed -n 's/^version = "\(.*\)"/\1/p' pyproject.toml) \
  --build-arg VCS_REF=$(git rev-parse HEAD) --build-arg BUILD_DATE=$(date -u +%Y-%m-%dT%H:%M:%SZ) -t mcp-gateway .
```

Both base images are pinned by digest in the `Dockerfile`; bump them on purpose. CI scans the pushed digest with trivy and fails on any HIGH or CRITICAL vulnerability that has a fix, so a failing scan means a new base digest, not a retry.

## Security

To report a vulnerability, follow [SECURITY.md](SECURITY.md). Do not open a public issue for a security problem.

## Contributing

This repository is a read-only mirror of the canonical one; see [CONTRIBUTING.md](CONTRIBUTING.md) for issues and pull requests.

## License

MIT, see [LICENSE](LICENSE).
