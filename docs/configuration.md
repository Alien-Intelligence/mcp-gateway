# Configuration reference

The gateway reads one JSON document from the environment variable `MCP_GATEWAY_CONFIG`. What clients see (error codes, `_meta` keys, statuses, headers) is the [protocol contract](protocol.md).

- Every key is required. There is no default for any key. An optional value is written as an explicit `null`.
- Unknown keys are refused at every level.
- Types are strict: `"5"` is not a number, `1` is not `true`.
- A secret is never written inline. Keys that hold one (`secret_file`, `dsn_file`, `url_file`, `headers_file`, `client_key_file`, `encryption_key_file`) take the path of a file.
- A bad document stops the process at start with `startup refused: invalid MCP_GATEWAY_CONFIG: <path>: <rule>` on stdout. The message names the key and the rule, never the value.

In the tables, "required" means the key must be present. Durations are in seconds. "gt 0" means strictly greater than zero.

## Top level

| Key | Type | Meaning |
|---|---|---|
| `version` | integer | Configuration schema version. Must be `1`; a missing or different value stops the process at start with a message naming the supported version. The JSON Schema of version 1 is [`config.schema.v1.json`](config.schema.v1.json). |
| `public_url` | string, absolute http(s) URL | The canonical URL of the MCP endpoint, as clients reach it. Its path must not be empty or `/`, and it carries no query or fragment. The gateway serves MCP on that path and must find the URL in `auth.audiences`. The protected-resource metadata is served at `/.well-known/oauth-protected-resource<path>`. |
| `listen` | object | Where the process listens. |
| `auth` | object | Token verification and identity claims. |
| `resolver` | object | How an identity becomes an Alien API key. |
| `resolver_cache` | object or `null` | Cache in front of the resolver. `null` disables it. |
| `upstreams` | object, at least one entry | Name to upstream MCP server. |
| `limits` | object | Request body and stream bounds. |
| `audit` | object | The audit log. |
| `observability` | object | Traces and metrics export. |
| `sessions` | object | Binding of upstream session ids to subjects. |
| `state` | object | Where session bindings and cached credentials live: in this process or in a Redis shared by every replica. |
| `policies` | object | Origin policy, tool policy and rate limits. |

`GET /healthz` answers `{"status":"ok"}` without authentication: it is the liveness probe and never looks at the state backend. `GET /readyz` is the readiness probe (see [`state`](#state)).

## `listen`

| Key | Type | Bounds | Meaning |
|---|---|---|---|
| `host` | string | non-empty | Bind address, for example `0.0.0.0`. |
| `port` | integer | 1 to 65535 | TCP port. |

## `auth`

Tokens are JWTs verified against the identity provider's key set. Only asymmetric algorithms are accepted.

| Key | Type | Bounds | Meaning |
|---|---|---|---|
| `issuer` | string, http(s) URL | | Required `iss`. Advertised as the authorization server in the protected-resource metadata. |
| `jwks_uri` | string, http(s) URL | | Where the signing keys are fetched. |
| `audiences` | list of strings | at least one; no blank entries, no spaces | Accepted `aud` values. Must contain `public_url`. |
| `algorithms` | list of strings | at least one; each one of `RS256 RS384 RS512 PS256 PS384 PS512 ES256 ES256K ES384 ES512 EdDSA` | Accepted signature algorithms. `HS*` and `none` are refused at start. |
| `required_scopes` | list of strings | entries non-empty, no spaces; may be empty | Scopes the token must carry (from `scope`, space separated, and/or `scp`, string or list). A token without them gets `403 insufficient_scope`. |
| `scopes_advertised` | list of strings | at least one; no blank entries, no spaces | The `scope` in the `WWW-Authenticate` challenge and in the metadata. |
| `clock_skew_seconds` | number | >= 0 | Leeway on `exp`, `nbf` and `iat`. |
| `jwks_cache_ttl_seconds` | number | gt 0 | How long a fetched key set is trusted before a refresh. |
| `jwks_max_stale_seconds` | number | >= 0 | How long past the TTL the cached key set is still served when a refresh fails. Past it, requests get `503` with `Retry-After`. |
| `jwks_fetch_timeout_seconds` | number | gt 0 | Timeout of one key-set fetch. |
| `identity` | object | | Where the identity sits in the claims. |

Authentication failures never reach the resolver or an upstream. `401` carries `WWW-Authenticate: Bearer resource_metadata="...", scope="..."` (plus `error` and `error_description` when a token was presented). A token whose claims give no subject is `401`.

### `auth.identity`

Each value is a dotted path into the verified claims: `ext.org` is `claims["ext"]["org"]`.

| Key | Type | Meaning |
|---|---|---|
| `subject` | string, non-empty dotted path | Stable user id. Required in the token and a string. |
| `email` | dotted path or `null` | The e-mail. |
| `email_verified` | dotted path or `null` | A claim that must be the boolean `true` before `email` is trusted. `null` means an e-mail is never trusted: the identity has no e-mail and a static mapping with an `email` matcher is refused at start. |
| `organization` | dotted path or `null` | The organization claim, a string. |
| `groups` | dotted path or `null` | A list of strings. |

A claim of the wrong type is a `401`. A missing optional claim is an absent value.

## `resolver`

The resolver turns the verified identity into an Alien API key, an upstream name and labels, or a denial. `type` selects the kind; the other keys depend on it. A resolver that fails closes the request (`500`, or `503` with `Retry-After: 5` when a backend is down).

The identity handed to every resolver is `subject`, `email` (verified only, else `null`), `organization` (or `null`) and `groups`.

Denials return `403` and a JSON-RPC error: `-31010` for `no_mapping`, `-31011` for `ambiguous_mapping`, `-31012` for any other reason. The client sees a generic message; the reason is in the audit line.

An answer that names an upstream not present in `upstreams` is a gateway error (`500`). An API key must be printable ASCII with no whitespace. The key is never logged.

### `static`

| Key | Type | Meaning |
|---|---|---|
| `type` | `"static"` | |
| `mapping_file` | string, non-empty path | JSON file described below. Read at start (a missing or broken file stops the gateway) and reloaded when its mtime or size changes. A reload that fails makes every request a `500` until the file is fixed. |

```json
{"entries": [
  {"match": {"email": "alice@example.com"}, "api_key_file": "alice.key", "upstream": "main", "labels": {"client": "example"}},
  {"match": {"group": "analysts"}, "api_key_file": "/run/secrets/analysts.key", "upstream": "main", "labels": {}}
]}
```

- `match`: exactly one of `subject`, `email`, `organization`, `group`, with a non-empty string. E-mail matching is case-insensitive.
- `api_key_file`: a path, relative to the directory of the mapping file or absolute. Read on every request, so a rotated key needs no restart. An unreadable or empty file is a `500` (the log names the entry number, not the key).
- `upstream`: a name from `upstreams`.
- `labels`: an object of strings, copied to the audit line and to span attributes. Required (use `{}`).
- Order of matching: `subject`, then `email`, then `organization`, then `group`. Two entries with the same matcher are refused at load. Two different group entries matching the same identity is `ambiguous_mapping`; none is `no_mapping`.

### `webhook`

The gateway sends the identity to an HTTPS endpoint and gets a decision.

| Key | Type | Bounds | Meaning |
|---|---|---|---|
| `type` | `"webhook"` | | |
| `url` | string, http(s) URL | | The endpoint. Must be `https` unless `allow_insecure_http` is true. |
| `auth` | object | | `{"type": "hmac", "secret_file": "<path>"}` or `{"type": "mtls", "client_cert_file": "<path>", "client_key_file": "<path>"}`. `mtls` needs an `https` URL. |
| `ca_file` | path or `null` | non-empty when set | CA bundle used to verify the webhook server, for both auth modes. `null` uses the system CAs. |
| `default_upstream` | string | non-empty; a name from `upstreams` | Used when an answer gives `upstream: null`. |
| `allow_insecure_http` | boolean | | Allows a plain `http` URL. For dev and test stacks only: the answer carries the API key. |
| `timeout_seconds` | number | gt 0 | One attempt: connect, send and read. |
| `max_attempts` | integer | 1 to 10 | Attempts for a retryable failure. |
| `total_timeout_seconds` | number | gt 0, and > `timeout_seconds` | Wall-clock ceiling for the whole resolution, attempts and pauses included. |
| `backoff_base_seconds` | number | gt 0 | First pause between attempts; doubles each time, with jitter. |
| `backoff_max_seconds` | number | gt 0, >= `backoff_base_seconds` | Cap of one pause. |
| `max_ttl_seconds` | number | gt 0 | Upper bound applied to the `ttl_seconds` an answer asks for. |

Request: `POST <url>`, `Content-Type: application/json`, a compact body with only what the gateway verified, never the token or its claims:

```json
{"subject":"user-1","email":"alice@example.com","organization":"example-org","groups":["analysts"]}
```

`email` and `organization` are `null` when absent. `groups` is a list.

With `hmac`, three headers authenticate the call:

- `X-MCP-Gateway-Timestamp`: unix seconds.
- `X-MCP-Gateway-Nonce`: 32 random hex characters, new for every attempt.
- `X-MCP-Gateway-Signature`: `sha256=<hex>` where the digest is `HMAC-SHA256(secret, "<timestamp>.<nonce>.<body>")` over the exact body bytes. The secret is the content of `secret_file`, stripped.

The receiver must compare in constant time, reject a timestamp outside a short window, and reject a nonce already seen inside that window (remember each accepted nonce until its timestamp leaves the window, with bounded memory). With `mtls` the gateway presents the client certificate and the receiver verifies it.

Response: HTTP `200` with a JSON body, one of:

```json
{"decision":"allow","api_key":"oat_...","upstream":"main","labels":{"client":"example"},"ttl_seconds":300}
{"decision":"deny","reason":"no_mapping"}
```

- `allow` has exactly these keys: `api_key` (string), `upstream` (a name from `upstreams`, or `null` for `default_upstream`), `labels` (object of strings), `ttl_seconds` (number gt 0, or `null`). The cache keeps the answer for `ttl_seconds`, capped by `max_ttl_seconds` and by `resolver_cache.positive_ttl_seconds`.
- `deny` has exactly `reason`, a token matching `[a-z][a-z0-9_]{0,63}`. It appears in the audit line.
- Anything else (another status, a missing or extra key, a wrong type, a body over 64 KiB) is a resolver error: the request gets a `500`.
- Connection errors, timeouts and statuses `429 502 503 504` are retried with exponential backoff and jitter, up to `max_attempts` and `total_timeout_seconds`. After that the request gets `503` with `Retry-After: 5`. Any other non-200 status is an immediate `500`. A TLS certificate the gateway cannot verify is a `500` at once, never retried. Redirects are not followed.

### `store_sql`

The key is looked up in a PostgreSQL table you fill in. The pool opens lazily: a database that is down at start does not stop the gateway; requests get `503` until it is back.

| Key | Type | Bounds | Meaning |
|---|---|---|---|
| `type` | `"store_sql"` | | |
| `dsn_file` | string path | non-empty | File holding the DSN (it carries the password). |
| `query` | string | one `SELECT`; see below | The lookup. |
| `default_upstream` | string | non-empty; a name from `upstreams` | Used when the row's `upstream` is null. |
| `pool_min_size` | integer | >= 1 | |
| `pool_max_size` | integer | >= `pool_min_size` | |
| `connect_timeout_seconds` | number | gt 0 | Connection and pool checkout. |
| `statement_timeout_seconds` | number | gt 0 | Set for the transaction only (`set_config(..., is_local)`), so nothing leaks onto a pooled or PgBouncer-shared session. |

Query contract, checked at start:

- A single `SELECT` statement, no `;` inside (one trailing `;` is allowed).
- Named parameters only, and at least one of `%(subject)s`, `%(email)s`, `%(organization)s`. `email` and `organization` are null when the identity has none. A literal percent sign is `%%`. Positional placeholders are refused.
- It runs in a read-only transaction. Prepared statements are never used.

Row contract: columns `api_key` (text), `upstream` (text, nullable: null picks `default_upstream`) and `labels` (json or jsonb object of strings, nullable: null means none). No row is `no_mapping`; more than one row is `ambiguous_mapping`. A missing column or a wrong type is a `500`. A timeout, a pool timeout or an unreachable database is a `503`.

```sql
SELECT api_key, upstream, labels FROM gateway_keys WHERE email = %(email)s
```

### `store_redis`

| Key | Type | Bounds | Meaning |
|---|---|---|---|
| `type` | `"store_redis"` | | |
| `url_file` | string path | non-empty | File holding the `redis://` or `rediss://` URL (it may carry the password). |
| `key_pattern` | string | at least one placeholder | Key to read. |
| `default_upstream` | string | non-empty; a name from `upstreams` | Used when the value's `upstream` is null. |
| `connect_timeout_seconds` | number | gt 0 | |
| `socket_timeout_seconds` | number | gt 0 | A lookup is also bounded by the sum of both. Client retries are disabled. |
| `max_connections` | integer | >= 1 | |

`key_pattern` uses `{subject}`, `{email}` and `{organization}`, and no other braces (checked at start). Each substituted value is percent-encoded over its UTF-8 bytes: every character except `A-Z a-z 0-9 _ . - ~` becomes `%XX`. Subject `alice@x.example` is stored under `...:alice%40x.example`. This keeps a value from containing the pattern's separators, so `a:b` + `c` and `a` + `b:c` cannot render the same key. Write your keys the same way (Python: `urllib.parse.quote(value, safe="")`). An identity with no value for a placeholder the pattern uses is `no_mapping`.

Value contract: a JSON object with exactly the keys `api_key` (string), `upstream` (a name, or `null` for `default_upstream`) and `labels` (object of strings, or `null`):

```json
{"api_key":"oat_...","upstream":"main","labels":{"client":"example"}}
```

A missing key is `no_mapping`. A value that is not text, not that JSON or of the wrong shape is a `500`. A connection error or timeout is a `503`; an authentication failure is a `500`.

### `chain`

| Key | Type | Bounds | Meaning |
|---|---|---|---|
| `type` | `"chain"` | | |
| `resolvers` | list of resolver objects | at least 2; each one of `static`, `webhook`, `store_sql`, `store_redis` | Tried in order. A chain cannot contain a chain. |

`no_mapping` from a member moves on to the next. Any other denial (including `ambiguous_mapping`) and any error stop the chain and are returned as is, so a request fails closed. Every member denying `no_mapping` is `no_mapping`.

## `resolver_cache`

`null` disables the cache, or an object:

| Key | Type | Bounds | Meaning |
|---|---|---|---|
| `positive_ttl_seconds` | number | gt 0 | Lifetime of an answer that carries a key. A webhook `ttl_seconds` can only shorten it. |
| `negative_ttl_seconds` | number | gt 0 | Lifetime of a denial. Keep it short so a new mapping works soon. |
| `max_entries` | integer or `null` | >= 1 | Least recently used entries go first. Required with `state.type` `memory`, `null` with `redis` (shared entries expire by their TTL only). |

The key is the identity (subject, e-mail, organization, sorted groups). Errors are never cached. Concurrent misses for one identity share one backend call (per process: two replicas missing at once each ask the backend). When an upstream answers `401` or `402` for a cached key, the entry is dropped so the next request asks the backend again.

## `upstreams`

An object from upstream name to settings. The name is what resolvers return in `upstream` and appears in the audit line and in telemetry. The set of names is bounded by this file.

| Key | Type | Bounds | Meaning |
|---|---|---|---|
| `url` | string, http(s) URL | | The upstream MCP endpoint. Requests are forwarded to exactly this URL. |
| `connect_timeout_seconds` | number | gt 0 | |
| `read_timeout_seconds` | number | gt 0 | Longest silence on a response. It is also the ceiling for an upstream request that runs on after its client left (see [Client disconnects](#client-disconnects)); it is also the time requests in flight get at shutdown (see [Shutdown](#shutdown)). Under `policies.tools` it is also the wall-clock total for reading a buffered `tools/list` JSON body (see `limits.max_filtered_response_bytes`): a body still arriving after that many seconds ends the request with `504` (`-31004`). |
| `write_timeout_seconds` | number | gt 0 | |
| `pool_timeout_seconds` | number | gt 0 | Wait for a pooled connection. |
| `max_connections` | integer | gt 0 | Size of the connection pool to this upstream: the most connections open at once, requests in flight and requests running on after their client left alike. |
| `max_detached_reads` | integer | gt 0, below `max_connections` | The most upstream requests of this upstream that may run on after their client left (see [Client disconnects](#client-disconnects)). Beyond it a leaving client's request is cancelled and closed instead (counted `shed`), so clients who left can never hold every connection and starve those still here. Size `max_connections` for the expected concurrent requests plus this. |

Only these client headers reach the upstream: `Accept`, `Content-Type`, `Mcp-Session-Id`, `Mcp-Protocol-Version`, `Last-Event-ID`, `Mcp-Method`, `Mcp-Name`, `Mcp-Param-*` and `User-Agent`. The gateway adds `Authorization: Bearer <api key>` and `Accept-Encoding: identity`. Trace context is not propagated to the upstream. On the way back, hop-by-hop headers, `WWW-Authenticate`, `Set-Cookie`, `Date`, `Server` and `X-Request-Id` are removed.

Gateway-made errors: an upstream `401` or `402` is never relayed; the client gets a JSON-RPC error (`-31001` or `-31002`) with HTTP `200` for a request that expects a reply and `502` otherwise. A timeout is `504` (`-31004`), an unreachable upstream `502` (`-31003`).

## Client disconnects

A client that disconnects does not cancel an upstream request the gateway already sent. This is a deliberate decision ("disconnect is not a cancellation"), and it departs from the 2026-07-28 MCP rule that closing the stream cancels the request: the upstream finishes its work (and meters it), and the connection goes back to its pool cleanly. The only cancellation the gateway honours is an explicit `notifications/cancelled` from the client (2025 protocol versions): it is forwarded like any other notification, and no tool policy blocks it.

| Request | Client disconnects |
|---|---|
| `POST` carrying a JSON-RPC request (an `id` and a `method`), already sent upstream | The gateway keeps reading the upstream response and discards it (nothing is buffered), then closes it. This holds while waiting for the response headers, during an SSE or JSON relay, while a `tools/list` body is read for filtering under `policies.tools`, and while the body of an upstream `401`, `402` or failed `tools/list` is drained (the rejected credential is still invalidated). |
| `POST` notification or client response (no reply expected), `GET` listen stream, `DELETE` | The upstream response is closed at once. There is nothing to complete. |
| Any request not yet sent upstream (authentication, rate limit, credential lookup, session check) | Abandoned: nothing is in flight, so nothing is forwarded. |

A response that arrives after the client left, during the wait for the headers, still counts for what it says: an upstream `401` or `402` invalidates the credential and a `404` drops the session binding. A session id an `initialize` issues in such a response is not bound (the client never learned it): the upstream session expires on its own.

The request's audit line is written when the client leaves: status `499`, outcome `client_disconnected`. What the upstream does afterwards is not a second audit line, it is the metric and the log line below.

**Ceiling.** The part of a request that runs on after its client left is bounded by the upstream's `read_timeout_seconds` as a wall-clock total, on top of the per-read timeouts (the clock starts when the client leaves). When it expires the upstream response is closed and the request is counted as `timeout`. The reads are owned by the application: how they end at shutdown is described in [Shutdown](#shutdown).

**Metrics.** `mcp_gateway.upstream.detached` counts upstream requests that ran on after their client left, by `upstream` and `result`: `completed` (the response was read to its end), `timeout` (the ceiling, or an upstream read timeout), `failed` (the upstream broke the stream), `shutdown` (still running when the gateway stopped, so it was cancelled) or `shed` (the upstream already had `max_detached_reads` such requests, so this one was cancelled and closed, with a WARNING log). `mcp_gateway.upstream.detached.active` is the number running now. Each one also logs one INFO line (`detached upstream read ended`) with the upstream name, the result and the duration.

## Shutdown

On `SIGTERM` the gateway stops in three steps, and the whole stop is bounded by a figure derived from the configuration, not a key of its own.

1. **Stop accepting, close the listen streams.** The listener closes and every listen stream (`GET`) is ended at once: the response body finishes cleanly, with no error event, and the client reconnects (to another replica when there is one, which finds the session because the replicas share their state). The audit line of such a stream has outcome `forwarded` and `reason` `shutdown`. Idle keep-alive connections are closed.
2. **In-flight requests get the largest `upstreams.*.read_timeout_seconds`** (rounded up to whole seconds) to finish: a `tools/call` that is still running relays its reply to its client. A request still running when that time ends is cancelled; one that was already sent upstream carries on as a detached read (see above).
3. **The application's shutdown.** The detached reads in progress get 10 s to finish, the ones cancelled 10 s more to close their responses. Then the resolver, the shared state and the HTTP clients are closed and the telemetry is flushed: with `observability.otlp` set, the span exporter and then the metric exporter, each within `timeout_seconds` + 2 s.

The process exits by itself within the sum, the **stop budget**: the largest `read_timeout_seconds` + 0.1 s + 20 s + (with OTLP) 2 x (`otlp.timeout_seconds` + 2 s). With one upstream at `read_timeout_seconds: 130` and no OTLP that is 150.1 s. The gateway logs the figure at start (`stop sequence: ...`). Whatever stops the process must wait at least that long, or it kills it before step 3 ran and the telemetry and the detached-read counts are lost: the Helm chart computes `terminationGracePeriodSeconds` from it (preStop sleep and a margin included) and refuses a smaller value; for Docker Compose set `stop_grace_period` above it (the example uses 160 s for the example configurations). The figure is computed in `src/mcp_gateway/shutdown.py`.

The time to wait for in-flight requests is the read timeout and not a key of its own on purpose: it is the time the gateway already accepts to wait on an upstream that stays silent, and a second knob would have to be kept above or below it. A request that streams progress for longer than that is cut at step 2 all the same: its client sees the stream end without the reply, and the upstream request finishes as a detached read.

## `limits`

| Key | Type | Bounds | Meaning |
|---|---|---|---|
| `max_request_body_bytes` | integer | gt 0 | Larger bodies get `413` without reaching an upstream. |
| `request_body_timeout_seconds` | number | gt 0 | The whole body must arrive within it, else `408`. |
| `max_sse_event_bytes` | integer | gt 0 | One SSE event above it aborts the stream: the relay never buffers more than one event. |
| `max_filtered_response_bytes` | integer | gt 0 | The largest `application/json` `tools/list` response the gateway buffers to filter it (see `policies.tools`). A larger one is replaced by a `-31005` error, never relayed. The read of that body is also bounded in time: the upstream's `read_timeout_seconds` is a wall-clock total for the whole body, not only for each silence, and when it runs out the answer is `504` (`-31004`), not a partial list. Not used while `policies.tools` is `null`. |

## `sessions`

An upstream `Mcp-Session-Id` is bound to the subject it was issued to, in the backend chosen by [`state`](#state). A request that presents an unknown, expired or foreign session id gets `404` (`-31007`) and is not forwarded.

| Key | Type | Bounds | Meaning |
|---|---|---|---|
| `max_entries` | integer or `null` | gt 0 | Total bindings. When full, expired ones go first, then the least recently used. Required with `state.type` `memory`, `null` with `redis` (bindings expire by idle TTL and the per-subject cap there). |
| `max_entries_per_subject` | integer | gt 0, <= `max_entries` when it is set | One subject over the cap evicts its own oldest binding. |
| `idle_ttl_seconds` | number | gt 0 | A binding unused for this long expires. |

## `state`

State that must agree between replicas lives here: the session bindings (`sessions`), the resolver cache (`resolver_cache`) and the rate limit buckets (`policies.rate_limits`). Choose `memory` for a single replica or `redis` for several; the gateway never picks for you and never falls back from one to the other.

### `memory`

`{"type": "memory"}`. Bindings and cache entries live in the process. This is correct for exactly one replica: behind a load balancer with two replicas, a session bound on one would be unknown to the other (`404`), and an upstream `401` that drops a cached credential on one replica would not reach the other. `sessions.max_entries` and (when the cache is on) `resolver_cache.max_entries` are required.

### `redis`

```json
"state": {
  "type": "redis",
  "url_file": "/run/secrets/state-redis-url",
  "key_prefix": "mcp-gateway:",
  "connect_timeout_seconds": 1,
  "operation_timeout_seconds": 1,
  "max_connections": 16,
  "encryption_key_file": "/run/secrets/state-encryption-key"
}
```

| Key | Type | Bounds | Meaning |
|---|---|---|---|
| `type` | `"redis"` | | |
| `url_file` | string | non-empty | File holding a `redis://` or `rediss://` URL. It may carry the password, so it is never inline. |
| `key_prefix` | string | 1 to 64 characters of `A-Z a-z 0-9 _ : . -` | Prepended to every key, so several gateways can share one Redis. |
| `connect_timeout_seconds` | number | gt 0 | Socket connect timeout. |
| `operation_timeout_seconds` | number | gt 0 | Ceiling for every Redis operation, a script and the readiness ping included. There are no retries. |
| `max_connections` | integer | >= 1 | Connection pool size. When every connection is busy a command waits for one instead of failing; that wait consumes the same `operation_timeout_seconds` as the command itself, so a saturated pool surfaces as a timeout (`503`), never as an immediate refusal. |
| `encryption_key_file` | string | non-empty | File holding the base64 of exactly 32 random bytes: the AES-256-GCM key of cached credentials. Generate it with `head -c 32 /dev/urandom \| base64`. A missing, empty, non-base64 or wrongly sized file stops the process at start; the message never echoes the content. |

With `redis`, `sessions.max_entries` and `resolver_cache.max_entries` must be `null`: the config is refused otherwise (nothing is silently ignored). Entries are bounded by their TTLs and by `sessions.max_entries_per_subject`.

Keys (`p` is `key_prefix`). Nothing below carries a session id, a subject, an issuer, an e-mail or an API key in clear: key names hold keyed digests and values hold digests or ciphertext. `h(...)` below is HMAC-SHA256 under a key derived (HKDF-SHA256, `info` `mcp-gateway key names`) from the key in `encryption_key_file`, so a key name cannot be recomputed from a guessed subject or session id without that key. The consequence: every replica sharing a Redis must share the key file, and changing the key renames every key (see the rotation note below).

| Key | Holds | Expiry |
|---|---|---|
| `{p}s:{h(upstream, session id)}` | `h(subject key)`: the binding. | `sessions.idle_ttl_seconds`, refreshed on use. |
| `{p}ss:{h(subject key)}` | Sorted set of that subject's binding digests, scored by last use. It enforces `max_entries_per_subject` by evicting the subject's own oldest binding. | `sessions.idle_ttl_seconds`, refreshed on use. |
| `{p}c:{h(identity)}` | The cached credential or denial, AES-256-GCM encrypted, after a 4-byte key id (random 12-byte nonce; `{p}` and the entry digest as associated data, so a value copied to another entry or another gateway's prefix is refused). The absolute expiry is sealed inside the value. | The entry's TTL. |
| `{p}cg:{h(identity)}` | Invalidation generation of that identity. | One day, refreshed on each invalidation. |

Binding, checking and dropping a session are each one Lua script, so replicas cannot race a binding or the per-subject cap. The scripts derive the per-subject key from a stored digest: use one Redis (a primary with replicas is fine), not a sharded Redis Cluster. A value past its sealed expiry is a miss even if Redis still holds it (for example one replayed without its TTL); the expiry is the writing replica's wall clock (`time.time()`), so replicas whose clocks disagree by more than a cache TTL see entries expire early or late: keep them on NTP. A cached value that is malformed, or that carries this replica's key id but fails authentication (tampering, truncation), is deleted and counted as a miss, with a warning that carries no content.

**Rotating `encryption_key_file`.** Every cached value starts with a 4-byte key id derived from the key (HKDF-SHA256, `info` `mcp-gateway key id`). A value with another key id is counted as a miss and left alone (DEBUG log at most): it belongs to a replica still on the other key. Because key names derive from the same key, a replica on the new key also looks under different names, so during a rolling update the two generations simply do not see each other's entries: the cache of the new generation starts empty (the credential backend is asked once per identity) and entries of the old key expire by their TTL. Session bindings and rate limit buckets are keyed the same way, so a session opened on an old-key replica is unknown (`404`) to a new-key replica and rate limit buckets restart full. Rotate in a quiet window, or accept those effects; there is no outage, and no replica fails requests because of the other generation.

An upstream `401` or `402` deletes the cached entry and bumps its generation. A lookup writes its answer only if the generation is unchanged since before it asked the backend, so a lookup still running on replica B cannot put back a credential that replica A just invalidated.

**Fail closed.** If Redis errors, refuses the connection or exceeds `operation_timeout_seconds`, the request is not forwarded: the gateway answers `503` with `Retry-After: 5`, JSON-RPC error `-31013` ("The gateway's shared state is unavailable. Retry shortly.") and audit outcome `state_unavailable`. A session id is never honoured without its check, and the resolver is never called around a broken cache. If binding a new session fails after the upstream answered `initialize`, the upstream response is discarded and the client gets that `503`, so it never holds a session the gateway cannot check. Refreshing or dropping a binding after the response is already decided (end of a stream, a successful `DELETE`, an upstream `404`) cannot change that response: the failure is logged at ERROR and the stale binding expires by its TTL.

**Readiness.** `GET /readyz` answers `{"status":"ready"}` (`200`) with `memory`. With `redis` it sends `PING` within `operation_timeout_seconds` and answers `200` `{"status":"ready"}`, or `503` `{"status":"state_unavailable"}`. Point the load balancer's health check at it so a replica that lost Redis is drained; keep `/healthz` for liveness (restarting a replica does not fix Redis).

**Metrics.** The `mcp_gateway.sessions.bound` gauge counts the sessions in this process, so it exists with `memory` only; with `redis` it is not exported (a global count would need a scan).

## `policies`

| Key | Type | Meaning |
|---|---|---|
| `allowed_origins` | list of strings | Browser origins allowed on the MCP endpoint, `scheme://host[:port]`, http or https only. Scheme and host are lowercased and a default port is dropped. An entry with a path, query, fragment or userinfo is refused, and so is `null`. A request with an `Origin` header not in the list gets `403` (`-31008`) before authentication. An empty list refuses every request that carries an `Origin`; requests without one pass. |
| `tools` | object or `null` | Tool allow/deny policy, see below. `null` means no policy: the gateway never parses or rewrites a response, and relays bytes exactly as the upstream sent them. |
| `rate_limits` | object or `null` | Token buckets per subject and, optionally, per organization. `null` turns rate limiting off. See below. |

### `policies.tools`

```json
{
  "default": {"allow": ["search", "get_*"], "deny": []},
  "rules": [
    {"match": {"organization": "example-org"}, "allow": null, "deny": ["delete_*"]},
    {"match": {"label": {"name": "tier", "value": "basic"}}, "allow": ["search"], "deny": []}
  ]
}
```

| Key | Type | Meaning |
|---|---|---|
| `default` | object | The policy of every caller that matches no rule. |
| `rules` | list | Tried in order; the first rule that matches is the caller's policy, `default` is not merged into it. |

A policy (`default` and each rule) has two required keys: `allow` is `null` (every tool is allowed) or a list of patterns (only the tools they match); `deny` is a list of patterns, and a tool that matches one is refused whatever `allow` says. A rule adds `match`, which is exactly one of `{"organization": "<value>"}`, compared with the organization claim of the token, or `{"label": {"name": "<name>", "value": "<value>"}}`, compared with the labels the credential resolver returned. A caller without an organization claim never matches an `organization` rule.

A pattern is a tool name, or a name with `*` standing for any run of characters (`get_*`, `*_read`, `a*b`). `*` is the only wildcard: every other character, regular-expression characters included, is literal. A pattern is 1 to 256 characters with no whitespace or control characters.

What a caller sees:

- `tools/call` of a tool outside its policy: HTTP `200` with JSON-RPC error `-32602`, message `Unknown tool: <name>`, the request id. A denied tool answers exactly like a tool that does not exist, and the request is never forwarded. Audit outcome `tool_denied`. A `tools/call` whose `params.name` is missing or not a string is refused the same way with message `Invalid params`, because the gateway never forwards what it cannot check.
- `tools/list`: the upstream result is relayed with the tools outside the policy removed (a tool without a string `name` is removed too). Every other field, `ttlMs` and `nextCursor` included, is untouched, except `cacheScope`: when the result has that key it is set to `"private"` (a filtered list varies per caller); a result without it stays without it. The same applies to a JSON response and to the event that carries the reply in an SSE stream; the other events of the stream (progress, notifications) are relayed byte for byte. An upstream JSON-RPC error is relayed unchanged.
- Every SSE stream is filtered while a policy is active, whichever request opened it: the stream of a `tools/list`, of a `tools/call` or any other `POST`, and a `GET` stream (including one resumed with `Last-Event-ID`, which replays old events). The gateway looks at every event of the stream that is a JSON-RPC response (has `result` or `error`, no `method`) and filters each one whose `result.tools` is a list, whatever its id and however many there are. Other events pass byte for byte. A JSON (not SSE) response is filtered only when the request was a `tools/list`, because a JSON body answers exactly its own `POST`. A leading UTF-8 byte order mark at the start of a stream is ignored, as the SSE specification says.
- A reply the gateway cannot filter is never relayed as it came. For a `tools/list` JSON body that means: not JSON, not the expected shape, a different id, a content encoding other than identity, larger than `limits.max_filtered_response_bytes`. For an event of any stream it means: data that is not strict UTF-8 JSON (invalid UTF-8, UTF-16, truncated) or is nested deeper than 128 levels, a JSON array, a reply to a `tools/list` without a tool list, or an event larger than `limits.max_sse_event_bytes` once rewritten. The client gets HTTP `200` with JSON-RPC error `-31005` ("The gateway could not apply its tool policy to this response."), as a JSON body or as an event of the stream that takes the place of the offending one: its `id` is the request id, `null` on a `GET` stream. Audit outcome `tool_policy_failed`, with the failure class in `reason` (`too_deep` for a document nested deeper than 128 levels, `unparsable` for one that is not strict UTF-8 JSON); a stream with several such events is audited once. Events with no data (comments, keep-alives, an empty `data` field) pass unchanged.
- A non-2xx upstream answer to a `tools/list` is not relayed as it came either: it keeps its HTTP status (a `404` still means the session ended) and any `Retry-After`, and its body becomes the JSON-RPC error `-31005` "The service returned an error for this request." with the request id. Audit outcome `upstream_error`, `reason` `upstream_status`. Non-2xx answers to other methods are relayed unchanged.
- A request body the gateway cannot read is refused with HTTP `400`, JSON-RPC error `-32700` "Parse error" and `id` `null`, and is not forwarded. Audit outcome `unparsable_request`, with `reason` `too_deep` when the body is refused for its nesting. Unreadable means a non-empty body that is not strict UTF-8 JSON forming an object or an array (invalid UTF-8, a byte order mark, UTF-16, a bare string or number, nesting deeper than 128 levels), or a `Content-Type` that names a charset other than UTF-8 (any letter case; an absent charset is fine). The gateway never forwards what it cannot check: a body read differently by the gateway and the upstream could carry a tool name the policy never saw. An empty body is not unreadable. With `policies.tools` set to `null`, such a body is forwarded untouched. A body nested past 128 levels is then forwarded as a request whose `id` the gateway cannot read, so it is handled like a notification: a rate limit answers it with the plain `429` instead of the `isError` result a `tools/call` gets, and a client that disconnects cancels the upstream request instead of letting it run on (see "Client disconnects").
- A request (an object with a `method`) whose `id` key is present but is not a string or an integer (a float, a boolean, `null`, an object or an array) is refused with HTTP `400`, `-32600` "Invalid Request" and `id` `null`, and is not forwarded. Audit outcome `invalid_request`. The same applies to each element of a batch. A float id is the same number as an integer to some parsers and not to others, so no reply could be matched to it safely. A `tools/call` or `tools/list` with no `id` key at all is refused the same way, because those methods are requests, never notifications. Other notifications (no `id` key) and responses sent by the client (no `method`) are not affected.
- A JSON-RPC batch (an array body) that holds a `tools/call` or a `tools/list` is refused with HTTP `400`, `-32600`, "Batched tool requests are not accepted. Send one request per call.", and is not forwarded. Audit outcome `batch_refused`. Batches of other methods are forwarded.

The policy runs after the credential is resolved and the session checked, before the request is forwarded.

### `policies.rate_limits`

```json
"rate_limits": {
  "per_subject": {"burst": 30, "per_minute": 60},
  "per_organization": {"burst": 200, "per_minute": 600}
}
```

| Key | Type | Bounds | Meaning |
|---|---|---|---|
| `per_subject` | object | required, never `null` | One bucket per subject (the issuer and the subject claim together). It is required because a subject whose token carries no organization is charged to no organization bucket: without a subject bucket it would be unlimited. |
| `per_organization` | object or `null` | needs `auth.identity.organization` when set | One bucket shared by every subject whose token carries the same organization. `null` does not limit organizations. The config is refused when it is set but `auth.identity.organization` is `null`: no request would carry an organization. |

`per_subject` must be present (use `"rate_limits": null` to turn the feature off). A bucket has two keys, both required:

| Key | Type | Bounds | Meaning |
|---|---|---|---|
| `burst` | integer | 1 to 1000000 | The bucket's capacity, and its level when it is created. |
| `per_minute` | number | >= 0.001, finite | The refill rate: `per_minute / 60` tokens per second, up to `burst`. The floor is one token per 1000 minutes. |

What a token is: every authenticated request to the MCP endpoint (any method: `POST`, `GET`, `DELETE`; a `tools/call`, a `tools/list`, a notification) takes one token from each bucket that applies to it. A request without a valid token never takes one. The check runs after authentication and the `Mcp-*` header checks and before the credential lookup, so a flood is refused without reaching the resolver backend or the upstream.

Burst against rate, worked example with `{"burst": 30, "per_minute": 60}`: a new subject can send 30 requests at once, then one per second. After 30 s of silence it has 30 tokens back. A subject sending steadily 1.5 requests per second drains the bucket at 0.5 tokens per second and is refused after about 60 s, then gets through one request per second. `burst` sets how large a spike is tolerated; `per_minute` sets the sustained rate.

Subject and organization: with both set, a request takes a token from both buckets, atomically. A token leaves both or neither, so an exhausted organization bucket does not drain the tokens of the subject that was refused. The retry delay is the wait until every bucket the request needs has a token. A subject whose token carries no organization (the claim is absent) is charged to its subject bucket only. A `policies.tools` rule matching on `organization` is likewise refused when `auth.identity.organization` is `null`: it could never match.

The refusal has three shapes, chosen so that the sender can read it:

| Request | Answer |
|---|---|
| A single `tools/call` with an id | HTTP `200` and a JSON-RPC result with `isError: true`, a text "Rate limit reached for this account. Retry in N s." and `_meta["alien.club/retryAfterSeconds"]: N`. Header `Retry-After: N`. |
| Any other single request with an id | HTTP `200` and a JSON-RPC error `-31014` with `data.retry_after_seconds: N`. Header `Retry-After: N`. |
| No reply possible (notification, client response, `GET`, `DELETE`, a body that is not a JSON-RPC request) | HTTP `429`, header `Retry-After: N` and a JSON-RPC error `-31014` with id `null`. |

Why not `429` everywhere: of the failures a gateway can return, only a tool execution error reliably reaches the model, which can then wait or tell the user. The MCP TypeScript SDK and the Python SDK 1.x treat any non-2xx answer as a transport failure (the error text is generic, `Retry-After` is not read, and the failure can spill into sibling requests), and VS Code puts the whole server in an error state on any status of 300 or more. A `200` never disturbs the connection. `N` is whole seconds, at least 1 and at most 86400 (one day). The audit line has outcome `rate_limited` and `reason` `subject` or `organization` (the bucket that takes longest to refill); the request is never forwarded.

Where the buckets live: they follow [`state`](#state). With `memory` each replica keeps its own buckets, which is exact for a single replica only; behind a load balancer the effective limit is the configured one times the number of replicas that see the subject. Buckets that have refilled completely are dropped, so memory follows the subjects active within one refill window and needs no setting. With `redis` all replicas share one set of buckets in the same Redis (keys `{p}rl:s:<h>` and `{p}rl:o:<h>`, `h` being the same keyed digest, never the subject or organization in clear), refilled by the Redis server clock so replicas never disagree on time, and every call is bounded by `state.operation_timeout_seconds`. When Redis fails the gateway fails closed: `503` with `Retry-After`, JSON-RPC error `-31013` ("The gateway's shared state is unavailable. Retry shortly.") and audit outcome `state_unavailable`; the request is never passed unmetered.

## `audit`

| Key | Type | Meaning |
|---|---|---|
| `enabled` | boolean | One JSON line per request on stdout. `false` writes none. Telemetry is not affected. |

### Audit line

One line per request, whatever way it ends, written by the same call that ends the span and counts the request. Keys, in order:

| Key | Type | Meaning |
|---|---|---|
| `ts` | string | UTC ISO 8601 with milliseconds. |
| `event` | string | Always `mcp_request`. |
| `request_id` | string | UUID4 generated by the gateway, returned to the client as `x-request-id` on every response the gateway produces or relays. An inbound `x-request-id` is ignored and never forwarded. |
| `trace_id` | string or null | 32 hex characters of the request's trace; `null` when tracing is off. |
| `subject` | string or null | Verified subject; `null` before authentication succeeds. |
| `organization` | string or null | Verified organization claim. |
| `labels` | object or null | The resolver's labels; `null` before a credential was resolved. |
| `http_method` | string | `POST`, `GET` or `DELETE`. |
| `rpc_method` | string or null | JSON-RPC `method` of the body; `null` for a body that is not a JSON-RPC request. |
| `rpc_id` | string, integer or null | JSON-RPC `id`. |
| `tool` | string or null | `params.name` of a `tools/call`. |
| `status` | integer | HTTP status the gateway answered (the upstream's for a relayed response, `499` when the client left). |
| `outcome` | string | See below. |
| `reason` | string or null | Machine reason: a resolver denial reason, the failure class of a broken stream, or of a reply or stream event the gateway could not filter, or `invalid_params` for a `tools/call` without a readable tool name, or `upstream_status` for a failed `tools/list` whose body was replaced, or the bucket that refused a `rate_limited` request (`subject` or `organization`). |
| `upstream` | string or null | Upstream name, once one was chosen. |
| `latency_ms` | number | From receipt to the end of the response (the end of the stream for SSE). |
| `upstream_status` | integer or null | Status the upstream answered. |
| `upstream_request_id` | string or null | The upstream's `x-request-id`, which is not relayed to the client; `null` when it sent none. |
| `upstream_session` | boolean or null | Whether the upstream response carried an `Mcp-Session-Id`. Presence only. |

Text values are clipped to 256 characters. The line never holds request or response content, arguments, tokens, API keys or session ids.

`outcome` is one of: `origin_refused`, `unauthenticated`, `insufficient_scope`, `idp_unavailable`, `request_body_timeout`, `body_too_large`, `mcp_header_mismatch`, `duplicate_header`, `resolver_unavailable`, `state_unavailable`, `gateway_error`, `denied`, `rate_limited`, `session_not_found`, `upstream_error`, `upstream_auth_rejected`, `upstream_stream_failed`, `tool_denied`, `batch_refused`, `unparsable_request`, `invalid_request`, `tool_policy_failed`, `forwarded`, `client_disconnected`, `internal_error`.

Example:

```json
{"ts":"2026-11-20T09:14:03.512+00:00","event":"mcp_request","request_id":"6f1c2b0e-5a47-4d8e-9d52-3b0a4c9e7f10","trace_id":"4bf92f3577b34da6a3ce929d0e0e4736","subject":"user-1","organization":"example-org","labels":{"client":"example"},"http_method":"POST","rpc_method":"tools/call","rpc_id":7,"tool":"search","status":200,"outcome":"forwarded","reason":null,"upstream":"main","latency_ms":182.4,"upstream_status":200,"upstream_request_id":"req-81c4","upstream_session":false}
```

## `observability`

| Key | Type | Meaning |
|---|---|---|
| `otlp` | object or `null` | `null` installs nothing: no exporter, no provider, no-op instruments. |

Providers are owned by the gateway. No global OpenTelemetry provider is ever set.

### `observability.otlp`

| Key | Type | Bounds | Meaning |
|---|---|---|---|
| `endpoint` | string, http(s) URL | no credentials, query or fragment | Collector base URL. With `http/protobuf` the gateway posts to `<endpoint>/v1/traces` and `<endpoint>/v1/metrics`. With `grpc` the endpoint is used as is; an `http` scheme means a plaintext channel. |
| `protocol` | `"grpc"` or `"http/protobuf"` | | |
| `headers_file` | path or `null` | non-empty when set | File of `key=value` lines sent with every export, for an auth header. Blank lines and lines starting with `#` are skipped; the name is everything before the first `=`; names are case-insensitive and may not repeat; a value may contain `=`. A missing, unreadable or empty file, or a bad line, stops the gateway at start (the message names the file and line, never a value). Never inline. |
| `allow_insecure_http` | boolean | required | Allows an `http` endpoint together with `headers_file`, which sends the auth header in clear. Without a `headers_file` or with an `https` endpoint it has no effect. For dev and test stacks only. |
| `timeout_seconds` | number | gt 0 | One export attempt. |
| `export_interval_seconds` | number | gt 0, >= `timeout_seconds` | Metric collection period, and the delay of the span batch. |
| `service_name` | string | non-empty | The `service.name` resource attribute. |
| `resource_attributes` | object of strings | non-empty keys; not `service.name` | Extra resource attributes. |

On shutdown both providers are flushed, each bounded by `timeout_seconds` plus 2 seconds. Other `OTEL_EXPORTER_OTLP_*` environment variables (for example the CA certificate) are still read by the exporters for the settings this block does not carry.

### Spans

Each request is a new root trace; an inbound `traceparent` is never its parent. A single valid inbound `traceparent` (and no `tracestate`) becomes a span link, so a client can correlate without choosing our trace or span ids. A malformed, all-zero or repeated one is ignored.

| Span | Kind | Attributes |
|---|---|---|
| `mcp-gateway request` | server | `http.request.method`, `http.response.status_code`, `mcp.method.name`, `mcp.tool.name`, `mcp_gateway.outcome`, `mcp_gateway.request_id`, `mcp_gateway.session.present` (an `Mcp-Session-Id` was sent), `mcp_gateway.upstream`, `mcp_gateway.upstream.status_code`, `mcp_gateway.reason`, `mcp_gateway.label.<key>` for each resolver label. Lasts until the response ends. |
| `mcp-gateway resolve_credential` | internal | `mcp_gateway.resolver.type` (`static`, `webhook`, `store_sql`, `store_redis`, `chain`), `mcp_gateway.resolver.cache` (`hit`, `miss`, `negative`, `none` when the cache is off), `mcp_gateway.resolver.result` (`credential`, `denied`, `unavailable`, `error`, `disconnected`, `aborted`). |
| `mcp-gateway upstream` | client | `mcp_gateway.upstream`, `http.response.status_code`, `mcp_gateway.upstream.error` (`timeout`, `unreachable`, `aborted`). Ends at the response headers. |

Error status is set on the request span for `status >= 500`, `upstream_stream_failed`, `tool_policy_failed` and `internal_error`, with no message and no recorded exception. Spans never carry the subject, e-mail, organization, a token, a key, a session id, arguments or content.

### Metrics

| Instrument | Type | Unit | Attributes |
|---|---|---|---|
| `mcp_gateway.requests` | counter | `{request}` | `outcome`, `status_class` (`2xx`..), `rpc_method`, `upstream` (`none` when no upstream was chosen) |
| `mcp_gateway.request.duration` | histogram | `s` | same as `mcp_gateway.requests` |
| `mcp_gateway.upstream.duration` | histogram | `s` | `upstream`, `status_class` (`error` when no response came). Time to the response headers. |
| `mcp_gateway.resolver.lookups` | counter | `{lookup}` | `resolver_type`, `cache`, `result` (values as in the resolver span) |
| `mcp_gateway.upstream.detached` | counter | `{request}` | `upstream`, `result` (`completed`, `timeout`, `failed`, `shed`, `shutdown`). Upstream requests that ran on after their client left. |
| `mcp_gateway.upstream.detached.active` | observable gauge | `{request}` | none |
| `mcp_gateway.sessions.bound` | observable gauge | `{session}` | none |
| `mcp_gateway.telemetry.failures` | observable counter | `{failure}` | `signal`: `record` (recording a finished request in process raised), `traces` or `metrics` (an OTLP export returned a failure or raised). All three are always reported, `0` included. |

Telemetry is best effort: it never changes a response that is already decided or sent. A failure while recording a request is logged at ERROR with its traceback (the message names the exception type and carries no request data) and counted under `signal="record"`, and the audit line is still written. OTLP export failures happen in the SDK's background threads, which only log them; the gateway counts them under `traces` and `metrics` without altering what the SDK sees. Alert on a rising `mcp_gateway.telemetry.failures`: a collector that is down is otherwise visible only in the logs.

`rpc_method` is one of the standard MCP methods or `other` (and `none` for a body that is not JSON-RPC): the method is client-chosen text, so it is bounded. Tool names and resolver labels are never metric attributes (their values come from clients and backends, not from this file); they are span attributes only.

## Complete example

Two upstreams, a static mapping with a webhook behind it, a cache, and OTLP over HTTP with an auth header from a file.

```json
{
  "version": 1,
  "public_url": "https://mcp.example.com/mcp",
  "listen": {"host": "0.0.0.0", "port": 8000},
  "auth": {
    "issuer": "https://idp.example.com/realms/main",
    "jwks_uri": "https://idp.example.com/realms/main/protocol/openid-connect/certs",
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
      "organization": "ext.org",
      "groups": "groups"
    }
  },
  "resolver": {
    "type": "chain",
    "resolvers": [
      {"type": "static", "mapping_file": "/etc/mcp-gateway/mapping.json"},
      {
        "type": "webhook",
        "url": "https://keys.example.com/resolve",
        "auth": {"type": "hmac", "secret_file": "/run/secrets/webhook-hmac"},
        "ca_file": null,
        "default_upstream": "main",
        "allow_insecure_http": false,
        "timeout_seconds": 3,
        "max_attempts": 3,
        "total_timeout_seconds": 8,
        "backoff_base_seconds": 0.2,
        "backoff_max_seconds": 2,
        "max_ttl_seconds": 600
      }
    ]
  },
  "resolver_cache": {"positive_ttl_seconds": 300, "negative_ttl_seconds": 15, "max_entries": 10000},
  "upstreams": {
    "main": {
      "url": "https://alien.example.com/mcp/main",
      "connect_timeout_seconds": 10,
      "read_timeout_seconds": 130,
      "write_timeout_seconds": 30,
      "pool_timeout_seconds": 10,
      "max_connections": 200,
      "max_detached_reads": 50
    },
    "legal": {
      "url": "https://alien.example.com/mcp/legal",
      "connect_timeout_seconds": 10,
      "read_timeout_seconds": 130,
      "write_timeout_seconds": 30,
      "pool_timeout_seconds": 10,
      "max_connections": 200,
      "max_detached_reads": 50
    }
  },
  "limits": {
    "max_request_body_bytes": 1048576,
    "request_body_timeout_seconds": 30,
    "max_sse_event_bytes": 65536,
    "max_filtered_response_bytes": 1048576
  },
  "audit": {"enabled": true},
  "observability": {
    "otlp": {
      "endpoint": "https://otel.example.com:4318",
      "protocol": "http/protobuf",
      "headers_file": "/run/secrets/otlp-headers",
      "allow_insecure_http": false,
      "timeout_seconds": 10,
      "export_interval_seconds": 15,
      "service_name": "mcp-gateway",
      "resource_attributes": {"deployment.environment": "production"}
    }
  },
  "sessions": {"max_entries": 10000, "max_entries_per_subject": 20, "idle_ttl_seconds": 1800},
  "state": {"type": "memory"},
  "policies": {
    "allowed_origins": ["https://app.example.com"],
    "tools": null,
    "rate_limits": {"per_subject": {"burst": 30, "per_minute": 60}, "per_organization": null}
  }
}
```

With tracing and metrics off, the block is `"observability": {"otlp": null}`.
