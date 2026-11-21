# Behaviour overview

A summary of what the gateway does and how each part is configured, with pointers to the full reference in [configuration.md](configuration.md) and the wire contract in [protocol.md](protocol.md).

## Configuration

The wire contract (JSON-RPC error codes, `_meta` keys, HTTP statuses, challenges, headers) is in [`docs/protocol.md`](protocol.md); it is stable within a major version.

One strict JSON document in the `MCP_GATEWAY_CONFIG` environment variable. Every key is required, unknown keys are refused, and there is no default for any issuer, audience or secret: the gateway refuses to start on an incomplete configuration. The full reference, key by key with bounds and a complete example, is [`docs/configuration.md`](configuration.md). The gateway services in [`compose.yaml`](../compose.yaml) are working examples.

Top-level keys: `version` (always `1`), `public_url`, `listen`, `auth` (issuer, JWKS, audiences, scopes, claim paths), `resolver`, `resolver_cache`, `upstreams`, `limits`, `audit`, `observability`, `sessions`, `state` and `policies` (allowed origins, tool policy, rate limits).

- `sessions`: an upstream `Mcp-Session-Id` is bound to the subject it was issued to (in memory, or in Redis with `state.type` `redis`, which several replicas need). A request that presents an unknown, expired or foreign session id gets `404` and a JSON-RPC error (`-31007`) and is not forwarded; the client starts a new session.
- `state`: `memory` (one replica) or `redis` (shared by every replica: sessions, the credential cache and the rate limit buckets). With Redis the gateway fails closed (`503`, `-31013`) when it cannot reach it, and `GET /readyz` reports readiness. See [`docs/configuration.md`](configuration.md#state).
- `policies.allowed_origins`: browser origins allowed on the MCP endpoint. An `Origin` header that is not listed gets `403` (`-31008`) before authentication; no `Origin` header passes.
- `policies.tools`: per-caller tool allow and deny lists (exact names or `*`, matched on organization or resolver label). A denied `tools/call` is answered as an unknown tool and never forwarded, and `tools/list` replies are filtered. See [`docs/configuration.md`](configuration.md#policiestools).
- `policies.rate_limits`: token buckets per subject and per organization, in memory or shared in Redis. A refused request is answered in the form its sender can read (`isError` tool result, `-31014`, or `429`) with `Retry-After`. See [`docs/configuration.md`](configuration.md#policiesrate_limits).
- A client disconnect is not a cancellation: an upstream request already sent keeps being read to its end (and discarded), bounded by the upstream's `read_timeout_seconds` and by `max_detached_reads` per upstream (beyond it the request is cancelled, counted `shed`); only an explicit `notifications/cancelled` cancels. Listen streams, `DELETE` and notifications are closed on disconnect. See [`docs/configuration.md`](configuration.md#client-disconnects).
- Requests whose `Mcp-Method` or `Mcp-Name` header disagrees with the JSON-RPC body get `400` and `-32020` (MCP 2026-07-28). If the upstream stream fails before the reply to a request was relayed, the client receives one final SSE event with a JSON-RPC error (`-31006`); in every other mid-stream failure the connection is aborted so it cannot pass for a clean end.

### Resolvers

The resolver turns the verified identity (subject, verified e-mail, organization, groups) into an Alien API key, an upstream name and labels, or a denial (`403`). Five kinds, selected by `resolver.type`:

| Type | Source of the key | Use it when |
|---|---|---|
| `static` | A mapping file plus one key file per entry, re-read on each request. | A few users or organizations, keys managed as files or mounted secrets. |
| `webhook` | An HTTPS endpoint you run, called with the identity and authenticated by an HMAC signature or mutual TLS. Bounded retries with backoff, fail closed (`503`, `Retry-After`). | You already own the mapping and want it decided on your side. |
| `store_sql` | One read-only `SELECT` against a PostgreSQL table you fill in. | The mapping lives in a database. |
| `store_redis` | One key lookup in Redis, under a pattern you choose. | Many users and cheap lookups. |
| `chain` | Several of the above in order: `no_mapping` moves to the next, any other denial or error stops the chain. | Different populations in different stores. |

Any of them can sit behind the optional `resolver_cache` (positive and negative TTL, single flight, dropped on an upstream `401`/`402`). Secrets (key files, DSN, Redis URL, HMAC secret, client certificates) are always referenced by file path, never inlined. See [`docs/configuration.md`](configuration.md#resolver).

On Alien, an API key is a billed user and an upstream session: users that share a key share a line in the consumption feed. Map one key per user when you need per-user accounting.

### Observability

`observability.otlp` exports traces and metrics over OTLP (`grpc` or `http/protobuf`); `{"otlp": null}` exports nothing. Each request is one root trace with three spans (request, credential resolution, upstream call) and the metrics `mcp_gateway.requests`, `mcp_gateway.request.duration`, `mcp_gateway.upstream.duration`, `mcp_gateway.resolver.lookups`, `mcp_gateway.upstream.detached`, `mcp_gateway.upstream.detached.active`, `mcp_gateway.sessions.bound` and `mcp_gateway.telemetry.failures` (telemetry is best effort: a recording or export failure is counted, never changes a response). Spans and metrics never carry the subject, e-mail, a token, a key, a session id, arguments or content; resolver labels are span attributes only. Details: [`docs/configuration.md`](configuration.md#observability).

### Request id and audit line

Every response carries an `x-request-id` the gateway generated (a client-sent one is ignored and never forwarded upstream). With `audit.enabled`, each request writes one JSON line to stdout, whatever way it ends:

```json
{"ts":"2026-11-20T09:14:03.512+00:00","event":"mcp_request","request_id":"6f1c2b0e-5a47-4d8e-9d52-3b0a4c9e7f10","trace_id":"4bf92f3577b34da6a3ce929d0e0e4736","subject":"user-1","organization":"example-org","labels":{"client":"example"},"http_method":"POST","rpc_method":"tools/call","rpc_id":7,"tool":"search","status":200,"outcome":"forwarded","reason":null,"upstream":"main","latency_ms":182.4,"upstream_status":200,"upstream_request_id":"req-81c4","upstream_session":false}
```

The line holds identifiers, the method and tool name, the outcome and the latency; never content, arguments, tokens, keys or session ids. `request_id` joins the line to the trace (`trace_id`) and to the client's response header. Field list and outcomes: [`docs/configuration.md`](configuration.md#audit-line).

