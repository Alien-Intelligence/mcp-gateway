# 6. Votre serveur OAuth

Ce chapitre configure Keycloak pour la passerelle et donne une liste de contrôle pour les autres serveurs OAuth. Tout ce dont la passerelle a besoin de la part du serveur figure dans le tableau du [chapitre 2](02-prerequisites.md#ce-que-votre-serveur-oauth-doit-fournir).

## 6.1 Keycloak

Ce dépôt fournit un modèle de realm pour Keycloak 26.8 dans [`deploy/keycloak/`](../../../deploy/keycloak/) : le realm auquel vos utilisateurs se connectent, avec l'audience, les revendications d'identité, l'enregistrement anonyme de clients restreint aux clients d'IA connus, et des durées de vie de jetons adaptées à la production. Il ne contient ni utilisateur, ni client, ni secret. Il a été testé sur `quay.io/keycloak/keycloak:26.8.2`. [`deploy/keycloak/README.md`](../../../deploy/keycloak/README.md) fait référence pour chaque réglage ; ce chapitre donne les étapes et les raisons.

La pile de développement de la passerelle (`dev/keycloak/`) a un realm différent. C'est un **banc d'essai** que les tests de bout en bout exercent (jetons de 60 secondes, liste d'hôtes de confiance conçue pour le réseau compose) ; ne l'utilisez pas comme déploiement.

### Produire les paramètres substituables

Le modèle comporte deux paramètres substituables (placeholders), que `render.sh` remplace :

| Paramètre | Valeur |
|---|---|
| `${REALM_NAME}` | Le nom du realm, de 1 à 64 caractères parmi `a-z 0-9 _ -`, par exemple `example-publisher`. Il entre dans la composition de l'émetteur : `https://<keycloak>/realms/<REALM_NAME>`. |
| `${GATEWAY_PUBLIC_URL}` | Exactement le `public_url` de la passerelle, par exemple `https://mcp.example.com/mcp`. Il devient le `aud` de chaque jeton d'accès. |

```bash
cd deploy/keycloak
REALM_NAME=example-publisher GATEWAY_PUBLIC_URL=https://mcp.example.com/mcp \
  ./render.sh > example-publisher-realm.json
```

`render.sh` n'affiche rien et se termine avec un code non nul lorsqu'une variable est absente ou mal formée, ou lorsqu'un `${NOM}` en majuscules subsiste dans la sortie. Produisez d'abord le fichier, même si Keycloak développe `${...}` à partir de son propre environnement lors d'un import : il laisse silencieusement en place un paramètre inconnu, et le realm fonctionnerait alors avec un `${GATEWAY_PUBLIC_URL}` littéral comme audience.

### Importer le realm

Importez le fichier produit de l'une des trois manières suivantes :

- Au démarrage du serveur : placez-le dans `/opt/keycloak/data/import/` et démarrez avec `--import-realm`. Un realm qui existe déjà est laissé tel quel : cette méthode ne fait donc que créer.
- Hors ligne, serveur arrêté : `bin/kc.sh import --file example-publisher-realm.json`.
- Console d'administration : *Create realm*, puis *Browse* et choisissez le fichier.

Pour essayer le realm sur une seule machine avant de déployer réellement Keycloak, exécutez l'image testée en mode développement avec le fichier produit dans le répertoire d'import. `start-dev` sert du HTTP simple avec une base de données embarquée qui est perdue lorsque le conteneur est supprimé : c'est pour un essai, jamais pour la production. Les deux variables `KC_BOOTSTRAP_ADMIN_*` créent l'administrateur du realm `master` au premier démarrage ; conservez-les dans un fichier de mode 600 (`--env-file`), pas sur la ligne de commande :

```bash
umask 077
printf 'KC_BOOTSTRAP_ADMIN_USERNAME=admin\nKC_BOOTSTRAP_ADMIN_PASSWORD=%s\n' "$(head -c 18 /dev/urandom | base64)" > keycloak-admin.env
mkdir -p import && cp example-publisher-realm.json import/ && chmod 755 import && chmod 644 import/*.json
docker run -d --name keycloak --env-file keycloak-admin.env -p 127.0.0.1:8080:8080 \
  -v "$PWD/import:/opt/keycloak/data/import:ro" quay.io/keycloak/keycloak:26.8.2 start-dev --import-realm
```

L'émetteur est alors `http://localhost:8080/realms/example-publisher` (Keycloak en mode développement tire le nom d'hôte de la requête : connectez-vous donc par cette adresse). Le `sslRequired: external` du realm n'accepte le HTTP simple que depuis des adresses privées, ce qu'est un essai sur une seule machine. Une passerelle qui s'exécute dans un conteneur ne peut pas récupérer l'ensemble de clés depuis `localhost` : connectez Keycloak au réseau de la passerelle (avec l'exemple compose du chapitre 5 : `docker network connect mcp-gateway_edge keycloak`) et définissez `auth.jwks_uri` sur `http://keycloak:8080/realms/example-publisher/protocol/openid-connect/certs`, en laissant `auth.issuer` tel quel (chapitre 2 : seule la passerelle récupère l'ensemble de clés).

Créez ensuite vos utilisateurs (ou connectez votre fédération d'utilisateurs ou votre fournisseur d'identité). Donnez à chacun une adresse e-mail vérifiée et, si votre resolver fait la correspondance dessus, l'attribut `org`. Avec la CLI d'administration de Keycloak, dans le conteneur (chaque commande qui a besoin d'un mot de passe le demande, de sorte qu'il reste hors de la liste des processus et de l'historique du shell ; le mot de passe doit satisfaire la politique du realm : au moins 12 caractères, différent du nom d'utilisateur, différent de l'adresse e-mail) :

```bash
docker exec -it keycloak /opt/keycloak/bin/kcadm.sh config credentials --config /tmp/kcadm.config \
  --server http://localhost:8080 --realm master --user admin
docker exec keycloak /opt/keycloak/bin/kcadm.sh create users --config /tmp/kcadm.config -r example-publisher \
  -s username=alice@example.com -s email=alice@example.com -s emailVerified=true -s enabled=true \
  -s 'attributes.org=["example-org"]'
docker exec -it keycloak /opt/keycloak/bin/kcadm.sh set-password --config /tmp/kcadm.config \
  -r example-publisher --username alice@example.com
```

Ne définissez `emailVerified` que pour une adresse que vous avez vérifiée : la passerelle ne fait confiance à une adresse e-mail que lorsque cet indicateur vaut `true`, et les exemples `static` et webhook font la correspondance dessus.

Si vos utilisateurs ont besoin de la console de compte de Keycloak, une correction est nécessaire après l'import : Keycloak crée son client intégré `account-console` sans aucune portée de client (client scope) lorsqu'un realm est importé avec ses propres portées de client, et la console répond alors `403`. Donnez à `account-console` les portées de client par défaut `basic`, `acr`, `profile`, `email`, `roles` et `web-origins` (*Clients* > `account-console` > *Client scopes* > *Add client scope*, type *Default*). Les clients MCP n'en ont pas besoin.

Pour la production, démarrez Keycloak derrière un proxy inverse TLS avec une vraie base de données, par exemple `bin/kc.sh start --hostname https://sso.example.com --proxy-headers xforwarded --http-enabled true --db postgres ...`. L'`auth.issuer` de la passerelle est alors `https://sso.example.com/realms/example-publisher` et `auth.jwks_uri` est `<issuer>/protocol/openid-connect/certs`.

### L'audience

La passerelle n'accepte un jeton que si son `aud` contient l'URL de la passerelle (`auth.audiences`, qui doit contenir `public_url`). Sans mappeur, Keycloak place `account` dans `aud`, et la passerelle répond `401` avec `token audience does not match this resource`.

Le modèle ajoute un mappeur **Audience** (`oidc-audience-mapper`) à une portée de client nommée `mcp-gateway` ; son *Included Custom Audience* vaut `${GATEWAY_PUBLIC_URL}`. La portée est une portée **par défaut** du realm : chaque client l'obtient, quoi qu'il ait demandé, de sorte qu'un jeton émis par ce realm a toujours un `aud` égal à l'URL de la passerelle.

C'est une portée par défaut plutôt que facultative, parce que les clients diffèrent par ce qu'ils demandent et enregistrent, et qu'un jeton sans l'audience est refusé par la passerelle (le client boucle alors sur la connexion). Le coût est que chaque jeton du realm est valable auprès de la passerelle : gardez donc le realm dédié à la passerelle. Si d'autres applications doivent le partager, faites plutôt de `mcp-gateway` une portée facultative et vérifiez chaque client que vous prenez en charge.

Keycloak 26.8 dispose aussi d'une implémentation expérimentale des indicateurs de ressource (RFC 8707, `--features=resource-indicators`) qui fixerait l'audience à partir du paramètre `resource` qu'envoient ChatGPT et Cursor. Sans cette fonctionnalité, Keycloak n'utilise pas `resource` pour choisir l'audience ; le modèle n'en a pas besoin.

La passerelle annonce ces portées, et le realm doit permettre à un client anonyme d'obtenir chacune d'elles :

<!-- config-fragment: auth.scopes_advertised -->
```json
["mcp-gateway", "offline_access"]
```

Les clients d'IA demandent les portées figurant dans le défi `WWW-Authenticate`, à défaut celles du `scopes_supported` des métadonnées : les deux proviennent de `auth.scopes_advertised` ; plusieurs clients les envoient aussi comme `scope` de leur enregistrement dynamique. Ce que le README du modèle a démontré avec Keycloak 26.8.2 :

- **N'annoncez pas `openid`.** Ce n'est pas une portée de client Keycloak : un enregistrement dont le `scope` la contient est refusé (`403`, « Not permitted to use specified clientScope »), et VS Code envoie les portées du défi dans son enregistrement. Sans `openid`, Keycloak émet un jeton d'accès et pas de jeton d'identité, ce qui suffit à la passerelle.
- **Annoncez `offline_access`.** Claude l'ajoute à sa demande dès que Keycloak la liste dans `scopes_supported` (ce que Keycloak fait toujours). Un client enregistré avec un `scope` qui ne la contient pas ne l'obtient pas comme portée facultative, et sa requête d'autorisation échoue avec `invalid_scope`.
- **Annoncez quelque chose.** Sans portée dans le défi ni dans les métadonnées, Claude demande toutes les portées du `scopes_supported` de Keycloak, dont certaines que le client n'a pas, et Keycloak répond `invalid_scope` (<https://github.com/anthropics/claude-ai-mcp/issues/1084>).

La configuration exige au moins une entrée. Pour un autre serveur, n'annoncez que des portées qui y existent et qu'un client anonyme peut obtenir. Guide propre à Keycloak : <https://www.keycloak.org/securing-apps/mcp-authz-server>.

### Les revendications d'identité

La passerelle lit les revendications nommées dans `auth.identity`. Le modèle les ajoute par une portée de client `gateway-identity`, portée par défaut du realm. Le profil utilisateur ne laisse que les administrateurs modifier `username`, `email` et `org`, et l'inscription libre est désactivée, parce que quiconque peut modifier ces valeurs choisit la clé d'API qu'utilise une requête :

- `org` : un mappeur d'attribut (`oidc-usermodel-attribute-mapper`) de l'attribut utilisateur `org` vers la revendication `org`, une chaîne, dans le jeton d'accès. C'est `auth.identity.organization: "org"`.
- `groups` : un mappeur d'appartenance à des groupes (`oidc-group-membership-mapper`), revendication `groups`, `full.path` à true, de sorte que le sous-groupe `analysts` de `team-a` devient `/team-a/analysts` dans la revendication et qu'un groupe de premier niveau `analysts` devient `/analysts`. Des chemins complets, parce que deux groupes peuvent porter le même nom sous des parents différents : avec les seuls noms, les membres de `/team-a/analysts` et de `/team-b/analysts` porteraient la même revendication et un résolveur leur donnerait la même clé d'API. Faites la correspondance sur le chemin (`{"group": "/team-a/analysts"}` dans un fichier de correspondance statique). C'est `auth.identity.groups: "groups"`.
- `sub`, `email` et `email_verified` proviennent des portées standard du realm. Définissez `auth.identity.email_verified` sur `email_verified` : une adresse e-mail ne compte que si cette revendication vaut `true`.

### Durée de vie des jetons

L'`accessTokenLifespan` du modèle est de **300 secondes**. La passerelle ne peut pas révoquer un jeton : un jeton volé, ou le jeton d'un utilisateur que vous venez de désactiver, fonctionne jusqu'à son expiration ; cinq minutes bornent ce risque, et les clients renouvellent en arrière-plan (Claude renouvelle jusqu'à cinq minutes avant l'expiration, documentation de l'éditeur). Les sessions qui soutiennent les jetons de renouvellement : inactivité de 7 jours, maximum 30 jours, à la fois pour les sessions SSO et hors ligne (les clients MCP demandent `offline_access`) ; les jetons de renouvellement tournent (chaque renouvellement en renvoie un nouveau et le précédent est refusé). Le README du modèle contient le tableau complet et les raisons. Les jetons de 60 secondes du realm de développement existent pour les tests d'expiration et sont trop courts pour de vrais clients. La passerelle tolère `auth.clock_skew_seconds` de décalage d'horloge sur `exp`, `nbf` et `iat`.

Pour exclure un utilisateur immédiatement : désactivez-le, puis mettez fin à ses sessions et à ses sessions hors ligne dans la console d'administration. Les jetons déjà émis restent valables 5 minutes au plus.

### Hôtes de confiance, enregistrement et clients

Les clients s'enregistrent par **enregistrement dynamique de client** (anonyme) ou s'identifient par un **Client ID Metadata Document**.

- Le **DCR anonyme** est refusé par Keycloak sauf si les politiques d'enregistrement du realm l'autorisent. Le modèle conserve les politiques anonymes de Keycloak et définit la politique **Trusted Hosts** : chaque URI de redirection du client en cours d'enregistrement (et son `client_uri`) doit se trouver sur un hôte de confiance (`client-uris-must-match: true`), tandis que `host-sending-registration-request-must-match` vaut `false`, parce que la requête vient des serveurs des éditeurs ou de l'ordinateur de l'utilisateur lui-même, dont vous ne pouvez pas lister les adresses. Un enregistrement qui ne satisfait pas la politique est refusé ; le symptôme rapporté est un HTTP `403` sur la requête d'enregistrement. La **Max Clients Limit** est de 1000 (elle compte chaque client du realm) ; lorsqu'elle est atteinte, tout nouvel enregistrement échoue : surveillez donc le nombre de clients et supprimez ceux qui ne se connectent plus. Le **consentement** est exigé pour les clients enregistrés, et PKCE `S256` est exigé pour tout client créé anonymement. Un tel enregistrement est refusé s'il demande le flux implicite ou hybride ou l'octroi par mot de passe, et le point de terminaison de jeton du realm n'accorde que `authorization_code` et `refresh_token`, à tous les clients : les identifiants client (client credentials), l'octroi d'appareil (device grant) et l'octroi par mot de passe (y compris sur le client intégré `admin-cli` de Keycloak) sont refusés. Sans cela, un client enregistré sur un hôte de confiance avec un schéma personnalisé (`evilapp://claude.ai/cb` : Keycloak ne compare que l'hôte) obtenait un jeton pour la passerelle par le flux implicite, et un client enregistré avec les identifiants client en obtenait un sans aucun utilisateur. La section [client policies](../../../deploy/keycloak/README.md#client-policies-the-code-flow-with-pkce-nothing-else) du README donne le détail ; la suite de bout en bout les vérifie contre un vrai Keycloak.
- Les hôtes auxquels le modèle fait confiance (le README donne les URI de redirection, la page de l'éditeur et la date de chaque vérification, dans [son tableau](../../../deploy/keycloak/README.md#trusted-hosts-and-where-each-comes-from)) :

  | Hôte | Client |
  |---|---|
  | `claude.ai` | Claude (web, Desktop, mobile) |
  | `localhost`, `127.0.0.1` | Claude Code, VS Code, Cursor desktop (boucle locale) |
  | `chatgpt.com` | ChatGPT |
  | `global.consent.azure-apim.net` | Microsoft Copilot Studio (vérifiez l'URL de rappel qu'il vous montre ; si son hôte diffère, ajoutez-le) |
  | `vscode.dev`, `insiders.vscode.dev`, `code.visualstudio.com` | VS Code |
  | `www.cursor.com`, `anysphere.cursor-mcp` | Cursor web et agents cloud, Cursor desktop |

  Retirez les hôtes des clients que vous ne prenez pas en charge, et n'ajoutez que des hôtes que vous avez vérifiés dans la documentation à jour de l'éditeur. Keycloak ne compare que l'**hôte** d'une URI de redirection : le schéma, le port et le chemin ne sont pas examinés, les sous-domaines ne sont pas implicitement de confiance, et une seule URI non fiable fait refuser tout l'enregistrement. Le README liste ce que cela autorise et ce que cela refuse.
- La documentation de Claude note qu'avec le DCR, il enregistre un nouveau client à chaque nouvelle connexion : surveillez donc la limite « Max Clients » sur un realm très sollicité. L'enregistrement étant anonyme, n'importe qui peut aussi remplir cette limite délibérément, après quoi plus aucun client ne peut s'enregistrer : limitez le débit du chemin d'enregistrement (`/realms/<realm>/clients-registrations/`) dans votre proxy inverse (le README en donne un [exemple nginx](../../../deploy/keycloak/README.md#rate-limit-the-registration-endpoint) ; les clients hébergés s'enregistrent depuis les serveurs de leur éditeur, dimensionnez donc la limite pour l'ensemble de vos utilisateurs d'un même éditeur), surveillez le nombre de clients et supprimez ceux qui ne se connectent plus.
- Le **CIMD** est **expérimental** dans Keycloak (`--features=cimd`) et **désactivé dans le modèle**. `deploy/keycloak/cimd-client-policies.json` contient un profil de client et une politique pour Claude (il faut que `accept-public-client-with-confidential-client-only-grant` soit activé, dans la 26.8) que vous appliquez par l'API d'administration ; les étapes figurent dans la [section CIMD](../../../deploy/keycloak/README.md#cimd-experimental) du README. **Démarrer Keycloak avec `--features=cimd` modifie tous les clients, pas seulement Claude** : Keycloak annonce alors `client_id_metadata_document_supported: true` et ajoute `none` à `token_endpoint_auth_methods_supported`, avec ou sans politique, et les clients qui prennent en charge le CIMD l'utilisent. ChatGPT donne la priorité au CIMD et Keycloak ne prend pas en charge son document : sa connexion échoue donc (`Client not found`) à moins que l'application ChatGPT ne soit réglée pour utiliser le DCR ; VS Code a son propre document CIMD et a besoin de sa propre politique. Le DCR est la voie qui fonctionne avec le modèle tel quel.
- Keycloak avant 26.6 présentait une incompatibilité du DCR avec les clients MCP : utilisez la 26.6 ou une version ultérieure (la 26.8 est la version avec laquelle la passerelle est testée).
- Les métadonnées de Keycloak avec un émetteur à chemin inséré (`/.well-known/oauth-authorization-server/realms/<realm>`) sont servies depuis la 26.4.

Guide propre à Keycloak : <https://www.keycloak.org/securing-apps/mcp-authz-server>.

### Le côté passerelle

<!-- config-fragment: auth -->
```json
{
  "issuer": "https://idp.example.com/realms/example-publisher",
  "jwks_uri": "https://idp.example.com/realms/example-publisher/protocol/openid-connect/certs",
  "audiences": ["https://mcp.example.com/mcp"],
  "algorithms": ["RS256"],
  "required_scopes": [],
  "scopes_advertised": ["mcp-gateway", "offline_access"],
  "clock_skew_seconds": 5,
  "jwks_cache_ttl_seconds": 300,
  "jwks_max_stale_seconds": 3600,
  "jwks_fetch_timeout_seconds": 5,
  "identity": {
    "subject": "sub",
    "email": "email",
    "email_verified": "email_verified",
    "organization": "org",
    "groups": "groups"
  },
  "userinfo": null
}
```

- `issuer` doit être égal au `iss` du jeton, exactement (Keycloak : l'URL publique de votre realm, y compris le chemin `/realms/<realm>`).
- `jwks_uri` est le `.../protocol/openid-connect/certs` de Keycloak. Cela peut être une adresse interne, puisque seule la passerelle le récupère.
- `algorithms` : l'algorithme de signature par défaut d'un realm Keycloak est `RS256`.
- `required_scopes` : portées que le jeton doit porter. Avec une liste non vide, un jeton qui en est dépourvu reçoit `403 insufficient_scope`.
- L'ensemble de clés est mis en cache pendant `jwks_cache_ttl_seconds`. Si un renouvellement échoue, l'ensemble en cache continue de servir pendant `jwks_max_stale_seconds` supplémentaires ; passé ce délai, les requêtes reçoivent `503` avec `Retry-After`. Un jeton signé avec un `kid` inconnu déclenche un renouvellement, au plus un toutes les 10 secondes.

### Vérifier un jeton

Obtenez un jeton de votre realm avec n'importe quel client que vous maîtrisez, décodez sa charge utile et vérifiez : `iss` est égal à `auth.issuer` ; `aud` contient `public_url` ; `sub` est présent ; `email`, `email_verified`, `org` et `groups` sont ce qu'attend votre resolver ; `exp` est dans le futur. Appelez ensuite la passerelle avec ce jeton : `HTTP 403` avec le JSON-RPC `-31010` signifie que le jeton est correct et que l'utilisateur n'a pas de correspondance (chapitre 4) ; `401` signifie que le jeton n'est pas accepté, et l'`error_description` en donne la raison (`token expired`, `token audience does not match this resource`, `token issuer is not trusted`, `token signature is invalid`, `token algorithm is not accepted`, `token is missing a required claim`, `token signing key is unknown`).

## 6.2 Autres serveurs OAuth : une liste de contrôle

La passerelle fonctionne avec tout serveur qui fournit les prérequis du chapitre 2. Ce tutoriel n'a testé que **Keycloak 26.8.2** et **Ory Hydra v26.2** (tous deux dans la pile de développement, où la même suite de bout en bout s'exécute contre chacun). Il n'affirme rien au sujet d'un autre produit. Pour Auth0, Microsoft Entra ID, Okta, authentik ou un autre serveur, vérifiez vous-même chaque point avec un vrai jeton et un vrai client avant de vous y fier :

- [ ] Des métadonnées à une URL de découverte RFC 8414 ou OpenID Connect, listant `S256` dans `code_challenge_methods_supported`, ainsi que `authorization_endpoint`, `token_endpoint` et `jwks_uri`.
- [ ] Les jetons d'accès sont des **JWT** (et non des chaînes opaques) signés avec un algorithme asymétrique de la liste du chapitre 2, avec un `kid` qui correspond au JWKS.
- [ ] Leur `iss` est exactement l'émetteur que vous configurez, et `aud` contient exactement le `public_url` de la passerelle. Certains serveurs veulent que l'URL de la ressource soit déclarée d'abord (la documentation de Claude note que Microsoft Entra ID exige que l'URL du serveur MCP soit enregistrée comme Application ID URI, faute de quoi la demande de jeton échoue avec `AADSTS9010010`).
- [ ] Les portées que vous annoncez existent et peuvent être demandées par un client anonyme ou préenregistré, et le serveur les accepte dans une requête d'enregistrement (Keycloak y refuse `openid`) ; le serveur ne répond pas `invalid_scope` lorsque le client demande exactement ce que vous annoncez.
- [ ] Enregistrement des clients : DCR sans jeton préémis, ou CIMD avec `client_id_metadata_document_supported: true` et `none` dans `token_endpoint_auth_methods_supported`, ou clients préenregistrés avec les URI de redirection du chapitre 7. (Connu : le DCR d'authentik 2026.8 exige un jeton préémis et émet des jetons HS256 ; préenregistrez plutôt.)
- [ ] Le jeton porte un `sub` stable et les revendications sur lesquelles vous faites la correspondance (`email` avec `email_verified`, `org`, `groups`), aux chemins que vous configurez.
- [ ] Le point de terminaison de jeton accepte `application/x-www-form-urlencoded` et répond aux requêtes de découverte, d'enregistrement et de jeton bien en dessous de 10 secondes (la limite de Claude).
- [ ] Le serveur est joignable depuis les clouds des éditeurs d'IA (chapitre 2).

Suite : [7. Connecter les clients](07-clients.md).
