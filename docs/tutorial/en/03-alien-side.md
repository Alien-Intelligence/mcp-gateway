# 3. Creating the Alien side

The gateway only looks up an existing Alien API key. Before it can serve anyone you must create, on the Alien platform, one **client-managed user** with an **API key** for each person (or, if you accept the consequences in chapter 4, for each group of people) who may use the gateway, inside a **managed organization** whose consumption is paid by your sponsor group.

The calls on this page are those of `scripts/dev_fixture.py`, the script that runs these steps against an Alien platform for the gateway's own end-to-end tests (read it if you want to automate them), checked against the platform's backend code where the section says so: see [What the backend code shows](#what-the-backend-code-shows) for who may do what, and the status of each call.

## The objects

| Object | What it is |
|---|---|
| Sponsor organization | Your organization on Alien. It owns the datasets (on a cluster) that your users will read. |
| Sponsor service account | A client-managed user of the sponsor organization with the `org-admin` role, authenticated by an `api-secret` key. Every call below runs as this user. |
| Grant | A named permission to read a dataset, owned by the sponsor organization. |
| Sponsored group | A group of the sponsor organization that holds the grant and the client organizations, with `isSponsored` true. |
| Managed organization | A client organization created through the group's managed-organization route. It is attached to the group by that same call. |
| Client-managed user | A user created inside a managed organization through the API, not by e-mail invitation. |
| API key | A token of scope `api-key` minted for one client-managed user. The gateway sends it as `Authorization: Bearer oat_...`. |

Two facts to know first:

- **Deleting an organization is refused once it has ledger history** (`ORGANIZATION_HAS_LEDGER_HISTORY`): a managed organization that has been metered stays. Name organizations with care, and look an organization up by name before you create one (the fixture does).
- The sponsor organization (an **enterprise** account), the private cluster that holds your datasets and the service account must exist before you start: the fixture takes the organization id, the cluster id, the dataset id and the service account's key as inputs. Who creates what is in [What the backend code shows](#what-the-backend-code-shows).

## Conventions used in the examples

- `$ALIEN_API` is the base URL of the public Alien API, `https://api.alien.club`. (The variable only keeps the examples short; use another value only when Alien gives you a different environment.)
- Every answer is an envelope `{"success": true, "data": ..., "meta": ...}`. A non-2xx status, or `success` other than `true`, is an error. A failed call has an `error` object with a `name`.
- Authentication is `Authorization: Bearer <key>`. To keep a key out of the process list, put the header in a file and let curl read it with `-H @file`:

```bash
umask 077
printf 'Authorization: Bearer %s\n' "$(cat sponsor.key)" > sponsor.hdr   # printf is a shell built-in: the key is not in any argv
```

- Calls that act inside the sponsor organization also carry `x-organization-id: <sponsor org id>`. Calls on `/organizations/{id}/users...` do not.
- The examples write `SPONSOR_ORG`, `GROUP_ID`, `ORG_ID`, `USER_ID` for ids returned by earlier calls.

```bash
ALIEN_API=https://api.alien.club
SPONSOR_ORG=<your sponsor organization id>
```

## 3.1 Check the service account

<!-- not-config -->
```bash
curl -sS --max-time 30 -H @sponsor.hdr -H 'Accept: application/json' "$ALIEN_API/users/me"
```

Expected: `success` true and `data` with the user's `id` and `userType` (`client-managed` for a service account). A `401` means the key is wrong or revoked.

## 3.2 Once: the grant and the sponsored group

Skip this step if your datasets are already shared with a sponsored group.

Create the grant, a read permission on one dataset (`CLUSTER_ID` and `DATASET_ID` are numbers):

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

Expected: `data.id` is the grant id. The fixture lists grants (`GET /grants`) first and reuses one with the same name; a creation can answer `409`.

Create the group:

<!-- not-config -->
```bash
curl -sS --max-time 30 -H @sponsor.hdr -H "x-organization-id: $SPONSOR_ORG" \
  -H 'Content-Type: application/json' \
  -d '{"name": "example-publisher-sponsored", "description": "client organizations paid by example-publisher"}' \
  "$ALIEN_API/groups"
```

Expected: `data.status` is `created` and `data.group.id` is the group id (`GROUP_ID`).

Attach the grant to the group (the grant must be attached before the group can be made sponsored):

<!-- not-config -->
```bash
curl -sS --max-time 30 -H @sponsor.hdr -H "x-organization-id: $SPONSOR_ORG" \
  -H 'Content-Type: application/json' -d "{\"grantIds\": [$GRANT_ID]}" \
  "$ALIEN_API/groups/$GROUP_ID/grants"
```

Make the group sponsored. The backend refuses to create a managed organization in a group that is not sponsored yet (`409` `GROUP_NOT_SPONSORED`), and refuses to sponsor a group whose grants name anything but cluster data (a grant whose ability paths start at a `cluster`, as the one above):

<!-- not-config -->
```bash
curl -sS --max-time 30 -X PATCH -H @sponsor.hdr -H "x-organization-id: $SPONSOR_ORG" \
  -H 'Content-Type: application/json' -d '{"isSponsored": true}' "$ALIEN_API/groups/$GROUP_ID"
```

Read the group back with `GET /groups/$GROUP_ID` and check `isSponsored` is `true`. The sponsor must be an enterprise organization, or this call is refused.

## 3.3 One managed organization per client

Do this once per client organization, after step 3.2 (the group must be sponsored). The route creates the organization, makes the caller its owner and attaches it to the group in one call. A name or a slug already used by another organization answers `409`; the name is compared without regard to case:

<!-- not-config -->
```bash
curl -sS --max-time 30 -H @sponsor.hdr -H "x-organization-id: $SPONSOR_ORG" \
  -H 'Content-Type: application/json' \
  -d '{"name": "example-publisher-managed-client-x", "slug": "example-publisher-managed-client-x"}' \
  "$ALIEN_API/groups/$GROUP_ID/organizations/managed"
```

Expected: `data.id` is the organization id (`ORG_ID`) and `data.accountType` is `managed`. The fixture stops if an organization it found is not `managed`.

To look up an existing one first, list the group's organizations and match on `name`:

<!-- not-config -->
```bash
curl -sS --max-time 30 -H @sponsor.hdr -H "x-organization-id: $SPONSOR_ORG" \
  "$ALIEN_API/groups/$GROUP_ID/organizations"
```

## 3.4 One client-managed user per person

Call as the service account, without `x-organization-id`:

<!-- not-config -->
```bash
curl -sS --max-time 30 -H @sponsor.hdr -H 'Content-Type: application/json' \
  -d '{"userType": "client-managed", "email": "alice@managed-client-x.example", "description": "Alice, example-publisher"}' \
  "$ALIEN_API/organizations/$ORG_ID/users"
```

Expected: `data.id` is the user id (`USER_ID`).

The user's e-mail on Alien does not have to be the address the person signs in with at your OAuth server. The gateway never compares them: your resolver maps the identity from your OAuth server (for example the sign-in e-mail) to this user's key. The fixture uses different addresses on purpose (`alice@client-x.test` at the identity provider, `alice@managed-client-x.test` on Alien) and records both. To find an existing user, `GET /organizations/$ORG_ID/users` and match on `email` (case-insensitive); check that `userType` is `client-managed`.

## 3.5 Attach an organization that already exists (optional)

Organizations that already exist (not created through step 3.3) are attached with `POST /groups/$GROUP_ID/organizations` and `{"organizationIds": [..]}` (same headers). A sponsored group may refuse some of them; nothing is attached then.

## 3.6 Mint the API key

<!-- not-config -->
```bash
umask 077
curl -sS --max-time 30 -H @sponsor.hdr -H 'Content-Type: application/json' \
  -d '{"name": "mcp-gateway", "scope": "api-key"}' \
  "$ALIEN_API/organizations/$ORG_ID/users/$USER_ID/tokens" > minted.json
```

Expected: `data.token` is the key (it starts with `oat_`) and `data.identifier` is the token's id, which you need to revoke it. The fixture reads `token` from this answer and writes it to a file of mode 600. Do the same: keep the identifier, write the key to its own file (the `static` resolver and the example webhook of chapter 4 read one key per file), move it to your secret store, and delete `minted.json`:

<!-- not-config -->
```bash
umask 077
sed -n 's/.*"identifier": *\([0-9]*\).*/\1/p' minted.json                 # the token identifier, for 3.9
sed -n 's/.*"token": *"\([^"]*\)".*/\1/p' minted.json > alice.key       # the key, one per file
rm minted.json
```

`api-key` is the scope the fixture uses for the keys the gateway holds. The sponsor service account's own key has scope `api-secret`. Give the gateway only `api-key` keys: the service account's key can create organizations, users and keys, and must stay out of the gateway.

## 3.7 Check the key

Call `GET /users/me` **with the new key** (not the service account's):

<!-- not-config -->
```bash
umask 077
printf 'Authorization: Bearer %s\n' "$(cat alice.key)" > alice.hdr
curl -sS --max-time 30 -H @alice.hdr -H 'Accept: application/json' "$ALIEN_API/users/me"
```

Expected: `data.id` equals `USER_ID`, `data.userType` is `client-managed`, `data.currentOrganizationId` equals `ORG_ID`, and `data.currentAccessToken.abilities` lists what the key may do. The fixture refuses a key that fails any of these. A `401` means the key is not accepted.

To prove the key against the Alien MCP server itself, send an MCP `initialize` with it (this is what `call_mcp_tool` in the fixture does first). The URL is the MCP server Alien gave you:

<!-- not-config -->
```bash
curl -sS --max-time 30 -H @alice.hdr -H 'Accept: application/json, text/event-stream' \
  -H 'Content-Type: application/json' "$ALIEN_MCP_URL" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-11-25","capabilities":{},"clientInfo":{"name":"setup-check","version":"0"}}}'
```

## 3.8 Check the billing

Every call is metered to the key's user and, when the group is sponsored, shown in the sponsor's feed as well. Read the organization's MCP feed (call as the service account, with the client organization's id in `x-organization-id`):

<!-- not-config -->
```bash
curl -sS --max-time 30 -H @sponsor.hdr -H "x-organization-id: $ORG_ID" \
  "$ALIEN_API/organizations/$ORG_ID/consumption/events?first=100&sourceType=mcp&start=2026-01-01T00:00:00%2B00:00"
```

Expected: `data.events` with, for each call, `user.id` (the client-managed user), `organizationId`, `calledAt`, `jobId` and `sponsored` (with `groupId` and `organizationId` when sponsorship applied). `data.pageInfo` has `hasNextPage` and `endCursor`; pass `after=<endCursor>` for the next page. Metering is asynchronous: the fixture polls with backoff for up to 60 seconds. The sponsor's own feed is `GET /organizations/$SPONSOR_ORG/consumption/events` with `x-organization-id: $SPONSOR_ORG`; look for events whose `sponsored.groupId` is your group.

## 3.9 Revoke a key

List the user's keys, then delete one by its `identifier`:

<!-- not-config -->
```bash
curl -sS --max-time 30 -H @sponsor.hdr "$ALIEN_API/organizations/$ORG_ID/users/$USER_ID/tokens"
curl -sS --max-time 30 -X DELETE -H @sponsor.hdr \
  "$ALIEN_API/organizations/$ORG_ID/users/$USER_ID/tokens/$TOKEN_IDENTIFIER"
```

The list returns each key's `identifier` and its `name`, which the platform prefixes with the key's type: a key minted with the name `mcp-gateway` is listed as `API_KEY: mcp-gateway`. Match on the identifier you kept at minting. A revoked key is refused by Alien (`401` on `/users/me`); a gateway that still holds it answers its clients with `-31001` until the resolver cache entry is dropped (chapter 8 explains how to rotate without that gap).

## What you now have

For each person: an Alien user id, an organization id and a key (`oat_...`) in your secret store. Chapter 4 turns these into the resolver the gateway uses.

## What the backend code shows

Read from the backend's source, branch `dev` of the platform repository at commit `c5d5c746c` (2026-11-20). Nothing here was run against a live API for this chapter.

**Which account and which key.**

- Creating users and keys under an organization (`POST /organizations/{id}/users` and `.../users/{user}/tokens`) is allowed only when the caller's key carries the `user:write` ability, the caller is an **admin of that organization or above**, and the organization's type is **enterprise or managed**. A key of scope `api-key` has `user:read` only: user and key creation need scope `api-secret` (or an unrestricted key). The group routes of this chapter need the `grant:write` ability, which `api-secret` has and `api-key` does not.
- The managed-organization route needs both `grant:write` and `user:write` and admin rights on the group's organization.
- A group can be made sponsored only when its organization is an **enterprise** one. An organization's account type is changed only by **platform administrators** (the update route refuses everyone else: "Only platform administrators can update organization account type, limits and internal status"). **So the sponsor organization is made an enterprise account by Alien, at onboarding: ask Alien.**
- The **sponsor service account** is a `client-managed` user created inside the sponsor organization by `POST /organizations/{sponsor}/users` with `{"userType": "client-managed", "email": ..., "description": ...}`, called by an admin of that organization with a key that has `user:write`; its key is then minted with `POST .../users/{id}/tokens` and `{"name": ..., "scope": "api-secret"}`. The code does not say how the first admin key of the sponsor organization is obtained: ask Alien. A customer can therefore create the service account and its key through the API, once Alien has made the organization an enterprise account and given an admin of it a key that may write users.
- The service account's email must be **unused on the whole platform**: creating a `client-managed` user with an e-mail that already belongs to any account answers `409` `EMAIL_ALREADY_EXISTS` (the lookup is by e-mail alone, across organizations). The same applies to every per-person user of step 3.4. The e-mail needs to be a valid address; it does not need to receive mail (the code sends none for `client-managed` users).
- Key scopes accepted at minting: `api-key` (the default when `scope` is omitted) and `api-secret`; anything else answers `400`. An optional `expiresIn` (a number) sets an expiry; without it the code sets none. `api-key` carries read abilities only (access tokens, AI models, chat sessions, clusters, collections, datasets, entries, files, workflows, grants, jobs, licenses, MCP configurations, nodes, providers, roles, tags, users).

**Status of each creation call.** The backend's controllers send their success answers through one helper that sets no status code, so these calls answer the framework's default, `200`, and not `201`; no error path of these calls is a `2xx`. Treat any `2xx` as success, as the fixture does. Failures use these statuses: `400` invalid parameter or key scope, `404` not found, `409` already exists or not sponsored (`EMAIL_ALREADY_EXISTS`, `ORGANIZATION_NAME_ALREADY_EXISTS`, `ORGANIZATION_SLUG_ALREADY_EXISTS`, `GROUP_NOT_SPONSORED`). `POST /groups` is the exception for a taken name: it answers success with `data.status` `name-taken` (not an error status), which is why step 3.2 checks `data.status`. These statuses come from the code, not from a run: confirm them on your own calls.

**Can a key be shown again?** No. The raw key is in the answer of the minting call only (`data.token`); listing a user's keys returns `identifier`, `name`, `abilities`, `lastUsedAt`, `expiresAt` and no key. A lost key is replaced by minting another and revoking the first.

**Availability.** The managed-organization route and the `managed` account type are on the platform's `dev` and `staging` branches and are **not on its `main` branch** at the time of writing. Whether the API at `https://api.alien.club` already has them: ask Alien before you plan around this chapter.

## What this chapter could not confirm

- How the first admin key of a sponsor organization is issued, and when the sponsor organization is made an enterprise account: ask Alien.
- Whether the public API already runs the managed-organization route (see Availability above).
- The statuses above were read, not observed.

Continue with [4. Choosing a resolver](04-resolvers.md).
