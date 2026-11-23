"""Write the `alien-gateway-userinfo` Keycloak realm (dev/keycloak/alien-gateway-userinfo-realm.json)
from the dev realm (dev/keycloak/alien-gateway-dev-realm.json).

The result models an identity provider whose ACCESS tokens identify the user (`sub`) and nothing
else, as the gateway's `auth.userinfo` setting expects: the `email`, `email verified`, `org` and
`groups` mappers keep their claims in the ID token and the userinfo document only. Besides that
it differs from the dev realm in two places:

- the `mcp-gateway` scope puts the audiences of the userinfo gateways in the token, and only those;
- the optional scope `mcp-userinfo-mismatch` makes the userinfo document answer a `sub` that is
  not the token's (a hardcoded-claim mapper that skips the access token), so the gateway's
  subject check can be exercised against a real identity provider. Clients ask for it with
  `scope`; anonymous registrations get it because it is a realm optional scope.

``python scripts/gen_userinfo_realm.py`` rewrites the committed file; ``--check`` exits 1 when it
is not what the generator produces. The unit test runs the same comparison.
"""

import argparse
import copy
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SOURCE_PATH = ROOT / "dev" / "keycloak" / "alien-gateway-dev-realm.json"
TARGET_PATH = ROOT / "dev" / "keycloak" / "alien-gateway-userinfo-realm.json"

REALM_NAME = "alien-gateway-userinfo"
# The public URLs of the gateways that trust this realm (compose.yaml): the one that reads the
# userinfo document, and the one whose userinfo URL points at a closed port.
GATEWAY_AUDIENCES = ("http://localhost:18008/mcp", "http://localhost:18011/mcp")
# The optional scope that makes the userinfo `sub` differ from the token's, and the value it gives.
MISMATCH_SCOPE = "mcp-userinfo-mismatch"
MISMATCH_SUBJECT = "subject-that-is-not-the-token-subject"
# Client scope -> its mappers that must stop writing the access token.
USERINFO_ONLY_MAPPERS = {
    "email": ("email", "email verified"),
    "gateway-identity": ("org", "groups"),
}
AUDIENCE_SCOPE = "mcp-gateway"


class RealmError(Exception):
    """The dev realm no longer has the shape this generator transforms."""


def _scope(realm: dict[str, Any], name: str) -> dict[str, Any]:
    for scope in realm["clientScopes"]:
        if scope["name"] == name:
            return scope
    raise RealmError(f"the dev realm has no client scope {name!r}")


def _make_userinfo_only(scope: dict[str, Any], mapper_names: tuple[str, ...]) -> None:
    """Keep the claims of the named mappers in the userinfo document and the ID token only."""
    mappers = {mapper["name"]: mapper for mapper in scope["protocolMappers"]}
    for name in mapper_names:
        if name not in mappers:
            raise RealmError(f"client scope {scope['name']!r} has no mapper {name!r}")
        config = mappers[name]["config"]
        if config.get("userinfo.token.claim") != "true":
            raise RealmError(f"mapper {name!r} does not write the userinfo document")
        config["access.token.claim"] = "false"
        config["introspection.token.claim"] = "false"


def _audience_mapper(index: int, audience: str) -> dict[str, Any]:
    return {
        "name": f"gateway-audience-{index}",
        "protocol": "openid-connect",
        "protocolMapper": "oidc-audience-mapper",
        "consentRequired": False,
        "config": {
            "included.custom.audience": audience,
            "access.token.claim": "true",
            "id.token.claim": "false",
            "introspection.token.claim": "true",
        },
    }


def _mismatch_scope() -> dict[str, Any]:
    return {
        "name": MISMATCH_SCOPE,
        "protocol": "openid-connect",
        "description": "Test only: the userinfo document answers a subject that is not the token's",
        "attributes": {"include.in.token.scope": "true", "display.on.consent.screen": "false"},
        "protocolMappers": [
            {
                "name": "userinfo-subject-mismatch",
                "protocol": "openid-connect",
                "protocolMapper": "oidc-hardcoded-claim-mapper",
                "consentRequired": False,
                "config": {
                    "claim.name": "sub",
                    "claim.value": MISMATCH_SUBJECT,
                    "jsonType.label": "String",
                    "access.token.claim": "false",
                    "id.token.claim": "false",
                    "introspection.token.claim": "false",
                    "userinfo.token.claim": "true",
                },
            }
        ],
    }


def transform(realm: dict[str, Any]) -> dict[str, Any]:
    """The userinfo realm for a parsed dev realm."""
    out = copy.deepcopy(realm)
    out["realm"] = REALM_NAME
    if "displayName" in out:
        out["displayName"] = REALM_NAME
    for scope_name, mapper_names in USERINFO_ONLY_MAPPERS.items():
        _make_userinfo_only(_scope(out, scope_name), mapper_names)
    _scope(out, AUDIENCE_SCOPE)["protocolMappers"] = [
        _audience_mapper(index, audience) for index, audience in enumerate(GATEWAY_AUDIENCES)
    ]
    if any(scope["name"] == MISMATCH_SCOPE for scope in out["clientScopes"]):
        raise RealmError(f"the dev realm already has a client scope {MISMATCH_SCOPE!r}")
    out["clientScopes"].append(_mismatch_scope())
    out["defaultOptionalClientScopes"].append(MISMATCH_SCOPE)
    return out


def render() -> str:
    """The realm as the exact text of the committed file."""
    realm: dict[str, Any] = json.loads(SOURCE_PATH.read_text(encoding="utf-8"))
    return json.dumps(transform(realm), indent=2) + "\n"


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail if the committed file is stale")
    args = parser.parse_args(argv)
    text = render()
    if args.check:
        if TARGET_PATH.exists() and TARGET_PATH.read_text(encoding="utf-8") == text:
            return 0
        print(f"{TARGET_PATH} is stale: run python scripts/gen_userinfo_realm.py", file=sys.stderr)
        return 1
    TARGET_PATH.write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
