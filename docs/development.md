# Development

How to run the gateway's development stack, its test suites, the load test and the CI. Contributions: see [CONTRIBUTING.md](../CONTRIBUTING.md). For a deployment, start from the [tutorial](tutorial/en/README.md).

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
| `keycloak-template` (stub target) | `http://localhost:18081` | The deployable realm template (`deploy/keycloak/`) rendered by its `render.sh` (realm `template-check`, audience `http://localhost:18009/mcp`, no gateway behind it) and imported into the same Keycloak image (`dev/keycloak-template/`); `tests/e2e/stub/test_keycloak_template.py` checks what an anonymous client can get from it |

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

`--name-prefix` (default `mcp-gateway-e2e`, the shared fixture) names every object the fixture creates: the grant, the group, the organizations, the token and, for any other prefix, the Alien-side addresses (`alice@<prefix>.managed-client-x.test`), which must be unique on the platform. A new prefix with a new `--state-dir` sets up a separate chain from scratch, for example to check the creation path again; nothing it creates can be deleted once metered.

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
- `e2e_stub`: every test performs the real OAuth journey (challenge, resource metadata, authorization-server metadata, anonymous DCR, authorization code + PKCE S256 + `resource` through the provider's real login form) with a real test user, then calls the gateway against the stub upstream. It covers: resource metadata, challenges and `403 insufficient_scope`, the Origin policy, the `Mcp-Method`/`Mcp-Name` check and repeated headers, header stripping and key injection, session binding between two users, `DELETE` and upstream session expiry, `Last-Event-ID` replay, a stateless upstream's `405`, upstream `401`/`402`/`404`/`500`/`502`/`503`, incremental and failing streams, a client leaving mid-stream (the upstream request runs to its end) and a closed `GET` stream (closed upstream too), each resolver of the chain, the resolver cache (counted at the webhook), an unreachable webhook (`503`, `Retry-After`), the replica pair (a session opened on one replica used on the other and refused to another user, an upstream `401` on one replica invalidating the shared resolver cache for the other, the tool policy on JSON and SSE `tools/list` and on a denied call and a batch, rate limit buckets shared across replicas and across an organization with every refusal shape, a gateway without its Redis answering `503` and not ready), `x-request-id`, the deployable Keycloak realm template (only the authorization code flow with PKCE and the refresh grant yield a token, for the gateway audience, with full group paths: implicit, hybrid and password registrations refused, device, client credentials and `admin-cli` password grants refused), and the telemetry the collector receives (request span, metrics, and a scan proving no token, key, e-mail, subject, session id or tool argument is exported).

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
