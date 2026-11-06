"""
CMED's request log: what happened to every request CMED's servers sent.

CMED asked to see this without asking AIMS LAB each time. Every request to
the token API, API 2, API 3, the status API and the FHIR form is written here
when its reply leaves: the time, the address, which API, the HTTP status, the
reply's code and reason, the visit_id and the request id. Never a body, a
token or a clinical value.

    GET /api/v4/clinical/log    the caller's own rows, newest first

A row belongs to the client that sent it: the token's client for API 2 / 3 /
status, the client an assertion or refresh token names for the token API. A
request whose client cannot be told (no token, an unknown token, a JWT that
cannot be read) is shown to every client that has called from that address in
the last 30 days, so CMED sees its own "invalid token" refusals too.
"""
from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import random
from dataclasses import dataclass
from typing import Any, Dict, Optional, Set

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import Response

import api_v2
import clinical
import cmed_auth
import integrity
import security

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v4/clinical", tags=["clinical-v4"])

KEEP_DAYS = 30
LOG_PATH = "/api/v4/clinical/log"
_PREFIXES = (cmed_auth.TOKEN_PATH, "/api/v4/clinical/", "/api/v2/clinical/", "/fhir/r4/")
_QUIET = (LOG_PATH, "/fhir/r4/metadata", "/fhir/r4/.well-known/")


@dataclass
class _Note:
    """Filled in by the endpoint while it runs; read when the reply leaves."""
    client_id: Optional[str] = None
    api: Optional[str] = None


_NOTE: contextvars.ContextVar[Optional[_Note]] = contextvars.ContextVar("cmed_log_note",
                                                                       default=None)


def note(*, client_id: Optional[str] = None, api: Optional[str] = None) -> None:
    """Called by the token API and authentication: who is calling, and what."""
    current = _NOTE.get()
    if current is None:
        return
    if client_id:
        current.client_id = client_id[:64]
    if api:
        current.api = api[:40]


def _api_of(method: str, path: str) -> str:
    if path == cmed_auth.TOKEN_PATH:
        return "API 0 · token"
    if path.endswith("/patient-information"):
        return "API 2 · patient information"
    if path.endswith("/prescription"):
        return "API 3 · prescription"
    if path.endswith("/status"):
        return "Status"
    if "$process-message" in path:
        return "FHIR message"
    if "/Encounter" in path or "/visits" in path:
        return "Visits"
    return f"{method} {path}"[:40]


def _outcome(body: bytes) -> Dict[str, Any]:
    """code, reason and visit_id from a JSON reply; nothing else is kept."""
    try:
        data = json.loads(body.decode("utf-8")) if body else {}
    except (UnicodeError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    code = data.get("code") or data.get("error")
    reason = data.get("error_description") or data.get("message") or data.get("detail")
    if data.get("resourceType") == "OperationOutcome":
        issue = (data.get("issue") or [{}])[0]
        code = (((issue.get("details") or {}).get("coding") or [{}])[0].get("code"))
        reason = (issue.get("details") or {}).get("text")
    fields = data.get("fields")
    if fields and isinstance(fields, list):
        reason = f"{reason or ''} (fields: {', '.join(str(f) for f in fields)})".strip()
    warnings = data.get("warnings")
    if isinstance(warnings, list) and warnings and not reason:
        reason = f"{len(warnings)} warning(s)"
    elif isinstance(warnings, list) and warnings:
        reason = f"{reason} {len(warnings)} warning(s): " + "; ".join(
            f"{w.get('field')}: {w.get('note')}" for w in warnings[:5] if isinstance(w, dict))
    visit = data.get("visit_id")
    return {"code": str(code)[:64] if code else None,
            "reason": (reason if isinstance(reason, str) else
                       json.dumps(reason, ensure_ascii=False) if reason else None),
            "visit_id": str(visit)[:64] if visit else None}


_PENDING: Set[asyncio.Task] = set()


async def _write(row: Dict[str, Any]) -> None:
    try:
        pool = api_v2._repo()._pool
        async with pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO cmed_request_log (client_id, ip, method, path, api, status,
                                              code, reason, visit_id, request_id)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
            """, row["client_id"], row["ip"], row["method"], row["path"], row["api"],
                row["status"], row["code"], (row["reason"] or None) and row["reason"][:500],
                row["visit_id"], row["request_id"])
            if random.random() < 0.01:
                await conn.execute(f"""
                    DELETE FROM cmed_request_log WHERE id IN (
                        SELECT id FROM cmed_request_log
                         WHERE at < now() - interval '{KEEP_DAYS} days' LIMIT 1000)
                """)
    except Exception as exc:                        # the log never fails a request
        logger.debug("CMED request log not written: %s", exc)


def install(app: FastAPI) -> None:
    @app.middleware("http")
    async def _log_cmed_request(request: Request, call_next):
        path = request.url.path
        if not path.startswith(_PREFIXES) or path.startswith(_QUIET):
            return await call_next(request)
        holder = _Note()
        token = _NOTE.set(holder)
        try:
            try:
                response = await call_next(request)
            except Exception:
                _schedule(request, holder, 500, b"")
                raise
            body = b"".join([chunk async for chunk in response.body_iterator])
            _schedule(request, holder, response.status_code, body)
            return Response(content=body, status_code=response.status_code,
                            headers=dict(response.headers), media_type=response.media_type)
        finally:
            _NOTE.reset(token)


def _schedule(request: Request, holder: _Note, status: int, body: bytes) -> None:
    path = request.url.path
    row = {"client_id": holder.client_id, "ip": security.client_ip(request)[:64],
           "method": request.method[:8], "path": path[:200],
           "api": holder.api or _api_of(request.method, path), "status": status,
           "request_id": security.request_id()[:64], **{"code": None, "reason": None,
                                                         "visit_id": None},
           **_outcome(body)}
    task = asyncio.get_running_loop().create_task(_write(row))
    _PENDING.add(task)
    task.add_done_callback(_PENDING.discard)


@router.get("/log")
async def request_log(request: Request, limit: int = 100, before: Optional[int] = None,
                      errors_only: bool = False, visit_id: Optional[str] = None):
    """
    The caller's own requests, newest first: limit 1 to 500 (100), `before`
    an id from the previous page, `errors_only` for replies of 400 and up,
    `visit_id` for one visit.
    """
    try:
        principal = await clinical._authenticate(request, cmed_auth.READ)
    except clinical._Refusal as exc:
        return clinical._reply(exc.status, exc.code, str(exc), **exc.extra)
    limit = max(1, min(int(limit), 500))
    async with api_v2._repo()._pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT id, at, api, method, path, status, code, reason, visit_id, request_id, ip,
                   client_id
              FROM cmed_request_log l
             WHERE (l.client_id = $1
                    OR (l.client_id IS NULL AND l.ip IN (
                        SELECT DISTINCT ip FROM cmed_request_log
                         WHERE client_id = $1 AND at > now() - interval '30 days')))
               AND ($2::bigint IS NULL OR l.id < $2)
               AND (NOT $3 OR l.status >= 400)
               AND ($4::text IS NULL OR l.visit_id = $4)
             ORDER BY l.id DESC LIMIT $5
        """, principal.client_id, before, errors_only, visit_id, limit)
    entries = [{"id": r["id"], "at": integrity.iso_utc(r["at"]), "api": r["api"],
                "status": r["status"], "code": r["code"], "reason": r["reason"],
                "visit_id": r["visit_id"], "request_id": r["request_id"], "ip": r["ip"],
                "client_known": r["client_id"] is not None,
                "path": f'{r["method"]} {r["path"]}'} for r in rows]
    return clinical._reply(200, "OK", client_id=principal.client_id, count=len(entries),
                           next_before=entries[-1]["id"] if len(entries) == limit else None,
                           entries=entries)


__all__ = ["router", "install", "note"]
