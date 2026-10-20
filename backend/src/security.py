"""
Request-level protection shared by every route.

    client_ip(request)      the caller's real address, read from the proxy's
                            X-Forwarded-For only when the peer IS the proxy
    request ids             every request carries one (X-Request-ID): in each
                            log line, in the reply header, in every error body
    rate limits             per address, shared by every API process through
                            Redis (in-process if Redis is down)
    lockout                 by CREDENTIAL, not by address: a PC with a stale
                            token, or one revoked hospital, never locks out the
                            clinic or the other hospitals behind the same
                            address. Requests with no credential at all are
                            counted per address, with a much higher threshold.
    problem+json            every error outside FHIR and OAuth is RFC 9457,
                            keeping "detail" for the recorders that read it

Exempt from address limits and address lockout: an address that authenticated
successfully in the last 15 minutes, and any address on a CMED client's
allow-list.
"""
from __future__ import annotations

import contextvars
import hashlib
import ipaddress
import logging
import os
import re
import time
import uuid
from collections import OrderedDict
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------ request ids

REQUEST_ID: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")
_SAFE_ID = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")

_factory = logging.getLogRecordFactory()


def _record_with_id(*args, **kwargs):
    record = _factory(*args, **kwargs)
    record.request_id = REQUEST_ID.get()
    return record


logging.setLogRecordFactory(_record_with_id)


def request_id() -> str:
    return REQUEST_ID.get()


# ------------------------------------------------------------------ addresses

# Docker's own networks and loopback. Caddy reaches the API from one of these;
# nothing on the internet does, so a forwarded header from anywhere else is
# the caller's own claim and is ignored.
_DEFAULT_PROXIES = "127.0.0.0/8,::1/128,172.16.0.0/12,10.0.0.0/8,192.168.0.0/16"


def _networks(text: str) -> List[ipaddress._BaseNetwork]:
    nets = []
    for part in (text or "").split(","):
        part = part.strip()
        if part:
            try:
                nets.append(ipaddress.ip_network(part, strict=False))
            except ValueError:
                logger.warning("Ignoring '%s' in a network list: not an address range", part)
    return nets


def parse_ip(text: str) -> Optional[ipaddress._BaseAddress]:
    """An address as a proxy may write it: brackets and a port are dropped,
    and an IPv4 address carried in IPv6 is read as the IPv4 address."""
    text = (text or "").strip()
    if not text:
        return None
    if text.startswith("["):                          # [2001:db8::1]:443
        text = text[1:].split("]", 1)[0]
    elif text.count(":") == 1:                        # 203.0.113.9:5555
        text = text.split(":", 1)[0]
    try:
        ip = ipaddress.ip_address(text)
    except ValueError:
        return None
    mapped = getattr(ip, "ipv4_mapped", None)
    return mapped or ip


def _in(address: str, nets: Iterable[ipaddress._BaseNetwork]) -> bool:
    ip = parse_ip(address)
    return ip is not None and any(ip.version == n.version and ip in n for n in nets)


_PROXIES = _networks(os.getenv("AIMS_TRUSTED_PROXIES", _DEFAULT_PROXIES))


def client_ip(request: Request) -> str:
    """
    The address the request really came from. Caddy writes X-Forwarded-For
    itself, overwriting whatever the caller sent, so its last entry is the
    one Caddy saw; that is trusted only when the connection is from Caddy.
    A forwarded value that cannot be read is NOT replaced by the proxy's own
    address - that would put every caller behind one key.
    """
    client = getattr(request, "client", None)
    peer = client.host if client else ""
    if peer and _in(peer, _PROXIES):
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded.strip():
            last = forwarded.split(",")[-1]
            ip = parse_ip(last)
            if ip is not None:
                return str(ip)
            logger.warning("The proxy sent an unreadable X-Forwarded-For: %r", forwarded[:100])
            return "xff-unparsed"
    ip = parse_ip(peer)
    return str(ip) if ip is not None else (peer or "unknown")


def ip_allowed(address: str, cidrs: Optional[Iterable[str]]) -> bool:
    """True when no list is set, or the address is inside one of its ranges."""
    nets = _networks(",".join(cidrs)) if cidrs else []
    return not nets or _in(address, nets)


# ------------------------------------------------------------------ counters

class RateLimiter:
    """
    A token bucket per key: `rate` requests per `per` seconds, bursting to
    `rate`. Old keys are dropped so memory stays bounded whatever the traffic.
    The in-process form, used when Redis is not there and in tests.
    """

    def __init__(self, rate: int, per: float = 60.0, *, max_keys: int = 50_000,
                 clock: Callable[[], float] = time.monotonic):
        self.rate = max(1, int(rate))
        self.per = float(per)
        self._buckets: "OrderedDict[str, Tuple[float, float]]" = OrderedDict()
        self._max = max_keys
        self._clock = clock

    def allow(self, key: str, cost: float = 1.0) -> Tuple[bool, float]:
        """(allowed, seconds until one more request would be)."""
        now = self._clock()
        tokens, at = self._buckets.pop(key, (float(self.rate), now))
        tokens = min(float(self.rate), tokens + (now - at) * self.rate / self.per)
        ok = tokens >= cost
        if ok:
            tokens -= cost
        self._buckets[key] = (tokens, now)
        while len(self._buckets) > self._max:
            self._buckets.popitem(last=False)
        wait = 0.0 if ok else (cost - tokens) * self.per / self.rate
        return ok, wait


class FailureTracker:
    """
    Wrong credentials per key. After `limit` inside `window` seconds the key
    is locked out for `lockout` seconds; `fail` returns True once per lockout
    (that is when the alert is raised), not once per attempt.
    """

    def __init__(self, limit: int = 10, window: float = 300.0, lockout: float = 900.0, *,
                 clock: Callable[[], float] = time.monotonic, max_keys: int = 50_000):
        self.limit, self.window, self.lockout = limit, window, lockout
        self._clock = clock
        self._fails: "OrderedDict[str, List[float]]" = OrderedDict()
        self._locked: Dict[str, float] = {}
        self._max = max_keys

    def locked(self, key: str) -> float:
        until = self._locked.get(key)
        if until is None:
            return 0.0
        left = until - self._clock()
        if left <= 0:
            self._locked.pop(key, None)
            return 0.0
        return left

    def fail(self, key: str) -> bool:
        now = self._clock()
        recent = [t for t in self._fails.pop(key, []) if now - t < self.window]
        recent.append(now)
        self._fails[key] = recent
        while len(self._fails) > self._max:
            self._fails.popitem(last=False)
        if len(recent) >= self.limit and key not in self._locked:
            self._locked[key] = now + self.lockout
            self._fails.pop(key, None)
            return True
        return False

    def succeed(self, key: str) -> None:
        self._fails.pop(key, None)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "") or default)
    except ValueError:
        return default


PER_ADDRESS_RATE = _env_int("AIMS_RATE_PER_IP_PER_MINUTE", 1200)
CREDENTIAL_FAILURES = _env_int("AIMS_AUTH_FAILURE_LIMIT", 10)
ANONYMOUS_FAILURES = _env_int("AIMS_ANON_FAILURE_LIMIT", 100)
FAILURE_WINDOW = 300
LOCKOUT_SECONDS = 900
TRUSTED_FOR = 900                    # an address that authenticated is trusted this long

# The in-process fallbacks. With Redis up these are not consulted.
PER_ADDRESS = RateLimiter(PER_ADDRESS_RATE)
AUTH_FAILURES = FailureTracker(limit=CREDENTIAL_FAILURES, window=FAILURE_WINDOW,
                               lockout=LOCKOUT_SECONDS)
ANON_FAILURES = FailureTracker(limit=ANONYMOUS_FAILURES, window=FAILURE_WINDOW,
                               lockout=LOCKOUT_SECONDS)
_TRUSTED: "OrderedDict[str, float]" = OrderedDict()
_LOCAL_LIMITERS: Dict[Tuple[str, int, float], RateLimiter] = {}


def _redis():
    """The shared Redis client, or None (then the in-process counters are used)."""
    try:
        import api_v2
        client = getattr(getattr(api_v2.ctx, "redis", None), "_client", None)
        return client
    except Exception:
        return None


async def _guard(coro, fallback):
    """A Redis call that can never fail the request: on error, the fallback."""
    try:
        return await coro
    except Exception as exc:
        logger.debug("Redis counter unavailable (%s); using in-process counters", exc)
        return fallback()


async def rate_allow(scope: str, key: str, rate: int, per: float = 60.0) -> Tuple[bool, float]:
    """One request against `rate` per `per` seconds, shared by every process."""
    def local():
        limiter = _LOCAL_LIMITERS.setdefault((scope, rate, per), RateLimiter(rate, per))
        return limiter.allow(f"{scope}:{key}")
    r = _redis()
    if r is None:
        return local()

    async def shared():
        window = int(time.time() // per)
        name = f"aims:rl:{scope}:{key}:{window}"
        count = await r.incr(name)
        if count == 1:
            await r.expire(name, int(per) + 5)
        if count <= rate:
            return True, 0.0
        return False, per - (time.time() % per)
    return await _guard(shared(), local)


def _tracker(kind: str) -> FailureTracker:
    return AUTH_FAILURES if kind == "cred" else ANON_FAILURES


async def locked_for(kind: str, key: str) -> float:
    """Seconds of lockout left for this credential or address."""
    r = _redis()
    if r is None:
        return _tracker(kind).locked(key)

    async def shared():
        ttl = await r.ttl(f"aims:lock:{kind}:{key}")
        return float(ttl) if ttl and ttl > 0 else 0.0
    return await _guard(shared(), lambda: _tracker(kind).locked(key))


async def record_failure(kind: str, key: str) -> bool:
    """Count one failure. True when this one started a lockout (raise the alert)."""
    limit = CREDENTIAL_FAILURES if kind == "cred" else ANONYMOUS_FAILURES
    r = _redis()
    if r is None:
        return _tracker(kind).fail(key)

    async def shared():
        name = f"aims:fail:{kind}:{key}"
        count = await r.incr(name)
        if count == 1:
            await r.expire(name, FAILURE_WINDOW)
        if count >= limit:
            started = await r.set(f"aims:lock:{kind}:{key}", "1", ex=LOCKOUT_SECONDS, nx=True)
            await r.delete(name)
            return bool(started)
        return False
    return await _guard(shared(), lambda: _tracker(kind).fail(key))


async def mark_trusted(ip: str) -> None:
    """This address just presented a credential that worked."""
    _TRUSTED[ip] = time.monotonic() + TRUSTED_FOR
    _TRUSTED.move_to_end(ip)
    while len(_TRUSTED) > 50_000:
        _TRUSTED.popitem(last=False)
    r = _redis()
    if r is not None:
        await _guard(r.set(f"aims:ok:{ip}", "1", ex=TRUSTED_FOR), lambda: None)


async def is_trusted(ip: str) -> bool:
    until = _TRUSTED.get(ip)
    if until is not None and until > time.monotonic():
        return True
    r = _redis()
    if r is None:
        return False
    return bool(await _guard(r.exists(f"aims:ok:{ip}"), lambda: 0))


# Every CMED client's allow-list, refreshed once a minute: those addresses are
# CMED's own servers and are never limited or locked by address.
_ALLOWLIST: Dict[str, Any] = {"at": 0.0, "nets": []}


async def on_cmed_allowlist(ip: str) -> bool:
    if time.monotonic() - _ALLOWLIST["at"] > 60:
        _ALLOWLIST["at"] = time.monotonic()
        try:
            import api_v2
            async with api_v2._repo()._pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT unnest(allowed_cidrs) AS c FROM cmed_clients "
                    "WHERE revoked_at IS NULL AND allowed_cidrs IS NOT NULL")
            _ALLOWLIST["nets"] = _networks(",".join(r["c"] for r in rows))
        except Exception:
            pass
    return _in(ip, _ALLOWLIST["nets"])


_CREDENTIAL_HEADERS = ("authorization", "x-cmed-key", "x-device-token", "x-admin-key",
                       "x-worker-key")


def credential_key(request: Request) -> Optional[str]:
    """A short fingerprint of the credential the request presents, if any."""
    for name in _CREDENTIAL_HEADERS:
        value = request.headers.get(name)
        if value and value.strip():
            return name + ":" + hashlib.sha256(value.strip().encode()).hexdigest()[:24]
    return None


# ------------------------------------------------------------------ replies

def too_many(wait: float) -> JSONResponse:
    retry = max(1, int(wait + 0.999))
    return problem(429, "RATE_LIMITED", f"Too many requests. Try again in {retry} s.",
                   headers={"Retry-After": str(retry)})


def problem(status: int, code: str, detail: Any, *, title: Optional[str] = None,
            headers: Optional[Dict[str, str]] = None, **extra) -> JSONResponse:
    """RFC 9457 problem details. `detail` stays as FastAPI gave it, for old clients."""
    from http import HTTPStatus
    try:
        phrase = HTTPStatus(status).phrase
    except ValueError:
        phrase = "Error"
    body = {"type": f"urn:aimscribe:problem:{code.lower()}", "title": title or phrase,
            "status": status, "code": code, "detail": detail,
            "request_id": request_id(), **extra}
    return JSONResponse(status_code=status, content=body, headers=headers,
                        media_type="application/problem+json")


def _code_for(status: int) -> str:
    return {400: "BAD_REQUEST", 401: "UNAUTHORIZED", 403: "FORBIDDEN", 404: "NOT_FOUND",
            405: "METHOD_NOT_ALLOWED", 409: "CONFLICT", 413: "TOO_LARGE",
            422: "INVALID_REQUEST", 429: "RATE_LIMITED", 502: "BAD_GATEWAY",
            503: "UNAVAILABLE"}.get(status, "ERROR")


_EXEMPT = ("/health", "/health/live", "/health/ready")


def install(app: FastAPI) -> None:
    @app.middleware("http")
    async def _guard_request(request: Request, call_next):
        inbound = request.headers.get("x-request-id", "")
        token = REQUEST_ID.set(inbound if _SAFE_ID.match(inbound) else uuid.uuid4().hex)
        try:
            try:
                response = await _checked(request, call_next)
            except Exception:
                # Caught here, inside the request, so the 500 still carries
                # this request's id - the one thing the caller can quote.
                response = _crashed(request)
            response.headers["X-Request-ID"] = request_id()
            return response
        finally:
            REQUEST_ID.reset(token)

    async def _checked(request: Request, call_next):
        path = request.url.path
        if path in _EXEMPT:
            return await call_next(request)
        ip = client_ip(request)
        cred = credential_key(request)
        cmed_server = await on_cmed_allowlist(ip)
        # An address on CMED's list, or one that just authenticated, is never
        # locked out for anonymous noise from somewhere else behind it.
        exempt = cmed_server or await is_trusted(ip)
        if cred is not None:
            left = await locked_for("cred", cred)
            if left > 0:
                return too_many(left)
        elif not exempt:
            left = await locked_for("ip", ip)
            if left > 0:
                return too_many(left)
        if not cmed_server:
            ok, wait = await rate_allow("ip", ip, PER_ADDRESS_RATE)
            if not ok:
                return too_many(wait)
        response = await call_next(request)
        if response.status_code == 401:
            if cred is not None:
                await note_credential_failure(cred, ip, path)
            elif not exempt:
                if await record_failure("ip", ip):
                    _alert_later("ip", ip, path)
        elif cred is not None and response.status_code < 400:
            await mark_trusted(ip)
        return response

    def _is_fhir(request: Request) -> bool:
        return request.url.path.startswith("/fhir/")

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException):
        if _is_fhir(request):
            import fhir
            return fhir.refused(exc.status_code, _code_for(exc.status_code), str(exc.detail))
        return problem(exc.status_code, _code_for(exc.status_code), exc.detail,
                       headers=getattr(exc, "headers", None))

    @app.exception_handler(RequestValidationError)
    async def _invalid(request: Request, exc: RequestValidationError):
        errors = exc.errors()
        if _is_fhir(request):
            import fhir
            return fhir.refused(422, "INVALID_REQUEST", "The request is not in the expected form.")
        return problem(422, "INVALID_REQUEST", _plain(errors))

    @app.exception_handler(Exception)
    async def _unexpected(request: Request, exc: Exception):
        return _crashed(request)


def _crashed(request: Request) -> JSONResponse:
    """
    The detail goes to the log, where the operator reads it; the caller learns
    only that it failed and the request id to quote.
    """
    logger.exception("Unhandled error on %s %s", request.method, request.url.path)
    if request.url.path.startswith("/fhir/"):
        import fhir
        return fhir.refused(500, "INTERNAL_ERROR", "The server could not complete this request.")
    return problem(500, "INTERNAL_ERROR", "The server could not complete this request.",
                   reference=request_id())


def _plain(value):
    """Validation errors made JSON-safe (they can carry exceptions and bytes)."""
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items() if k != "ctx"}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


async def note_credential_failure(cred: str, ip: str, path: str) -> None:
    if await record_failure("cred", cred):
        logger.warning("Locked out a credential from %s after repeated failures", ip)
        _alert_later("credential", ip, path)


def note_failure(request: Request) -> None:
    """Kept for callers outside a request's middleware; schedules the count."""
    import asyncio
    cred = credential_key(request) or ("ip:" + client_ip(request))
    try:
        asyncio.get_running_loop().create_task(
            note_credential_failure(cred, client_ip(request), request.url.path))
    except RuntimeError:
        pass


def _alert_later(kind: str, ip: str, path: str) -> None:
    """Raise the alert without making the refused caller wait for the database."""
    import asyncio

    async def _raise():
        try:
            import api_v2
            await api_v2._repo().raise_alert(
                alert_type="auth_failures", severity="warning",
                detail={"locked": kind, "address": ip, "path": path,
                        "lockout_seconds": LOCKOUT_SECONDS})
        except Exception as exc:                       # never fail the request
            logger.warning("Could not raise the auth_failures alert: %s", exc)

    try:
        task = asyncio.get_running_loop().create_task(_raise())
        _PENDING.add(task)
        task.add_done_callback(_PENDING.discard)
    except RuntimeError:
        pass


_PENDING: set = set()

__all__ = ["client_ip", "parse_ip", "ip_allowed", "RateLimiter", "FailureTracker", "install",
           "note_failure", "note_credential_failure", "too_many", "problem", "request_id",
           "rate_allow", "record_failure", "locked_for", "mark_trusted", "is_trusted",
           "credential_key", "PER_ADDRESS", "AUTH_FAILURES", "ANON_FAILURES", "REQUEST_ID"]
