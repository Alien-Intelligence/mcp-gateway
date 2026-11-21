# 6. Your OAuth server

This chapter sets up Keycloak for the gateway, and gives a checklist for other OAuth servers. Everything the gateway needs from the server is in the table of [chapter 2](02-prerequisites.md#what-your-oauth-server-must-provide).

## 6.1 Keycloak

The gateway's development stack runs Keycloak **26.8.2** (`dev/keycloak/Dockerfile`) with a realm that the end-to-end tests exercise against the gateway: the whole OAuth journey (discovery, anonymous DCR, authorization code with PKCE `S256` and `resource`, login) and the negative cases. That realm is the basis of the template for your own.

TODO(keycloak): the realm template for your deployment is being written in `deploy/keycloak/`. The structure below is final; the file names, the placeholder names and the exact import command are TODO(keycloak).

### Import the realm

TODO(keycloak): which file to import, how: the development image copies its realm file into `/opt/keycloak/data/import/` of the Keycloak container, where Keycloak imports it at start. The command and flags for your installation (container, Operator, Helm chart of Keycloak) are TODO(keycloak).

### Render the placeholders

TODO(keycloak): the template contains values that are yours: the gateway's public URL (the audience), the origins and hosts to trust, the realm name. Which placeholders exist and how to render them is TODO(keycloak).

### The audience

The gateway accepts a token only when its `aud` contains the gateway's URL (`auth.audiences`, which must contain `public_url`). Without a mapper, Keycloak puts `account` in `aud`, and the gateway answers `401` with `token audience does not match this resource`.

The way that works today, and that the development realm uses: an **optional client scope** named `mcp-gateway` that holds an **Audience** mapper (`oidc-audience-mapper`) with `included.custom.audience` set to the gateway's `public_url`, added to the access token. A client gets the audience when it **requests that scope**, which is why the gateway advertises it:

<!-- config-fragment: auth.scopes_advertised -->
```json
["openid", "mcp-gateway"]
```

AI clients request the scopes in the `WWW-Authenticate` challenge, or else those in the metadata's `scopes_supported`: both come from `auth.scopes_advertised`. Advertise only scopes that exist in your realm, because clients ask for them. The configuration requires at least one. Claude, when no scope is advertised, asks for every scope the server has and gets `invalid_scope` (<https://github.com/anthropics/claude-ai-mcp/issues/1084>): the fix is to advertise `openid`, which is why every example does.

Keycloak also has an experimental implementation of resource indicators (RFC 8707, started with `--features=resource-indicators`, in 26.8) that would set the audience from the `resource` parameter that ChatGPT and Cursor send. It is experimental: this tutorial uses the audience mapper.

### The identity claims

The gateway reads the claims named in `auth.identity`. The development realm adds them through a client scope `gateway-identity`, a default scope of the realm:

- `org`: an attribute mapper (`oidc-usermodel-attribute-mapper`) from the user attribute `org` to the claim `org`, a string, in the access token. That is `auth.identity.organization: "org"`.
- `groups`: a group-membership mapper (`oidc-group-membership-mapper`), claim `groups`, `full.path` false, so a group `/analysts` is `analysts` in the claim. That is `auth.identity.groups: "groups"`.
- `sub`, `email` and `email_verified` come from the realm's standard scopes. Set `auth.identity.email_verified` to `email_verified`: an e-mail counts only when that claim is `true`.

### Token lifetime

The development realm issues tokens that live **60 seconds**, so that the tests can exercise expiry quickly. That is not a production value. Claude refreshes a token proactively up to five minutes before its expiry (vendor documentation), so a lifetime that short makes it refresh constantly. TODO(keycloak): the lifetime of the template. The gateway tolerates `auth.clock_skew_seconds` of clock difference on `exp`, `nbf` and `iat`.

### Trusted hosts, registration and clients

Clients register through **Dynamic Client Registration** (anonymous) or identify themselves with a **Client ID Metadata Document**.

- **Anonymous DCR** is off by default in Keycloak: the "Trusted Hosts" client registration policy lists no host. The policy checks both the host that sends the registration request and the redirect URIs of the client being registered. A registration that fails this policy is refused; the symptom reported for it (in the project's notes, not reproduced for this tutorial) is an HTTP `403` on the registration request. The development realm trusts `localhost`, `127.0.0.1` and the compose network's gateway address, with `host-sending-registration-request-must-match` and `client-uris-must-match` both true, and caps registered clients at 200 ("Max Clients Limit"). For your realm add the hosts of the clients you serve (chapter 7: `claude.ai`, `chatgpt.com`, the hosts of Copilot Studio's callback, loopback). TODO(keycloak): the exact host list of the template.
- Claude's documentation notes that with DCR it registers a new client on every fresh connection, so watch the "Max Clients" limit on a busy realm.
- **CIMD** is **experimental** in Keycloak (`--features=cimd`). For claude.ai it also needs the setting `accept-public-client-with-confidential-client-only-grant=true` (26.8). **ChatGPT through CIMD is not supported by Keycloak**; ChatGPT works through DCR with `chatgpt.com` in the trusted hosts. TODO(keycloak): whether the template ships the CIMD profile and with which flags.
- Keycloak before 26.6 had a DCR incompatibility with MCP clients: use 26.6 or later (26.8 is the version the gateway is tested with).
- Keycloak's metadata with a path-inserted issuer (`/.well-known/oauth-authorization-server/realms/<realm>`) is served since 26.4.

Keycloak's own guide: <https://www.keycloak.org/securing-apps/mcp-authz-server>.

### The gateway's side

<!-- config-fragment: auth -->
```json
{
  "issuer": "https://idp.example.com/realms/example-publisher",
  "jwks_uri": "https://idp.example.com/realms/example-publisher/protocol/openid-connect/certs",
  "audiences": ["https://mcp.example.com/mcp"],
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
    "groups": "groups"
  }
}
```

- `issuer` must equal the token's `iss` exactly (Keycloak: your public realm URL, including the `/realms/<realm>` path).
- `jwks_uri` is Keycloak's `.../protocol/openid-connect/certs`. It may be an internal address, since only the gateway fetches it.
- `algorithms`: Keycloak's default realm signature algorithm is `RS256`.
- `required_scopes`: scopes the token must carry. With a non-empty list, a token without one gets `403 insufficient_scope`.
- The key set is cached for `jwks_cache_ttl_seconds`. If a refresh fails, the cached set keeps serving for `jwks_max_stale_seconds` more; after that requests get `503` with `Retry-After`. A token signed with an unknown `kid` triggers a refresh, at most one every 10 seconds.

### Check a token

Get a token from your realm with any client you control, decode its payload, and check: `iss` equals `auth.issuer`; `aud` contains `public_url`; `sub` is present; `email`, `email_verified`, `org` and `groups` are what your resolver expects; `exp` is in the future. Then call the gateway with it: `HTTP 403` with JSON-RPC `-31010` means the token is fine and the user has no mapping (chapter 4); `401` means the token is not accepted, and the `error_description` says why (`token expired`, `token audience does not match this resource`, `token issuer is not trusted`, `token signature is invalid`, `token algorithm is not accepted`, `token is missing a required claim`, `token signing key is unknown`).

## 6.2 Other OAuth servers: a checklist

The gateway works with any server that provides the prerequisites of chapter 2. This tutorial only tested **Keycloak 26.8.2** and **Ory Hydra v26.2** (both in the development stack, where the same end-to-end suite runs against each). It makes no claim about any other product. For Auth0, Microsoft Entra ID, Okta, authentik or another server, verify each point yourself with a real token and a real client before you rely on it:

- [ ] Metadata at an RFC 8414 or OpenID Connect discovery URL, listing `S256` in `code_challenge_methods_supported`, `authorization_endpoint`, `token_endpoint` and `jwks_uri`.
- [ ] Access tokens are **JWTs** (not opaque strings) signed with an asymmetric algorithm from the list in chapter 2, with a `kid` that matches the JWKS.
- [ ] Their `iss` is exactly the issuer you configure, and `aud` contains the gateway's `public_url` exactly. Some servers want the resource URL declared first (the Claude documentation notes that Microsoft Entra ID needs the MCP server URL registered as an Application ID URI, or the token request fails with `AADSTS9010010`).
- [ ] The scopes you advertise exist and can be requested by an anonymous or pre-registered client; the server does not reject a request for scopes it does not know with `invalid_scope` when the client asks for exactly what you advertise.
- [ ] Client registration: DCR without a pre-issued token, or CIMD with `client_id_metadata_document_supported: true` and `none` in `token_endpoint_auth_methods_supported`, or pre-registered clients with the redirect URIs of chapter 7. (Known: authentik 2026.8 DCR needs a pre-issued token and issues HS256 tokens; pre-register instead.)
- [ ] The token carries a stable `sub` and the claims you map on (`email` with `email_verified`, `org`, `groups`), at the paths you configure.
- [ ] The token endpoint accepts `application/x-www-form-urlencoded` and answers discovery, registration and token requests well under 10 seconds (Claude's limit).
- [ ] The server is reachable from the AI vendors' clouds (chapter 2).

Continue with [7. Connecting clients](07-clients.md).
