# 3. Créer le côté Alien

La passerelle ne fait que rechercher une clé d'API Alien existante. Avant qu'elle puisse servir qui que ce soit, vous devez créer, sur la plateforme Alien, un **utilisateur géré par le client** (client-managed) avec une **clé d'API** pour chaque personne (ou, si vous acceptez les conséquences exposées au chapitre 4, pour chaque groupe de personnes) autorisée à utiliser la passerelle, au sein d'une **organisation gérée** dont la consommation est payée par votre groupe parrain.

Les appels de cette page sont ceux de `scripts/dev_fixture.py`, le script qui exécute ces étapes sur une plateforme Alien pour les tests de bout en bout de la passerelle (lisez-le si vous voulez les automatiser), vérifiés contre le code du backend de la plateforme là où la section le précise : voir [Ce que montre le code du backend](#ce-que-montre-le-code-du-backend) pour savoir qui peut faire quoi et connaître le statut de chaque appel.

## Les objets

| Objet | Ce que c'est |
|---|---|
| Organisation parrain | Votre organisation sur Alien. Elle possède les jeux de données (sur un cluster) que vos utilisateurs liront. |
| Compte de service parrain | Un utilisateur géré par le client de l'organisation parrain, avec le rôle `org-admin`, authentifié par une clé `api-secret`. Tous les appels ci-dessous s'exécutent sous cet utilisateur. |
| Grant | Une permission nommée de lire un jeu de données, détenue par l'organisation parrain. |
| Groupe parrainé | Un groupe de l'organisation parrain qui détient le grant et les organisations clientes, avec `isSponsored` à true. |
| Organisation gérée | Une organisation cliente créée par la route d'organisation gérée du groupe. Elle est rattachée au groupe par ce même appel. |
| Utilisateur géré par le client | Un utilisateur créé au sein d'une organisation gérée par l'API, et non par invitation par e-mail. |
| Clé d'API | Un jeton de portée `api-key` émis pour un utilisateur géré par le client. La passerelle l'envoie sous la forme `Authorization: Bearer oat_...`. |

Deux faits à connaître d'abord :

- **La suppression d'une organisation est refusée dès qu'elle a un historique comptable** (`ORGANIZATION_HAS_LEDGER_HISTORY`) : une organisation gérée dont la consommation a été mesurée reste en place. Nommez les organisations avec soin, et cherchez une organisation par son nom avant d'en créer une (le script de fixture le fait).
- L'organisation parrain (un compte **enterprise**), le cluster privé qui contient vos jeux de données et le compte de service doivent exister avant de commencer : le script de fixture prend en entrée l'identifiant de l'organisation, celui du cluster, celui du jeu de données et la clé du compte de service. Qui crée quoi est expliqué dans [Ce que montre le code du backend](#ce-que-montre-le-code-du-backend).

## Conventions utilisées dans les exemples

- `$ALIEN_API` est l'URL de base de l'API publique d'Alien, `https://api.alien.club`. (La variable ne sert qu'à raccourcir les exemples ; n'utilisez une autre valeur que si Alien vous fournit un environnement différent.)
- Chaque réponse est une enveloppe `{"success": true, "data": ..., "meta": ...}`. Un statut non 2xx, ou un `success` différent de `true`, est une erreur. Un appel en échec porte un objet `error` avec un `name`.
- L'authentification est `Authorization: Bearer <clé>`. Pour garder une clé hors de la liste des processus, placez l'en-tête dans un fichier et laissez curl le lire avec `-H @fichier` :

```bash
umask 077
printf 'Authorization: Bearer %s\n' "$(cat sponsor.key)" > sponsor.hdr   # printf is a shell built-in: the key is not in any argv
```

- Les appels qui agissent au sein de l'organisation parrain portent aussi `x-organization-id: <identifiant de l'organisation parrain>`. Les appels sur `/organizations/{id}/users...` n'en portent pas.
- Les exemples écrivent `SPONSOR_ORG`, `GROUP_ID`, `ORG_ID`, `USER_ID` pour les identifiants renvoyés par les appels précédents.

```bash
ALIEN_API=https://api.alien.club
SPONSOR_ORG=<your sponsor organization id>
```

## 3.1 Vérifier le compte de service

<!-- not-config -->
```bash
curl -sS --max-time 30 -H @sponsor.hdr -H 'Accept: application/json' "$ALIEN_API/users/me"
```

Résultat attendu : `success` à true et `data` avec l'`id` de l'utilisateur et son `userType` (`client-managed` pour un compte de service). Un `401` signifie que la clé est fausse ou révoquée.

## 3.2 Une seule fois : le grant et le groupe parrainé

Ignorez cette étape si vos jeux de données sont déjà partagés avec un groupe parrainé.

Créez le grant, une permission de lecture sur un jeu de données (`12` et `345` sont des identifiants d'exemple : utilisez les identifiants numériques de votre cluster et de votre jeu de données) :

<!-- not-config -->
```json
{
  "name": "example-publisher-dataset-read",
  "description": "read one dataset",
  "abilities": [
    {
      "path": [
        {"resource": "cluster", "resourceId": 12},
        {"resource": "dataset", "resourceId": 345}
      ],
      "accessType": "read"
    }
  ]
}
```

```bash
curl -sS --max-time 30 -H @sponsor.hdr -H "x-organization-id: $SPONSOR_ORG" \
  -H 'Content-Type: application/json' -d @grant.json "$ALIEN_API/grants"
```

Résultat attendu : `data.id` est l'identifiant du grant. Le script de fixture liste d'abord les grants (`GET /grants`) et réutilise celui qui porte le même nom ; une création peut répondre `409`.

Créez le groupe :

<!-- not-config -->
```bash
curl -sS --max-time 30 -H @sponsor.hdr -H "x-organization-id: $SPONSOR_ORG" \
  -H 'Content-Type: application/json' \
  -d '{"name": "example-publisher-sponsored", "description": "client organizations paid by example-publisher"}' \
  "$ALIEN_API/groups"
```

Résultat attendu : `data.status` vaut `created` et `data.group.id` est l'identifiant du groupe (`GROUP_ID`).

Rattachez le grant au groupe (le grant doit être rattaché avant que le groupe puisse être parrainé) :

<!-- not-config -->
```bash
curl -sS --max-time 30 -H @sponsor.hdr -H "x-organization-id: $SPONSOR_ORG" \
  -H 'Content-Type: application/json' -d "{\"grantIds\": [$GRANT_ID]}" \
  "$ALIEN_API/groups/$GROUP_ID/grants"
```

Rendez le groupe parrainé. Le backend refuse de créer une organisation gérée dans un groupe qui n'est pas encore parrainé (`409` `GROUP_NOT_SPONSORED`), et refuse de parrainer un groupe dont les grants désignent autre chose que des données de cluster (un grant dont les chemins d'abilities commencent par un `cluster`, comme celui ci-dessus) :

<!-- not-config -->
```bash
curl -sS --max-time 30 -X PATCH -H @sponsor.hdr -H "x-organization-id: $SPONSOR_ORG" \
  -H 'Content-Type: application/json' -d '{"isSponsored": true}' "$ALIEN_API/groups/$GROUP_ID"
```

Relisez le groupe avec `GET /groups/$GROUP_ID` et vérifiez que `isSponsored` vaut `true`. L'organisation parrain doit être une organisation enterprise, faute de quoi cet appel est refusé.

## 3.3 Une organisation gérée par client

Faites-le une fois par organisation cliente, après l'étape 3.2 (le groupe doit être parrainé). La route crée l'organisation, en fait l'appelant le propriétaire et la rattache au groupe en un seul appel. Un nom ou un slug déjà utilisé par une autre organisation répond `409` ; le nom est comparé sans tenir compte de la casse :

<!-- not-config -->
```bash
curl -sS --max-time 30 -H @sponsor.hdr -H "x-organization-id: $SPONSOR_ORG" \
  -H 'Content-Type: application/json' \
  -d '{"name": "example-publisher-managed-client-x", "slug": "example-publisher-managed-client-x"}' \
  "$ALIEN_API/groups/$GROUP_ID/organizations/managed"
```

Résultat attendu : `data.id` est l'identifiant de l'organisation (`ORG_ID`) et `data.accountType` vaut `managed`. Le script de fixture s'arrête si une organisation qu'il a trouvée n'est pas `managed`.

Pour rechercher d'abord une organisation existante, listez les organisations du groupe et faites la correspondance sur `name` :

<!-- not-config -->
```bash
curl -sS --max-time 30 -H @sponsor.hdr -H "x-organization-id: $SPONSOR_ORG" \
  "$ALIEN_API/groups/$GROUP_ID/organizations"
```

## 3.4 Un utilisateur géré par le client par personne

Appelez sous le compte de service, sans `x-organization-id` :

<!-- not-config -->
```bash
curl -sS --max-time 30 -H @sponsor.hdr -H 'Content-Type: application/json' \
  -d '{"userType": "client-managed", "email": "alice@managed-client-x.test", "description": "Alice, example-publisher"}' \
  "$ALIEN_API/organizations/$ORG_ID/users"
```

Résultat attendu : `data.id` est l'identifiant de l'utilisateur (`USER_ID`).

L'adresse e-mail de l'utilisateur sur Alien n'a pas à être celle avec laquelle la personne se connecte à votre serveur OAuth. La passerelle ne les compare jamais : votre resolver fait correspondre l'identité issue de votre serveur OAuth (par exemple l'adresse e-mail de connexion) à la clé de cet utilisateur. Le script de fixture utilise volontairement des adresses différentes (`alice@client-x.test` chez le fournisseur d'identité, `alice@managed-client-x.test` sur Alien) et enregistre les deux. Pour trouver un utilisateur existant, faites `GET /organizations/$ORG_ID/users` et la correspondance sur `email` (insensible à la casse) ; vérifiez que `userType` vaut `client-managed`.

## 3.5 Rattacher une organisation qui existe déjà (facultatif)

Les organisations qui existent déjà (non créées par l'étape 3.3) sont rattachées avec `POST /groups/$GROUP_ID/organizations` et `{"organizationIds": [..]}` (mêmes en-têtes). Un groupe parrainé peut en refuser certaines ; rien n'est alors rattaché.

## 3.6 Émettre la clé d'API

<!-- not-config -->
```bash
umask 077
curl -sS --max-time 30 -H @sponsor.hdr -H 'Content-Type: application/json' \
  -d '{"name": "mcp-gateway", "scope": "api-key"}' \
  "$ALIEN_API/organizations/$ORG_ID/users/$USER_ID/tokens" > minted.json
```

Résultat attendu : `data.token` est la clé (elle commence par `oat_`) et `data.identifier` est l'identifiant du jeton, dont vous avez besoin pour le révoquer. Le script de fixture lit `token` dans cette réponse et l'écrit dans un fichier de mode 600. Faites de même : conservez l'identifiant, écrivez la clé dans son propre fichier (le resolver `static` et le webhook d'exemple du chapitre 4 lisent une clé par fichier), déplacez-la vers votre magasin de secrets, et supprimez `minted.json` :

<!-- not-config -->
```bash
umask 077
sed -n 's/.*"identifier": *\([0-9]*\).*/\1/p' minted.json                 # the token identifier, for 3.9
sed -n 's/.*"token": *"\([^"]*\)".*/\1/p' minted.json > alice.key       # the key, one per file
rm minted.json
```

`api-key` est la portée que le script de fixture utilise pour les clés que détient la passerelle. La clé du compte de service parrain a la portée `api-secret`. Ne donnez à la passerelle que des clés `api-key` : la clé du compte de service peut créer des organisations, des utilisateurs et des clés, et doit rester hors de la passerelle.

## 3.7 Vérifier la clé

Appelez `GET /users/me` **avec la nouvelle clé** (et non celle du compte de service) :

<!-- not-config -->
```bash
umask 077
printf 'Authorization: Bearer %s\n' "$(cat alice.key)" > alice.hdr
curl -sS --max-time 30 -H @alice.hdr -H 'Accept: application/json' "$ALIEN_API/users/me"
```

Résultat attendu : `data.id` est égal à `USER_ID`, `data.userType` vaut `client-managed`, `data.currentOrganizationId` est égal à `ORG_ID`, et `data.currentAccessToken.abilities` liste ce que la clé peut faire. Le script de fixture refuse une clé qui échoue à l'un de ces contrôles. Un `401` signifie que la clé n'est pas acceptée.

Pour éprouver la clé contre le serveur MCP Alien lui-même, envoyez-lui un `initialize` MCP (c'est ce que fait en premier `call_mcp_tool` dans le script de fixture). L'URL est celle du serveur MCP que vous a donnée Alien :

<!-- not-config -->
```bash
curl -sS --max-time 30 -H @alice.hdr -H 'Accept: application/json, text/event-stream' \
  -H 'Content-Type: application/json' "$ALIEN_MCP_URL" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-11-25","capabilities":{},"clientInfo":{"name":"setup-check","version":"0"}}}'
```

## 3.8 Vérifier la facturation

Chaque appel est imputé à l'utilisateur de la clé et, lorsque le groupe est parrainé, apparaît aussi dans le flux du parrain. Lisez le flux MCP de l'organisation (appelez sous le compte de service, avec l'identifiant de l'organisation cliente dans `x-organization-id`) :

<!-- not-config -->
```bash
curl -sS --max-time 30 -H @sponsor.hdr -H "x-organization-id: $ORG_ID" \
  "$ALIEN_API/organizations/$ORG_ID/consumption/events?first=100&sourceType=mcp&start=2026-01-01T00:00:00%2B00:00"
```

Résultat attendu : `data.events` avec, pour chaque appel, `user.id` (l'utilisateur géré par le client), `organizationId`, `calledAt`, `jobId` et `sponsored` (avec `groupId` et `organizationId` lorsque le parrainage s'est appliqué). `data.pageInfo` contient `hasNextPage` et `endCursor` ; passez `after=<endCursor>` pour la page suivante. La mesure est asynchrone : le script de fixture interroge avec un délai croissant (backoff) pendant 60 secondes au plus. Le flux propre du parrain est `GET /organizations/$SPONSOR_ORG/consumption/events` avec `x-organization-id: $SPONSOR_ORG` ; cherchez les événements dont `sponsored.groupId` est votre groupe.

## 3.9 Révoquer une clé

Listez les clés de l'utilisateur, puis supprimez-en une par son `identifier` :

<!-- not-config -->
```bash
curl -sS --max-time 30 -H @sponsor.hdr "$ALIEN_API/organizations/$ORG_ID/users/$USER_ID/tokens"
curl -sS --max-time 30 -X DELETE -H @sponsor.hdr \
  "$ALIEN_API/organizations/$ORG_ID/users/$USER_ID/tokens/$TOKEN_IDENTIFIER"
```

La liste renvoie l'`identifier` de chaque clé et son `name`, que la plateforme préfixe par le type de la clé : une clé émise avec le nom `mcp-gateway` apparaît sous la forme `API_KEY: mcp-gateway`. Faites la correspondance sur l'identifiant que vous avez conservé à l'émission. Une clé révoquée est refusée par Alien (`401` sur `/users/me`) ; une passerelle qui la détient encore répond à ses clients par `-31001` jusqu'à ce que l'entrée du cache du resolver soit supprimée (le chapitre 8 explique comment effectuer une rotation sans cet intervalle).

## Ce que vous avez maintenant

Pour chaque personne : un identifiant d'utilisateur Alien, un identifiant d'organisation et une clé (`oat_...`) dans votre magasin de secrets. Le chapitre 4 transforme ces éléments en le resolver qu'utilise la passerelle.

## Ce que montre le code du backend

Lu dans les sources du backend à la date du 2026-11-20. Rien de ce qui suit n'a été exécuté contre une API réelle pour ce chapitre.

**Quel compte et quelle clé.**

- La création d'utilisateurs et de clés sous une organisation (`POST /organizations/{id}/users` et `.../users/{user}/tokens`) n'est permise que lorsque la clé de l'appelant porte l'ability `user:write`, que l'appelant est **administrateur de cette organisation ou plus**, et que le type de l'organisation est **enterprise ou managed**. Une clé de portée `api-key` n'a que `user:read` : la création d'utilisateurs et de clés exige la portée `api-secret` (ou une clé sans restriction). Les routes de groupe de ce chapitre exigent l'ability `grant:write`, que `api-secret` possède et que `api-key` n'a pas.
- La route d'organisation gérée exige à la fois `grant:write` et `user:write`, ainsi que les droits d'administrateur sur l'organisation du groupe.
- Un groupe ne peut être parrainé que si son organisation est une organisation **enterprise**. Le type de compte d'une organisation n'est modifié que par les **administrateurs de la plateforme** (la route de mise à jour refuse tous les autres : « Only platform administrators can update organization account type, limits and internal status »). **L'organisation parrain devient donc un compte enterprise par l'action d'Alien, à l'intégration : demandez à Alien.**
- Le **compte de service parrain** est un utilisateur `client-managed` créé au sein de l'organisation parrain par `POST /organizations/{sponsor}/users` avec `{"userType": "client-managed", "email": ..., "description": ...}`, appelé par un administrateur de cette organisation avec une clé qui possède `user:write` ; sa clé est ensuite émise avec `POST .../users/{id}/tokens` et `{"name": ..., "scope": "api-secret"}`. Le code ne dit pas comment s'obtient la première clé d'administrateur de l'organisation parrain : demandez à Alien. Un client peut donc créer le compte de service et sa clé par l'API, une fois qu'Alien a fait de l'organisation un compte enterprise et a remis à l'un de ses administrateurs une clé autorisée à écrire des utilisateurs.
- L'adresse e-mail du compte de service doit être **inutilisée sur toute la plateforme** : créer un utilisateur `client-managed` avec une adresse e-mail qui appartient déjà à un compte quelconque répond `409` `EMAIL_ALREADY_EXISTS` (la recherche se fait sur l'adresse seule, toutes organisations confondues). Il en va de même pour chaque utilisateur par personne de l'étape 3.4. L'adresse doit être valide ; elle n'a pas besoin de recevoir de courrier (le code n'en envoie aucun aux utilisateurs `client-managed`).
- Portées de clé acceptées à l'émission : `api-key` (la valeur par défaut lorsque `scope` est omis) et `api-secret` ; toute autre valeur répond `400`. Un `expiresIn` facultatif (un nombre) fixe une expiration ; sans lui, le code n'en fixe aucune. `api-key` ne porte que des abilities de lecture (jetons d'accès, modèles d'IA, sessions de discussion, clusters, collections, jeux de données, entrées, fichiers, workflows, grants, jobs, licences, configurations MCP, nœuds, fournisseurs, rôles, étiquettes, utilisateurs).

**Statut de chaque appel de création.** Les contrôleurs du backend envoient leurs réponses de succès par un unique utilitaire qui ne fixe aucun code de statut : ces appels répondent donc par la valeur par défaut du framework, `200`, et non `201` ; aucun chemin d'erreur de ces appels n'est un `2xx`. Considérez tout `2xx` comme un succès, comme le fait le script de fixture. Les échecs utilisent les statuts suivants : `400` paramètre ou portée de clé invalide, `404` introuvable, `409` existe déjà ou non parrainé (`EMAIL_ALREADY_EXISTS`, `ORGANIZATION_NAME_ALREADY_EXISTS`, `ORGANIZATION_SLUG_ALREADY_EXISTS`, `GROUP_NOT_SPONSORED`). `POST /groups` fait exception pour un nom déjà pris : il répond par un succès avec `data.status` valant `name-taken` (et non un statut d'erreur), ce qui explique que l'étape 3.2 vérifie `data.status`. Ces statuts proviennent du code, non d'une exécution : confirmez-les sur vos propres appels.

**Une clé peut-elle être affichée de nouveau ?** Non. La clé brute ne figure que dans la réponse de l'appel d'émission (`data.token`) ; la liste des clés d'un utilisateur renvoie `identifier`, `name`, `abilities`, `lastUsedAt`, `expiresAt` et aucune clé. Une clé perdue se remplace en en émettant une autre et en révoquant la première.

**Disponibilité.** La route d'organisation gérée et le type de compte `managed` sont disponibles sur `https://api.alien.club` depuis le backend 0.43 (novembre 2026).

## Ce que ce chapitre n'a pas pu confirmer

- Comment la première clé d'administrateur d'une organisation parrain est délivrée, et quand l'organisation parrain devient un compte enterprise : demandez à Alien.
- Les statuts ci-dessus ont été lus, non observés.

Suite : [4. Choisir un resolver](04-resolvers.md).
