"""
Researchers who look after hospitals (migration 038).

Researchers see and download every hospital, as before. An admin also gives
each researcher the hospitals they look after, and for those:

    copies    every alert the data collectors get (the Jabra unplugged, a
              patient recorded without it, no sound, a recording problem, a PC
              gone quiet, a recording nobody stopped, nobody on shift), saying
              who was told - without the Seen button: the collector acts
    handled   "handled by <collector> after N min" when the collector presses
              Seen, or "not handled in 15 min" when nobody does
    report    one message per hospital every evening (AIMS_REPORT_AT, 20:30):
              recordings, the Jabra, what will be kept, the collectors, the PCs,
              and what to do tomorrow

A researcher who wants less chooses 'problems': only what went wrong and
stayed wrong, and the report. Everything goes through the collectors'
Telegram bot and mailer (collectors.py); nothing here deletes anything.
"""
from __future__ import annotations

import os
import secrets
from collections import Counter
from datetime import date, datetime, time as dtime, timedelta, timezone
from statistics import median
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

import api_v2
import collectors
import portal

router = APIRouter(prefix="/api/v2/dashboard/watch", tags=["researchers"])
admin_router = APIRouter(prefix="/api/v2/admin", tags=["researchers"])

# What a researcher on 'problems' still gets.
PROBLEMS_ONLY = {"escalation", "pc_silent", "recording_problem", "no_shift_started",
                 "recording_too_long", "daily_report"}
MODES = ("everything", "problems")
UNHANDLED_AFTER = timedelta(minutes=15)
SHORT_SECONDS = 60
LATE_MINUTES = 15


def report_time() -> dtime:
    text = os.getenv("AIMS_REPORT_AT", "").strip() or "20:30"
    try:
        return dtime.fromisoformat(text)
    except ValueError:
        return dtime(20, 30)


def _pool():
    return collectors._pool()


# ============================================================ who looks after what

async def watched_hospitals(email: str, conn=None) -> List[str]:
    query = ("SELECT hospital_id FROM researcher_hospitals WHERE email = $1 "
             "AND removed_at IS NULL ORDER BY hospital_id")
    if conn is not None:
        return [r["hospital_id"] for r in await conn.fetch(query, email)]
    async with _pool().acquire() as own:
        return [r["hospital_id"] for r in await own.fetch(query, email)]


async def watchers_of(conn, hospital_id: Optional[str]) -> List[Tuple[str, str]]:
    """(email, mode) of everyone still looking after this hospital."""
    if not hospital_id:
        return []
    rows = await conn.fetch("""
        SELECT r.email, p.alert_mode FROM researcher_hospitals r
          JOIN portal_people p ON p.email = r.email AND p.removed_at IS NULL
         WHERE r.hospital_id = $1 AND r.removed_at IS NULL
           AND p.role IN ('researcher', 'admin')
         ORDER BY r.email
    """, hospital_id)
    return [(r["email"], r["alert_mode"] or "everything") for r in rows]


async def copy_to_watchers(conn, *, hospital_id: Optional[str], event: str, subject: str,
                           body: str, event_key: str, urgent: bool = False, device_id=None,
                           session_id: Optional[str] = None,
                           happened_at: Optional[datetime] = None) -> int:
    """The researchers' copy of an alert; once per person and event, like the collectors'."""
    sent = 0
    for email, mode in await watchers_of(conn, hospital_id):
        if mode == "problems" and event not in PROBLEMS_ONLY:
            continue
        note_id = await collectors._record(
            conn, email=email, event=event, subject=subject, body=body, event_key=event_key,
            urgent=urgent, hospital_id=hospital_id, device_id=device_id,
            session_id=session_id, happened_at=happened_at, audience="watcher")
        if note_id is not None:
            await collectors._deliver(conn, note_id)
            sent += 1
    return sent


# ============================================================ the Notifier's rounds

async def handled_followups(conn) -> None:
    """A collector pressed Seen: the researchers who had the copy hear who, and how fast."""
    rows = await conn.fetch("""
        SELECT w.id, w.email, w.event_key, w.subject, w.created_at, w.hospital_id,
               p.alert_mode, c.email AS by_email, c.seen_at, cp.display_name
          FROM collector_notifications w
          JOIN portal_people p ON p.email = w.email
          JOIN LATERAL (
                SELECT m.email, m.seen_at FROM collector_notifications m
                 WHERE m.audience = 'collector' AND m.seen_at IS NOT NULL
                   AND m.event_key IN (w.event_key, 'esc:' || w.event_key)
                 ORDER BY m.seen_at LIMIT 1) c ON TRUE
          LEFT JOIN portal_people cp ON cp.email = c.email
         WHERE w.audience = 'watcher' AND w.urgent AND w.followed_up_at IS NULL
           AND w.event <> 'escalation'
           AND w.created_at > now() - interval '12 hours'
         ORDER BY w.id LIMIT 100
    """)
    for r in rows:
        await conn.execute("UPDATE collector_notifications SET followed_up_at = now() "
                           "WHERE id = $1", r["id"])
        if (r["alert_mode"] or "everything") != "everything":
            continue
        minutes = max(0, round((r["seen_at"] - r["created_at"]).total_seconds() / 60))
        who = r["display_name"] or r["by_email"]
        subject = f"Handled: {r['subject']}"
        body = (f"{who} marked it seen at {collectors._hhmm(r['seen_at'])}, "
                f"{minutes} min after the alert.")
        note_id = await collectors._record(
            conn, email=r["email"], event="handled", subject=subject, body=body,
            event_key=f"handled:{r['event_key']}", hospital_id=r["hospital_id"],
            audience="watcher")
        if note_id is not None:
            await collectors._deliver(conn, note_id)


async def daily_reports(conn, now: Optional[datetime] = None) -> None:
    """Every evening, once: one report per hospital to each researcher who looks after it."""
    local = (now or datetime.now(timezone.utc)).astimezone(collectors.zone())
    if local.time() < report_time():
        return
    day = local.date()
    rows = await conn.fetch("""
        SELECT r.email, r.hospital_id FROM researcher_hospitals r
          JOIN portal_people p ON p.email = r.email AND p.removed_at IS NULL
         WHERE r.removed_at IS NULL AND p.role IN ('researcher', 'admin')
           AND NOT EXISTS (SELECT 1 FROM collector_notifications n
                            WHERE n.email = r.email
                              AND n.event_key = 'report:' || $1::text || ':' || r.hospital_id)
         ORDER BY r.hospital_id, r.email
    """, day.isoformat())
    texts: Dict[str, str] = {}
    for r in rows:
        hid = r["hospital_id"]
        if hid not in texts:
            texts[hid] = await hospital_report(conn, hid, day)
        note_id = await collectors._record(
            conn, email=r["email"], event="daily_report",
            subject=f"Daily report: {hid}, {day:%d %b}", body=texts[hid],
            event_key=f"report:{day.isoformat()}:{hid}", hospital_id=hid, audience="watcher")
        if note_id is not None:
            await collectors._deliver(conn, note_id)


# ============================================================ the evening report

def _pct(part: int, whole: int) -> str:
    return f"{round(100 * part / whole)}%" if whole else "-"


def _minutes(seconds: Optional[float]) -> float:
    return round(float(seconds or 0) / 60, 1)


async def hospital_report(conn, hospital_id: str, day: date) -> str:
    """
    One hospital's day, as one Telegram message: a verdict, then what was
    recorded, on which microphone, what will be kept, how the collectors did,
    the PCs, and at most four things to do tomorrow.
    """
    tz = collectors.zone()
    start = datetime.combine(day, dtime(0), tzinfo=tz).astimezone(timezone.utc)
    end = start + timedelta(days=1)
    now = datetime.now(timezone.utc)
    name = await conn.fetchval("SELECT name FROM hospitals WHERE hospital_id = $1", hospital_id)
    red: List[str] = []
    amber: List[str] = []
    todo: List[str] = []

    # ---------------------------------------------------------- recordings
    recs = [dict(r) for r in await conn.fetch("""
        SELECT s.session_id, s.doctor_id, s.device_id, s.opened_at, s.closed_at,
               s.total_duration_seconds, s.microphones, s.quarantine_reason, s.close_reason,
               s.confirmation, s.segment_count, s.archived_at, s.copied_at,
               (SELECT count(*) FROM segments g WHERE g.session_id = s.session_id) AS received,
               (SELECT count(*) FROM integrity_alerts a WHERE a.session_id = s.session_id
                  AND a.alert_type = 'microphone_switched') AS switches,
               EXISTS (SELECT 1 FROM integrity_alerts a WHERE a.session_id = s.session_id
                         AND a.alert_type = 'silent_session') AS no_sound
          FROM sessions s
         WHERE s.hospital_id = $1 AND s.opened_at >= $2 AND s.opened_at < $3
         ORDER BY s.opened_at
    """, hospital_id, start, end)]
    week = await conn.fetchval("""
        SELECT count(*) FROM sessions WHERE hospital_id = $1
           AND opened_at >= $2::timestamptz - interval '7 days' AND opened_at < $2::timestamptz
    """, hospital_id, start)
    n = len(recs)
    closed = [r for r in recs if r["closed_at"] is not None]
    lengths = [float(r["total_duration_seconds"] or 0) for r in closed]
    short = [r for r in closed if float(r["total_duration_seconds"] or 0) < SHORT_SECONDS]
    long_ = [r for r in recs if (r["closed_at"] or now) - r["opened_at"] > collectors.TOO_LONG]
    still_open = [r for r in recs if r["closed_at"] is None]
    doctors = sorted({r["doctor_id"] for r in recs if r["doctor_id"]})

    lines = [f"\U0001F4CB Daily report: {hospital_id}" + (f" {name}" if name else "")
             + f", {day:%a %d %b %Y}", ""]
    body: List[str] = []
    body.append("RECORDINGS")
    if n:
        body.append(f"{n} recordings (7-day average {week / 7:.0f}), {len(doctors)} doctor"
                    f"{'s' if len(doctors) != 1 else ''}, {sum(lengths) / 3600:.1f} h")
        extra = [f"median {median(lengths) / 60:.0f} min" if lengths else ""]
        if short:
            extra.append(f"{len(short)} under 1 min")
        if long_:
            extra.append(f"{len(long_)} over 60 min")
        if still_open:
            extra.append(f"{len(still_open)} still open")
        body.append(", ".join(e for e in extra if e))
    else:
        body.append("No recordings today.")
    if len(short) >= 3:
        amber.append(f"{len(short)} recordings under 1 minute")
        todo.append(f"{len(short)} recordings were under 1 minute: started by mistake, or "
                    "stopped too soon? Ask the doctors.")
    if long_:
        amber.append(f"{len(long_)} recordings ran over 60 minutes")
        todo.append(f"{len(long_)} recording(s) ran over 60 min: a doctor may be forgetting "
                    "to press Stop between patients.")

    # ---------------------------------------------------------- the microphone
    kinds = {r["session_id"]: collectors.microphone_label(r["microphones"], r["switches"])["kind"]
             for r in recs}
    known = [r for r in recs if kinds[r["session_id"]] != "unknown"]
    on_jabra = [r for r in known if kinds[r["session_id"]] == "jabra"]
    mixed = [r for r in known if kinds[r["session_id"]] == "mixed"]
    pc_only = [r for r in known if kinds[r["session_id"]] == "pc"]
    no_sound = [r for r in recs if r["no_sound"]]
    if n:
        body.append("")
        body.append("MICROPHONE")
        body.append(f"Jabra all through: {len(on_jabra)} of {len(known)} "
                    f"({_pct(len(on_jabra), len(known))})"
                    + (f", {n - len(known)} not reported" if n - len(known) else ""))
        body.append(f"Switched during the recording: {len(mixed)}, laptop microphone only: "
                    f"{len(pc_only)}, no sound at all: {len(no_sound)}")
        per_doctor: Dict[str, List[int]] = {}
        for r in known:
            d = per_doctor.setdefault(r["doctor_id"] or "unknown", [0, 0])
            d[1] += 1
            d[0] += kinds[r["session_id"]] == "jabra"
        weak = sorted(((d, j, t) for d, (j, t) in per_doctor.items() if t and j / t < 0.9),
                      key=lambda x: x[1] / x[2])
        if weak:
            body.append("Doctors below 90% on the Jabra: "
                        + ", ".join(f"{d} {j}/{t}" for d, j, t in weak))
        share = len(on_jabra) / len(known) if known else 1.0
        if len(known) >= 3 and share < 0.7:
            red.append(f"only {_pct(len(on_jabra), len(known))} on the Jabra")
        elif len(known) >= 3 and share < 0.9:
            amber.append(f"{_pct(len(on_jabra), len(known))} on the Jabra")
        if weak and weak[0][2] - weak[0][1] >= 2:
            d, j, t = weak[0]
            todo.append(f"Ask {d} to keep the Jabra plugged in: {t - j} of {t} recordings "
                        "were not on it all through.")
        if no_sound:
            red.append(f"{len(no_sound)} recording(s) with no sound")
            todo.append(f"{len(no_sound)} recording(s) had no sound at all: check the "
                        "microphone is not muted on that PC.")

    # ---------------------------------------------------------- what will be kept
    if n:
        conf = Counter(r["confirmation"] for r in recs)
        quarantined = [r for r in recs if r["quarantine_reason"]]
        ended_normally = {"prescription_built", "superseded_by_new_patient",
                          "counselling_limit", "", None}
        stopped_early = [r for r in closed if r["close_reason"] not in ended_normally]
        incomplete = [r for r in closed if (r["received"] or 0) < (r["segment_count"] or 0)]
        body.append("")
        body.append("WHAT WILL BE KEPT")
        body.append(f"CMED confirmed {conf.get('confirmed', 0)}, waiting {conf.get('pending', 0)}, "
                    f"not confirmed {conf.get('unconfirmed', 0)}, refused {conf.get('refused', 0)}"
                    + (f", before CMED {conf.get('legacy', 0)}" if conf.get("legacy") else ""))
        body.append(f"Quarantined {len(quarantined)}, stopped early {len(stopped_early)}, "
                    f"audio not all on the server {len(incomplete)}")
        body.append(f"Archived {sum(1 for r in recs if r['archived_at'])}, "
                    f"cloud copy {sum(1 for r in recs if r['copied_at'])}")
        if conf.get("unconfirmed"):
            amber.append(f"{conf['unconfirmed']} not confirmed by CMED")
            todo.append(f"{conf['unconfirmed']} recording(s) are not confirmed by CMED and will "
                        "be erased unless CMED confirms them: check those visits with CMED.")
        if quarantined:
            amber.append(f"{len(quarantined)} quarantined")
            todo.append(f"{len(quarantined)} recording(s) quarantined: see why in Recordings "
                        "(filter: quarantined).")

    # ---------------------------------------------------------- the collectors
    body.append("")
    body.append("DATA COLLECTORS")
    places = [dict(p) for p in await conn.fetch("""
        SELECT pl.* FROM places pl JOIN place_hospitals ph ON ph.place_id = pl.place_id
         WHERE ph.hospital_id = $1 AND ph.removed_at IS NULL ORDER BY pl.name
    """, hospital_id)]
    if not places:
        body.append("No place includes this hospital, so no data collector is alerted.")
        red.append("no data collector covers it")
        todo.append("Put this hospital in a place (People > Places), so its collectors "
                    "are alerted.")
    uncovered = 0
    for place in places:
        shifts = [dict(s) for s in await conn.fetch("""
            SELECT s.*, p.display_name FROM collector_shifts s
              LEFT JOIN portal_people p ON p.email = s.email
             WHERE s.place_id = $1 AND s.shift_date = $2 ORDER BY s.started_at
        """, place["place_id"], day)]
        shown = []
        for sh in shifts:
            who = sh["display_name"] or sh["email"]
            note = ""
            if sh["shift"] in ("morning", "evening"):
                due = datetime.combine(day, place[f"{sh['shift']}_start"], tzinfo=tz)
                late = (sh["started_at"] - due).total_seconds() / 60
                if late > LATE_MINUTES:
                    note += f", {late:.0f} min late"
                    amber.append(f"{sh['shift']} shift started {late:.0f} min late")
                    todo.append(f"{who} started the {sh['shift']} shift {late:.0f} min late.")
            if sh["ended_by"] == "auto":
                note += ", did not press End"
            shown.append(f"{sh['shift'].title()}: {who} {collectors._hhmm(sh['started_at'])}"
                         f"-{collectors._hhmm(sh['ended_at']) or 'still on'}{note}")
        body.append(f"{place['name']}: " + ("; ".join(shown) if shown else "nobody started a shift"))
        for kind in ("morning", "evening"):
            w0 = datetime.combine(day, place[f"{kind}_start"], tzinfo=tz)
            w1 = datetime.combine(day, place[f"{kind}_end"], tzinfo=tz)
            inside = [r for r in recs if w0 <= r["opened_at"] < w1]
            lost = [r for r in inside
                    if not any(sh["started_at"] <= r["opened_at"] < (sh["ended_at"] or now)
                               for sh in shifts)]
            uncovered += len(lost)
            if inside and len(lost) == len(inside):
                todo.append(f"Nobody was on the {kind} shift at {place['name']} for "
                            f"{len(lost)} recording(s).")
        people = await collectors.collectors_of(conn, place["place_id"])
        linked = {r["email"] for r in await conn.fetch(
            "SELECT email FROM collector_telegram WHERE email = ANY($1::text[])", people)}
        missing = [e for e in people if e not in linked]
        body.append(f"Telegram: {len(linked)} of {len(people)} collectors connected"
                    + (f" (not: {', '.join(missing)})" if missing else ""))
        if missing:
            amber.append("a collector without Telegram")
    if uncovered:
        red.append(f"{uncovered} recording(s) with nobody on shift")

    alerts = await conn.fetch("""
        SELECT n.event_key, min(n.event) AS event, bool_or(n.urgent) AS urgent,
               min(n.created_at) AS at,
               (SELECT min(m.seen_at) FROM collector_notifications m
                 WHERE m.audience = 'collector'
                   AND m.event_key IN (n.event_key, 'esc:' || n.event_key)) AS seen
          FROM collector_notifications n
         WHERE n.hospital_id = $1 AND n.audience = 'collector' AND n.event <> 'escalation'
           AND n.created_at >= $2 AND n.created_at < $3
         GROUP BY n.event_key
    """, hospital_id, start, end)
    urgent = [a for a in alerts if a["urgent"]]
    handled = [a for a in urgent if a["seen"]]
    waits = [(a["seen"] - a["at"]).total_seconds() / 60 for a in handled]
    unhandled = [a for a in urgent if not a["seen"] and now - a["at"] > UNHANDLED_AFTER]
    if alerts:
        kinds_sent = Counter(a["event"] for a in alerts)
        body.append("Alerts to collectors: " + ", ".join(
            f"{ALERT_NAMES.get(k, k)} {v}" for k, v in kinds_sent.most_common()))
    if urgent:
        body.append(f"Urgent {len(urgent)}: handled {len(handled)}"
                    + (f" (median {median(waits):.0f} min, slowest {max(waits):.0f} min)"
                       if waits else "")
                    + f", not handled {len(unhandled)}")
    if unhandled:
        red.append(f"{len(unhandled)} urgent alert(s) not handled")
        todo.insert(0, f"{len(unhandled)} urgent alert(s) were never marked seen: talk to the "
                       "collector on shift.")

    # ---------------------------------------------------------- the PCs
    body.append("")
    body.append("PCs")
    pcs = await conn.fetch("""
        SELECT d.device_id, d.machine_name, d.app_version, d.last_seen_at, d.pending_segments,
               d.held_count, d.disk_encrypted, d.clock_offset_seconds, d.microphone_is_clinic,
               (SELECT count(DISTINCT n.event_key) FROM collector_notifications n
                 WHERE n.device_id = d.device_id AND n.event = 'pc_silent'
                   AND n.created_at >= $2 AND n.created_at < $3) AS offline
          FROM devices d WHERE d.hospital_id = $1 AND d.revoked_at IS NULL
         ORDER BY d.machine_name
    """, hospital_id, start, end)
    if not pcs:
        body.append("No PC is enrolled at this hospital.")
    versions = sorted({p["app_version"] for p in pcs if p["app_version"]})
    for p in pcs:
        pc = p["machine_name"] or "a PC"
        notes = []
        if p["offline"]:
            notes.append(f"offline {p['offline']} time{'s' if p['offline'] > 1 else ''}")
        if day == now.astimezone(tz).date() and (
                p["last_seen_at"] is None or now - p["last_seen_at"] > collectors.SILENT_AFTER):
            notes.append("not reporting now")
        if p["pending_segments"]:
            notes.append(f"{p['pending_segments']} clips still to upload")
            amber.append(f"{pc} has clips waiting")
            todo.append(f"{pc}: {p['pending_segments']} clips are still on the PC - check its "
                        "internet.")
        if p["held_count"]:
            notes.append(f"{p['held_count']} recordings held on the PC")
        if p["microphone_is_clinic"] is False:
            notes.append("Jabra not plugged in now")
        if p["disk_encrypted"] is False:
            notes.append("disk not encrypted")
            amber.append(f"{pc} disk not encrypted")
        if p["clock_offset_seconds"] is not None and abs(float(p["clock_offset_seconds"])) > 120:
            notes.append(f"clock {abs(float(p['clock_offset_seconds'])) / 60:.0f} min off")
        if len(versions) > 1:
            notes.append(f"recorder {p['app_version']}")
        body.append(f"{pc}: " + (", ".join(notes) if notes else "fine"))

    # ---------------------------------------------------------- verdict and to-do
    if red:
        verdict = "\U0001F534 Needs attention: " + "; ".join(red[:3])
    elif amber:
        verdict = "\U0001F7E1 Mostly fine: " + "; ".join(amber[:3])
    elif n:
        verdict = "\U0001F7E2 All in order"
    else:
        verdict = "⚪ No recordings today"
    lines.append(verdict)
    lines.append("")
    lines += body
    if todo:
        lines += ["", "TO DO TOMORROW"]
        lines += [f"{i}. {t}" for i, t in enumerate(todo[:4], 1)]
    return "\n".join(lines)[:3900]


ALERT_NAMES = {
    "jabra_unplugged": "Jabra unplugged", "jabra_back": "Jabra back",
    "jabra_not_plugged": "Jabra not plugged in", "recording_problem": "recording problem",
    "pc_silent": "PC not reporting", "recording_without_jabra": "recorded without the Jabra",
    "recording_too_long": "recording not stopped",
}


# ============================================================ the researcher's page

async def _watcher(request: Request, *, write: bool = False) -> portal.Person:
    person = await portal.person_for(request)
    if person is None:
        raise HTTPException(status_code=401, detail="sign in first")
    if person.role not in ("researcher", "admin"):
        raise HTTPException(status_code=403, detail="this is for researchers")
    if write:
        portal.check_csrf(request)
    return person


@router.get("/me")
async def watch_me(request: Request):
    """The researcher's hospitals, alert setting, Telegram, and their last 50 alerts."""
    person = await _watcher(request)
    async with _pool().acquire() as conn:
        hospitals = await conn.fetch("""
            SELECT r.hospital_id, h.name FROM researcher_hospitals r
              LEFT JOIN hospitals h ON h.hospital_id = r.hospital_id
             WHERE r.email = $1 AND r.removed_at IS NULL ORDER BY r.hospital_id
        """, person.email)
        mode = await conn.fetchval("SELECT alert_mode FROM portal_people WHERE email = $1",
                                   person.email)
        chat = await conn.fetchrow("SELECT username, linked_at FROM collector_telegram "
                                   "WHERE email = $1", person.email)
        notes = await conn.fetch("""
            SELECT id, created_at, happened_at, event, urgent, subject, hospital_id,
                   delivered, error, channel
              FROM collector_notifications WHERE email = $1 AND audience = 'watcher'
             ORDER BY id DESC LIMIT 50
        """, person.email)
    return {"email": person.email,
            "hospitals": [dict(h) for h in hospitals],
            "mode": mode or "everything",
            "report_at": report_time().strftime("%H:%M"),
            "telegram": {"ready": collectors.telegram_ready(), "linked": chat is not None,
                         "username": chat["username"] if chat else None,
                         "linked_at": collectors._iso(chat["linked_at"]) if chat else None},
            "notifications": [{k: collectors._iso(v) for k, v in dict(n).items()}
                              for n in notes]}


class ModeRequest(BaseModel):
    mode: str = Field(..., pattern=r"^(everything|problems)$")


@router.post("/mode")
async def set_mode(body: ModeRequest, request: Request):
    person = await _watcher(request, write=True)
    async with _pool().acquire() as conn:
        await conn.execute("UPDATE portal_people SET alert_mode = $2 WHERE email = $1",
                           person.email, body.mode)
    await portal._audit("researcher.alert_mode", person.email, {"mode": body.mode})
    return {"status": "saved", "mode": body.mode}


@router.post("/telegram/link")
async def watch_telegram_link(request: Request):
    person = await _watcher(request, write=True)
    bot = await collectors.telegram_bot()
    if bot is None:
        raise HTTPException(status_code=409, detail=collectors.telegram_status_now()["problem"]
                            or "Telegram is not set up on this server yet")
    code = secrets.token_urlsafe(18)
    async with _pool().acquire() as conn:
        await conn.execute("INSERT INTO collector_telegram_codes (code, email) VALUES ($1, $2)",
                           code, person.email)
    return {"url": f"https://t.me/{bot}?start={code}", "bot": bot}


@router.post("/telegram/test")
async def watch_telegram_test(request: Request):
    person = await _watcher(request, write=True)
    async with _pool().acquire() as conn:
        chat = await conn.fetchval("SELECT chat_id FROM collector_telegram WHERE email = $1",
                                   person.email)
    if chat is None:
        raise HTTPException(status_code=409, detail="connect Telegram first")
    ok, error = await collectors.send_telegram(
        chat, "Test alert from AIMScribe.\n\nAlerts for the hospitals you look after, and the "
              "evening report, will come here.")
    return {"sent": ok, "error": error}


@router.get("/report")
async def report(request: Request, hospital_id: str, day: Optional[str] = None):
    """A hospital's report for a day (today by default), as it is sent in the evening."""
    await _watcher(request)
    try:
        when = date.fromisoformat(day) if day else datetime.now(collectors.zone()).date()
    except ValueError:
        raise HTTPException(status_code=400, detail="day must be YYYY-MM-DD")
    async with _pool().acquire() as conn:
        if not await conn.fetchval("SELECT 1 FROM hospitals WHERE hospital_id = $1", hospital_id):
            raise HTTPException(status_code=404, detail="no such hospital")
        text = await hospital_report(conn, hospital_id, when)
    return {"hospital_id": hospital_id, "day": when.isoformat(), "text": text}


# ============================================================ admin: who looks after what

class WatchRequest(BaseModel):
    email: str = Field(..., max_length=254)
    hospital_id: str = Field(..., min_length=1, max_length=64)
    remove: bool = False


@admin_router.post("/people/watch")
async def assign(body: WatchRequest, request: Request, _: None = Depends(api_v2.require_admin)):
    """Give a researcher a hospital to look after, or take it away (marked, never deleted)."""
    email = body.email.strip().lower()
    by = await portal._actor(request)
    async with _pool().acquire() as conn:
        role = await conn.fetchval("SELECT role FROM portal_people WHERE email = $1 "
                                   "AND removed_at IS NULL", email)
        if role is None:
            raise HTTPException(status_code=404, detail="no such person")
        if role not in ("researcher", "admin"):
            raise HTTPException(status_code=409,
                                detail="only researchers and admins look after hospitals; "
                                       "a data collector's hospitals come from their place")
        if not await conn.fetchval("SELECT 1 FROM hospitals WHERE hospital_id = $1",
                                   body.hospital_id):
            raise HTTPException(status_code=404, detail="no such hospital")
        if body.remove:
            await conn.execute("""
                UPDATE researcher_hospitals SET removed_at = now()
                 WHERE email = $1 AND hospital_id = $2 AND removed_at IS NULL
            """, email, body.hospital_id)
        else:
            await conn.execute("""
                INSERT INTO researcher_hospitals (email, hospital_id, added_by)
                VALUES ($1, $2, $3)
                ON CONFLICT (email, hospital_id) DO UPDATE
                   SET removed_at = NULL, added_by = EXCLUDED.added_by, added_at = now()
            """, email, body.hospital_id, by)
    await portal._audit("researcher.hospital", by, {"email": email,
                                                    "hospital_id": body.hospital_id,
                                                    "removed": body.remove})
    return {"status": "removed" if body.remove else "added"}


__all__ = ["router", "admin_router", "watched_hospitals", "watchers_of", "copy_to_watchers",
           "handled_followups", "daily_reports", "hospital_report", "report_time"]
