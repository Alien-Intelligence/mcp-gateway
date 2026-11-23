# 8. Operations

## 8.1 Rotating an Alien key

Alien keys are minted and revoked through the API (chapter 3). The gateway holds a key only as long as its resolver and its cache do, so rotate in this order:

1. **Mint** a new key for the same user (`POST /organizations/{org}/users/{user}/tokens`). The old one keeps working: a user can hold several.
2. **Update the source** the resolver reads: the key file of a `static` entry, the row of a `store_sql` table, the value of a `store_redis` key, or what your webhook returns. A `static` key file is read on every request, so nothing needs restarting.
3. **Wait for the cache.** With a `resolver_cache`, the gateway keeps answering with the old key until the entry expires: at most `positive_ttl_seconds` (a webhook's `ttl_seconds` can shorten that). With no cache the new key is used at once. Replicas each have their own entries (or share them in Redis): wait for the full TTL.
4. **Revoke** the old key (`DELETE /organizations/{org}/users/{user}/tokens/{identifier}`).

If you revoke before the cache expires, the next request through a replica that still holds the old key is refused by Alien: the client receives a JSON-RPC error `-31001` (HTTP `200` for a request that expects a reply), and the gateway **drops that cache entry** so the following request resolves the new key. One failed request per cached user is the worst case; waiting avoids it. Alien answering `402` has the same handling (`-31002`).

The same procedure rotates a compromised key, minus the waiting: revoke, accept the `-31001` that the user sees until the entry is dropped, and update the source.

## 8.2 Rotating secrets of the gateway

### The state encryption key (`state.encryption_key_file`)

Only relevant with `state.type` `redis`. One file holds the base64 of 32 random bytes; the key names in Redis and the encryption of cached credentials both derive from it, so changing the key changes the names the gateway looks under. Exactly what happens, from the state code and the configuration reference:

| What | Effect of a new key |
|---|---|
| Cached credentials (`resolver_cache`) | The new generation looks under different names and finds nothing: its cache starts **empty** (the credential backend is asked once per identity). The old entries expire by their TTL. A value that carries another key id is counted as a miss and left alone |
| Session bindings | A session that was opened through a replica on the old key is unknown to a replica on the new key: the client gets `404`, JSON-RPC `-31007` (audit outcome `session_not_found`) and starts a new session. The sessions at the Alien upstream are not touched; the client opens new ones |
| Rate-limit buckets | Keyed the same way, so buckets **restart full** |
| Requests in flight and everything else | No replica fails requests because of the other generation: there is no outage |

During a rolling update the two generations simply do not see each other's entries. Rotate in a quiet window, or accept those effects. With `state.type` `memory` the file is not used. Every replica sharing a Redis must end up on the same file.

To rotate: write the new file into the Secret that is mounted, then `kubectl rollout restart` (mounted Secrets are not watched). With the compose example, replace `secrets/gateway/state-encryption-key` and run `docker compose restart gateway`.

### The webhook's HMAC secret and client certificate

The gateway reads the `secret_file` of a `webhook` resolver's `auth` once, when it starts, and signs with that one secret: **there is no way to give it two**. A new secret therefore needs a restart of the gateway, and the webhook must accept both secrets for the length of the roll:

1. Make your webhook accept the old and the new secret (the example [`webhook_resolver.py`](../examples/webhook_resolver.py) accepts one: extend `signature_is_valid` to try both).
2. Put the new secret in the gateway's secret file and roll the pods.
3. When every pod runs the new one, remove the old secret from the webhook.

An `mtls` client certificate and key are loaded when the resolver is built, so they follow the same rule: a roll is needed.

### Other files

The `static` resolver re-reads its mapping file (when its modification time or size changes) and its key files (on every request). For the other `*_file` keys (database DSN, Redis URLs, OTLP headers), plan on a roll of the pods after a change; this tutorial did not test live reloading of them.

## 8.3 Observability

### The audit line

With `audit.enabled`, every request writes one JSON line to stdout, whatever way it ends. It carries identifiers, the method and the tool name, the outcome and the latency, never content, arguments, tokens, keys or session ids.

<!-- not-config -->
```json
{"ts":"2026-11-20T09:14:03.512+00:00","event":"mcp_request","request_id":"6f1c2b0e-5a47-4d8e-9d52-3b0a4c9e7f10","trace_id":"4bf92f3577b34da6a3ce929d0e0e4736","subject":"user-1","organization":"example-org","labels":{"client":"example"},"http_method":"POST","rpc_method":"tools/call","rpc_id":7,"tool":"search","status":200,"outcome":"forwarded","reason":null,"upstream":"main","latency_ms":182.4,"upstream_status":200,"upstream_request_id":"req-81c4","upstream_session":false}
```

Fields: `ts`, `event` (always `mcp_request`), `request_id`, `trace_id`, `subject`, `organization`, `labels`, `http_method`, `rpc_method`, `rpc_id`, `tool`, `status`, `outcome`, `reason`, `upstream`, `latency_ms`, `upstream_status`, `upstream_request_id`, `upstream_session`. Text values are clipped to 256 characters.

`request_id` is also returned to the client as the `x-request-id` header of every response the gateway produces or relays: when a user reports a problem, ask for that header and search the audit line. A client-sent `x-request-id` is ignored.

The `outcome` is one of: `origin_refused`, `unauthenticated`, `insufficient_scope`, `idp_unavailable`, `userinfo_unavailable`, `request_body_timeout`, `body_too_large`, `mcp_header_mismatch`, `duplicate_header`, `resolver_unavailable`, `state_unavailable`, `gateway_error`, `denied`, `rate_limited`, `session_not_found`, `upstream_error`, `upstream_auth_rejected`, `upstream_stream_failed`, `tool_denied`, `batch_refused`, `unparsable_request`, `invalid_request`, `tool_policy_failed`, `forwarded`, `client_disconnected`, `internal_error`. `reason` holds the machine reason where there is one: your resolver's denial reason, the failure class of a broken stream, the bucket (`subject` or `organization`) that refused a rate-limited request, or, with `auth.userinfo`, `userinfo_rejected`, `userinfo_subject_mismatch`, `userinfo_claims` or `userinfo_invalid`.

The logs of the process itself (startup, resolver and JWKS errors) are JSON lines on stdout as well, with `ts`, `level`, `logger` and `message`.

### Traces and metrics

`observability.otlp` exports traces and metrics over OTLP. `{"otlp": null}` exports nothing. Each request is a new root trace with three spans, and a fourth with `auth.userinfo`:

| Span | What |
|---|---|
| `mcp-gateway request` | The whole request, until the response ends |
| `mcp-gateway resolve_credential` | The resolver step; attributes `mcp_gateway.resolver.type`, `mcp_gateway.resolver.cache` (`hit`, `miss`, `negative`, `none`) and `mcp_gateway.resolver.result` |
| `mcp-gateway userinfo` | With `auth.userinfo` only: the userinfo lookup; attributes `mcp_gateway.userinfo.cache` (`hit`, `miss`, `none`) and `mcp_gateway.userinfo.result` (`profile`, `rejected`, `subject_mismatch`, `claims_invalid`, `unavailable`, `invalid`, `state_unavailable`, `disconnected`, `aborted`) |
| `mcp-gateway upstream` | The call to Alien, until the response headers |

Metrics:

| Instrument | Type | Use |
|---|---|---|
| `mcp_gateway.requests` | counter | Requests by `outcome`, `status_class`, `rpc_method`, `upstream`. Alert on a rising share of outcomes other than `forwarded` |
| `mcp_gateway.request.duration` | histogram, seconds | Request latency, same attributes |
| `mcp_gateway.upstream.duration` | histogram, seconds | Time to the response headers from Alien |
| `mcp_gateway.resolver.lookups` | counter | Resolver calls by `resolver_type`, `cache`, `result`. The cache hit rate and the failure rate of your webhook |
| `mcp_gateway.userinfo.lookups` | counter | With `auth.userinfo` only: lookups by `cache` and `result`. The cache hit rate, and a rising `unavailable` or `invalid` when the identity provider fails |
| `mcp_gateway.upstream.detached` | counter | Requests that ran on after their client left, by `result` (section 8.5) |
| `mcp_gateway.upstream.detached.active` | gauge | Those running now |
| `mcp_gateway.sessions.bound` | gauge | Sessions in this process (with `state.type` `memory` only) |
| `mcp_gateway.telemetry.failures` | counter | Failures of telemetry itself, by `signal` (`record`, `traces`, `metrics`). **Alert on a rising value**: a collector that is down is otherwise visible only in the logs |

Spans and metrics never carry the subject, the e-mail, a token, a key, a session id, arguments or content. Resolver labels are span attributes only (`mcp_gateway.label.<key>`); tool names are span attributes only, never metric attributes. Telemetry is best effort: a recording or export failure is counted and never changes a response. Trace context is not propagated to Alien, and an inbound `traceparent` becomes a span link, never the parent.

### Probes

`GET /healthz` (liveness, never looks at Redis) and `GET /readyz` (readiness; with `state.type` `redis` it pings Redis and answers `503` `{"status":"state_unavailable"}` when it cannot).

## 8.4 Rate limits and tool policies

### Rate limits

Token buckets per subject and, optionally, per organization. Every authenticated request takes one token from each bucket that applies; a request without a valid token takes none. The check runs before the credential lookup, so a flood never reaches your resolver or Alien.

<!-- config-fragment: policies.rate_limits -->
```json
{
  "per_subject": {"burst": 30, "per_minute": 60},
  "per_organization": {"burst": 200, "per_minute": 600}
}
```

`burst` is the bucket's size (and its level when created); `per_minute` is the refill rate. With `{"burst": 30, "per_minute": 60}` a new subject can send 30 requests at once, then one per second. `per_subject` is required when the feature is on (`"rate_limits": null` turns it off). `per_organization` needs `auth.identity.organization`.

A refused request is answered in the form its sender can read, always with `Retry-After`: a `tools/call` gets HTTP `200` and a result with `isError: true` and `_meta["alien.club/retryAfterSeconds"]`; another request with an id gets HTTP `200` and `-31014`; a request that cannot be answered (a notification, `GET`, `DELETE`) gets HTTP `429`. The audit outcome is `rate_limited`, `reason` `subject` or `organization`. With `auth.userinfo` the organization is known only after the userinfo call, so the subject bucket is taken first and the organization bucket after: a request refused for `organization` has already spent its subject token.

With `state.type` `memory` each replica keeps its own buckets: behind a load balancer the effective limit is the configured one times the number of replicas that see the subject. With `redis` all replicas share one set of buckets.

### Tool policies

<!-- config-fragment: policies.tools -->
```json
{
  "default": {"allow": ["search", "get_*"], "deny": []},
  "rules": [
    {"match": {"organization": "example-org"}, "allow": null, "deny": ["delete_*"]},
    {"match": {"label": {"name": "tier", "value": "basic"}}, "allow": ["search"], "deny": []}
  ]
}
```

The first rule that matches a caller is that caller's policy (`default` is not merged into it). `allow` is `null` (every tool) or a list of names with `*` as the only wildcard; `deny` always wins. `match` is either the token's organization or a label returned by your resolver. A denied `tools/call` is answered exactly like a tool that does not exist (`-32602`, `Unknown tool: <name>`) and is never forwarded; `tools/list` replies are filtered. While a policy is active the gateway parses requests and the replies it must filter, and refuses what it cannot check (unreadable bodies, batches that contain `tools/call` or `tools/list`). With `"tools": null` it never parses a body and relays bytes exactly as sent. The full list of cases is in [`configuration.md`](../../configuration.md#policiestools).

## 8.5 A client disconnect is not a cancellation

When a client disconnects after the gateway has sent its request to Alien, the gateway **keeps reading the upstream response** and discards it, then closes it: the work finishes, Alien meters it, and the connection returns to its pool cleanly. This departs on purpose from the 2026-07-28 MCP rule that closing the stream cancels the request. Only an explicit `notifications/cancelled` from the client cancels. Listen streams (`GET`), `DELETE` and notifications are closed at once on disconnect.

The audit line of such a request is written when the client leaves: status `499`, outcome `client_disconnected`.

**The ceiling.** The part that runs on is bounded by the upstream's `read_timeout_seconds` as a wall-clock total, counted from the moment the client left; after that the response is closed and counted as `timeout`. In addition, `max_detached_reads` (below `max_connections`) bounds how many such requests one upstream may have at once: beyond it a leaving client's request is cancelled and counted `shed`, with a WARNING log, so that clients who left cannot starve those still there. Size `max_connections` for the concurrent requests you expect plus `max_detached_reads`.

Operationally: Alien may bill work that no client waited for, `mcp_gateway.upstream.detached` shows how often it happens, and the pod's grace period must exceed the stop budget, which includes the largest `read_timeout_seconds` (section 8.6; the chart enforces it).

## 8.6 Upgrades

- **Versions.** The image and the chart carry the same version (`Chart.yaml` `version` and `appVersion` equal the image version). Pin a version or a digest; never `latest`.
- **Configuration.** The configuration document carries `"version": 1`, the only schema version this gateway reads. A new schema version will be a new number, never a silent change of meaning under the same one. The JSON Schema of version 1 is [`config.schema.v1.json`](../../config.schema.v1.json). Validate a document before rolling it: `helm template` runs the chart's checks, and the gateway refuses an invalid one at start, in the pod log (`startup refused: ...`).
- **The wire contract** ([`protocol.md`](../../protocol.md)): error codes, `_meta` keys, statuses, challenge shapes and headers change only with a major version of the gateway. A new code may be added in a minor version; an existing code never changes meaning or number. Match on the code, never on the message text, which may be reworded in any release.
- **Rolling upgrade with the chart.** `helm upgrade` rolls the Deployment with `maxSurge: 1` and `maxUnavailable: 0` (a new pod is ready, on `/readyz`, before an old one stops), the pod keeps serving for `preStopSleepSeconds` after it is marked terminating, and the grace period lets requests in flight finish and the gateway run its own shutdown (next item). Listen streams are closed at once when a pod stops; the client reconnects to another replica, which finds its session in Redis.
- **What a stop does.** On `SIGTERM` the gateway stops accepting connections and ends its listen streams cleanly (no error event; the audit line says outcome `forwarded`, reason `shutdown`). Requests in flight, a `tools/call` for example, get the largest `read_timeout_seconds` to finish and relay their reply. Then the application shuts down: detached reads get 10 s to finish and 10 s to close, and the telemetry is flushed (with OTLP, up to `timeout_seconds` + 2 s for each of traces and metrics). The sum is the stop budget, 150 s with one upstream at `read_timeout_seconds` 130 and no OTLP; the gateway logs it at start. The chart sets `terminationGracePeriodSeconds` from it (160 s there, with the preStop sleep and a margin) and refuses a smaller value; with Docker Compose, keep `stop_grace_period` above it. A grace period below the budget means the process is killed before the flush: no telemetry, and the detached reads are not counted. Details: [`configuration.md`](../../configuration.md#shutdown).
- **Mixed versions on one Redis: not verified.** During a rolling upgrade, two versions of the gateway run against the same Redis. The state layout is documented (keys, their expiry, the value formats), but this project has **not** tested two different gateway versions against one Redis, so it cannot promise they are compatible. Until a release states that the state layout did not change, treat an upgrade that crosses versions as one that may reset sessions and cached credentials. If you need to be sure, give the new version its own `state.key_prefix` (a new keyspace: sessions and cache start empty, buckets start full, nothing is misread), and remove the old keys by their expiry.
- **Rollback.** `helm rollback` returns to the previous revision. The same caveat applies to the state shared with the version you leave.

Continue with [9. Troubleshooting](09-troubleshooting.md).
