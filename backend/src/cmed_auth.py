"""
Who CMED's server is, hospital by hospital (HL7 SMART Backend Services).

CMED runs the software for many hospitals. A single shared key would speak for
all of them, so one leak would open every hospital's records. Instead each
hospital is its own client:

  1. AIMS LAB registers the client once: its id, the CMED hospital id(s) it may
     speak for, optionally the addresses it calls from, and its PUBLIC key.
     The private key is made on CMED's server and never leaves it.
  2. To send, CMED signs a short assertion with that private key (RFC 7523:
     iss = sub = client id, aud = our token URL, a single-use jti, at most five
     minutes long) and posts it to  POST /api/v2/oauth/token.
  3. It receives an access token that lives five minutes, names that
     hospital, and is kept here only as a hash.
  4. Every API 2 / API 3b / visits call carries  Authorization: Bearer <token>.
     A message for any other hospital is refused (HOSPITAL_NOT_ALLOWED).

What a thief gets:
  - a copy of our database: public keys and token hashes - nothing that signs;
  - one access token: five minutes, one hospital;
  - one hospital's private key: that hospital only, until it is revoked here
    (one call), and only from CMED's addresses if an address list is set.

During the switch the old X-CMED-Key still works, but only a key issued for
named hospitals, and not at all once AIMS_CMED_LEGACY_KEYS=off.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, FrozenSet, Iterable, List, Optional, Tuple

import asyncpg
import jwt
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

import api_v2
import integrity
import security

logger = logging.getLogger(__name__)

router = APIRouter(tags=["cmed-auth"])

TOKEN_PATH = "/api/v2/oauth/token"
TOKEN_LIFETIME = 300                    # seconds; SMART recommends no more than 5 minutes
ASSERTION_MAX_LIFETIME = 300            # exp may be at most this far ahead
CLOCK_LEEWAY = 60                       # CMED's clock and ours may differ a little
ALGORITHMS = ("ES384", "RS384", "ES256")
ASSERTION_TYPE = "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"

# SMART v2 scope names (resource.permissions). Sending API 2 or API 3b is
# creating a message Bundle; the visits list is reading and searching
# Encounters. SMART v1 spellings, and the names this server used first, are
# accepted as aliases and mean exactly the same.
WRITE = "system/Bundle.c"               # API 2, API 3b
READ = "system/Encounter.rs"            # the visits list, and one Encounter
ALL_SCOPES = (WRITE, READ)

_SCOPE_ALIASES = {
    WRITE: WRITE, READ: READ,
    "system/clinical.write": WRITE, "system/Encounter.read": READ,
    "system/*.write": WRITE, "system/*.read": READ,
    "system/*.*": (WRITE, READ), "system/*.cruds": (WRITE, READ),
    "system/*.c": WRITE, "system/*.rs": READ,
    "system/Bundle.write": WRITE, "system/Bundle.c": WRITE,
    "system/MessageHeader.c": WRITE, "system/MessageHeader.write": WRITE,
    "system/Encounter.rs": READ, "system/Encounter.r": READ, "system/Encounter.s": READ,
}

_PRIVATE_JWK_MEMBERS = ("d", "p", "q", "dp", "dq", "qi", "oth", "k")
_KID = re.compile(r"^[\x21-\x7e]{1,128}$")      # printable ASCII, no spaces
MAX_EXP = 10 ** 10                              # the year 2286; anything larger is noise
_RANGE_TOKEN = re.compile(r"^[A-Za-z0-9_.-]{3,64}$")


class AuthError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code = status, code


@dataclass(frozen=True)
class Principal:
    """The caller of one Channel B request, once proven."""
    client_id: str
    hospitals: FrozenSet[str]
    scopes: FrozenSet[str]
    via: str                            # "token" or "legacy_key"
    kid: Optional[str] = None

    @property
    def actor(self) -> str:
        return f"cmed:{self.client_id}"

    def may_write_for(self, cmed_hospital_id: str) -> bool:
        return cmed_hospital_id in self.hospitals


def legacy_keys_allowed() -> bool:
    return os.getenv("AIMS_CMED_LEGACY_KEYS", "on").strip().lower() not in ("off", "0",
                                                                           "false", "no")


def canonical_scopes(scopes: Iterable[str]) -> FrozenSet[str]:
    """Stored scope names, old or new, as the names checked today."""
    out = set()
    for word in scopes:
        mapped = _SCOPE_ALIASES.get(word, word)
        out.update(mapped if isinstance(mapped, tuple) else (mapped,))
    return frozenset(out)


def token_audiences(request: Request) -> List[str]:
    """
    The token URL as CMED must write it in `aud`. With AIMS_PUBLIC_BASE_URL set
    (every real server) that is the only one accepted: an assertion made for a
    staging server cannot be spent on production. Unset - a test machine - the
    address the request came in on is used instead.
    """
    out = []
    base = os.getenv("AIMS_PUBLIC_BASE_URL", "").strip().rstrip("/")
    if base:
        return [base + TOKEN_PATH]
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    host = request.headers.get("host")
    if host:
        out.append(f"{proto}://{host}{TOKEN_PATH}")
    return out


def _digest(token: str) -> bytes:
    return integrity.sha256_bytes(token.encode("utf-8"))


def _pool():
    return api_v2._repo()._pool


def granted_scopes(requested: str, allowed: Iterable[str]) -> FrozenSet[str]:
    allowed = set(allowed)
    if not requested.strip():
        return frozenset(allowed)
    wanted = set()
    for word in requested.split():
        mapped = _SCOPE_ALIASES.get(word)
        if mapped is None:
            continue
        wanted.update(mapped if isinstance(mapped, tuple) else (mapped,))
    return frozenset(wanted & allowed)


# ------------------------------------------------------------------ the token endpoint

def _oauth_error(status: int, error: str, description: str) -> JSONResponse:
    # RFC 6749 §5.2 shape, which every OAuth client library reads; the request
    # id is an extra member, which OAuth clients ignore and people can quote.
    return JSONResponse(status_code=status, headers={"Cache-Control": "no-store"},
                        content={"error": error, "error_description": description,
                                 "request_id": security.request_id()})


TOKEN_RATE = int(os.getenv("AIMS_TOKEN_RATE_PER_MINUTE", "60") or 60)


def _claimed_client(assertion: str) -> str:
    """The client an assertion says it is from, unverified - only to key its failures."""
    try:
        iss = jwt.decode(assertion, options={"verify_signature": False}).get("iss")
    except Exception:
        return "-"
    return iss if isinstance(iss, str) and _RANGE_TOKEN.match(iss) else "-"


@router.post(TOKEN_PATH)
async def token(request: Request):
    ip = security.client_ip(request)
    ok, wait = await security.rate_allow("token", ip, TOKEN_RATE)
    if not ok:
        return security.too_many(wait)
    form = await request.form()
    grant_type = str(form.get("grant_type") or "")
    assertion_type = str(form.get("client_assertion_type") or "")
    assertion = str(form.get("client_assertion") or "")
    if grant_type != "client_credentials":
        return _oauth_error(400, "unsupported_grant_type",
                            "grant_type must be client_credentials")
    if assertion_type != ASSERTION_TYPE or not assertion:
        return _oauth_error(400, "invalid_request",
                            "client_assertion_type must be jwt-bearer, with client_assertion")
    # Failures are counted per claimed client AND address: someone elsewhere
    # sending bad assertions in CMED's name cannot lock CMED's own server out,
    # and one hospital's broken client cannot lock out the others.
    failure_key = f"client:{_claimed_client(assertion)}|{ip}"
    left = await security.locked_for("cred", failure_key)
    if left > 0:
        return security.too_many(left)
    try:
        client, claims, scopes = await verify_assertion(
            assertion, token_audiences(request), ip, requested=str(form.get("scope") or ""))
    except AuthError as exc:
        logger.warning("Token refused for %s: %s", ip, exc)
        if exc.status == 401:
            await security.note_credential_failure(failure_key, ip, TOKEN_PATH)
        return _oauth_error(exc.status, exc.code, str(exc))

    access = "cat_" + secrets.token_urlsafe(32)
    expires = datetime.now(timezone.utc) + timedelta(seconds=TOKEN_LIFETIME)
    async with _pool().acquire() as conn:
        await conn.execute("""
            INSERT INTO cmed_access_tokens
                (token_sha256, client_id, cmed_hospital_ids, scopes, expires_at, kid)
            VALUES ($1, $2, $3, $4, $5, $6)
        """, _digest(access), client["client_id"], list(client["cmed_hospital_ids"]),
             sorted(scopes), expires, claims["_kid"])
        await conn.execute("UPDATE cmed_clients SET last_used_at = now() WHERE client_id = $1",
                           client["client_id"])
        # Housekeeping, a few rows at a time, so the tables never grow.
        await conn.execute("""
            DELETE FROM cmed_access_tokens WHERE token_sha256 IN (
                SELECT token_sha256 FROM cmed_access_tokens
                 WHERE expires_at < now() - interval '1 day' LIMIT 500)
        """)
        await conn.execute("""
            DELETE FROM cmed_assertion_jti WHERE (client_id, jti) IN (
                SELECT client_id, jti FROM cmed_assertion_jti
                 WHERE expires_at < now() - interval '1 hour' LIMIT 500)
        """)
    await security.mark_trusted(ip)
    return JSONResponse(headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
                        content={"access_token": access, "token_type": "bearer",
                                 "expires_in": TOKEN_LIFETIME, "scope": " ".join(sorted(scopes)),
                                 "hospital_ids": sorted(client["cmed_hospital_ids"])})


async def verify_assertion(assertion: str, audiences: List[str], ip: str, *,
                           requested: str = "") -> Tuple[Dict[str, Any], Dict[str, Any],
                                                         FrozenSet[str]]:
    """
    The client's signed assertion, checked in full, and the scopes it may
    have. Returns (client, claims, scopes). The assertion's jti is spent only
    when everything else passed, so a request refused for its scope can be
    sent again with the same assertion.
    """
    try:
        header = jwt.get_unverified_header(assertion)
        unverified = jwt.decode(assertion, options={"verify_signature": False})
    except jwt.PyJWTError:
        raise AuthError(400, "invalid_client", "client_assertion is not a JWT")
    alg, kid = header.get("alg"), header.get("kid")
    client_id = unverified.get("iss")
    if alg not in ALGORITHMS:
        raise AuthError(400, "invalid_client", f"alg must be one of {', '.join(ALGORITHMS)}")
    if not isinstance(kid, str) or not _KID.match(kid):
        raise AuthError(400, "invalid_client",
                        "the assertion header needs a kid of printable ASCII, 1-128 characters")
    if not isinstance(client_id, str) or not _RANGE_TOKEN.match(client_id):
        raise AuthError(400, "invalid_client", "iss must be the client id")

    try:
        async with _pool().acquire() as conn:
            client = await conn.fetchrow("""
                SELECT *, expires_at <= now() AS expired FROM cmed_clients
                 WHERE client_id = $1
            """, client_id)
            key = await conn.fetchrow("""
                SELECT alg, public_jwk, revoked_at FROM cmed_client_keys
                 WHERE client_id = $1 AND kid = $2
            """, client_id, kid) if client else None
    except (asyncpg.exceptions.DataError, UnicodeError):
        raise AuthError(401, "invalid_client", "client_id or kid is not valid text")
    # Each case named, so whoever sets up the sending side can fix it alone.
    # Client ids are not secret; every refusal still counts towards the lockout.
    if client is None:
        raise AuthError(401, "invalid_client", f'no client "{client_id}" is registered')
    if client["revoked_at"] is not None:
        raise AuthError(401, "invalid_client", f'client "{client_id}" was revoked on '
                                               f'{client["revoked_at"]:%Y-%m-%d}')
    if client["expired"]:
        raise AuthError(401, "invalid_client", f'client "{client_id}" expired on '
                                               f'{client["expires_at"]:%Y-%m-%d}; renew it')
    if key is None:
        raise AuthError(401, "invalid_client",
                        f'kid "{kid}" is not registered for client "{client_id}"')
    if key["revoked_at"] is not None:
        raise AuthError(401, "invalid_client",
                        f'kid "{kid}" of client "{client_id}" was revoked')
    if key["alg"] != alg:
        raise AuthError(401, "invalid_client", "alg does not match the registered key")
    if not security.ip_allowed(ip, client["allowed_cidrs"]):
        raise AuthError(401, "invalid_client", "not allowed from this address")

    jwk = key["public_jwk"]
    if isinstance(jwk, str):
        jwk = json.loads(jwk)
    try:
        public = jwt.PyJWK(jwk, algorithm=alg).key
        claims = jwt.decode(assertion, public, algorithms=[alg], audience=audiences,
                            issuer=client_id, leeway=CLOCK_LEEWAY,
                            options={"require": ["iss", "sub", "aud", "exp", "jti"]})
    except (jwt.ExpiredSignatureError, jwt.ImmatureSignatureError) as exc:
        # The signature is good; the clocks disagree. Said plainly, and not
        # counted as a wrong credential - the fix is NTP on CMED's server.
        raise AuthError(400, "invalid_client",
                        f"clock skew: {exc}. This server allows {CLOCK_LEEWAY} s; "
                        "keep the sending server's clock synchronised (NTP)")
    except jwt.InvalidSignatureError:
        raise AuthError(401, "invalid_client",
                        f'signature does not match the public key registered as kid "{kid}" '
                        f'of client "{client_id}": signed with a different private key')
    except (jwt.PyJWTError, OverflowError, ValueError, TypeError) as exc:
        raise AuthError(401, "invalid_client", f"assertion rejected: {exc}")
    if claims.get("sub") != client_id:
        raise AuthError(401, "invalid_client", "sub must equal iss")
    exp = claims["exp"]
    if type(exp) is not int or not 0 < exp < MAX_EXP:
        raise AuthError(401, "invalid_client", "exp must be a whole number of seconds")
    now = time.time()
    if exp > now + ASSERTION_MAX_LIFETIME + CLOCK_LEEWAY:
        raise AuthError(400, "invalid_client",
                        "exp may be at most five minutes ahead (clock skew, or too long a "
                        "lifetime)")
    jti = claims["jti"]
    if not isinstance(jti, str) or not 8 <= len(jti) <= 256 or "\x00" in jti:
        raise AuthError(401, "invalid_client", "jti must be 8 to 256 characters")
    scopes = granted_scopes(requested, canonical_scopes(client["scopes"]))
    if not scopes:
        raise AuthError(400, "invalid_scope", "none of the requested scopes is granted")
    try:
        async with _pool().acquire() as conn:
            fresh = await conn.fetchval("""
                INSERT INTO cmed_assertion_jti (client_id, jti, expires_at)
                VALUES ($1, $2, to_timestamp($3)) ON CONFLICT DO NOTHING RETURNING 1
            """, client_id, jti, float(exp + CLOCK_LEEWAY))
    except (asyncpg.exceptions.DataError, UnicodeError):
        raise AuthError(401, "invalid_client", "jti is not acceptable")
    if not fresh:
        raise AuthError(401, "invalid_client", "this assertion was already used")
    claims = dict(claims, _kid=kid)
    return dict(client), claims, scopes


# ------------------------------------------------------------------ every Channel B call

async def authenticate(request: Request, scope: str) -> Principal:
    """The caller of an API 2, API 3b or visits request, or AuthError."""
    header = request.headers.get("authorization") or ""
    if header[:7].lower() == "bearer ":
        principal = await _from_token(header[7:].strip(), security.client_ip(request))
    elif request.headers.get("x-cmed-key"):
        principal = await _from_legacy_key(request.headers["x-cmed-key"])
    else:
        raise AuthError(401, "INVALID_TOKEN", "Send Authorization: Bearer <access token>.")
    if scope not in principal.scopes:
        raise AuthError(403, "INSUFFICIENT_SCOPE", f"This token does not carry {scope}.")
    return principal


async def _from_token(supplied: str, ip: str) -> Principal:
    if not supplied:
        raise AuthError(401, "INVALID_TOKEN", "The access token is missing.")
    row = await api_v2._repo().cmed_token_lookup(_digest(supplied))
    if row is None:
        raise AuthError(401, "INVALID_TOKEN",
                        "The access token is unknown or expired; ask for a new one.")
    if not security.ip_allowed(ip, row["allowed_cidrs"]):
        raise AuthError(401, "INVALID_TOKEN", "Not allowed from this address.")
    return Principal(client_id=row["client_id"], hospitals=frozenset(row["cmed_hospital_ids"]),
                     scopes=canonical_scopes(row["scopes"]), via="token")


async def _from_legacy_key(supplied: str) -> Principal:
    if not legacy_keys_allowed():
        raise AuthError(401, "INVALID_KEY",
                        "Static keys are switched off; use the token method.")
    row = await api_v2._repo().cmed_key_lookup(_digest(supplied))
    if row is None:
        raise AuthError(401, "INVALID_KEY", "The CMED key is missing or not valid.")
    if not row["cmed_hospital_ids"]:
        raise AuthError(403, "KEY_NOT_BOUND",
                        "This key names no hospital. Ask AIMS LAB for a key per hospital.")
    return Principal(client_id=f"key:{row['label']}",
                     hospitals=frozenset(row["cmed_hospital_ids"]),
                     scopes=frozenset(ALL_SCOPES), via="legacy_key")


# ------------------------------------------------------------------ registering clients (admin)

def jwk_thumbprint(jwk: Dict[str, Any]) -> str:
    """RFC 7638: the key's own name, so the same key always has the same kid."""
    members = {"EC": ("crv", "kty", "x", "y"), "RSA": ("e", "kty", "n")}.get(jwk.get("kty"))
    if members is None:
        raise ValueError("kty must be EC or RSA")
    canonical = json.dumps({m: jwk[m] for m in members}, separators=(",", ":"),
                           sort_keys=True)
    import base64
    return base64.urlsafe_b64encode(hashlib.sha256(canonical.encode()).digest()) \
        .decode().rstrip("=")


def check_public_jwk(jwk: Dict[str, Any], alg: str) -> str:
    """A public key fit for `alg`, or ValueError. Returns its kid."""
    if not isinstance(jwk, dict):
        raise ValueError("jwk must be an object")
    if any(m in jwk for m in _PRIVATE_JWK_MEMBERS):
        raise ValueError("this is a PRIVATE key. Send only the public half; "
                         "the private key must never leave CMED")
    want = {"ES384": ("EC", "P-384"), "ES256": ("EC", "P-256"), "RS384": ("RSA", None)}[alg]
    if jwk.get("kty") != want[0] or (want[1] and jwk.get("crv") != want[1]):
        raise ValueError(f"{alg} needs kty {want[0]}" + (f" and crv {want[1]}" if want[1] else ""))
    try:
        key = jwt.PyJWK(jwk, algorithm=alg).key
    except jwt.PyJWTError as exc:
        raise ValueError(f"not a usable key: {exc}")
    if want[0] == "RSA" and key.key_size < 2048:
        raise ValueError("RSA keys must be at least 2048 bits")
    kid = str(jwk.get("kid") or jwk_thumbprint(jwk))
    if not _KID.match(kid):
        raise ValueError("kid must be printable ASCII without spaces, 1-128 characters")
    return kid


class ClientRequest(BaseModel):
    client_id: str = Field(..., pattern=r"^[A-Za-z0-9_.-]{3,64}$")
    label: str = Field(..., min_length=1, max_length=200)
    cmed_hospital_ids: List[str] = Field(..., min_length=1, max_length=20)
    allowed_cidrs: Optional[List[str]] = Field(None, max_length=50)
    expires_in_days: int = Field(365, ge=1, le=730)
    created_by: str = Field(..., min_length=1, max_length=128)
    jwk: Dict[str, Any]
    alg: str = Field("ES384", pattern=r"^(ES384|RS384|ES256)$")
    # The identifier systems this hospital's FHIR resources use, when they
    # carry several identifiers: {"Patient": "https://...", "Practitioner": ...,
    # "Organization": ...}. Optional; the server-wide systems apply otherwise.
    identifier_systems: Optional[Dict[str, str]] = None


class RenewRequest(BaseModel):
    days: int = Field(365, ge=1, le=730)
    created_by: str = Field(..., min_length=1, max_length=128)


class KeyRequest(BaseModel):
    jwk: Dict[str, Any]
    alg: str = Field("ES384", pattern=r"^(ES384|RS384|ES256)$")
    created_by: str = Field(..., min_length=1, max_length=128)


def _check_ids(ids: Iterable[str]) -> List[str]:
    out = []
    for value in ids:
        try:
            out.append(integrity.safe_identifier(value, field="cmed_hospital_id"))
        except ValueError:
            raise HTTPException(status_code=422, detail=f"'{value}' is not a valid hospital id")
    return sorted(set(out))


def _check_systems(systems: Optional[Dict[str, str]]) -> Optional[Dict[str, str]]:
    if not systems:
        return None
    out = {}
    for kind, uri in systems.items():
        if kind not in ("Patient", "Practitioner", "Organization"):
            raise HTTPException(status_code=422,
                                detail="identifier_systems names Patient, Practitioner "
                                       "or Organization")
        if not isinstance(uri, str) or not re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:\S{1,250}$", uri):
            raise HTTPException(status_code=422, detail=f"identifier_systems.{kind} must be a URI")
        out[kind] = uri
    return out


_SYSTEMS_CACHE: Dict[str, Tuple[float, Dict[str, str]]] = {}


async def client_systems(client_id: str) -> Dict[str, str]:
    """A hospital's own identifier systems (registered with it), cached a minute."""
    hit = _SYSTEMS_CACHE.get(client_id)
    if hit and time.monotonic() - hit[0] < 60:
        return hit[1]
    systems: Dict[str, str] = {}
    try:
        async with _pool().acquire() as conn:
            raw = await conn.fetchval(
                "SELECT identifier_systems FROM cmed_clients WHERE client_id = $1", client_id)
        if isinstance(raw, str):
            raw = json.loads(raw)
        if isinstance(raw, dict):
            systems = {k: v for k, v in raw.items() if isinstance(v, str)}
    except Exception:
        systems = {}
    _SYSTEMS_CACHE[client_id] = (time.monotonic(), systems)
    return systems


# The interim static key is going away: every reply it authenticates says so,
# in the two standard headers (RFC 9745 Deprecation, RFC 8594 Sunset).
DEPRECATED_SINCE = 1792454400                  # 2026-10-20, when the token method shipped


def legacy_headers() -> Dict[str, str]:
    sunset = os.getenv("AIMS_CMED_LEGACY_SUNSET", "2027-01-31").strip()
    try:
        when = datetime.fromisoformat(sunset).replace(tzinfo=timezone.utc)
    except ValueError:
        when = datetime(2027, 1, 31, tzinfo=timezone.utc)
    from email.utils import format_datetime
    return {"Deprecation": f"@{DEPRECATED_SINCE}",
            "Sunset": format_datetime(when, usegmt=True),
            "Link": f'<{TOKEN_PATH}>; rel="successor-version"'}


def _check_cidrs(cidrs: Optional[List[str]]) -> Optional[List[str]]:
    if not cidrs:
        return None
    import ipaddress
    try:
        return [str(ipaddress.ip_network(c.strip(), strict=False)) for c in cidrs]
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"allowed_cidrs: {exc}")


@router.post("/api/v2/admin/cmed-client")
async def register_client(body: ClientRequest, _: None = Depends(api_v2.require_admin)):
    """One hospital's client, with its first public key."""
    hospitals = _check_ids(body.cmed_hospital_ids)
    cidrs = _check_cidrs(body.allowed_cidrs)
    systems = _check_systems(body.identifier_systems)
    try:
        kid = check_public_jwk(body.jwk, body.alg)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    expires = datetime.now(timezone.utc) + timedelta(days=body.expires_in_days)
    async with _pool().acquire() as conn:
        async with conn.transaction():
            if await conn.fetchval("SELECT 1 FROM cmed_clients WHERE client_id = $1",
                                   body.client_id):
                raise HTTPException(status_code=409, detail="that client id exists; "
                                    "add a key to it or pick another id")
            await conn.execute("""
                INSERT INTO cmed_clients (client_id, label, cmed_hospital_ids, allowed_cidrs,
                                          created_by, expires_at, scopes, identifier_systems)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            """, body.client_id, body.label, hospitals, cidrs, body.created_by, expires,
                list(ALL_SCOPES), json.dumps(systems) if systems else None)
            await conn.execute("""
                INSERT INTO cmed_client_keys (client_id, kid, alg, public_jwk)
                VALUES ($1, $2, $3, $4)
            """, body.client_id, kid, body.alg, json.dumps(body.jwk))
    await api_v2._repo().audit(event_type="cmed_client.registered", actor_type="admin",
                               actor_id=body.created_by,
                               detail={"client_id": body.client_id, "hospitals": hospitals,
                                       "kid": kid, "alg": body.alg, "cidrs": cidrs})
    return {"client_id": body.client_id, "kid": kid, "cmed_hospital_ids": hospitals,
            "allowed_cidrs": cidrs, "expires_at": expires.isoformat(),
            "token_url": TOKEN_PATH}


@router.post("/api/v2/admin/cmed-client/{client_id}/key")
async def add_key(client_id: str, body: KeyRequest, _: None = Depends(api_v2.require_admin)):
    """A second key, for rotation: add the new one, let CMED switch, revoke the old."""
    try:
        kid = check_public_jwk(body.jwk, body.alg)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    async with _pool().acquire() as conn:
        if not await conn.fetchval("SELECT 1 FROM cmed_clients WHERE client_id = $1 "
                                   "AND revoked_at IS NULL", client_id):
            raise HTTPException(status_code=404, detail="no active client with that id")
        await conn.execute("""
            INSERT INTO cmed_client_keys (client_id, kid, alg, public_jwk) VALUES ($1,$2,$3,$4)
            ON CONFLICT (client_id, kid) DO NOTHING
        """, client_id, kid, body.alg, json.dumps(body.jwk))
    await api_v2._repo().audit(event_type="cmed_client.key_added", actor_type="admin",
                               actor_id=body.created_by,
                               detail={"client_id": client_id, "kid": kid})
    return {"client_id": client_id, "kid": kid}


@router.post("/api/v2/admin/cmed-client/{client_id}/key/{kid}/revoke")
async def revoke_key(client_id: str, kid: str, _: None = Depends(api_v2.require_admin)):
    """The key, and every live token it bought - a stolen key stops at once."""
    async with _pool().acquire() as conn:
        async with conn.transaction():
            done = await conn.execute("""
                UPDATE cmed_client_keys SET revoked_at = now()
                 WHERE client_id = $1 AND kid = $2 AND revoked_at IS NULL
            """, client_id, kid)
            tokens = await conn.execute("""
                UPDATE cmed_access_tokens SET revoked_at = now()
                 WHERE client_id = $1 AND kid = $2 AND revoked_at IS NULL
            """, client_id, kid)
    if done.endswith(" 0"):
        raise HTTPException(status_code=404, detail="no active key with that id")
    await api_v2._repo().audit(event_type="cmed_client.key_revoked", actor_type="admin",
                               detail={"client_id": client_id, "kid": kid, "tokens": tokens})
    return {"status": "revoked", "client_id": client_id, "kid": kid}


@router.post("/api/v2/admin/cmed-client/{client_id}/revoke")
async def revoke_client(client_id: str, _: None = Depends(api_v2.require_admin)):
    """The hospital's client, its keys and every live token - effective at once."""
    async with _pool().acquire() as conn:
        async with conn.transaction():
            done = await conn.execute("UPDATE cmed_clients SET revoked_at = now() "
                                      "WHERE client_id = $1 AND revoked_at IS NULL", client_id)
            await conn.execute("UPDATE cmed_client_keys SET revoked_at = now() "
                               "WHERE client_id = $1 AND revoked_at IS NULL", client_id)
            tokens = await conn.execute("UPDATE cmed_access_tokens SET revoked_at = now() "
                                        "WHERE client_id = $1 AND revoked_at IS NULL",
                                        client_id)
    if done.endswith(" 0"):
        raise HTTPException(status_code=404, detail="no active client with that id")
    await api_v2._repo().audit(event_type="cmed_client.revoked", actor_type="admin",
                               detail={"client_id": client_id, "tokens": tokens})
    return {"status": "revoked", "client_id": client_id}


@router.post("/api/v2/admin/cmed-client/{client_id}/renew")
async def renew_client(client_id: str, body: RenewRequest,
                       _: None = Depends(api_v2.require_admin)):
    """Another period for a hospital's client, counted from today or its end, later."""
    async with _pool().acquire() as conn:
        expires = await conn.fetchval("""
            UPDATE cmed_clients
               SET expires_at = GREATEST(expires_at, now()) + make_interval(days => $2)
             WHERE client_id = $1 AND revoked_at IS NULL
            RETURNING expires_at
        """, client_id, body.days)
    if expires is None:
        raise HTTPException(status_code=404, detail="no active client with that id")
    await api_v2._repo().audit(event_type="cmed_client.renewed", actor_type="admin",
                               actor_id=body.created_by,
                               detail={"client_id": client_id, "expires_at": expires.isoformat()})
    return {"client_id": client_id, "expires_at": expires.isoformat()}


EXPIRY_WARNING_DAYS = 30


async def expiring_clients(days: int = EXPIRY_WARNING_DAYS, *, alert: bool = True) -> List[Dict[str, Any]]:
    """
    Clients that stop working within `days`. With alert, one cmed_client_expiring
    alert per client per day - for the monitor or the sweep to call.
    """
    async with _pool().acquire() as conn:
        rows = await conn.fetch("""
            SELECT client_id, label, cmed_hospital_ids, expires_at FROM cmed_clients
             WHERE revoked_at IS NULL AND expires_at < now() + make_interval(days => $1)
             ORDER BY expires_at
        """, days)
        out = [{"client_id": r["client_id"], "label": r["label"],
                "cmed_hospital_ids": list(r["cmed_hospital_ids"]),
                "expires_at": r["expires_at"].isoformat()} for r in rows]
        if alert:
            for c in out:
                raised = await conn.fetchval("""
                    SELECT 1 FROM integrity_alerts
                     WHERE alert_type = 'cmed_client_expiring' AND resolved_at IS NULL
                       AND detail->>'client_id' = $1 AND raised_at > now() - interval '1 day'
                """, c["client_id"])
                if not raised:
                    await api_v2._repo().raise_alert(alert_type="cmed_client_expiring",
                                                     severity="warning", detail=c)
    return out


@router.get("/api/v2/admin/cmed-clients/expiring")
async def list_expiring(days: int = EXPIRY_WARNING_DAYS,
                        _: None = Depends(api_v2.require_admin)):
    return {"days": days, "clients": await expiring_clients(max(1, min(days, 365)))}


@router.get("/api/v2/admin/cmed-clients")
async def list_clients(_: None = Depends(api_v2.require_admin)):
    async with _pool().acquire() as conn:
        rows = await conn.fetch("""
            SELECT c.client_id, c.label, c.cmed_hospital_ids, c.allowed_cidrs, c.created_at,
                   c.expires_at, c.revoked_at, c.last_used_at,
                   COALESCE(array_agg(k.kid) FILTER (WHERE k.revoked_at IS NULL), '{}') AS kids
              FROM cmed_clients c LEFT JOIN cmed_client_keys k USING (client_id)
             GROUP BY c.client_id ORDER BY c.client_id
        """)
    return {"clients": [{k: (v.isoformat() if isinstance(v, datetime) else v)
                         for k, v in dict(r).items()} for r in rows]}


__all__ = ["router", "authenticate", "verify_assertion", "Principal", "AuthError",
           "WRITE", "READ", "ALL_SCOPES", "TOKEN_PATH", "TOKEN_LIFETIME", "granted_scopes",
           "canonical_scopes", "check_public_jwk", "jwk_thumbprint", "legacy_keys_allowed",
           "legacy_headers", "client_systems", "expiring_clients"]
