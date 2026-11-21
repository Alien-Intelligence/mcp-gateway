# Production compose example

One gateway and the Redis that holds its shared state, hardened, for a single host. It is separate from the development stack (`compose.yaml` at the repository root, which builds from source and runs test identity providers). For a cluster, use the Helm chart in [`../../charts/`](../../charts/).

| File | What it is |
|---|---|
| `compose.yaml` | The two services. Reads `.env`. |
| `.env.example` | Image, configuration file, secrets directory, published address. Copy to `.env`. |
| `config.static.example.json` | A gateway configuration with the `static` resolver (a mapping file and one file per API key). |
| `config.store-redis.example.json` | The same with the `store_redis` resolver (keys looked up in a Redis you fill). |
| `mapping.example.json` | The static mapping that `init-secrets.sh` copies into the secrets directory. |
| `init-secrets.sh` | Creates the secrets directory: Redis password, state encryption key, key directory. |
| `nginx.example.conf` | TLS reverse proxy that does not break streaming. |

Both configurations are checked by the gateway's own loader in the unit tests, so they stay valid as the configuration schema evolves. Every key is described in [`docs/configuration.md`](../../docs/configuration.md).

## Start

```sh
cd deploy/compose
cp .env.example .env                                  # set the image tag
cp config.static.example.json config.json             # or config.store-redis.example.json
./init-secrets.sh                                     # creates ./secrets
# edit config.json: public_url, auth (issuer, jwks_uri, audiences), upstreams
# edit secrets/gateway/keys/mapping.json, write each API key it names into secrets/gateway/keys/
docker compose up -d --wait
curl -fsS http://127.0.0.1:8000/readyz                # {"status":"ready"}
```

Neither `.env`, `config.json` nor `secrets/` belongs in version control.

The image is the release image, `rg.fr-par.scw.cloud/ns-data-streaming/mcp-gateway:<version>`. Its public pull location is not announced yet; until it is, mirror the image into your own registry and set `MCP_GATEWAY_IMAGE` to the mirror.

## The configuration

The gateway reads its whole configuration from one variable, `MCP_GATEWAY_CONFIG`. Compose mounts `MCP_GATEWAY_CONFIG_FILE` at `/etc/mcp-gateway/config.json` and a one-line shell entrypoint loads it into that variable: a missing file stops the start, and an empty or invalid document is refused by the gateway with `startup refused: invalid MCP_GATEWAY_CONFIG: <key>: <rule>` in `docker compose logs gateway`.

What to change in an example:

- `public_url` and `auth.audiences`: the URL clients use, behind your reverse proxy (`https://mcp.example.com/mcp`).
- `auth.issuer` and `auth.jwks_uri`: your identity provider. With the [Keycloak realm template](../keycloak/), `https://<keycloak>/realms/<realm>` and `<issuer>/protocol/openid-connect/certs`; keep `scopes_advertised` and `identity` as they are, the template's README explains them.
- `upstreams`: the Alien MCP servers, by name. The resolver's answer (`upstream` in the mapping, `default_upstream` for `store_redis`) must name one of them.
- `policies`: `allowed_origins` stays empty unless a browser application calls the gateway directly; `rate_limits.per_subject` is per user.

The gateway reads the file once, at start. `docker compose up -d` does not restart a running gateway when only `config.json` changed: apply a change with `docker compose restart gateway`. The same holds for the secret files, except the static mapping and its key files, which the gateway re-reads by itself.

Secrets are never in the configuration. Every `*_file` key names a file under `/run/mcp-gateway/secrets/`, which is the `gateway/` directory of `MCP_GATEWAY_SECRETS_DIR`, mounted read-only.

### `static` resolver

`resolver.mapping_file` is `/run/mcp-gateway/secrets/keys/mapping.json`. Each entry matches a verified e-mail, an organization, a group or a subject, and names a key file relative to the mapping. A key file is read on every request, so replacing it rotates the key without a restart. `resolver_cache` is `null`: reading a local file needs no cache.

### `store_redis` resolver

The keys live in a Redis that you fill and keep (`mcp-gateway:credential:<e-mail, percent-encoded>` holding `{"api_key": ..., "upstream": null, "labels": {...}}`, see [`store_redis`](../../docs/configuration.md#store_redis)). Its URL, with its password, goes in `secrets/gateway/credentials-redis-url`. Use your own persistent Redis for it, not the state Redis of this file, which keeps nothing across a restart. The cache (`resolver_cache`, 300 s) saves a lookup per request; an upstream `401` drops the cached key at once.

## Hardening

| Setting | Gateway | Redis |
|---|---|---|
| `read_only: true` | The image writes nothing to disk. | A 16 MB `tmpfs` on `/data`, its working directory. |
| `cap_drop: [ALL]`, `no-new-privileges` | Runs as uid 1000, needs no capability. | Runs as its own uid 999 directly, so its entrypoint has nothing to change user for. |
| `restart: unless-stopped` | Comes back after a crash or a reboot of the daemon. | Same. |
| Resource limits | 1 CPU, 512 MB. Two replicas under 20 concurrent clients measured below 75 MB each; raise for your load. | 0.5 CPU, 320 MB for a 256 MB `maxmemory`. |
| Health | `/readyz`: `503` when Redis is unreachable, so `--wait` and your monitoring see a gateway that cannot serve. | `PING` with the password. |
| Network | `edge` (published port, identity provider, upstreams) and `state`. | `state` only, an `internal` network: no route out, no published port. |
| Logs | JSON lines on stdout, one audit line per request; rotated at 5 × 50 MB. | Same rotation. |

`stop_grace_period` is 160 s, above the stop budget of the example configs (largest upstream `read_timeout_seconds` 130 s + 20 s for detached reads + a margin; see "Shutdown" in `docs/configuration.md`). On SIGTERM the gateway closes its listen streams at once, lets requests in flight finish for that time, then runs its shutdown. Raise it with any `read_timeout_seconds` you raise.

Secrets: `init-secrets.sh` makes the directory `0700` and the files in it `0644`. The containers run as uids 1000 and 999, not as your account, so they need to read the files; a bind mount does not go through the parent directory, so other accounts of the host stay out. Redis gets its password as a compose secret file (`requirepass` in `redis.conf`), never on its command line or in its environment.

## Redis

The state Redis holds session bindings, cached credentials (AES-256-GCM, under the key in `state-encryption-key`) and rate limit buckets. All of it expires on its own and is rebuilt by the gateway, so it is not persisted (`--save ""`, `--appendonly no`): after a Redis restart, MCP sessions are unknown (`404`, clients start a new one), the cache refills from the resolver, and rate limit buckets start full. Persisting it would write encrypted credentials to disk for no gain.

`maxmemory-policy noeviction`: when Redis is full, writes fail and the gateway answers `503` (fail closed) rather than Redis silently dropping a session binding or a rate limit bucket. Size `maxmemory` to your users: each entry is small, and every one has a TTL.

One replica with `state.type` `redis` is the starting point because adding a second replica then changes nothing in the configuration. With one replica only, `{"type": "memory"}` works too (no Redis service), but then `sessions.max_entries` (and `resolver_cache.max_entries` when the cache is on) must be numbers.

## TLS and the reverse proxy

The gateway speaks plain HTTP and is published on `127.0.0.1:8000`. Put a TLS reverse proxy in front, on the host name of `public_url`. MCP responses are Server-Sent Events streams: the proxy must not buffer them, compress them or cut them on a read timeout. [`nginx.example.conf`](nginx.example.conf) does that:

- `proxy_buffering off` and `proxy_request_buffering off`: each event goes to the client when it arrives.
- `gzip off`: compression buffers.
- `proxy_read_timeout 3600s`: longer than any silence on a stream (the gateway itself ends an upstream silent for `read_timeout_seconds`, 130 s in the examples).
- `proxy_http_version 1.1` with an empty `Connection` header: keep-alive to the gateway.

Other proxies need the same: Caddy (`flush_interval -1`), Traefik and HAProxy (raise their idle and read timeouts; no response buffering or compression middleware on this route). A load balancer in front needs an idle timeout above the same bound.
