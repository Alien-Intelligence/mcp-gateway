# Keycloak realm template

A Keycloak 26.8 realm for the MCP gateway: the identity provider your users sign in to when an MCP client (Claude, ChatGPT, Copilot Studio, VS Code, Cursor) connects to the gateway. It issues access tokens whose `aud` is the gateway, lets those clients register themselves (anonymous dynamic client registration, restricted to their known redirect hosts), and carries the claims the gateway maps to an API key.

| File | What it is |
|---|---|
| `realm-template.json` | The realm, with two placeholders. No users, no clients, no secrets. |
| `render.sh` | Substitutes the placeholders and refuses to print anything if one is unset, invalid or left over. |
| `cimd-client-policies.json` | EXPERIMENTAL, off unless you apply it: Client ID Metadata Document support for Claude. See [CIMD](#cimd-experimental). |

Tested on `quay.io/keycloak/keycloak:26.8.2` (the image the gateway's development stack pins).

## Render and import

| Placeholder | Value |
|---|---|
| `${REALM_NAME}` | The realm name, 1 to 64 characters of `a-z 0-9 _ -`, for example `example-publisher`. It becomes part of the issuer: `https://<keycloak>/realms/<REALM_NAME>`. |
| `${GATEWAY_PUBLIC_URL}` | Exactly the gateway's `public_url` (scheme, host, path, no trailing slash added or removed), for example `https://mcp.example.com/mcp`. It becomes the `aud` of every access token. |

```sh
REALM_NAME=example-publisher GATEWAY_PUBLIC_URL=https://mcp.example.com/mcp \
  ./render.sh > example-publisher-realm.json
```

`render.sh` exits non-zero, with nothing on stdout, when a variable is unset or malformed, or when an upper-case `${NAME}` is left in the output. Render first even though Keycloak expands `${...}` from its own environment during an import: Keycloak leaves an unknown placeholder in place silently, and the realm would then work with a literal `${GATEWAY_PUBLIC_URL}` as audience. Keycloak's own keys (`${client_id}`, `${rolesScopeConsentText}`) are lower or camel case and stay untouched.

Import the rendered file one of three ways:

- At server start: put it in `/opt/keycloak/data/import/` and start with `--import-realm`. A realm that already exists is left as it is (strategy `IGNORE_EXISTING`), so this only creates.
- Offline, server stopped: `bin/kc.sh import --file example-publisher-realm.json`.
- Admin console: *Create realm*, then *Browse* and pick the file.

Then create your users (or connect your user federation or identity provider), and give each one a verified e-mail and, if you match on it, the `org` attribute ([user profile](#users-and-the-identity-claims)).

If your users need Keycloak's account console (their password, sessions, second factor): Keycloak creates its built-in clients without any client scope when a realm is imported with its own client scopes, and the console then answers `403` to everything. After the import, give `account-console` the default client scopes `basic`, `acr`, `profile`, `email`, `roles` and `web-origins` (*Clients* > `account-console` > *Client scopes* > *Add client scope*, type *Default*). MCP clients do not need this: the clients they register get the realm's default scopes.

Production start, behind a TLS reverse proxy: `bin/kc.sh start --hostname https://sso.example.com --proxy-headers xforwarded --http-enabled true` with a real database (`--db postgres ...`). The gateway's `auth.issuer` is then `https://sso.example.com/realms/example-publisher` and its `auth.jwks_uri` is `<issuer>/protocol/openid-connect/certs`.

## Gateway configuration that matches the realm

```json
"auth": {
  "issuer": "https://sso.example.com/realms/example-publisher",
  "jwks_uri": "https://sso.example.com/realms/example-publisher/protocol/openid-connect/certs",
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
  },
  "userinfo": null
}
```

`audiences` holds the same URL as `public_url` and as `${GATEWAY_PUBLIC_URL}`. The full examples are in [`../compose/`](../compose/).

### `scopes_advertised`: exactly `["mcp-gateway", "offline_access"]`

The gateway sends this list as the `scope` of its `401` challenge and as `scopes_supported` in its protected-resource metadata. MCP clients request what it says, and several of them also send it as the `scope` of their dynamic registration. Each entry must be a client scope this realm lets an anonymously registered client have, or the client fails before the user sees a login page:

- **No `openid`.** `openid` is not a Keycloak client scope. A registration whose `scope` contains it is refused by the *Allowed Client Scopes* policy (`403`, "Not permitted to use specified clientScope"). VS Code sends the challenge's scopes in its registration, so advertising `openid` breaks VS Code against this realm. Without `openid` in the request, Keycloak issues an OAuth access token and no ID token, which is all the gateway needs.
- **`offline_access` included.** Claude adds `offline_access` to its request whenever Keycloak lists it in `scopes_supported`, which Keycloak always does. A client that registered with a `scope` that does not contain `offline_access` does not get it as an optional scope, and its authorization request then fails with `invalid_scope`. Advertising both makes every client register and request both.
- **Something must be advertised.** With no scope in the challenge or the metadata, Claude requests every scope in Keycloak's `scopes_supported`, some of which the client does not have, and Keycloak answers `invalid_scope` ([anthropics/claude-ai-mcp#1084](https://github.com/anthropics/claude-ai-mcp/issues/1084)).

`required_scopes` stays empty: the audience already proves the token was issued for the gateway.

## The audience

The `mcp-gateway` client scope carries an *Audience* mapper whose *Included Custom Audience* is `${GATEWAY_PUBLIC_URL}`. The scope is a realm **default** scope, so every client gets it, whatever it asked for: a token issued by this realm always has `aud` = the gateway URL, also for a client that requests only `openid` or nothing. The gateway accepts no other audience.

Why not the `resource` parameter (RFC 8707)? MCP clients send `resource=<gateway URL>`, but Keycloak 26.8 implements resource indicators as an experimental feature (`--features=resource-indicators`, which may change in breaking ways), so without it Keycloak does not use the parameter to choose the audience (it still refuses an empty `resource` with `invalid_target`). The fixed audience gives the same result for a realm that serves one gateway.

Why a default scope rather than an optional one (the Keycloak MCP guide's example)? An optional scope reaches the token only when the client requests it and the client was registered with it. Clients differ in what they request and register; a token without the audience is refused by the gateway, and the client loops on sign-in. The cost: every token of this realm is valid at the gateway. Keep the realm dedicated to the gateway. If other applications must share it, make `mcp-gateway` an optional scope instead and check every MCP client you support.

## Token and session lifetimes

| Setting | Value | Why |
|---|---|---|
| `accessTokenLifespan` | 300 s (5 min) | The gateway cannot revoke a token: a stolen token, or the token of a user you just disabled, works until it expires. Five minutes bounds that, and clients refresh in the background. |
| `ssoSessionIdleTimeout` | 604800 s (7 days) | A refresh token dies after a week without use: a connector used weekly keeps working, a lost laptop's token expires on its own. |
| `ssoSessionMaxLifespan` | 2592000 s (30 days) | Everyone signs in again at least monthly, which also re-runs your MFA and account checks. |
| `offlineSessionIdleTimeout` | 604800 s (7 days) | Same as the SSO session: MCP clients request `offline_access` (see above), so their refresh tokens are offline tokens. |
| `offlineSessionMaxLifespanEnabled`, `offlineSessionMaxLifespan` | `true`, 2592000 s (30 days) | Without it an offline token lives as long as it is used. With it, the 30-day ceiling of the SSO session applies to offline tokens too. |
| `revokeRefreshToken`, `refreshTokenMaxReuse` | `true`, `0` | Rotation: each refresh returns a new refresh token and the previous one is refused (`invalid_grant`). A leaked refresh token is detected the first time both copies are used. A client that refreshes twice concurrently with the same token gets one of the two calls refused; if a client you support does that, set `revokeRefreshToken` to `false` and rely on the session limits. |
| `accessCodeLifespan` | 60 s | The authorization code is exchanged at once by the client. |
| `accessCodeLifespanUserAction`, `accessCodeLifespanLogin` | 300 s, 1800 s | Time to complete a login page, and the whole login. Keycloak defaults. |

Keycloak's own defaults are 300 s, 30 min, 10 h and 30 days (offline idle, no ceiling). The development realm of this repository uses 60 s tokens on purpose (expiry tests); that value is too short for real clients.

Revoking access now: disable the user (or remove their credential), then end their sessions and offline sessions in the admin console (*Users* > the user > *Sessions*, and *Consents* for offline tokens). Tokens already issued stay valid for at most 5 minutes.

## Users and the identity claims

The gateway reads the identity from the access token (`auth.identity` above): `sub`, `email` (trusted only when `email_verified` is `true`), `org` and `groups`. Whoever can change those values chooses which API key a request uses, so in this realm only administrators can:

- The user profile makes `username`, `email` and `org` editable by `admin` only. A user cannot change them in the account console.
- `registrationAllowed: false`: no self sign-up. Users are created by an administrator, or come from your federation or broker, where the same rule applies to the mapped attributes.
- `duplicateEmailsAllowed: false` and `loginWithEmailAllowed: true`: one account per e-mail, so an e-mail matcher of the static resolver names one person.
- `editUsernameAllowed: false`, `verifyEmail: false`: an administrator creating a user sets `emailVerified` to `true` after checking it; with `verifyEmail` on, Keycloak would ask the user to confirm it by e-mail (needs SMTP).

The `gateway-identity` default scope adds the `org` claim (user attribute `org`) and the `groups` claim (full group paths: a member of the subgroup `analysts` of `team-a` gets `"/team-a/analysts"`). Full paths because two groups may share a name under different parents (`/team-a/analysts`, `/team-b/analysts`): with names alone both would be `analysts` in the token, and a resolver matching on `analysts` would give the members of both the same API key. Match on the path: `{"group": "/team-a/analysts"}` in a static mapping file, the same string in a webhook. The standard `email` and `profile` scopes add `email`, `email_verified` and the name claims.

Other realm settings: `sslRequired: external` (HTTPS for every address that is not private; your reverse proxy terminates TLS), `bruteForceProtected: true` with 10 failures, `passwordPolicy` `length(12) and notUsername and notEmail`, login and admin events stored for 30 days (they hold IP addresses: personal data, size the retention to your policy).

## Anonymous dynamic client registration

MCP clients register themselves (RFC 7591) at `<issuer>/clients-registrations/openid-connect` without credentials. Keycloak refuses that unless the realm's anonymous client registration policies allow it. The template keeps Keycloak's default anonymous policies and sets these:

| Policy | Setting | Why |
|---|---|---|
| Trusted Hosts | `trusted-hosts`: the list below | Every redirect URI, and `client_uri`/`rootUrl`/`baseUrl` if sent, must be on one of these hosts. |
| Trusted Hosts | `host-sending-registration-request-must-match: false` | The registration request comes from the vendor's servers (claude.ai, chatgpt.com, Azure) or from the user's own computer (VS Code, Cursor), whose addresses you cannot list. When `true`, Keycloak resolves each trusted host name and compares the addresses with the sender's: every real client would be refused. |
| Trusted Hosts | `client-uris-must-match: true` | The check that matters: a client can only register redirect URIs on the hosts below. |
| Max Clients Limit | `1000` | Registration is open to anyone, so this bounds how many clients can be created (Keycloak counts every client of the realm, its built-in ones included). **Anyone can reach the limit on purpose**: 1000 anonymous requests, and from then on every new registration fails, so no new user of a client that registers per connection (Claude does) can sign in until you delete clients. Rate-limit the registration endpoint at your reverse proxy ([below](#rate-limit-the-registration-endpoint)), monitor the client count, and delete registered clients that no longer sign in. |
| Consent Required | (default) | Every registered client shows a consent screen naming it and its scopes before its first token: the user's chance to notice a client they did not start. |
| Full Scope Disabled, Allowed Protocol Mapper Types, Allowed Client Scopes (`allow-default-scopes: true`), Allowed Registration Web Origins | (defaults) | No full role scope, no custom mappers, only the realm's default and optional client scopes, no browser (CORS) registration. |

### Client policies: the code flow with PKCE, nothing else

The registration policies above check where a client may redirect; they do not look at the grant types it asks for. Two client policies of the template do:

| Client policy | Applies to | Effect |
|---|---|---|
| `anonymous-registration` | clients being created or updated by an anonymous registration | PKCE `S256` is set as required on the client (`pkce-enforcer`): an authorization request without `code_challenge` is refused (`invalid_request`). A registration that asks for the implicit or hybrid flow (`grant_types` `implicit`, a `response_types` with `token` or `id_token`) or the password grant is refused (`reject-implicit-grant`, `reject-ropc-grant`, not auto-configured: Keycloak answers `403` `invalid_client_metadata` instead of quietly altering the client). |
| `authorization-code-and-refresh-only` | every token request of the realm, every client | Any grant other than `authorization_code` and `refresh_token` is refused at the token endpoint (`reject-request`, `400` "Request not allowed"): the password grant, client credentials and the device grant (each checked, below), and any other grant type. |

Why both. Keycloak compares only the host of a redirect URI ([below](#how-keycloak-checks-a-redirect-uri-2682)), so anyone can register a client on a custom scheme whose host is trusted, such as `evilapp://claude.ai/cb`. Without these policies, such a client registered with `grant_types: ["implicit"]` received an access token for the gateway in the URL fragment as soon as a user signed in and consented, with no PKCE and no code exchange; a client registered with `client_credentials` (and a client secret, which anonymous registration hands out) received a token for the gateway with no user at all; and the device grant gave a registrant a token for any user it talked into entering a code. The password grant was open on the realm's built-in `admin-cli` client (Keycloak creates it in every realm with direct access grants on): its token's audience is Keycloak's admin API, not the gateway, but it let anyone test passwords without the login page. All of these are refused now.

The device authorization endpoint still answers a registered device client and the user can still approve the code; the token request that follows is refused. Your administrators are not affected: they sign in to the `master` realm (the admin console, `kcadm.sh --realm master`), not to this one.

What is left, by design: a registrant on a trusted host gets a code at its own redirect URI if a user signs in and consents, and exchanges it with its own PKCE verifier. That is what dynamic registration is. The consent screen is the user's protection, and it shows the `client_name` the registrant chose, which can be "Claude". To narrow it, remove the trusted hosts of clients you do not support, and consider the `secure-redirect-uris-enforcer` executor, which can refuse private-use schemes and plain http on every host (see the end of the next section: Cursor's registration needs both).

These checks run against a real Keycloak 26.8.2 that imports the rendered template, in the gateway's end-to-end suite (`tests/e2e/stub/test_keycloak_template.py`).

### Trusted hosts and where each comes from

Re-verified against each vendor on 2026-11-21. Where a vendor page no longer says it, the table says so.

| Host | Client | Redirect URIs | Source (captured 2026-11-21) |
|---|---|---|---|
| `claude.ai` | claude.ai web, Desktop, mobile, Cowork | `https://claude.ai/api/mcp/auth_callback` | [claude.com/docs/connectors/building/authentication](https://claude.com/docs/connectors/building/authentication): "register exactly this redirect URI: https://claude.ai/api/mcp/auth_callback" |
| `localhost`, `127.0.0.1` | Claude Code, VS Code, Cursor desktop | loopback, any port (Claude Code), `http://127.0.0.1/` and `http://127.0.0.1:33418/` (VS Code), `http://localhost:8787/callback` (Cursor) | Claude page above: "accept a loopback redirect on any port"; VS Code and Cursor rows below |
| `chatgpt.com` | ChatGPT | `https://chatgpt.com/connector_platform_oauth_redirect` (Keycloak returns `iss`, RFC 9207, so ChatGPT uses this stable URI) or `https://chatgpt.com/connector/oauth/{callback_id}` | [developers.openai.com/apps-sdk/build/auth](https://developers.openai.com/apps-sdk/build/auth): "If your authorization server meets those requirements, ChatGPT uses the stable redirect URI https://chatgpt.com/connector_platform_oauth_redirect" |
| `global.consent.azure-apim.net` | Microsoft Copilot Studio (Power Platform connectors) | `https://global.consent.azure-apim.net/redirect/<connector id>` | [learn.microsoft.com/.../azure-active-directory-authentication](https://learn.microsoft.com/en-us/connectors/custom-connectors/azure-active-directory-authentication): "for example, https://global.consent.azure-apim.net/redirect/samplecustomconnector123-…". **The Copilot Studio MCP pages ([mcp-add-existing-server-to-agent](https://learn.microsoft.com/en-us/microsoft-copilot-studio/mcp-add-existing-server-to-agent)) do not name the host**: they say "a callback URL might appear" and to copy it. Check the callback URL Copilot Studio shows you; if its host differs, add that host. |
| `vscode.dev`, `insiders.vscode.dev` | VS Code | `https://vscode.dev/redirect`, `https://insiders.vscode.dev/redirect` | VS Code source, [`src/vs/base/common/oauth.ts`](https://github.com/microsoft/vscode/blob/main/src/vs/base/common/oauth.ts) (`fetchDynamicRegistration`), and its CIMD document [vscode.dev/oauth/client-metadata.json](https://vscode.dev/oauth/client-metadata.json). **The VS Code user documentation does not state these URIs.** |
| `code.visualstudio.com` | VS Code | none: it is the `client_uri` VS Code sends with its registration, and Keycloak checks that URL against the same list | VS Code source above: `client_uri: 'https://code.visualstudio.com'` |
| `www.cursor.com` | Cursor web and cloud agents | `https://www.cursor.com/agents/mcp/oauth/callback` | [cursor.com/docs/mcp](https://cursor.com/docs/mcp): "Register the callback for each surface your users authenticate from: https://www.cursor.com/agents/mcp/oauth/callback http://localhost:8787/callback" |
| `anysphere.cursor-mcp` | Cursor desktop, older flow | `cursor://anysphere.cursor-mcp/oauth/callback` | **No longer on the Cursor docs page.** Cursor staff on the [Cursor forum](https://forum.cursor.com/t/oauth-redirect-uri-changed-from-cursor-to-http-localhost-for-streamable-http-mcp/165019) (2026-07-15): "Allowlist all three callbacks Cursor may send [...] with Dynamic Client Registration, Cursor sends all three at once". One refused URI refuses the whole registration, so the host stays in the list. |

Only add hosts you have checked against the vendor's current documentation. Remove the ones of clients you do not support.

### How Keycloak checks a redirect URI (26.8.2)

From the policy's source ([`TrustedHostClientRegistrationPolicy.java` at 26.8.2](https://github.com/keycloak/keycloak/blob/26.8.2/services/src/main/java/org/keycloak/services/clientregistration/policy/impl/TrustedHostClientRegistrationPolicy.java)) and from registrations against the rendered template on a fresh 26.8.2:

- **Only the host is compared.** Keycloak takes `new URI(uri).getHost()` and compares it with each trusted host, exactly, or with `*.domain` entries as a suffix. Scheme, port, path and query are not looked at.
- **A custom scheme passes when its host part is trusted.** `cursor://anysphere.cursor-mcp/oauth/callback` has the host `anysphere.cursor-mcp`, so it is accepted, and Keycloak then completes the code flow to it. The flip side: `evil://claude.ai/callback`, `http://claude.ai/...` (plain http) and `https://claude.ai/any/path` are accepted too. Such a client is limited to the code flow with PKCE by the [client policies](#client-policies-the-code-flow-with-pkce-nothing-else): it gets a code, at the URI it registered, only after a user signed in and approved it on the consent screen.
- **Ports are ignored, at registration.** `http://127.0.0.1:1/x` is accepted when `127.0.0.1` is trusted.
- **At sign-in, loopback ports depend on what was registered.** A loopback redirect registered without a port (`http://127.0.0.1/callback`, `http://localhost/callback`) matches any port at the authorization request; one registered with a port (`http://127.0.0.1:33418/`) matches that port only.
- **Wildcards are not refused.** A registered `https://claude.ai/*` is accepted (it stays on a trusted host).
- **A URI without a host fails with a server error.** `cursor:/oauth/callback` makes Keycloak answer `500` (a `NullPointerException` in the policy); the registration is refused all the same.
- **Subdomains are not trusted implicitly.** `https://www.claude.ai/...` and `https://claude.ai.evil.example.net/...` are refused.
- **One untrusted URI refuses the whole registration**, and so does an untrusted `client_uri`.

If you want stricter redirect rules for registered clients, Keycloak's `secure-redirect-uris-enforcer` client policy executor can refuse wildcards, plain http or private-use schemes; check it against every client you support first (Cursor's registration includes a private-use scheme and a plain-http loopback URI).

### Rate-limit the registration endpoint

Keycloak has no rate limit of its own on anonymous registration. Put one in your reverse proxy, on the registration path of the realm only, so that sign-ins and token refreshes are never throttled. With nginx (adapt the realm name and the `proxy_pass` to your existing Keycloak location):

```nginx
# http {} context
limit_req_zone $binary_remote_addr zone=keycloak_registration:10m rate=30r/m;

# server {} context of the Keycloak host
location /realms/example-publisher/clients-registrations/ {
    limit_req zone=keycloak_registration burst=60 nodelay;
    limit_req_status 429;
    proxy_pass http://keycloak:8080;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header X-Forwarded-Host $host;
}
```

The limit is per source address, and the hosted clients register from their vendor's servers: every Claude, ChatGPT or Copilot Studio user of your gateway shares the few addresses of that vendor. Size the rate to your peak of new connections, not to one user, and watch for `429` on this path. A rate limit slows an exhaustion of the Max Clients limit down; it does not prevent it. Monitoring the client count does.

## CIMD (experimental)

Client ID Metadata Documents let a client use an `https` URL as its `client_id`: Keycloak fetches the document there instead of registering a client. Keycloak 26.8 supports it only as an experimental feature that "may introduce breaking changes in future versions" ([Keycloak MCP guide](https://www.keycloak.org/securing-apps/mcp-authz-server)). It is off in this template.

`cimd-client-policies.json` holds a client profile and client policy for Claude, following that guide's single-profile recipe for Claude and Claude Code: client ids on `claude.ai`, allowed domains `claude.ai`, `localhost`, `127.0.0.1`, *Restrict same domain* off (Claude Code uses a loopback redirect), *Only allow confidential client* off, and `accept-public-client-with-confidential-client-only-grant` on (Claude's document lists the `jwt-bearer` grant, which Keycloak refuses for a public client; with the option on, Keycloak drops that grant). It also requires PKCE `S256`. The file repeats every client profile and client policy of the template, because the admin API replaces the whole list: applying a file without them would remove the [client policies](#client-policies-the-code-flow-with-pkce-nothing-else).

Before you enable it, know what changes for every client, not only Claude: with `--features=cimd`, Keycloak advertises `"client_id_metadata_document_supported": true` and adds `none` to `token_endpoint_auth_methods_supported` in its metadata, whether or not a policy exists. Clients that support CIMD then use it:

- Claude selects CIMD when both values are present; the policy covers it.
- ChatGPT "prioritizes CIMD when it is available", and Keycloak does not support ChatGPT's document (Keycloak MCP guide). With the feature on, ChatGPT's sign-in fails (`Client not found`) unless the ChatGPT app is set to use DCR.
- VS Code also has a CIMD document (`https://vscode.dev/oauth/client-metadata.json`); it needs its own policy (the Keycloak guide has one).

To enable it:

1. Start Keycloak with `--features=cimd`. Keycloak fetches the documents from `claude.ai`, so it needs outbound HTTPS.
2. Apply both lists (replace the token and the host):

   ```sh
   jq '.clientProfiles' cimd-client-policies.json | curl -fsS -X PUT -H "Authorization: Bearer $ADMIN_TOKEN" \
     -H 'Content-Type: application/json' --data-binary @- \
     https://sso.example.com/admin/realms/example-publisher/client-policies/profiles
   jq '.clientPolicies' cimd-client-policies.json | curl -fsS -X PUT -H "Authorization: Bearer $ADMIN_TOKEN" \
     -H 'Content-Type: application/json' --data-binary @- \
     https://sso.example.com/admin/realms/example-publisher/client-policies/policies
   ```

What was observed with it on a fresh 26.8.2, before the template's client policies refused every grant but the code flow (not re-run since; Claude's client uses the code flow): Claude's hosted client id reaches the login page with its documented redirect URI and completes the flow (the token's `azp` is the document URL, `aud` the gateway); without PKCE, or with a redirect URI not in the document, the request is refused; Claude Code's client id is accepted with `http://localhost:<any port>/callback`; ChatGPT's client id gets `Client not found`. The *Resource indicator allow list* of the executor only matters with `--features=resource-indicators` and is not set.

## Not in the template

Users, groups, identity providers and user federation, SMTP, themes, and any client secret: they are yours to add. The realm holds no secret, so the template can be kept in version control; the rendered file holds none either.
