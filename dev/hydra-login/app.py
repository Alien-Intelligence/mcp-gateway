"""Login and consent UI for the development Hydra.

Hydra delegates authentication to an operator-written app: this is that app.
It authenticates the test personas and, at consent, binds the RFC 8707
``resource`` parameter to the access token audience (Hydra has no native
resource-indicator support).
"""

import hmac
import html
import json
import logging
import os
import secrets
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from starlette.routing import Route

logger = logging.getLogger("hydra-login")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

CSRF_COOKIE = "login_csrf"
HYDRA_TIMEOUT = httpx.Timeout(10.0, connect=5.0)
ADMIN_PATH = "/admin/oauth2/auth/requests"


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"required environment variable {name} is not set")
    return value


HYDRA_ADMIN_URL = _require_env("HYDRA_ADMIN_URL").rstrip("/")
USER_PASSWORD = _require_env("E2E_USER_PASSWORD")
ALLOWED_RESOURCES = frozenset(
    item.strip() for item in _require_env("ALLOWED_RESOURCES").split(",") if item.strip()
)
if not ALLOWED_RESOURCES:
    raise RuntimeError("ALLOWED_RESOURCES must list at least one resource")
USERS: dict[str, dict[str, Any]] = json.loads(
    (Path(__file__).with_name("users.json")).read_text(encoding="utf-8")
)

LOGIN_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Sign in</title></head>
<body>
<h1>Sign in</h1>
{error}
<form method="post" action="/login">
  <input type="hidden" name="login_challenge" value="{challenge}">
  <input type="hidden" name="csrf_token" value="{csrf}">
  <label>Email <input type="email" name="email" autocomplete="username" required></label>
  <label>Password <input type="password" name="password" autocomplete="current-password" required></label>
  <button type="submit">Sign in</button>
</form>
</body></html>"""


class HydraError(Exception):
    """Hydra admin API call failed or answered unexpectedly."""


async def _hydra(
    client: httpx.AsyncClient,
    method: str,
    path: str,
    challenge_param: str,
    challenge: str,
    body: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    url = f"{HYDRA_ADMIN_URL}{path}"
    try:
        response = await client.request(
            method, url, params={challenge_param: challenge}, json=body, timeout=HYDRA_TIMEOUT
        )
    except httpx.HTTPError as exc:
        logger.error("hydra admin %s %s failed: %r", method, path, exc)
        raise HydraError(f"hydra admin unreachable: {type(exc).__name__}") from exc
    if response.status_code >= 400:
        logger.error(
            "hydra admin %s %s -> %s %s", method, path, response.status_code, response.text[:300]
        )
        raise HydraError(f"hydra admin answered {response.status_code}")
    payload = response.json()
    if not isinstance(payload, dict):
        raise HydraError("hydra admin answered a non-object body")
    return payload


def _bad_gateway(exc: HydraError) -> Response:
    return HTMLResponse(f"<h1>Bad gateway</h1><p>{html.escape(str(exc))}</p>", status_code=502)


def _render_login(challenge: str, csrf: str, error: str | None, status: int) -> Response:
    error_html = f'<p role="alert">{html.escape(error)}</p>' if error else ""
    page = LOGIN_PAGE.format(
        error=error_html, challenge=html.escape(challenge, quote=True), csrf=csrf
    )
    response = HTMLResponse(page, status_code=status)
    # Plain HTTP on localhost in dev, so the cookie cannot be marked Secure.
    response.set_cookie(CSRF_COOKIE, csrf, httponly=True, samesite="lax", path="/")
    return response


async def login_get(request: Request) -> Response:
    challenge = request.query_params.get("login_challenge")
    if not challenge:
        return PlainTextResponse("missing login_challenge", status_code=400)
    async with httpx.AsyncClient() as client:
        try:
            login_request = await _hydra(
                client, "GET", f"{ADMIN_PATH}/login", "login_challenge", challenge
            )
            if login_request.get("skip"):
                accepted = await _hydra(
                    client,
                    "PUT",
                    f"{ADMIN_PATH}/login/accept",
                    "login_challenge",
                    challenge,
                    {"subject": login_request["subject"]},
                )
                return RedirectResponse(accepted["redirect_to"], status_code=303)
        except HydraError as exc:
            return _bad_gateway(exc)
    return _render_login(challenge, secrets.token_urlsafe(32), None, 200)


async def login_post(request: Request) -> Response:
    body = parse_qs((await request.body()).decode("utf-8"), keep_blank_values=True)
    field = {key: values[0] for key, values in body.items() if values}
    challenge = field.get("login_challenge", "")
    cookie_csrf = request.cookies.get(CSRF_COOKIE, "")
    form_csrf = field.get("csrf_token", "")
    if not challenge:
        return PlainTextResponse("missing login_challenge", status_code=400)
    if not cookie_csrf or not hmac.compare_digest(cookie_csrf, form_csrf):
        logger.warning("login rejected: csrf token mismatch")
        return PlainTextResponse("invalid csrf token", status_code=403)

    email = field.get("email", "").strip().lower()
    password = field.get("password", "")
    user = USERS.get(email)
    password_ok = hmac.compare_digest(password.encode(), USER_PASSWORD.encode())
    if user is None or not password_ok:
        logger.info("login refused for %r", email)
        return _render_login(challenge, secrets.token_urlsafe(32), "Invalid credentials.", 401)

    async with httpx.AsyncClient() as client:
        try:
            accepted = await _hydra(
                client,
                "PUT",
                f"{ADMIN_PATH}/login/accept",
                "login_challenge",
                challenge,
                {"subject": user["subject"], "remember": False},
            )
        except HydraError as exc:
            return _bad_gateway(exc)
    return RedirectResponse(accepted["redirect_to"], status_code=303)


def _requested_resources(request_url: str) -> list[str]:
    return parse_qs(urlsplit(request_url).query).get("resource", [])


def granted_audience(request_url: str, requested_audience: list[str]) -> list[str]:
    """Return the allowed audiences: RFC 8707 resources plus requested audiences."""
    wanted = [*_requested_resources(request_url), *requested_audience]
    granted: list[str] = []
    for value in wanted:
        if value in ALLOWED_RESOURCES and value not in granted:
            granted.append(value)
        elif value not in ALLOWED_RESOURCES:
            logger.info("audience %r not allowed, not granted", value)
    return granted


async def consent_get(request: Request) -> Response:
    challenge = request.query_params.get("consent_challenge")
    if not challenge:
        return PlainTextResponse("missing consent_challenge", status_code=400)
    async with httpx.AsyncClient() as client:
        try:
            consent = await _hydra(
                client, "GET", f"{ADMIN_PATH}/consent", "consent_challenge", challenge
            )
            user = next((u for u in USERS.values() if u["subject"] == consent.get("subject")), None)
            if user is None:
                raise HydraError("consent request for an unknown subject")
            # First-party identity provider: consent is implicit, there is no consent UI.
            accepted = await _hydra(
                client,
                "PUT",
                f"{ADMIN_PATH}/consent/accept",
                "consent_challenge",
                challenge,
                {
                    "grant_scope": consent["requested_scope"],
                    "grant_access_token_audience": granted_audience(
                        consent["request_url"], consent.get("requested_access_token_audience") or []
                    ),
                    "remember": False,
                    "session": {
                        "access_token": {
                            "email": user["email"],
                            "email_verified": True,
                            "org": user["org"],
                            "groups": user["groups"],
                        },
                        "id_token": {"email": user["email"], "email_verified": True},
                    },
                },
            )
        except (HydraError, KeyError) as exc:
            logger.error("consent failed: %r", exc)
            return _bad_gateway(HydraError(str(exc)))
    return RedirectResponse(accepted["redirect_to"], status_code=303)


async def healthz(_: Request) -> Response:
    return PlainTextResponse("ok")


app = Starlette(
    routes=[
        Route("/login", login_get, methods=["GET"]),
        Route("/login", login_post, methods=["POST"]),
        Route("/consent", consent_get, methods=["GET"]),
        Route("/healthz", healthz, methods=["GET"]),
    ]
)
