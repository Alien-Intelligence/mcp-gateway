# 3. Creating the Alien side

The gateway only looks up an existing Alien API key. Before it can serve anyone you must create, on the Alien platform, one **client-managed user** with an **API key** for each person (or, if you accept the consequences in chapter 4, for each group of people) who may use the gateway, inside a **managed organization** whose consumption is paid by your sponsor group.

Everything on this page is taken from `scripts/dev_fixture.py`, the script that does exactly these steps against a real Alien platform for the gateway's own end-to-end tests. Read it next to this chapter if you want to automate the steps.

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

- **Managed organizations cannot be deleted on Alien.** Name them with care, and look an organization up by name before you create one (the fixture does).
- The sponsor organization, the private cluster that holds your datasets and the service account must exist before you start: the fixture takes the organization id, the cluster id, the dataset id and the service account's key as inputs. How the service account and its `api-secret` key are created is not covered by the repository, so this tutorial starts from "you hold that key". Ask your Alien contact if you do not.

## Conventions used in the examples

- `$ALIEN_API` is the base URL of the Alien platform API. Alien gives you the value; this repository does not state it.
- Every answer is an envelope `{"success": true, "data": ..., "meta": ...}`. A non-2xx status, or `success` other than `true`, is an error. A failed call has an `error` object with a `name`.
- Authentication is `Authorization: Bearer <key>`. To keep a key out of the process list, put the header in a file and let curl read it with `-H @file`:

```bash
umask 077
printf 'Authorization: Bearer %s\n' "$(cat sponsor.key)" > sponsor.hdr   # printf is a shell built-in: the key is not in any argv
```

- Calls that act inside the sponsor organization also carry `x-organization-id: <sponsor org id>`. Calls on `/organizations/{id}/users...` do not.
- The examples write `SPONSOR_ORG`, `GROUP_ID`, `ORG_ID`, `USER_ID` for ids returned by earlier calls.

```bash
ALIEN_API=<the Alien API base URL>
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

Attach the grant to the group:

<!-- not-config -->
```bash
curl -sS --max-time 30 -H @sponsor.hdr -H "x-organization-id: $SPONSOR_ORG" \
  -H 'Content-Type: application/json' -d "{\"grantIds\": [$GRANT_ID]}" \
  "$ALIEN_API/groups/$GROUP_ID/grants"
```

The group must be made sponsored **after** the grant and the client organizations are attached (step 3.5): the fixture does it in that order.

## 3.3 One managed organization per client

Do this once per client organization. The route creates the organization and attaches it to the group in one call:

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

## 3.5 Make the group sponsored

After the grant and every client organization are attached:

<!-- not-config -->
```bash
curl -sS --max-time 30 -X PATCH -H @sponsor.hdr -H "x-organization-id: $SPONSOR_ORG" \
  -H 'Content-Type: application/json' -d '{"isSponsored": true}' "$ALIEN_API/groups/$GROUP_ID"
```

Attaching organizations that were not attached yet is `POST /groups/$GROUP_ID/organizations` with `{"organizationIds": [..]}` (same headers). Read the group back with `GET /groups/$GROUP_ID` and check `isSponsored` is `true`.

## 3.6 Mint the API key

<!-- not-config -->
```bash
umask 077
curl -sS --max-time 30 -H @sponsor.hdr -H 'Content-Type: application/json' \
  -d '{"name": "mcp-gateway", "scope": "api-key"}' \
  "$ALIEN_API/organizations/$ORG_ID/users/$USER_ID/tokens" > minted.json
```

Expected: `data.token` is the key (it starts with `oat_`) and `data.identifier` is the token's id, which you need to revoke it. The fixture reads `token` from this answer and writes it to a file of mode 600. Do the same: store the key in your secret store at once and do not leave `minted.json` around.

`api-key` is the scope the fixture uses for the keys the gateway holds. The sponsor service account's own key has scope `api-secret`. Give the gateway only `api-key` keys: the service account's key can create organizations, users and keys, and must stay out of the gateway.

## 3.7 Check the key

Call `GET /users/me` **with the new key** (not the service account's):

<!-- not-config -->
```bash
umask 077
printf 'Authorization: Bearer %s\n' "$(sed -n 's/.*"token": *"\([^"]*\)".*/\1/p' minted.json)" > alice.hdr
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

The list returns each key's `name` and `identifier`. A revoked key is refused by Alien (`401` on `/users/me`); a gateway that still holds it answers its clients with `-31001` until the resolver cache entry is dropped (chapter 8 explains how to rotate without that gap).

## What you now have

For each person: an Alien user id, an organization id and a key (`oat_...`) in your secret store. Chapter 4 turns these into the resolver the gateway uses.

## What this chapter could not confirm

- The exact HTTP status of each creation call (any `2xx` is accepted by the fixture).
- Whether an Alien e-mail must be unique across the platform.
- How a service account is created.
- Whether the key can be shown again after minting.

Continue with [4. Choosing a resolver](04-resolvers.md).
