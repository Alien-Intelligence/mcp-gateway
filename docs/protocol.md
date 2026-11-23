# Public protocol contract (v1)

This page is the contract between the gateway and its clients: what a client, an operator's monitoring or a
test can rely on without reading the source. The configuration contract is [`configuration.md`](configuration.md)
with its JSON Schema [`config.schema.v1.json`](config.schema.v1.json).

**Stability.** Everything on this page is version 1. The error codes, the `_meta` key, the HTTP statuses and
challenge shapes, the headers and the `data` fields below change only with a major version of the gateway
(a new code may be added in a minor version; an existing code never changes meaning or number). Message texts
are for people and may be reworded in any release: match on the code, never on the text.

## Endpoints

| Path | Methods | Answer |
|---|---|---|
| the path of `public_url` (for example `/mcp`) | `GET`, `POST`, `DELETE` | The MCP endpoint, proxied to the upstream chosen for the user. |
| `/.well-known/oauth-protected-resource<path>` | any | OAuth Protected Resource Metadata (RFC 9728) for `public_url`: `resource`, `authorization_servers`, `scopes_supported`, `bearer_methods_supported: ["header"]`. Only the path-specific URL is served. |
| `/healthz` | `GET` | `200` `{"status": "ok"}`. |
| `/readyz` | `GET` | `200` `{"status": "ready"}`, or `503` `{"status": "state_unavailable"}` when the shared state cannot be reached. |

## JSON-RPC error codes

Gateway-made errors have the JSON-RPC shape `{"jsonrpc": "2.0", "id": <request id or null>, "error": {"code", "message", "data"?}}`.
`id` is the id of the request when it has one and a reply can be correlated, otherwise `null`. `data` is present
only where the table lists fields.

The gateway family is `-31001` to `-31015`. It sits outside `-32768..-32000` on purpose: the MCP 2026-07-28
specification says new implementation codes SHOULD be allocated outside that range, reserves `-32020..-32099`
for itself and leaves `-32000..-32019` to implementations, where the Python SDK already uses `-32000`
(connection closed) and `-32001` (request timeout). None of the spec's `schema.ts`, the TypeScript SDK `ProtocolErrorCode` or the Python SDK (`mcp_types.jsonrpc`,
`mcp.server.streamable_http`) uses a code in `-31999..-31000`.

| Code | Constant | When | HTTP status | `data` |
|---|---|---|---|---|
| `-31001` | `UPSTREAM_UNAUTHORIZED` | The upstream answered `401` to the credential the gateway holds for this user. The upstream body is never relayed. The cached credential is dropped. | `200` when the request expects a reply (has an id), otherwise `502` | `{"upstream_status": 401}` |
| `-31002` | `UPSTREAM_PAYMENT_REQUIRED` | The upstream answered `402`. Same handling as `-31001`. | `200` with an id, otherwise `502` | `{"upstream_status": 402}` |
| `-31003` | `UPSTREAM_UNREACHABLE` | Connection to the upstream failed (transport error). | `502` | none |
| `-31004` | `UPSTREAM_TIMEOUT` | Connect, write, read or pool timeout towards the upstream, including a buffered `tools/list` body that did not arrive in time. | `504` | none |
| `-31005` | `GATEWAY_ERROR` | The gateway could not do its job: the credential lookup failed, the resolver named an unknown upstream, an unexpected internal error, the tool policy could not be applied to a reply (it could not be parsed as strict UTF-8 JSON, it is nested deeper than 128 levels, it is too large, and so on), or the upstream answered a non-2xx to `tools/list`. | `500` for the first three; `200` when the tool policy could not be applied (as a JSON body, or as the SSE event that replaces the offending event); the upstream's own status for a non-2xx `tools/list` (with its `Retry-After` if any) | none |
| `-31006` | `UPSTREAM_STREAM_FAILED` | The upstream stream broke before the reply to the request was relayed. Sent as one final SSE event (`event: message`) on the already-open stream; any other mid-stream failure aborts the connection instead. | the status of the stream (normally `200`) | none |
| `-31007` | `SESSION_NOT_FOUND` | `Mcp-Session-Id` is unknown here, expired, or bound to another user. Not forwarded. The client starts a new session. | `404` | none |
| `-31008` | `ORIGIN_REFUSED` | An `Origin` header is present and not in `policies.allowed_origins`. Answered before authentication. `id` is `null`. | `403` | none |
| `-31009` | `RESOLVER_UNAVAILABLE` | The credential backend (webhook, store) could not be reached or timed out. Retry shortly. | `503`, header `Retry-After: 5` | none |
| `-31010` | `NO_MAPPING` | The signed-in user has no Alien account mapping (resolver reason `no_mapping`). No OAuth challenge. | `403` | none |
| `-31011` | `AMBIGUOUS_MAPPING` | The user matches more than one mapping (reason `ambiguous_mapping`). | `403` | none |
| `-31012` | `DENIED` | The resolver denied the user for any other reason. | `403` | none |
| `-31013` | `STATE_UNAVAILABLE` | The shared state (Redis) failed or timed out; the gateway fails closed and forwards nothing. | `503`, header `Retry-After: 5` | none |
| `-31014` | `RATE_LIMITED` | A rate limit bucket is empty, for any request that is not a `tools/call` with an id (see [Rate limit shapes](#rate-limit-shapes)). | `200` for a request with an id; `429` when no reply can be correlated (`id` null) | `{"retry_after_seconds": N}`, plus header `Retry-After: N` |
| `-31015` | `USERINFO_UNAVAILABLE` | The identity provider's userinfo endpoint could not be reached or kept failing, after the configured retries. The request is not forwarded. | `503`, with `Retry-After: 5` | none |

The audit line carries the outcome and, for denials, the reason; the client sees only the code.

### Standard codes the gateway also emits

| Code | Name | When | HTTP status |
|---|---|---|---|
| `-32700` | Parse error | Tool policy active and the body is not strict UTF-8 JSON (or is nested deeper than 128 levels), or a non-UTF-8 charset is declared. `id` is `null`. | `400` |
| `-32600` | Invalid Request | Request body too large (`413`) or not received in time (`408`); a repeated `Mcp-Session-Id` or `Mcp-Protocol-Version` header (`400`); with a tool policy active, a request whose `id` is neither a string nor an integer, a `tools/call` or `tools/list` without an id, or a batch holding a `tools/call` or `tools/list` (`400`). | `400`, `408` or `413` |
| `-32602` | Invalid params | Tool policy active: a `tools/call` naming a tool the user may not call (message `Unknown tool: <name>`, indistinguishable from a tool that does not exist) or without a usable `name`. | `200` |
| `-32020` | Header mismatch (MCP 2026-07-28, spec-defined) | `Mcp-Method` or `Mcp-Name` disagrees with the JSON-RPC body, is repeated, or is not valid base64 in the `=?base64?...?=` form. Headers that are absent are not checked. | `400` |

## `_meta` keys

| Key | Where | Value |
|---|---|---|
| `alien.club/retryAfterSeconds` | `result._meta` of the `isError` result that answers a rate-limited `tools/call` | integer, seconds to wait. |

Spec check (MCP 2026-07-28, `_meta` key rules): the prefix is a series of labels ending in `/`; labels start
with a letter and end with a letter or digit; only a prefix whose second label is `modelcontextprotocol` or `mcp`
is reserved. `alien.club/` is valid and not reserved. The spec's preference for reverse-DNS prefixes is a SHOULD;
this prefix is the one the Alien MCP servers already emit (`alien.club/cost`), so clients see one namespace.

## Rate limit shapes

A refused request is answered in the form its sender can read, always with `Retry-After: N`:

| Request | Answer |
|---|---|
| single `tools/call` with an id | HTTP `200`, a result with `isError: true`, one text content item and `_meta["alien.club/retryAfterSeconds"]: N`. |
| any other single request with an id | HTTP `200`, JSON-RPC error `-31014`, `data.retry_after_seconds: N`. |
| no reply possible (notification, client response, `GET`, `DELETE`, a body that is not a JSON-RPC request, which includes a body nested deeper than 128 levels that no tool policy refused) | HTTP `429`, JSON-RPC error `-31014` with `id` null, `data.retry_after_seconds: N`. |

## HTTP statuses and challenge shapes before any JSON-RPC

Authentication happens before the body is read. These answers are OAuth JSON, not JSON-RPC.

| Status | Body | Header | When |
|---|---|---|---|
| `401` | `{"error": "invalid_token", "error_description": "<text>"}` | `WWW-Authenticate: Bearer resource_metadata="<PRM url>", scope="<scopes_advertised, space separated>"` | No bearer token. |
| `401` | same | `WWW-Authenticate: Bearer resource_metadata="...", scope="...", error="invalid_token", error_description="<text>"` | Token invalid, expired, wrong audience or issuer, or its claims do not identify a user. |
| `403` | `{"error": "insufficient_scope", "error_description": "<text>"}` | `WWW-Authenticate: Bearer resource_metadata="...", scope="<required_scopes>", error="insufficient_scope", error_description="<text>"` | Token valid, a required scope is missing. |
| `503` | `{"error": "temporarily_unavailable", "error_description": "identity provider keys are unavailable"}` | `Retry-After: 5` | The identity provider's signing keys cannot be fetched. |
| `499` | empty | none | The client left before the answer; nothing is written to anyone (the status is only logged and audited). |

A `401` from the upstream is never turned into one of these: it becomes `-31001` so that the client does not
loop on OAuth re-authentication.

## Headers

Added or set by the gateway:

- Response: `x-request-id` (the gateway's request id on every answer; an upstream `x-request-id` is dropped and audited),
  `x-accel-buffering: no` on relayed upstream responses, `Retry-After` where listed above.
- Request to the upstream: `authorization: Bearer <the user's Alien API key>` (the client's token never leaves the gateway)
  and `accept-encoding: identity`.

Forwarded from the client to the upstream (allow-list, case-insensitive): `accept`, `content-type`, `mcp-session-id`,
`mcp-protocol-version`, `last-event-id`, `mcp-method`, `mcp-name`, `user-agent`, and every header starting with `mcp-param-`.
Nothing else is forwarded; in particular `authorization`, `cookie`, `x-forwarded-*`, `forwarded`, `x-real-ip`,
`x-request-id`, `host` and the `x-job-id`, `x-organization-id`, `x-mcp-config-id`, `x-source-type`,
`x-mcp-session-id` family are stripped.

Stripped from the upstream's response: hop-by-hop headers (`connection`, `keep-alive`, `transfer-encoding`, `te`,
`trailer`, `upgrade`, anything `Connection` names, `proxy-*`), `www-authenticate`, `set-cookie`, `date`, `server`.
Everything else, including `mcp-session-id`, is relayed.

Single-value request headers: a repeated `Mcp-Session-Id` or `Mcp-Protocol-Version` is refused with `-32600`.
