# 9. Troubleshooting

## Start here

1. Get the **`x-request-id`** response header of the failing call (every response the gateway produces or relays has one).
2. Find the audit line with that `request_id` in the gateway's stdout. Read `outcome`, `reason` and `status`: they tell which step refused.
3. The client sees only an error code and a generic message. The reason is in the audit line and in the gateway's logs, never in the answer.

Match on the **code**, never on the message text: texts may be reworded in any release. All codes are in [`protocol.md`](../../protocol.md).

## Sign-in does not start or does not finish

| Symptom | Cause | Fix |
|---|---|---|
| Claude says it could not reach the MCP server; the gateway got the first request but your OAuth server saw no traffic | The client could not read the metadata: the `401` has no `resource_metadata`, or the metadata path is not routed to the gateway, or your OAuth server is not reachable from Anthropic's `160.79.104.0/21` (a WAF in front of the identity provider is a common cause) | `curl` the metadata URL of the challenge (chapter 5.5); route `/.well-known/oauth-protected-resource<path>` to the gateway; allow Anthropic's range on **both** the gateway and the OAuth server |
| Claude fails at sign-in with `invalid_scope` | Known with Claude and Keycloak: when no scope is advertised Claude asks for every scope the server has and gets `invalid_scope`; and Claude adds `offline_access` when Keycloak lists it, which fails for a client that registered without it. The gateway always advertises what `auth.scopes_advertised` lists (at least one entry), so look for a challenge or metadata that does not carry it (another layer rewriting them), or a listed scope that your server does not know | Advertise `["mcp-gateway", "offline_access"]` in `auth.scopes_advertised` (Keycloak: a client registered without `offline_access` in its `scope` fails with `invalid_scope` when Claude adds it); check the challenge's `scope` with `curl -i` (chapter 5.5) and that every advertised scope exists in the realm |
| VS Code's registration is refused (`403`, "Not permitted to use specified clientScope") | `auth.scopes_advertised` contains `openid`: VS Code sends the challenge's scopes as the `scope` of its registration, and Keycloak has no client scope `openid` | Advertise `["mcp-gateway", "offline_access"]` (chapter 6) |
| Keycloak refuses the client's registration (reported symptom: HTTP `403` on the registration request) | The "Trusted Hosts" registration policy has no entry for the host that registers or for the client's redirect URIs | Add the hosts of chapter 7 to the realm's anonymous Trusted Hosts policy (chapter 6) |
| ChatGPT cannot register through CIMD against Keycloak | Keycloak does not support ChatGPT through CIMD | Use DCR, with `chatgpt.com` in the trusted hosts |
| The client signs in, then every call is `401` with `token audience does not match this resource` | The token's `aud` does not contain `public_url`. With Keycloak and no audience mapper, `aud` is `account` | Add the audience mapper to a client scope the client requests (chapter 6) and advertise that scope; compare `aud` with `public_url` character by character |
| Sign-in loops: the client re-authenticates after each failure | A `401` with an OAuth challenge reaches the client although its token is valid. The gateway never does that: it turns an Alien `401` into JSON-RPC `-31001`. Something else in the path (a proxy, a second gateway, a custom layer in front or a resolver backend answering with a challenge) relays an upstream `401` | Call the gateway directly with a valid token and compare; remove the layer that relays the `401` |
| The redirect after login fails at your OAuth server ("invalid redirect URI") | The client's redirect URI is not allowed in its client | Chapter 7: the redirect URI of each client. ChatGPT: copy the one its management page shows |
| Copilot Studio fails after login | PKCE, `resource` and CIMD behaviour of Copilot Studio are unconfirmed (chapter 7) | Use the audience mapper; test with a tenant; read the failing request at your OAuth server |

## The gateway answers `401` or `403` before any resolver

`401` bodies are `{"error": "invalid_token", "error_description": "<text>"}` with a `WWW-Authenticate: Bearer resource_metadata="...", scope="...", error="invalid_token", ...`.

| `error_description` (audit outcome `unauthenticated`) | Cause | Fix |
|---|---|---|
| `missing bearer token` | No `Authorization: Bearer` header: this is the normal first request | Nothing, unless a client that has a token still gets it: check that no proxy strips `Authorization` |
| `token is malformed` | Not a JWT: an opaque access token | Configure the OAuth server to issue JWT access tokens |
| `token algorithm is not accepted` | The token's `alg` is not in `auth.algorithms` (or is `HS*`/`none`) | Add the algorithm your server signs with, if asymmetric |
| `token signing key is unknown` | The `kid` is not in the key set at `jwks_uri` (right after a key rotation at the server, or the wrong `jwks_uri`). The gateway refetches for an unknown `kid`, at most once every 10 seconds | Check `jwks_uri` from the gateway's network; wait one refresh |
| `token signature is invalid` | Signed by another key than the one the key set holds | Same: wrong realm or `jwks_uri` |
| `token issuer is not trusted` | `iss` differs from `auth.issuer` (trailing slash, internal versus public host name, wrong realm) | Make `auth.issuer` equal the token's `iss` exactly |
| `token audience does not match this resource` | See above | Audience mapper |
| `token expired`, `token not yet valid` | Lifetime too short for the client's refresh, or clocks differ more than `auth.clock_skew_seconds` | Synchronize clocks (NTP); lengthen the token's lifetime (chapter 6) |
| `token is missing a required claim` | The token has no `exp`, `iss` or `aud` | Fix the OAuth server's token mapping |
| `token does not identify a user` | The claim at `auth.identity.subject` is missing or not a string, or another mapped claim has the wrong type (a claim of the wrong type is a `401`; a missing optional claim is just absent) | Check the claim paths against a decoded token |

| Status | Body | Cause | Fix |
|---|---|---|---|
| `403` | `insufficient_scope` | The token lacks a scope listed in `auth.required_scopes` (outcome `insufficient_scope`) | The client must request it: advertise it in `scopes_advertised` too and make sure the realm grants it |
| `503` + `Retry-After: 5` | `temporarily_unavailable`, "identity provider keys are unavailable" (outcome `idp_unavailable`) | The gateway cannot fetch the key set and the cached copy is past `jwks_max_stale_seconds` | Fix the network path to `jwks_uri` (and its certificate); the gateway logs `JWKS fetch failed` |
| `403` + `-31008` | `ORIGIN_REFUSED` (outcome `origin_refused`) | A browser origin that is not in `policies.allowed_origins` | Add that exact origin (chapter 7). Requests without an `Origin` header pass |
| `413` / `408` | `-32600` | Body above `limits.max_request_body_bytes`, or not received within `limits.request_body_timeout_seconds` | Raise the limit, or find the slow sender |
| `400` | `-32020`, or `-32600` for a repeated `Mcp-Session-Id` or `Mcp-Protocol-Version` | `Mcp-Method` or `Mcp-Name` disagrees with the body; or a proxy duplicated a header | Remove what rewrites or duplicates MCP headers |

## The user is signed in but the call fails

| Symptom | Cause | Fix |
|---|---|---|
| `403`, `-31010` (outcome `denied`, reason `no_mapping`) | The resolver found no key for this identity | Create the mapping (chapter 4). Check what the identity contained: no verified e-mail means `email` is `null` (the claim named by `email_verified` must be `true`); a wrong `organization` or `groups` path |
| `403`, `-31011` (reason `ambiguous_mapping`) | Two group entries of a `static` mapping match the user, or a `store_sql` query returned more than one row | Make the match unique |
| `403`, `-31012` | Your resolver denied with another reason (see `reason` in the audit line) | Your resolver's rule |
| `503` + `Retry-After: 5`, `-31009` (outcome `resolver_unavailable`) | Your webhook, database or Redis could not be reached, or timed out, after the retries | Check the backend and the gateway's network path; log line `credential backend unavailable` |
| `500`, `-31005` ("The gateway could not obtain credentials for this request."; outcome `gateway_error`) | A resolver error: your webhook answered a status other than `200` (a `401` from a signature it rejected, for example), a malformed or oversized answer, an unknown `upstream` name or an invalid key; a `static` key file unreadable or empty; a broken mapping file; a TLS certificate the gateway cannot verify | Log line `credential resolution failed` names the cause (never the content). Test the webhook with `resolve_probe.py` (chapter 4) |
| `-31001` with `data.upstream_status` `401` (HTTP `200` when the request expects a reply) | Alien rejected the key the gateway holds for this user: revoked, wrong scope, or the user was deleted. The gateway dropped its cached copy | Fix or re-mint the key (chapter 3) and update the source (chapter 8). This is not an OAuth problem: the user does not need to sign in again |
| `-31002` with `upstream_status` `402` | Alien answered `402` to the key | Ask Alien: the key's account is refused for payment reasons. The gateway dropped its cached copy |
| `-31003` (`502`) / `-31004` (`504`) | The Alien MCP server is unreachable, or connect, write, read or pool timed out | Check `upstreams.*.url` and the network; raise the timeouts only if the tool is legitimately slow (`read_timeout_seconds` is the longest silence on a response) |
| `-31006` | The upstream stream broke before the reply was relayed | Check the upstream; retry |
| `404`, `-31007` ("The session was not found. Start a new session.") | The `Mcp-Session-Id` is unknown here, expired (`sessions.idle_ttl_seconds`), or bound to another user | Normal after an idle period: the client starts a new session. If it happens right after another request, **two replicas with `state.type` `memory`** (use Redis, chapter 5), or the encryption key was rotated (chapter 8) |
| `503` + `Retry-After: 5`, `-31013` (outcome `state_unavailable`) | The shared state (Redis) failed or timed out; the gateway fails closed and forwards nothing | Fix Redis; the replica also reports `GET /readyz` `503` and leaves the Service |
| A tool result `isError: true` "Rate limit reached for this account. Retry in N s.", or `-31014`, or HTTP `429` | A rate-limit bucket is empty (outcome `rate_limited`, reason `subject` or `organization`) | Wait `Retry-After`; raise `burst` or `per_minute` (chapter 8). With `memory` state each replica has its own buckets |
| `-32602`, "Unknown tool: x" | `policies.tools` denies the tool for this caller, or the tool does not exist (they look the same on purpose) | Check the rule that matches the caller (organization or resolver label) |
| `-31005` "The gateway could not apply its tool policy to this response." (HTTP `200`), outcome `tool_policy_failed` | A reply the gateway had to filter could not be filtered: not JSON, too large (`limits.max_filtered_response_bytes`, `limits.max_sse_event_bytes`), compressed, or wrongly shaped | Look at `reason` (the failure class); raise the limit if the reply is legitimately large |
| `-31005` "The service returned an error for this request." (outcome `upstream_error`) | The upstream answered a non-2xx to a `tools/list` while a policy is active | Check the upstream |
| `400`, `-32700` or `-32600` while a tool policy is active | Unreadable body, a `tools/call` without an id, or a batch containing `tools/call` or `tools/list` | The client must send one request per call |

## Streaming problems

| Symptom | Cause | Fix |
|---|---|---|
| Results of long tools arrive all at once at the end, or progress never shows | A proxy in front of the gateway **buffers** the Server-Sent Events | Turn buffering off (the gateway sends `X-Accel-Buffering: no`; ingress-nginx also needs `proxy-buffering: "off"`; other proxies have their own setting, chapter 5) |
| Streams are cut after a fixed time (30 s, 60 s, ...) although the tool is still working | An idle or request timeout in a proxy, load balancer or ingress shorter than your longest tool | Raise it above the largest `upstreams.*.read_timeout_seconds` (the chart's example uses `3600` s for ingress-nginx; Gateway API: `timeouts.request: 0s`) |
| The call finished at Alien but the client got nothing | The client disconnected: the gateway still read the answer, discarded it and Alien metered it (chapter 8.5) | Normal. See `mcp_gateway.upstream.detached` |

## The gateway does not start or is not ready

| Symptom | Cause | Fix |
|---|---|---|
| The pod or container exits at once with `startup refused: invalid MCP_GATEWAY_CONFIG: <key>: <rule>` | The configuration is invalid; the message names the key and the rule, never the value. Frequent: `public_url must be listed in auth.audiences`; `version: ... must declare "version": 1`; `sessions.max_entries is required when state.type is memory` or `must be null when state.type is redis`; an unknown key (every key is required, unknown ones are refused) | Fix the key named. Run `helm template` first: the chart checks the document |
| `startup refused: ... file unreadable` or `file is empty` | A `*_file` secret is missing, empty or not mounted | Mount the Secret; check the mount path equals the path in `config` |
| `helm install` fails with a message about shared state, grace period or ports | Render-time checks (chapter 5.3) | Read the message: it says which value to change |
| `GET /readyz` answers `503` | Redis unreachable (`state.type` `redis`) | Fix Redis, its URL file and its network path. `/healthz` stays `200`: the process is fine |
| Replicas disagree: a user is limited on one and not on the other, or sessions are lost | `state.type` `memory` with more than one replica | Use Redis (the chart refuses this combination) |

## What this tutorial could not confirm

- Copilot Studio: PKCE in manual mode, the `resource` parameter and CIMD (chapter 7).
- VS Code and Cursor: their redirect URIs are in the template README's table with the source of each; the VS Code user documentation does not state them.
- The origins that ChatGPT, Copilot Studio and VS Code send (chapter 7).
- Keycloak's HTTP `403` on a refused anonymous registration: it is the reported symptom, not reproduced here.
- Two gateway versions on one Redis (chapter 8.6).
