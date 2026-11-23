# Alien MCP Gateway : tutoriel d'installation

[English version](../en/README.md)

Ce tutoriel s'adresse à un éditeur (ou à toute organisation) qui exploite lui-même l'Alien MCP Gateway devant des serveurs MCP Alien, sous son propre domaine et avec sa propre authentification. Il vous conduit de zéro à un déploiement fonctionnel auquel Claude, ChatGPT, Microsoft Copilot Studio, VS Code et Cursor peuvent se connecter.

Tous les exemples utilisent des noms génériques : `example-publisher`, `mcp.example.com` (votre passerelle), `idp.example.com` (votre serveur OAuth), `keys.example.com` (votre webhook). Remplacez-les par les vôtres. Chaque clé de configuration, point de terminaison, commande et code d'erreur de ces pages a été lu dans le code de la passerelle, dans ses documents de référence ([`configuration.md`](../../configuration.md), [`protocol.md`](../../protocol.md), [`config.schema.v1.json`](../../config.schema.v1.json)), dans le chart Helm ou dans les scripts de ce dépôt. Lorsqu'une affirmation provient de la documentation d'un éditeur tiers, l'URL de cette documentation est indiquée. Lorsqu'un point n'a pas pu être confirmé, le texte le dit.

| Chapitre | Vous apprenez |
|---|---|
| [1. Ce qu'est la passerelle, et ce qu'elle n'est pas](01-what-it-is.md) | Le rôle de la passerelle, ce qu'elle ne fait jamais, l'architecture |
| [2. Prérequis](02-prerequisites.md) | Ce que votre serveur OAuth doit fournir, réseau et TLS |
| [3. Créer le côté Alien](03-alien-side.md) | Les appels d'API exacts qui créent organisations, utilisateurs et clés |
| [4. Choisir un resolver](04-resolvers.md) | Comment un utilisateur connecté devient une clé Alien ; écrire un webhook |
| [5. Déploiement](05-deploying.md) | Helm, docker compose, secrets, haute disponibilité |
| [6. Votre serveur OAuth](06-oauth-server.md) | Configuration de Keycloak, et liste de contrôle pour les autres serveurs |
| [7. Connecter les clients](07-clients.md) | Ce qu'il faut saisir dans chaque client d'IA |
| [8. Exploitation](08-operations.md) | Rotation des clés, observabilité, limites, mises à niveau |
| [9. Dépannage](09-troubleshooting.md) | Symptôme, cause, correctif |

Les fichiers utilisés par le tutoriel se trouvent dans [`../examples/`](../examples/) : des documents de configuration complets (`config-single.json`, `config-ha.json`), un fichier de correspondance statique, un fichier de valeurs Helm et un webhook écrit avec la seule bibliothèque standard. Un test unitaire (`tests/unit/test_tutorial_examples.py`) charge chaque configuration de ces pages et de `examples/` avec le chargeur de la passerelle lui-même : un exemple qui cesse d'être valide fait donc échouer la construction.

## Le chemin court

1. Lisez le chapitre 1 et vérifiez le chapitre 2 par rapport à votre serveur OAuth.
2. Créez les organisations, les utilisateurs et les clés côté Alien (chapitre 3).
3. Choisissez un resolver (chapitre 4). Avec une poignée d'utilisateurs, `static` suffit.
4. Déployez (chapitre 5), configurez votre serveur OAuth (chapitre 6).
5. Ajoutez l'URL de la passerelle dans un client d'IA (chapitre 7) et surveillez la ligne d'audit (chapitre 8).
