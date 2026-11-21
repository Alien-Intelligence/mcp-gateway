# 9. Dépannage

## Commencer ici

1. Récupérez l'en-tête de réponse **`x-request-id`** de l'appel en échec (chaque réponse que la passerelle produit ou relaie en porte un).
2. Trouvez la ligne d'audit portant ce `request_id` dans la sortie standard de la passerelle. Lisez `outcome`, `reason` et `status` : ils indiquent quelle étape a refusé.
3. Le client ne voit qu'un code d'erreur et un message générique. La raison figure dans la ligne d'audit et dans les journaux de la passerelle, jamais dans la réponse.

Faites la correspondance sur le **code**, jamais sur le texte du message : les textes peuvent être reformulés à chaque version. Tous les codes sont dans [`protocol.md`](../../protocol.md).

## La connexion ne démarre pas ou ne se termine pas

| Symptôme | Cause | Correctif |
|---|---|---|
| Claude indique qu'il n'a pas pu joindre le serveur MCP ; la passerelle a reçu la première requête mais votre serveur OAuth n'a vu aucun trafic | Le client n'a pas pu lire les métadonnées : le `401` n'a pas de `resource_metadata`, ou le chemin des métadonnées n'est pas acheminé vers la passerelle, ou votre serveur OAuth n'est pas joignable depuis le `160.79.104.0/21` d'Anthropic (un WAF placé devant le fournisseur d'identité en est une cause courante) | Faites un `curl` sur l'URL des métadonnées du défi (chapitre 5.5) ; acheminez `/.well-known/oauth-protected-resource<path>` vers la passerelle ; autorisez la plage d'Anthropic sur la passerelle **et** sur le serveur OAuth |
| Claude échoue à la connexion avec `invalid_scope` | Connu avec Claude et Keycloak : lorsqu'aucune portée n'est annoncée, Claude demande toutes les portées du serveur et reçoit `invalid_scope` ; et Claude ajoute `offline_access` lorsque Keycloak la liste, ce qui échoue pour un client enregistré sans elle. La passerelle annonce toujours ce que liste `auth.scopes_advertised` (au moins une entrée) : cherchez donc un défi ou des métadonnées qui ne la portent pas (une autre couche les réécrit), ou une portée listée que votre serveur ne connaît pas | Annoncez `["mcp-gateway", "offline_access"]` dans `auth.scopes_advertised` (Keycloak : un client enregistré sans `offline_access` dans son `scope` échoue avec `invalid_scope` lorsque Claude l'ajoute) ; vérifiez le `scope` du défi avec `curl -i` (chapitre 5.5) et que chaque portée annoncée existe dans le realm |
| L'enregistrement de VS Code est refusé (`403`, « Not permitted to use specified clientScope ») | `auth.scopes_advertised` contient `openid` : VS Code envoie les portées du défi comme `scope` de son enregistrement, et Keycloak n'a pas de portée de client `openid` | Annoncez `["mcp-gateway", "offline_access"]` (chapitre 6) |
| Keycloak refuse l'enregistrement du client (symptôme rapporté : HTTP `403` sur la requête d'enregistrement) | La politique d'enregistrement « Trusted Hosts » n'a pas d'entrée pour l'hôte qui s'enregistre ou pour les URI de redirection du client | Ajoutez les hôtes du chapitre 7 à la politique Trusted Hosts anonyme du realm (chapitre 6) |
| ChatGPT ne peut pas s'enregistrer par CIMD auprès de Keycloak | Keycloak ne prend pas en charge ChatGPT par le CIMD | Utilisez le DCR, avec `chatgpt.com` dans les hôtes de confiance |
| Le client se connecte, puis chaque appel est un `401` avec `token audience does not match this resource` | L'`aud` du jeton ne contient pas `public_url`. Avec Keycloak et sans mappeur d'audience, `aud` vaut `account` | Ajoutez le mappeur d'audience à une portée de client que le client demande (chapitre 6) et annoncez cette portée ; comparez `aud` avec `public_url` caractère par caractère |
| La connexion boucle : le client se réauthentifie après chaque échec | Un `401` avec un défi OAuth atteint le client alors que son jeton est valide. La passerelle ne fait jamais cela : elle transforme un `401` d'Alien en JSON-RPC `-31001`. Un autre élément du chemin (un proxy, une seconde passerelle, une couche personnalisée en amont, ou un système d'appui du resolver qui répond par un défi) relaie un `401` d'upstream | Appelez directement la passerelle avec un jeton valide et comparez ; retirez la couche qui relaie le `401` |
| La redirection après la connexion échoue chez votre serveur OAuth (« invalid redirect URI ») | L'URI de redirection du client n'est pas autorisée dans son client | Chapitre 7 : l'URI de redirection de chaque client. ChatGPT : copiez celle qu'affiche sa page de gestion |
| Copilot Studio échoue après la connexion | Le comportement de Copilot Studio vis-à-vis de PKCE, de `resource` et du CIMD n'est pas confirmé (chapitre 7) | Utilisez le mappeur d'audience ; testez avec un tenant ; lisez la requête en échec chez votre serveur OAuth |

## La passerelle répond `401` ou `403` avant tout resolver

Les corps des `401` sont `{"error": "invalid_token", "error_description": "<text>"}` avec un `WWW-Authenticate: Bearer resource_metadata="...", scope="...", error="invalid_token", ...`.

| `error_description` (résultat d'audit `unauthenticated`) | Cause | Correctif |
|---|---|---|
| `missing bearer token` | Pas d'en-tête `Authorization: Bearer` : c'est la première requête normale | Rien, sauf si un client qui possède un jeton reçoit quand même cette erreur : vérifiez qu'aucun proxy ne supprime `Authorization` |
| `token is malformed` | Ce n'est pas un JWT : un jeton d'accès opaque | Configurez le serveur OAuth pour qu'il émette des jetons d'accès JWT |
| `token algorithm is not accepted` | Le `alg` du jeton n'est pas dans `auth.algorithms` (ou est `HS*`/`none`) | Ajoutez l'algorithme avec lequel votre serveur signe, s'il est asymétrique |
| `token signing key is unknown` | Le `kid` n'est pas dans l'ensemble de clés situé à `jwks_uri` (juste après une rotation de clé sur le serveur, ou mauvais `jwks_uri`). La passerelle récupère de nouveau l'ensemble pour un `kid` inconnu, au plus une fois toutes les 10 secondes | Vérifiez `jwks_uri` depuis le réseau de la passerelle ; attendez un renouvellement |
| `token signature is invalid` | Signé par une autre clé que celles de l'ensemble de clés | Idem : mauvais realm ou mauvais `jwks_uri` |
| `token issuer is not trusted` | `iss` diffère de `auth.issuer` (barre oblique finale, nom d'hôte interne ou public, mauvais realm) | Faites en sorte que `auth.issuer` soit exactement égal au `iss` du jeton |
| `token audience does not match this resource` | Voir plus haut | Mappeur d'audience |
| `token expired`, `token not yet valid` | Durée de vie trop courte pour le renouvellement du client, ou horloges différant de plus de `auth.clock_skew_seconds` | Synchronisez les horloges (NTP) ; allongez la durée de vie du jeton (chapitre 6) |
| `token is missing a required claim` | Le jeton n'a ni `exp`, ni `iss`, ni `aud` | Corrigez la correspondance de jeton du serveur OAuth |
| `token does not identify a user` | La revendication située à `auth.identity.subject` est absente ou n'est pas une chaîne, ou une autre revendication mappée a un mauvais type (une revendication d'un mauvais type donne un `401` ; une revendication facultative absente est simplement absente) | Vérifiez les chemins de revendications contre un jeton décodé |

| Statut | Corps | Cause | Correctif |
|---|---|---|---|
| `403` | `insufficient_scope` | Le jeton n'a pas une portée listée dans `auth.required_scopes` (résultat `insufficient_scope`) | Le client doit la demander : annoncez-la aussi dans `scopes_advertised` et assurez-vous que le realm l'accorde |
| `503` + `Retry-After: 5` | `temporarily_unavailable`, « identity provider keys are unavailable » (résultat `idp_unavailable`) | La passerelle ne peut pas récupérer l'ensemble de clés et la copie en cache a dépassé `jwks_max_stale_seconds` | Corrigez le chemin réseau vers `jwks_uri` (et son certificat) ; la passerelle journalise `JWKS fetch failed` |
| `403` + `-31008` | `ORIGIN_REFUSED` (résultat `origin_refused`) | Une origine de navigateur qui n'est pas dans `policies.allowed_origins` | Ajoutez cette origine exacte (chapitre 7). Les requêtes sans en-tête `Origin` passent |
| `413` / `408` | `-32600` | Corps supérieur à `limits.max_request_body_bytes`, ou non reçu dans le délai `limits.request_body_timeout_seconds` | Relevez la limite, ou trouvez l'émetteur lent |
| `400` | `-32020`, ou `-32600` pour un `Mcp-Session-Id` ou un `Mcp-Protocol-Version` répété | `Mcp-Method` ou `Mcp-Name` ne concorde pas avec le corps ; ou un proxy a dupliqué un en-tête | Retirez ce qui réécrit ou duplique les en-têtes MCP |

## L'utilisateur est connecté mais l'appel échoue

| Symptôme | Cause | Correctif |
|---|---|---|
| `403`, `-31010` (résultat `denied`, raison `no_mapping`) | Le resolver n'a trouvé aucune clé pour cette identité | Créez la correspondance (chapitre 4). Vérifiez ce que contenait l'identité : sans adresse e-mail vérifiée, `email` vaut `null` (la revendication désignée par `email_verified` doit valoir `true`) ; un mauvais chemin `organization` ou `groups` |
| `403`, `-31011` (raison `ambiguous_mapping`) | Deux entrées de groupe d'une correspondance `static` correspondent à l'utilisateur, ou une requête `store_sql` a renvoyé plus d'une ligne | Rendez la correspondance unique |
| `403`, `-31012` | Votre resolver a refusé avec une autre raison (voir `reason` dans la ligne d'audit) | La règle de votre resolver |
| `503` + `Retry-After: 5`, `-31009` (résultat `resolver_unavailable`) | Votre webhook, base de données ou Redis n'a pas pu être joint, ou a dépassé le délai, après les nouvelles tentatives | Vérifiez le système d'appui et le chemin réseau de la passerelle ; ligne de journal `credential backend unavailable` |
| `500`, `-31005` (« The gateway could not obtain credentials for this request. » ; résultat `gateway_error`) | Une erreur du resolver : votre webhook a répondu par un statut autre que `200` (un `401` pour une signature qu'il a rejetée, par exemple), une réponse malformée ou trop volumineuse, un nom d'`upstream` inconnu ou une clé invalide ; un fichier de clé `static` illisible ou vide ; un fichier de correspondance défectueux ; un certificat TLS que la passerelle ne peut pas vérifier | La ligne de journal `credential resolution failed` nomme la cause (jamais le contenu). Testez le webhook avec `resolve_probe.py` (chapitre 4) |
| `-31001` avec `data.upstream_status` valant `401` (HTTP `200` lorsque la requête attend une réponse) | Alien a rejeté la clé que la passerelle détient pour cet utilisateur : révoquée, mauvaise portée, ou utilisateur supprimé. La passerelle a supprimé sa copie en cache | Corrigez ou réémettez la clé (chapitre 3) et mettez à jour la source (chapitre 8). Ce n'est pas un problème OAuth : l'utilisateur n'a pas besoin de se reconnecter |
| `-31002` avec `upstream_status` valant `402` | Alien a répondu `402` à la clé | Demandez à Alien : le compte de la clé est refusé pour des raisons de paiement. La passerelle a supprimé sa copie en cache |
| `-31003` (`502`) / `-31004` (`504`) | Le serveur MCP Alien est injoignable, ou la connexion, l'écriture, la lecture ou le pool a dépassé le délai | Vérifiez `upstreams.*.url` et le réseau ; ne relevez les délais que si l'outil est légitimement lent (`read_timeout_seconds` est le plus long silence sur une réponse) |
| `-31006` | Le flux amont s'est interrompu avant que la réponse ne soit relayée | Vérifiez l'upstream ; réessayez |
| `404`, `-31007` (« The session was not found. Start a new session. ») | Le `Mcp-Session-Id` est inconnu ici, expiré (`sessions.idle_ttl_seconds`), ou lié à un autre utilisateur | Normal après une période d'inactivité : le client démarre une nouvelle session. Si cela se produit juste après une autre requête, **deux réplicas avec `state.type` valant `memory`** (utilisez Redis, chapitre 5), ou la clé de chiffrement a été renouvelée (chapitre 8) |
| `503` + `Retry-After: 5`, `-31013` (résultat `state_unavailable`) | L'état partagé (Redis) a échoué ou dépassé le délai ; la passerelle échoue en mode fermé et ne transmet rien | Réparez Redis ; le réplica signale aussi `GET /readyz` `503` et quitte le Service |
| Un résultat d'outil `isError: true` « Rate limit reached for this account. Retry in N s. », ou `-31014`, ou HTTP `429` | Un seau de limitation de débit est vide (résultat `rate_limited`, raison `subject` ou `organization`) | Attendez `Retry-After` ; relevez `burst` ou `per_minute` (chapitre 8). Avec l'état `memory`, chaque réplica a ses propres seaux |
| `-32602`, « Unknown tool: x » | `policies.tools` refuse l'outil à cet appelant, ou l'outil n'existe pas (ils se présentent de la même façon, volontairement) | Vérifiez la règle qui correspond à l'appelant (organisation ou étiquette du resolver) |
| `-31005` « The gateway could not apply its tool policy to this response. » (HTTP `200`), résultat `tool_policy_failed` | Une réponse que la passerelle devait filtrer n'a pas pu l'être : pas du JSON, trop volumineuse (`limits.max_filtered_response_bytes`, `limits.max_sse_event_bytes`), compressée, ou de forme erronée | Regardez `reason` (la classe d'échec) ; relevez la limite si la réponse est légitimement volumineuse |
| `-31005` « The service returned an error for this request. » (résultat `upstream_error`) | L'upstream a répondu un code non 2xx à un `tools/list` alors qu'une politique est active | Vérifiez l'upstream |
| `400`, `-32700` ou `-32600` alors qu'une politique d'outils est active | Corps illisible, un `tools/call` sans identifiant, ou un lot contenant `tools/call` ou `tools/list` | Le client doit envoyer une requête par appel |

## Problèmes de flux continu

| Symptôme | Cause | Correctif |
|---|---|---|
| Les résultats des outils longs arrivent d'un seul coup à la fin, ou la progression n'apparaît jamais | Un proxy placé devant la passerelle **met en tampon** les Server-Sent Events | Désactivez la mise en tampon (la passerelle envoie `X-Accel-Buffering: no` ; ingress-nginx a aussi besoin de `proxy-buffering: "off"` ; les autres proxys ont leur propre réglage, chapitre 5) |
| Les flux sont coupés après une durée fixe (30 s, 60 s, ...) alors que l'outil travaille encore | Un délai d'inactivité ou de requête dans un proxy, un répartiteur de charge ou un ingress, plus court que votre outil le plus long | Relevez-le au-dessus du plus grand `upstreams.*.read_timeout_seconds` (l'exemple du chart utilise `3600` s pour ingress-nginx ; Gateway API : `timeouts.request: 0s`) |
| L'appel s'est terminé chez Alien mais le client n'a rien reçu | Le client s'est déconnecté : la passerelle a quand même lu la réponse, l'a jetée et Alien l'a mesurée (chapitre 8.5) | Normal. Voir `mcp_gateway.upstream.detached` |

## La passerelle ne démarre pas ou n'est pas prête

| Symptôme | Cause | Correctif |
|---|---|---|
| Le pod ou le conteneur se termine aussitôt avec `startup refused: invalid MCP_GATEWAY_CONFIG: <key>: <rule>` | La configuration est invalide ; le message nomme la clé et la règle, jamais la valeur. Cas fréquents : `public_url must be listed in auth.audiences` ; `version: ... must declare "version": 1` ; `sessions.max_entries is required when state.type is memory` ou `must be null when state.type is redis` ; une clé inconnue (chaque clé est obligatoire, les clés inconnues sont refusées) | Corrigez la clé nommée. Exécutez d'abord `helm template` : le chart contrôle le document |
| `startup refused: ... file unreadable` ou `file is empty` | Un secret `*_file` est absent, vide ou non monté | Montez le Secret ; vérifiez que le chemin de montage est égal au chemin dans `config` |
| `helm install` échoue avec un message sur l'état partagé, le délai de grâce ou les ports | Contrôles au moment du rendu (chapitre 5.3) | Lisez le message : il indique quelle valeur modifier |
| `GET /readyz` répond `503` | Redis injoignable (`state.type` valant `redis`) | Réparez Redis, son fichier d'URL et son chemin réseau. `/healthz` reste à `200` : le processus va bien |
| Les réplicas ne sont pas d'accord : un utilisateur est limité sur l'un et pas sur l'autre, ou des sessions sont perdues | `state.type` valant `memory` avec plus d'un réplica | Utilisez Redis (le chart refuse cette combinaison) |

## Ce que ce tutoriel n'a pas pu confirmer

- Copilot Studio : PKCE en mode manuel, le paramètre `resource` et le CIMD (chapitre 7).
- VS Code et Cursor : leurs URI de redirection figurent dans le tableau du README du modèle, avec la source de chacune ; la documentation utilisateur de VS Code ne les indique pas.
- Les origines qu'envoient ChatGPT, Copilot Studio et VS Code (chapitre 7).
- Le HTTP `403` de Keycloak sur un enregistrement anonyme refusé : c'est le symptôme rapporté, non reproduit ici.
- Deux versions de la passerelle sur un même Redis (chapitre 8.6).
