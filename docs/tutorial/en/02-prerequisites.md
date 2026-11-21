# 2. Prerequisites

The gateway is not an OAuth server, so a compatible one is a prerequisite of the product. Check it before you deploy anything.

## What your OAuth server must provide

| # | Requirement | Why | How to check |
|---|---|---|---|
| 1 | Discovery metadata: RFC 8414 authorization-server metadata or OpenID Connect discovery, advertising `S256` in `code_challenge_methods_supported` | AI clients read it to find the endpoints, and they refuse to start a flow without PKCE `S256` | `curl -s https://idp.example.com/realms/example-publisher/.well-known/openid-configuration` and read `code_challenge_methods_supported`, `authorization_endpoint`, `token_endpoint`, `jwks_uri` |
| 2 | JWT access tokens signed with an asymmetric algorithm (`RS256`, `RS384`, `RS512`, `PS256`, `PS384`, `PS512`, `ES256`, `ES256K`, `ES384`, `ES512` or `EdDSA`) and a published JWKS | The gateway validates tokens locally. It does not call token introspection. `HS*` and `none` are refused at start | Decode a token you obtained: the header's `alg`, `kid` and the key at `jwks_uri` |
| 3 | The gateway URL in the token's `aud` | The gateway accepts only tokens whose `aud` contains one of `auth.audiences`, and `auth.audiences` must contain `public_url` | Decode a token and read `aud` (Keycloak puts `account` there unless you configure an audience mapper, chapter 6) |
| 4 | A client registration path that the AI clients can use: anonymous Dynamic Client Registration (RFC 7591), Client ID Metadata Documents (CIMD), or clients you pre-register for each AI client | Each AI client needs a client id at your server. Chapter 7 says which clients use which path | The metadata shows `registration_endpoint` (DCR) and `client_id_metadata_document_supported` (CIMD) |
| 5 | Identity claims in the access token: a stable subject, and the claims you map on (`email` with `email_verified`, an organization claim, groups) | The resolver can only decide on what the token carries. Claim paths are configurable in `auth.identity`, including nested ones such as `ext.org` | Decode a token and look for the claims |
| 6 | The scopes you advertise exist at your server | The gateway advertises `auth.scopes_advertised` in the challenge and in its metadata. A client then asks for those scopes | Compare `scopes_advertised` with the server's scope list |

Notes on the table:

- The gateway does no discovery of its own. You write `auth.issuer` and `auth.jwks_uri` in the configuration. The token's `iss` must equal `issuer` exactly, character for character.
- Claude uses only the first entry of `authorization_servers`. The gateway always publishes exactly one: `auth.issuer`.
- For an issuer that has a path (Keycloak realms do), clients probe several metadata locations. The MCP specification of 2025-11-25 lists the order for the issuer `https://idp.example.com/realms/x`: `/.well-known/oauth-authorization-server/realms/x`, then `/.well-known/openid-configuration/realms/x`, then `/realms/x/.well-known/openid-configuration`. Your server must answer at one of them.
- `email` is trusted only when the claim named by `auth.identity.email_verified` is the boolean `true`. If you set `email_verified` to `null`, an e-mail is never trusted and the identity has none, so an `email` matcher cannot work.
- Known incompatibility: authentik 2026.8 requires a pre-issued bearer token for dynamic client registration, and the clients it creates sign tokens with their own secret (HS256) under a per-application issuer, so a resource server cannot verify them through a JWKS. With authentik, pre-register the AI clients instead.

Chapter 6 gives a worked setup for Keycloak and a checklist for other servers.

## Network reachability

The AI clients are not on your network.

- **Claude** (claude.ai, Desktop, mobile, Cowork) calls the gateway and your OAuth server from Anthropic's cloud. Anthropic's outbound traffic comes from `160.79.104.0/21` ([Anthropic IP addresses](https://platform.claude.com/docs/en/api/ip-addresses)). Your gateway **and** your OAuth server must be reachable from that range, including through any WAF in front of the identity provider. A symptom of a blocked OAuth server is that the gateway receives the first request and the OAuth server sees no traffic at all.
- Claude gives your discovery, registration and token endpoints **10 seconds** to answer, and refresh requests 30 seconds. Slower answers are treated as a failed flow (vendor documentation: <https://claude.com/docs/connectors/building/authentication>).
- **Microsoft Copilot Studio**: the token endpoint is reached from Azure (a note in the project's research, not confirmed on a vendor page). **ChatGPT**: where its requests come from was not confirmed for this tutorial. If you filter by address, check each vendor's current documentation.
- The gateway reaches your OAuth server's JWKS (`auth.jwks_uri`) and the Alien MCP server (`upstreams.*.url`), and your resolver backends (webhook, database, Redis). Outbound access to these must be open from the gateway's network. The JWKS can be an internal URL: only the gateway fetches it, so `jwks_uri` may differ from the public `issuer`.

## TLS

- Clients need `https` for your public URL. Terminate TLS in front of the gateway (ingress, load balancer or reverse proxy). The gateway itself serves plain HTTP on `listen.host`:`listen.port`.
- The webhook resolver requires an `https` URL unless `allow_insecure_http` is true, which is for development stacks only because the webhook's answer carries the API key.
- Use certificates from a public certificate authority for your OAuth server and for the gateway's public name. This tutorial did not test a private authority with any AI client.
- The proxy in front of the gateway must not buffer responses and must not cut quiet streams. See chapter 5.

## Checklist before you continue

- [ ] I can fetch my OAuth server's metadata and it lists `S256`.
- [ ] I know how my AI clients will register (DCR, CIMD or a pre-registered client id).
- [ ] I can make a token that carries the gateway URL in `aud` (or I will, in chapter 6).
- [ ] Tokens carry a stable subject and the claims I want to map on.
- [ ] The gateway's public URL and my OAuth server are reachable from the vendors' clouds.
- [ ] I have a DNS name and a certificate for the gateway.

Continue with [3. The Alien side](03-alien-side.md).
