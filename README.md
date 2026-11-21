# Alien MCP Gateway

A gateway you host in front of Alien MCP servers.

- It accepts MCP clients (Claude, ChatGPT, Copilot, VS Code, Cursor) that sign in with **your own OAuth server**, for example Keycloak.
- It maps each verified identity to an Alien API key through a pluggable credential resolver.
- It forwards the MCP traffic to Alien unchanged.

Your users sign in with their usual accounts, and your MCP endpoint lives on your own domain.

Status: in development. What exists today is described below; the setup tutorial will be published before the first release.

## How it works

```
MCP client ──OAuth 2.1 + PKCE──> your OAuth server (token aud = the gateway URL)
    │
    └─ Bearer JWT ─> gateway: check the JWT (JWKS, iss, aud, exp/nbf)
                             identity = claims (subject, verified email, organization, groups)
                             resolver(identity) -> Alien API key, or 403
                             drop the client's credentials and forgeable headers, add the key
                             stream to the Alien MCP server, stream the answer back untouched
```

- The gateway is an OAuth **resource server** only. It issues no tokens and registers no clients: your OAuth server does that.
- It publishes Protected Resource Metadata (RFC 9728) at the path-specific URL only (`/.well-known/oauth-protected-resource/mcp` for a `/mcp` endpoint), with `resource` equal to `public_url`. The origin-level URL answers `404`: RFC 9728 requires `resource` to match the URL the document was fetched for.
- A request without a valid token gets `401` with `WWW-Authenticate: Bearer resource_metadata="…", scope="…"`.
- A signed-in user with no mapping gets `403` with a JSON-RPC error (`-31010`) and no challenge.
- If Alien rejects the mapped key (`401` or `402`), the client gets a JSON-RPC error (`-31001` / `-31002`), never an OAuth challenge, so it does not loop on re-authentication.
- Inbound headers are allow-listed. `Authorization`, `Cookie`, `X-Oauth-Access-Token`, `X-Job-Id`, `X-Organization-Id`, `X-Mcp-Config-Id` and `X-Forwarded-*` never reach Alien.
- One JSON audit line per request: subject, organization, labels, method, tool, status, outcome, latency. Never content, tokens or keys.

## Prerequisites: your OAuth server

The gateway works with any OAuth server that provides all of the following:

1. **Discovery:** RFC 8414 authorization-server metadata or OpenID Connect discovery, advertising `S256` in `code_challenge_methods_supported`.
2. **JWT access tokens** signed with an asymmetric key (RS256, ES256, PS256 or EdDSA) and a published JWKS. The gateway validates tokens locally; it does not use token introspection.
3. **The gateway URL as audience:** the access token's `aud` must contain the gateway's public URL. Keycloak does this with an optional client scope carrying an Audience mapper; Ory Hydra does it in the consent step (`grant_access_token_audience`).
4. **Client registration that MCP clients can use:** anonymous Dynamic Client Registration (RFC 7591), Client ID Metadata Documents, or clients you pre-register for each MCP client.
5. **Identity claims** in the access token: a stable subject, and the claims you map on (`email` with `email_verified`, an organization claim, groups). Claim paths are configurable, including nested ones such as `ext.org`.

Known incompatibility: authentik 2026.8 dynamic client registration requires a pre-issued bearer token, and the clients it creates sign tokens with their own secret (HS256) under a per-application issuer, so a resource server cannot verify them through a JWKS. With authentik, pre-register the MCP clients instead.

## Configuration

The wire contract (JSON-RPC error codes, `_meta` keys, HTTP statuses, challenges, headers) is in [`docs/protocol.md`](docs/protocol.md); it is stable within a major version.

One strict JSON document in the `MCP_GATEWAY_CONFIG` environment variable. Every key is required, unknown keys are refused, and there is no default for any issuer, audience or secret: the gateway refuses to start on an incomplete configuration. The full reference, key by key with bounds and a complete example, is [`docs/configuration.md`](docs/configuration.md). The gateway services in [`compose.yaml`](compose.yaml) are working examples.

Top-level keys: `version` (always `1`), `public_url`, `listen`, `auth` (issuer, JWKS, audiences, scopes, claim paths), `resolver`, `resolver_cache`, `upstreams`, `limits`, `audit`, `observability`, `sessions`, `state` and `policies` (allowed origins, tool policy, rate limits).

- `sessions`: an upstream `Mcp-Session-Id` is bound to the subject it was issued to (in memory, or in Redis with `state.type` `redis`, which several replicas need). A request that presents an unknown, expired or foreign session id gets `404` and a JSON-RPC error (`-31007`) and is not forwarded; the client starts a new session.
- `state`: `memory` (one replica) or `redis` (shared by every replica: sessions, the credential cache and the rate limit buckets). With Redis the gateway fails closed (`503`, `-31013`) when it cannot reach it, and `GET /readyz` reports readiness. See [`docs/configuration.md`](docs/configuration.md#state).
- `policies.allowed_origins`: browser origins allowed on the MCP endpoint. An `Origin` header that is not listed gets `403` (`-31008`) before authentication; no `Origin` header passes.
- `policies.tools`: per-caller tool allow and deny lists (exact names or `*`, matched on organization or resolver label). A denied `tools/call` is answered as an unknown tool and never forwarded, and `tools/list` replies are filtered. See [`docs/configuration.md`](docs/configuration.md#policiestools).
- `policies.rate_limits`: token buckets per subject and per organization, in memory or shared in Redis. A refused request is answered in the form its sender can read (`isError` tool result, `-31014`, or `429`) with `Retry-After`. See [`docs/configuration.md`](docs/configuration.md#policiesrate_limits).
- A client disconnect is not a cancellation: an upstream request already sent keeps being read to its end (and discarded), bounded by the upstream's `read_timeout_seconds` and by `max_detached_reads` per upstream (beyond it the request is cancelled, counted `shed`); only an explicit `notifications/cancelled` cancels. Listen streams, `DELETE` and notifications are closed on disconnect. See [`docs/configuration.md`](docs/configuration.md#client-disconnects).
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

Any of them can sit behind the optional `resolver_cache` (positive and negative TTL, single flight, dropped on an upstream `401`/`402`). Secrets (key files, DSN, Redis URL, HMAC secret, client certificates) are always referenced by file path, never inlined. See [`docs/configuration.md`](docs/configuration.md#resolver).

On Alien, an API key is a billed user and an upstream session: users that share a key share a line in the consumption feed. Map one key per user when you need per-user accounting.

### Observability

`observability.otlp` exports traces and metrics over OTLP (`grpc` or `http/protobuf`); `{"otlp": null}` exports nothing. Each request is one root trace with three spans (request, credential resolution, upstream call) and the metrics `mcp_gateway.requests`, `mcp_gateway.request.duration`, `mcp_gateway.upstream.duration`, `mcp_gateway.resolver.lookups`, `mcp_gateway.upstream.detached`, `mcp_gateway.upstream.detached.active`, `mcp_gateway.sessions.bound` and `mcp_gateway.telemetry.failures` (telemetry is best effort: a recording or export failure is counted, never changes a response). Spans and metrics never carry the subject, e-mail, a token, a key, a session id, arguments or content; resolver labels are span attributes only. Details: [`docs/configuration.md`](docs/configuration.md#observability).

### Request id and audit line

Every response carries an `x-request-id` the gateway generated (a client-sent one is ignored and never forwarded upstream). With `audit.enabled`, each request writes one JSON line to stdout, whatever way it ends:

```json
{"ts":"2026-11-20T09:14:03.512+00:00","event":"mcp_request","request_id":"6f1c2b0e-5a47-4d8e-9d52-3b0a4c9e7f10","trace_id":"4bf92f3577b34da6a3ce929d0e0e4736","subject":"user-1","organization":"example-org","labels":{"client":"example"},"http_method":"POST","rpc_method":"tools/call","rpc_id":7,"tool":"search","status":200,"outcome":"forwarded","reason":null,"upstream":"main","latency_ms":182.4,"upstream_status":200,"upstream_request_id":"req-81c4","upstream_session":false}
```

The line holds identifiers, the method and tool name, the outcome and the latency; never content, arguments, tokens, keys or session ids. `request_id` joins the line to the trace (`trace_id`) and to the client's response header. Field list and outcomes: [`docs/configuration.md`](docs/configuration.md#audit-line).

## Container image

The release flow builds one image for `linux/amd64` and `linux/arm64` and pushes it to the registry configured in `.gitlab-ci.yml` (`REGISTRY`, `IMAGE_NAME`). Tags: `<version>` from `main`, `<version>-rc` from `staging`, `<version>-dev` from `dev`, plus the moving tags `latest`, `latest-staging` and `latest-dev`. Pin deployments to a version, or better to the digest. Each image carries an SBOM and a provenance attestation, and the OCI labels `source`, `version`, `revision` and `created`.

The image runs as uid 1000 with no writable path: the gateway keeps its state in memory or in Redis and logs to stdout. Run it the way it is tested, read-only and without capabilities:

```bash
docker run --read-only --cap-drop ALL --security-opt no-new-privileges \
  -e MCP_GATEWAY_CONFIG="$(cat config.json)" \
  -v "$PWD/secrets:/run/secrets:ro" \
  -p 8000:8000 <registry>/mcp-gateway:<version>

curl http://localhost:8000/healthz   # {"status":"ok"}: the process is up
curl http://localhost:8000/readyz    # {"status":"ready"}: the shared state answers; 503 drains the replica
```

The image's `HEALTHCHECK` is a real GET of `/healthz` on the port in `MCP_GATEWAY_CONFIG`, so `docker ps` shows `healthy` once the gateway serves. In Kubernetes use `/healthz` for the liveness probe and `/readyz` for the readiness probe, and set `readOnlyRootFilesystem: true`.

Build it yourself (the version, revision and date are required build arguments, a build without them fails):

```bash
docker build --build-arg VERSION=$(sed -n 's/^version = "\(.*\)"/\1/p' pyproject.toml) \
  --build-arg VCS_REF=$(git rev-parse HEAD) --build-arg BUILD_DATE=$(date -u +%Y-%m-%dT%H:%M:%SZ) -t mcp-gateway .
```

Both base images are pinned by digest in the `Dockerfile`; bump them on purpose. CI scans the pushed digest with trivy and fails on any HIGH or CRITICAL vulnerability that has a fix, so a failing scan means a new base digest, not a retry.

## Development stack

One compose file set (`compose.yaml` plus a target file) runs the whole thing on loopback. Three targets share it:

- **dev**: the gateways forward to a real dev MCP server (`compose.dev.yaml`) and read the state that `scripts/dev_fixture.py` writes from the real Alien dev platform.
- **ci-dev** (`compose.ci-dev.yaml`): the dev target for CI. The state is laid out from CI-provided key files (`scripts/ci_dev_state.py`) into a volume instead of a directory on your machine, so it works against a remote daemon. Run through `scripts/ci_e2e_dev.sh`.
- **stub**: the gateways forward to a fault-injection upstream (`compose.stub.yaml`) and the stack writes its own state with fake keys, so nothing outside the repository is needed. This is what CI runs.

| Service | Address | Notes |
|---|---|---|
| Keycloak 26.8 | `http://localhost:18080` | realm `alien-gateway-dev` from `dev/keycloak/` |
| Ory Hydra v26.2 | `http://localhost:14444` | login/consent app from `dev/hydra-login/` on `:13000` |
| `gateway-keycloak` | `http://localhost:18000/mcp` | Keycloak, `static` resolver, a tool policy (a rule on the `client` label) and a per-subject rate limit |
| `gateway-hydra` | `http://localhost:18001/mcp` | Hydra, `static` resolver, same policy and limit |
| `gateway-chain` | `http://localhost:18002/mcp` | Keycloak, `chain` of `store_redis`, `store_sql` and `webhook` (HMAC) behind a resolver cache; requires the `mcp-write` scope |
| `gateway-unreachable` | `http://localhost:18003/mcp` | Keycloak, a `webhook` resolver nobody listens to (the resolver-down case) |
| `gateway-ha-a`, `gateway-ha-b` | `http://localhost:18005/mcp`, `http://localhost:18006/mcp` | Two replicas of one gateway (same config, same `public_url` `http://localhost:18004/mcp`, so one token works on both): Keycloak, `store_redis` store holding every persona, `state` `redis` so sessions, the resolver cache and the rate limit buckets are shared, a tool policy denying `policy_denied_probe`, rate limits from `HA_RATE_LIMITS`; healthchecks on `/readyz` |
| `gateway-lb` | `http://localhost:18004/mcp` | nginx round robin to the two replicas (`dev/gateway-lb/`), the address the pair's tokens are issued for |
| `gateway-noredis` | `http://localhost:18007/mcp` | Keycloak, `static` resolver behind a cache, `state` `redis` pointing at a closed port: alive (`/healthz`) but never ready, every request is a `503` |
| `otel-collector` | internal `:4318` | Receives the OTLP export of `gateway-chain` (the other gateways export nothing) and writes it to the `otel` volume as JSON lines; the stub harness reads it |
| PostgreSQL, Redis | `127.0.0.1:15432`, `127.0.0.1:16379` | credential stores, refilled at every start by the `seed` service |
| dev webhook | `127.0.0.1:18090` | `dev/webhook-resolver/`, `GET /_stats` counts the calls it served |
| upstream stub (stub target) | `127.0.0.1:14000` | `dev/upstream-stub/`, control endpoints under `/_stub/` |

Every gateway has two upstreams, `stateful` and `stateless`, chosen per mapping entry. In the stub target they are the stub's `/stateful/mcp` and `/stateless/mcp`; in the dev target both are the real dev MCP (all of them are stateless). Gateway configuration is one JSON block per gateway in `compose.yaml`.

Test users, the same in both identity providers: `alice@client-x.test`, `bob@client-y.test`, `carol@client-x.test`, `dave@client-x.test` (behind the webhook, stub target only: the resolver cache test's own persona), `erin@client-q.test` and `frank@client-q.test` (one organization, for the per-organization rate limit; Keycloak and the stub target only), `nomap@client-z.test` (no mapping), `revoked@client-r.test` and `unpaid@client-p.test` (their keys are refused by the stub with 401 and 402; in the dev target they have no mapping). On the replica pair every persona is resolved from Redis (`mcp-gateway:ha:{email}`); bob and carol use the stateful upstream, the others the stateless one. On `gateway-chain`, alice is resolved by Redis, bob by PostgreSQL and carol and dave by the webhook. Access tokens live 60 seconds. Anonymous DCR is enabled on both providers (Keycloak through Trusted Hosts covering loopback and the compose network gateway). `allowed_origins` is `https://claude.ai` and `http://localhost:6274` (the MCP Inspector).

### Dev target

```bash
cp .env.example .env          # fill in random secrets, the upstream MCP URL and a state directory
chmod 600 .env                # COMPOSE_FILE in .env selects the dev target
uv sync --extra dev
# write the state (see "Alien fixture" below), then:
docker compose up -d --build --wait
```

### Stub target

```bash
sh scripts/ci_e2e_stub.sh     # secrets, stack, harness, artifacts in artifacts/, stack removed
```

`HA_RATE_LIMITS`, the `policies.rate_limits` value of the replica pair, comes from the env file: the script writes a small one (burst 8 per subject and 12 per organization, one token per minute) so tests can exhaust it, the dev target's `.env.example` documents `null`. The `seed` step writes the pair's Redis URL and a fresh encryption key (`state.key`, base64 of 32 random bytes) at every start.

The script needs only a Docker daemon with the compose plugin. To keep the stack up for debugging, run the compose commands it contains by hand (`.env.stub` holds the throwaway secrets it generated).

The gateway state lives in a directory (`state_layout.py`): one key file per persona, `mapping.json` for the static resolver, `stores.json` for the seed step and `webhook/` for the webhook service. `scripts/stub_fixture.py` writes it with fake keys (the `state-init` service runs it), `scripts/dev_fixture.py` with real ones. The `seed` service loads PostgreSQL and Redis from it and writes the connection secrets (database DSN, Redis URL, webhook HMAC secret) from the stack's environment into a volume the gateways mount read-only. Config files reach the daemon through image builds, not bind mounts, so the stack also runs against a remote daemon (CI).

### Alien fixture (dev platform)

`scripts/dev_fixture.py` sets up, through the real Alien API, two managed client organizations created inside a sponsored group of a sponsor organization that owns a private cluster, three client-managed users (alice and carol in the first organization, bob in the second), and an `api-key` token for each. Everything runs as the sponsor's service account (a client-managed `org-admin` user, authenticated by its `api-secret` key). It is idempotent: organizations cannot be deleted on Alien, so every object is found by name (or user by email) and reused, and a key is reused while `GET /users/me` accepts it. It writes the gateway state, the key files (mode 600) and a non-secret `fixture.json` into the state directory.

```bash
uv run python scripts/dev_fixture.py \
  --sponsor-key-file <file with the sponsor service account's api-secret key> \
  --api-base <Alien dev API> --mcp-url <dev MCP URL> \
  --sponsor-org-id <id> --cluster-id <id> --dataset-id <id> \
  --state-dir <MCP_GATEWAY_STATE_DIR>
```

The key is read from the file and never passed on the command line or printed. The IdP personas keep their addresses (`alice@client-x.test`); `fixture.json` records each persona's Alien-side address as `alien_email`.

## Tests

Three layers, from fastest to most real:

| Layer | What it proves | Where it runs | Command |
|---|---|---|---|
| Unit (`tests/unit`) | Logic of every module, no network | every push (CI `test` jobs), locally | `uv run pytest -m "not e2e and not e2e_stub and not integration"` |
| Integration and `e2e_stub` | The resolvers against real PostgreSQL and Redis; the whole gateway against real Keycloak and Hydra, a real OpenTelemetry collector and the fault-injection upstream stub | CI job `e2e-stub`, locally in a throwaway Docker daemon | `sh scripts/ci_e2e_stub.sh` |
| `e2e` (real dev) | The gateway in front of a real dev MCP server, with the Alien dev platform metering each call, read back from its consumption feeds | CI job `e2e-dev` (merge requests and the default branch), locally | see below |

```bash
uv run ruff check src tests scripts dev && uv run ruff format --check src tests scripts dev
uv run pyright && uv run pyright scripts tests/e2e
uv run pytest tests/unit -q
```

### `integration` and `e2e_stub`

- `integration`: the PostgreSQL and Redis resolvers against real servers (`MCP_GATEWAY_TEST_PG_DSN`, `MCP_GATEWAY_TEST_REDIS_URL`; a selected test without them fails).
- `e2e_stub`: every test performs the real OAuth journey (challenge, resource metadata, authorization-server metadata, anonymous DCR, authorization code + PKCE S256 + `resource` through the provider's real login form) with a real test user, then calls the gateway against the stub upstream. It covers: resource metadata, challenges and `403 insufficient_scope`, the Origin policy, the `Mcp-Method`/`Mcp-Name` check and repeated headers, header stripping and key injection, session binding between two users, `DELETE` and upstream session expiry, `Last-Event-ID` replay, a stateless upstream's `405`, upstream `401`/`402`/`404`/`500`/`502`/`503`, incremental and failing streams, a client leaving mid-stream (the upstream request runs to its end) and a closed `GET` stream (closed upstream too), each resolver of the chain, the resolver cache (counted at the webhook), an unreachable webhook (`503`, `Retry-After`), the replica pair (a session opened on one replica used on the other and refused to another user, an upstream `401` on one replica invalidating the shared resolver cache for the other, the tool policy on JSON and SSE `tools/list` and on a denied call and a batch, rate limit buckets shared across replicas and across an organization with every refusal shape, a gateway without its Redis answering `503` and not ready), `x-request-id`, and the telemetry the collector receives (request span, metrics, and a scan proving no token, key, e-mail, subject, session id or tool argument is exported).

`scripts/ci_e2e_stub.sh` runs both in a container on the daemon's host network (so the `localhost` issuers resolve as on a laptop) and needs only Docker with the compose plugin. To reproduce the CI job before pushing (CI minutes are paid), run it inside a privileged `docker:<version>-dind` daemon of its own, whose ports never clash with a running dev stack:

```bash
docker run -d --privileged --name ci-dind -e DOCKER_TLS_CERTDIR= docker:28.3.3-dind
docker run --rm --link ci-dind:docker -e DOCKER_HOST=tcp://docker:2375 \
  -v "$PWD":/src:ro --tmpfs /work:exec -w /work docker:28.3.3-cli \
  sh -c 'tar cf - -C /src --exclude=./.git --exclude=./.venv --exclude=./.env* . | tar xf - && sh scripts/ci_e2e_stub.sh'
```

### Load test

`tests/load/loadtest.py` opens N concurrent long-lived SSE streams (N workers, each holding one stream at a time, reading it to the reply event) and measures what the gateway adds. Per target it records the time to response headers, to the first event and to the reply, the status, the JSON-RPC or tool error and the event count of every request (`raw-<target>.csv`). A request is an error when the status is not 200, the reply is a JSON-RPC error or `isError`, the event count differs from `--expected-events`, or it times out. The targets run one after the other with the same workload (warmup requests excluded from the statistics), and every measurement parameter is a required argument: there is no default that changes what is measured, so a real-dev run is explicitly small (for example `--concurrency 20 --requests-per-worker 3`).

```bash
sh scripts/loadtest_stub.sh   # needs the LOAD_* variables listed in its header
```

`scripts/loadtest_stub.sh` brings the stub stack up like `ci_e2e_stub.sh`, samples CPU and memory of the gateway containers (`LOAD_GATEWAY_SERVICES`, `scripts/loadtest_stats.sh`), runs the load client in a container on the host network (`compose.load.yaml`) against the stub directly and then against the gateway, copies the results to `ARTIFACTS_DIR` and tears everything down. Run it in a throwaway Docker daemon exactly like the stub suite above (add `-e ARTIFACTS_DIR=/artifacts -v <dir>:/artifacts -e LOAD_...=...` to the `docker run` of the CLI container to keep the results). A smoke workload:

```bash
LOAD_CONCURRENCY=20 LOAD_REQUESTS_PER_WORKER=3 LOAD_WARMUP_REQUESTS=1 LOAD_PERSONAS="alice bob carol dave" \
LOAD_TOOL=slow_sse LOAD_ARGUMENTS_JSON='{"count": 5, "delay_seconds": 0.2}' LOAD_EXPECTED_EVENTS=6 \
LOAD_TIMEOUT_SECONDS=30 LOAD_MAX_RUN_SECONDS=300 sh scripts/loadtest_stub.sh
```

Reading the results:

- `summary.json`, `targets.<name>`: `count`, `errors` and `errors_by_reason`, `error_rate`, `total_seconds` and `first_event_seconds` (`p50`, `p95`, `p99`, `max`, over the successful requests) and `throughput_per_second`. A worker whose handshake failed counts every call it could not make as an error.
- `summary.json`, `overhead.<name>`: the target's percentile minus the baseline's, for the same percentile. It is a difference of two distributions, not a per-request pairing, and it includes the cost of the gateway's authentication and routing as well as the proxying.
- `stats-summary.json`: per container, peak and mean CPU (percent of one core) and memory (MiB), from `stats.jsonl`.
- The run parameters, the start and end times (UTC) and the git commit (`LOADTEST_GIT_COMMIT`) are in `summary.json`.

OAuth tokens: every persona logs in once (one grant per persona, sequentially) before the workers start, so the measurement never includes a login. A token is obtained again when less than `max(5 s, 0.2 x its lifetime)` is left (lifetime = `exp - iat`; a token without both claims is refused). The dev realm issues 60-second tokens, so a long run still logs in again about every 48 s per persona; that is bounded (personas x run minutes x 1.25 grants) but not zero, and each login registers a new client: the realm caps dynamic client registration at 200 clients, so keep personas x run minutes well under it. `summary.json` carries `oauth_grants` (grants per target) so a run that keeps logging in is visible; a stub-key target reports 0.

Against another target (the real dev MCP, any gateway) run the client directly from `tests/` with `src/` and `scripts/` on `PYTHONPATH`: `python -m load.loadtest --target-url NAME=URL --target-auth NAME=oauth:<issuer> ...` (the users' password in `E2E_USER_PASSWORD`), or `NAME=stub-key` with `--state-dir` for the stub's keys.

### `e2e`: the real-dev suite

It behaves like a real MCP client against the dev target: it reads the gateway's 401, fetches the resource metadata and the authorization-server metadata, registers through DCR, runs authorization code + PKCE S256 + `resource` through the provider's real login form, then calls `initialize`, `tools/list` and `tools/call` through the gateway with the official `mcp` SDK. It reads the Alien consumption feeds back and asserts each call was billed to the expected client-managed user and client organization, with the sponsorship applied.

- Both static gateways: happy path for two personas, wrong audience (401), expired token (401), unmapped user (403), revoked Alien key (JSON-RPC error, no challenge), forged `X-Job-Id` / `X-Organization-Id` (stripped; each negative case has a positive control that goes direct to the upstream).
- Policy and limits on both static gateways (tool policy `default deny [datacluster_keyword_search, ...]` with a rule for `client-y`; per-subject rate limit burst 40, 20 per minute): `tools/list` filtered per persona, a denied `tools/call` answered `-32602` and never billed (proved against the same call through `gateway-chain` as a positive control), an allowed call billed, and a rate-limited persona (carol) refused in the documented shapes (`isError` result with `Retry-After` for `tools/call`, `-31014` for `ping`) without any new event in the feed.
- Replica pair (`gateway-ha-a`/`-b` behind nginx): four calls through the load balancer, each billed.
- `gateway-chain`: a token carrying `mcp-write` for a persona of each store (Redis, PostgreSQL, webhook); each call is metered for the right managed user and organization, with the sponsor line in the sponsor's feed; every response has an `x-request-id`; the stateless upstream's `GET` and `DELETE` answers (`405`) pass through untouched.

```bash
E2E_STATE_DIR=<state dir> E2E_SPONSOR_KEY_FILE=<sponsor key file> E2E_API_BASE=<Alien dev API> \
E2E_UPSTREAM_URL=<dev MCP URL> E2E_EXISTING_JOB_ID=<an existing dev job id the personas do not own> \
E2E_RUN_DIR=<evidence dir> uv run pytest tests/e2e -m e2e -v
```

Against another deployment of the same gateway (a Helm install on a cluster, say), set `E2E_GATEWAY_URL_<NAME>` and `E2E_ISSUER_<NAME>` for the providers the selected tests use (`KEYCLOAK`, `HYDRA`, `CHAIN`, `REPLICAS`). They replace the dev stack's `http://localhost:1800x/mcp` and issuer; each string must be exactly the deployment's `public_url` and issuer (the harness compares them with the protected-resource metadata and the token), the gateway must be configured like the dev-stack service of that name, and the identity provider must put the gateway URL in the token's audience. The state, sponsor key and upstream variables stay as above; `test_revoked_alien_key_gives_a_jsonrpc_error_not_a_challenge` swaps a key file in `E2E_STATE_DIR` and so only works where the gateway reads that directory.

It needs the dev target up (`docker compose up -d --build --wait` with `.env`), the fixture applied and network access to the Alien dev platform. A run takes about fifteen minutes, mostly waiting for tokens to expire and for metering to appear in the feeds.

## CI

| Job | When | What |
|---|---|---|
| lint, type check, unit tests, SAST, secret detection | every pipeline | shared templates; `PYTEST_MARKER` keeps the stack-bound suites out |
| `docker-build`, `image-scan` | the release branch's release commit, and `staging` and `main` | `scripts/ci_image_build.sh` (buildx, both architectures, SBOM and provenance attestations, in a `docker:dind` service), then `scripts/ci_image_scan.sh` (trivy on the pushed digest, CycloneDX SBOM per architecture as an artifact; fails on fixable HIGH or CRITICAL) |
| `e2e-stub` | merge requests and branches, when code, tests, scripts, `dev/`, compose files, Dockerfiles, `pyproject.toml`, `uv.lock` or `.gitlab-ci.yml` change | `scripts/ci_e2e_stub.sh` in a `docker:dind` service; fake keys only |
| `e2e-dev` | merge requests and the default branch (never a push to `dev`), same path filter | `scripts/ci_e2e_dev.sh` in a `docker:dind` service against the real dev MCP server and Alien dev platform; one run at a time (`resource_group`) |

A branch push is skipped while the branch has an open merge request (its merge request pipeline runs instead).

`e2e-dev` reads these CI/CD variables. File-type variables hold the content of the file; all are protected, so the job gets them on protected branches and tags only, and a merge request pipeline from an unprotected branch fails at once with the list of missing names. The key files are masked.

| Variable | Type | Content |
|---|---|---|
| `E2E_DEV_SPONSOR_KEY_FILE` | file | API key of the sponsor's service account (reads the feeds, mints the key the revoked-key test revokes) |
| `E2E_DEV_ALICE_KEY_FILE`, `E2E_DEV_BOB_KEY_FILE`, `E2E_DEV_CAROL_KEY_FILE` | file | API keys of the three client-managed users |
| `E2E_DEV_FIXTURE_FILE` | file | `fixture.json` written by `scripts/dev_fixture.py` (ids and names, no secret) |
| `E2E_DEV_UPSTREAM_URL` | value | URL of the dev MCP server |
| `E2E_DEV_API_BASE` | value | Base URL of the Alien dev API |
| `E2E_DEV_EXISTING_JOB_ID` | value | A dev job id the personas do not own (the forged-header positive control) |

The identity providers' and stores' secrets are generated per run. To reproduce the job locally, run `scripts/ci_e2e_dev.sh` in a `docker:dind` setup like the one above, with the variables set to file paths and values. The job's artifacts (evidence, JUnit, compose logs) are visible to developers only and expire after a week.
