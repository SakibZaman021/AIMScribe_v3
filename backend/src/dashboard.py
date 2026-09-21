"""
The operational dashboard (SRS 3.3 §8.9, `SRS-DSH-01`-`09`).

One question runs through all of it: *is anything wrong today, and where?*
Every number here opens into the rows behind it (`SRS-DSH-07`), because a
count nobody can look inside is a count nobody can act on.

**It reads the recordings database only** (`SRS-DSH-06`). It holds no
credential for `aims_clinical` and never shows a patient's name or anything
clinical - the patient's number, which the clinic uses to find the person,
is as far as it goes.

The page itself is one HTML file (`dashboard.html`) that calls these
endpoints. No build step: a dashboard that cannot be opened because a
toolchain broke is worse than a plain one.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse

import api_v2
from api_v2 import require_admin
from integrity import safe_identifier

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v2/dashboard", tags=["dashboard"])

# Three missed heartbeats and a recorder is called silent (SRS-DSH-04).
HEARTBEAT_SECONDS = 60
SILENT_AFTER = timedelta(seconds=3 * HEARTBEAT_SECONDS)


def _pool():
    repo = api_v2._repo()
    return repo._pool


def clinic_today() -> date:
    """
    Today where the clinic is, not today at Greenwich.

    Recordings are filed under the hospital's own date (`SRS-SES-05`), so a
    dashboard defaulting to the UTC date shows an empty day for the six hours
    either side of midnight in Dhaka - which is exactly when someone checking
    on an evening clinic would look. The server's own zone is the clinic's;
    it is set in the compose file and on the UIU server.
    """
    name = os.getenv("TZ", "").strip()
    if name:
        try:
            return datetime.now(ZoneInfo(name)).date()
        except Exception:
            logger.warning("TZ=%s is not a zone this machine knows; "
                           "the dashboard is using UTC", name)
    return datetime.now(timezone.utc).date()


def _day(value: Optional[str], fallback: date) -> date:
    # Not a string when the endpoint is called directly rather than through
    # FastAPI - as the tests do, so that what is tested is the real function.
    if not isinstance(value, str) or not value:
        return fallback
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise HTTPException(status_code=400, detail="date must be YYYY-MM-DD")


def _clinic(value: Optional[str]) -> Optional[str]:
    if not isinstance(value, str) or not value:
        return None
    try:
        return safe_identifier(value, field="hospital_id")
    except ValueError:
        raise HTTPException(status_code=400, detail="hospital_id is not an identifier")


# ============================================================
# What today looks like
# ============================================================

@router.get("/summary")
async def summary(from_date: Optional[str] = Query(None, alias="from"),
                  to_date: Optional[str] = Query(None, alias="to"),
                  hospital_id: Optional[str] = None,
                  _: None = Depends(require_admin)):
    """
    Everything §8.9 asks for, for a span of days: volume, confirmation,
    integrity, recorder health, reconciliation and the state of the copies.
    """
    today = clinic_today()
    start = _day(from_date, today)
    end = _day(to_date, today)
    if end < start:
        start, end = end, start
    clinic = _clinic(hospital_id)

    async with _pool().acquire() as conn:
        return {
            "from": start.isoformat(), "to": end.isoformat(),
            "hospital_id": clinic,
            "volume": await _volume(conn, start, end, clinic),
            "confirmation": await _confirmation(conn, start, end, clinic),
            "integrity": await _integrity(conn, start, end, clinic),
            "recorders": await _recorders(conn, clinic),
            "reconciliation": await _reconciliation(conn, start, end, clinic),
            "copies": await _copies(conn),
        }


async def _volume(conn, start: date, end: date, clinic: Optional[str]) -> Dict[str, Any]:
    """`SRS-DSH-01`: recordings and recorded hours, by clinic and by doctor."""
    rows = await conn.fetch("""
        SELECT session_date, hospital_id, doctor_id,
               count(*) AS recordings,
               COALESCE(sum(total_duration_seconds), 0) / 3600.0 AS hours
          FROM sessions
         WHERE session_date BETWEEN $1 AND $2
           AND ($3::text IS NULL OR hospital_id = $3)
           AND confirmation <> 'refused'
         GROUP BY session_date, hospital_id, doctor_id
         ORDER BY session_date, hospital_id, doctor_id
    """, start, end, clinic)

    by_day: Dict[str, Dict[str, Any]] = {}
    by_clinic: Dict[str, Dict[str, Any]] = {}
    by_doctor: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        day = row["session_date"].isoformat() if row["session_date"] else "undated"
        for bucket, key, extra in (
            (by_day, day, {"date": day}),
            (by_clinic, row["hospital_id"], {"hospital_id": row["hospital_id"]}),
            (by_doctor, f"{row['hospital_id']}/{row['doctor_id']}",
             {"hospital_id": row["hospital_id"], "doctor_id": row["doctor_id"]}),
        ):
            entry = bucket.setdefault(key, {**extra, "recordings": 0, "hours": 0.0})
            entry["recordings"] += row["recordings"]
            entry["hours"] += float(row["hours"])

    def tidy(bucket) -> List[Dict[str, Any]]:
        return [{**e, "hours": round(e["hours"], 2)} for e in bucket.values()]

    return {
        "total_recordings": sum(r["recordings"] for r in rows),
        "total_hours": round(sum(float(r["hours"]) for r in rows), 2),
        "by_day": tidy(by_day),
        "by_clinic": tidy(by_clinic),
        "by_doctor": sorted(tidy(by_doctor),
                            key=lambda e: e["recordings"], reverse=True),
    }


async def _confirmation(conn, start: date, end: date,
                        clinic: Optional[str]) -> Dict[str, Any]:
    """`SRS-DSH-08`: confirmed, still confirming, unconfirmed - per day and clinic."""
    rows = await conn.fetch("""
        SELECT session_date, hospital_id, confirmation, count(*) AS n
          FROM sessions
         WHERE session_date BETWEEN $1 AND $2
           AND ($3::text IS NULL OR hospital_id = $3)
         GROUP BY session_date, hospital_id, confirmation
         ORDER BY session_date, hospital_id
    """, start, end, clinic)

    totals: Dict[str, int] = {}
    per_day: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        state = row["confirmation"]
        totals[state] = totals.get(state, 0) + row["n"]
        day = row["session_date"].isoformat() if row["session_date"] else "undated"
        key = f"{day}/{row['hospital_id']}"
        entry = per_day.setdefault(key, {"date": day, "hospital_id": row["hospital_id"]})
        entry[state] = row["n"]
    return {"totals": totals, "by_day": list(per_day.values())}


async def _integrity(conn, start: date, end: date,
                     clinic: Optional[str]) -> Dict[str, Any]:
    """
    `SRS-DSH-03`: chains that failed, recordings quarantined and why, and
    consultations the doctor stopped before the end.

    Shown against clinic and doctor on purpose: one room with a recurring
    problem is a pattern, not a series of incidents.
    """
    quarantined = await conn.fetch("""
        SELECT hospital_id, doctor_id, quarantine_reason, count(*) AS n
          FROM sessions
         WHERE session_date BETWEEN $1 AND $2
           AND ($3::text IS NULL OR hospital_id = $3)
           AND quarantine_reason IS NOT NULL
         GROUP BY hospital_id, doctor_id, quarantine_reason
         ORDER BY n DESC
    """, start, end, clinic)

    stopped = await conn.fetch("""
        SELECT hospital_id, doctor_id, close_reason, count(*) AS n
          FROM sessions
         WHERE session_date BETWEEN $1 AND $2
           AND ($3::text IS NULL OR hospital_id = $3)
           AND close_reason IS NOT NULL
           AND close_reason NOT IN ('prescription_built', '')
         GROUP BY hospital_id, doctor_id, close_reason
         ORDER BY n DESC
    """, start, end, clinic)

    alerts = await conn.fetch("""
        SELECT a.alert_type, a.severity, count(*) AS n
          FROM integrity_alerts a
         WHERE a.raised_at >= $1::date AND a.raised_at < ($2::date + 1)
         GROUP BY a.alert_type, a.severity
         ORDER BY n DESC
    """, start, end)

    return {
        "quarantined": [dict(r) for r in quarantined],
        "quarantined_total": sum(r["n"] for r in quarantined),
        "stopped_early": [dict(r) for r in stopped],
        "stopped_early_total": sum(r["n"] for r in stopped),
        "alerts": [dict(r) for r in alerts],
        "alerts_total": sum(r["n"] for r in alerts),
    }


async def _recorders(conn, clinic: Optional[str]) -> Dict[str, Any]:
    """
    `SRS-DSH-04`: one row per enrolled PC - when it was last heard from, and
    how much audio is still waiting on it.
    """
    rows = await conn.fetch("""
        SELECT device_id, hospital_id, machine_name, app_version,
               last_seen_at, spool_bytes, pending_segments, revoked_at
          FROM devices
         WHERE ($1::text IS NULL OR hospital_id = $1)
         ORDER BY hospital_id, machine_name
    """, clinic)

    now = datetime.now(timezone.utc)
    rooms, silent, holding = [], 0, 0
    for row in rows:
        seen = row["last_seen_at"]
        is_silent = (row["revoked_at"] is None and
                     (seen is None or now - seen > SILENT_AFTER))
        pending = row["pending_segments"] or 0
        rooms.append({
            "device_id": str(row["device_id"]),
            "hospital_id": row["hospital_id"],
            "machine_name": row["machine_name"],
            "app_version": row["app_version"],
            "last_seen_at": seen.isoformat() if seen else None,
            "minutes_since_seen": round((now - seen).total_seconds() / 60, 1)
                                  if seen else None,
            "pending_segments": pending,
            "spool_mb": round((row["spool_bytes"] or 0) / 1024 ** 2, 1),
            "revoked": row["revoked_at"] is not None,
            "silent": is_silent,
        })
        if is_silent:
            silent += 1
        if pending:
            holding += 1
    return {"rooms": rooms, "silent": silent, "holding_audio": holding,
            "enrolled": len([r for r in rows if r["revoked_at"] is None])}


async def _reconciliation(conn, start: date, end: date,
                          clinic: Optional[str]) -> Dict[str, Any]:
    """
    `SRS-DSH-05`: recordings with nothing from CMED, and messages from CMED
    with no recording.

    Both sides are counted here from the five fields on the recordings side
    (`confirmation_notices`), never from the clinical database.
    """
    without_record = await conn.fetchval("""
        SELECT count(*) FROM sessions s
         WHERE s.session_date BETWEEN $1 AND $2
           AND ($3::text IS NULL OR s.hospital_id = $3)
           AND s.confirmation IN ('pending', 'unconfirmed')
    """, start, end, clinic)

    without_recording = await conn.fetch("""
        SELECT n.hospital_id, n.visit_date, count(*) AS n
          FROM confirmation_notices n
         WHERE n.visit_date BETWEEN $1 AND $2
           AND ($3::text IS NULL OR n.hospital_id = $3)
           AND n.claimed_by_jti IS NULL
           AND n.received_at < now() - interval '15 minutes'
         GROUP BY n.hospital_id, n.visit_date
         ORDER BY n.visit_date DESC
    """, start, end, clinic)

    return {
        "recordings_without_a_record": int(without_record or 0),
        "records_without_a_recording": sum(r["n"] for r in without_recording),
        "records_without_a_recording_by_clinic": [dict(r) for r in without_recording],
    }


async def _copies(conn) -> Dict[str, Any]:
    """
    `SRS-DSH-09`: what is not yet safe in two places - recordings without a
    cloud copy, pieces still in the segment store after 48 hours, and when
    the last restore was actually tested.
    """
    uncopied = await conn.fetchrow("""
        SELECT count(*) AS n,
               EXTRACT(EPOCH FROM (now() - min(archived_at))) / 3600 AS oldest_hours
          FROM sessions
         WHERE archived_at IS NOT NULL AND copied_at IS NULL
           AND quarantine_reason IS NULL
    """)
    stale_pieces = await conn.fetchval("""
        SELECT count(*) FROM segments
         WHERE object_deleted_at IS NULL
           AND committed_at < now() - interval '48 hours'
    """)
    restore = await conn.fetchrow("""
        SELECT occurred_at, detail FROM audit_log
         WHERE event_type = 'operations.restore_tested'
         ORDER BY occurred_at DESC LIMIT 1
    """)
    # JSONB arrives as text unless a codec is registered; the dashboard is
    # read-only and shapes its own answer, so it parses here.
    detail = restore["detail"] if restore else None
    if isinstance(detail, str):
        try:
            detail = json.loads(detail)
        except ValueError:
            detail = {"raw": detail}

    return {
        "recordings_without_a_cloud_copy": int(uncopied["n"] or 0),
        "oldest_uncopied_hours": round(float(uncopied["oldest_hours"]), 1)
                                 if uncopied["oldest_hours"] is not None else None,
        "pieces_older_than_48h": int(stale_pieces or 0),
        "last_restore_test": {
            "at": restore["occurred_at"].isoformat() if restore else None,
            "detail": detail,
        },
    }


# ============================================================
# The rows behind every number (SRS-DSH-02, -07)
# ============================================================

@router.get("/recordings")
async def recordings(from_date: Optional[str] = Query(None, alias="from"),
                     to_date: Optional[str] = Query(None, alias="to"),
                     hospital_id: Optional[str] = None,
                     doctor_id: Optional[str] = None,
                     patient_id: Optional[str] = None,
                     state: Optional[str] = None,
                     search: Optional[str] = None,
                     limit: int = 100,
                     _: None = Depends(require_admin)):
    """
    Clinic, then doctor, then date, then patient - and a recording at any
    level (`SRS-DSH-02`). `state` opens one of the summary's counts:

        confirmed | pending | unconfirmed | refused | expired | legacy
        quarantined | stopped_early | unarchived | uncopied
    """
    today = clinic_today()
    start = _day(from_date, today - timedelta(days=7))
    end = _day(to_date, today)
    clinic = _clinic(hospital_id)
    doctor = _clinic(doctor_id) if doctor_id else None
    patient = _clinic(patient_id) if patient_id else None
    limit = max(1, min(limit if isinstance(limit, int) else 100, 500))

    where = ["s.session_date BETWEEN $1 AND $2"]
    args: List[Any] = [start, end]

    def add(clause: str, value: Any) -> None:
        args.append(value)
        where.append(clause.replace("$N", f"${len(args)}"))

    if clinic:
        add("s.hospital_id = $N", clinic)
    if doctor:
        add("s.doctor_id = $N", doctor)
    if patient:
        add("s.patient_id = $N", patient)
    if isinstance(search, str) and search:
        # Part of a file name, on the trigram index (SRS-DBA-24).
        add("s.file_stem ILIKE '%' || $N || '%'", search[:128])

    states = {
        "quarantined": "s.quarantine_reason IS NOT NULL",
        "stopped_early": "s.close_reason IS NOT NULL AND "
                         "s.close_reason NOT IN ('prescription_built', '')",
        "unarchived": "s.closed_at IS NOT NULL AND s.archived_at IS NULL",
        "uncopied": "s.archived_at IS NOT NULL AND s.copied_at IS NULL",
    }
    if state in states:
        where.append(states[state])
    elif state:
        add("s.confirmation = $N", state)

    args.append(limit)
    async with _pool().acquire() as conn:
        rows = await conn.fetch(f"""
            SELECT s.session_id, s.session_date, s.hospital_id, s.doctor_id,
                   s.patient_id, s.file_stem, s.local_start, s.local_end,
                   s.confirmation, s.status, s.total_duration_seconds,
                   s.segment_count, s.opened_at, s.closed_at, s.archived_at,
                   s.copied_at, s.archive_relpath, s.quarantine_reason, s.close_reason
              FROM sessions s
             WHERE {' AND '.join(where)}
             ORDER BY s.opened_at DESC NULLS LAST
             LIMIT ${len(args)}
        """, *args)

    return {"count": len(rows), "recordings": [_recording_row(r) for r in rows]}


def _recording_row(row) -> Dict[str, Any]:
    def moment(value):
        return value.isoformat() if hasattr(value, "isoformat") else value

    return {
        "session_id": row["session_id"],
        "date": moment(row["session_date"]),
        "hospital_id": row["hospital_id"],
        "doctor_id": row["doctor_id"],
        # CMED's own patient number. The dashboard shows no name and nothing
        # clinical (SRS-DSH-06); this is how a clinic finds the consultation.
        "patient_id": row["patient_id"],
        "file_stem": row["file_stem"],
        "local_start": moment(row["local_start"]),
        "local_end": moment(row["local_end"]),
        "confirmation": row["confirmation"],
        "status": row["status"],
        "minutes": round(float(row["total_duration_seconds"] or 0) / 60, 1),
        "segments": row["segment_count"],
        "archived": row["archived_at"] is not None,
        "copied": row["copied_at"] is not None,
        "archive_relpath": row["archive_relpath"],
        "quarantine_reason": row["quarantine_reason"],
        "close_reason": row["close_reason"],
    }


@router.get("/clinics")
async def clinics(_: None = Depends(require_admin)):
    """The clinics and doctors a dashboard filter can offer."""
    async with _pool().acquire() as conn:
        rows = await conn.fetch("""
            SELECT h.hospital_id, h.name, h.timezone,
                   (SELECT count(*) FROM devices d
                     WHERE d.hospital_id = h.hospital_id AND d.revoked_at IS NULL)
                   AS recorders
              FROM hospitals h ORDER BY h.hospital_id
        """)
        doctors = await conn.fetch("""
            SELECT DISTINCT hospital_id, doctor_id FROM sessions
             WHERE session_date > now()::date - 90 ORDER BY hospital_id, doctor_id
        """)
    return {"clinics": [dict(r) for r in rows], "doctors": [dict(r) for r in doctors]}


# ============================================================
# The restore drill, recorded where the dashboard can show it
# ============================================================

@router.post("/restore-test")
async def record_restore_test(body: Dict[str, Any], _: None = Depends(require_admin)):
    """
    Write down that a restore was tested, and what happened (`SRS-DSH-09`,
    `SRS-STO-06`). A drill nobody recorded is a drill nobody did - and this
    is the only place the dashboard can learn that it happened.
    """
    await api_v2._repo().audit(
        event_type="operations.restore_tested", actor_type="admin",
        actor_id=str(body.get("by", "operator"))[:64],
        detail={"result": str(body.get("result", ""))[:200],
                "what": str(body.get("what", ""))[:200],
                "notes": str(body.get("notes", ""))[:1000]})
    return {"status": "recorded"}


# ============================================================
# The page
# ============================================================

@router.get("", response_class=HTMLResponse, include_in_schema=False)
@router.get("/", response_class=HTMLResponse, include_in_schema=False)
async def page() -> HTMLResponse:
    """
    The dashboard itself: one file, no build step, no packages.

    It asks for the admin key in the browser and keeps it in memory for the
    session only - never in local storage, where every script on the machine
    could read it.
    """
    html = Path(__file__).with_name("dashboard.html")
    if not html.is_file():
        raise HTTPException(status_code=404, detail="dashboard.html is missing")
    return HTMLResponse(html.read_text(encoding="utf-8"))


__all__ = ["router"]
