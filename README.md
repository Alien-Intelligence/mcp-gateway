# Alien MCP Gateway

A gateway you host in front of Alien MCP servers.

- It accepts MCP clients (Claude, ChatGPT, Copilot, VS Code, Cursor) that sign in with **your own OAuth server**, for example Keycloak.
- It maps each verified identity to an Alien API key through a pluggable credential resolver.
- It forwards the MCP traffic to Alien unchanged.

Your users sign in with their usual accounts, and your MCP endpoint lives on your own domain.

Status: in development (walking skeleton). What exists today is described below; the setup tutorial will be published before the first release.

## How it works

```
MCP client ──OAuth 2.1 + PKCE──> your OAuth server (token aud = the gateway URL)
    │
    └─ Bearer JWT ─> gateway: check the JWT (JWKS, iss, aud, exp/nbf)
                             identity = claims (subject, verified email, organization, groups)
                             resolver(identity) -> Alien API key, or 403
                             drop the client's credentials and forgeable headers, add the key
                             stream to the Alien MCP server, stream the answer back untouched
```

- The gateway is an OAuth **resource server** only. It issues no tokens and registers no clients: your OAuth server does that.
- It publishes Protected Resource Metadata (RFC 9728) at the path-specific URL only (`/.well-known/oauth-protected-resource/mcp` for a `/mcp` endpoint), with `resource` equal to `public_url`. The origin-level URL answers `404`: RFC 9728 requires `resource` to match the URL the document was fetched for.
- A request without a valid token gets `401` with `WWW-Authenticate: Bearer resource_metadata="…", scope="…"`.
- A signed-in user with no mapping gets `403` with a JSON-RPC error (`-32010`) and no challenge.
- If Alien rejects the mapped key (`401` or `402`), the client gets a JSON-RPC error (`-32001` / `-32002`), never an OAuth challenge, so it does not loop on re-authentication.
- Inbound headers are allow-listed. `Authorization`, `Cookie`, `X-Oauth-Access-Token`, `X-Job-Id`, `X-Organization-Id`, `X-Mcp-Config-Id` and `X-Forwarded-*` never reach Alien.
- One JSON audit line per request: subject, organization, labels, method, tool, status, outcome, latency. Never content, tokens or keys.

## Prerequisites: your OAuth server

The gateway works with any OAuth server that provides all of the following:

1. **Discovery:** RFC 8414 authorization-server metadata or OpenID Connect discovery, advertising `S256` in `code_challenge_methods_supported`.
2. **JWT access tokens** signed with an asymmetric key (RS256, ES256, PS256 or EdDSA) and a published JWKS. The gateway validates tokens locally; it does not use token introspection.
3. **The gateway URL as audience:** the access token's `aud` must contain the gateway's public URL. Keycloak does this with an optional client scope carrying an Audience mapper; Ory Hydra does it in the consent step (`grant_access_token_audience`).
4. **Client registration that MCP clients can use:** anonymous Dynamic Client Registration (RFC 7591), Client ID Metadata Documents, or clients you pre-register for each MCP client.
5. **Identity claims** in the access token: a stable subject, and the claims you map on (`email` with `email_verified`, an organization claim, groups). Claim paths are configurable, including nested ones such as `ext.org`.

Known incompatibility: authentik 2026.8 dynamic client registration requires a pre-issued bearer token, and the clients it creates sign tokens with their own secret (HS256) under a per-application issuer, so a resource server cannot verify them through a JWKS. With authentik, pre-register the MCP clients instead.

## Configuration

One strict JSON document in the `MCP_GATEWAY_CONFIG` environment variable. Every key is required, unknown keys are refused, and there is no default for any issuer, audience or secret: the gateway refuses to start on an incomplete configuration. See the two gateway services in [`compose.yaml`](compose.yaml) for complete examples (one for Keycloak, one for Hydra).

- `public_url`: the canonical URL of the MCP endpoint; it must be listed in `auth.audiences`.
- `auth`: `issuer`, `jwks_uri`, `audiences`, `algorithms` (asymmetric only), `required_scopes`, `scopes_advertised` (the scope in the 401 challenge and the metadata), clock skew, JWKS cache settings (`jwks_cache_ttl_seconds`, and `jwks_max_stale_seconds`: how long past the TTL the cached key set is still served when a refresh fails), and `identity` claim paths. `identity.email_verified` is the path of the boolean claim that must be `true` before the email is trusted; with `null` the email is never trusted and a mapping with `email` matchers is refused.
- `resolver`: `static` today. It reads a mapping file:

  ```json
  {"entries": [
    {"match": {"email": "alice@example.com"}, "api_key_file": "alice.key", "upstream": "main", "labels": {"client": "example"}},
    {"match": {"organization": "example-org"}, "api_key_file": "example-org.key", "upstream": "main", "labels": {"client": "example-org"}}
  ]}
  ```

  A `match` names exactly one of `subject`, `email`, `organization` or `group`, tried in that order. Key files are read relative to the mapping file and re-read on each request, so a rotated key is picked up without a restart. The mapping file is reloaded when it changes.
- `upstreams`: name to URL and timeouts.
- `limits` (`max_request_body_bytes`, `request_body_timeout_seconds`: 408 when the body does not arrive in time, `max_sse_event_bytes`: a larger SSE event aborts the stream), `audit`.
- `sessions` (`max_entries`, `idle_ttl_seconds`): an upstream `Mcp-Session-Id` is bound in memory to the subject it was issued to. A request that presents an unknown, expired or foreign session id gets `404` and a JSON-RPC error (`-32007`) and is not forwarded; the client starts a new session.
- `policies.allowed_origins`: browser origins (`scheme://host[:port]`) allowed on the MCP endpoint. An `Origin` header that is not listed gets `403` (`-32008`) before authentication; no `Origin` header passes.

Requests whose `Mcp-Method` or `Mcp-Name` header disagrees with the JSON-RPC body get `400` and `-32020` (`HeaderMismatch`, MCP 2026-07-28). If the upstream stream fails before the reply to a request was relayed, the client receives one final SSE event with a JSON-RPC error (`-32006`); in every other mid-stream failure the connection is aborted so it cannot pass for a clean end.

On Alien, an API key is a billed user and an upstream session: users that share a key share a line in the consumption feed. Map one key per user when you need per-user accounting.

## Development stack

`docker compose` runs two real OAuth servers and one gateway for each:

| Service | URL | Notes |
|---|---|---|
| Keycloak 26.8 | `http://localhost:18080` | realm `alien-gateway-dev` imported from `dev/keycloak/` |
| Ory Hydra v26.2 | `http://localhost:14444` | login/consent app from `dev/hydra-login/` on `:13000` |
| gateway for Keycloak | `http://localhost:18000/mcp` | |
| gateway for Hydra | `http://localhost:18001/mcp` | |

Both providers have the same three test users: `alice@client-x.test`, `bob@client-y.test`, and `nomap@client-z.test`, who has no mapping. Access tokens live 60 seconds. Anonymous DCR is enabled on both (Keycloak through Trusted Hosts covering loopback and the compose network gateway).

```bash
cp .env.example .env          # fill in random secrets, the upstream MCP URL and a state directory
chmod 600 .env
uv sync --extra dev
docker compose up -d --build
docker compose ps             # every service healthy
```

The gateways read `mapping.json` and the key files from `MCP_GATEWAY_STATE_DIR`, mounted read-only. Create them with the fixture below.

### Alien fixture (dev platform)

`scripts/dev_fixture.py` sets up, through the real Alien API, two enterprise client organizations attached to a sponsored group of a sponsor organization that owns a private cluster, one client-managed user per client organization, and an `api-key` token for each. It is idempotent: organizations cannot be deleted on Alien, so every object is found by name and reused. It writes `mapping.json`, the key files (mode 600) and a non-secret `fixture.json` into the state directory.

```bash
uv run python scripts/dev_fixture.py \
  --tokens-file <markdown file with the dev Admin/Normal tokens> \
  --api-base <Alien dev API> --mcp-url <dev MCP URL> \
  --sponsor-org-id <id> --cluster-id <id> --dataset-id <id> \
  --state-dir <MCP_GATEWAY_STATE_DIR>
```

The tokens are read from the file and never passed on the command line or printed.

## Tests

```bash
uv run pytest tests/unit -q                       # unit tests, no network
uv run ruff check src tests scripts dev && uv run pyright src
```

The end-to-end suite (`-m e2e`) behaves like a real MCP client against both providers: it reads the gateway's 401, fetches the resource metadata and the authorization-server metadata, registers through DCR, runs authorization code + PKCE S256 + `resource` through the provider's real login form, then calls `initialize`, `tools/list` and `tools/call` through the gateway with the official `mcp` SDK. It then reads the Alien consumption feeds back and asserts the call was billed to the expected client-managed user and client organization, with the sponsorship applied. Negative cases: wrong audience (401), expired token (401), unmapped user (403), revoked Alien key (JSON-RPC error, no challenge), and forged `X-Job-Id` / `X-Organization-Id` (not recorded by Alien), each with a positive control that goes direct to the upstream.

```bash
E2E_STATE_DIR=<state dir> E2E_TOKENS_FILE=<tokens file> E2E_API_BASE=<Alien dev API> \
E2E_UPSTREAM_URL=<dev MCP URL> E2E_EXISTING_JOB_ID=<an existing dev job id> \
E2E_RUN_DIR=<evidence dir> uv run pytest tests/e2e -m e2e -v
```

A run takes about ten minutes, mostly waiting for tokens to expire and for metering to appear in the feeds. It needs the compose stack up, the fixture applied and network access to the Alien dev platform. CI runs the unit tests only.
