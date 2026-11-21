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
{"ts":"2026-11-20T09:14:03.512+00:00","event":"mcp_request","request_id":"6f1c2b0e-5a47-4d8e-9d52-3b0a4c9e7f10","trace_id":"4bf92f3577b34da6a3ce929d0e0e4736","subject":"user-1","organization":"example-org","labels":{"client":"example"},"http_method":"POST","rpc_method":"tools/call","rpc_id":7,"tool":"search","status":200,"outcome":"forwarded","reason":null,"upstream":"main","latency_ms":182.4,"upstream_status":200,"upstream_request_id":"req-81c4","upstream_session":false}
```

Champs : `ts`, `event` (toujours `mcp_request`), `request_id`, `trace_id`, `subject`, `organization`, `labels`, `http_method`, `rpc_method`, `rpc_id`, `tool`, `status`, `outcome`, `reason`, `upstream`, `latency_ms`, `upstream_status`, `upstream_request_id`, `upstream_session`. Les valeurs textuelles sont tronquées à 256 caractères.

`request_id` est aussi renvoyé au client dans l'en-tête `x-request-id` de chaque réponse que la passerelle produit ou relaie : lorsqu'un utilisateur signale un problème, demandez-lui cet en-tête et cherchez la ligne d'audit. Un `x-request-id` envoyé par le client est ignoré.

Le `outcome` est l'une des valeurs suivantes : `origin_refused`, `unauthenticated`, `insufficient_scope`, `idp_unavailable`, `request_body_timeout`, `body_too_large`, `mcp_header_mismatch`, `duplicate_header`, `resolver_unavailable`, `state_unavailable`, `gateway_error`, `denied`, `rate_limited`, `session_not_found`, `upstream_error`, `upstream_auth_rejected`, `upstream_stream_failed`, `tool_denied`, `batch_refused`, `unparsable_request`, `invalid_request`, `tool_policy_failed`, `forwarded`, `client_disconnected`, `internal_error`. `reason` contient la raison lisible par machine lorsqu'il y en a une : la raison de refus de votre resolver, la classe d'échec d'un flux interrompu, ou le seau (`subject` ou `organization`) qui a refusé une requête limitée en débit.

Les journaux du processus lui-même (démarrage, erreurs du resolver et du JWKS) sont aussi des lignes JSON sur la sortie standard, avec `ts`, `level`, `logger` et `message`.

### Traces et métriques

`observability.otlp` exporte les traces et les métriques par OTLP. `{"otlp": null}` n'exporte rien. Chaque requête est une nouvelle trace racine comportant trois spans :

| Span | Quoi |
|---|---|
| `mcp-gateway request` | La requête entière, jusqu'à la fin de la réponse |
| `mcp-gateway resolve_credential` | L'étape du resolver ; attributs `mcp_gateway.resolver.type`, `mcp_gateway.resolver.cache` (`hit`, `miss`, `negative`, `none`) et `mcp_gateway.resolver.result` |
| `mcp-gateway upstream` | L'appel à Alien, jusqu'aux en-têtes de la réponse |

Métriques :

| Instrument | Type | Usage |
|---|---|---|
| `mcp_gateway.requests` | compteur | Requêtes par `outcome`, `status_class`, `rpc_method`, `upstream`. Alertez sur une part croissante de résultats autres que `forwarded` |
| `mcp_gateway.request.duration` | histogramme, secondes | Latence des requêtes, mêmes attributs |
| `mcp_gateway.upstream.duration` | histogramme, secondes | Délai jusqu'aux en-têtes de la réponse d'Alien |
| `mcp_gateway.resolver.lookups` | compteur | Appels au resolver par `resolver_type`, `cache`, `result`. Le taux de succès du cache et le taux d'échec de votre webhook |
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

Une requête refusée reçoit une réponse sous la forme que son émetteur peut lire, toujours avec `Retry-After` : un `tools/call` reçoit un HTTP `200` et un résultat avec `isError: true` et `_meta["alien.club/retryAfterSeconds"]` ; une autre requête portant un identifiant reçoit un HTTP `200` et `-31014` ; une requête à laquelle on ne peut pas répondre (une notification, `GET`, `DELETE`) reçoit un HTTP `429`. Le résultat d'audit est `rate_limited`, `reason` valant `subject` ou `organization`.

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

Suite : [9. Dépannage](09-troubleshooting.md).
