# 7. Connecter les clients

Chaque client attend de vous la même chose : l'**URL publique** de la passerelle (par exemple `https://mcp.example.com/mcp`). Il trouve tout le reste, votre serveur OAuth et les portées à demander, en appelant cette URL et en suivant le défi `401` et les métadonnées du [chapitre 1](01-what-it-is.md#une-requête-pas-à-pas).

Ce qui diffère, c'est la manière dont chaque client **s'enregistre** auprès de votre serveur OAuth, et l'**URI de redirection** que votre serveur doit accepter.

| Client | URL à saisir | Enregistrement | URI de redirection à autoriser | Documentation de l'éditeur |
|---|---|---|---|---|
| Claude (claude.ai, Desktop, mobile, Cowork) | L'URL publique, ajoutée comme connecteur personnalisé | CIMD, DCR, ou un identifiant de client (et un secret) que vous créez et saisissez | `https://claude.ai/api/mcp/auth_callback` | <https://claude.com/docs/connectors/building/authentication> |
| Claude Code | L'URL publique | CIMD (son propre document de métadonnées) | Une adresse de boucle locale sur n'importe quel port, `localhost` et `127.0.0.1` | même page |
| ChatGPT (connecteurs / apps) | L'URL publique | CIMD de préférence, ou DCR | `https://chatgpt.com/connector_platform_oauth_redirect` ou `https://chatgpt.com/connector/oauth/{callback_id}` | <https://developers.openai.com/apps-sdk/build/auth> |
| Microsoft Copilot Studio | L'URL publique, comme « Server URL » d'un outil MCP | OAuth 2.0 : découverte dynamique (DCR), dynamique, ou identifiant et secret de client manuels | L'URL de rappel que l'assistant affiche pour le connecteur | <https://learn.microsoft.com/en-us/microsoft-copilot-studio/mcp-add-existing-server-to-agent> |
| VS Code | L'URL publique dans `mcp.json` | Un identifiant de client préenregistré (`oauth.clientId`) ; le DCR et le CIMD figurent dans les recherches du projet, voir ci-dessous | Voir ci-dessous | <https://code.visualstudio.com/docs/copilot/reference/mcp-configuration> |
| Cursor | L'URL publique dans sa configuration MCP | DCR (client public), ou un identifiant de client statique dans `auth` | `https://www.cursor.com/agents/mcp/oauth/callback` et `http://localhost:8787/callback` | <https://cursor.com/docs/context/mcp> |

Tous utilisent le flux de code d'autorisation avec PKCE. Votre serveur doit laisser passer l'URI de redirection de chaque client, et les `policies.allowed_origins` de la passerelle doivent lister les **origines de navigateur** qui l'appellent (voir ci-dessous).

## Claude

- Ajoutez l'URL de la passerelle comme **connecteur personnalisé** (la page de l'éditeur ci-dessus décrit comment un connecteur personnalisé identifie Claude auprès de votre serveur : par le Client ID Metadata Document qu'héberge Anthropic, par l'enregistrement dynamique de client, ou avec un identifiant de client OAuth que vous avez enregistré et que l'utilisateur saisit dans la boîte de dialogue, avec un secret de client seulement si votre serveur en exige un).
- Claude exige un `401` (et non un en-tête sur un `200`) pour démarrer la connexion, n'utilise que la première entrée de `authorization_servers`, et envoie PKCE `S256` à chaque requête d'autorisation. La passerelle fait les trois.
- Claude demande les portées figurant dans le `scope` du défi, à défaut celles du `scopes_supported` des métadonnées ; sans aucune, il demande toutes les portées et échoue avec `invalid_scope`. Il ajoute aussi `offline_access` lorsque les métadonnées du serveur la listent : avec Keycloak, annoncez donc `mcp-gateway` et `offline_access`, et non `openid` (chapitre 6).
- **CIMD** : Claude ne l'utilise normalement que lorsque les métadonnées de votre serveur annoncent à la fois `"client_id_metadata_document_supported": true` et `"none"` dans `token_endpoint_auth_methods_supported`, et se rabat sinon sur le DCR. Le CIMD de Keycloak est expérimental (chapitre 6) : le DCR est la voie qui fonctionne avec lui aujourd'hui.
- Accessibilité et limite de 10 secondes : chapitre 2.
- Ajoutez `https://claude.ai` à `policies.allowed_origins` ; les exemples le font.
- **Claude Code** utilise une redirection de boucle locale sur un port éphémère. Votre serveur doit faire correspondre `http://localhost/callback` et `http://127.0.0.1/callback` en ignorant le port.

## ChatGPT

- Les métadonnées de votre serveur OAuth doivent lister `S256` dans `code_challenge_methods_supported`.
- ChatGPT préfère le CIMD lorsque votre serveur annonce `client_id_metadata_document_supported: true`, et s'enregistre dynamiquement si les métadonnées comportent un `registration_endpoint` (un enregistrement par connexion à un serveur MCP, réutilisé). **Keycloak ne prend pas en charge ChatGPT par le CIMD** (recherches du projet) ; utilisez le DCR, avec `chatgpt.com` dans les hôtes de confiance de Keycloak.
- ChatGPT envoie le paramètre `resource` à la fois dans la requête d'autorisation et dans la requête de jeton, et attend qu'il soit repris dans le jeton, typiquement comme `aud`. La page de l'éditeur ne dit pas quelle valeur ChatGPT place dans `resource`. Avec le mappeur d'audience du chapitre 6, l'`aud` du jeton est l'URL de la passerelle quoi que le client envoie, ce que la passerelle vérifie.
- URI de redirection : si votre serveur prend en charge l'identification de l'émetteur (RFC 9207 : `authorization_response_iss_parameter_supported: true` et `iss` dans chaque réponse d'autorisation, égal à l'`issuer` des métadonnées), ChatGPT utilise l'URI stable `https://chatgpt.com/connector_platform_oauth_redirect` ; sinon `https://chatgpt.com/connector/oauth/{callback_id}`, que la page de gestion du connecteur affiche. Copiez la valeur que ChatGPT vous montre dans la liste d'autorisation de votre serveur.
- ChatGPT n'utilise pas de clés d'API pour ces connecteurs (recherches du projet).

## Microsoft Copilot Studio

- Dans votre agent : **Tools**, **Add a tool**, **New tool**, **Model Context Protocol**. Renseignez le nom du serveur, une description et la **Server URL** (le `public_url` de la passerelle). Choisissez **OAuth 2.0** et l'une des options suivantes :
  - **Dynamic discovery** : le client découvre vos points de terminaison et s'enregistre lui-même (DCR). L'éditeur la présente comme la méthode la plus simple.
  - **Dynamic** : DCR, mais vous saisissez l'URL d'autorisation et le modèle d'URL du jeton.
  - **Manual** : vous enregistrez un client auprès de votre serveur et saisissez son identifiant de client, son secret de client, l'URL d'autorisation, le modèle d'URL du jeton, l'URL de renouvellement et (facultatif) les portées.
- Lorsqu'une URL de rappel est affichée, copiez-la dans les URI de redirection du client de votre serveur (ou dans les hôtes de confiance de votre serveur pour le DCR).
- Copilot Studio ne prend en charge que le transport Streamable HTTP (la passerelle relaie le transport sans le modifier).
- **Non confirmé, nécessite un tenant Copilot Studio pour être testé** : si Copilot Studio envoie PKCE `S256` en mode manuel, s'il envoie le paramètre `resource` (de sorte que votre `aud` soit correct sans le mappeur d'audience) et s'il prend en charge le CIMD. La page de l'éditeur ne le dit pas. Utilisez le mappeur d'audience (pour que `aud` soit correct dans tous les cas), et testez avec un tenant avant de promettre Copilot Studio à quiconque. L'URL de redirection exacte (une adresse `global.consent.azure-apim.net/redirect/...`, d'après les recherches du projet) est générée par connecteur : utilisez celle que l'assistant affiche.

## VS Code

- Ajoutez le serveur dans `mcp.json` avec son URL. Pour un serveur qui exige un **client OAuth préenregistré**, la page de l'éditeur documente `oauth` avec un `clientId` (un secret de client est stocké séparément, pas dans `mcp.json`), et le champ à plat `oauthClientId` dans le format portable `mcpServers`. VS Code gère le flux et ouvre un navigateur à la première connexion.
- **Non confirmé sur la page de l'éditeur** : l'enregistrement dynamique, le CIMD et les URI de redirection. Les recherches du projet listent `http://127.0.0.1:33418` et `https://vscode.dev/redirect` comme URI de redirection, et le document CIMD `https://vscode.dev/oauth/client-metadata.json`. Vérifiez-les par rapport à votre version de VS Code avant de vous y fier. Si vous préenregistrez un client, utilisez les URI de redirection que votre VS Code affiche ou les deux issues des recherches.
- VS Code met le serveur entier en état d'erreur pour tout statut HTTP de 300 ou plus (c'est pourquoi la passerelle répond aux limitations de débit et à la plupart des autres refus par un HTTP `200` avec une erreur JSON-RPC lorsqu'une réponse est possible ; voir [`configuration.md`](../../configuration.md#policiesrate_limits)).

## Cursor

- Cursor utilise le flux de code d'autorisation avec PKCE et lit les métadonnées de votre ressource protégée pour trouver le serveur d'autorisation et la ressource.
- Avec l'enregistrement dynamique, il s'enregistre comme client public (sans secret). Pour un serveur qui donne un identifiant de client fixe, placez un objet `auth` dans la configuration MCP avec `CLIENT_ID`, éventuellement `CLIENT_SECRET` (clients confidentiels uniquement) et `scopes` (lorsqu'il est omis, Cursor lit `scopes_supported` dans `/.well-known/oauth-authorization-server`).
- Enregistrez les deux rappels : `https://www.cursor.com/agents/mcp/oauth/callback` (web et Cursor Agents) et `http://localhost:8787/callback` (application de bureau). Cursor identifie le serveur par le paramètre OAuth `state`, de sorte que les deux URL servent tous les serveurs MCP.
- Les recherches du projet listent aussi `cursor://anysphere.cursor-mcp/oauth/callback` ; la page actuelle de l'éditeur ne le fait pas. **Non confirmé.**

## Origines autorisées

La passerelle répond `403` avec `-31008` (avant l'authentification) à une requête qui porte un en-tête `Origin` absent de `policies.allowed_origins`. Une requête sans en-tête `Origin` passe. Une liste vide refuse toute requête qui en porte un. Les entrées sont de la forme `scheme://host[:port]`, http ou https, sans chemin.

- La pile de développement autorise `https://claude.ai` et `http://localhost:6274` (le MCP Inspector).
- Les origines qu'envoient les autres clients n'ont pas été confirmées pour ce tutoriel. Si les appels d'un client sont refusés avec `-31008`, la ligne d'audit montre le résultat `origin_refused` mais pas l'origine elle-même : lisez l'en-tête `Origin` de la requête refusée (outils de développement du navigateur ou journal de votre proxy) et ajoutez exactement cette origine.

Suite : [8. Exploitation](08-operations.md).
