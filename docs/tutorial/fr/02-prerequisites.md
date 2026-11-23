# 2. Prérequis

La passerelle n'est pas un serveur OAuth : un serveur compatible est donc un prérequis du produit. Vérifiez-le avant de déployer quoi que ce soit.

## Ce que votre serveur OAuth doit fournir

| # | Exigence | Pourquoi | Comment le vérifier |
|---|---|---|---|
| 1 | Des métadonnées de découverte : métadonnées de serveur d'autorisation RFC 8414 ou découverte OpenID Connect, annonçant `S256` dans `code_challenge_methods_supported` | Les clients d'IA les lisent pour trouver les points de terminaison, et refusent de démarrer un flux sans PKCE `S256` | `curl -s https://idp.example.com/realms/example-publisher/.well-known/openid-configuration`, puis lire `code_challenge_methods_supported`, `authorization_endpoint`, `token_endpoint`, `jwks_uri` |
| 2 | Des jetons d'accès JWT signés avec un algorithme asymétrique (`RS256`, `RS384`, `RS512`, `PS256`, `PS384`, `PS512`, `ES256`, `ES256K`, `ES384`, `ES512` ou `EdDSA`) et un JWKS publié | La passerelle valide les jetons localement. Elle n'appelle pas l'introspection de jetons. `HS*` et `none` sont refusés au démarrage. Les jetons opaques ne peuvent pas être validés, avec ou sans userinfo | Décoder un jeton obtenu : le `alg` et le `kid` de l'en-tête, et la clé située à `jwks_uri` |
| 3 | L'URL de la passerelle dans le `aud` du jeton | La passerelle n'accepte que les jetons dont le `aud` contient l'une des valeurs de `auth.audiences`, et `auth.audiences` doit contenir `public_url` | Décoder un jeton et lire `aud` (Keycloak y met `account`, sauf si vous configurez un mappeur d'audience, chapitre 6) |
| 4 | Une voie d'enregistrement de clients utilisable par les clients d'IA : enregistrement dynamique de client anonyme (Dynamic Client Registration, RFC 7591), Client ID Metadata Documents (CIMD), ou clients que vous préenregistrez pour chaque client d'IA | Chaque client d'IA a besoin d'un identifiant de client auprès de votre serveur. Le chapitre 7 indique quels clients utilisent quelle voie | Les métadonnées montrent `registration_endpoint` (DCR) et `client_id_metadata_document_supported` (CIMD) |
| 5 | Des revendications d'identité (claims) : un sujet stable dans le jeton d'accès, et les revendications sur lesquelles vous faites la correspondance (`email` avec `email_verified`, une revendication d'organisation, des groupes) dans le jeton d'accès ou, si vous activez `auth.userinfo`, au point de terminaison userinfo | Le resolver ne peut décider que sur ce que le jeton ou le point de terminaison userinfo porte. Les chemins de revendications sont configurables dans `auth.identity`, y compris imbriqués comme `ext.org` | Décoder un jeton et y chercher les revendications |
| 6 | Les portées que vous annoncez existent sur votre serveur | La passerelle annonce `auth.scopes_advertised` dans le défi et dans ses métadonnées. Un client demande alors ces portées | Comparer `scopes_advertised` avec la liste des portées du serveur |

Remarques sur le tableau :

- La passerelle ne fait aucune découverte de son côté. Vous écrivez `auth.issuer` et `auth.jwks_uri` dans la configuration. Le `iss` du jeton doit être strictement égal à `issuer`, caractère pour caractère.
- Claude n'utilise que la première entrée de `authorization_servers`. La passerelle en publie toujours exactement une : `auth.issuer`.
- Pour un émetteur qui comporte un chemin (c'est le cas des realms Keycloak), les clients sondent plusieurs emplacements de métadonnées. La spécification MCP du 2025-11-25 donne l'ordre pour l'émetteur `https://idp.example.com/realms/x` : `/.well-known/oauth-authorization-server/realms/x`, puis `/.well-known/openid-configuration/realms/x`, puis `/realms/x/.well-known/openid-configuration`. Votre serveur doit répondre à l'un d'eux.
- Le point de terminaison userinfo est facultatif. Si vos jetons d'accès ne portent que `sub` (fréquent avec Auth0 et Okta), `auth.userinfo` fait appeler ce point de terminaison par la passerelle, avec le jeton vérifié, pour lire les autres revendications (chapitre 6.3). Il fournit des revendications, il ne valide jamais le jeton : la passerelle vérifie toujours un JWT localement, et l'introspection de jetons n'est toujours pas utilisée.
- `email` n'est considéré comme fiable que si la revendication désignée par `auth.identity.email_verified` est le booléen `true`. Si vous mettez `email_verified` à `null`, une adresse e-mail n'est jamais considérée comme fiable et l'identité n'en a pas : un critère `email` ne peut donc pas fonctionner.
- Incompatibilité connue : authentik 2026.8 exige un jeton porteur préémis pour l'enregistrement dynamique de client, et les clients qu'il crée signent leurs jetons avec leur propre secret (HS256) sous un émetteur propre à chaque application, de sorte qu'un serveur de ressources ne peut pas les vérifier par un JWKS. Avec authentik, préenregistrez plutôt les clients d'IA.

Le chapitre 6 donne une configuration détaillée pour Keycloak et une liste de contrôle pour les autres serveurs.

## Accessibilité réseau

Les clients d'IA ne sont pas sur votre réseau.

- **Claude** (claude.ai, Desktop, mobile, Cowork) appelle la passerelle et votre serveur OAuth depuis le cloud d'Anthropic. Le trafic sortant d'Anthropic provient de `160.79.104.0/21` ([adresses IP d'Anthropic](https://platform.claude.com/docs/en/api/ip-addresses)). Votre passerelle **et** votre serveur OAuth doivent être joignables depuis cette plage, y compris à travers tout WAF placé devant le fournisseur d'identité. Un serveur OAuth bloqué se manifeste ainsi : la passerelle reçoit la première requête et le serveur OAuth ne voit aucun trafic.
- Claude laisse **10 secondes** à vos points de terminaison de découverte, d'enregistrement et de jeton pour répondre, et 30 secondes aux requêtes de renouvellement. Une réponse plus lente est traitée comme un flux en échec (documentation de l'éditeur : <https://claude.com/docs/connectors/building/authentication>).
- **Microsoft Copilot Studio** : le point de terminaison de jeton est atteint depuis Azure (une note issue des recherches du projet, non confirmée par une page de l'éditeur). **ChatGPT** : l'origine de ses requêtes n'a pas été confirmée pour ce tutoriel. Si vous filtrez par adresse, consultez la documentation à jour de chaque éditeur.
- La passerelle joint le JWKS de votre serveur OAuth (`auth.jwks_uri`), le serveur MCP Alien (`upstreams.*.url`) et les systèmes d'appui de votre resolver (webhook, base de données, Redis). L'accès sortant vers ces destinations doit être ouvert depuis le réseau de la passerelle. Le JWKS peut être une URL interne : seule la passerelle le récupère, donc `jwks_uri` peut différer de l'`issuer` public.

## TLS

- Les clients exigent `https` pour votre URL publique. Terminez le TLS devant la passerelle (ingress, répartiteur de charge ou proxy inverse). La passerelle elle-même sert du HTTP simple sur `listen.host`:`listen.port`.
- Le resolver webhook exige une URL `https` sauf si `allow_insecure_http` vaut true, ce qui est réservé aux environnements de développement, car la réponse du webhook transporte la clé d'API.
- Utilisez des certificats d'une autorité de certification publique pour votre serveur OAuth et pour le nom public de la passerelle. Ce tutoriel n'a testé aucune autorité privée avec un client d'IA.
- Le proxy placé devant la passerelle ne doit ni mettre les réponses en tampon ni couper les flux silencieux. Voir le chapitre 5.

## Liste de contrôle avant de poursuivre

- [ ] Je peux récupérer les métadonnées de mon serveur OAuth et elles mentionnent `S256`.
- [ ] Je sais comment mes clients d'IA s'enregistreront (DCR, CIMD ou identifiant de client préenregistré).
- [ ] Je peux produire un jeton qui porte l'URL de la passerelle dans `aud` (ou je le ferai au chapitre 6).
- [ ] Les jetons portent un sujet stable et les revendications sur lesquelles je veux faire la correspondance.
- [ ] L'URL publique de la passerelle et mon serveur OAuth sont joignables depuis les clouds des éditeurs.
- [ ] J'ai un nom DNS et un certificat pour la passerelle.

Suite : [3. Le côté Alien](03-alien-side.md).
