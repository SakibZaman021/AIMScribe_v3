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

    # 20 Nov: the two filters that showed no number.
    more = await conn.fetchrow("""
        SELECT count(*) FILTER (WHERE closed_at IS NOT NULL AND archived_at IS NULL)
                   AS unarchived,
               count(*) FILTER (WHERE EXISTS (SELECT 1 FROM unnest(microphones) m
                                               WHERE m NOT ILIKE '%jabra%'
                                                 AND m NOT ILIKE 'echo cancelling speakerphone (j%'))
                   AS not_jabra
          FROM sessions
         WHERE session_date BETWEEN $1 AND $2
           AND ($3::text[] IS NULL OR hospital_id = ANY($3::text[]))
    """, start, end, clinic)

    return {
        "not_jabra_total": int(more["not_jabra"] or 0) if more else 0,
        "unarchived_total": int(more["unarchived"] or 0) if more else 0,
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

    args.append(sorted(api_v2.INTERNAL_PAUSES))
    holds = f"${len(args)}"
    args.append(limit)
    async with _pool().acquire() as conn:
        rows = await conn.fetch(f"""
            SELECT s.session_id, s.session_date, s.hospital_id, s.doctor_id,
                   s.patient_id,
                   -- 20 Nov: the grant's visit_id while the recording is still
                   -- going (recordings opened before it was saved at the start).
                   COALESCE(s.visit_id, CASE WHEN g.patient_id = s.patient_id
                                             THEN g.visit_id END) AS visit_id,
                   s.file_stem, s.local_start, s.local_end,
                   s.confirmation, s.status, s.total_duration_seconds,
                   s.segment_count, s.opened_at, s.closed_at, s.archived_at,
                   s.copied_at, s.archive_relpath, s.quarantine_reason, s.close_reason,
                   s.microphones, s.object_prefix,
                   -- 20 Nov (recorder 3.9.6+): pieces the PC says it still holds,
                   -- less any that have arrived since.
                   (SELECT array_agg(n ORDER BY n) FROM unnest(s.pc_waiting_numbers) n
                     WHERE NOT EXISTS (SELECT 1 FROM segments q WHERE
                                       q.session_id = s.session_id AND q.seq_no = n))
                       AS pc_still_waiting, s.pc_reported_at,
                   (SELECT count(*) FROM integrity_alerts a WHERE a.session_id = s.session_id
                      AND a.alert_type = 'microphone_switched') AS mic_switches,
                   -- 18 Nov: paused now, and how much of it was paused.
                   s.paused_since, s.pause_reason, s.paused_seconds,
                   (SELECT count(*) FROM chain_entries c WHERE c.session_id = s.session_id
                      AND c.entry_type = 'pause'
                      AND COALESCE(c.payload->>'reason', '') <> ALL({holds}::text[]))
                       AS pauses,
                   -- 20 Nov: each pause, when and how long (no reasons on the dashboard).
                   (SELECT json_agg(json_build_object('entry_type', c.entry_type,
                                                      'payload', c.payload,
                                                      'occurred_at', c.occurred_at)
                                    ORDER BY c.entry_no)
                      FROM chain_entries c WHERE c.session_id = s.session_id
                       AND c.entry_type IN ('pause', 'resume')) AS pause_entries,
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
                   g.start_time AS cmed_start_time,
                   -- 21 Nov: a waiting recording whose visit another PC also
                   -- asked for. Never confirmed by itself (the two-PC rule);
                   -- an admin decides, seeing both PCs and their times.
                   CASE WHEN s.confirmation IN ('pending', 'unconfirmed') THEN (
                       SELECT json_agg(json_build_object(
                                  'pc', od.machine_name, 'session_id', os.session_id,
                                  'opened_at', COALESCE(os.opened_at, o.issued_at),
                                  'closed_at', os.closed_at,
                                  'confirmation', os.confirmation)
                              ORDER BY COALESCE(os.opened_at, o.issued_at))
                         FROM grant_authorisations o
                         LEFT JOIN sessions os ON os.grant_jti = o.jti
                         LEFT JOIN devices od ON od.device_id = o.device_id
                        WHERE o.device_id <> g.device_id
                          AND o.patient_id = g.patient_id AND o.doctor_id = g.doctor_id
                          AND o.cmed_hospital_id = g.cmed_hospital_id
                          AND o.start_time = g.start_time AND o.visit_date = g.visit_date)
                   END AS other_pcs
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
        "opened_at": moment(row["opened_at"]) if "opened_at" in row.keys() else None,
        "closed_at": moment(row["closed_at"]) if "closed_at" in row.keys() else None,
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
        # 18 Nov: paused now (open only), and the pauses the doctor made.
        "paused_since": moment(row["paused_since"])
        if "paused_since" in row.keys() and row["closed_at"] is None else None,
        "pause_reason": row["pause_reason"] if "pause_reason" in row.keys() else None,
        "paused_minutes": round(float(row["paused_seconds"] or 0) / 60, 1)
        if "paused_seconds" in row.keys() else 0,
        "pauses": int(row["pauses"] or 0) if "pauses" in row.keys() else 0,
        "pause_list": _pause_list(row),
        # 20 Nov (recorder 3.9.6+): piece numbers still on its PC, and when said.
        "pc_waiting": list(row["pc_still_waiting"] or [])
        if "pc_still_waiting" in row.keys() else [],
        "pc_reported_at": moment(row["pc_reported_at"]) if "pc_reported_at" in row.keys() else None,
        # 21 Nov: other PCs that asked for this waiting recording's visit.
        "other_pcs": _other_pcs(row),
        # 20 Nov: its folder in the segment store, for the pieces log.
        "pieces_folder": (f"audio/{row['object_prefix'] or row['session_id']}/"
                          if "object_prefix" in row.keys() else None),
    }


def _other_pcs(row) -> List[Dict[str, Any]]:
    """
    21 Nov 2026: the other PCs that asked for this waiting recording's visit -
    each with its recording (if it opened one), its times, and whether the two
    recordings ran at the same time. Two at once is two PCs recording one
    consultation; one after the other is more likely the doctor moving PC
    (visit-e2984763). The admin who confirms by hand sees which.
    """
    if "other_pcs" not in row.keys() or not row["other_pcs"]:
        return []
    found = row["other_pcs"]
    if isinstance(found, str):
        found = json.loads(found)
    now = datetime.now(timezone.utc)
    start, end = row["opened_at"], row["closed_at"] or now

    def when(value):
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00")) if value else None
        except ValueError:
            return None

    out = []
    for other in found:
        o_start, o_end = when(other.get("opened_at")), when(other.get("closed_at"))
        overlapped = None
        if start is not None and o_start is not None and other.get("session_id"):
            overlapped = o_start < end and (o_end or now) > start
        out.append({"pc": other.get("pc"), "session_id": other.get("session_id"),
                    "opened_at": other.get("opened_at"), "closed_at": other.get("closed_at"),
                    "confirmation": other.get("confirmation"), "overlapped": overlapped})
    return out


def _pause_list(row) -> List[Dict[str, Any]]:
    """20 Nov: each pause the doctor made - when it began and its minutes."""
    if "pause_entries" not in row.keys() or not row["pause_entries"]:
        return []
    import workbook
    entries = row["pause_entries"]
    if isinstance(entries, str):
        entries = json.loads(entries)
    out = []
    for p in workbook.pair_pauses(entries, row["closed_at"]):
        out.append({"at": p["at"].isoformat() if p["at"] else None,
                    "minutes": round((p["seconds"] or 0) / 60, 1),
                    "open": bool(p.get("open"))})
    return out


# ============================================================
# The clinical tables as CSV (19 Nov 2026)
# ============================================================

# What a researcher may download, and where each is read from.
# 19 Nov: "visits" is every visit with its demographics and prescription side by
# side - one row per visit, whether or not CMED's API 2 or API 3 came - the
# file the Patients page downloads for one patient.
_VISITS_SQL = """(
    SELECT o.clinic, o.cmed_clinic_code, o.patient_id, o.doctor_id, o.visit_id,
           o.visit_date, o.start_time,
           o.api2_received_at AS demographics_received_at,
           d.sex, d.date_of_birth, d.age, d.blood_group, d.blood_pressure, d.pulse,
           d.temperature, d.weight, d.height, d.spo2,
           o.api3_received_at AS prescription_received_at,
           p.chief_complaints, p.examination, p.diagnoses, p.medicines, p.investigations,
           p.advice, p.follow_up, p.issued_at,
           o.recordings, o.archive_folder, o.visit_key
      FROM by_visit.overview o
      LEFT JOIN LATERAL (SELECT * FROM by_visit.demographics x
                          WHERE x.visit_key = o.visit_key LIMIT 1) d ON true
      LEFT JOIN LATERAL (SELECT * FROM by_visit.prescription x
                          WHERE x.visit_key = o.visit_key LIMIT 1) p ON true
) visits"""
_CLINICAL_CSV = {"demographics": "by_visit.demographics",   # one row per visit with API 2
                 "prescriptions": "by_visit.prescription",  # one row per visit with API 3
                 "medicines": "by_visit.medicines",         # one row per medicine prescribed
                 "visits": _VISITS_SQL}                     # one row per visit, both together
_CSV_MOST = 200_000          # more than this, and the dates must be narrowed


def _cell(value: Any) -> Any:
    if value is None:
        return ""
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


@router.get("/clinical.csv")
async def clinical_csv(request: Request, table: str = "demographics",
                       from_date: Optional[str] = Query(None, alias="from"),
                       to_date: Optional[str] = Query(None, alias="to"),
                       hospital_id: Optional[str] = None,
                       patient_id: Optional[str] = None,
                       visit_id: Optional[str] = None,
                       _: None = Depends(require_viewer),
                       scope: Any = Depends(collectors.hospital_scope)):
    """
    19 Nov 2026: the demographics (API 2), prescriptions (API 3), medicines,
    or all of a visit together ("visits"), as one CSV - read from the same
    by_visit tables as the Patients page. Researchers and admins only, and
    every download is logged by name in clinical_access_log. No dates: every
    visit. The clinic is ours (the dashboard's) or CMED's code for it. A
    patient ID, a visit ID, or both narrow it to one patient or consultation.
    """
    actor = await _clinical_reader(request)
    view = _CLINICAL_CSV.get(table if isinstance(table, str) else "")
    if view is None:
        raise HTTPException(status_code=400,
                            detail="table is demographics, prescriptions, medicines or visits.")
    where: List[str] = []
    args: List[Any] = []
    start = _day(from_date, None) if from_date else None
    end = _day(to_date, None) if to_date else None
    if start is not None:
        args.append(start)
        where.append(f"visit_date >= ${len(args)}")
    if end is not None:
        args.append(end)
        where.append(f"visit_date <= ${len(args)}")
    clinic = _clinic(hospital_id)
    if clinic is not None:
        args.append(clinic)
        where.append(f"(clinic = ${len(args)} OR cmed_clinic_code = ${len(args)})")
    if isinstance(scope, list):
        args.append(scope)
        where.append(f"clinic = ANY(${len(args)}::text[])")
    patient = None
    if isinstance(patient_id, str) and patient_id.strip():
        try:
            patient = safe_identifier(patient_id.strip(), field="patient_id")
        except ValueError:
            raise HTTPException(status_code=400, detail="patient_id is not an identifier")
        args.append(patient)
        where.append(f"patient_id = ${len(args)}")
    visit = None
    if isinstance(visit_id, str) and visit_id.strip():
        visit = visit_id.strip()
        if not api_v2.VISIT_ID.match(visit):
            raise HTTPException(status_code=400,
                                detail="A visit ID has letters, digits and '-' only.")
        args.append(visit)
        where.append(f"visit_id = ${len(args)}")
    store = api_v2.ctx.clinical
    if store is None:
        raise HTTPException(status_code=503, detail="The clinical database is not connected.")
    sql = (f"SELECT * FROM {view}"
           + (f" WHERE {' AND '.join(where)}" if where else "")
           + f" LIMIT {_CSV_MOST + 1}")
    async with store._pool.acquire() as conn:
        statement = await conn.prepare(sql)
        columns = [a.name for a in statement.get_attributes() if a.name != "visit_key"]
        rows = await statement.fetch(*args)
    if len(rows) > _CSV_MOST:
        raise HTTPException(status_code=413, detail=f"Over {_CSV_MOST:,} rows: choose fewer "
                                                    "days or one clinic.")
    await store.log_access(actor=actor, action="dashboard.csv", patient_id=patient,
                           detail={"table": table, "rows": len(rows),
                                   "from": start.isoformat() if start else None,
                                   "to": end.isoformat() if end else None,
                                   "hospital_id": clinic,
                                   **({"visit_id": visit} if visit else {})})
    out = [{k: _cell(r[k]) for k in columns} for r in rows]
    # e.g. demographics_all.csv, visits_P4347353.csv, visits_P4347353_VIS-T-31492.csv,
    # demographics_TEST_2026-11-01_2026-11-30.csv
    dates = [d.isoformat() for d in (start, end) if d]
    named = [x for x in (clinic, patient, visit) if x]
    name = "_".join([table, *named, *(dates or ([] if named else ["all"]))]) + ".csv"
    return Response(_csv(out, columns), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


# ============================================================
# The pieces log (20 Nov 2026)
# ============================================================
#
# Every piece of audio that ever reached the segment store has a row in
# `segments`, kept after the piece itself is deleted: its number, length,
# fingerprint, when it was recorded and when it arrived, and where it went.
# This turns those rows into the log the team asked for: one row per piece,
# or one per recording with what arrived, what is missing and where it is.

_PIECE_WHERE = """
    CASE WHEN p.state = 'quarantined' THEN 'rejected: did not match what the PC signed'
         WHEN p.object_deleted_at IS NOT NULL AND s.confirmation = 'refused'
              THEN 'erased: the patient refused'
         WHEN p.object_deleted_at IS NOT NULL AND s.confirmation = 'expired'
              THEN 'erased: CMED did not confirm it in time'
         WHEN p.object_deleted_at IS NOT NULL AND s.copied_at IS NOT NULL
              THEN 'in the archive; piece removed after the archive copy was checked'
         WHEN p.object_deleted_at IS NOT NULL OR p.state = 'purged'
              THEN 'removed from the segment store'
         WHEN p.state = 'archived'
              THEN 'joined into the archive WAV; piece still in the segment store'
         WHEN s.closed_at IS NULL THEN 'in the segment store; recording still sending'
         WHEN s.confirmation = 'confirmed'
              THEN 'in the segment store; confirmed, waiting to be joined'
         ELSE 'in the segment store; waiting for CMED' END"""

# The visit the recording belongs to: saved on it, or its grant's while that
# grant is still this recording's patient.
_PIECE_VISIT = ("COALESCE(s.visit_id, CASE WHEN g.patient_id = s.patient_id "
                "THEN g.visit_id END)")

_PIECES_SQL = f"""
    SELECT * FROM (
    SELECT s.hospital_id AS clinic, s.doctor_id AS doctor, s.session_date AS day,
           s.patient_id, {_PIECE_VISIT} AS visit_id,
           s.session_id AS recording, d.machine_name AS pc,
           p.seq_no AS piece,
           p.captured_start_at AS recorded_from,
           p.captured_end_at AS recorded_to,
           p.duration_seconds AS seconds, p.bytes,
           encode(p.sha256, 'hex') AS fingerprint_sha256,
           p.committed_at AS arrived_at,
           round(extract(epoch FROM p.committed_at - p.captured_end_at))::int
               AS arrived_seconds_after_recorded,
           {_PIECE_WHERE} AS where_now,
           p.object_deleted_at
               AS removed_from_store_at,
           p.object_key AS address_in_segment_store,
           COALESCE(h.timezone, 'Asia/Dhaka') AS _clock, s.opened_at AS _opened
      FROM segments p
      JOIN sessions s ON s.session_id = p.session_id
      LEFT JOIN hospitals h ON h.hospital_id = s.hospital_id
      LEFT JOIN devices d ON d.device_id = s.device_id
      LEFT JOIN grant_authorisations g ON g.jti = s.grant_jti
     WHERE {{where}}
    UNION ALL
    -- Recorder 3.9.6+: the pieces the PC says it still holds, not yet here.
    SELECT s.hospital_id, s.doctor_id, s.session_date, s.patient_id, {_PIECE_VISIT},
           s.session_id, d.machine_name, n.piece,
           NULL::timestamptz, NULL::timestamptz, NULL::numeric, NULL::bigint, NULL::text,
           NULL::timestamptz, NULL::int,
           CASE WHEN n.piece = ANY(COALESCE(s.pc_rejected_numbers, '{{{{}}}}'))
                THEN 'on the PC: refused by the server, kept there for review'
                ELSE 'on the PC: not sent yet' END,
           NULL::timestamptz, NULL::text,
           COALESCE(h.timezone, 'Asia/Dhaka'), s.opened_at
      FROM sessions s
      CROSS JOIN LATERAL unnest(COALESCE(s.pc_waiting_numbers, '{{{{}}}}')
                                || COALESCE(s.pc_rejected_numbers, '{{{{}}}}')) AS n(piece)
      LEFT JOIN hospitals h ON h.hospital_id = s.hospital_id
      LEFT JOIN devices d ON d.device_id = s.device_id
      LEFT JOIN grant_authorisations g ON g.jti = s.grant_jti
     WHERE s.confirmation NOT IN ('refused', 'expired')
       AND NOT EXISTS (SELECT 1 FROM segments q
                        WHERE q.session_id = s.session_id AND q.seq_no = n.piece)
       AND {{where}}
    ) pieces
     ORDER BY day, clinic, doctor, _opened, recording, piece
     LIMIT {{most}}"""

_PIECE_RECORDINGS_SQL = f"""
    SELECT s.hospital_id AS clinic, s.doctor_id AS doctor, s.session_date AS day,
           s.patient_id, {_PIECE_VISIT} AS visit_id,
           s.session_id AS recording, d.machine_name AS pc,
           s.opened_at AS started,
           s.closed_at AS pc_finished_sending_at,
           CASE WHEN s.closed_at IS NOT NULL THEN s.segment_count END AS pieces_in_recording,
           a.arrived AS pieces_arrived,
           a.last_piece,
           -- An erased recording's pieces are gone by design, not missing.
           CASE WHEN s.confirmation NOT IN ('refused', 'expired') THEN m.missing END
               AS missing_piece_numbers,
           -- Recorder 3.9.6+: what the PC itself says it still holds, less what
           -- has arrived since it said so. Empty for older recorders.
           s.pc_pieces_recorded AS pieces_recorded_on_pc,
           cardinality(w.still) AS pieces_waiting_on_pc,
           array_to_string(w.still, ' ') AS piece_numbers_waiting_on_pc,
           array_to_string(s.pc_rejected_numbers, ' ') AS piece_numbers_refused_kept_on_pc,
           s.pc_reported_at AS pc_reported_at,
           -- Every recorder: the PC's one count for all its recordings.
           CASE WHEN s.closed_at IS NULL OR cardinality(w.still) > 0
                THEN d.pending_segments END AS pieces_waiting_on_this_pc_all_recordings,
           d.last_seen_at AS pc_last_seen,
           a.last_arrived AS last_piece_arrived_at,
           a.sent_late AS pieces_arrived_over_2_min_late,
           a.longest_wait AS longest_wait_seconds,
           a.in_store AS pieces_in_segment_store,
           s.confirmation AS cmed,
           s.archived_at AS archived_at,
           s.copied_at AS archive_copy_at,
           CASE WHEN s.status = 'quarantined' OR s.quarantine_reason IS NOT NULL
                     THEN 'held for review: ' || COALESCE(s.quarantine_reason, '')
                WHEN s.confirmation IN ('refused', 'expired') THEN 'erased'
                WHEN cardinality(w.still) > 0
                     THEN 'the PC still has ' || cardinality(w.still) || ' piece(s) to send: '
                          || array_to_string(w.still, ' ')
                WHEN cardinality(COALESCE(s.pc_rejected_numbers, '{{{{}}}}')) > 0
                     THEN 'the server refused piece(s) '
                          || array_to_string(s.pc_rejected_numbers, ' ')
                          || '; the PC keeps them for review'
                WHEN s.closed_at IS NULL AND a.arrived = 0
                     THEN 'started; no piece has arrived yet'
                WHEN s.closed_at IS NULL
                     THEN 'recording, or the PC still has pieces to send'
                WHEN m.missing IS NOT NULL THEN 'finished, but pieces are missing'
                WHEN s.copied_at IS NOT NULL THEN 'archived, and its archive copy made'
                WHEN s.archived_at IS NOT NULL THEN 'archived; archive copy pending'
                WHEN s.confirmation = 'confirmed'
                     THEN 'every piece in; confirmed; waiting to be joined'
                ELSE 'every piece in; waiting for CMED' END AS status,
           'audio/' || COALESCE(s.object_prefix, s.session_id) || '/'
               AS folder_in_segment_store,
           COALESCE(h.timezone, 'Asia/Dhaka') AS _clock
      FROM sessions s
      LEFT JOIN hospitals h ON h.hospital_id = s.hospital_id
      LEFT JOIN devices d ON d.device_id = s.device_id
      LEFT JOIN grant_authorisations g ON g.jti = s.grant_jti
      CROSS JOIN LATERAL (
          SELECT count(*) AS arrived, max(p.seq_no) AS last_piece,
                 max(p.committed_at) AS last_arrived,
                 count(*) FILTER (WHERE p.committed_at - p.captured_end_at
                                        > interval '2 minutes') AS sent_late,
                 max(round(extract(epoch FROM p.committed_at - p.captured_end_at)))::int
                     AS longest_wait,
                 count(*) FILTER (WHERE p.object_deleted_at IS NULL
                                    AND p.state <> 'purged') AS in_store
            FROM segments p WHERE p.session_id = s.session_id) a
      CROSS JOIN LATERAL (
          SELECT array_agg(n ORDER BY n) FILTER (WHERE n IS NOT NULL) AS still
            FROM unnest(COALESCE(s.pc_waiting_numbers, '{{{{}}}}')) n
           WHERE NOT EXISTS (SELECT 1 FROM segments q
                              WHERE q.session_id = s.session_id AND q.seq_no = n)) w
      CROSS JOIN LATERAL (
          SELECT string_agg(n::text, ' ' ORDER BY n) AS missing
            FROM generate_series(1, GREATEST(COALESCE(a.last_piece, 0),
                                             COALESCE(s.pc_pieces_recorded, 0),
                                             CASE WHEN s.closed_at IS NOT NULL
                                                  THEN COALESCE(s.segment_count, 0)
                                                  ELSE 0 END)) n
           WHERE NOT EXISTS (SELECT 1 FROM segments q
                              WHERE q.session_id = s.session_id AND q.seq_no = n)) m
     WHERE s.protocol_version >= 2 AND {{where}}
     ORDER BY s.session_date, s.hospital_id, s.doctor_id, s.opened_at
     LIMIT {{most}}"""


def _clinic_clock(value: Any, zone: str) -> Any:
    """A moment as the clinic's wall clock reads it, to the second; else as _cell."""
    if isinstance(value, datetime) and value.tzinfo is not None:
        try:
            value = value.astimezone(ZoneInfo(zone))
        except Exception:
            value = value.astimezone(timezone.utc)
        return value.strftime("%Y-%m-%d %H:%M:%S")
    return _cell(value)


@router.get("/pieces.csv")
async def pieces_csv(request: Request, table: str = "pieces",
                     from_date: Optional[str] = Query(None, alias="from"),
                     to_date: Optional[str] = Query(None, alias="to"),
                     hospital_id: Optional[str] = None,
                     patient_id: Optional[str] = None,
                     visit_id: Optional[str] = None,
                     session_id: Optional[str] = None,
                     _: None = Depends(require_viewer),
                     scope: Any = Depends(collectors.hospital_scope)):
    """
    20 Nov 2026: the pieces log. table=pieces is one row per piece that
    reached the segment store - when it was recorded, when it arrived, its
    fingerprint and where it is now; table=recordings is one row per
    recording - pieces arrived, the numbers missing, whether the PC has
    finished sending and how many pieces it says it still holds, CMED, the
    archive. One recording (session_id), a patient, a visit, or the dates and
    clinic chosen (the last 7 days when nothing narrows it). Not clinical: no
    value from CMED's messages is in it. Researchers and admins (036).
    """
    await _no_collector_files(request, "the recordings")
    if table not in ("pieces", "recordings"):
        raise HTTPException(status_code=400, detail="table is pieces or recordings.")
    where: List[str] = []
    args: List[Any] = []

    def add(clause: str, value: Any) -> None:
        args.append(value)
        where.append(clause.replace("$N", f"${len(args)}"))

    recording = None
    if isinstance(session_id, str) and session_id.strip():
        try:
            recording = api_v2.safe_session_id(session_id.strip())
        except ValueError:
            raise HTTPException(status_code=400, detail="session_id is not a recording ID")
        add("s.session_id = $N", recording)
    patient = None
    if isinstance(patient_id, str) and patient_id.strip():
        try:
            patient = safe_identifier(patient_id.strip(), field="patient_id")
        except ValueError:
            raise HTTPException(status_code=400, detail="patient_id is not an identifier")
        add("s.patient_id = $N", patient)
    visit = None
    if isinstance(visit_id, str) and visit_id.strip():
        visit = visit_id.strip()
        if not api_v2.VISIT_ID.match(visit):
            raise HTTPException(status_code=400,
                                detail="A visit ID has letters, digits and '-' only.")
        add(f"{_PIECE_VISIT} = $N", visit)
    # One recording, patient or visit: every day of it unless dates are given.
    # Otherwise the last 7 days, as on the Recordings page.
    narrowed = bool(recording or patient or visit)
    today = clinic_today()
    start = _day(from_date, None if narrowed else today - timedelta(days=7))
    end = _day(to_date, None if narrowed else today)
    if start is not None and end is not None and start > end:
        raise HTTPException(status_code=400, detail="From is after To.")
    if start is not None:
        add("s.session_date >= $N", start)
    if end is not None:
        add("s.session_date <= $N", end)
    clinic = _clinic(hospital_id)
    clinics = collectors.narrow(scope, clinic)
    if clinics is not None:
        add("s.hospital_id = ANY($N::text[])", clinics)
    sql = (_PIECES_SQL if table == "pieces" else _PIECE_RECORDINGS_SQL).format(
        where=" AND ".join(where) or "true", most=_CSV_MOST + 1)
    async with _pool().acquire() as conn:
        statement = await conn.prepare(sql)
        columns = [a.name for a in statement.get_attributes()
                   if a.name not in ("_clock", "_opened")]
        rows = await statement.fetch(*args)
    if len(rows) > _CSV_MOST:
        raise HTTPException(status_code=413, detail=f"Over {_CSV_MOST:,} rows: choose fewer "
                                                    "days or one clinic.")
    out = [{k: _clinic_clock(r[k], r["_clock"]) for k in columns} for r in rows]
    # e.g. pieces_01M7SPZC8F929V47JCX96JWRS5.csv,
    # recordings_AALO_KARAIL_2026-11-13_2026-11-20.csv
    dates = [d.isoformat() for d in (start, end) if d]
    named = [x for x in (recording, clinic, patient, visit) if x]
    name = "_".join([table, *named, *dates]) + ".csv"
    return Response(_csv(out, columns), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


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


# ============================================================ the event log (18 Nov 2026)

EVENT_KINDS = ("recording", "pause", "consent", "microphone", "cmed", "alert")
_EVENT_CSV = ["time", "hospital", "pc", "doctor", "patient_id", "visit_id", "kind", "event",
              "detail", "session_id"]
_PROBLEM_ALERTS = {
    "capture_failed", "microphone_unavailable", "start_abandoned", "disk_full",
    "local_chain_invalid", "local_segment_unreadable", "silent_session",
    "microphone_switched", "unexpected_agent_exit"}


async def _event_rows(conn, start: date, end: date, clinics: Optional[List[str]],
                      doctor: Optional[str], kind: Optional[str],
                      limit_sessions: int = 1000) -> List[Dict[str, Any]]:
    """Everything that happened to each recording in these days, one row per event."""
    import watchers
    where = ["s.session_date BETWEEN $1 AND $2"]
    args: List[Any] = [start, end]
    if clinics is not None:
        args.append(clinics)
        where.append(f"s.hospital_id = ANY(${len(args)}::text[])")
    if doctor:
        args.append(doctor)
        where.append(f"s.doctor_id = ${len(args)}")
    args.append(limit_sessions)
    sessions = await conn.fetch(f"""
        SELECT s.session_id, s.hospital_id, s.doctor_id, s.patient_id, s.opened_at,
               s.closed_at, s.close_reason, s.confirmation, s.total_duration_seconds,
               s.paused_seconds, s.microphones, s.device_id,
               COALESCE(s.visit_id, g.visit_id) AS visit_id,
               (SELECT r.refused_at FROM session_refusals r
                 WHERE r.session_id = s.session_id LIMIT 1) AS refused_at,
               d.machine_name,
               g.cmed_hospital_id AS cmed_code,
               COALESCE(s.visit_id, g.visit_id) AS cmed_visit_id,
               g.start_time AS cmed_start_time
          FROM sessions s
          LEFT JOIN devices d ON d.device_id = s.device_id
          LEFT JOIN grant_authorisations g ON g.jti = s.grant_jti AND g.patient_id = s.patient_id
         WHERE {' AND '.join(where)}
         ORDER BY s.opened_at DESC NULLS LAST
         LIMIT ${len(args)}
    """, *args)
    by_id = {r["session_id"]: r for r in sessions}
    out: List[Dict[str, Any]] = []

    def add(at, r, what_kind: str, event: str, detail: str = "") -> None:
        if at is None or (kind and what_kind != kind):
            return
        out.append({"at": at, "hospital": r["hospital_id"], "pc": r["machine_name"] or "",
                    "doctor": r["doctor_id"] or "", "patient_id": r["patient_id"] or "",
                    "visit_id": r["visit_id"] or "", "kind": what_kind, "event": event,
                    "detail": detail, "session_id": r["session_id"]})

    ids = list(by_id)
    for r in sessions:
        mics = ", ".join(r["microphones"] or []) or "microphone not reported"
        add(r["opened_at"], r, "recording", "Recording started", mics)
        if r["refused_at"] is not None:
            add(r["refused_at"], r, "consent", "Patient did not consent",
                "the recording was deleted")
        elif r["closed_at"] is not None:
            ended = watchers._ended_words(r["close_reason"], False, True)
            usual = collectors.stop_kind(r["close_reason"]) is None
            minutes = round(float(r["total_duration_seconds"] or 0) / 60, 1)
            paused = round(float(r["paused_seconds"] or 0) / 60, 1)
            add(r["closed_at"], r, "recording",
                "Recording ended" if usual else "Recording stopped unusually",
                f"{ended}; {minutes} min recorded" + (f", {paused} min paused" if paused else ""))
    if ids:
        for c in await conn.fetch("""
            SELECT session_id, entry_type, payload, occurred_at FROM chain_entries
             WHERE session_id = ANY($1::text[]) AND entry_type IN ('pause', 'resume')
             ORDER BY session_id, entry_no
        """, ids):
            payload = c["payload"]
            if isinstance(payload, str):
                payload = json.loads(payload)
            payload = payload or {}
            r = by_id[c["session_id"]]
            if c["entry_type"] == "pause":
                reason = payload.get("reason") or ""
                if reason in api_v2.INTERNAL_PAUSES:
                    continue
                add(c["occurred_at"], r, "pause", "Paused by the doctor",
                    payload.get("reason_detail") or reason.replace("_", " "))
            else:
                seconds = float(payload.get("paused_seconds") or 0)
                if seconds and c["occurred_at"]:
                    add(c["occurred_at"], r, "pause", "Resumed",
                        f"after {int(seconds // 60)} min {int(seconds % 60):02d} s paused")
        for a in await conn.fetch("""
            SELECT session_id, alert_type, raised_at, detail FROM integrity_alerts
             WHERE session_id = ANY($1::text[]) AND alert_type = ANY($2::text[])
        """, ids, sorted(_PROBLEM_ALERTS)):
            detail = a["detail"]
            if isinstance(detail, str):
                try:
                    detail = json.loads(detail)
                except ValueError:
                    detail = {}
            detail = detail or {}
            text = str(detail.get("detail") or detail.get("microphone") or "")[:200]
            label = ("Microphone changed" if a["alert_type"] == "microphone_switched"
                     else collectors.PROBLEMS.get(a["alert_type"], human_code(a["alert_type"])))
            add(a["raised_at"], by_id[a["session_id"]], "microphone",
                label[:1].upper() + label[1:], text)
        times = await _cmed_message_times(sessions)
        for sid, t in times.items():
            r = by_id[sid]
            if not t.get("cmed_matched") or r["refused_at"] is not None:
                continue
            for name, label in (("api2_at", "CMED API 2 (demographics) arrived"),
                                ("api3_at", "CMED API 3 (prescription) arrived")):
                if t.get(name):
                    add(datetime.fromisoformat(t[name]), r, "cmed", label)
            missing = [w for n, w in (("api2_at", "API 2"), ("api3_at", "API 3"))
                       if not t.get(n)]
            if missing and r["closed_at"] is not None:
                add(r["closed_at"], r, "cmed", "Not received from CMED",
                    " and ".join(missing) + " for this recording")
        for n in await conn.fetch("""
            SELECT DISTINCT ON (event_key) event_key, event, subject, created_at, session_id,
                   (SELECT count(DISTINCT m.email) FROM collector_notifications m
                     WHERE m.event_key = n.event_key) AS people,
                   (SELECT min(m.seen_at) FROM collector_notifications m
                     WHERE m.event_key = n.event_key AND m.audience = 'collector') AS seen_at
              FROM collector_notifications n
             WHERE session_id = ANY($1::text[])
             ORDER BY event_key, created_at
        """, ids):
            r = by_id.get(n["session_id"])
            if r is None:
                continue
            seen = (f"; Seen {n['seen_at'].astimezone(_zone()).strftime('%H:%M')}"
                    if n["seen_at"] else "")
            add(n["created_at"], r, "alert", "Alert sent",
                f"{n['subject']} - to {n['people']} people{seen}")
    out.sort(key=lambda e: e["at"], reverse=True)
    return out


def human_code(code: str) -> str:
    return str(code or "").replace("_", " ")


def _zone() -> ZoneInfo:
    return collectors.zone()


def _event_out(e: Dict[str, Any]) -> Dict[str, Any]:
    return {**e, "at": e["at"].isoformat(),
            "time": e["at"].astimezone(_zone()).strftime("%Y-%m-%d %H:%M:%S")}


def _csv(rows: List[Dict[str, Any]], columns: List[str]) -> bytes:
    import csv
    import io
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({k: row.get(k, "") for k in columns})
    return buf.getvalue().encode("utf-8-sig")


async def _no_collector_files(request: Request, what: str = "the event log") -> None:
    """A CSV is a download: researchers and admins, as for audio (036)."""
    import portal
    person = await portal.person_for(request)
    if person is not None and person.role == "collector":
        raise HTTPException(status_code=403, detail=f"A data collector can view {what} "
                                                    "but not download it.")


@router.get("/events")
async def events(request: Request,
                 from_date: Optional[str] = Query(None, alias="from"),
                 to_date: Optional[str] = Query(None, alias="to"),
                 hospital_id: Optional[str] = None, doctor_id: Optional[str] = None,
                 kind: Optional[str] = None, format: Optional[str] = None,
                 _: None = Depends(require_viewer),
                 scope: Any = Depends(collectors.hospital_scope)):
    """
    18 Nov 2026: the event log - every recording's start and end, each pause
    and resume with its reason, refusals, microphone changes, CMED's API 2 and
    API 3 (and which never came), and each alert sent, newest first. With
    format=csv, the same as a file.
    """
    today = clinic_today()
    start = _day(from_date, today)
    end = _day(to_date, today)
    clinics = collectors.narrow(scope, _clinic(hospital_id))
    doctor = _clinic(doctor_id) if doctor_id else None
    if kind and kind not in EVENT_KINDS:
        raise HTTPException(status_code=422, detail=f"kind is one of {', '.join(EVENT_KINDS)}")
    if format == "csv":
        await _no_collector_files(request)
    async with _pool().acquire() as conn:
        rows = await _event_rows(conn, start, end, clinics, doctor, kind)
    if format == "csv":
        name = f"aimscribe_events_{start.isoformat()}_{end.isoformat()}.csv"
        return Response(_csv([_event_out(e) for e in rows], _EVENT_CSV),
                        media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="{name}"'})
    shown = rows[:3000]
    return {"count": len(rows), "events": [_event_out(e) for e in shown]}


_MISSING_CSV = ["cmed_hospital_id", "hospital", "doctor_id", "patient_id", "visit_id", "date",
                "cmed_start_time", "recording_start", "recording_end", "api2_demographics",
                "api3_prescription"]


@router.get("/cmed-missing.csv")
async def cmed_missing(request: Request,
                       from_date: Optional[str] = Query(None, alias="from"),
                       to_date: Optional[str] = Query(None, alias="to"),
                       hospital_id: Optional[str] = None,
                       _: None = Depends(require_viewer),
                       scope: Any = Depends(collectors.hospital_scope)):
    """
    18 Nov 2026: the recordings CMED's API 2 or API 3 never reached us for -
    with the five fields CMED matches a visit on, no names - to send to CMED.
    Only recordings that ended over 10 minutes ago and were not refused.
    """
    await _no_collector_files(request)
    today = clinic_today()
    start = _day(from_date, today)
    end = _day(to_date, today)
    clinics = collectors.narrow(scope, _clinic(hospital_id))
    where = ["s.session_date BETWEEN $1 AND $2", "s.closed_at < now() - interval '10 minutes'",
             "s.confirmation IS DISTINCT FROM 'refused'", "g.cmed_hospital_id IS NOT NULL"]
    args: List[Any] = [start, end]
    if clinics is not None:
        args.append(clinics)
        where.append(f"s.hospital_id = ANY(${len(args)}::text[])")
    async with _pool().acquire() as conn:
        rows = await conn.fetch(f"""
            SELECT s.session_id, s.hospital_id, s.doctor_id, s.patient_id, s.session_date,
                   s.opened_at, s.closed_at,
                   g.cmed_hospital_id AS cmed_code,
                   COALESCE(s.visit_id, g.visit_id) AS cmed_visit_id,
                   g.start_time AS cmed_start_time
              FROM sessions s
              JOIN grant_authorisations g ON g.jti = s.grant_jti AND g.patient_id = s.patient_id
             WHERE {' AND '.join(where)}
             ORDER BY s.opened_at
             LIMIT 5000
        """, *args)
    times = await _cmed_message_times(rows)
    tz = _zone()

    def when(value) -> str:
        if not value:
            return "missing"
        moment = datetime.fromisoformat(value) if isinstance(value, str) else value
        return moment.astimezone(tz).strftime("%Y-%m-%d %H:%M")

    out = []
    for r in rows:
        t = times.get(r["session_id"], {})
        if t.get("api2_at") and t.get("api3_at"):
            continue
        out.append({"cmed_hospital_id": r["cmed_code"], "hospital": r["hospital_id"],
                    "doctor_id": r["doctor_id"], "patient_id": r["patient_id"],
                    "visit_id": r["cmed_visit_id"] or "",
                    "date": r["session_date"].isoformat() if r["session_date"] else "",
                    "cmed_start_time": r["cmed_start_time"] or "",
                    "recording_start": r["opened_at"].astimezone(tz).strftime("%H:%M:%S")
                    if r["opened_at"] else "",
                    "recording_end": r["closed_at"].astimezone(tz).strftime("%H:%M:%S")
                    if r["closed_at"] else "",
                    "api2_demographics": when(t.get("api2_at")),
                    "api3_prescription": when(t.get("api3_at"))})
    name = f"cmed_missing_{start.isoformat()}_{end.isoformat()}.csv"
    return Response(_csv(out, _MISSING_CSV), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


__all__ = ["router"]
