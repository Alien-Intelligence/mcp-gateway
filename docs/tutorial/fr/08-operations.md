# 8. Exploitation

## 8.1 Renouveler une clé Alien

Les clés Alien sont émises et révoquées par l'API (chapitre 3). La passerelle ne détient une clé que le temps où son resolver et son cache la détiennent : effectuez donc la rotation dans cet ordre :

1. **Émettez** une nouvelle clé pour le même utilisateur (`POST /organizations/{org}/users/{user}/tokens`). L'ancienne continue de fonctionner : un utilisateur peut en détenir plusieurs.
2. **Mettez à jour la source** lue par le resolver : le fichier de clé d'une entrée `static`, la ligne d'une table `store_sql`, la valeur d'une clé `store_redis`, ou ce que renvoie votre webhook. Un fichier de clé `static` est lu à chaque requête : rien n'a donc besoin d'être redémarré.
3. **Attendez le cache.** Avec un `resolver_cache`, la passerelle continue de répondre avec l'ancienne clé jusqu'à l'expiration de l'entrée : au plus `positive_ttl_seconds` (le `ttl_seconds` d'un webhook peut le raccourcir). Sans cache, la nouvelle clé est utilisée immédiatement. Chaque réplica a ses propres entrées (ou les partage dans Redis) : attendez le TTL complet.
4. **Révoquez** l'ancienne clé (`DELETE /organizations/{org}/users/{user}/tokens/{identifier}`).

Si vous révoquez avant l'expiration du cache, la requête suivante qui passe par un réplica détenant encore l'ancienne clé est refusée par Alien : le client reçoit une erreur JSON-RPC `-31001` (HTTP `200` pour une requête qui attend une réponse), et la passerelle **supprime cette entrée du cache** afin que la requête suivante résolve la nouvelle clé. Une requête en échec par utilisateur en cache est le pire cas ; attendre l'évite. Une réponse `402` d'Alien reçoit le même traitement (`-31002`).

La même procédure renouvelle une clé compromise, sans l'attente : révoquez, acceptez le `-31001` que l'utilisateur voit jusqu'à ce que l'entrée soit supprimée, et mettez à jour la source.

## 8.2 Renouveler les secrets de la passerelle

### La clé de chiffrement de l'état (`state.encryption_key_file`)

Ne concerne que `state.type` valant `redis`. Un fichier contient le base64 de 32 octets aléatoires ; les noms de clés dans Redis et le chiffrement des identifiants en cache en dérivent tous deux : changer la clé change donc les noms sous lesquels la passerelle cherche. Voici exactement ce qui se passe, d'après le code de l'état et la référence de configuration :

| Quoi | Effet d'une nouvelle clé |
|---|---|
| Identifiants en cache (`resolver_cache`) | La nouvelle génération cherche sous d'autres noms et ne trouve rien : son cache démarre **vide** (le système d'appui des identifiants est interrogé une fois par identité). Les anciennes entrées expirent par leur TTL. Une valeur qui porte un autre identifiant de clé est comptée comme un échec de cache et laissée telle quelle |
| Liaisons de session | Une session ouverte par un réplica sur l'ancienne clé est inconnue d'un réplica sur la nouvelle clé : le client reçoit `404`, JSON-RPC `-31007` (résultat d'audit `session_not_found`) et démarre une nouvelle session. Les sessions auprès de l'upstream Alien ne sont pas touchées ; le client en ouvre de nouvelles |
| Seaux de limitation de débit | Indexés de la même manière : les seaux **repartent pleins** |
| Requêtes en cours et tout le reste | Aucun réplica ne fait échouer de requêtes à cause de l'autre génération : il n'y a pas d'interruption de service |

Pendant une mise à jour progressive, les deux générations ne voient simplement pas les entrées l'une de l'autre. Effectuez la rotation dans une fenêtre calme, ou acceptez ces effets. Avec `state.type` valant `memory`, le fichier n'est pas utilisé. Chaque réplica qui partage un Redis doit finir sur le même fichier.

Pour effectuer la rotation : écrivez le nouveau fichier dans le Secret monté, puis `kubectl rollout restart` (les Secrets montés ne sont pas surveillés). Avec l'exemple compose, remplacez `secrets/gateway/state-encryption-key` et exécutez `docker compose restart gateway`.

### Le secret HMAC du webhook et le certificat client

La passerelle lit une seule fois, à son démarrage, le `secret_file` du `auth` d'un resolver `webhook`, et signe avec ce seul secret : **il n'y a aucun moyen de lui en donner deux**. Un nouveau secret nécessite donc un redémarrage de la passerelle, et le webhook doit accepter les deux secrets pendant la durée du déploiement progressif :

1. Faites en sorte que votre webhook accepte l'ancien et le nouveau secret (l'exemple [`webhook_resolver.py`](../examples/webhook_resolver.py) n'en accepte qu'un : étendez `signature_is_valid` pour essayer les deux).
2. Placez le nouveau secret dans le fichier de secret de la passerelle et renouvelez les pods.
3. Lorsque tous les pods exécutent le nouveau, retirez l'ancien secret du webhook.

Un certificat client `mtls` et sa clé sont chargés lors de la construction du resolver : ils suivent donc la même règle, un renouvellement des pods est nécessaire.

### Autres fichiers

Le resolver `static` relit son fichier de correspondance (lorsque sa date de modification ou sa taille change) et ses fichiers de clés (à chaque requête). Pour les autres clés `*_file` (DSN de base de données, URL Redis, en-têtes OTLP), prévoyez un renouvellement des pods après une modification ; ce tutoriel n'a pas testé leur rechargement à chaud.

## 8.3 Observabilité

### La ligne d'audit

Avec `audit.enabled`, chaque requête écrit une ligne JSON sur la sortie standard, quelle que soit la manière dont elle se termine. Elle contient des identifiants, la méthode et le nom de l'outil, le résultat et la latence, jamais de contenu, d'arguments, de jetons, de clés ni d'identifiants de session.

<!-- not-config -->
```json
{"ts":"2026-11-20T09:14:03.512+00:00","event":"mcp_request","request_id":"6f1c2b0e-5a47-4d8e-9d52-3b0a4c9e7f10","trace_id":"4bf92f3577b34da6a3ce929d0e0e4736","subject":"user-1","organization":"example-org","client_id":"claude-code","labels":{"client":"example"},"http_method":"POST","rpc_method":"tools/call","rpc_id":7,"tool":"search","status":200,"outcome":"forwarded","reason":null,"upstream":"main","latency_ms":182.4,"upstream_status":200,"upstream_request_id":"req-81c4","upstream_session":false}
```

Champs : `ts`, `event` (toujours `mcp_request`), `request_id`, `trace_id`, `subject`, `organization`, `client_id`, `labels`, `http_method`, `rpc_method`, `rpc_id`, `tool`, `status`, `outcome`, `reason`, `upstream`, `latency_ms`, `upstream_status`, `upstream_request_id`, `upstream_session`. Les valeurs textuelles sont tronquées à 256 caractères.

`request_id` est aussi renvoyé au client dans l'en-tête `x-request-id` de chaque réponse que la passerelle produit ou relaie : lorsqu'un utilisateur signale un problème, demandez-lui cet en-tête et cherchez la ligne d'audit. Un `x-request-id` envoyé par le client est ignoré.

Le `outcome` est l'une des valeurs suivantes : `origin_refused`, `unauthenticated`, `insufficient_scope`, `idp_unavailable`, `userinfo_unavailable`, `request_body_timeout`, `body_too_large`, `mcp_header_mismatch`, `duplicate_header`, `resolver_unavailable`, `state_unavailable`, `gateway_error`, `denied`, `rate_limited`, `session_not_found`, `upstream_error`, `upstream_auth_rejected`, `upstream_stream_failed`, `tool_denied`, `batch_refused`, `unparsable_request`, `invalid_request`, `tool_policy_failed`, `forwarded`, `client_disconnected`, `internal_error`. `reason` contient la raison lisible par machine lorsqu'il y en a une : la raison de refus de votre resolver, la classe d'échec d'un flux interrompu, le seau (`subject` ou `organization`) qui a refusé une requête limitée en débit, ou, avec `auth.userinfo`, `userinfo_rejected`, `userinfo_subject_mismatch`, `userinfo_claims` ou `userinfo_invalid`.

Les journaux du processus lui-même (démarrage, erreurs du resolver et du JWKS) sont aussi des lignes JSON sur la sortie standard, avec `ts`, `level`, `logger` et `message`.

### Traces et métriques

`observability.otlp` exporte les traces et les métriques par OTLP. `{"otlp": null}` n'exporte rien. Chaque requête est une nouvelle trace racine comportant trois spans, et un quatrième avec `auth.userinfo` :

| Span | Quoi |
|---|---|
| `mcp-gateway request` | La requête entière, jusqu'à la fin de la réponse |
| `mcp-gateway resolve_credential` | L'étape du resolver ; attributs `mcp_gateway.resolver.type`, `mcp_gateway.resolver.cache` (`hit`, `miss`, `negative`, `none`) et `mcp_gateway.resolver.result` |
| `mcp-gateway userinfo` | Avec `auth.userinfo` uniquement : la recherche userinfo ; attributs `mcp_gateway.userinfo.cache` (`hit`, `miss`, `none`) et `mcp_gateway.userinfo.result` (`profile`, `rejected`, `subject_mismatch`, `claims_invalid`, `unavailable`, `invalid`, `state_unavailable`, `disconnected`, `aborted`) |
| `mcp-gateway upstream` | L'appel à Alien, jusqu'aux en-têtes de la réponse |

Métriques :

| Instrument | Type | Usage |
|---|---|---|
| `mcp_gateway.requests` | compteur | Requêtes par `outcome`, `status_class`, `rpc_method`, `upstream`. Alertez sur une part croissante de résultats autres que `forwarded` |
| `mcp_gateway.request.duration` | histogramme, secondes | Latence des requêtes, mêmes attributs |
| `mcp_gateway.upstream.duration` | histogramme, secondes | Délai jusqu'aux en-têtes de la réponse d'Alien |
| `mcp_gateway.resolver.lookups` | compteur | Appels au resolver par `resolver_type`, `cache`, `result`. Le taux de succès du cache et le taux d'échec de votre webhook |
| `mcp_gateway.userinfo.lookups` | compteur | Avec `auth.userinfo` uniquement : recherches par `cache` et `result`. Le taux de succès du cache, et une hausse de `unavailable` ou `invalid` quand le fournisseur d'identité défaille |
| `mcp_gateway.upstream.detached` | compteur | Requêtes qui se sont poursuivies après le départ de leur client, par `result` (section 8.5) |
| `mcp_gateway.upstream.detached.active` | jauge | Celles qui sont en cours |
| `mcp_gateway.sessions.bound` | jauge | Sessions dans ce processus (avec `state.type` valant `memory` uniquement) |
| `mcp_gateway.telemetry.failures` | compteur | Échecs de la télémétrie elle-même, par `signal` (`record`, `traces`, `metrics`). **Alertez sur une valeur croissante** : un collecteur hors service n'est sinon visible que dans les journaux |

Les spans et les métriques ne portent jamais le sujet, l'adresse e-mail, un jeton, une clé, un identifiant de session, des arguments ni du contenu. Les étiquettes du resolver ne sont que des attributs de span (`mcp_gateway.label.<key>`) ; les noms d'outils ne sont que des attributs de span, jamais des attributs de métrique. La télémétrie est au mieux de l'effort : un échec d'enregistrement ou d'export est compté et ne modifie jamais une réponse. Le contexte de trace n'est pas propagé à Alien, et un `traceparent` entrant devient un lien de span, jamais le parent.

### Sondes

`GET /healthz` (vivacité, ne regarde jamais Redis) et `GET /readyz` (disponibilité ; avec `state.type` valant `redis`, il interroge Redis et répond `503` `{"status":"state_unavailable"}` lorsqu'il ne le peut pas).

## 8.4 Limites de débit et politiques d'outils

### Limites de débit

Des seaux à jetons par sujet et, facultativement, par organisation. Chaque requête authentifiée prélève un jeton dans chaque seau qui s'applique ; une requête sans jeton valide n'en prélève aucun. Le contrôle s'exécute avant la recherche d'identifiants : un déluge de requêtes n'atteint donc jamais votre resolver ni Alien.

<!-- config-fragment: policies.rate_limits -->
```json
{
  "per_subject": {"burst": 30, "per_minute": 60},
  "per_organization": {"burst": 200, "per_minute": 600}
}
```

`burst` est la taille du seau (et son niveau à la création) ; `per_minute` est le rythme de remplissage. Avec `{"burst": 30, "per_minute": 60}`, un nouveau sujet peut envoyer 30 requêtes d'un coup, puis une par seconde. `per_subject` est obligatoire lorsque la fonctionnalité est activée (`"rate_limits": null` la désactive). `per_organization` nécessite `auth.identity.organization`.

Une requête refusée reçoit une réponse sous la forme que son émetteur peut lire, toujours avec `Retry-After` : un `tools/call` reçoit un HTTP `200` et un résultat avec `isError: true` et `_meta["alien.club/retryAfterSeconds"]` ; une autre requête portant un identifiant reçoit un HTTP `200` et `-31014` ; une requête à laquelle on ne peut pas répondre (une notification, `GET`, `DELETE`) reçoit un HTTP `429`. Le résultat d'audit est `rate_limited`, `reason` valant `subject` ou `organization`. Avec `auth.userinfo`, l'organisation n'est connue qu'après l'appel userinfo : le seau du sujet est donc prélevé en premier et celui de l'organisation ensuite, et une requête refusée pour `organization` a déjà dépensé son jeton de sujet.

Avec `state.type` valant `memory`, chaque réplica conserve ses propres seaux : derrière un répartiteur de charge, la limite effective est la limite configurée multipliée par le nombre de réplicas qui voient le sujet. Avec `redis`, tous les réplicas partagent un seul ensemble de seaux.

### Politiques d'outils

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

La première règle qui correspond à un appelant est la politique de cet appelant (`default` n'y est pas fusionnée). `allow` vaut `null` (tous les outils) ou une liste de noms avec `*` pour seul caractère générique ; `deny` l'emporte toujours. `match` désigne soit l'organisation du jeton, soit une étiquette renvoyée par votre resolver. Un `tools/call` refusé reçoit exactement la réponse d'un outil qui n'existe pas (`-32602`, `Unknown tool: <name>`) et n'est jamais transmis ; les réponses `tools/list` sont filtrées. Tant qu'une politique est active, la passerelle analyse les requêtes et les réponses qu'elle doit filtrer, et refuse ce qu'elle ne peut pas contrôler (corps illisibles, lots qui contiennent `tools/call` ou `tools/list`). Avec `"tools": null`, elle n'analyse jamais un corps et relaie les octets exactement tels qu'envoyés. La liste complète des cas se trouve dans [`configuration.md`](../../configuration.md#policiestools).

## 8.5 Une déconnexion du client n'est pas une annulation

Lorsqu'un client se déconnecte après que la passerelle a envoyé sa requête à Alien, la passerelle **continue de lire la réponse amont** et la jette, puis la ferme : le travail se termine, Alien le mesure, et la connexion retourne proprement dans son pool. Cela s'écarte volontairement de la règle MCP du 2026-07-28 selon laquelle fermer le flux annule la requête. Seul un `notifications/cancelled` explicite du client annule. Les flux d'écoute (`GET`), `DELETE` et les notifications sont fermés immédiatement à la déconnexion.

La ligne d'audit d'une telle requête est écrite au départ du client : statut `499`, résultat `client_disconnected`.

**Le plafond.** La partie qui se poursuit est bornée par le `read_timeout_seconds` de l'upstream, en durée totale d'horloge, comptée à partir du moment où le client est parti ; passé ce délai, la réponse est fermée et comptée comme `timeout`. En outre, `max_detached_reads` (inférieur à `max_connections`) borne le nombre de telles requêtes qu'un upstream peut avoir simultanément : au-delà, la requête d'un client qui part est annulée et comptée `shed`, avec un journal WARNING, afin que les clients partis ne privent pas de ressources ceux qui sont encore là. Dimensionnez `max_connections` pour les requêtes concurrentes que vous attendez plus `max_detached_reads`.

Sur le plan opérationnel : Alien peut facturer un travail qu'aucun client n'a attendu, `mcp_gateway.upstream.detached` montre la fréquence de ce cas, et le délai de grâce du pod doit dépasser le budget d'arrêt, qui inclut le plus grand `read_timeout_seconds` (section 8.6 ; le chart l'impose).

## 8.6 Mises à niveau

- **Versions.** L'image et le chart portent la même version (`version` et `appVersion` de `Chart.yaml` sont égales à la version de l'image). Épinglez une version ou un condensat ; jamais `latest`.
- **Configuration.** Le document de configuration porte `"version": 1`, la seule version de schéma que cette passerelle lit. Une nouvelle version de schéma sera un nouveau numéro, jamais un changement de sens silencieux sous le même. Le schéma JSON de la version 1 est [`config.schema.v1.json`](../../config.schema.v1.json). Validez un document avant de le déployer : `helm template` exécute les contrôles du chart, et la passerelle refuse un document invalide au démarrage, dans le journal du pod (`startup refused: ...`).
- **Le contrat de communication** ([`protocol.md`](../../protocol.md)) : les codes d'erreur, les clés `_meta`, les statuts, les formes de défi et les en-têtes ne changent qu'avec une version majeure de la passerelle. Un nouveau code peut être ajouté dans une version mineure ; un code existant ne change jamais de sens ni de numéro. Faites la correspondance sur le code, jamais sur le texte du message, qui peut être reformulé à chaque version.
- **Mise à niveau progressive avec le chart.** `helm upgrade` renouvelle le Deployment avec `maxSurge: 1` et `maxUnavailable: 0` (un nouveau pod est prêt, sur `/readyz`, avant qu'un ancien ne s'arrête), le pod continue de servir pendant `preStopSleepSeconds` après avoir été marqué en cours d'arrêt, et le délai de grâce laisse se terminer les requêtes en cours et la passerelle exécuter son propre arrêt (point suivant). Les flux d'écoute sont fermés immédiatement lorsqu'un pod s'arrête ; le client se reconnecte à un autre réplica, qui retrouve sa session dans Redis.
- **Ce que fait un arrêt.** Sur `SIGTERM`, la passerelle cesse d'accepter des connexions et termine proprement ses flux d'écoute (aucun événement d'erreur ; la ligne d'audit porte le résultat `forwarded` et la raison `shutdown`). Les requêtes en cours, un `tools/call` par exemple, disposent du plus grand `read_timeout_seconds` pour se terminer et relayer leur réponse. Ensuite l'application s'arrête : les lectures détachées ont 10 s pour finir et 10 s pour se fermer, et la télémétrie est vidée (avec OTLP, jusqu'à `timeout_seconds` + 2 s pour chacune des traces et des métriques). La somme est le budget d'arrêt : 150 s avec un upstream à `read_timeout_seconds` 130 et sans OTLP ; la passerelle l'écrit dans son journal au démarrage. Le chart en déduit `terminationGracePeriodSeconds` (160 s ici, avec la pause preStop et une marge) et refuse une valeur plus petite ; avec Docker Compose, gardez `stop_grace_period` au-dessus. Un délai de grâce inférieur au budget signifie que le processus est tué avant le vidage : pas de télémétrie, et les lectures détachées ne sont pas comptées. Détails : [`configuration.md`](../../configuration.md#shutdown).
- **Versions mixtes sur un même Redis : non vérifié.** Pendant une mise à niveau progressive, deux versions de la passerelle s'exécutent contre le même Redis. La structure de l'état est documentée (clés, expiration, formats de valeurs), mais ce projet n'a **pas** testé deux versions différentes de la passerelle contre un même Redis : il ne peut donc pas promettre qu'elles sont compatibles. Tant qu'une version n'indique pas que la structure de l'état n'a pas changé, considérez une mise à niveau qui franchit des versions comme une opération pouvant réinitialiser les sessions et les identifiants en cache. Si vous avez besoin d'en être sûr, donnez à la nouvelle version son propre `state.key_prefix` (un nouvel espace de clés : sessions et cache démarrent vides, les seaux démarrent pleins, rien n'est mal lu), et supprimez les anciennes clés par leur expiration.
- **Retour arrière.** `helm rollback` revient à la révision précédente. La même réserve s'applique à l'état partagé avec la version que vous quittez.

## 8.7 Suspension et fin de contrat

Cette section s'adresse à un **opérateur sponsor**, par exemple un éditeur qui vend l'accès à ses clients au moyen des groupes du chapitre 3. Tout ce qui suit a été exécuté sur un déploiement réel de la passerelle devant une vraie plateforme Alien ; chaque réponse est celle que ce déploiement a renvoyée. Les appels suivent les conventions du [chapitre 3](03-alien-side.md#conventions-utilisées-dans-les-exemples) (`sponsor.hdr`, `$ALIEN_API`, `$SPONSOR_ORG`) et les identifiants `GROUP_ID`, `ORG_ID`, `USER_ID` et `TOKEN_IDENTIFIER` de ses étapes.

### Qui coupe quoi

| Levier | Où | Ce qu'il arrête |
|---|---|---|
| Suspension du groupe sponsorisé | Alien (`/groups/{id}/suspension`) | Toutes les lectures sponsorisées des organisations rattachées au groupe. Alien la vérifie à **chaque requête** : rien n'est facturé |
| Suspension d'un utilisateur géré par le client | Alien (`/organizations/{org}/users/{user}`) | Les lectures faites avec les clés de cet utilisateur. Alien la vérifie à chaque requête |
| Révocation d'une clé | Alien (`/tokens/{identifier}`) | Une clé. La passerelle répond `-31001` tant que le resolver n'en donne pas une autre |
| L'authentification unique du client | Le fournisseur d'identité du client | Les nouvelles connexions de ses personnes. La passerelle ne la consulte plus une fois la requête authentifiée |
| La passerelle | aucun des leviers ci-dessus | La passerelle ne détient aucun état de suspension |

La passerelle **n'a pas besoin que son cache soit invalidé pour une suspension** : la requête d'un appelant suspendu va quand même en amont, Alien la refuse, et la passerelle relaie ce refus tel quel. Seules une suspension permanente d'utilisateur et une clé révoquée font répondre `401` à Alien ; la passerelle supprime alors elle-même l'identifiant en cache (chapitre 8.1).

Une suspension vaut pour un groupe. Pour suspendre un client sans les autres, ce client doit avoir son propre groupe sponsorisé.

### 8.7.1 Suspendre un client temporairement

Suspendez l'accès sponsorisé du groupe, avec le message que liront les personnes du client :

<!-- not-config -->
```bash
curl -sS --max-time 30 -H @sponsor.hdr -H "x-organization-id: $SPONSOR_ORG" \
  -H 'Content-Type: application/json' \
  -d '{"kind": "temporary", "message": "Access is paused while we settle your account. Contact support@example-publisher.test."}' \
  "$ALIEN_API/groups/$GROUP_ID/suspension"
```
Attendu : `200`, et le groupe avec `suspensionKind` à `temporary`, `suspendedAt` et votre `suspensionMessage` :

<!-- not-config -->
```json
{"success": true, "data": {"id": 454, "isSponsored": true, "suspendedAt": "2026-11-24T12:06:41.755+01:00", "suspensionKind": "temporary", "suspensionMessage": "Access is paused while we settle your account. Contact support@example-publisher.test.", "...": "..."}}
```
**Ce que voit le client IA.** Le `tools/call` suivant, à travers la passerelle, reçoit HTTP `200`, et non une erreur HTTP, et un résultat d'outil marqué `isError` dont le texte est votre message dans l'enveloppe d'erreur du serveur d'outils. La passerelle le relaie tel quel (résultat d'audit `forwarded`) :

<!-- not-config -->
```json
{"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": "Error calling tool 'datacluster_get_dataset': {\n  \"success\": false,\n  \"error\": \"Access is paused while we settle your account. Contact support@example-publisher.test.\"\n}"}], "isError": true}}
```
`tools/list` et les autres méthodes ne sont pas touchés. Le client IA affiche le texte à la personne : c'est pourquoi le message doit dire qui contacter. **Rien n'est facturé** pour un appel refusé, et il n'apparaît ni dans le flux de consommation du client ni dans celui du sponsor.

Pour lever la suspension :

<!-- not-config -->
```bash
curl -sS --max-time 30 -X DELETE -H @sponsor.hdr -H "x-organization-id: $SPONSOR_ORG" \
  "$ALIEN_API/groups/$GROUP_ID/suspension"
```
L'appel suivant est de nouveau servi. Rien n'est à changer sur la passerelle.

### 8.7.2 Suspendre un client définitivement

La requête est la même avec `"kind": "permanent"` :

<!-- not-config -->
```bash
curl -sS --max-time 30 -H @sponsor.hdr -H "x-organization-id: $SPONSOR_ORG" \
  -H 'Content-Type: application/json' \
  -d '{"kind": "permanent", "message": "Your contract has ended. Contact support@example-publisher.test."}' \
  "$ALIEN_API/groups/$GROUP_ID/suspension"
```
Le client IA voit exactement ce qu'il voit pour une suspension temporaire, avec le message définitif. Une suspension permanente est **finale** : la lever, ou la repasser en temporaire, répond `409` :

<!-- not-config -->
```json
{"success": false, "message": "A permanent suspension cannot be lifted.", "error": {"status": 409, "name": "GROUP_SUSPENSION_PERMANENT", "message": "This group's sponsored access is permanently suspended.", "data": null, "origin": "client"}}
```
Considérez-la comme irréversible. Utilisez-la pour un client qui part, pas pour mettre un client en pause.

### 8.7.3 Suspendre ou réactiver un utilisateur

Un utilisateur géré par le client se suspend par la route de l'utilisateur. Envoyez toujours `kind` : omis, il vaut par défaut une suspension permanente. `message` est ce que lit la personne, et `reason` n'est conservé que pour votre piste d'audit.

**Temporaire :**

<!-- not-config -->
```bash
curl -sS --max-time 30 -X DELETE -H @sponsor.hdr -H 'Content-Type: application/json' \
  -d '{"kind": "temporary", "reason": "invoice 2026-11 unpaid", "message": "Your access is paused. Contact support@example-publisher.test."}' \
  "$ALIEN_API/organizations/$ORG_ID/users/$USER_ID"
```
Attendu : `200` et l'utilisateur avec `isLocked` à `true`, `suspensionKind` à `temporary` :

<!-- not-config -->
```json
{"success": true, "data": {"id": 1016, "userType": "client-managed", "isLocked": true, "suspensionKind": "temporary", "lockMessage": "Your access is paused. Contact support@example-publisher.test.", "...": "..."}}
```
**Ce que voit le client IA.** HTTP `200` et une erreur d'outil dont le texte est votre message **seul**, sans enveloppe. `tools/list` fonctionne toujours. La passerelle transmet la requête et conserve son identifiant en cache :

<!-- not-config -->
```json
{"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": "Your access is paused. Contact support@example-publisher.test."}], "isError": true}}
```
Les appels refusés ne sont ni facturés ni listés. Réactivez avec :

<!-- not-config -->
```bash
curl -sS --max-time 30 -X PATCH -H @sponsor.hdr \
  "$ALIEN_API/organizations/$ORG_ID/users/$USER_ID"
```
La clé de l'utilisateur refonctionne aussitôt : rien n'a été révoqué, donc le resolver et la passerelle ne sont pas touchés.

**Permanente** (même requête avec `"kind": "permanent"`) :

<!-- not-config -->
```bash
curl -sS --max-time 30 -X DELETE -H @sponsor.hdr -H 'Content-Type: application/json' \
  -d '{"kind": "permanent", "reason": "contract ended", "message": "Your access has ended."}' \
  "$ALIEN_API/organizations/$ORG_ID/users/$USER_ID"
curl -sS --max-time 30 -H @sponsor.hdr "$ALIEN_API/organizations/$ORG_ID/users/$USER_ID/tokens"
```
Les clés de l'utilisateur sont **supprimées** : la liste des jetons est maintenant vide.

<!-- not-config -->
```json
{"success": true, "data": []}
```
La passerelle détient encore l'ancienne clé dans son resolver, Alien lui répond `401`, et le client IA reçoit HTTP `200` avec le `-31001` de la passerelle. La passerelle supprime son identifiant en cache dès ce premier refus :

<!-- not-config -->
```json
{"jsonrpc": "2.0", "id": 1, "error": {"code": -31001, "message": "The credential configured for this account was rejected by the service. Contact your administrator.", "data": {"upstream_status": 401}}}
```
Le même `PATCH` que ci-dessus réactive l'utilisateur, mais les clés supprimées ne reviennent pas : émettez-en une nouvelle (8.7.4) et mettez le resolver à jour.

### 8.7.4 Révoquer une clé et la remplacer

Révoquez la clé par son identifiant, puis émettez-en une nouvelle pour le même utilisateur. Conservez le nouveau `data.token` et `data.identifier` comme à l'[étape 3.6](03-alien-side.md#36-émettre-la-clé-dapi) :

<!-- not-config -->
```bash
curl -sS --max-time 30 -X DELETE -H @sponsor.hdr \
  "$ALIEN_API/organizations/$ORG_ID/users/$USER_ID/tokens/$TOKEN_IDENTIFIER"
curl -sS --max-time 30 -H @sponsor.hdr -H 'Content-Type: application/json' \
  -d '{"name": "mcp-gateway", "scope": "api-key"}' \
  "$ALIEN_API/organizations/$ORG_ID/users/$USER_ID/tokens"
```
Placez la nouvelle clé là où votre resolver la lit : le fichier de clé d'une entrée `static`, ou ce que renvoie votre webhook. **La passerelle n'est pas redémarrée.** Sur le déploiement réel, l'appel suivant avec la nouvelle clé a fonctionné environ 5 secondes après la mise à jour du fichier ; une fois, le Secret monté a mis 55 secondes à atteindre les pods (la synchronisation propre du kubelet), et tous les appels de cette fenêtre ont reçu `-31001`. Un `resolver_cache` ajoute son TTL (8.1). Pour éviter tout creux, suivez l'ordre du 8.1 : émettre, mettre à jour, attendre, révoquer.

**Ce que voit le client IA** entre la révocation et la mise à jour est le `-31001` montré plus haut, en HTTP `200`.

### 8.7.5 Quota mensuel

Un sponsor fixe un plafond mensuel, en euros, sur un groupe, avec le message affiché une fois atteint :

<!-- not-config -->
```bash
curl -sS --max-time 30 -X PATCH -H @sponsor.hdr -H "x-organization-id: $SPONSOR_ORG" \
  -H 'Content-Type: application/json' \
  -d '{"monthlyQuota": 100, "quotaMessage": "The monthly allowance is used up. Contact support@example-publisher.test."}' \
  "$ALIEN_API/groups/$GROUP_ID"
```
Lisez le groupe (`GET`, ci-dessous) pour suivre le mois : `currentMonth` porte `amount`, `currency` et `percentage` du quota.

<!-- not-config -->
```bash
curl -sS --max-time 30 -H @sponsor.hdr -H "x-organization-id: $SPONSOR_ORG" "$ALIEN_API/groups/$GROUP_ID"
```
- **Jusqu'à environ 80 % : rien.** Sur le déploiement réel, les appels à 67 % et 75 % ne portaient aucun avis.
- **À partir de 80 % : un avis.** La lecture qui amène la dépense du mois à 80 % ou plus le porte déjà. Le premier mesuré est venu à **83 %**, puis 91 % et 100 %. Il voyage dans le résultat que reçoit le client IA, sous forme d'une liste `notices` dans le JSON (ou d'un bloc final `**Notice:**`, selon l'outil), et le serveur d'outils demande au modèle de le relayer à la personne :

<!-- not-config -->
```json
{"success": true, "data": {"...": "..."}, "notices": ["<client organization>'s <sponsor organization> access has used 83 % of its monthly quota. Contact <sponsor organization> to raise it."]}
```
- **À 100 % : un refus.** Les appels suivants reçoivent HTTP `200`, `isError` à `true`, avec votre `quotaMessage` dans la même enveloppe qu'une suspension de groupe :

<!-- not-config -->
```json
{"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": "Error calling tool 'datacluster_get_dataset': {\n  \"success\": false,\n  \"error\": \"The monthly allowance is used up. Contact support@example-publisher.test.\"\n}"}], "isError": true}}
```
**Les refus ne sont pas comptés.** La dépense est restée à son plafond, rien n'a été facturé au client, et les appels refusés ne figurent dans aucun flux. Pour lever le plafond, faites un `PATCH` du groupe avec un `monthlyQuota` plus élevé ou `null`.

### 8.7.6 Clore un client à la fin du contrat

**Pas encore disponible, en attente de correctifs de la plateforme.** Il n'existe pas encore de moyen pris en charge pour clore le compte d'un client côté plateforme, et ce tutoriel n'en documente pas.

Ce qu'il faut faire aujourd'hui pour couper tout accès et toute dépense d'un client :

1. **Suspendez définitivement son groupe sponsorisé** (8.7.2). C'est final.
2. **Suspendez définitivement chacun de ses utilisateurs** (8.7.3). Leurs clés sont supprimées.
3. **Révoquez toute clé que vous détenez encore** (8.7.4) et retirez les entrées du client de votre resolver, pour que plus personne ne soit associé à un identifiant.
4. **Demandez au client de désactiver la connexion** de ses personnes dans son propre fournisseur d'identité, ce qui arrête les nouvelles connexions.

Conservez les organisations et leur historique de consommation : c'est votre trace de facturation.

Suite : [9. Dépannage](09-troubleshooting.md).
