# 5. Déploiement

La passerelle est une seule image de conteneur. Ce chapitre couvre les deux manières prises en charge de l'exécuter : le chart Helm de `charts/mcp-gateway/` (Kubernetes) et docker compose.

## 5.1 L'image

L'image publique est **`ghcr.io/alien-intelligence/mcp-gateway`**, avec les étiquettes `X.Y.Z`, `X.Y`, `X` et `latest` (README, « Container image ») ; fixez une version, ou mieux son empreinte (digest). Vous pouvez aussi construire l'image vous-même depuis la racine du dépôt et la déposer dans votre propre registre (ou utiliser l'étiquette locale `mcp-gateway:local` sur une seule machine). Les trois arguments de construction sont obligatoires : la construction refuse de s'exécuter sans eux.

```bash
docker buildx build \
  --build-arg VERSION="$(sed -n 's/^version = "\(.*\)"$/\1/p' pyproject.toml | head -n 1)" \
  --build-arg VCS_REF="$(git rev-parse HEAD)" \
  --build-arg BUILD_DATE="$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --tag mcp-gateway:local --load .
```

- Une seule image pour `linux/amd64` et `linux/arm64`. Étiquettes : `<version>` (une version issue de `main`), `<version>-rc` (`staging`), `<version>-dev` (`dev`) et les étiquettes mobiles `latest`, `latest-staging`, `latest-dev`.
- Épinglez une version, ou mieux le condensat (digest). N'exécutez jamais `latest` en production.
- Le processus s'exécute sous l'uid 1000, n'a besoin d'aucun chemin inscriptible (l'état vit en mémoire ou dans Redis, les journaux vont sur la sortie standard) et est testé en lecture seule et sans capacités :

```bash
docker run --read-only --cap-drop ALL --security-opt no-new-privileges \
  -e MCP_GATEWAY_CONFIG="$(cat config.json)" \
  -v "$PWD/secrets:/run/secrets:ro" \
  -p 8000:8000 ghcr.io/alien-intelligence/mcp-gateway:<version>

curl http://localhost:8000/healthz   # {"status":"ok"}: the process is up
curl http://localhost:8000/readyz    # {"status":"ready"}; 503 {"status":"state_unavailable"} when the shared state cannot be reached
```

Toute la configuration est un seul document JSON dans une seule variable d'environnement, `MCP_GATEWAY_CONFIG`. (Les exportateurs OpenTelemetry lisent aussi les variables standard `OTEL_EXPORTER_OTLP_*` pour les réglages que le bloc `observability.otlp` ne porte pas, par exemple un certificat d'autorité.) Un document erroné arrête le processus au démarrage, avec `startup refused: invalid MCP_GATEWAY_CONFIG: <key>: <rule>` dans le journal ; le message nomme la clé et la règle, jamais la valeur.

## 5.2 La configuration et ses secrets

Deux exemples sont fournis, tous deux acceptés par le chargeur de la passerelle :

- [`examples/config-single.json`](../examples/config-single.json) : un réplica, resolver `static`, état en mémoire. Commencez par là.
- [`examples/config-ha.json`](../examples/config-ha.json) : deux réplicas ou plus, resolver `webhook` derrière un cache, état dans Redis, limites de débit, export OTLP.

Modifiez au minimum : `public_url`, `auth.issuer`, `auth.jwks_uri`, `auth.audiences` (il doit contenir `public_url`), `upstreams.main.url` (l'URL du serveur MCP Alien, fournie par Alien) et `policies.allowed_origins`. [`configuration.md`](../../configuration.md) documente chaque clé, avec ses bornes.

**Les secrets sont des fichiers.** La configuration ne contient jamais de secret : des clés telles que `secret_file`, `dsn_file`, `url_file`, `headers_file`, `client_key_file` et `encryption_key_file` désignent un fichier qui le contient. Voici ceux qu'utilise `config-ha.json` :

| Fichier | Contenu | Comment le produire |
|---|---|---|
| `state-redis-url` | `rediss://:<password>@<host>:6379/0` (une URL `redis://` ou `rediss://` ; elle peut porter le mot de passe) | Votre Redis |
| `state-encryption-key` | Le base64 d'exactement 32 octets aléatoires : la clé AES-256-GCM des identifiants mis en cache, et la clé dont dérivent les noms des clés Redis. **Chaque réplica qui partage un Redis doit avoir le même fichier.** | `head -c 32 /dev/urandom \| base64 > state-encryption-key` |
| `webhook-hmac` | Le secret partagé du resolver webhook. Le webhook détient le même contenu | `head -c 32 /dev/urandom \| base64 > webhook-hmac` (tout secret aléatoire convient) |
| `otlp-headers` | Des lignes `clé=valeur` envoyées avec chaque export de télémétrie | L'en-tête d'authentification de votre collecteur |
| `mapping.json` et les fichiers de clés | La correspondance du resolver `static` (chapitre 4) | Uniquement avec `static` |

Créez les fichiers avec un umask restrictif (`umask 077`), gardez-les hors du contrôle de version et des historiques de shell, et montez-les en lecture seule.

## 5.3 Kubernetes avec Helm

Depuis la racine du dépôt (le chart est `charts/mcp-gateway`, les exemples se trouvent dans `docs/tutorial/examples/` ; copiez d'abord les deux fichiers d'exemple et modifiez-les), dans le répertoire qui contient les fichiers de secrets du point 5.2 :

```bash
kubectl create namespace gateway
kubectl -n gateway create secret generic mcp-gateway-secrets \
  --from-file=state-redis-url --from-file=state-encryption-key \
  --from-file=webhook-hmac --from-file=otlp-headers

helm install gateway charts/mcp-gateway -n gateway \
  -f docs/tutorial/examples/helm-values.yaml \
  --set-json "config=$(cat docs/tutorial/examples/config-ha.json)"
```

Le Secret contient un `--from-file` par clé `*_file` de votre configuration, nommé comme se termine le chemin de la clé : retirez `otlp-headers` avec `"otlp": null`, ajoutez par exemple le `ca_file` d'un webhook dont le certificat provient d'une autorité privée.

**Redis est à vous.** Le chart n'installe que la passerelle, pas le Redis que `config-ha.json` désigne dans `state-redis-url`. Utilisez un Redis géré, un opérateur ou un chart que vous exploitez déjà, avec les mêmes choix que l'exemple compose ([son README](../../../deploy/compose/README.md#redis) : un seul Redis, un mot de passe, aucune persistance nécessaire, `maxmemory-policy noeviction`). Un Service Redis `redis` dans le namespace `gateway` donne `redis://:<password>@redis.gateway.svc.cluster.local:6379/0` ; utilisez `rediss://` lorsqu'il sert du TLS.

[`examples/helm-values.yaml`](../examples/helm-values.yaml) définit l'image, deux réplicas, le montage du Secret et un ingress. Ses `image.repository` et `image.tag` sont des valeurs provisoires : indiquez votre miroir et une version publiée. La configuration entre sous la forme `config` (le chart l'écrit dans un Secret nommé d'après la release, clé `MCP_GATEWAY_CONFIG`), ici depuis le fichier JSON avec `--set-json`. Vous pouvez aussi pointer `existingSecret.name` vers un Secret que vous gérez (sealed secrets, external-secrets) : le chart ne peut alors pas vérifier le document, vous devez donc définir `existingSecret.sharedState=true` lorsque plus d'un réplica s'exécute, et définir `terminationGracePeriodSeconds`.

Le Secret nommé dans `secretMounts` est monté en lecture seule, en mode `0440`, groupe 1000, à `mountPath` ; les chemins de `config` sont ces chemins de montage (`/run/gateway/secrets/...` dans les exemples).

### La forme haute disponibilité

| Réglage | Pourquoi |
|---|---|
| `replicaCount: 2` (ou plus), `config.state.type: redis` | Plusieurs réplicas **ont besoin** de Redis : il contient les liaisons de session, le cache du resolver et les seaux de limitation de débit, qui doivent être cohérents entre les réplicas. Avec `memory`, une session ouverte sur un réplica est inconnue de l'autre (`404`, `-31007`), et une clé en cache invalidée sur un réplica reste en cache sur l'autre. Le chart refuse de produire plus d'un réplica (ou un HPA dont `maxReplicas` dépasse 1) sans `state.type` valant `redis` |
| Un seul Redis | Utilisez un seul Redis (un primaire avec des réplicas convient), et non un Redis Cluster à partitions : les scripts de session supposent un seul espace de clés |
| Le même `state.encryption_key_file` sur chaque réplica | Les noms de clés et le chiffrement du cache en dérivent (le chapitre 8 traite de la rotation) |
| Sondes | `startupProbe` et `livenessProbe` appellent `GET /healthz`, qui ne regarde jamais Redis : une panne de Redis ne redémarre donc pas la flotte. `readinessProbe` appelle `GET /readyz`, qui interroge Redis : un réplica qui l'a perdu quitte le Service et, entre-temps, échoue en mode fermé avec `503`/`-31013` |
| `terminationGracePeriodSeconds` | Par défaut : le plus grand `upstreams.*.read_timeout_seconds` + 20 s + le vidage OTLP lorsque OTLP est activé + `preStopSleepSeconds` (5 s) + 5 s. Avec les `read_timeout_seconds` de 130 des exemples et sans OTLP, cela donne 160 s. Le rendu échoue si vous fixez une valeur qui n'est pas supérieure à ce même total sans les 5 s finales (le budget d'arrêt, chapitre 8.6). Les requêtes en cours disposent du délai de lecture pour se terminer, puis la passerelle exécute son arrêt, et un délai de grâce plus court la tuerait avant qu'elle ait terminé |
| `preStopSleepSeconds: 5` | Le pod continue de servir pendant qu'il quitte les Services |
| `strategy` | Progressive (rolling), `maxSurge: 1`, `maxUnavailable: 0` : un nouveau pod est prêt avant qu'un ancien ne s'arrête |
| PodDisruptionBudget | Activé par défaut lorsque plus d'un réplica peut s'exécuter, `maxUnavailable: 1` (`minAvailable` l'emporte lorsqu'il est défini) |
| `topologySpreadConstraints` | Répartissent les réplicas sur les nœuds (l'exemple le fait) |
| `securityContext` | uid 1000 non root, système de fichiers racine en lecture seule, pas d'élévation de privilèges, toutes les capacités retirées. Conservez les valeurs par défaut |
| `resources` | Requêtes 100m / 192Mi, limites 1 CPU / 512Mi. La passerelle est un serveur Python mono-processus : un cœur est son plafond, il faut donc monter en charge par réplicas (ou par le HPA) |

Les flux ouverts encore actifs lorsqu'un pod s'arrête sont fermés. Le client se reconnecte à un autre réplica, qui retrouve sa session parce que les réplicas partagent leur état.

### Exposition : le proxy ne doit pas mettre en tampon

Les réponses MCP sont des Server-Sent Events. Tout ce qui se trouve entre Internet et la passerelle (contrôleur d'ingress, répartiteur de charge, CDN, WAF) ne doit **pas mettre en tampon** les réponses et ne doit **pas couper les flux silencieux**. La passerelle ajoute `X-Accel-Buffering: no` à chaque réponse amont relayée, ce que respectent les proxys fondés sur nginx ; les autres demandent leur propre réglage.

- `ingress` : achemine `publicPath` (`/mcp`) et `/.well-known/oauth-protected-resource<publicPath>` vers le Service. Le second chemin est celui par lequel les clients découvrent votre serveur OAuth. Les annotations de l'exemple sont celles d'ingress-nginx : `proxy-buffering: "off"`, `proxy-request-buffering: "off"`, et `proxy-read-timeout` / `proxy-send-timeout` à `"3600"`, au-dessus du plus grand `read_timeout_seconds`.
- `httpRoute` (Gateway API) : les mêmes deux chemins ; `httpRoute.timeouts` produit `rules[].timeouts` (`request: 0s` désactive le délai de requête, dont un flux a besoin). Les implémentations diffèrent dans leur prise en charge de cette option.
- Le `public_url` qu'utilisent les clients, l'audience du jeton et l'hôte de la route doivent être la même adresse.

### Contrôles au moment du rendu

`helm template` et `helm install` échouent avec un message plutôt que de produire un Deployment qui ne peut pas fonctionner : ni `config` ni `existingSecret.name`, ou les deux à la fois ; plus d'un réplica sans état Redis ; un délai de grâce non supérieur au plus grand délai de lecture ; `config.listen.port` différent de `containerPort`, un `listen.host` qui n'est ni `0.0.0.0` ni `::`, ou `publicPath` différent du chemin de `public_url` ; `ingress` et `httpRoute` activés ensemble. Un `config` sans `version: 1` est refusé par le schéma des valeurs.

### Modifier la configuration

Un `helm upgrade` qui modifie `config` renouvelle les pods (le chart place une somme de contrôle du Secret de configuration dans le modèle de pod). Les secrets que vous montez, et un `existingSecret`, ne sont pas surveillés : relancez avec `kubectl -n gateway rollout restart deployment/gateway-mcp-gateway` (le Deployment de la release nommée `gateway` ci-dessus). (Le resolver `static` relit lui-même ses fichiers de correspondance et de clés.) La liste complète des valeurs du chart se trouve dans [`charts/mcp-gateway/README.md`](../../../charts/mcp-gateway/README.md).

## 5.4 docker compose

[`deploy/compose/`](../../../deploy/compose/) est un exemple de production pour un seul hôte : la passerelle et le Redis qui contient son état partagé, durcis. Son [README](../../../deploy/compose/README.md) fait référence (tableau de durcissement, choix de Redis, réglages du proxy) ; cette section donne les étapes.

| Fichier | Ce que c'est |
|---|---|
| `compose.yaml` | Les deux services (`gateway`, `redis`). Lit `.env`. |
| `.env.example` | `MCP_GATEWAY_IMAGE`, `MCP_GATEWAY_CONFIG_FILE`, `MCP_GATEWAY_SECRETS_DIR`, `MCP_GATEWAY_BIND`. À copier vers `.env`. |
| `config.static.example.json`, `config.store-redis.example.json` | Une configuration de passerelle avec le resolver `static`, ou avec le resolver `store_redis` (chapitre 4). |
| `mapping.example.json` | La correspondance statique que `init-secrets.sh` copie dans le répertoire des secrets. |
| `init-secrets.sh` | Crée le répertoire des secrets : le mot de passe Redis, la clé de chiffrement de l'état, un répertoire de clés vide. |
| `nginx.example.conf` | Un proxy inverse TLS qui ne casse pas le flux continu. |

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

Ni `.env`, ni `config.json`, ni `secrets/` n'ont leur place dans le contrôle de version.

- **Configuration :** compose monte `MCP_GATEWAY_CONFIG_FILE` à `/etc/mcp-gateway/config.json`, et un point d'entrée d'une ligne le charge dans `MCP_GATEWAY_CONFIG` : un fichier absent interrompt le démarrage, et un document invalide est refusé par la passerelle avec `startup refused: invalid MCP_GATEWAY_CONFIG: <key>: <rule>` dans `docker compose logs gateway`. Chaque clé `*_file` désigne un fichier sous `/run/mcp-gateway/secrets/`, le répertoire `gateway/` de `MCP_GATEWAY_SECRETS_DIR`, monté en lecture seule.
- **Durcissement :** système de fichiers racine en lecture seule, toutes les capacités retirées, `no-new-privileges`, limites de ressources (1 CPU et 512 Mo pour la passerelle), `restart: unless-stopped`, rotation des journaux. Redis se trouve sur un réseau `internal` sans route vers l'extérieur et sans port publié. Le contrôle de santé de la passerelle appelle `/readyz`, qui répond `503` tant que Redis est injoignable : `--wait` échoue donc pour une passerelle qui ne peut pas servir.
- **Redis :** il ne conserve rien d'un redémarrage à l'autre, volontairement (les sessions, les identifiants en cache et les seaux de limitation de débit expirent tous et sont reconstruits), et avec `noeviction`, un Redis plein fait répondre `503` à la passerelle plutôt que de perdre une liaison de session. Pour le resolver `store_redis`, utilisez votre propre Redis persistant pour les clés, pas celui-ci.
- **Modifier la configuration :** compose monte `config.json` et la passerelle ne le lit qu'une fois, au démarrage ; `docker compose up -d` ne redémarre pas une passerelle en cours d'exécution pour un fichier modifié. Appliquez un changement avec `docker compose restart gateway` (il en va de même pour une nouvelle `state-encryption-key` ou un nouveau secret de webhook). Les fichiers de correspondance et de clés du resolver `static` font exception : ils sont relus sans redémarrage.
- **TLS :** la passerelle est publiée sur `127.0.0.1:8000` en HTTP simple. Placez un proxy inverse sur le nom d'hôte de `public_url` ; il ne doit ni mettre en tampon, ni compresser, ni interrompre par délai les flux d'événements, ce que fait `nginx.example.conf` (`proxy_buffering off`, `gzip off`, `proxy_read_timeout 3600s`).
- **Fournisseur d'identité :** avec le modèle Keycloak du chapitre 6, `auth.issuer` vaut `https://<keycloak>/realms/<realm>` ; conservez tels quels les `scopes_advertised` et `identity` de l'exemple.
- **Image :** `MCP_GATEWAY_IMAGE` n'a pas de valeur par défaut et compose refuse de démarrer sans elle. Définissez-la à `ghcr.io/alien-intelligence/mcp-gateway:<version>`, ou construisez l'image comme en 5.1 et définissez `MCP_GATEWAY_IMAGE=mcp-gateway:local` (ou la référence de votre registre).

Autres faits :

- Le conteneur de la passerelle n'a besoin que de `MCP_GATEWAY_CONFIG` et des fichiers de secrets du point 5.2, rien d'autre.
- Le `HEALTHCHECK` propre à l'image est un véritable `GET /healthz` sur le port indiqué dans `MCP_GATEWAY_CONFIG` : `docker ps` affiche donc `healthy` dès que la passerelle sert.
- Deux conteneurs de passerelle ou plus derrière une même adresse ont besoin de `state.type` valant `redis`, pour les mêmes raisons que sur Kubernetes.
- Le `compose.yaml` du dépôt lui-même est la pile de **développement** (Keycloak, Hydra, magasins de test, passerelles avec des configurations de test) : un banc d'essai, pas une recette de déploiement.

## 5.5 Vérifier le déploiement

La passerelle étant joignable à `https://mcp.example.com/mcp` :

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

Les sondes `/healthz` et `/readyz` sont destinées à la plateforme, pas à Internet : l'ingress du chart n'achemine que le chemin MCP et le chemin des métadonnées.

Vérifiez que `resource` est égal à `public_url` caractère pour caractère et que `authorization_servers` contient votre serveur OAuth. Vérifiez aussi que l'URL au niveau de l'origine, `https://mcp.example.com/.well-known/oauth-protected-resource`, répond `404` : c'est normal.

Passez ensuite à [6. Votre serveur OAuth](06-oauth-server.md).
