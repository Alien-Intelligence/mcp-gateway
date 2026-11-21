# 7. Connecting clients

Every client needs the same thing from you: the gateway's **public URL** (for example `https://mcp.example.com/mcp`). It finds everything else, your OAuth server and the scopes to ask for, by calling that URL and following the `401` challenge and the metadata of [chapter 1](01-what-it-is.md#one-request-step-by-step).

What differs is how each client **registers** at your OAuth server, and which **redirect URI** your server must accept.

| Client | URL to enter | Registration | Redirect URI(s) to allow | Vendor documentation |
|---|---|---|---|---|
| Claude (claude.ai, Desktop, mobile, Cowork) | The public URL, added as a custom connector | CIMD, DCR, or a client id (and secret) you create and enter | `https://claude.ai/api/mcp/auth_callback` | <https://claude.com/docs/connectors/building/authentication> |
| Claude Code | The public URL | CIMD (its own metadata document) | A loopback address on any port, `localhost` and `127.0.0.1` | same page |
| ChatGPT (connectors / apps) | The public URL | CIMD preferred, or DCR | `https://chatgpt.com/connector_platform_oauth_redirect` or `https://chatgpt.com/connector/oauth/{callback_id}` | <https://developers.openai.com/apps-sdk/build/auth> |
| Microsoft Copilot Studio | The public URL, as the "Server URL" of an MCP tool | OAuth 2.0: dynamic discovery (DCR), dynamic, or manual client id and secret | The callback URL that the wizard shows for the connector | <https://learn.microsoft.com/en-us/microsoft-copilot-studio/mcp-add-existing-server-to-agent> |
| VS Code | The public URL in `mcp.json` | A pre-registered client id (`oauth.clientId`); DCR and CIMD are listed in the project's research, see below | See below | <https://code.visualstudio.com/docs/copilot/reference/mcp-configuration> |
| Cursor | The public URL in its MCP configuration | DCR (public client), or a static client id in `auth` | `https://www.cursor.com/agents/mcp/oauth/callback` and `http://localhost:8787/callback` | <https://cursor.com/docs/context/mcp> |

All of them use the authorization code flow with PKCE. Your server must let each client's redirect URI through, and the gateway's `policies.allowed_origins` must list the **browser origins** that call it (below).

## Claude

- Add the gateway's URL as a **custom connector** (the vendor page above describes how a custom connector identifies Claude to your server: through the Client ID Metadata Document that Anthropic hosts, through Dynamic Client Registration, or with an OAuth client id that you registered and that the user enters in the dialog, with a client secret only if your server needs one).
- Claude requires a `401` (not a header on a `200`) to start sign-in, uses only the first entry of `authorization_servers`, and sends PKCE `S256` on every authorization request. The gateway does all three.
- Claude requests the scopes in the challenge's `scope`, or else the metadata's `scopes_supported`; with none it asks for every scope and fails with `invalid_scope`. Advertise `openid` (chapter 6).
- **CIMD**: Claude normally uses it only when your server's metadata advertises both `"client_id_metadata_document_supported": true` and `"none"` in `token_endpoint_auth_methods_supported`, and otherwise falls back to DCR. Keycloak's CIMD is experimental (chapter 6): DCR is the path that works with it today.
- Reachability and the 10-second limit: chapter 2.
- Add `https://claude.ai` to `policies.allowed_origins`; the examples do.
- **Claude Code** uses a loopback redirect on an ephemeral port. Your server must match `http://localhost/callback` and `http://127.0.0.1/callback` with the port ignored.

## ChatGPT

- Your OAuth server's metadata must list `S256` in `code_challenge_methods_supported`.
- ChatGPT prefers CIMD when your server advertises `client_id_metadata_document_supported: true`, and registers dynamically if the metadata has a `registration_endpoint` (one registration per MCP server connection, reused). **Keycloak does not support ChatGPT through CIMD** (project research); use DCR, with `chatgpt.com` in Keycloak's trusted hosts.
- ChatGPT sends the `resource` parameter in both the authorization and the token request and expects it echoed into the token, typically as `aud`. The vendor page does not say which value ChatGPT puts in `resource`. With the audience mapper of chapter 6 the token's `aud` is the gateway URL whatever the client sends, which is what the gateway checks.
- Redirect URI: if your server supports issuer identification (RFC 9207: `authorization_response_iss_parameter_supported: true` and `iss` in every authorization response, equal to the metadata `issuer`), ChatGPT uses the stable `https://chatgpt.com/connector_platform_oauth_redirect`; otherwise `https://chatgpt.com/connector/oauth/{callback_id}`, which the connector's management page shows. Copy the value ChatGPT shows you into your server's allowlist.
- ChatGPT does not use API keys for these connectors (project research).

## Microsoft Copilot Studio

- In your agent: **Tools**, **Add a tool**, **New tool**, **Model Context Protocol**. Fill in the server name, a description and the **Server URL** (the gateway's `public_url`). Choose **OAuth 2.0** and one of:
  - **Dynamic discovery**: the client discovers your endpoints and registers itself (DCR). The vendor calls it the simplest method.
  - **Dynamic**: DCR, but you enter the authorization URL and the token URL template.
  - **Manual**: you register a client at your server and enter its client id, client secret, authorization URL, token URL template, refresh URL and (optional) scopes.
- When a callback URL is shown, copy it into the redirect URIs of your server's client (or into your server's trusted hosts for DCR).
- Copilot Studio supports the Streamable HTTP transport only (the gateway proxies the transport unchanged).
- **Unconfirmed, needs a Copilot Studio tenant to test**: whether Copilot Studio sends PKCE `S256` in manual mode, whether it sends the `resource` parameter (so that your `aud` is right without the audience mapper) and whether it supports CIMD. The vendor page does not say. Use the audience mapper (so `aud` is correct regardless), and test with a tenant before you promise Copilot Studio to anyone. The exact redirect URL (a `global.consent.azure-apim.net/redirect/...` address, per project research) is generated per connector: use the one the wizard shows.

## VS Code

- Add the server in `mcp.json` with its URL. For a server that needs a **pre-registered OAuth client**, the vendor page documents `oauth` with a `clientId` (a client secret is stored separately, not in `mcp.json`), and the flat field `oauthClientId` in the portable `mcpServers` format. VS Code handles the flow and opens a browser on the first connection.
- **Unconfirmed on the vendor page**: dynamic registration, CIMD and the redirect URIs. The project's research lists `http://127.0.0.1:33418` and `https://vscode.dev/redirect` as redirect URIs and the CIMD document `https://vscode.dev/oauth/client-metadata.json`. Check them against your VS Code version before relying on them. If you pre-register a client, use the redirect URIs your VS Code shows or the research's two.
- VS Code puts the whole server in an error state on any HTTP status of 300 or more (this is why the gateway answers rate limits and most other refusals as HTTP `200` with a JSON-RPC error where a reply is possible; see [`configuration.md`](../../configuration.md#policiesrate_limits)).

## Cursor

- Cursor uses the authorization code flow with PKCE and reads your protected resource metadata to find the authorization server and the resource.
- With dynamic registration it registers as a public client (no secret). For a server that gives a fixed client id, put an `auth` object in the MCP configuration with `CLIENT_ID`, optionally `CLIENT_SECRET` (confidential clients only) and `scopes` (when omitted, Cursor reads `scopes_supported` from `/.well-known/oauth-authorization-server`).
- Register both callbacks: `https://www.cursor.com/agents/mcp/oauth/callback` (web and Cursor Agents) and `http://localhost:8787/callback` (desktop app). Cursor identifies the server through the OAuth `state` parameter, so the two URLs serve every MCP server.
- The project's research also lists `cursor://anysphere.cursor-mcp/oauth/callback`; the current vendor page does not. **Unconfirmed.**

## Allowed origins

The gateway answers `403` with `-31008` (before authentication) to a request that carries an `Origin` header not listed in `policies.allowed_origins`. A request without an `Origin` header passes. An empty list refuses every request that carries one. Entries are `scheme://host[:port]`, http or https, with no path.

- The development stack allows `https://claude.ai` and `http://localhost:6274` (the MCP Inspector).
- Which origins the other clients send was not confirmed for this tutorial. If a client's calls are refused with `-31008`, the audit line shows the outcome `origin_refused` but not the origin itself: read the `Origin` header of the refused request (browser developer tools or your proxy's log) and add exactly that origin.

Continue with [8. Operations](08-operations.md).
