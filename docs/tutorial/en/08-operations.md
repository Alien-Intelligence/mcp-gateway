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

## 8.7 Suspension and end of contract

This section is for a **sponsor operator**, for example a publisher that sells access to its clients through the groups of chapter 3. Everything below was run against a live deployment of the gateway in front of a real Alien platform; each answer is what that deployment returned. The calls use the conventions of [chapter 3](03-alien-side.md#conventions-used-in-the-examples) (`sponsor.hdr`, `$ALIEN_API`, `$SPONSOR_ORG`) and the ids `GROUP_ID`, `ORG_ID`, `USER_ID` and `TOKEN_IDENTIFIER` of its steps.

### Who cuts what

| Lever | Where | What it stops |
|---|---|---|
| Suspension of the sponsored group | Alien (`/groups/{id}/suspension`) | Every sponsored read of the organizations attached to the group. Alien checks it on **every request**: nothing is billed |
| Suspension of one client-managed user | Alien (`/organizations/{org}/users/{user}`) | The reads made with that user's keys. Alien checks it on every request |
| Revocation of a key | Alien (`/tokens/{identifier}`) | One key. The gateway answers `-31001` until the resolver gives it another |
| The customer's single sign-on | The customer's identity provider | New sign-ins of its people. The gateway does not look at it again once a request is authenticated |
| The gateway | none of the above | The gateway holds no suspension state of its own |

The gateway **does not need its cache invalidated for a suspension**: a suspended caller's request still goes upstream, Alien refuses it, and the gateway relays that refusal unchanged. Only a permanent user suspension and a revoked key make Alien answer `401`; the gateway then drops the cached credential by itself (chapter 8.1).

A suspension is per group. If one client must be suspended without the others, that client needs its own sponsored group.

### 8.7.1 Suspend a client temporarily

Suspend the group's sponsored access, with the message the client's people will read:

<!-- not-config -->
```bash
curl -sS --max-time 30 -H @sponsor.hdr -H "x-organization-id: $SPONSOR_ORG" \
  -H 'Content-Type: application/json' \
  -d '{"kind": "temporary", "message": "Access is paused while we settle your account. Contact support@example-publisher.test."}' \
  "$ALIEN_API/groups/$GROUP_ID/suspension"
```

Expected: `200`, and the group with `suspensionKind` `temporary`, `suspendedAt` and your `suspensionMessage`:

<!-- not-config -->
```json
{"success": true, "data": {"id": 454, "isSponsored": true, "suspendedAt": "2026-11-24T12:06:41.755+01:00", "suspensionKind": "temporary", "suspensionMessage": "Access is paused while we settle your account. Contact support@example-publisher.test.", "...": "..."}}
```

**What the AI client sees.** The next `tools/call` through the gateway gets HTTP `200`, not an HTTP error, and a tool result flagged `isError` whose text is your message inside the tool server's error envelope. The gateway relays it unchanged (audit outcome `forwarded`):

<!-- not-config -->
```json
{"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": "Error calling tool 'datacluster_get_dataset': {\n  \"success\": false,\n  \"error\": \"Access is paused while we settle your account. Contact support@example-publisher.test.\"\n}"}], "isError": true}}
```

`tools/list` and the other methods are not affected. The AI client shows the text to the person, which is why the message should say who to contact. **Nothing is billed** for a refused call, and it appears in neither the client's nor the sponsor's consumption feed.

Lift it:

<!-- not-config -->
```bash
curl -sS --max-time 30 -X DELETE -H @sponsor.hdr -H "x-organization-id: $SPONSOR_ORG" \
  "$ALIEN_API/groups/$GROUP_ID/suspension"
```

The next call is served again. No change is needed on the gateway.

### 8.7.2 Suspend a client permanently

The request is the same with `"kind": "permanent"`:

<!-- not-config -->
```bash
curl -sS --max-time 30 -H @sponsor.hdr -H "x-organization-id: $SPONSOR_ORG" \
  -H 'Content-Type: application/json' \
  -d '{"kind": "permanent", "message": "Your contract has ended. Contact support@example-publisher.test."}' \
  "$ALIEN_API/groups/$GROUP_ID/suspension"
```

The AI client sees exactly what it sees for a temporary suspension, with the permanent message. A permanent suspension is **final**: lifting it, or switching it back to temporary, answers `409`:

<!-- not-config -->
```json
{"success": false, "message": "A permanent suspension cannot be lifted.", "error": {"status": 409, "name": "GROUP_SUSPENSION_PERMANENT", "message": "This group's sponsored access is permanently suspended.", "data": null, "origin": "client"}}
```

Treat it as irreversible. Use it for a client that is leaving, not to pause one.

### 8.7.3 Suspend or reactivate one user

A client-managed user is suspended through the user's route. Always send `kind`: left out, it defaults to a permanent suspension. `message` is what the person reads, and `reason` is kept for your audit trail only.

**Temporary:**

<!-- not-config -->
```bash
curl -sS --max-time 30 -X DELETE -H @sponsor.hdr -H 'Content-Type: application/json' \
  -d '{"kind": "temporary", "reason": "invoice 2026-11 unpaid", "message": "Your access is paused. Contact support@example-publisher.test."}' \
  "$ALIEN_API/organizations/$ORG_ID/users/$USER_ID"
```

Expected: `200` and the user with `isLocked` `true`, `suspensionKind` `temporary`:

<!-- not-config -->
```json
{"success": true, "data": {"id": 1016, "userType": "client-managed", "isLocked": true, "suspensionKind": "temporary", "lockMessage": "Your access is paused. Contact support@example-publisher.test.", "...": "..."}}
```

**What the AI client sees.** HTTP `200` and a tool error whose text is your message **alone**, without an envelope. `tools/list` still works. The gateway forwards the request and keeps its cached credential:

<!-- not-config -->
```json
{"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": "Your access is paused. Contact support@example-publisher.test."}], "isError": true}}
```

Refused calls are not billed and not listed. Reactivate with:

<!-- not-config -->
```bash
curl -sS --max-time 30 -X PATCH -H @sponsor.hdr \
  "$ALIEN_API/organizations/$ORG_ID/users/$USER_ID"
```

The user's key then works again at once: nothing was revoked, so the resolver and the gateway are untouched.

**Permanent** (same request with `"kind": "permanent"`):

<!-- not-config -->
```bash
curl -sS --max-time 30 -X DELETE -H @sponsor.hdr -H 'Content-Type: application/json' \
  -d '{"kind": "permanent", "reason": "contract ended", "message": "Your access has ended."}' \
  "$ALIEN_API/organizations/$ORG_ID/users/$USER_ID"
curl -sS --max-time 30 -H @sponsor.hdr "$ALIEN_API/organizations/$ORG_ID/users/$USER_ID/tokens"
```

The user's keys are **deleted**: the token list is now empty.

<!-- not-config -->
```json
{"success": true, "data": []}
```

The gateway still holds the old key in its resolver, Alien answers `401` to it, and the AI client gets HTTP `200` with the gateway's `-31001`. The gateway drops its cached credential on that first refusal:

<!-- not-config -->
```json
{"jsonrpc": "2.0", "id": 1, "error": {"code": -31001, "message": "The credential configured for this account was rejected by the service. Contact your administrator.", "data": {"upstream_status": 401}}}
```

The same `PATCH` as above reactivates the user, but the deleted keys do not come back: mint a new one (8.7.4) and update the resolver.

### 8.7.4 Revoke one key and replace it

Revoke the key by its identifier, then mint a new one for the same user. Keep the new `data.token` and `data.identifier` as in [step 3.6](03-alien-side.md#36-mint-the-api-key):

<!-- not-config -->
```bash
curl -sS --max-time 30 -X DELETE -H @sponsor.hdr \
  "$ALIEN_API/organizations/$ORG_ID/users/$USER_ID/tokens/$TOKEN_IDENTIFIER"
curl -sS --max-time 30 -H @sponsor.hdr -H 'Content-Type: application/json' \
  -d '{"name": "mcp-gateway", "scope": "api-key"}' \
  "$ALIEN_API/organizations/$ORG_ID/users/$USER_ID/tokens"
```

Put the new key where your resolver reads it: the key file of a `static` entry, or what your webhook returns. **The gateway is not restarted.** On the live deployment the next call with the new key worked about 5 seconds after the file was updated; once, the mounted Secret took 55 seconds to reach the pods (the kubelet's own sync), and every call in that window got `-31001`. A `resolver_cache` adds its TTL (8.1). To avoid the gap altogether, rotate in the order of 8.1: mint, update, wait, revoke.

**What the AI client sees** between the revocation and the update is the `-31001` shown above, HTTP `200`.

### 8.7.5 Monthly quota

A sponsor sets a monthly cap, in euros, on a group, with the message shown once it is reached:

<!-- not-config -->
```bash
curl -sS --max-time 30 -X PATCH -H @sponsor.hdr -H "x-organization-id: $SPONSOR_ORG" \
  -H 'Content-Type: application/json' \
  -d '{"monthlyQuota": 100, "quotaMessage": "The monthly allowance is used up. Contact support@example-publisher.test."}' \
  "$ALIEN_API/groups/$GROUP_ID"
```

Read the group (`GET`, below) to follow the month: `currentMonth` carries `amount`, `currency` and `percentage` of the quota.

<!-- not-config -->
```bash
curl -sS --max-time 30 -H @sponsor.hdr -H "x-organization-id: $SPONSOR_ORG" "$ALIEN_API/groups/$GROUP_ID"
```

- **Up to about 80 %: nothing.** On the live deployment, calls at 67 % and 75 % carried no notice.
- **From 80 %: a notice.** The read that brings the month's spend to 80 % or more already carries it. The first one measured came at **83 %**, then 91 % and 100 %. It rides in the result the AI client receives, as a `notices` list in the JSON (or a final `**Notice:**` block, depending on the tool), and the tool server asks the model to relay it to the person:

<!-- not-config -->
```json
{"success": true, "data": {"...": "..."}, "notices": ["<client organization>'s <sponsor organization> access has used 83 % of its monthly quota. Contact <sponsor organization> to raise it."]}
```

- **At 100 %: a refusal.** The next calls get HTTP `200`, `isError` `true`, with your `quotaMessage` in the same envelope as a group suspension:

<!-- not-config -->
```json
{"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": "Error calling tool 'datacluster_get_dataset': {\n  \"success\": false,\n  \"error\": \"The monthly allowance is used up. Contact support@example-publisher.test.\"\n}"}], "isError": true}}
```

**Refusals are not counted.** The spend stayed at its cap, nothing was billed to the client, and the refused calls are in no feed. To lift the cap, `PATCH` the group with a higher `monthlyQuota` or `null`.

### 8.7.6 Closing a client at the end of the contract

**Not yet available, pending platform fixes.** There is no supported way yet to close a client's account from the platform side, and this tutorial does not document one.

What to do today, to stop all access and all spending of a client:

1. **Suspend its sponsored group permanently** (8.7.2). This is final.
2. **Suspend each of its users permanently** (8.7.3). Their keys are deleted.
3. **Revoke any key you still hold** (8.7.4) and remove the client's entries from your resolver, so that no one is mapped to a credential any more.
4. **Ask the customer to disable the sign-in** of its people in its own identity provider, which stops new logins.

Keep the organizations and their consumption history: they are your billing record.

Continue with [9. Troubleshooting](09-troubleshooting.md).
