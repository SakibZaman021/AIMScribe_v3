"""
The device log (migration 042): what every PC was told, and why.

A PC that cannot join, or is refused later, is told the reason in the
server's reply - and until now only the PC kept it. Every attempt a PC makes
to join (with a token, or by itself, whether it worked or not), and every
refusal (HTTP 400 and up) the server sends a PC afterwards, is kept here when
the reply leaves: when, which PC and from where, what it was doing, and the
exact code and message it was sent. The same refusal to the same PC within
ten minutes adds to one line (repeats, last_at), so a refused heartbeat every
30 seconds is one line, not 2,880 a day.

    GET /api/v2/admin/device-log        admins only: newest first, filtered
    GET /api/v2/admin/device-log.csv    the same, as a spreadsheet

Never a request body, a token or a patient value: for a malformed request,
only the names of the fields that were wrong. Each line also goes to the
API's log as "DEVICE ...", so ./server-logs.sh api today DEVICE finds it.
Writing it never fails or slows a reply. Nothing is deleted.
"""
from __future__ import annotations

import asyncio
import contextvars
import csv
import io
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Set

from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.responses import Response

import api_v2
import security

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v2/admin", tags=["device-log"])

# What a PC calls. The archive worker's and the dashboard's routes are not here.
DEVICE_PREFIXES = ("/api/v2/device/", "/api/v2/session/", "/api/v2/segment/",
                   "/api/v2/heartbeat", "/api/v2/grant/", "/api/v2/trusted-origins",
                   "/api/v2/doctors")
JOIN_PATHS = ("/api/v2/device/register", "/api/v2/device/enroll")
STEPS = {
    "/api/v2/device/register": "join by itself (no token)",
    "/api/v2/device/enroll": "join with an enrolment token",
    "/api/v2/grant/mint": "permission to record (grant)",
    "/api/v2/session/open": "start a recording",
    "/api/v2/segment/authorize": "upload a piece (ask)",
    "/api/v2/segment/commit": "upload a piece (confirm)",
    "/api/v2/session/close": "close a recording",
    "/api/v2/session/refuse": "patient did not consent",
    "/api/v2/session/pause": "pause", "/api/v2/session/resume": "resume",
    "/api/v2/session/marker": "marker", "/api/v2/session/reassign": "reassign",
    "/api/v2/heartbeat": "heartbeat", "/api/v2/device/health": "health report",
    "/api/v2/trusted-origins": "trusted EHR addresses", "/api/v2/doctors": "doctor list",
}
REPEAT_WINDOW = timedelta(minutes=10)


@dataclass
class _Note:
    """Filled in while the request runs (which PC, what it sent); read as the reply leaves."""
    device_id: Optional[str] = None
    machine_name: Optional[str] = None
    app_version: Optional[str] = None
    hospital_id: Optional[str] = None
    cmed_hospital_id: Optional[str] = None


_NOTE: contextvars.ContextVar[Optional[_Note]] = contextvars.ContextVar("device_log_note",
                                                                       default=None)


def note(**fields: Any) -> None:
    """Called where the PC is known: require_device, and the two join routes."""
    current = _NOTE.get()
    if current is None:
        return
    for key, value in fields.items():
        if value not in (None, "") and hasattr(current, key):
            setattr(current, key, str(value)[:128])


def step_of(path: str) -> str:
    if path in STEPS:
        return STEPS[path]
    if path.startswith("/api/v2/session/") and path.endswith("/receipts"):
        return "receipts"
    if path.startswith("/api/v2/session/") and path.endswith("/confirmation"):
        return "CMED confirmation"
    return path[len("/api/v2/"):][:60]


def outcome(status: int, body: bytes) -> Dict[str, Optional[str]]:
    """The code and message the PC was sent - never the values it sent us."""
    try:
        data = json.loads(body.decode("utf-8")) if body else {}
    except (UnicodeError, ValueError):
        return {"code": None, "message": None}
    if not isinstance(data, dict):
        return {"code": None, "message": None}
    code = data.get("code") or data.get("error")
    message = data.get("message") or data.get("error_description")
    detail = data.get("detail")
    if message is None and isinstance(detail, str):
        message = detail
    elif message is None and isinstance(detail, list):
        # FastAPI's 422: which fields, and what was wrong - not their values.
        parts = []
        for item in detail[:8]:
            if isinstance(item, dict):
                where = ".".join(str(x) for x in item.get("loc", []) if x != "body")
                parts.append(f"{where}: {item.get('msg', '')}".strip(": "))
        message = "the request was not valid - " + "; ".join(parts)
        code = code or "INVALID_REQUEST"
    if status < 400:                                 # a join that worked
        approval = data.get("approval") or ("approved" if data.get("device_token") else None)
        code = code or ("JOINED" if approval in (None, "approved") else "JOINED_PENDING")
        message = message or (f"joined clinic {data.get('hospital_id')}"
                              + (f", {approval}" if approval else "")
                              + (f" - approved after CMED confirms "
                                 f"{data.get('approved_after_confirmations')} recordings"
                                 if approval == "pending" else ""))
    if code is None and status >= 400:
        code = f"HTTP_{status}"
    return {"code": str(code)[:64] if code else None,
            "message": str(message)[:1000] if message else None}


_PENDING: Set[asyncio.Task] = set()


async def _write(row: Dict[str, Any]) -> None:
    """One line, or one more repeat of the same refusal to the same PC within ten minutes."""
    try:
        pool = api_v2._repo()._pool
        async with pool.acquire() as conn:
            same = await conn.fetchval("""
                UPDATE device_events SET repeats = repeats + 1, last_at = now(),
                       message = COALESCE($6, message)
                 WHERE id = (SELECT id FROM device_events
                              WHERE path = $1 AND status = $2
                                AND code IS NOT DISTINCT FROM $3
                                AND COALESCE(device_id, address) = COALESCE($4, $5)
                                AND last_at > now() - $7::interval
                                AND path <> ALL($8::text[])
                              ORDER BY id DESC LIMIT 1)
             RETURNING id
            """, row["path"], row["status"], row["code"], row["device_id"], row["address"],
                row["message"], REPEAT_WINDOW, list(JOIN_PATHS))
            if same is None:
                await conn.execute("""
                    INSERT INTO device_events (step, method, path, status, code, message,
                                               device_id, machine_name, app_version, address,
                                               hospital_id, cmed_hospital_id, request_id)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
                """, row["step"], row["method"], row["path"], row["status"], row["code"],
                    row["message"], row["device_id"], row["machine_name"], row["app_version"],
                    row["address"], row["hospital_id"], row["cmed_hospital_id"],
                    row["request_id"])
    except Exception as exc:                        # the log never fails a reply
        logger.debug("Device log not written: %s", exc)


def _record(request: Request, holder: _Note, status: int, body: bytes) -> None:
    path = request.url.path
    row = {"step": step_of(path), "method": request.method[:8], "path": path[:200],
           "status": status, "address": security.client_ip(request)[:64],
           "request_id": (security.request_id() or "")[:64] or None,
           "device_id": holder.device_id, "machine_name": holder.machine_name,
           "app_version": holder.app_version, "hospital_id": holder.hospital_id,
           "cmed_hospital_id": holder.cmed_hospital_id, **outcome(status, body)}
    line = (f"DEVICE {row['step']} | {row['machine_name'] or '-'} "
            f"{(row['device_id'] or '')[:8]} from {row['address']}"
            + (f" | CMED code {row['cmed_hospital_id']}" if row["cmed_hospital_id"] else "")
            + f" | {status} {row['code'] or ''}: {row['message'] or ''}")
    (logger.warning if status >= 400 else logger.info)(line[:600])
    task = asyncio.get_running_loop().create_task(_write(row))
    _PENDING.add(task)
    task.add_done_callback(_PENDING.discard)


def install(app: FastAPI) -> None:
    @app.middleware("http")
    async def _log_device_request(request: Request, call_next):
        path = request.url.path
        if not path.startswith(DEVICE_PREFIXES):
            return await call_next(request)
        holder = _Note()
        token = _NOTE.set(holder)
        try:
            try:
                response = await call_next(request)
            except Exception:
                _record(request, holder, 500, b"")
                raise
            if response.status_code < 400 and path not in JOIN_PATHS:
                return response                      # an ordinary success: not kept
            body = b"".join([chunk async for chunk in response.body_iterator])
            _record(request, holder, response.status_code, body)
            return Response(content=body, status_code=response.status_code,
                            headers=dict(response.headers), media_type=response.media_type)
        finally:
            _NOTE.reset(token)


# ============================================================ admins: read it

def _iso(value: Any) -> Any:
    return value.isoformat() if hasattr(value, "isoformat") else value


async def _rows(days: int, failed_only: bool, q: str, limit: int) -> List[Dict[str, Any]]:
    days = max(1, min(int(days or 7), 3650))
    limit = max(1, min(int(limit or 500), 5000))
    term = f"%{q.strip()}%" if q and q.strip() else None
    pool = api_v2._repo()._pool
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT * FROM device_events
             WHERE last_at > now() - make_interval(days => $1)
               AND (NOT $2 OR status >= 400)
               AND ($3::text IS NULL OR concat_ws(' ', machine_name, device_id, address, code,
                        message, cmed_hospital_id, hospital_id, step, app_version) ILIKE $3)
             ORDER BY last_at DESC LIMIT $4
        """, days, failed_only, term, limit)
    return [{k: _iso(v) for k, v in dict(r).items()} for r in rows]


@router.get("/device-log")
async def device_log(days: int = 7, failed_only: bool = False, q: str = "", limit: int = 500,
                     _: None = Depends(api_v2.require_admin)):
    """Every join attempt, and every refusal sent to a PC: newest first (admins only)."""
    return {"rows": await _rows(days, failed_only, q, limit)}


CSV_COLUMNS = ["first_at", "last_at", "repeats", "step", "status", "code", "message",
               "machine_name", "device_id", "address", "app_version", "hospital_id",
               "cmed_hospital_id", "method", "path", "request_id"]


@router.get("/device-log.csv")
async def device_log_csv(days: int = 30, failed_only: bool = False, q: str = "",
                         _: None = Depends(api_v2.require_admin)):
    rows = await _rows(days, failed_only, q, 5000)
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=CSV_COLUMNS, extrasaction="ignore")
    writer.writeheader()
    for r in rows:
        writer.writerow({k: r.get(k, "") for k in CSV_COLUMNS})
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    return Response(content=buf.getvalue().encode("utf-8-sig"),
                    media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition":
                             f'attachment; filename="aimscribe_device_log_{stamp}.csv"'})


__all__ = ["router", "install", "note", "outcome", "step_of"]
