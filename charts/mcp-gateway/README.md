# mcp-gateway Helm chart

Deploys the Alien MCP Gateway: an OAuth resource server and streaming reverse proxy for MCP. One Deployment (the replica pair of the dev stack, generalised), a Service, and optionally an HPA, a PodDisruptionBudget, an Ingress or a Gateway API HTTPRoute.

The gateway reads one strict JSON document from `MCP_GATEWAY_CONFIG` ([`docs/configuration.md`](../../docs/configuration.md)). It holds no secret inline: every secret is a file named by a `*_file` key. So the chart takes the document in one Secret and mounts the other Secrets as files.

## Install

```bash
helm install gateway charts/mcp-gateway -f my-values.yaml
```

A minimal two-replica `my-values.yaml` (the configuration keys are all required, see the complete example in the configuration reference; `ci/ha-redis-values.yaml` is a full, valid one):

```yaml
image:
  repository: registry.example.com/mcp-gateway   # your mirror; the default is the release pipeline's registry
  tag: "<version>"   # a released version; the default is the chart appVersion
replicaCount: 2
config:            # the MCP_GATEWAY_CONFIG document, as YAML
  version: 1       # required: the configuration schema version (the only one supported)
  public_url: https://mcp.example.com/mcp
  listen: {host: 0.0.0.0, port: 8000}
  # ... auth, resolver, upstreams, limits, audit, observability, sessions, policies ...
  state:
    type: redis
    url_file: /run/gateway/secrets/state-redis-url
    key_prefix: "mcp-gateway:state:"
    connect_timeout_seconds: 2
    operation_timeout_seconds: 2
    max_connections: 32
    encryption_key_file: /run/gateway/secrets/state-encryption-key
secretMounts:
  - name: secrets
    secretName: mcp-gateway-secrets      # created by you: state-redis-url, state-encryption-key, ...
    mountPath: /run/gateway/secrets
```

## How the secrets work

- The configuration document goes into a Secret holding the key `MCP_GATEWAY_CONFIG`. Either the chart renders it from `config` (an object, written to JSON), or you point `existingSecret.name` at a Secret you manage.
- The files a configuration names (the Redis URL and encryption key of `state`, the `static` resolver's mapping and key files, a webhook HMAC secret, database DSNs, an OTLP headers file) live in Secrets you create, mounted read-only by `secretMounts` (mode 0440, group 1000). The paths in `config` are the mount paths.
- The gateway's settings all come from `MCP_GATEWAY_CONFIG`; its OpenTelemetry exporters also read the standard `OTEL_EXPORTER_OTLP_*` variables for what the configuration's `otlp` block does not carry (configuration reference, observability). `secretEnv` exists for variables that must carry a secret and takes references only (`valueFrom.secretKeyRef`); the Deployment never holds a secret value.
- The configuration Secret carries a checksum in the pod template when the chart renders it, so a `helm upgrade` that changes `config` rolls the pods. The mounted Secrets and an `existingSecret` are not watched: roll with `kubectl rollout restart` (the `static` resolver re-reads its mapping file by itself).

## Render-time checks

`helm template` and `helm install` fail with a message instead of producing a Deployment that cannot work:

| Check | Why |
|---|---|
| Neither or both of `config` and `existingSecret.name` | One source of configuration. |
| More than one replica (`replicaCount` > 1, or `autoscaling.enabled` with `maxReplicas` > 1) and `config.state.type` is not `redis` | A per-process state gives every replica its own sessions, resolver cache and rate limit buckets. With `existingSecret` the chart cannot read the document: you confirm with `existingSecret.sharedState=true`. |
| `terminationGracePeriodSeconds` not greater than the largest `upstreams.*.read_timeout_seconds` | A request that runs on after its client left is bounded by that read timeout (see "Client disconnects" in the configuration reference); the kubelet would kill it. With `existingSecret` the value is required. |
| `config.listen.port` differs from `containerPort`, `config.listen.host` is not `0.0.0.0` or `::`, `publicPath` differs from the path of `config.public_url` | The probes, the Service and the routes depend on them. |
| `ingress.enabled` and `httpRoute.enabled` together | One way in. |

All reads of `config` are guarded, so `helm lint` reports the message and never a nil-pointer panic.

## Shutdown

`terminationGracePeriodSeconds` defaults to the largest `read_timeout_seconds` + 20 s (the detached-read deadlines: 10 s to finish, 10 s to close once cancelled) + `preStopSleepSeconds` (5 s: the pod keeps serving while it leaves the Services) + 5 s. With the default upstream `read_timeout_seconds` of 130 that is 160 s. Open streams still running when the process stops are closed; the client reconnects to another replica, which finds its session because the replicas share their state.

## Probes

- `startupProbe`: `GET /healthz` until the process is up.
- `livenessProbe`: `GET /healthz`. It never looks at Redis, so a Redis outage does not restart the fleet.
- `readinessProbe`: `GET /readyz`, which pings the shared state. A replica that lost Redis leaves the Service (and fails closed with `503` meanwhile).

## Exposure

Both are off by default and need `hosts` / `hostnames` set.

- `ingress`: routes `publicPath` and `/.well-known/oauth-protected-resource<publicPath>` (clients fetch the second to discover the authorization server) to the Service. MCP responses are Server-Sent Events: the proxy must not buffer them and must not cut a quiet stream. The commented annotations in `values.yaml` are for ingress-nginx (`proxy-buffering: "off"`, `proxy-read-timeout`); other controllers have their own.
- `httpRoute`: the same two paths; `httpRoute.timeouts` renders `rules[].timeouts` (`request: 0s` disables the request timeout, which a stream needs). Implementations differ in support for it.

The `public_url` clients use, the token audience and the route host must be the same address.

## Values

| Key | Default | Meaning |
|---|---|---|
| `replicaCount` | `1` | Replicas (ignored while `autoscaling.enabled`). Above 1 needs Redis state. |
| `image.repository`, `image.tag`, `image.digest`, `image.pullPolicy` | none (required), chart `appVersion`, none, `IfNotPresent` | The image. `repository` has no default: the pull location of the release image is announced in the project README; until then build it from the Dockerfile (README, "Container image") and push it to your registry. With `digest` (`sha256:...`) set the Deployment pulls `repository@digest`, whatever `tag` is. |
| `imagePullSecrets` | `[]` | Pull secrets. |
| `nameOverride`, `fullnameOverride` | `""` | Naming. |
| `config` | `{}` | The `MCP_GATEWAY_CONFIG` document as an object. |
| `existingSecret.name`, `.key`, `.sharedState` | `""`, `MCP_GATEWAY_CONFIG`, `false` | Use a Secret you manage instead of `config`. |
| `secretMounts` | `[]` | Secrets mounted as directories: `name`, `secretName`, `mountPath`, optional `defaultMode` (default 0440) and `items`. |
| `secretEnv` | `[]` | Environment variables from a Secret key: `name`, `secretName`, `key`. |
| `env` | `[]` | Plain environment variables. |
| `containerPort` | `8000` | Must equal `config.listen.port`. |
| `publicPath` | `/mcp` | Must equal the path of `config.public_url`. |
| `service.type`, `.port`, `.nodePort`, `.annotations` | `ClusterIP`, `80`, unset, `{}` | The Service (`nodePort` for `NodePort` and `LoadBalancer`). |
| `serviceAccount.create`, `.name`, `.annotations`, `.automountServiceAccountToken` | `true`, `""`, `{}`, `false` | The gateway never calls the Kubernetes API. |
| `terminationGracePeriodSeconds` | `null` | Computed when null (see Shutdown). |
| `preStopSleepSeconds` | `5` | Pause before the process gets SIGTERM. |
| `podAnnotations`, `podLabels` | `{}` | Added to the pods. |
| `podSecurityContext` | non-root uid/gid/fsGroup 1000, seccomp `RuntimeDefault` | |
| `securityContext` | non-root uid 1000, read-only root filesystem, no privilege escalation, drop `ALL`, seccomp `RuntimeDefault` | |
| `resources` | requests 100m / 192Mi, limits 1 / 512Mi | A single-process Python server: one core is its ceiling. |
| `startupProbe`, `readinessProbe`, `livenessProbe` | see `values.yaml` | Timing of the probes (the paths are fixed). |
| `strategy` | rolling, `maxSurge: 1`, `maxUnavailable: 0` | A new pod is ready before an old one goes. |
| `autoscaling.enabled`, `.minReplicas`, `.maxReplicas`, `.targetCPUUtilizationPercentage`, `.targetMemoryUtilizationPercentage` | `false`, `2`, `5`, `70`, unset | HPA (`autoscaling/v2`). `maxReplicas` above 1 needs Redis state. |
| `podDisruptionBudget.enabled`, `.maxUnavailable`, `.minAvailable` | unset (on when more than one replica can run), `1`, unset | `minAvailable` wins when set. |
| `nodeSelector`, `tolerations`, `affinity`, `topologySpreadConstraints` | empty | Scheduling. |
| `ingress.*` | off | `enabled`, `className`, `annotations`, `hosts[].host`, `tls`. |
| `httpRoute.*` | off | `enabled`, `annotations`, `parentRefs`, `hostnames`, `timeouts`. |

`values.schema.json` rejects unknown keys and wrong types, and validates a non-empty `config` against the gateway's own JSON Schema (`docs/config.schema.v1.json`, embedded by `scripts/gen_config_schema.py`), so `helm install` refuses a misspelled key, a missing field or a wrong type. What a schema cannot express (the cross-field rules, files that must exist) is checked by the gateway itself at start (`startup refused: invalid MCP_GATEWAY_CONFIG: <path>: <rule>` in the pod log).

## Try it on kind

`dev/kind/install.sh` installs the chart on a throwaway kind cluster with what it needs around it: Redis, Keycloak with the dev realm (`dev/keycloak/`), the Secrets (staged as files, never put in an argument list), the image built from this checkout, and two gateway replicas sharing their state in Redis (`dev/kind/gateway_values.py` writes the values: the dev stack's `gateway-keycloak` configuration). `dev/kind/kind-config.yaml` publishes Keycloak on `localhost:38080` and the gateway on `localhost:38000`: token issuer, audience and the URLs the pods and the host use are the same strings, and the gateway pods only reach Keycloak by its in-cluster Service (`auth.jwks_uri`). The real-dev suite then runs from the host against `http://localhost:38000/mcp` (see "`e2e`: the real-dev suite" in the repository README for `E2E_GATEWAY_URL_<NAME>`).

## Checks

```bash
sh scripts/chart_check.sh     # helm lint, helm template | kubeconform -strict, and the render failures asserted
```

The files in `ci/` are the values the check renders; the ones named `ha-no-redis`, `hpa-no-redis`, `grace-too-short` and `existing-secret-no-shared-state` must fail.
