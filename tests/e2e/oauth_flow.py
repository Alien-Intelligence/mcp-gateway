"""Scripted MCP client OAuth flow: discovery, DCR, authorization code + PKCE, token.

It behaves like a real MCP client against a real identity provider: the login
and consent pages are real HTML forms, driven generically.
"""

import base64
import hashlib
import json
import secrets
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit

import httpx

HTTP_TIMEOUT = httpx.Timeout(15.0, connect=5.0)
MAX_REDIRECTS = 20
MAX_FORM_STEPS = 6
BODY_EXCERPT = 300


class OAuthFlowError(Exception):
    """A step of the OAuth flow did not behave as a conforming client expects."""


def _excerpt(response: httpx.Response) -> str:
    return response.text[:BODY_EXCERPT].replace("\n", " ")


def _describe(response: httpx.Response) -> str:
    return (
        f"{response.request.method} {response.url} -> {response.status_code}: {_excerpt(response)}"
    )


def _json_object(response: httpx.Response, expected_status: int) -> dict[str, Any]:
    if response.status_code != expected_status:
        raise OAuthFlowError(f"unexpected status (wanted {expected_status}): {_describe(response)}")
    try:
        payload = response.json()
    except ValueError as exc:
        raise OAuthFlowError(f"response is not JSON: {_describe(response)}") from exc
    if not isinstance(payload, dict):
        raise OAuthFlowError(f"response is not a JSON object: {_describe(response)}")
    return payload


async def fetch_protected_resource_metadata(
    client: httpx.AsyncClient, resource_metadata_url: str
) -> dict[str, Any]:
    response = await client.get(resource_metadata_url, timeout=HTTP_TIMEOUT)
    metadata = _json_object(response, 200)
    for key in ("resource", "authorization_servers"):
        if key not in metadata:
            raise OAuthFlowError(f"protected resource metadata lacks {key!r}: {metadata}")
    return metadata


def discovery_urls(issuer: str) -> list[str]:
    """Metadata URLs in the MCP 2025-11-25 probe order."""
    parts = urlsplit(issuer)
    origin = f"{parts.scheme}://{parts.netloc}"
    path = parts.path.rstrip("/")
    if not path:
        return [
            f"{origin}/.well-known/oauth-authorization-server",
            f"{origin}/.well-known/openid-configuration",
        ]
    return [
        f"{origin}/.well-known/oauth-authorization-server{path}",
        f"{origin}/.well-known/openid-configuration{path}",
        f"{issuer.rstrip('/')}/.well-known/openid-configuration",
    ]


async def discover_authorization_server(client: httpx.AsyncClient, issuer: str) -> dict[str, Any]:
    attempts: list[str] = []
    for url in discovery_urls(issuer):
        response = await client.get(url, timeout=HTTP_TIMEOUT)
        if response.status_code != 200:
            attempts.append(f"{url} -> {response.status_code}")
            continue
        metadata = _json_object(response, 200)
        if metadata.get("issuer") != issuer:
            raise OAuthFlowError(
                f"issuer mismatch at {url}: expected {issuer!r}, got {metadata.get('issuer')!r}"
            )
        if "S256" not in metadata.get("code_challenge_methods_supported", []):
            raise OAuthFlowError(f"{url} does not advertise PKCE S256")
        if not metadata.get("registration_endpoint"):
            raise OAuthFlowError(f"{url} advertises no registration_endpoint")
        return metadata
    raise OAuthFlowError(f"no authorization server metadata for {issuer}: {'; '.join(attempts)}")


async def register_client(
    client: httpx.AsyncClient, registration_endpoint: str, redirect_uri: str, client_name: str
) -> dict[str, Any]:
    """Anonymous RFC 7591 registration of a public client."""
    response = await client.post(
        registration_endpoint,
        json={
            "client_name": client_name,
            "redirect_uris": [redirect_uri],
            "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
        },
        timeout=HTTP_TIMEOUT,
    )
    registration = _json_object(response, 201)
    if not registration.get("client_id"):
        raise OAuthFlowError(f"registration returned no client_id: {_describe(response)}")
    return registration


@dataclass(frozen=True)
class Pkce:
    verifier: str
    challenge: str

    @classmethod
    def generate(cls) -> "Pkce":
        verifier = secrets.token_urlsafe(48)
        digest = hashlib.sha256(verifier.encode("ascii")).digest()
        return cls(verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii"))


@dataclass(frozen=True)
class AuthorizationResult:
    code: str
    redirect_uri: str
    pkce: Pkce


@dataclass
class _Form:
    action: str
    method: str
    inputs: list[tuple[str, str, str]]  # (name, type, value)
    buttons: list[tuple[str, str, str]]  # (name, id, value)


class _FormParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.forms: list[_Form] = []
        self._current: _Form | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k: v or "" for k, v in attrs}
        if tag == "form":
            self._current = _Form(a.get("action", ""), a.get("method", "get").lower(), [], [])
        elif self._current is not None and tag == "input":
            kind = a.get("type", "text").lower()
            if kind in ("submit", "button"):
                self._current.buttons.append(
                    (a.get("name", ""), a.get("id", ""), a.get("value", ""))
                )
            else:
                self._current.inputs.append((a.get("name", ""), kind, a.get("value", "")))
        elif self._current is not None and tag == "button":
            self._current.buttons.append((a.get("name", ""), a.get("id", ""), a.get("value", "")))

    def handle_endtag(self, tag: str) -> None:
        if tag == "form" and self._current is not None:
            self.forms.append(self._current)
            self._current = None


def _parse_forms(page: str) -> list[_Form]:
    parser = _FormParser()
    parser.feed(page)
    parser.close()
    return parser.forms


def _is_accept(button: tuple[str, str, str]) -> bool:
    name, button_id, value = button
    return "cancel" not in f"{name} {button_id} {value}".lower() and (
        "accept" in f"{name} {button_id} {value}".lower()
        or "yes" in value.lower()
        or "allow" in value.lower()
        or "authorize" in value.lower()
    )


def _fill_login_form(form: _Form, username: str, password: str) -> dict[str, str]:
    """Fill a form with a password input: the field before it is the username."""
    data: dict[str, str] = {}
    previous_text_field: str | None = None
    password_field: str | None = None
    for name, kind, value in form.inputs:
        if not name:
            continue
        if kind == "password":
            password_field = name
            data[name] = password
        elif kind in ("text", "email"):
            previous_text_field = name
            data[name] = value
        elif kind == "hidden":
            data[name] = value
    if password_field is None or previous_text_field is None:
        raise OAuthFlowError("login form has no username/password field pair")
    data[previous_text_field] = username
    return data


def _query(url: str) -> dict[str, list[str]]:
    return parse_qs(urlsplit(url).query)


def _is_callback(url: str, redirect_uri: str) -> bool:
    target, base = urlsplit(url), urlsplit(redirect_uri)
    return (target.scheme, target.netloc, target.path) == (base.scheme, base.netloc, base.path)


def build_authorization_url(
    authorization_endpoint: str,
    client_id: str,
    redirect_uri: str,
    pkce: Pkce,
    state: str,
    resource: str,
    scope: str,
) -> str:
    query = urlencode(
        {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "code_challenge": pkce.challenge,
            "code_challenge_method": "S256",
            "state": state,
            "resource": resource,
            "scope": scope,
        }
    )
    return f"{authorization_endpoint}?{query}"


def _check_callback(location: str, redirect_uri: str, state: str, issuer: str | None) -> str:
    params = _query(location)
    if "error" in params:
        raise OAuthFlowError(f"authorization error: {params}")
    if params.get("state") != [state]:
        raise OAuthFlowError(f"state mismatch in callback {location}")
    if "iss" in params and issuer is not None and params["iss"] != [issuer]:
        raise OAuthFlowError(f"RFC 9207 iss mismatch: {params['iss']} != {issuer!r}")
    codes = params.get("code")
    if not codes:
        raise OAuthFlowError(f"callback carries no code: {location}")
    return codes[0]


async def authorize(
    client: httpx.AsyncClient,
    *,
    authorization_endpoint: str,
    client_id: str,
    redirect_uri: str,
    resource: str,
    scope: str,
    username: str,
    password: str,
    issuer: str | None = None,
) -> AuthorizationResult:
    """Drive the real login (and consent) pages until the redirect to redirect_uri.

    The client must not follow redirects on its own and should own a fresh cookie jar.
    """
    pkce = Pkce.generate()
    state = secrets.token_urlsafe(16)
    url = build_authorization_url(
        authorization_endpoint, client_id, redirect_uri, pkce, state, resource, scope
    )
    method, data = "GET", None
    login_submitted = False
    for _ in range(MAX_FORM_STEPS * MAX_REDIRECTS):
        response = await client.request(method, url, data=data, timeout=HTTP_TIMEOUT)
        if response.is_redirect:
            location = urljoin(str(response.url), response.headers["location"])
            if _is_callback(location, redirect_uri):
                code = _check_callback(location, redirect_uri, state, issuer)
                return AuthorizationResult(code, redirect_uri, pkce)
            url, method, data = location, "GET", None
            continue
        if response.status_code != 200 and not (login_submitted and response.status_code == 401):
            raise OAuthFlowError(f"unexpected page: {_describe(response)}")
        forms = _parse_forms(response.text)
        login_form = next((f for f in forms if any(k == "password" for _, k, _ in f.inputs)), None)
        if login_form is not None:
            if login_submitted:
                raise OAuthFlowError(f"login rejected, form shown again: {_describe(response)}")
            login_submitted = True
            data = _fill_login_form(login_form, username, password)
            method = login_form.method.upper()
            url = urljoin(str(response.url), login_form.action)
            continue
        consent = next((f for f in forms if any(_is_accept(b) for b in f.buttons)), None)
        if consent is None:
            raise OAuthFlowError(f"no login or consent form on page: {_describe(response)}")
        accept = next(b for b in consent.buttons if _is_accept(b))
        data = {name: value for name, kind, value in consent.inputs if name and kind == "hidden"}
        if accept[0]:
            data[accept[0]] = accept[2]
        method = consent.method.upper()
        url = urljoin(str(response.url), consent.action)
    raise OAuthFlowError("too many steps before reaching the redirect_uri")


async def approve_device_authorization(
    client: httpx.AsyncClient, *, verification_uri_complete: str, username: str, password: str
) -> str:
    """Approve a device authorization as the user would in a browser: open the verification
    page, sign in and consent. Returns the text of the final page (the provider's verdict).

    The client must not follow redirects on its own and should own a fresh cookie jar.
    """
    url, method, data = verification_uri_complete, "GET", None
    login_submitted = False
    for _ in range(MAX_FORM_STEPS * MAX_REDIRECTS):
        response = await client.request(method, url, data=data, timeout=HTTP_TIMEOUT)
        if response.is_redirect:
            location = urljoin(str(response.url), response.headers["location"])
            url, method, data = location, "GET", None
            continue
        if response.status_code != 200:
            raise OAuthFlowError(f"unexpected page: {_describe(response)}")
        forms = _parse_forms(response.text)
        login_form = next((f for f in forms if any(k == "password" for _, k, _ in f.inputs)), None)
        if login_form is not None:
            if login_submitted:
                raise OAuthFlowError(f"login rejected, form shown again: {_describe(response)}")
            login_submitted = True
            data = _fill_login_form(login_form, username, password)
            method = login_form.method.upper()
            url = urljoin(str(response.url), login_form.action)
            continue
        consent = next((f for f in forms if any(_is_accept(b) for b in f.buttons)), None)
        if consent is None:
            return response.text
        accept = next(b for b in consent.buttons if _is_accept(b))
        data = {name: value for name, kind, value in consent.inputs if name and kind == "hidden"}
        if accept[0]:
            data[accept[0]] = accept[2]
        method = consent.method.upper()
        url = urljoin(str(response.url), consent.action)
    raise OAuthFlowError("too many steps on the device verification pages")


async def exchange_code(
    client: httpx.AsyncClient,
    *,
    token_endpoint: str,
    client_id: str,
    authorization: AuthorizationResult,
    resource: str,
) -> dict[str, Any]:
    response = await client.post(
        token_endpoint,
        data={
            "grant_type": "authorization_code",
            "code": authorization.code,
            "redirect_uri": authorization.redirect_uri,
            "client_id": client_id,
            "code_verifier": authorization.pkce.verifier,
            "resource": resource,
        },
        timeout=HTTP_TIMEOUT,
    )
    token = _json_object(response, 200)
    if "access_token" not in token:
        raise OAuthFlowError(f"token response lacks access_token: {_describe(response)}")
    return token


def decode_jwt_payload(token: str) -> dict[str, Any]:
    """Decode a JWT payload WITHOUT verifying it. For assertions in tests only."""
    parts = token.split(".")
    if len(parts) != 3:
        raise OAuthFlowError("not a compact JWT")
    padded = parts[1] + "=" * (-len(parts[1]) % 4)
    payload = json.loads(base64.urlsafe_b64decode(padded))
    if not isinstance(payload, dict):
        raise OAuthFlowError("JWT payload is not an object")
    return payload


_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "[::1]"})


def new_client() -> httpx.AsyncClient:
    """A client with its own cookie jar that leaves redirects to authorize().

    Browsers accept and send Secure cookies on http://localhost, the stdlib cookie
    jar does not, and Keycloak marks its login cookies Secure even over plain http.
    The response hook emulates the browser rule for loopback hosts only.
    """
    client = httpx.AsyncClient(follow_redirects=False, timeout=HTTP_TIMEOUT)

    async def loopback_is_a_secure_context(response: httpx.Response) -> None:
        if response.url.host in _LOOPBACK_HOSTS:
            for cookie in client.cookies.jar:
                cookie.secure = False

    client.event_hooks["response"].append(loopback_is_a_secure_context)
    return client
