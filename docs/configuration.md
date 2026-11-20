# Configuration reference

The gateway reads one JSON document from the environment variable `MCP_GATEWAY_CONFIG`.

- Every key is required. There is no default for any key. An optional value is written as an explicit `null`.
- Unknown keys are refused at every level.
- Types are strict: `"5"` is not a number, `1` is not `true`.
- A secret is never written inline. Keys that hold one (`secret_file`, `dsn_file`, `url_file`, `headers_file`, `client_key_file`) take the path of a file.
- A bad document stops the process at start with `startup refused: invalid MCP_GATEWAY_CONFIG: <path>: <rule>` on stdout. The message names the key and the rule, never the value.

In the tables, "required" means the key must be present. Durations are in seconds. "gt 0" means strictly greater than zero.

## Top level

| Key | Type | Meaning |
|---|---|---|
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
| `policies` | object | Origin policy. |

`GET /healthz` answers `{"status":"ok"}` without authentication.

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

The identity handed to every resolver is `subject`, `email` (verified only, else null), `organization` (or null) and `groups`.

Denials return `403` and a JSON-RPC error: `-32010` for `no_mapping`, `-32011` for `ambiguous_mapping`, `-32012` for any other reason. The client sees a generic message; the reason is in the audit line.

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
| `max_entries` | integer | >= 1 | Least recently used entries go first. |

The key is the identity (subject, e-mail, organization, sorted groups). Errors are never cached. Concurrent misses for one identity share one backend call. When an upstream answers `401` or `402` for a cached key, the entry is dropped so the next request asks the backend again.

## `upstreams`

An object from upstream name to settings. The name is what resolvers return in `upstream` and appears in the audit line and in telemetry. The set of names is bounded by this file.

| Key | Type | Bounds | Meaning |
|---|---|---|---|
| `url` | string, http(s) URL | | The upstream MCP endpoint. Requests are forwarded to exactly this URL. |
| `connect_timeout_seconds` | number | gt 0 | |
| `read_timeout_seconds` | number | gt 0 | Longest silence on a response. |
| `write_timeout_seconds` | number | gt 0 | |
| `pool_timeout_seconds` | number | gt 0 | Wait for a pooled connection. |

Only these client headers reach the upstream: `Accept`, `Content-Type`, `Mcp-Session-Id`, `Mcp-Protocol-Version`, `Last-Event-ID`, `Mcp-Method`, `Mcp-Name`, `Mcp-Param-*` and `User-Agent`. The gateway adds `Authorization: Bearer <api key>` and `Accept-Encoding: identity`. Trace context is not propagated to the upstream. On the way back, hop-by-hop headers, `WWW-Authenticate`, `Set-Cookie` and `X-Request-Id` are removed.

Gateway-made errors: an upstream `401` or `402` is never relayed; the client gets a JSON-RPC error (`-32001` or `-32002`) with HTTP `200` for a request that expects a reply and `502` otherwise. A timeout is `504` (`-32004`), an unreachable upstream `502` (`-32003`).

## `limits`

| Key | Type | Bounds | Meaning |
|---|---|---|---|
| `max_request_body_bytes` | integer | gt 0 | Larger bodies get `413` without reaching an upstream. |
| `request_body_timeout_seconds` | number | gt 0 | The whole body must arrive within it, else `408`. |
| `max_sse_event_bytes` | integer | gt 0 | One SSE event above it aborts the stream: the relay never buffers more than one event. |

## `sessions`

An upstream `Mcp-Session-Id` is bound in memory to the subject it was issued to. A request that presents an unknown, expired or foreign session id gets `404` (`-32007`) and is not forwarded.

| Key | Type | Bounds | Meaning |
|---|---|---|---|
| `max_entries` | integer | gt 0 | Total bindings. When full, expired ones go first, then the least recently used. |
| `max_entries_per_subject` | integer | gt 0, <= `max_entries` | One subject over the cap evicts its own oldest binding. |
| `idle_ttl_seconds` | number | gt 0 | A binding unused for this long expires. |

## `policies`

| Key | Type | Meaning |
|---|---|---|
| `allowed_origins` | list of strings | Browser origins allowed on the MCP endpoint, `scheme://host[:port]`, http or https only. Scheme and host are lowercased and a default port is dropped. An entry with a path, query, fragment or userinfo is refused, and so is `null`. A request with an `Origin` header not in the list gets `403` (`-32008`) before authentication. An empty list refuses every request that carries an `Origin`; requests without one pass. |

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
| `reason` | string or null | Machine reason: a resolver denial reason, or the failure class of a broken stream. |
| `upstream` | string or null | Upstream name, once one was chosen. |
| `latency_ms` | number | From receipt to the end of the response (the end of the stream for SSE). |
| `upstream_status` | integer or null | Status the upstream answered. |
| `upstream_request_id` | string or null | The upstream's `x-request-id`, which is not relayed to the client; `null` when it sent none. |
| `upstream_session` | boolean or null | Whether the upstream response carried an `Mcp-Session-Id`. Presence only. |

Text values are clipped to 256 characters. The line never holds request or response content, arguments, tokens, API keys or session ids.

`outcome` is one of: `origin_refused`, `unauthenticated`, `insufficient_scope`, `idp_unavailable`, `request_body_timeout`, `body_too_large`, `mcp_header_mismatch`, `duplicate_header`, `resolver_unavailable`, `gateway_error`, `denied`, `session_not_found`, `upstream_error`, `upstream_auth_rejected`, `upstream_stream_failed`, `forwarded`, `client_disconnected`, `internal_error`.

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

Error status is set on the request span for `status >= 500`, `upstream_stream_failed` and `internal_error`, with no message and no recorded exception. Spans never carry the subject, e-mail, organization, a token, a key, a session id, arguments or content.

### Metrics

| Instrument | Type | Unit | Attributes |
|---|---|---|---|
| `mcp_gateway.requests` | counter | `{request}` | `outcome`, `status_class` (`2xx`..), `rpc_method`, `upstream` (`none` when no upstream was chosen) |
| `mcp_gateway.request.duration` | histogram | `s` | same as `mcp_gateway.requests` |
| `mcp_gateway.upstream.duration` | histogram | `s` | `upstream`, `status_class` (`error` when no response came). Time to the response headers. |
| `mcp_gateway.resolver.lookups` | counter | `{lookup}` | `resolver_type`, `cache`, `result` (values as in the resolver span) |
| `mcp_gateway.sessions.bound` | observable gauge | `{session}` | none |
| `mcp_gateway.telemetry.failures` | observable counter | `{failure}` | `signal`: `record` (recording a finished request in process raised), `traces` or `metrics` (an OTLP export returned a failure or raised). All three are always reported, `0` included. |

Telemetry is best effort: it never changes a response that is already decided or sent. A failure while recording a request is logged at ERROR (exception type only) and counted under `signal="record"`, and the audit line is still written. OTLP export failures happen in the SDK's background threads, which only log them; the gateway counts them under `traces` and `metrics` without altering what the SDK sees. Alert on a rising `mcp_gateway.telemetry.failures`: a collector that is down is otherwise visible only in the logs.

`rpc_method` is one of the standard MCP methods or `other` (and `none` for a body that is not JSON-RPC): the method is client-chosen text, so it is bounded. Tool names and resolver labels are never metric attributes (their values come from clients and backends, not from this file); they are span attributes only.

## Complete example

Two upstreams, a static mapping with a webhook behind it, a cache, and OTLP over HTTP with an auth header from a file.

```json
{
  "public_url": "https://mcp.example.com/mcp",
  "listen": {"host": "0.0.0.0", "port": 8000},
  "auth": {
    "issuer": "https://idp.example.com/realms/main",
    "jwks_uri": "https://idp.example.com/realms/main/protocol/openid-connect/certs",
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
      "pool_timeout_seconds": 10
    },
    "legal": {
      "url": "https://alien.example.com/mcp/legal",
      "connect_timeout_seconds": 10,
      "read_timeout_seconds": 130,
      "write_timeout_seconds": 30,
      "pool_timeout_seconds": 10
    }
  },
  "limits": {
    "max_request_body_bytes": 1048576,
    "request_body_timeout_seconds": 30,
    "max_sse_event_bytes": 65536
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
  "policies": {"allowed_origins": ["https://app.example.com"]}
}
```

With tracing and metrics off, the block is `"observability": {"otlp": null}`.
