# 4. Choisir un resolver

Le resolver transforme l'identité que la passerelle a vérifiée en clé d'API Alien. C'est la seule partie de la passerelle que vous adaptez à vos propres systèmes. La passerelle remet à tout resolver la même identité :

| Champ | Contenu |
|---|---|
| `subject` | L'identifiant stable de l'utilisateur, tiré du jeton |
| `email` | L'adresse e-mail, **uniquement** lorsque la revendication `email_verified` du jeton vaut `true`, sinon `null` |
| `organization` | La revendication d'organisation, ou `null` |
| `groups` | La revendication de groupes, une liste (éventuellement vide). Le modèle de realm Keycloak du chapitre 6 y met les chemins complets des groupes (`/analysts`, `/team-a/analysts`) |

Le resolver répond par une clé, le nom d'un upstream (l'une des clés de `upstreams`) et des étiquettes (labels), ou par un refus. Les étiquettes sont des chaînes copiées dans la ligne d'audit et dans les attributs de trace, et utilisées par les politiques d'outils (chapitre 8).

## Les types

| `resolver.type` | Source de la clé | À utiliser lorsque |
|---|---|---|
| `static` | Un fichier de correspondance plus un fichier de clé par entrée. Les fichiers de clé sont lus à chaque requête ; la correspondance est rechargée lorsque la date de modification ou la taille du fichier change | Quelques utilisateurs ou organisations, avec des clés conservées sous forme de fichiers ou de secrets montés |
| `webhook` | Un point de terminaison HTTPS que vous exploitez, appelé avec l'identité et authentifié par une signature HMAC ou par TLS mutuel | Vous détenez déjà la correspondance (une base clients, un CRM) et voulez décider de votre côté |
| `store_sql` | Un seul `SELECT` en lecture seule sur une table PostgreSQL que vous alimentez | La correspondance vit dans PostgreSQL |
| `store_redis` | Une seule recherche de clé dans Redis, selon un motif que vous choisissez | Nombreux utilisateurs et recherches peu coûteuses |
| `chain` | Plusieurs des types ci-dessus, dans l'ordre | Des populations différentes à des endroits différents |

N'importe lequel d'entre eux peut se placer derrière le `resolver_cache` facultatif. Chaque secret dont un resolver a besoin (fichiers de clés, DSN, URL Redis, secret HMAC, certificats clients) est un chemin vers un fichier, jamais une valeur en ligne.

## Une clé par utilisateur, ou des clés partagées

Sur Alien, une clé d'API est un utilisateur facturé et une session amont. Les conséquences :

- **Une clé par utilisateur** (recommandé) : chaque personne a sa propre ligne dans le flux de consommation, son propre quota et sa propre session amont. Révoquer une clé n'affecte qu'une seule personne.
- **Une clé partagée** (par exemple une clé par organisation, avec une entrée `static` qui fait la correspondance sur `organization`, ou un `webhook` qui renvoie la même clé à tous les membres d'une organisation) : tous les utilisateurs de cette clé partagent **un seul utilisateur facturé et une seule session amont**. Leur consommation est fusionnée en une seule ligne du flux, et vous ne pouvez ni les distinguer ni les limiter séparément du côté d'Alien. La passerelle peut en revanche les distinguer pour sa propre ligne d'audit et pour ses limites de débit, car celles-ci utilisent le sujet vérifié.

Les notes de conception du projet le disent sans détour : l'identifiant de session amont est dérivé de la clé, de sorte que les utilisateurs qui partagent une clé partagent les caches et une ligne dans le flux. Choisissez des clés par utilisateur dès que vous avez besoin d'une comptabilité par utilisateur. Le chapitre 3 montre comment les créer.

## `static`

La configuration la plus simple. Deux fichiers et un fragment de configuration.

<!-- config-fragment: resolver -->
```json
{"type": "static", "mapping_file": "/run/gateway/secrets/mapping.json"}
```

Le fichier de correspondance :

<!-- static-mapping -->
```json
{"entries": [
  {"match": {"email": "alice@example.com"}, "api_key_file": "alice.key", "upstream": "main", "labels": {"client": "example-publisher"}},
  {"match": {"subject": "3f1c0e52-0f34-4b8e-9a52-1d6b5d7c9a10"}, "api_key_file": "bob.key", "upstream": "main", "labels": {"client": "example-publisher"}}
]}
```

- `match` contient exactement l'un de `subject`, `email`, `organization`, `group`, avec une chaîne non vide. La correspondance sur l'e-mail ignore la casse.
- `api_key_file` est relatif au répertoire du fichier de correspondance, ou absolu. Chaque fichier contient une clé (`oat_...`).
- `upstream` doit être une clé de `upstreams`. `labels` est obligatoire ; utilisez `{}` pour n'en mettre aucune.
- L'ordre de correspondance est : sujet, puis e-mail, puis organisation, puis groupe. Deux entrées ayant le même critère sont refusées au chargement du fichier. Deux entrées de groupe **différentes** qui correspondent toutes deux à un même utilisateur donnent `ambiguous_mapping` ; l'absence totale de correspondance donne `no_mapping`.
- Un critère `email` exige que `auth.identity.email_verified` soit défini (différent de `null`) : sinon, la passerelle refuse de démarrer.
- Un fichier de correspondance défectueux ou absent arrête la passerelle au démarrage. Une modification ultérieure qui casse le fichier fait de chaque requête un `500` tant que le fichier n'est pas corrigé. Un fichier de clé renouvelé prend effet sans redémarrage.

## `webhook`

La passerelle envoie l'identité à votre point de terminaison et obtient une décision.

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

### La requête

`POST <url>` avec `Content-Type: application/json` et un corps compact qui ne porte que ce que la passerelle a vérifié, jamais le jeton ni ses autres revendications :

<!-- not-config -->
```json
{"subject":"3f1c0e52-0f34-4b8e-9a52-1d6b5d7c9a10","email":"alice@example.com","organization":"example-publisher","groups":["/analysts"]}
```

`email` et `organization` valent `null` lorsqu'ils sont absents. `groups` est toujours une liste.

### Authentification de la passerelle

L'une ou l'autre des méthodes suivantes :

- **`hmac`** : trois en-têtes.
  - `X-MCP-Gateway-Timestamp` : secondes Unix.
  - `X-MCP-Gateway-Nonce` : 32 caractères hexadécimaux aléatoires, nouveaux à chaque tentative (une nouvelle tentative a un nouveau nonce).
  - `X-MCP-Gateway-Signature` : `sha256=<hex>`, où le condensat est `HMAC-SHA256(secret, "<timestamp>.<nonce>.<body>")` calculé sur les octets exacts du corps. Le secret est le contenu de `secret_file`, dépouillé des espaces qui l'entourent.

  Votre point de terminaison doit comparer la signature en temps constant, rejeter un horodatage situé hors d'une fenêtre courte, et rejeter un nonce qu'il a déjà accepté dans cette fenêtre (mémorisez chaque nonce accepté jusqu'à ce que son horodatage sorte de la fenêtre, avec une mémoire bornée).
- **`mtls`** : `{"type": "mtls", "client_cert_file": "...", "client_key_file": "..."}`. La passerelle présente le certificat client et votre point de terminaison le vérifie. Une URL `https` est nécessaire. `ca_file` est le lot de certificats qui vérifie le certificat de **votre** serveur, pour les deux modes ; `null` utilise les autorités de certification du système.

### La réponse

HTTP `200` avec un corps JSON, parmi les deux suivants :

<!-- webhook-answer -->
```json
{"decision":"allow","api_key":"oat_example-key","upstream":"main","labels":{"client":"example-publisher"},"ttl_seconds":300}
```

<!-- webhook-answer -->
```json
{"decision":"deny","reason":"no_mapping"}
```

- `allow` comporte **exactement** ces clés : `api_key` (une chaîne d'ASCII imprimable sans espace), `upstream` (un nom parmi `upstreams`, ou `null` pour utiliser `default_upstream`), `labels` (un objet de chaînes, éventuellement vide) et `ttl_seconds` (un nombre supérieur à zéro, ou `null`). Le cache du resolver conserve la réponse pendant `ttl_seconds`, plafonné par `max_ttl_seconds` et par `resolver_cache.positive_ttl_seconds` : la valeur ne peut que raccourcir la durée de mise en cache.
- `deny` comporte exactement `reason`, un jeton qui correspond à `[a-z][a-z0-9_]{0,63}`. Il apparaît dans la ligne d'audit. Utilisez `no_mapping` pour « cet utilisateur n'a pas de clé » : c'est la raison qui permet à une `chain` de passer au resolver suivant.
- Tout le reste est un échec : un autre statut, une clé manquante ou en trop, un type erroné, une raison de refus qui n'est pas un tel jeton, ou un corps de plus de 64 Kio.

### Ce que voit l'utilisateur lorsque cela se passe mal

| Ce qui se passe | La passerelle | Le client reçoit |
|---|---|---|
| Votre point de terminaison répond `allow` | Transmet avec la clé | La réponse MCP |
| `deny` avec `no_mapping` | Résultat d'audit `denied`, raison `no_mapping` | HTTP `403`, erreur JSON-RPC `-31010`, pas de défi OAuth |
| `deny` avec `ambiguous_mapping` | Idem, raison `ambiguous_mapping` | HTTP `403`, `-31011` |
| `deny` avec toute autre raison | Idem, avec votre raison | HTTP `403`, `-31012` |
| Erreur de connexion, délai dépassé, ou statut `429`, `502`, `503`, `504` | Réessaie avec un backoff exponentiel et de la gigue (jitter), jusqu'à `max_attempts` et `total_timeout_seconds` | Après la dernière tentative : HTTP `503`, `Retry-After: 5`, `-31009` |
| Tout autre statut (y compris `401` pour une signature que vous avez rejetée, `404`, `500`) | Aucune nouvelle tentative | HTTP `500`, `-31005` |
| Une réponse malformée ou trop volumineuse, un nom d'`upstream` inconnu, une clé invalide | Aucune nouvelle tentative | HTTP `500`, `-31005` |
| Un certificat TLS que la passerelle ne peut pas vérifier | Aucune nouvelle tentative | HTTP `500`, `-31005` |

Le client voit toujours un message générique. La raison figure dans la ligne d'audit et les journaux de la passerelle, jamais dans la réponse faite au client. Les redirections émises par votre point de terminaison ne sont pas suivies.

### Un webhook minimal

[`examples/webhook_resolver.py`](../examples/webhook_resolver.py) est un webhook complet écrit avec la seule bibliothèque standard de Python. Il vérifie la signature et le nonce, recherche l'adresse e-mail vérifiée dans un fichier JSON, lit la clé dans un fichier de clé et répond `allow` ou `deny`. Remplacez `load_entries` par une requête sur votre propre système.

La table de correspondance [`examples/webhook-keys.json`](../examples/webhook-keys.json) :

<!-- not-config -->
```json
{
  "alice@example.com": {"api_key_file": "alice.key", "upstream": null, "labels": {"client": "example-publisher"}, "ttl_seconds": 300},
  "bob@example.com": {"api_key_file": "bob.key", "upstream": "main", "labels": {"client": "example-publisher"}, "ttl_seconds": null}
}
```

Lancez-le (il n'a aucune valeur par défaut : chaque variable est obligatoire, sauf les deux variables TLS, qui vont ensemble) :

```bash
export WEBHOOK_HMAC_SECRET_FILE=./webhook-hmac        # the same file content as the gateway's secret_file
export WEBHOOK_KEYS_FILE=./keys/webhook-keys.json      # key files sit next to it
export WEBHOOK_TIMESTAMP_WINDOW_SECONDS=30
export WEBHOOK_HOST=0.0.0.0 WEBHOOK_PORT=8443
export WEBHOOK_TLS_CERT_FILE=./tls.crt WEBHOOK_TLS_KEY_FILE=./tls.key
python webhook_resolver.py
```

Testez-le avant d'y pointer la passerelle, avec [`examples/resolve_probe.py`](../examples/resolve_probe.py), qui signe une requête exactement comme le fait la passerelle et affiche la réponse avec la clé masquée :

```bash
python resolve_probe.py --url https://keys.example.com/resolve --secret-file ./webhook-hmac --email alice@example.com
```

La sonde vérifie le certificat du webhook contre les autorités du système. Pour un certificat issu de votre propre autorité (la passerelle lui fait confiance par `ca_file`), pointez la sonde vers le même lot : `SSL_CERT_FILE=./webhook-ca.crt python resolve_probe.py ...`.

Un test unitaire exécute cet exemple contre le resolver webhook de la passerelle elle-même (un `allow`, un `deny`, un mauvais secret, une requête rejouée), de sorte qu'il reste correct.

Ce que l'exemple ne fait pas, et dont un webhook de production peut avoir besoin : le TLS mutuel (placez devant lui un proxy inverse qui vérifie les certificats clients, ou utilisez les options TLS de votre framework), un pool de connexions à la base de données, une politique de journalisation des requêtes adaptée à vos propres besoins d'audit, et une limitation de débit des signatures en échec. Ne journalisez jamais la réponse : elle contient la clé.

## `store_sql`

Un seul `SELECT` en lecture seule dans PostgreSQL.

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

- Une seule instruction `SELECT`, sans `;` à l'intérieur (un `;` final est autorisé), avec uniquement des paramètres nommés (`%(subject)s`, `%(email)s`, `%(organization)s`, au moins l'un d'eux). Un signe pour cent littéral s'écrit `%%`.
- Colonnes : `api_key` (texte), `upstream` (texte, nullable : null sélectionne `default_upstream`) et `labels` (un objet json ou jsonb de chaînes, nullable). Aucune ligne donne `no_mapping` ; plus d'une ligne donne `ambiguous_mapping`.
- Il s'exécute dans une transaction en lecture seule. Une base de données hors service au démarrage n'arrête pas la passerelle : les requêtes reçoivent `503` (`-31009`) jusqu'à son retour.

## `store_redis`

Une seule recherche de clé.

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

`key_pattern` utilise `{subject}`, `{email}` et `{organization}` et aucune autre accolade. Chaque valeur substituée est encodée en pourcentage sur ses octets UTF-8 : tout caractère autre que `A-Z a-z 0-9 _ . - ~` devient `%XX`, de sorte que `alice@example.com` est stocké sous `mcp-gateway:cred:alice%40example.com`. Écrivez vos clés de la même manière (Python : `urllib.parse.quote(value, safe="")`).

La valeur est un objet JSON comportant exactement `api_key`, `upstream` (un nom, ou `null`) et `labels` (un objet de chaînes, ou `null`) :

<!-- not-config -->
```json
{"api_key":"oat_example-key","upstream":"main","labels":{"client":"example-publisher"}}
```

```bash
redis-cli -u "$REDIS_URL" SET 'mcp-gateway:cred:alice%40example.com' \
  '{"api_key":"oat_example-key","upstream":"main","labels":{"client":"example-publisher"}}'
```

(La commande est destinée à un test : ne mettez pas de vraies clés dans l'historique d'un shell.) Une clé absente donne `no_mapping`. Une valeur qui n'est pas ce JSON donne un `500`. Une erreur de connexion ou un délai dépassé donne un `503`.

## `chain`

Plusieurs resolvers dans l'ordre, au moins deux, dont aucun n'est une chaîne. `no_mapping` renvoyé par un membre passe au suivant. **Tout autre refus et toute erreur interrompent la chaîne** et sont renvoyés tels quels, de sorte qu'une requête échoue en mode fermé (fail closed).

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

## Le cache du resolver

Sans cache, chaque requête appelle le resolver (et donc votre webhook ou votre base de données). `resolver_cache` conserve les réponses :

<!-- config-fragment: resolver_cache -->
```json
{"positive_ttl_seconds": 300, "negative_ttl_seconds": 15, "max_entries": 10000}
```

- `positive_ttl_seconds` : durée de conservation d'une réponse qui porte une clé. Le `ttl_seconds` d'un webhook ne peut que la raccourcir.
- `negative_ttl_seconds` : durée de conservation d'un refus. Gardez-la courte : un utilisateur que vous ajoutez à la correspondance maintenant reste refusé pendant cette durée au plus.
- La clé de cache est l'identité entière (sujet, e-mail, organisation, groupes triés). Les erreurs ne sont jamais mises en cache.
- Les échecs de cache concurrents pour une même identité partagent un seul appel au système d'appui, par processus : deux réplicas qui échouent en même temps interrogent chacun le système d'appui.
- Lorsqu'Alien répond `401` ou `402` pour une clé en cache, l'entrée est supprimée afin que la requête suivante interroge de nouveau le resolver.
- `max_entries` est un nombre avec `state.type` valant `memory`, et `null` avec `redis`, où les entrées n'expirent que par leur TTL (la configuration est refusée dans le cas contraire).

L'effet du TTL sur l'exploitation : une clé que vous modifiez à la source n'atteint la passerelle qu'après l'expiration de l'entrée en cache (chapitre 8, rotation des clés).

Avec `state.type` valant `redis` et plusieurs réplicas, utilisez cette forme (avec le bloc d'état du chapitre 5) :

<!-- config-fragment: resolver_cache base=ha -->
```json
{"positive_ttl_seconds": 300, "negative_ttl_seconds": 15, "max_entries": null}
```

## Choisir

| Situation | Resolver |
|---|---|
| Jusqu'à quelques dizaines d'utilisateurs, vous pouvez écrire un fichier | `static`, une clé par utilisateur, sans cache |
| Vous avez une base clients ou un CRM | `webhook`, derrière un cache de quelques minutes, `ttl_seconds` fixé par vous |
| La correspondance vit déjà dans PostgreSQL | `store_sql` derrière un cache |
| Grand nombre d'utilisateurs, recherches simples | `store_redis` |
| Le personnel dans un fichier, tous les autres dans une base de données | `chain` de `static` puis `store_sql` |

Suite : [5. Déploiement](05-deploying.md).
