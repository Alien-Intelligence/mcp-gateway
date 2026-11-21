"""The deployable Keycloak realm template (deploy/keycloak), imported into the pinned Keycloak
the way its README tells a customer to, and attacked the way an anonymous client could.

The ``keycloak-template`` service of the stub target renders the template with ``render.sh`` and
imports it at start. These tests create their own user and groups through the admin API of that
Keycloak, then check what an anonymous registrant can and cannot get from the realm: only the
authorization code flow with PKCE S256 yields a token, and that token is for the gateway.
"""

import os
import secrets
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest

from e2e import oauth_flow
from e2e.oauth_flow import HTTP_TIMEOUT, OAuthFlowError

pytestmark = pytest.mark.e2e_stub

REQUIRED_ENV = (
    "E2E_KC_TEMPLATE_URL",
    "E2E_KC_TEMPLATE_REALM",
    "E2E_KC_TEMPLATE_AUDIENCE",
    "E2E_KC_ADMIN_USERNAME",
    "E2E_KC_ADMIN_PASSWORD",
)
LOOPBACK_REDIRECT = "http://127.0.0.1/callback"
# A loopback redirect registered without a port matches any port at the authorization request.
LOOPBACK_REDIRECT_AT_SIGN_IN = "http://127.0.0.1:53682/callback"
SCOPE = "mcp-gateway offline_access"
# Two groups with the same name under different parents: with group names alone in the claim,
# their members would be indistinguishable to a resolver.
GROUP_PARENTS = ("team-a", "team-b")
GROUP_NAME = "analysts"


@dataclass(frozen=True)
class TemplateRealm:
    base_url: str
    realm: str
    audience: str
    admin_username: str
    admin_password: str = field(repr=False)

    @property
    def issuer(self) -> str:
        return f"{self.base_url}/realms/{self.realm}"

    @property
    def admin_url(self) -> str:
        return f"{self.base_url}/admin/realms/{self.realm}"


@dataclass(frozen=True)
class RealmUser:
    username: str
    password: str = field(repr=False)
    group_path: str


@pytest.fixture(scope="module")
def template() -> TemplateRealm:
    missing = [name for name in REQUIRED_ENV if not os.environ.get(name)]
    if missing:
        pytest.fail("realm template checks are not configured, missing: " + ", ".join(missing))
    return TemplateRealm(
        base_url=os.environ["E2E_KC_TEMPLATE_URL"].rstrip("/"),
        realm=os.environ["E2E_KC_TEMPLATE_REALM"],
        audience=os.environ["E2E_KC_TEMPLATE_AUDIENCE"],
        admin_username=os.environ["E2E_KC_ADMIN_USERNAME"],
        admin_password=os.environ["E2E_KC_ADMIN_PASSWORD"],
    )


# Credentials an answer can carry; never put in an assertion message (the junit report keeps it).
SECRET_FIELDS = frozenset(
    {
        "access_token",
        "refresh_token",
        "id_token",
        "registration_access_token",
        "client_secret",
        "device_code",
    }
)


def _redacted(body: Any) -> Any:
    if isinstance(body, dict):
        return {k: "<redacted>" if k in SECRET_FIELDS else v for k, v in body.items()}
    return body


def _describe(response: httpx.Response) -> str:
    try:
        shown = _redacted(response.json())
    except ValueError:
        shown = response.text[:300]
    return f"{response.request.method} {response.url} -> {response.status_code}: {shown}"


def _json(response: httpx.Response, status: int) -> Any:
    if response.status_code != status:
        raise AssertionError(f"wanted {status}: {_describe(response)}")
    return response.json()


def _created_id(response: httpx.Response) -> str:
    if response.status_code != 201:
        raise AssertionError(f"wanted 201: {_describe(response)}")
    return response.headers["location"].rsplit("/", 1)[1]


async def _admin_client(template: TemplateRealm) -> httpx.AsyncClient:
    """An admin API client, signed in as the bootstrap administrator of the master realm (the
    way the template's README creates users: never through a client of the gateway realm)."""
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as login:
        response = await login.post(
            f"{template.base_url}/realms/master/protocol/openid-connect/token",
            data={
                "grant_type": "password",
                "client_id": "admin-cli",
                "username": template.admin_username,
                "password": template.admin_password,
            },
        )
    token = _json(response, 200)["access_token"]
    return httpx.AsyncClient(timeout=HTTP_TIMEOUT, headers={"Authorization": f"Bearer {token}"})


@pytest.fixture
async def user(template: TemplateRealm) -> AsyncIterator[RealmUser]:
    """A user with a verified e-mail, the ``org`` attribute and one of two same-named groups,
    removed with its groups afterwards."""
    run = secrets.token_hex(4)
    username = f"template-{run}@client-x.test"
    # The realm's password policy: 12 characters or more, not the username, not the e-mail.
    password = secrets.token_urlsafe(24)
    async with await _admin_client(template) as admin:
        parent_ids: list[str] = []
        try:
            group_ids: list[str] = []
            for parent in GROUP_PARENTS:
                parent_ids.append(
                    _created_id(
                        await admin.post(
                            f"{template.admin_url}/groups", json={"name": f"{parent}-{run}"}
                        )
                    )
                )
                group_ids.append(
                    _created_id(
                        await admin.post(
                            f"{template.admin_url}/groups/{parent_ids[-1]}/children",
                            json={"name": GROUP_NAME},
                        )
                    )
                )
            user_id = _created_id(
                await admin.post(
                    f"{template.admin_url}/users",
                    json={
                        "username": username,
                        "email": username,
                        "emailVerified": True,
                        "enabled": True,
                        "attributes": {"org": ["client-x"]},
                        "credentials": [
                            {"type": "password", "value": password, "temporary": False}
                        ],
                    },
                )
            )
            try:
                response = await admin.put(
                    f"{template.admin_url}/users/{user_id}/groups/{group_ids[0]}"
                )
                assert response.status_code == 204, response.text[:300]
                yield RealmUser(username, password, f"/{GROUP_PARENTS[0]}-{run}/{GROUP_NAME}")
            finally:
                response = await admin.delete(f"{template.admin_url}/users/{user_id}")
                assert response.status_code == 204, response.text[:300]
        finally:
            for parent_id in parent_ids:
                response = await admin.delete(f"{template.admin_url}/groups/{parent_id}")
                assert response.status_code == 204, response.text[:300]


@pytest.fixture
async def metadata(template: TemplateRealm) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        return await oauth_flow.discover_authorization_server(client, template.issuer)


@dataclass(frozen=True)
class TokenAnswer:
    """A token endpoint answer. ``body`` holds credentials: it is kept out of the repr, so a
    failing test never prints a token."""

    status: int
    body: dict[str, Any] = field(repr=False)

    @property
    def issued(self) -> bool:
        return "access_token" in self.body

    def __str__(self) -> str:
        return f"HTTP {self.status} {_redacted(self.body)}"


async def _post_token(metadata: dict[str, Any], data: dict[str, str], **kwargs: Any) -> TokenAnswer:
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        response = await client.post(metadata["token_endpoint"], data=data, **kwargs)
    return TokenAnswer(response.status_code, response.json())


async def test_a_registered_client_with_pkce_gets_a_token_for_the_gateway(
    template: TemplateRealm, user: RealmUser, metadata: dict[str, Any], evidence: dict[str, Any]
) -> None:
    """The positive control: what every MCP client does works, and the token is the gateway's."""
    async with oauth_flow.new_client() as client:
        registration = await oauth_flow.register_client(
            client, metadata["registration_endpoint"], LOOPBACK_REDIRECT, "template-check"
        )
        authorization = await oauth_flow.authorize(
            client,
            authorization_endpoint=metadata["authorization_endpoint"],
            client_id=registration["client_id"],
            redirect_uri=LOOPBACK_REDIRECT_AT_SIGN_IN,
            resource=template.audience,
            scope=SCOPE,
            username=user.username,
            password=user.password,
            issuer=template.issuer,
        )
        token = await oauth_flow.exchange_code(
            client,
            token_endpoint=metadata["token_endpoint"],
            client_id=registration["client_id"],
            authorization=authorization,
            resource=template.audience,
        )
    claims = oauth_flow.decode_jwt_payload(token["access_token"])
    audience = claims["aud"] if isinstance(claims["aud"], list) else [claims["aud"]]
    evidence.update(aud=audience, groups=claims.get("groups"), scope=claims.get("scope"))
    assert audience == [template.audience]
    assert claims["iss"] == template.issuer
    assert claims["email"] == user.username
    assert claims["email_verified"] is True
    assert claims["org"] == "client-x"

    # The refresh grant is the other grant the realm allows.
    refreshed = await _post_token(
        metadata,
        {
            "grant_type": "refresh_token",
            "refresh_token": token["refresh_token"],
            "client_id": registration["client_id"],
        },
    )
    assert refreshed.issued, str(refreshed)
    assert oauth_flow.decode_jwt_payload(refreshed.body["access_token"])["aud"] == template.audience


async def test_an_authorization_request_without_pkce_is_refused(
    template: TemplateRealm, metadata: dict[str, Any]
) -> None:
    async with oauth_flow.new_client() as client:
        registration = await oauth_flow.register_client(
            client, metadata["registration_endpoint"], LOOPBACK_REDIRECT, "template-check"
        )
        response = await client.get(
            metadata["authorization_endpoint"],
            params={
                "response_type": "code",
                "client_id": registration["client_id"],
                "redirect_uri": LOOPBACK_REDIRECT_AT_SIGN_IN,
                "scope": SCOPE,
                "state": "no-pkce",
            },
        )
    assert response.is_redirect, _describe(response)
    callback = httpx.URL(response.headers["location"])
    assert callback.params.get("error") == "invalid_request", str(callback)
    assert "code" not in callback.params


async def test_the_positive_control_flow_detects_a_wrong_password(
    template: TemplateRealm, user: RealmUser, metadata: dict[str, Any]
) -> None:
    """The form driver of the other tests reports a refused login instead of passing silently."""
    async with oauth_flow.new_client() as client:
        registration = await oauth_flow.register_client(
            client, metadata["registration_endpoint"], LOOPBACK_REDIRECT, "template-check"
        )
        with pytest.raises(OAuthFlowError, match="login rejected"):
            await oauth_flow.authorize(
                client,
                authorization_endpoint=metadata["authorization_endpoint"],
                client_id=registration["client_id"],
                redirect_uri=LOOPBACK_REDIRECT_AT_SIGN_IN,
                resource=template.audience,
                scope=SCOPE,
                username=user.username,
                password=user.password + "-wrong",
                issuer=template.issuer,
            )
