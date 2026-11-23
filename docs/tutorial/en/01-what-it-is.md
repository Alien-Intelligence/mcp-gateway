# 1. What the gateway is and is not

## What it is

The Alien MCP Gateway sits between AI clients and an Alien MCP server. It does three things:

1. It is an OAuth **resource server**. It accepts requests that carry an access token issued by **your** OAuth server (for example Keycloak), and checks that token locally: signature against the published key set (JWKS), issuer, audience, expiry and, optionally, required scopes.
2. It swaps credentials. From the verified token it builds an identity (subject, verified e-mail, organization, groups), asks a **resolver** which Alien API key belongs to that identity, drops everything the client sent for authentication and adds `Authorization: Bearer <that key>`.
3. It streams. The MCP traffic goes to the Alien MCP server and the answer comes back, including Server-Sent Events, as it arrives. Apart from the opt-in policies of chapter 8 (tool allow and deny lists), the gateway does not read or rewrite responses.

Your users sign in with their usual accounts, your MCP endpoint lives on your own domain, and Alien sees one billed user per API key.

## What it is not

| It does not | What that means for you |
|---|---|
| Issue tokens or register clients | There is no OAuth server in the gateway. You need one (chapter 2). It publishes only Protected Resource Metadata (RFC 9728) that names your OAuth server. |
| Manage users or keys | Creating organizations, users and API keys on Alien is your job, through the Alien API (chapter 3). The gateway only looks up an existing key. A missing mapping is an explicit denial, never a fallback key. |
| Brand anything | Your OAuth server shows the login page. The server name and instructions that an assistant displays come from the Alien MCP server, not from the gateway. |
| Terminate TLS | The process listens on plain HTTP (`listen` has only `host` and `port`). Put an ingress or reverse proxy with a certificate in front of it. |
| Pass the client's token upstream | `Authorization`, cookies, `X-Forwarded-*` and the other headers listed in [`protocol.md`](../../protocol.md#headers) never reach Alien. Only an allow-list of headers is forwarded. |
| Turn an Alien `401` into a sign-in prompt | If Alien rejects the key the gateway holds for a user, the client receives a JSON-RPC error (`-31001`), never an OAuth challenge. Otherwise the client would loop on re-authentication. |

## Architecture

```
                         (1) sign in, PKCE S256, token with aud = https://mcp.example.com/mcp
   ┌────────────┐ ─────────────────────────────────────────────────────►  ┌──────────────────────┐
   │ MCP client │                                                           │ your OAuth server     │
   │ Claude,    │ ◄─────────────────────────────────────────────────────  │ idp.example.com       │
   │ ChatGPT,.. │            access token (JWT)                             │ (Keycloak, ...)       │
   └─────┬──────┘                                                           └──────────┬───────────┘
         │ (2) MCP over HTTPS, Authorization: Bearer <JWT>                              │ (3) JWKS
         ▼                                                                              │
   ┌─────────────────────────────────────────────────────────────────────┐             │
   │ Alien MCP Gateway  (you host it: mcp.example.com)                   │ ◄───────────┘
   │  a. check the JWT: signature, iss, aud, exp/nbf, scopes             │
   │  b. identity = subject, verified e-mail, organization, groups       │
   │  c. resolver(identity) ─► Alien API key + upstream + labels, or deny│ ──► your webhook / database /
   │  d. drop client credentials, add Authorization: Bearer <Alien key>  │     Redis / mapping file
   │  e. stream the request, stream the answer back                      │
   └──────────────────────────────────────┬──────────────────────────────┘
                                          │ (4) MCP over HTTPS, one Alien API key per user
                                          ▼
                                 ┌─────────────────────┐
                                 │ Alien MCP server     │ ─► meters the call to the key's user
                                 └─────────────────────┘

   Set-up time only (chapter 3): you ──► Alien API ──► organizations, client-managed users, API keys
```

Two details of step (1) matter for everything that follows:

- The token's audience (`aud`) must contain the gateway's public URL, exactly as written in `public_url`. That is what makes the token "issued for this resource". Chapter 6 shows how.
- The gateway publishes its metadata at the path-specific URL only. For `public_url` `https://mcp.example.com/mcp` that is `https://mcp.example.com/.well-known/oauth-protected-resource/mcp`. The origin-level URL answers `404`.

## One request, step by step

1. The client calls `https://mcp.example.com/mcp` without a token. The gateway answers `401` with `WWW-Authenticate: Bearer resource_metadata="https://mcp.example.com/.well-known/oauth-protected-resource/mcp", scope="mcp-gateway offline_access"`.
2. The client fetches that metadata, finds `authorization_servers` (your OAuth server, one entry), reads your server's own metadata, registers or identifies itself, and runs the authorization code flow with PKCE.
3. The client calls the gateway again with the access token. The gateway verifies it, asks the resolver, and forwards the request with the user's Alien key. If `auth.userinfo` is set, it also asks your OAuth server's userinfo endpoint for the user's claims before it asks the resolver (chapter 6.3).
4. Alien meters the call to the user that owns the key. If that user's organization is sponsored, the sponsor's feed shows the call too (chapter 3).

Continue with [2. Prerequisites](02-prerequisites.md).
