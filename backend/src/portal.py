"""
The portal: who may open the dashboard and the database viewer.

Google says who someone is; this server decides whether they may come in.

    /auth/login      to Google (OpenID Connect, authorization code + PKCE)
    /auth/callback   Google's answer: the ID token's signature, audience,
                     issuer, nonce and verified email are checked, then the
                     email is looked up in portal_people. Allowed: a session
                     cookie (8 hours). Not on the list: refused, and logged
    /auth/check      what the gateway asks before /dashboard, /db/ and the
                     admin routes (Caddy forward_auth): 200 with the person's
                     email and role, a redirect to sign in, or 403
    /auth/me         who is signed in (the dashboard asks)
    /auth/logout     ends the session

Roles: admin (everything, and managing people) and researcher (look, never
change). The owner (AIMS_PORTAL_OWNER_EMAIL) is always an admin, so the portal
cannot lock its owner out. Removing a person ends their sessions at once.

A browser signed in by cookie must send X-AIMS-Portal on anything that is not
a GET: a page on another site can make a browser send the cookie, but not a
custom header, so a session can never be used to change something from
elsewhere. The admin key (X-Admin-Key) still works for the tools and the
loopback door; it never involves a browser.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import html
import logging
import os
import secrets
import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Any, Dict, List, Optional
from urllib.parse import quote, urlencode

import httpx
import jwt
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

router = APIRouter(tags=["portal"])

GOOGLE_AUTHORIZE = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN = "https://oauth2.googleapis.com/token"
GOOGLE_KEYS = "https://www.googleapis.com/oauth2/v3/certs"
GOOGLE_ISSUERS = ["https://accounts.google.com", "accounts.google.com"]

COOKIE = "aims_portal"
SESSION_SECONDS = 8 * 3600
LOGIN_MINUTES = 10
ROLES = ("admin", "researcher")
CSRF_HEADER = "x-aims-portal"
ADMIN_PATHS = ("/api/v2/admin",)


@dataclass(frozen=True)
class Person:
    email: str
    role: str


def _env(name: str) -> str:
    return os.getenv(name, "").strip()


def configured() -> bool:
    return bool(_env("AIMS_GOOGLE_CLIENT_ID") and _env("AIMS_GOOGLE_CLIENT_SECRET")
                and _env("AIMS_PUBLIC_HOST"))


def owner_email() -> str:
    return _env("AIMS_PORTAL_OWNER_EMAIL").lower()


def _base() -> str:
    return "https://" + _env("AIMS_PUBLIC_HOST")


def redirect_uri() -> str:
    return _base() + "/auth/callback"


def _pool():
    import api_v2
    repo = api_v2.ctx.repo
    pool = getattr(repo, "_pool", None)
    if pool is None:
        raise HTTPException(status_code=503, detail="the portal needs the database")
    return pool


def _sha(token: str) -> bytes:
    return hashlib.sha256(token.encode("utf-8")).digest()


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def safe_next(path: Optional[str]) -> str:
    """Only a path on this site: never another site, whatever was asked for."""
    if not path or not path.startswith("/") or path.startswith("//") or "\\" in path \
            or path.startswith("/auth/"):
        return "/dashboard"
    return path[:500]


async def _audit(event: str, actor: str, detail: Dict[str, Any]) -> None:
    try:
        import api_v2
        await api_v2._repo().audit(event_type=event, actor_type="admin", actor_id=actor,
                                   detail=detail)
    except Exception as exc:                       # never fails a sign-in
        logger.warning("Could not audit %s: %s", event, exc)


# ------------------------------------------------------------------ sessions

async def person_for(request: Request) -> Optional[Person]:
    """The signed-in person behind this request's cookie, or None."""
    token = request.cookies.get(COOKIE) or ""
    if not token or len(token) > 200:
        return None
    try:
        pool = _pool()
    except HTTPException:
        return None
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT p.email, p.role FROM portal_sessions s
              JOIN portal_people p ON p.email = s.email
             WHERE s.token_sha256 = $1 AND s.ended_at IS NULL AND s.expires_at > now()
               AND p.removed_at IS NULL
        """, _sha(token))
    return Person(row["email"], row["role"]) if row else None


def check_csrf(request: Request) -> None:
    if request.method not in ("GET", "HEAD", "OPTIONS") and \
            request.headers.get(CSRF_HEADER) != "1":
        raise HTTPException(status_code=403,
                            detail="a signed-in browser must send X-AIMS-Portal: 1")


def _wants_page(request: Request) -> bool:
    return "text/html" in (request.headers.get("accept") or "")


def _page(title: str, body: str, status: int = 200) -> HTMLResponse:
    return HTMLResponse(status_code=status, content=(
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<title>{html.escape(title)} - AIMScribe</title></head>"
        f"<body><h1>{html.escape(title)}</h1>{body}</body></html>"))


# ------------------------------------------------------------------ sign-in

@router.get("/auth/login")
async def login(request: Request, next: str = "/dashboard"):
    if not configured():
        return _page("Google sign-in is not set up",
                     "<p>This server has no Google client yet "
                     "(<code>AIMS_GOOGLE_CLIENT_ID</code>, <code>AIMS_GOOGLE_CLIENT_SECRET</code>)."
                     " The administrator sets it once.</p>", status=503)
    state, nonce = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
    verifier = secrets.token_urlsafe(48)
    async with _pool().acquire() as conn:
        await conn.execute("DELETE FROM portal_logins WHERE created_at < now() - make_interval(mins => $1)",
                           LOGIN_MINUTES)
        await conn.execute("INSERT INTO portal_logins (state, nonce, verifier, next_path) "
                           "VALUES ($1, $2, $3, $4)", state, nonce, verifier, safe_next(next))
    query = urlencode({
        "client_id": _env("AIMS_GOOGLE_CLIENT_ID"), "redirect_uri": redirect_uri(),
        "response_type": "code", "scope": "openid email profile", "state": state,
        "nonce": nonce, "prompt": "select_account",
        "code_challenge": _b64url(hashlib.sha256(verifier.encode()).digest()),
        "code_challenge_method": "S256"})
    return RedirectResponse(f"{GOOGLE_AUTHORIZE}?{query}", status_code=302)


_keys: Optional[jwt.PyJWKClient] = None


def _verify_id_token(token: str, nonce: str) -> Dict[str, Any]:
    """Google's signature, this client as the audience, Google as the issuer."""
    global _keys
    if _keys is None:
        _keys = jwt.PyJWKClient(GOOGLE_KEYS, cache_keys=True, lifespan=3600)
    key = _keys.get_signing_key_from_jwt(token).key
    claims = jwt.decode(token, key, algorithms=["RS256"],
                        audience=_env("AIMS_GOOGLE_CLIENT_ID"), issuer=GOOGLE_ISSUERS,
                        options={"require": ["exp", "iat", "aud", "iss", "sub"]},
                        leeway=60)
    if not secrets.compare_digest(str(claims.get("nonce", "")), nonce):
        raise jwt.InvalidTokenError("nonce does not match this sign-in")
    if claims.get("email_verified") is not True or not claims.get("email"):
        raise jwt.InvalidTokenError("Google has not verified this email")
    return claims


async def _exchange(code: str, verifier: str) -> Dict[str, Any]:
    async with httpx.AsyncClient(timeout=15) as client:
        reply = await client.post(GOOGLE_TOKEN, data={
            "code": code, "client_id": _env("AIMS_GOOGLE_CLIENT_ID"),
            "client_secret": _env("AIMS_GOOGLE_CLIENT_SECRET"),
            "redirect_uri": redirect_uri(), "grant_type": "authorization_code",
            "code_verifier": verifier})
    if reply.status_code != 200:
        raise HTTPException(status_code=502, detail="Google did not accept the sign-in code")
    return reply.json()


@router.get("/auth/callback")
async def callback(request: Request, state: str = "", code: str = "", error: str = ""):
    if error or not code or not state:
        return _page("Sign-in was not completed",
                     "<p><a href='/auth/login'>Try again</a></p>", status=400)
    async with _pool().acquire() as conn:
        flow = await conn.fetchrow("""
            DELETE FROM portal_logins
             WHERE state = $1 AND created_at > now() - make_interval(mins => $2)
         RETURNING nonce, verifier, next_path
        """, state, LOGIN_MINUTES)
    if flow is None:
        return _page("This sign-in has expired",
                     "<p><a href='/auth/login'>Sign in again</a></p>", status=400)

    tokens = await _exchange(code, flow["verifier"])
    try:
        claims = await asyncio.to_thread(_verify_id_token, tokens.get("id_token", ""),
                                         flow["nonce"])
    except jwt.PyJWTError as exc:
        logger.warning("A Google sign-in failed verification: %s", exc)
        return _page("Sign-in could not be verified",
                     "<p><a href='/auth/login'>Try again</a></p>", status=401)

    email = str(claims["email"]).lower()
    name = str(claims.get("name") or "")[:200]
    import security
    address = security.client_ip(request)
    async with _pool().acquire() as conn:
        if email == owner_email():
            await conn.execute("""
                INSERT INTO portal_people (email, role, added_by, display_name)
                VALUES ($1, 'admin', 'the server (AIMS_PORTAL_OWNER_EMAIL)', $2)
                ON CONFLICT (email) DO UPDATE
                   SET role = 'admin', removed_at = NULL, removed_by = NULL
            """, email, name)
        person = await conn.fetchrow(
            "SELECT email, role FROM portal_people WHERE email = $1 AND removed_at IS NULL", email)
        if person is None:
            await _audit("portal.refused", email, {"address": address,
                                                   "reason": "not on the access list"})
            return _page("You have not been given access",
                         f"<p>{html.escape(email)} is not on this server's access list. "
                         "Ask the AIMScribe administrator to add you.</p>", status=403)
        token = secrets.token_urlsafe(32)
        await conn.execute("""
            INSERT INTO portal_sessions (token_sha256, email, expires_at, address, user_agent)
            VALUES ($1, $2, now() + make_interval(secs => $3), $4, $5)
        """, _sha(token), email, SESSION_SECONDS, address,
            (request.headers.get("user-agent") or "")[:300])
        await conn.execute("UPDATE portal_people SET last_signed_in = now(), "
                           "display_name = COALESCE(NULLIF($2, ''), display_name) "
                           "WHERE email = $1", email, name)
    await _audit("portal.signed_in", email, {"role": person["role"], "address": address})
    reply = RedirectResponse(safe_next(flow["next_path"]), status_code=302)
    reply.set_cookie(COOKIE, token, max_age=SESSION_SECONDS, path="/", secure=True,
                     httponly=True, samesite="lax")
    return reply


@router.get("/auth/logout")
@router.post("/auth/logout")
async def logout(request: Request):
    token = request.cookies.get(COOKIE) or ""
    if token:
        try:
            async with _pool().acquire() as conn:
                await conn.execute("UPDATE portal_sessions SET ended_at = now() "
                                   "WHERE token_sha256 = $1 AND ended_at IS NULL", _sha(token))
        except HTTPException:
            pass
    reply = _page("Signed out", "<p><a href='/auth/login'>Sign in again</a></p>")
    reply.delete_cookie(COOKIE, path="/")
    return reply


@router.get("/auth/me")
async def me(request: Request):
    person = await person_for(request)
    if person is None:
        return JSONResponse(status_code=401, content={"signed_in": False,
                                                      "google": configured()})
    return {"signed_in": True, "email": person.email, "role": person.role}


@router.get("/auth/check")
async def check(request: Request):
    """
    Caddy's forward_auth, before every portal path. The original path comes
    as X-Forwarded-Uri; the person's email and role go back as headers, which
    the gateway hands on (the viewer reads the role).
    """
    uri = request.headers.get("x-forwarded-uri") or "/"
    person = await person_for(request)
    if person is None:
        if _wants_page(request):
            return RedirectResponse("/auth/login?next=" + quote(safe_next(uri), safe="/"),
                                    status_code=302)
        return JSONResponse(status_code=401, content={"detail": "sign in first"})
    if uri.startswith(ADMIN_PATHS) and person.role != "admin":
        return JSONResponse(status_code=403, content={"detail": "this needs an administrator"})
    return JSONResponse(content={"ok": True},
                        headers={"X-Portal-Email": person.email, "X-Portal-Role": person.role})


# ------------------------------------------------------------------ people (admins)

class PersonRequest(BaseModel):
    email: str = Field(..., min_length=5, max_length=254,
                       pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    role: str = Field("researcher", pattern=r"^(admin|researcher)$")


class RemoveRequest(BaseModel):
    email: str = Field(..., min_length=5, max_length=254)


async def _actor(request: Request) -> str:
    person = await person_for(request)
    return person.email if person else "admin key"


def _admin():
    import api_v2
    return Depends(api_v2.require_admin)


@router.get("/api/v2/admin/people")
async def list_people(_: None = _admin()):
    async with _pool().acquire() as conn:
        rows = await conn.fetch("""
            SELECT email, role, display_name, added_by, added_at, last_signed_in
              FROM portal_people WHERE removed_at IS NULL ORDER BY role, email
        """)
    owner = owner_email()
    return {"people": [{**{k: (v.isoformat() if hasattr(v, "isoformat") else v)
                           for k, v in dict(r).items()},
                        "owner": r["email"] == owner} for r in rows],
            "google": configured(), "mail": _mail_configured()}


@router.post("/api/v2/admin/people")
async def add_person(body: PersonRequest, request: Request, _: None = _admin()):
    email = body.email.strip().lower()
    by = await _actor(request)
    async with _pool().acquire() as conn:
        await conn.execute("""
            INSERT INTO portal_people (email, role, added_by) VALUES ($1, $2, $3)
            ON CONFLICT (email) DO UPDATE
               SET role = EXCLUDED.role, added_by = EXCLUDED.added_by, added_at = now(),
                   removed_at = NULL, removed_by = NULL
        """, email, body.role, by)
    await _audit("portal.person_added", by, {"email": email, "role": body.role})
    invited = await _invite(email, body.role, by)
    return {"status": "added", "email": email, "role": body.role, "invited": invited,
            "link": _base() + "/dashboard"}


@router.post("/api/v2/admin/people/remove")
async def remove_person(body: RemoveRequest, request: Request, _: None = _admin()):
    email = body.email.strip().lower()
    if email == owner_email():
        raise HTTPException(status_code=409, detail="the owner cannot be removed")
    by = await _actor(request)
    async with _pool().acquire() as conn:
        async with conn.transaction():
            done = await conn.execute("""
                UPDATE portal_people SET removed_at = now(), removed_by = $2
                 WHERE email = $1 AND removed_at IS NULL
            """, email, by)
            await conn.execute("UPDATE portal_sessions SET ended_at = now() "
                               "WHERE email = $1 AND ended_at IS NULL", email)
    if not done.endswith("1"):
        raise HTTPException(status_code=404, detail="no such person")
    await _audit("portal.person_removed", by, {"email": email})
    return {"status": "removed", "email": email}


# ------------------------------------------------------------------ the invitation

def _mail_configured() -> bool:
    return bool(_env("AIMS_SMTP_HOST") and _env("AIMS_SMTP_FROM"))


async def _invite(email: str, role: str, by: str) -> bool:
    """An email like a shared document's: who gave access, and the link."""
    if not _mail_configured():
        return False
    message = EmailMessage()
    message["From"] = _env("AIMS_SMTP_FROM")
    message["To"] = email
    message["Subject"] = "You have been given access to AIMScribe"
    message.set_content(
        f"{by} has given you {role} access to the AIMScribe research portal.\n\n"
        f"Open {_base()}/dashboard and sign in with this Google account ({email}).\n\n"
        "If you did not expect this, you can ignore this email.\n")

    def send() -> None:
        port = int(_env("AIMS_SMTP_PORT") or "587")
        with smtplib.SMTP(_env("AIMS_SMTP_HOST"), port, timeout=20) as smtp:
            smtp.starttls(context=ssl.create_default_context())
            if _env("AIMS_SMTP_USER"):
                smtp.login(_env("AIMS_SMTP_USER"), _env("AIMS_SMTP_PASSWORD"))
            smtp.send_message(message)

    try:
        await asyncio.to_thread(send)
        return True
    except Exception as exc:
        logger.warning("The invitation to %s was not sent: %s", email, exc)
        return False


__all__ = ["router", "person_for", "check_csrf", "Person", "configured", "safe_next"]
