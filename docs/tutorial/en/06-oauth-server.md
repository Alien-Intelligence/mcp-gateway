# 6. Your OAuth server

This chapter sets up Keycloak for the gateway, and gives a checklist for other OAuth servers. Everything the gateway needs from the server is in the table of [chapter 2](02-prerequisites.md#what-your-oauth-server-must-provide).

## 6.1 Keycloak

This repository ships a realm template for Keycloak 26.8 in [`deploy/keycloak/`](../../../deploy/keycloak/): the realm your users sign in to, with the audience, the identity claims, anonymous client registration restricted to the known AI clients, and production token lifetimes. It has no users, no clients and no secret. It was tested on `quay.io/keycloak/keycloak:26.8.2`. [`deploy/keycloak/README.md`](../../../deploy/keycloak/README.md) is the reference for every setting; this chapter gives the steps and the reasons.

The gateway's development stack (`dev/keycloak/`) has a different realm. It is a **test fixture** that the end-to-end tests exercise (60-second tokens, a trusted-hosts list made for the compose network); do not use it as a deployment.

### Render the placeholders

The template has two placeholders, and `render.sh` substitutes them:

| Placeholder | Value |
|---|---|
| `${REALM_NAME}` | The realm name, 1 to 64 characters of `a-z 0-9 _ -`, for example `example-publisher`. It becomes part of the issuer: `https://<keycloak>/realms/<REALM_NAME>`. |
| `${GATEWAY_PUBLIC_URL}` | Exactly the gateway's `public_url`, for example `https://mcp.example.com/mcp`. It becomes the `aud` of every access token. |

```bash
cd deploy/keycloak
REALM_NAME=example-publisher GATEWAY_PUBLIC_URL=https://mcp.example.com/mcp \
  ./render.sh > example-publisher-realm.json
```

`render.sh` prints nothing and exits non-zero when a variable is unset or malformed, or when an upper-case `${NAME}` is left in the output. Render first even though Keycloak expands `${...}` from its own environment during an import: it leaves an unknown placeholder in place silently, and the realm would then work with a literal `${GATEWAY_PUBLIC_URL}` as audience.

### Import the realm

Import the rendered file one of three ways:

- At server start: put it in `/opt/keycloak/data/import/` and start with `--import-realm`. A realm that already exists is left as it is, so this only creates.
- Offline, with the server stopped: `bin/kc.sh import --file example-publisher-realm.json`.
- Admin console: *Create realm*, then *Browse* and pick the file.

To try the realm on one machine before you deploy Keycloak for real, run the tested image in development mode with the rendered file in the import directory. `start-dev` serves plain HTTP with an embedded database that is lost when the container is removed: it is for a trial, never for production. The two `KC_BOOTSTRAP_ADMIN_*` variables create the administrator of the `master` realm on the first start; keep them in a file of mode 600 (`--env-file`), not on the command line:

```bash
umask 077
printf 'KC_BOOTSTRAP_ADMIN_USERNAME=admin\nKC_BOOTSTRAP_ADMIN_PASSWORD=%s\n' "$(head -c 18 /dev/urandom | base64)" > keycloak-admin.env
mkdir -p import && cp example-publisher-realm.json import/ && chmod 755 import && chmod 644 import/*.json
docker run -d --name keycloak --env-file keycloak-admin.env -p 127.0.0.1:8080:8080 \
  -v "$PWD/import:/opt/keycloak/data/import:ro" quay.io/keycloak/keycloak:26.8.2 start-dev --import-realm
```

The issuer is then `http://localhost:8080/realms/example-publisher` (Keycloak in development mode takes the host name from the request, so sign in through that address). The realm's `sslRequired: external` accepts plain HTTP from private addresses only, which a trial on one machine is. A gateway that runs in a container cannot fetch the key set from `localhost`: connect Keycloak to the gateway's network (with the compose example of chapter 5: `docker network connect mcp-gateway_edge keycloak`) and set `auth.jwks_uri` to `http://keycloak:8080/realms/example-publisher/protocol/openid-connect/certs`, leaving `auth.issuer` as it is (chapter 2: only the gateway fetches the key set).

Then create your users (or connect your user federation or identity provider). Give each a verified e-mail and, if your resolver matches on it, the `org` attribute. With Keycloak's admin CLI, inside the container (each command that needs a password asks for it, so it stays out of the process list and the shell history; the password must satisfy the realm's policy: at least 12 characters, not the username, not the e-mail):

```bash
docker exec -it keycloak /opt/keycloak/bin/kcadm.sh config credentials --config /tmp/kcadm.config \
  --server http://localhost:8080 --realm master --user admin
docker exec keycloak /opt/keycloak/bin/kcadm.sh create users --config /tmp/kcadm.config -r example-publisher \
  -s username=alice@example.com -s email=alice@example.com -s emailVerified=true -s enabled=true \
  -s 'attributes.org=["example-org"]'
docker exec -it keycloak /opt/keycloak/bin/kcadm.sh set-password --config /tmp/kcadm.config \
  -r example-publisher --username alice@example.com
```

Set `emailVerified` only for an address you have checked: the gateway trusts an e-mail only when that flag is `true`, and the `static` and webhook examples map on it.

If your users need Keycloak's account console, one fix is needed after the import: Keycloak creates its built-in `account-console` client without any client scope when a realm is imported with its own client scopes, and the console then answers `403`. Give `account-console` the default client scopes `basic`, `acr`, `profile`, `email`, `roles` and `web-origins` (*Clients* > `account-console` > *Client scopes* > *Add client scope*, type *Default*). MCP clients do not need this.

For production, start Keycloak behind a TLS reverse proxy with a real database, for example `bin/kc.sh start --hostname https://sso.example.com --proxy-headers xforwarded --http-enabled true --db postgres ...`. The gateway's `auth.issuer` is then `https://sso.example.com/realms/example-publisher` and `auth.jwks_uri` is `<issuer>/protocol/openid-connect/certs`.

### The audience

The gateway accepts a token only when its `aud` contains the gateway's URL (`auth.audiences`, which must contain `public_url`). Without a mapper, Keycloak puts `account` in `aud`, and the gateway answers `401` with `token audience does not match this resource`.

The template adds an **Audience** mapper (`oidc-audience-mapper`) to a client scope named `mcp-gateway`; its *Included Custom Audience* is `${GATEWAY_PUBLIC_URL}`. The scope is a realm **default** scope, so every client gets it, whatever it asked for: a token issued by this realm always has `aud` equal to the gateway's URL.

A default scope rather than an optional one, because clients differ in what they request and register, and a token without the audience is refused by the gateway (the client then loops on sign-in). The cost is that every token of the realm is valid at the gateway, so keep the realm dedicated to the gateway. If other applications must share it, make `mcp-gateway` an optional scope instead and check every client you support.

Keycloak 26.8 also has an experimental implementation of resource indicators (RFC 8707, `--features=resource-indicators`) that would set the audience from the `resource` parameter that ChatGPT and Cursor send. Without that feature, Keycloak does not use `resource` to choose the audience; the template does not need it.

The gateway advertises these scopes, and the realm must let an anonymous client have each of them:

<!-- config-fragment: auth.scopes_advertised -->
```json
["mcp-gateway", "offline_access"]
```

AI clients request the scopes in the `WWW-Authenticate` challenge, or else those in the metadata's `scopes_supported`: both come from `auth.scopes_advertised`; several clients also send them as the `scope` of their dynamic registration. What the template's README proved with Keycloak 26.8.2:

- **Do not advertise `openid`.** It is not a Keycloak client scope: a registration whose `scope` contains it is refused (`403`, "Not permitted to use specified clientScope"), and VS Code sends the challenge's scopes in its registration. Without `openid`, Keycloak issues an access token and no ID token, which is all the gateway needs.
- **Advertise `offline_access`.** Claude adds it to its request whenever Keycloak lists it in `scopes_supported` (Keycloak always does). A client registered with a `scope` that lacks it does not get it as an optional scope, and its authorization request fails with `invalid_scope`.
- **Advertise something.** With no scope in the challenge or the metadata, Claude requests every scope in Keycloak's `scopes_supported`, some of which the client does not have, and Keycloak answers `invalid_scope` (<https://github.com/anthropics/claude-ai-mcp/issues/1084>).

The configuration requires at least one entry. For another server, advertise only scopes that exist there and that an anonymous client can have. Keycloak's own guide: <https://www.keycloak.org/securing-apps/mcp-authz-server>.

### The identity claims

The gateway reads the claims named in `auth.identity`. The template adds them through a client scope `gateway-identity`, a default scope of the realm. The user profile lets only administrators change `username`, `email` and `org`, and self sign-up is off, because whoever can change those values chooses which API key a request uses:

- `org`: an attribute mapper (`oidc-usermodel-attribute-mapper`) from the user attribute `org` to the claim `org`, a string, in the access token. That is `auth.identity.organization: "org"`.
- `groups`: a group-membership mapper (`oidc-group-membership-mapper`), claim `groups`, `full.path` false, so a group `/analysts` is `analysts` in the claim. That is `auth.identity.groups: "groups"`.
- `sub`, `email` and `email_verified` come from the realm's standard scopes. Set `auth.identity.email_verified` to `email_verified`: an e-mail counts only when that claim is `true`.

### Token lifetime

The template's `accessTokenLifespan` is **300 seconds**. The gateway cannot revoke a token, so a stolen token, or the token of a user you just disabled, works until it expires; five minutes bounds that, and clients refresh in the background (Claude refreshes up to five minutes before expiry, vendor documentation). The sessions behind the refresh tokens: idle 7 days, maximum 30 days, for both SSO and offline sessions (MCP clients request `offline_access`); refresh tokens rotate (each refresh returns a new one and the previous one is refused). The README of the template has the full table and the reasons. The development realm's 60-second tokens exist for expiry tests and are too short for real clients. The gateway tolerates `auth.clock_skew_seconds` of clock difference on `exp`, `nbf` and `iat`.

To lock a user out now: disable the user, then end their sessions and offline sessions in the admin console. Tokens already issued stay valid for at most 5 minutes.

### Trusted hosts, registration and clients

Clients register through **Dynamic Client Registration** (anonymous) or identify themselves with a **Client ID Metadata Document**.

- **Anonymous DCR** is refused by Keycloak unless the realm's registration policies allow it. The template keeps Keycloak's anonymous policies and sets the **Trusted Hosts** policy: every redirect URI of the client being registered (and its `client_uri`) must be on a trusted host (`client-uris-must-match: true`), while `host-sending-registration-request-must-match` is `false`, because the request comes from vendors' servers or from the user's own computer, whose addresses you cannot list. A registration that fails the policy is refused; the reported symptom is an HTTP `403` on the registration request. The **Max Clients Limit** is 1000 (it counts every client of the realm); when it is reached every new registration fails, so monitor the client count and delete clients that no longer sign in. **Consent** is required for registered clients, and PKCE `S256` is required for every client created anonymously.
- The hosts the template trusts (the README has the redirect URIs, the vendor page and the date each was checked, in [its table](../../../deploy/keycloak/README.md#trusted-hosts-and-where-each-comes-from)):

  | Host | Client |
  |---|---|
  | `claude.ai` | Claude (web, Desktop, mobile) |
  | `localhost`, `127.0.0.1` | Claude Code, VS Code, Cursor desktop (loopback) |
  | `chatgpt.com` | ChatGPT |
  | `global.consent.azure-apim.net` | Microsoft Copilot Studio (check the callback URL it shows you; if its host differs, add it) |
  | `vscode.dev`, `insiders.vscode.dev`, `code.visualstudio.com` | VS Code |
  | `www.cursor.com`, `anysphere.cursor-mcp` | Cursor web and cloud agents, Cursor desktop |

  Remove the hosts of clients you do not support, and add only hosts you checked against the vendor's current documentation. Keycloak compares only the **host** of a redirect URI: scheme, port and path are not looked at, subdomains are not trusted implicitly, and one untrusted URI refuses the whole registration. The README lists what that allows and what it refuses.
- Claude's documentation notes that with DCR it registers a new client on every fresh connection, so watch the "Max Clients" limit on a busy realm.
- **CIMD** is **experimental** in Keycloak (`--features=cimd`) and **off in the template**. `deploy/keycloak/cimd-client-policies.json` holds a client profile and policy for Claude (it needs `accept-public-client-with-confidential-client-only-grant` on, in 26.8) that you apply through the admin API; the steps are in the README's [CIMD section](../../../deploy/keycloak/README.md#cimd-experimental). **Starting Keycloak with `--features=cimd` changes every client, not only Claude**: Keycloak then advertises `client_id_metadata_document_supported: true` and adds `none` to `token_endpoint_auth_methods_supported`, with or without a policy, and clients that support CIMD use it. ChatGPT prioritizes CIMD and Keycloak does not support its document, so its sign-in fails (`Client not found`) unless the ChatGPT app is set to use DCR; VS Code has its own CIMD document and needs its own policy. DCR is the path that works with the template as it is.
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
  "scopes_advertised": ["mcp-gateway", "offline_access"],
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
- [ ] The scopes you advertise exist and can be requested by an anonymous or pre-registered client, and the server accepts them in a registration request (Keycloak refuses `openid` there); the server does not answer `invalid_scope` when the client asks for exactly what you advertise.
- [ ] Client registration: DCR without a pre-issued token, or CIMD with `client_id_metadata_document_supported: true` and `none` in `token_endpoint_auth_methods_supported`, or pre-registered clients with the redirect URIs of chapter 7. (Known: authentik 2026.8 DCR needs a pre-issued token and issues HS256 tokens; pre-register instead.)
- [ ] The token carries a stable `sub` and the claims you map on (`email` with `email_verified`, `org`, `groups`), at the paths you configure.
- [ ] The token endpoint accepts `application/x-www-form-urlencoded` and answers discovery, registration and token requests well under 10 seconds (Claude's limit).
- [ ] The server is reachable from the AI vendors' clouds (chapter 2).

Continue with [7. Connecting clients](07-clients.md).
