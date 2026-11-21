# 1. Ce qu'est la passerelle, et ce qu'elle n'est pas

## Ce qu'elle est

L'Alien MCP Gateway se place entre les clients d'IA et un serveur MCP Alien. Elle fait trois choses :

1. Elle est un **serveur de ressources** OAuth. Elle accepte les requêtes qui portent un jeton d'accès émis par **votre** serveur OAuth (par exemple Keycloak) et vérifie ce jeton localement : signature contre l'ensemble de clés publié (JWKS), émetteur, audience, expiration et, en option, portées (scopes) exigées.
2. Elle échange les identifiants. À partir du jeton vérifié, elle construit une identité (sujet, adresse e-mail vérifiée, organisation, groupes), demande à un **resolver** quelle clé d'API Alien correspond à cette identité, supprime tout ce que le client a envoyé pour s'authentifier et ajoute `Authorization: Bearer <cette clé>`.
3. Elle relaie en flux continu. Le trafic MCP part vers le serveur MCP Alien et la réponse revient au fur et à mesure, y compris les Server-Sent Events. Hormis les politiques facultatives du chapitre 8 (listes d'autorisation et de refus d'outils), la passerelle ne lit ni ne réécrit les réponses.

Vos utilisateurs se connectent avec leurs comptes habituels, votre point de terminaison MCP vit sur votre propre domaine, et Alien voit un utilisateur facturé par clé d'API.

## Ce qu'elle n'est pas

| Elle ne fait pas | Ce que cela signifie pour vous |
|---|---|
| Émettre des jetons ni enregistrer des clients | Il n'y a pas de serveur OAuth dans la passerelle. Vous en avez besoin d'un (chapitre 2). Elle publie uniquement les métadonnées de ressource protégée (Protected Resource Metadata, RFC 9728) qui désignent votre serveur OAuth. |
| Gérer les utilisateurs ou les clés | La création des organisations, des utilisateurs et des clés d'API sur Alien vous incombe, via l'API Alien (chapitre 3). La passerelle ne fait que rechercher une clé existante. Une correspondance absente est un refus explicite, jamais une clé de repli. |
| Porter une marque | Votre serveur OAuth affiche la page de connexion. Le nom du serveur et les instructions qu'un assistant affiche proviennent du serveur MCP Alien, pas de la passerelle. |
| Terminer le TLS | Le processus écoute en HTTP simple (`listen` ne comporte que `host` et `port`). Placez devant lui un ingress ou un proxy inverse muni d'un certificat. |
| Transmettre le jeton du client en amont | `Authorization`, les cookies, `X-Forwarded-*` et les autres en-têtes listés dans [`protocol.md`](../../protocol.md#headers) n'atteignent jamais Alien. Seule une liste d'autorisation d'en-têtes est transmise. |
| Transformer un `401` d'Alien en invite de connexion | Si Alien rejette la clé que la passerelle détient pour un utilisateur, le client reçoit une erreur JSON-RPC (`-31001`), jamais un défi OAuth. Sinon, le client boucle sur la réauthentification. |

## Architecture

```
                         (1) sign in, PKCE S256, token with aud = https://mcp.example.com/mcp
   ┌────────────┐ ─────────────────────────────────────────────────────►  ┌──────────────────────┐
   │ MCP client │                                                           │ your OAuth server     │
   │ Claude,    │ ◄─────────────────────────────────────────────────────  │ idp.example.com       │
   │ ChatGPT,.. │            access token (JWT)                             │ (Keycloak, ...)       │
   └─────┬──────┘                                                           └──────────┬───────────┘
         │ (2) MCP over HTTPS, Authorization: Bearer <JWT>                              │ (3) JWKS
         ▼                                                                              │
   ┌─────────────────────────────────────────────────────────────────────┐             │
   │ Alien MCP Gateway  (you host it: mcp.example.com)                   │ ◄───────────┘
   │  a. check the JWT: signature, iss, aud, exp/nbf, scopes             │
   │  b. identity = subject, verified e-mail, organization, groups       │
   │  c. resolver(identity) ─► Alien API key + upstream + labels, or deny│ ──► your webhook / database /
   │  d. drop client credentials, add Authorization: Bearer <Alien key>  │     Redis / mapping file
   │  e. stream the request, stream the answer back                      │
   └──────────────────────────────────────┬──────────────────────────────┘
                                          │ (4) MCP over HTTPS, one Alien API key per user
                                          ▼
                                 ┌─────────────────────┐
                                 │ Alien MCP server     │ ─► meters the call to the key's user
                                 └─────────────────────┘

   Set-up time only (chapter 3): you ──► Alien API ──► organizations, client-managed users, API keys
```

Deux détails de l'étape (1) comptent pour tout ce qui suit :

- L'audience du jeton (`aud`) doit contenir l'URL publique de la passerelle, exactement telle qu'écrite dans `public_url`. C'est ce qui fait du jeton un jeton « émis pour cette ressource ». Le chapitre 6 montre comment l'obtenir.
- La passerelle publie ses métadonnées uniquement à l'URL propre au chemin. Pour un `public_url` valant `https://mcp.example.com/mcp`, il s'agit de `https://mcp.example.com/.well-known/oauth-protected-resource/mcp`. L'URL au niveau de l'origine répond `404`.

## Une requête, pas à pas

1. Le client appelle `https://mcp.example.com/mcp` sans jeton. La passerelle répond `401` avec `WWW-Authenticate: Bearer resource_metadata="https://mcp.example.com/.well-known/oauth-protected-resource/mcp", scope="mcp-gateway offline_access"`.
2. Le client récupère ces métadonnées, y trouve `authorization_servers` (votre serveur OAuth, une seule entrée), lit les métadonnées propres à votre serveur, s'enregistre ou s'identifie, puis déroule le flux de code d'autorisation avec PKCE.
3. Le client rappelle la passerelle avec le jeton d'accès. La passerelle le vérifie, interroge le resolver et transmet la requête avec la clé Alien de l'utilisateur.
4. Alien impute l'appel à l'utilisateur propriétaire de la clé. Si l'organisation de cet utilisateur est parrainée, le flux du parrain montre également l'appel (chapitre 3).

Suite : [2. Prérequis](02-prerequisites.md).
