# Alien MCP Gateway: setup tutorial

[Version française](../fr/README.md)

This tutorial is for a publisher (or any organization) that runs the Alien MCP Gateway itself, in front of Alien MCP servers, under its own domain and with its own login. It takes you from nothing to a working deployment that Claude, ChatGPT, Microsoft Copilot Studio, VS Code and Cursor can connect to.

All examples use generic names: `example-publisher`, `mcp.example.com` (your gateway), `idp.example.com` (your OAuth server), `keys.example.com` (your webhook). Replace them with yours. Every configuration key, endpoint, command and error code in these pages was read from the gateway's code, its reference documents ([`configuration.md`](../../configuration.md), [`protocol.md`](../../protocol.md), [`config.schema.v1.json`](../../config.schema.v1.json)), the Helm chart or the scripts in this repository. Where a statement comes from a vendor's documentation, the vendor URL is given. Where something could not be confirmed, the text says so.

| Chapter | You learn |
|---|---|
| [1. What the gateway is and is not](01-what-it-is.md) | The role of the gateway, what it never does, the architecture |
| [2. Prerequisites](02-prerequisites.md) | What your OAuth server must provide, network and TLS |
| [3. The Alien side](03-alien-side.md) | The exact API calls that create organizations, users and keys |
| [4. Choosing a resolver](04-resolvers.md) | How a signed-in user becomes an Alien key; writing a webhook |
| [5. Deploying](05-deploying.md) | Helm, docker compose, secrets, high availability |
| [6. Your OAuth server](06-oauth-server.md) | Keycloak setup, and a checklist for other servers |
| [7. Connecting clients](07-clients.md) | What to enter in each AI client |
| [8. Operations](08-operations.md) | Key rotation, observability, limits, upgrades |
| [9. Troubleshooting](09-troubleshooting.md) | Symptom, cause, fix |

Files used by the tutorial are in [`../examples/`](../examples/): complete configuration documents (`config-single.json`, `config-ha.json`), a static mapping file, a Helm values file and a webhook written with the standard library only. A unit test (`tests/unit/test_tutorial_examples.py`) loads every configuration in these pages and in `examples/` with the gateway's own loader, so an example that stops being valid fails the build.

## The short path

1. Read chapter 1 and check chapter 2 against your OAuth server.
2. Create the organizations, users and keys on the Alien side (chapter 3).
3. Pick a resolver (chapter 4). With a handful of users, `static` is enough.
4. Deploy (chapter 5), configure your OAuth server (chapter 6).
5. Add the gateway URL in an AI client (chapter 7) and watch the audit line (chapter 8).
