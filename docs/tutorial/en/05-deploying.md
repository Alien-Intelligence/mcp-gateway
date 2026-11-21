# 5. Deploying

The gateway is one container image. This chapter covers the two supported ways to run it: the Helm chart in `charts/mcp-gateway/` (Kubernetes) and docker compose.

## 5.1 The image

Write `<registry>/mcp-gateway:<version>` below as the image reference. The release pipeline publishes the image to a registry that is internal to the project, so the **public pull location is announced in the repository README** and not repeated here. Until it is, mirror the image into your own registry and use your reference.

- One image for `linux/amd64` and `linux/arm64`. Tags: `<version>` (a release from `main`), `<version>-rc` (`staging`), `<version>-dev` (`dev`) and the moving tags `latest`, `latest-staging`, `latest-dev`.
- Pin a version, or better the digest. Never run `latest` in production.
- The process runs as uid 1000, needs no writable path (state lives in memory or Redis, logs go to stdout) and is tested read-only and without capabilities:

```bash
docker run --read-only --cap-drop ALL --security-opt no-new-privileges \
  -e MCP_GATEWAY_CONFIG="$(cat config.json)" \
  -v "$PWD/secrets:/run/secrets:ro" \
  -p 8000:8000 <registry>/mcp-gateway:<version>

curl http://localhost:8000/healthz   # {"status":"ok"}: the process is up
curl http://localhost:8000/readyz    # {"status":"ready"}; 503 {"status":"state_unavailable"} when the shared state cannot be reached
```

The whole configuration is one JSON document in one environment variable, `MCP_GATEWAY_CONFIG`. (The OpenTelemetry exporters also read the standard `OTEL_EXPORTER_OTLP_*` variables for the settings the `observability.otlp` block does not carry, for example a CA certificate.) A bad document stops the process at start with `startup refused: invalid MCP_GATEWAY_CONFIG: <key>: <rule>` in the log; the message names the key and the rule, never the value.

## 5.2 The configuration and its secrets

Two examples are provided, both accepted by the gateway's loader:

- [`examples/config-single.json`](../examples/config-single.json): one replica, `static` resolver, state in memory. Start here.
- [`examples/config-ha.json`](../examples/config-ha.json): two or more replicas, `webhook` resolver behind a cache, state in Redis, rate limits, OTLP export.

Change at least: `public_url`, `auth.issuer`, `auth.jwks_uri`, `auth.audiences` (it must contain `public_url`), `upstreams.main.url` (the Alien MCP server's URL, given by Alien) and `policies.allowed_origins`. [`configuration.md`](../../configuration.md) documents every key, with bounds.

**Secrets are files.** The configuration never holds a secret: keys such as `secret_file`, `dsn_file`, `url_file`, `headers_file`, `client_key_file` and `encryption_key_file` name a file that holds it. The ones `config-ha.json` uses:

| File | Content | How to make it |
|---|---|---|
| `state-redis-url` | `rediss://:<password>@<host>:6379/0` (a `redis://` or `rediss://` URL; it may carry the password) | Your Redis |
| `state-encryption-key` | The base64 of exactly 32 random bytes: the AES-256-GCM key of cached credentials, and the key that derives the names of Redis keys. **Every replica that shares a Redis must have the same file.** | `head -c 32 /dev/urandom \| base64 > state-encryption-key` |
| `webhook-hmac` | The shared secret of the webhook resolver. The webhook holds the same content | `head -c 32 /dev/urandom \| base64 > webhook-hmac` (any random secret works) |
| `otlp-headers` | `key=value` lines sent with every telemetry export | Your collector's auth header |
| `mapping.json` and the key files | The `static` resolver's mapping (chapter 4) | Only with `static` |

Create the files with a restrictive umask (`umask 077`), keep them out of version control and shell histories, and mount them read-only.

## 5.3 Kubernetes with Helm

From the root of the repository (the chart is `charts/mcp-gateway`, the examples are in `docs/tutorial/examples/`; copy the two example files and edit them first), in the directory that holds the secret files of 5.2:

```bash
kubectl create namespace gateway
kubectl -n gateway create secret generic mcp-gateway-secrets \
  --from-file=state-redis-url --from-file=state-encryption-key \
  --from-file=webhook-hmac --from-file=otlp-headers

helm install gateway charts/mcp-gateway -n gateway \
  -f docs/tutorial/examples/helm-values.yaml \
  --set-json "config=$(cat docs/tutorial/examples/config-ha.json)"
```

The Secret holds one `--from-file` per `*_file` key of your configuration, named as the key's path ends: drop `otlp-headers` with `"otlp": null`, add for example the `ca_file` of a webhook whose certificate comes from a private authority.

**Redis is yours.** The chart installs the gateway only, not the Redis that `config-ha.json` names in `state-redis-url`. Use a managed Redis, an operator or a chart you already run, with the same choices as the compose example ([its README](../../../deploy/compose/README.md#redis): one Redis, a password, no persistence needed, `maxmemory-policy noeviction`). A Redis Service `redis` in the namespace `gateway` gives `redis://:<password>@redis.gateway.svc.cluster.local:6379/0`; use `rediss://` when it serves TLS.

[`examples/helm-values.yaml`](../examples/helm-values.yaml) sets the image, two replicas, the Secret mount and an ingress. Its `image.repository` and `image.tag` are placeholders: set your mirror and a released version. The configuration goes in as `config` (the chart writes it into a Secret named by the release, key `MCP_GATEWAY_CONFIG`), here from the JSON file with `--set-json`. Alternatively point `existingSecret.name` at a Secret you manage (sealed secrets, external-secrets): the chart then cannot check the document, so you must set `existingSecret.sharedState=true` when more than one replica runs and set `terminationGracePeriodSeconds`.

The Secret named in `secretMounts` is mounted read-only, mode `0440`, group 1000, at `mountPath`; the paths in `config` are those mount paths (`/run/gateway/secrets/...` in the examples).

### The high-availability shape

| Setting | Why |
|---|---|
| `replicaCount: 2` (or more), `config.state.type: redis` | Several replicas **need** Redis: it holds the session bindings, the resolver cache and the rate-limit buckets that must agree between replicas. With `memory`, a session opened on one replica is unknown to the other (`404`, `-31007`), and a cached key invalidated on one replica stays cached on the other. The chart refuses to render more than one replica (or an HPA with `maxReplicas` above 1) without `state.type` `redis` |
| One Redis | Use one Redis (a primary with replicas is fine), not a sharded Redis Cluster: the session scripts assume one keyspace |
| The same `state.encryption_key_file` on every replica | The key names and the cache encryption derive from it (chapter 8 covers rotation) |
| Probes | `startupProbe` and `livenessProbe` call `GET /healthz`, which never looks at Redis, so a Redis outage does not restart the fleet. `readinessProbe` calls `GET /readyz`, which pings Redis: a replica that lost it leaves the Service and, meanwhile, fails closed with `503`/`-31013` |
| `terminationGracePeriodSeconds` | Default: the largest `upstreams.*.read_timeout_seconds` + 20 s + `preStopSleepSeconds` (5 s) + 5 s. With the examples' `read_timeout_seconds` of 130 that is 160 s. The render fails if you set a value not greater than that read timeout. A request that runs on after its client left is bounded by that read timeout, and a shorter grace period would kill it |
| `preStopSleepSeconds: 5` | The pod keeps serving while it leaves the Services |
| `strategy` | Rolling, `maxSurge: 1`, `maxUnavailable: 0`: a new pod is ready before an old one stops |
| PodDisruptionBudget | On by default when more than one replica can run, `maxUnavailable: 1` (`minAvailable` wins when set) |
| `topologySpreadConstraints` | Spread the replicas over nodes (the example does) |
| `securityContext` | Non-root uid 1000, read-only root filesystem, no privilege escalation, all capabilities dropped. Keep the defaults |
| `resources` | Requests 100m / 192Mi, limits 1 CPU / 512Mi. The gateway is a single-process Python server: one core is its ceiling, so scale by replicas (or the HPA) |

Open streams still running when a pod stops are closed. The client reconnects to another replica, which finds its session because the replicas share their state.

### Exposure: the proxy must not buffer

MCP answers are Server-Sent Events. Whatever sits between the internet and the gateway (ingress controller, load balancer, CDN, WAF) must **not buffer** responses and must **not cut quiet streams**. The gateway adds `X-Accel-Buffering: no` to every relayed upstream response, which nginx-based proxies honour; others need their own setting.

- `ingress`: routes `publicPath` (`/mcp`) and `/.well-known/oauth-protected-resource<publicPath>` to the Service. The second path is how clients discover your OAuth server. The example's annotations are for ingress-nginx: `proxy-buffering: "off"`, `proxy-request-buffering: "off"`, and `proxy-read-timeout` / `proxy-send-timeout` of `"3600"`, above the largest `read_timeout_seconds`.
- `httpRoute` (Gateway API): the same two paths; `httpRoute.timeouts` renders `rules[].timeouts` (`request: 0s` disables the request timeout, which a stream needs). Implementations differ in their support for it.
- The `public_url` that clients use, the token audience and the route's host must be the same address.

### Render-time checks

`helm template` and `helm install` fail with a message rather than produce a Deployment that cannot work: neither or both of `config` and `existingSecret.name`; more than one replica without Redis state; a grace period not greater than the largest read timeout; `config.listen.port` different from `containerPort`, a `listen.host` that is not `0.0.0.0` or `::`, or `publicPath` different from the path of `public_url`; `ingress` and `httpRoute` enabled together. A `config` without `version: 1` is refused by the values schema.

### Changing the configuration

A `helm upgrade` that changes `config` rolls the pods (the chart puts a checksum of the configuration Secret in the pod template). Secrets you mount, and an `existingSecret`, are not watched: roll with `kubectl -n gateway rollout restart deployment/gateway-mcp-gateway` (the Deployment of the release named `gateway` above). (The `static` resolver re-reads its mapping and key files by itself.) The full list of chart values is in [`charts/mcp-gateway/README.md`](../../../charts/mcp-gateway/README.md).

## 5.4 docker compose

[`deploy/compose/`](../../../deploy/compose/) is a production example for one host: the gateway and the Redis that holds its shared state, hardened. Its [README](../../../deploy/compose/README.md) is the reference (hardening table, Redis choices, proxy settings); this section gives the steps.

| File | What it is |
|---|---|
| `compose.yaml` | The two services (`gateway`, `redis`). Reads `.env`. |
| `.env.example` | `MCP_GATEWAY_IMAGE`, `MCP_GATEWAY_CONFIG_FILE`, `MCP_GATEWAY_SECRETS_DIR`, `MCP_GATEWAY_BIND`. Copy to `.env`. |
| `config.static.example.json`, `config.store-redis.example.json` | A gateway configuration with the `static` resolver, or with the `store_redis` resolver (chapter 4). |
| `mapping.example.json` | The static mapping that `init-secrets.sh` copies into the secrets directory. |
| `init-secrets.sh` | Creates the secrets directory: the Redis password, the state encryption key, an empty key directory. |
| `nginx.example.conf` | A TLS reverse proxy that does not break streaming. |

```bash
cd deploy/compose
cp .env.example .env                       # set MCP_GATEWAY_IMAGE to the image of 5.1
cp config.static.example.json config.json  # or config.store-redis.example.json
./init-secrets.sh                          # creates ./secrets (refuses to touch an existing one)
# edit config.json: public_url, auth (issuer, jwks_uri, audiences), upstreams
# edit secrets/gateway/keys/mapping.json and write each API key it names into secrets/gateway/keys/
docker compose up -d --wait
curl -fsS http://127.0.0.1:8000/readyz     # {"status":"ready"}
```

Neither `.env`, `config.json` nor `secrets/` belongs in version control.

- **Configuration:** compose mounts `MCP_GATEWAY_CONFIG_FILE` at `/etc/mcp-gateway/config.json`, and a one-line entrypoint loads it into `MCP_GATEWAY_CONFIG`: a missing file stops the start, and an invalid document is refused by the gateway with `startup refused: invalid MCP_GATEWAY_CONFIG: <key>: <rule>` in `docker compose logs gateway`. Every `*_file` key names a file under `/run/mcp-gateway/secrets/`, the `gateway/` directory of `MCP_GATEWAY_SECRETS_DIR`, mounted read-only.
- **Hardening:** read-only root filesystem, all capabilities dropped, `no-new-privileges`, resource limits (1 CPU and 512 MB for the gateway), `restart: unless-stopped`, log rotation. Redis sits on an `internal` network with no route out and no published port. The gateway's healthcheck calls `/readyz`, which answers `503` while Redis is unreachable, so `--wait` fails for a gateway that cannot serve.
- **Redis:** it keeps nothing across a restart on purpose (sessions, cached credentials and rate-limit buckets all expire and are rebuilt), and with `noeviction` a full Redis makes the gateway answer `503` rather than drop a session binding. For the `store_redis` resolver, use your own persistent Redis for the keys, not this one.
- **Changing the configuration:** compose mounts `config.json` and the gateway reads it once, at start; `docker compose up -d` does not restart a running gateway for a changed file. Apply a change with `docker compose restart gateway` (the same for a new `state-encryption-key` or webhook secret). The `static` resolver's mapping and key files are the exception: they are re-read without a restart.
- **TLS:** the gateway is published on `127.0.0.1:8000` over plain HTTP. Put a reverse proxy on the host name of `public_url`; it must not buffer, compress or time out the event streams, which `nginx.example.conf` does (`proxy_buffering off`, `gzip off`, `proxy_read_timeout 3600s`).
- **Identity provider:** with the Keycloak template of chapter 6, `auth.issuer` is `https://<keycloak>/realms/<realm>`; keep the example's `scopes_advertised` and `identity` as they are.
- **Image:** the examples name the release image; its public pull location is not announced yet, so until then mirror the image into your own registry and set `MCP_GATEWAY_IMAGE` to the mirror.

Other facts:

- The gateway container needs `MCP_GATEWAY_CONFIG` and the secret files of 5.2, nothing else.
- The image's own `HEALTHCHECK` is a real `GET /healthz` on the port in `MCP_GATEWAY_CONFIG`, so `docker ps` shows `healthy` once the gateway serves.
- Two or more gateway containers behind one address need `state.type` `redis` for the same reasons as on Kubernetes.
- The repository's own `compose.yaml` is the **development** stack (Keycloak, Hydra, test stores, gateways with test configurations): a test fixture, not a deployment recipe.

## 5.5 Check the deployment

With the gateway reachable at `https://mcp.example.com/mcp`:

```bash
# 1. No token: the challenge that starts every client's sign-in
curl -si --max-time 10 https://mcp.example.com/mcp -X POST -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"ping"}'
# 401 Unauthorized, body {"error":"invalid_token","error_description":"missing bearer token"}
# www-authenticate: Bearer resource_metadata="https://mcp.example.com/.well-known/oauth-protected-resource/mcp", scope="mcp-gateway offline_access"

# 2. The metadata the challenge points to (200)
curl -s --max-time 10 https://mcp.example.com/.well-known/oauth-protected-resource/mcp
# {"resource":"https://mcp.example.com/mcp","authorization_servers":["https://idp.example.com/realms/example-publisher"],"scopes_supported":["mcp-gateway","offline_access"],"bearer_methods_supported":["header"]}

```

The probes `/healthz` and `/readyz` are for the platform, not the internet: the chart's ingress routes only the MCP path and the metadata path.

Check that `resource` equals `public_url` character for character and that `authorization_servers` holds your OAuth server. Check also that the origin-level URL, `https://mcp.example.com/.well-known/oauth-protected-resource`, answers `404`: that is correct.

Then go on with [6. Your OAuth server](06-oauth-server.md).
