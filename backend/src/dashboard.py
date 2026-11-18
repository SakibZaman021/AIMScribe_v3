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

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, Response

import api_v2
import collectors
from api_v2 import require_admin, require_viewer
from integrity import safe_identifier

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v2/dashboard", tags=["dashboard"])

# Three missed heartbeats and a recorder is called silent (SRS-DSH-04).
HEARTBEAT_SECONDS = 60
SILENT_AFTER = timedelta(seconds=3 * HEARTBEAT_SECONDS)

# How a recording ends when nothing went wrong: the next patient opened, or
# two minutes after the prescription (older recorders: at the prescription).
# The lab's rule (15 Nov 2026): only two ends are ordinary - the next patient
# came in, or the consultation finished (the doctor's Stop in CMED or
# "Consultation finished" on the PC included). Everything else - the patient
# left, did not consent, an emergency, a technical problem, a crash, a
# shutdown - is "stopped early", the same list that alerts the collectors
# (collectors.ORDINARY_ENDS). Constants only, so safe to put in the SQL.
_ENDED_NORMALLY = "(" + ", ".join(f"'{r}'" for r in sorted(collectors.ORDINARY_ENDS)) + ")"


def _stopped_early(alias: str = "") -> str:
    """SQL: this recording ended in any way but the two ordinary ones."""
    return (f"(({alias}close_reason IS NOT NULL AND {alias}close_reason NOT IN "
            f"{_ENDED_NORMALLY}) OR EXISTS (SELECT 1 FROM session_refusals x "
            f"WHERE x.session_id = {alias}session_id))")


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
                  _: None = Depends(require_viewer),
                  scope: Any = Depends(collectors.hospital_scope)):
    """
    Everything §8.9 asks for, for a span of days: volume, confirmation,
    integrity, recorder health, reconciliation and the state of the copies.
    """
    today = clinic_today()
    start = _day(from_date, today)
    end = _day(to_date, today)
    if end < start:
        start, end = end, start
    # 036: a data collector sees their place's hospitals only.
    clinic = collectors.narrow(scope, _clinic(hospital_id))

    async with _pool().acquire() as conn:
        return {
            "from": start.isoformat(), "to": end.isoformat(),
            "hospital_id": _clinic(hospital_id),
            "scope": clinic if isinstance(scope, list) else None,
            "volume": await _volume(conn, start, end, clinic),
            "confirmation": await _confirmation(conn, start, end, clinic),
            "integrity": await _integrity(conn, start, end, clinic),
            "recorders": await _recorders(conn, clinic),
            "review": await _review(conn, clinic),
            "reconciliation": await _reconciliation(conn, start, end, clinic),
            "copies": await _copies(conn, clinic),
        }


async def _volume(conn, start: date, end: date, clinic: Optional[str]) -> Dict[str, Any]:
    """`SRS-DSH-01`: recordings and recorded hours, by clinic and by doctor."""
    rows = await conn.fetch("""
        SELECT session_date, hospital_id, doctor_id,
               count(*) AS recordings,
               COALESCE(sum(total_duration_seconds), 0) / 3600.0 AS hours
          FROM sessions
         WHERE session_date BETWEEN $1 AND $2
           AND ($3::text[] IS NULL OR hospital_id = ANY($3::text[]))
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
           AND ($3::text[] IS NULL OR hospital_id = ANY($3::text[]))
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
           AND ($3::text[] IS NULL OR hospital_id = ANY($3::text[]))
           AND quarantine_reason IS NOT NULL
         GROUP BY hospital_id, doctor_id, quarantine_reason
         ORDER BY n DESC
    """, start, end, clinic)

    stopped = await conn.fetch("""
        SELECT hospital_id, doctor_id,
               CASE WHEN EXISTS (SELECT 1 FROM session_refusals x
                                  WHERE x.session_id = sessions.session_id)
                    THEN 'patient_did_not_consent' ELSE close_reason END AS close_reason,
               count(*) AS n
          FROM sessions
         WHERE session_date BETWEEN $1 AND $2
           AND ($3::text[] IS NULL OR hospital_id = ANY($3::text[]))
           AND """ + _stopped_early("sessions.") + """
         GROUP BY 1, 2, 3
         ORDER BY n DESC
    """, start, end, clinic)

    alerts = await conn.fetch("""
        SELECT a.alert_type, a.severity, count(*) AS n
          FROM integrity_alerts a
          LEFT JOIN devices d ON d.device_id = a.device_id
         WHERE a.raised_at >= $1::date AND a.raised_at < ($2::date + 1)
           AND ($3::text[] IS NULL OR d.hospital_id = ANY($3::text[]))
         GROUP BY a.alert_type, a.severity
         ORDER BY n DESC
    """, start, end, clinic)

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
               last_seen_at, spool_bytes, pending_segments, revoked_at,
               held_sessions, held_count, disk_encrypted, clock_offset_seconds,
               approval, registered_via, microphone, microphone_is_clinic,
               blocked_at, revoked_reason, octet_length(tpm_pubkey) AS key_bytes
          FROM devices
         WHERE ($1::text[] IS NULL OR hospital_id = ANY($1::text[]))
         ORDER BY hospital_id, machine_name
    """, clinic)

    offline = await _offline_periods(conn, [row["device_id"] for row in rows])

    now = datetime.now(timezone.utc)
    rooms, silent, holding = [], 0, 0
    held_all: List[Dict[str, Any]] = []
    for row in rows:
        held = row["held_sessions"]
        if isinstance(held, str):
            held = json.loads(held)
        held = held or []
        held_all += [{**h, "machine_name": row["machine_name"],
                      "hospital_id": row["hospital_id"]} for h in held]
        offset = row["clock_offset_seconds"]
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
            # 025: a PC that registered itself waits for CMED, or for a person.
            "approval": row["approval"],
            "registered_via": row["registered_via"],
            "silent": is_silent,
            "offline_periods": offline.get(row["device_id"], []),
            # U3: what this PC is holding because the server said no, and why.
            "held_count": row["held_count"] or 0,
            "held": held,
            # X1: BitLocker on the drive holding its audio (None: not reported).
            "disk_encrypted": row["disk_encrypted"],
            # U1: server time minus the PC's time.
            "clock_offset_seconds": round(float(offset), 1) if offset is not None else None,
            # V2: below the build every PC should run.
            "outdated": api_v2.recorder_outdated(row["app_version"]),
            # 3.8: the microphone it records with, and whether it is the Jabra.
            "microphone": row["microphone"],
            "microphone_is_clinic": row["microphone_is_clinic"],
            # 033: blocked by the sweep on evidence; an admin may unblock it.
            "blocked": row["blocked_at"] is not None,
            "blocked_reason": row["revoked_reason"] if row["blocked_at"] else None,
            # 3.9.2: where its identity key lives - 65 bytes P-256 in its TPM
            # chip, 32 bytes Ed25519 in software (no usable chip, or before 3.6).
            "key": {65: "tpm", 32: "software"}.get(row["key_bytes"]),
        })
        if is_silent:
            silent += 1
        if pending:
            holding += 1
    return {"rooms": rooms, "silent": silent, "holding_audio": holding,
            "enrolled": len([r for r in rows if r["revoked_at"] is None]),
            "held": held_all,
            "minimum_version": api_v2.min_recorder_version(),
            "outdated": sum(1 for r in rooms if r["outdated"] and not r["revoked"]),
            "awaiting_approval": sum(1 for r in rooms
                                     if r["approval"] == "pending" and not r["revoked"]),
            "unencrypted": sum(1 for r in rooms
                               if r["disk_encrypted"] is False and not r["revoked"])}


REVIEW_ALERTS = ("labelling_review", "relabelled_after_archive")


async def _review(conn, clinic: Optional[str]) -> Dict[str, Any]:
    """
    S10: recordings a person should look at - labels the rules could not
    settle, and labels that changed after the files were written. Each with
    its parts, the rule behind each, and the file names to open. Unresolved
    alerts only, newest first.
    """
    rows = await conn.fetch("""
        SELECT a.id, a.alert_type, a.raised_at, a.session_id, a.detail,
               s.hospital_id, s.doctor_id, s.file_stem
          FROM integrity_alerts a
          LEFT JOIN sessions s ON s.session_id = a.session_id
         WHERE a.alert_type = ANY($1::text[]) AND a.resolved_at IS NULL
           AND ($2::text[] IS NULL OR s.hospital_id = ANY($2::text[]))
         ORDER BY a.raised_at DESC
         LIMIT 100
    """, list(REVIEW_ALERTS), clinic)
    sessions = list({r["session_id"] for r in rows if r["session_id"]})
    parts: Dict[str, List[Dict[str, Any]]] = {}
    if sessions:
        for p in await conn.fetch("""
            SELECT session_id, part_no, start_seconds, end_seconds, patient_id, rule,
                   needs_review, file_stem
              FROM consultation_parts WHERE session_id = ANY($1::text[])
             ORDER BY session_id, part_no
        """, sessions):
            parts.setdefault(p["session_id"], []).append({
                "part_no": p["part_no"],
                "from_seconds": round(float(p["start_seconds"]), 1),
                "to_seconds": round(float(p["end_seconds"]), 1)
                              if p["end_seconds"] is not None else None,
                "patient_id": p["patient_id"], "rule": p["rule"],
                "needs_review": p["needs_review"], "file_stem": p["file_stem"]})
    items = []
    for r in rows:
        detail = r["detail"]
        if isinstance(detail, str):
            detail = json.loads(detail)
        items.append({
            "alert_id": r["id"], "alert_type": r["alert_type"],
            "raised_at": r["raised_at"].isoformat() if r["raised_at"] else None,
            "session_id": r["session_id"], "hospital_id": r["hospital_id"],
            "doctor_id": r["doctor_id"], "file_stem": r["file_stem"],
            "parts": parts.get(r["session_id"], []), "detail": detail or {}})
    return {"items": items, "count": len(items)}


# Lines a minute apart belong to one outage; a longer gap starts the next.
OUTAGE_GAP = "3 minutes"


async def _offline_periods(conn, device_ids) -> Dict[Any, List[Dict[str, Any]]]:
    """
    When each recorder could not reach this server in the last week, and what
    it was doing meanwhile, from the health log it sent afterwards: "offline
    10:02-11:40, 3 consultations, microphone fine" rather than a blank.
    """
    if not device_ids:
        return {}
    rows = await conn.fetch(f"""
        WITH lines AS (
            SELECT device_id, occurred_at, data,
                   CASE WHEN occurred_at - lag(occurred_at) OVER w
                             <= interval '{OUTAGE_GAP}' THEN 0 ELSE 1 END AS starts
              FROM device_health_log
             WHERE kind = 'heartbeat' AND device_id = ANY($1::uuid[])
               AND occurred_at > now() - interval '7 days'
            WINDOW w AS (PARTITION BY device_id ORDER BY occurred_at)
        ), outages AS (
            SELECT *, sum(starts) OVER (PARTITION BY device_id ORDER BY occurred_at) AS n
              FROM lines
        )
        SELECT device_id, min(occurred_at) AS began, max(occurred_at) AS ended,
               count(DISTINCT data->>'session_id') AS consultations,
               bool_or(coalesce((data->>'silent')::boolean, false)) AS silent
          FROM outages
         GROUP BY device_id, n
         ORDER BY ended DESC
    """, list(device_ids))
    periods: Dict[Any, List[Dict[str, Any]]] = {}
    for row in rows:
        listed = periods.setdefault(row["device_id"], [])
        if len(listed) < 3:
            listed.append({
                "from": row["began"].isoformat(), "to": row["ended"].isoformat(),
                "minutes": round((row["ended"] - row["began"]).total_seconds() / 60) + 1,
                "consultations": row["consultations"],
                "microphone": "silent at times" if row["silent"] else "fine",
            })
    return periods


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
           AND ($3::text[] IS NULL OR s.hospital_id = ANY($3::text[]))
           AND s.confirmation IN ('pending', 'unconfirmed')
    """, start, end, clinic)

    without_recording = await conn.fetch("""
        SELECT n.hospital_id, n.visit_date, count(*) AS n
          FROM confirmation_notices n
         WHERE n.visit_date BETWEEN $1 AND $2
           AND ($3::text[] IS NULL OR n.hospital_id = ANY($3::text[]))
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


async def _copies(conn, clinic: Optional[List[str]] = None) -> Dict[str, Any]:
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
           AND quarantine_reason IS NULL AND confirmation = 'confirmed'
           AND ($1::text[] IS NULL OR hospital_id = ANY($1::text[]))
    """, clinic)
    stale_pieces = await conn.fetchval("""
        SELECT count(*) FROM segments g
         WHERE g.object_deleted_at IS NULL
           AND g.committed_at < now() - interval '48 hours'
           AND ($1::text[] IS NULL OR g.session_id IN (
                SELECT session_id FROM sessions WHERE hospital_id = ANY($1::text[])))
    """, clinic)
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
                     _: None = Depends(require_viewer),
                     scope: Any = Depends(collectors.hospital_scope)):
    """
    Clinic, then doctor, then date, then patient - and a recording at any
    level (`SRS-DSH-02`). `state` opens one of the summary's counts:

        confirmed | pending | unconfirmed | refused | expired | legacy
        quarantined | stopped_early | unarchived | uncopied | not_jabra
    """
    today = clinic_today()
    start = _day(from_date, today - timedelta(days=7))
    end = _day(to_date, today)
    clinics = collectors.narrow(scope, _clinic(hospital_id))
    doctor = _clinic(doctor_id) if doctor_id else None
    patient = _clinic(patient_id) if patient_id else None
    limit = max(1, min(limit if isinstance(limit, int) else 100, 500))

    where = ["s.session_date BETWEEN $1 AND $2"]
    args: List[Any] = [start, end]

    def add(clause: str, value: Any) -> None:
        args.append(value)
        where.append(clause.replace("$N", f"${len(args)}"))

    if clinics is not None:
        add("s.hospital_id = ANY($N::text[])", clinics)
    if doctor:
        add("s.doctor_id = $N", doctor)
    if patient:
        add("s.patient_id = $N", patient)
    if isinstance(search, str) and search:
        # Part of a file name, on the trigram index (SRS-DBA-24).
        add("s.file_stem ILIKE '%' || $N || '%'", search[:128])

    states = {
        "quarantined": "s.quarantine_reason IS NOT NULL",
        "stopped_early": _stopped_early("s."),
        "unarchived": "s.closed_at IS NOT NULL AND s.archived_at IS NULL",
        "uncopied": "s.archived_at IS NOT NULL AND s.copied_at IS NULL "
                    "AND s.confirmation = 'confirmed'",
        # 036: any part recorded on something other than the Jabra.
        "not_jabra": "EXISTS (SELECT 1 FROM unnest(s.microphones) m "
                     "WHERE m NOT ILIKE '%jabra%' "
                     "AND m NOT ILIKE 'echo cancelling speakerphone (j%')",
    }
    if state in states:
        where.append(states[state])
    elif state:
        add("s.confirmation = $N", state)

    args.append(limit)
    async with _pool().acquire() as conn:
        rows = await conn.fetch(f"""
            SELECT s.session_id, s.session_date, s.hospital_id, s.doctor_id,
                   s.patient_id, s.visit_id, s.file_stem, s.local_start, s.local_end,
                   s.confirmation, s.status, s.total_duration_seconds,
                   s.segment_count, s.opened_at, s.closed_at, s.archived_at,
                   s.copied_at, s.archive_relpath, s.quarantine_reason, s.close_reason,
                   s.microphones,
                   (SELECT count(*) FROM integrity_alerts a WHERE a.session_id = s.session_id
                      AND a.alert_type = 'microphone_switched') AS mic_switches,
                   -- 18 Nov: an open recording on a PC gone quiet is not "active".
                   (SELECT d.last_seen_at FROM devices d WHERE d.device_id = s.device_id)
                       AS pc_last_seen,
                   -- 18 Nov: the PC it was recorded on, by its Windows name.
                   (SELECT d.machine_name FROM devices d WHERE d.device_id = s.device_id)
                       AS pc_name,
                   s.device_id::text AS pc_id,
                   -- 18 Nov: the visit CMED's API 2 and API 3 are matched on.
                   g.cmed_hospital_id AS cmed_code,
                   COALESCE(s.visit_id, g.visit_id) AS cmed_visit_id,
                   g.start_time AS cmed_start_time
              FROM sessions s
              LEFT JOIN grant_authorisations g
                     ON g.jti = s.grant_jti AND g.patient_id = s.patient_id
             WHERE {' AND '.join(where)}
             ORDER BY s.opened_at DESC NULLS LAST
             LIMIT ${len(args)}
        """, *args)
        # The patient files each recording became, and the rule behind each, so
        # a wrong pick or a follow-up cut can be checked from this table.
        parts: Dict[str, List[Dict[str, Any]]] = {}
        ids = [r["session_id"] for r in rows]
        if ids:
            for p in await conn.fetch("""
                SELECT session_id, part_no, patient_id, file_stem, rule, needs_review,
                       start_seconds, end_seconds
                  FROM consultation_parts WHERE session_id = ANY($1::text[])
                 ORDER BY session_id, part_no
            """, ids):
                parts.setdefault(p["session_id"], []).append({
                    "part_no": p["part_no"], "patient_id": p["patient_id"],
                    "file_stem": p["file_stem"], "rule": p["rule"],
                    "needs_review": p["needs_review"],
                    "start_seconds": p["start_seconds"], "end_seconds": p["end_seconds"]})

    times = await _cmed_message_times(rows)
    return {"count": len(rows),
            "recordings": [{**_recording_row(r), "files": parts.get(r["session_id"], []),
                            **times.get(r["session_id"], {})}
                           for r in rows]}


# ============================================================
# One patient's history (18 Nov 2026)
# ============================================================

_PATIENT_VIEWS = ("overview", "demographics", "prescription", "medicines", "files")


async def _clinical_reader(request: Request) -> str:
    """
    Who may read a patient's clinical record here: an admin or a researcher
    signed in to the portal, or the admin key. Never a data collector, who
    sees no clinical value anywhere (036). Returns the name logged for it.
    """
    import portal
    person = await portal.person_for(request)
    if person is not None:
        if person.role not in ("admin", "researcher"):
            raise HTTPException(status_code=403,
                                detail="A patient's clinical record is for researchers and admins.")
        return person.email
    if request.headers.get("x-admin-key"):
        return "admin key"
    raise HTTPException(status_code=401, detail="Sign in to look up a patient.")


def _plain(value: Any) -> Any:
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if value is not None and type(value).__name__ == "Decimal":
        return float(value)
    return value


@router.get("/patient/{patient_ref}")
async def patient_history(patient_ref: str, request: Request,
                          visit_id: Optional[str] = None,
                          _: None = Depends(require_viewer),
                          scope: Any = Depends(collectors.hospital_scope)):
    """
    Everything held about one patient, newest visit first: who they are, and
    for each visit the demographics and vitals (API 2), the prescription
    (API 3), the medicines, and the recordings with their files. Found by
    CMED's patient ID, or by a visit ID of theirs. Read from the by_visit
    views (clinical/007, 008); every look-up is logged by name in
    clinical_access_log.
    """
    actor = await _clinical_reader(request)
    try:
        ref = safe_identifier(patient_ref.strip(), field="patient_id")
    except ValueError:
        raise HTTPException(status_code=400, detail="Give a patient ID or a visit ID.")
    # 18 Nov: with a visit ID as well, that one consultation only.
    only_visit = None
    if isinstance(visit_id, str) and visit_id.strip():
        only_visit = visit_id.strip()
        if not api_v2.VISIT_ID.match(only_visit):
            raise HTTPException(status_code=400,
                                detail="A visit ID has letters, digits and '-' only.")
    store = api_v2.ctx.clinical
    if store is None:
        raise HTTPException(status_code=503, detail="The clinical database is not connected.")
    async with store._pool.acquire() as conn:
        patient_id = await conn.fetchval(
            "SELECT patient_id FROM by_visit.overview WHERE patient_id = $1 LIMIT 1", ref)
        if patient_id is None:                   # perhaps a visit ID
            patient_id = await conn.fetchval(
                "SELECT patient_id FROM by_visit.overview WHERE visit_id = $1 LIMIT 1", ref)
        if patient_id is None:
            await store.log_access(actor=actor, action="dashboard.patient", patient_id=ref,
                                   detail={"found": False})
            return {"query": ref, "found": False, "patient": None, "visits": []}
        people = [dict(r) for r in await conn.fetch(
            "SELECT * FROM by_visit.patient_list WHERE patient_id = $1", patient_id)]
        rows = {name: [dict(r) for r in await conn.fetch(
                    f"SELECT * FROM by_visit.{name} WHERE patient_id = $1", patient_id)]
                for name in _PATIENT_VIEWS}

    visible = None if not isinstance(scope, list) else set(scope)
    visits: List[Dict[str, Any]] = []
    for v in rows["overview"]:
        if visible is not None and v.get("clinic") not in visible:
            continue
        if only_visit is not None and v.get("visit_id") != only_visit:
            continue
        key = v["visit_key"]
        demo = next((d for d in rows["demographics"] if d["visit_key"] == key), None)
        presc = next((d for d in rows["prescription"] if d["visit_key"] == key), None)
        visits.append({
            **{k: _plain(val) for k, val in v.items()},
            "demographics": {k: _plain(val) for k, val in demo.items()} if demo else None,
            "prescription": {k: _plain(val) for k, val in presc.items()} if presc else None,
            "medicines": [{k: _plain(val) for k, val in m.items()}
                          for m in rows["medicines"] if m["visit_key"] == key],
            "files": [{k: _plain(val) for k, val in f.items()}
                      for f in rows["files"] if f["visit_key"] == key],
        })

    # The recordings, for their download links: only copied, confirmed ones download.
    sessions = sorted({f["recording"] for v in visits for f in v["files"] if f.get("recording")}
                      | {s.strip() for v in visits for s in (v.get("recordings") or "").split(",")
                         if s.strip()})
    recorded: Dict[str, Dict[str, Any]] = {}
    if sessions:
        async with _pool().acquire() as conn:
            for r in await conn.fetch("""
                SELECT session_id, confirmation, copied_at IS NOT NULL AS copied,
                       total_duration_seconds, parts_count, local_start
                  FROM sessions WHERE session_id = ANY($1::text[])
            """, sessions):
                recorded[r["session_id"]] = {
                    "session_id": r["session_id"], "confirmation": r["confirmation"],
                    "copied": r["copied"], "parts": r["parts_count"],
                    "minutes": round(float(r["total_duration_seconds"] or 0) / 60, 1),
                    "local_start": _plain(r["local_start"])}
    for v in visits:
        ids = {f["recording"] for f in v["files"] if f.get("recording")} \
            | {s.strip() for s in (v.get("recordings") or "").split(",") if s.strip()}
        v["recordings_detail"] = [recorded[i] for i in sorted(ids) if i in recorded]

    await store.log_access(actor=actor, action="dashboard.patient", patient_id=patient_id,
                           detail={"visits": len(visits), "query": ref,
                                   **({"visit_id": only_visit} if only_visit else {})})
    return {"query": ref, "found": True, "visit_id": only_visit,
            "visit_found": None if only_visit is None else bool(visits),
            "patient": {k: _plain(val) for k, val in people[0].items()} if people else None,
            "visits": visits}


async def _cmed_message_times(rows) -> Dict[str, Dict[str, Any]]:
    """
    18 Nov 2026: when CMED's API 2 and API 3 for each listed recording reached
    us, so the newest recording's row answers "did API 2 come?" at a glance.
    `cmed_matched` is False for a recording with nothing to match on (no
    grant: a legacy recording). Never fails the list: unknown is None.
    """
    visits, out = [], {}
    for r in rows:
        keys = r.keys()
        code = r["cmed_code"] if "cmed_code" in keys else None
        matched = bool(code)
        out[r["session_id"]] = {"api2_at": None, "api3_at": None, "cmed_matched": matched}
        if matched:
            visits.append({"key": r["session_id"], "cmed_hospital_id": code,
                           "visit_id": r["cmed_visit_id"], "patient_id": r["patient_id"],
                           "start_time": r["cmed_start_time"]})
    store = api_v2.ctx.clinical
    if not visits or store is None or not hasattr(store, "message_times"):
        return out
    try:
        found = await store.message_times(visits)
    except Exception as exc:
        logger.warning("Could not read when API 2 / API 3 arrived: %s", exc)
        return out
    for key, when in found.items():
        out[key].update({name: value.isoformat() if hasattr(value, "isoformat") else value
                         for name, value in when.items()})
    return out


PC_QUIET = timedelta(minutes=10)


def _offline_since(row) -> Optional[str]:
    keys = row.keys()
    if "pc_last_seen" not in keys or row["closed_at"] is not None:
        return None
    seen = row["pc_last_seen"]
    if seen is None or datetime.now(timezone.utc) - seen < PC_QUIET:
        return None
    return seen.isoformat()


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
        # CMED's visit_id (interface 4), when API 1 carried one.
        "visit_id": row["visit_id"] if "visit_id" in row.keys() else None,
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
        # 18 Nov: the PC it was recorded on (its Windows name), to track a PC's problems.
        "pc": row["pc_name"] if "pc_name" in row.keys() else None,
        "pc_id": row["pc_id"] if "pc_id" in row.keys() else None,
        # Still open, and its PC not heard from for PC_QUIET: the audio is on
        # that PC, waiting, and arrives (and the recording closes) when the PC
        # is back online. Never closed from here - the PC still holds the audio.
        "on_pc_offline_since": _offline_since(row),
        # 036: which microphone(s) the recording used, and how often it switched.
        "microphone": collectors.microphone_label(
            row["microphones"] if "microphones" in row.keys() else None,
            int(row["mic_switches"] or 0) if "mic_switches" in row.keys() else 0),
    }


@router.get("/clinics")
async def clinics(_: None = Depends(require_viewer),
                  scope: Any = Depends(collectors.hospital_scope)):
    """The clinics and doctors a dashboard filter can offer (a collector: their place's)."""
    allowed = collectors.narrow(scope, None)
    async with _pool().acquire() as conn:
        rows = await conn.fetch("""
            SELECT h.hospital_id, h.name, h.timezone,
                   (SELECT count(*) FROM devices d
                     WHERE d.hospital_id = h.hospital_id AND d.revoked_at IS NULL)
                   AS recorders
              FROM hospitals h
             WHERE ($1::text[] IS NULL OR h.hospital_id = ANY($1::text[]))
             ORDER BY h.hospital_id
        """, allowed)
        doctors = await conn.fetch("""
            SELECT DISTINCT hospital_id, doctor_id FROM sessions
             WHERE session_date > now()::date - 90
               AND ($1::text[] IS NULL OR hospital_id = ANY($1::text[]))
             ORDER BY hospital_id, doctor_id
        """, allowed)
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
    # The same policy the gateway sets, so the page is as strict when it is
    # opened on the server itself, without the gateway in front.
    return HTMLResponse(html.read_text(encoding="utf-8"),
                        headers={"Content-Security-Policy": PAGE_POLICY})


# The page's own policy: its script, its style and its own API, nothing else.
# No inline script or style is allowed, which is why both are separate files.
PAGE_POLICY = ("default-src 'none'; script-src 'self'; style-src 'self'; "
               "connect-src 'self'; img-src 'self' data:; base-uri 'none'; "
               "form-action 'none'; frame-ancestors 'none'")

_STATIC = {"dashboard.css": "text/css; charset=utf-8",
           "dashboard.js": "text/javascript; charset=utf-8"}


@router.get("/static/{name}", include_in_schema=False)
async def static(name: str) -> Response:
    """The page's style and script. Two named files; nothing else is served."""
    media_type = _STATIC.get(name)
    path = Path(__file__).with_name(name)
    if media_type is None or not path.is_file():
        raise HTTPException(status_code=404, detail="not found")
    return Response(path.read_text(encoding="utf-8"), media_type=media_type,
                    headers={"Cache-Control": "no-cache"})


__all__ = ["router"]
