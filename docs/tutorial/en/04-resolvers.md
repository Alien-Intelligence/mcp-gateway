# 4. Choosing a resolver

The resolver turns the identity the gateway verified into an Alien API key. It is the one part of the gateway that you adapt to your own systems. The gateway hands every resolver the same identity:

| Field | Content |
|---|---|
| `subject` | The stable user id from the token |
| `email` | The e-mail, **only** when the token's `email_verified` claim is `true`, otherwise `null` |
| `organization` | The organization claim, or `null` |
| `groups` | The groups claim, a list (possibly empty). The Keycloak realm template of chapter 6 puts full group paths in it (`/analysts`, `/team-a/analysts`) |

The resolver answers with a key, the name of an upstream (one of the keys of `upstreams`) and labels, or with a denial. Labels are strings copied to the audit line and to trace attributes, and used by tool policies (chapter 8).

## The kinds

| `resolver.type` | Source of the key | Use it when |
|---|---|---|
| `static` | A mapping file plus one key file per entry. The key files are read on every request; the mapping is reloaded when the file's modification time or size changes | A few users or organizations, with keys kept as files or mounted secrets |
| `webhook` | An HTTPS endpoint you run, called with the identity and authenticated by an HMAC signature or mutual TLS | You already own the mapping (a customer database, a CRM) and want to decide on your side |
| `store_sql` | One read-only `SELECT` against a PostgreSQL table you fill in | The mapping lives in PostgreSQL |
| `store_redis` | One key lookup in Redis, under a pattern you choose | Many users and cheap lookups |
| `chain` | Several of the above, in order | Different populations in different places |

Any of them can sit behind the optional `resolver_cache`. Every secret a resolver needs (key files, DSN, Redis URL, HMAC secret, client certificates) is a path to a file, never an inline value.

## One key per user, or shared keys

On Alien an API key is a billed user and an upstream session. The consequences:

- **One key per user** (recommended): each person has their own line in the consumption feed, their own quota and their own upstream session. Revoking one key affects one person.
- **A shared key** (for example one key per organization, with a `static` entry that matches `organization`, or a `webhook` that returns the same key for everyone in an organization): all users of that key share **one billed user and one upstream session**. Their consumption is merged in a single line of the feed, and you cannot tell them apart or limit them separately on the Alien side. The gateway can still tell them apart for its own audit line and its rate limits, because those use the verified subject.

The design notes of the project say it plainly: the upstream session id is derived from the key, so users that share a key share caches and a line in the feed. Choose per-user keys whenever you need per-user accounting. Chapter 3 shows how to create them.

## `static`

The smallest setup. Two files and a configuration fragment.

<!-- config-fragment: resolver -->
```json
{"type": "static", "mapping_file": "/run/gateway/secrets/mapping.json"}
```

The mapping file:

<!-- static-mapping -->
```json
{"entries": [
  {"match": {"email": "alice@example.com"}, "api_key_file": "alice.key", "upstream": "main", "labels": {"client": "example-publisher"}},
  {"match": {"subject": "3f1c0e52-0f34-4b8e-9a52-1d6b5d7c9a10"}, "api_key_file": "bob.key", "upstream": "main", "labels": {"client": "example-publisher"}}
]}
```

- `match` has exactly one of `subject`, `email`, `organization`, `group`, with a non-empty string. E-mail matching ignores case.
- `api_key_file` is relative to the directory of the mapping file, or absolute. Each file holds one key (`oat_...`).
- `upstream` must be a key of `upstreams`. `labels` is required; use `{}` for none.
- Matching order is subject, then e-mail, then organization, then group. Two entries with the same matcher are refused when the file is loaded. Two **different** group entries that both match one user are `ambiguous_mapping`; no match at all is `no_mapping`.
- An `email` matcher needs `auth.identity.email_verified` to be set (not `null`): the gateway refuses to start otherwise.
- A broken or missing mapping file stops the gateway at start. A later edit that breaks the file makes every request a `500` until the file is fixed. A rotated key file takes effect without a restart.

## `webhook`

The gateway sends the identity to your endpoint and gets a decision.

<!-- config-fragment: resolver -->
```json
{
  "type": "webhook",
  "url": "https://keys.example.com/resolve",
  "auth": {"type": "hmac", "secret_file": "/run/gateway/secrets/webhook-hmac"},
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
```

### The request

`POST <url>` with `Content-Type: application/json` and a compact body that carries only what the gateway verified, never the token or its other claims:

<!-- not-config -->
```json
{"subject":"3f1c0e52-0f34-4b8e-9a52-1d6b5d7c9a10","email":"alice@example.com","organization":"example-publisher","groups":["/analysts"]}
```

`email` and `organization` are `null` when absent. `groups` is always a list.

### Authentication of the gateway

Either of:

- **`hmac`**: three headers.
  - `X-MCP-Gateway-Timestamp`: Unix seconds.
  - `X-MCP-Gateway-Nonce`: 32 random hexadecimal characters, new for every attempt (a retry has a new nonce).
  - `X-MCP-Gateway-Signature`: `sha256=<hex>`, where the digest is `HMAC-SHA256(secret, "<timestamp>.<nonce>.<body>")` over the exact body bytes. The secret is the content of `secret_file`, with surrounding whitespace removed.

  Your endpoint must compare the signature in constant time, reject a timestamp outside a short window, and reject a nonce it has already accepted inside that window (remember each accepted nonce until its timestamp leaves the window, with bounded memory).
- **`mtls`**: `{"type": "mtls", "client_cert_file": "...", "client_key_file": "..."}`. The gateway presents the client certificate and your endpoint verifies it. It needs an `https` URL. `ca_file` is the bundle that verifies **your** server's certificate, for both modes; `null` uses the system's certificate authorities.

### The answer

HTTP `200` with a JSON body, one of:

<!-- webhook-answer -->
```json
{"decision":"allow","api_key":"oat_example-key","upstream":"main","labels":{"client":"example-publisher"},"ttl_seconds":300}
```

<!-- webhook-answer -->
```json
{"decision":"deny","reason":"no_mapping"}
```

- `allow` has **exactly** these keys: `api_key` (a string of printable ASCII without whitespace), `upstream` (a name from `upstreams`, or `null` to use `default_upstream`), `labels` (an object of strings, possibly empty) and `ttl_seconds` (a number above zero, or `null`). The resolver cache keeps the answer for `ttl_seconds`, capped by `max_ttl_seconds` and by `resolver_cache.positive_ttl_seconds`: the value can only shorten the cache time.
- `deny` has exactly `reason`, a token that matches `[a-z][a-z0-9_]{0,63}`. It appears in the audit line. Use `no_mapping` for "this user has no key": it is the reason that lets a `chain` move on to the next resolver.
- Anything else is a failure: another status, a missing or extra key, a wrong type, a denial reason that is not such a token, or a body larger than 64 KiB.

### What the user sees when it goes wrong

| What happens | The gateway | The client receives |
|---|---|---|
| Your endpoint answers `allow` | Forwards with the key | The MCP answer |
| `deny` with `no_mapping` | Audit outcome `denied`, reason `no_mapping` | HTTP `403`, JSON-RPC error `-31010`, no OAuth challenge |
| `deny` with `ambiguous_mapping` | Same, reason `ambiguous_mapping` | HTTP `403`, `-31011` |
| `deny` with any other reason | Same, with your reason | HTTP `403`, `-31012` |
| Connection error, timeout, or status `429`, `502`, `503`, `504` | Retries with exponential backoff and jitter, up to `max_attempts` and `total_timeout_seconds` | After the last attempt: HTTP `503`, `Retry-After: 5`, `-31009` |
| Any other status (including `401` from a signature you rejected, `404`, `500`) | No retry | HTTP `500`, `-31005` |
| A malformed or oversized answer, an unknown `upstream` name, an invalid key | No retry | HTTP `500`, `-31005` |
| A TLS certificate the gateway cannot verify | No retry | HTTP `500`, `-31005` |

The client always sees a generic message. The reason is in the gateway's audit line and logs, never in the client's answer. Redirects from your endpoint are not followed.

### A minimal webhook

[`examples/webhook_resolver.py`](../examples/webhook_resolver.py) is a complete webhook written with the Python standard library only. It checks the signature and the nonce, looks the verified e-mail up in a JSON file, reads the key from a key file and answers `allow` or `deny`. Replace `load_entries` with a query on your own system.

The lookup table [`examples/webhook-keys.json`](../examples/webhook-keys.json):

<!-- not-config -->
```json
{
  "alice@example.com": {"api_key_file": "alice.key", "upstream": null, "labels": {"client": "example-publisher"}, "ttl_seconds": 300},
  "bob@example.com": {"api_key_file": "bob.key", "upstream": "main", "labels": {"client": "example-publisher"}, "ttl_seconds": null}
}
```

Run it (it has no defaults: every variable is required, except the two TLS ones, which go together):

```bash
export WEBHOOK_HMAC_SECRET_FILE=./webhook-hmac        # the same file content as the gateway's secret_file
export WEBHOOK_KEYS_FILE=./keys/webhook-keys.json      # key files sit next to it
export WEBHOOK_TIMESTAMP_WINDOW_SECONDS=30
export WEBHOOK_HOST=0.0.0.0 WEBHOOK_PORT=8443
export WEBHOOK_TLS_CERT_FILE=./tls.crt WEBHOOK_TLS_KEY_FILE=./tls.key
python webhook_resolver.py
```

Test it before you point the gateway at it, with [`examples/resolve_probe.py`](../examples/resolve_probe.py), which signs a request exactly as the gateway does and prints the answer with the key hidden:

```bash
python resolve_probe.py --url https://keys.example.com/resolve --secret-file ./webhook-hmac --email alice@example.com
```

The probe verifies the webhook's certificate against the system's authorities. For a certificate from your own authority (the gateway trusts it through `ca_file`), point the probe at the same bundle: `SSL_CERT_FILE=./webhook-ca.crt python resolve_probe.py ...`.

A unit test runs this example against the gateway's own webhook resolver (an allow, a deny, a wrong secret, a replayed request), so it stays correct.

Things the example does not do, and a production webhook may need: mutual TLS (put a reverse proxy that verifies client certificates in front, or use your framework's TLS options), a database connection pool, request logging policy for your own audit needs, and rate limiting of failed signatures. Never log the answer: it carries the key.

## `store_sql`

One read-only `SELECT` in PostgreSQL.

<!-- config-fragment: resolver -->
```json
{
  "type": "store_sql",
  "dsn_file": "/run/gateway/secrets/pg-dsn",
  "query": "SELECT api_key, upstream, labels FROM gateway_keys WHERE email = %(email)s",
  "default_upstream": "main",
  "pool_min_size": 1,
  "pool_max_size": 4,
  "connect_timeout_seconds": 2,
  "statement_timeout_seconds": 2
}
```

- One `SELECT` statement, no `;` inside (one trailing `;` is allowed), named parameters only (`%(subject)s`, `%(email)s`, `%(organization)s`, at least one of them). A literal percent sign is `%%`.
- Columns: `api_key` (text), `upstream` (text, nullable: null selects `default_upstream`) and `labels` (a json or jsonb object of strings, nullable). No row is `no_mapping`; more than one row is `ambiguous_mapping`.
- It runs in a read-only transaction. A database that is down at start does not stop the gateway: requests get `503` (`-31009`) until it is back.

## `store_redis`

One key lookup.

<!-- config-fragment: resolver -->
```json
{
  "type": "store_redis",
  "url_file": "/run/gateway/secrets/credentials-redis-url",
  "key_pattern": "mcp-gateway:cred:{email}",
  "default_upstream": "main",
  "connect_timeout_seconds": 2,
  "socket_timeout_seconds": 2,
  "max_connections": 8
}
```

`key_pattern` uses `{subject}`, `{email}` and `{organization}` and no other braces. Each substituted value is percent-encoded over its UTF-8 bytes: every character except `A-Z a-z 0-9 _ . - ~` becomes `%XX`, so `alice@example.com` is stored under `mcp-gateway:cred:alice%40example.com`. Write your keys the same way (Python: `urllib.parse.quote(value, safe="")`).

The value is a JSON object with exactly `api_key`, `upstream` (a name, or `null`) and `labels` (an object of strings, or `null`):

<!-- not-config -->
```json
{"api_key":"oat_example-key","upstream":"main","labels":{"client":"example-publisher"}}
```

```bash
redis-cli -u "$REDIS_URL" SET 'mcp-gateway:cred:alice%40example.com' \
  '{"api_key":"oat_example-key","upstream":"main","labels":{"client":"example-publisher"}}'
```

(The command is for a test: do not put real keys in a shell history.) A missing key is `no_mapping`. A value that is not that JSON is a `500`. A connection error or timeout is a `503`.

## `chain`

Several resolvers in order, at least two, none of them a chain. `no_mapping` from a member moves on to the next. **Any other denial and any error stop the chain** and are returned as they are, so a request fails closed.

<!-- config-fragment: resolver -->
```json
{
  "type": "chain",
  "resolvers": [
    {"type": "static", "mapping_file": "/run/gateway/secrets/mapping.json"},
    {
      "type": "webhook",
      "url": "https://keys.example.com/resolve",
      "auth": {"type": "hmac", "secret_file": "/run/gateway/secrets/webhook-hmac"},
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
}
```

## The resolver cache

Without a cache, every request calls the resolver (and your webhook or database). `resolver_cache` keeps answers:

<!-- config-fragment: resolver_cache -->
```json
{"positive_ttl_seconds": 300, "negative_ttl_seconds": 15, "max_entries": 10000}
```

- `positive_ttl_seconds`: how long an answer that carries a key is kept. A webhook's `ttl_seconds` can only make it shorter.
- `negative_ttl_seconds`: how long a denial is kept. Keep it short: a user you map now stays denied for at most this long.
- The cache key is the whole identity (subject, e-mail, organization, sorted groups). Errors are never cached.
- Concurrent misses for one identity share one backend call, per process: two replicas that miss at the same time each ask the backend.
- When Alien answers `401` or `402` for a cached key, the entry is dropped so the next request asks the resolver again.
- `max_entries` is a number with `state.type` `memory` and `null` with `redis`, where entries expire by their TTL only (the configuration is refused otherwise).

The effect of the TTL on operations: a key you change at the source reaches the gateway only after the cached entry expires (chapter 8, key rotation).

With `state.type` `redis` and several replicas, use this form (with the state block of chapter 5):

<!-- config-fragment: resolver_cache base=ha -->
```json
{"positive_ttl_seconds": 300, "negative_ttl_seconds": 15, "max_entries": null}
```

## Choosing

| Situation | Resolver |
|---|---|
| Up to a few dozen users, you can write a file | `static`, one key per user, behind no cache |
| You have a customer database or CRM | `webhook`, behind a cache of a few minutes, `ttl_seconds` set by you |
| The mapping already lives in PostgreSQL | `store_sql` behind a cache |
| Large number of users, simple lookups | `store_redis` |
| Staff in a file, everyone else in a database | `chain` of `static` then `store_sql` |

Continue with [5. Deploying](05-deploying.md).
