"""
Researchers who look after hospitals (migrations 038, 039).

Researchers see and download every hospital, as before. An admin also gives
each researcher the hospitals they look after. The collector on shift acts in
the room; the researcher supervises - so for their hospitals a researcher gets
each alert written for supervision, with who to call:

    alert          every alert the collectors get, as it happens: what
                   happened, the doctor, PC and consultation, what the
                   collector was asked to do, and the collector on shift (or,
                   with nobody on shift, all the place's collectors) with
                   their phone and Telegram - without the Seen button
    seen           "<collector> pressed Seen at 10:09 (4 min)"
    not seen       an urgent alert nobody pressed Seen for in 15 minutes
    still          a problem still there 30, 60 and 90 minutes after the
                   alert - also sent again to the collector on shift
    fixed / ended  once that problem is gone: how, and how long it took
    pattern        three unusual stops by one doctor within two hours
    summaries      at 14:30 and 21:00 (AIMS_MIDDAY_SUMMARY_AT, AIMS_REPORT_AT):
                   one message per hospital for that shift, and one CSV of
                   every recording and alert at their hospitals

Quiet hours (16 Nov): from the evening summary until a place's morning shift
starts, nobody is messaged - the PCs are being switched off and nobody is on
shift. What happens then is kept, marked 'held', and is in the next summary.
A PC that stops reporting outside the shift hours is not an alert at all.

A researcher who wants less chooses 'problems': the urgent alerts, what
stayed wrong, and the report. Everything goes through the collectors'
Telegram bot and mailer (collectors.py); nothing here deletes anything.
"""
from __future__ import annotations

import logging
import os
import re
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

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v2/dashboard/watch", tags=["researchers"])
admin_router = APIRouter(prefix="/api/v2/admin", tags=["researchers"])

# 18 Nov 2026, the lab's rule: only two ends are normal - the consultation
# finished, or the next patient came in. Everything else is a problem: the
# Jabra or PC microphone, any pause, a patient who left or did not consent,
# an emergency, a technical stop. A researcher on 'problems' gets every one of
# those; 'everything' adds only the normal news (EVERYTHING_ONLY).
EVERYTHING_ONLY = {"recording_finished", "jabra_back", "handled"}
PROBLEMS_ONLY = {"escalation", "pc_silent", "recording_problem", "no_shift_started",
                 "recording_too_long", "stopped_technical", "daily_report", "shift_report",
                 # 25 Nov 2026
                 "pc_not_on", "patients_not_recorded", "silent_recording",
                 "summary_csv", "summary_xlsx", "still_not_fixed", "problem_fixed",
                 "repeated_stops", "repeated_unusual",
                 "stopped_unusually", "jabra_unplugged", "jabra_not_plugged",
                 "recording_without_jabra", "recording_paused", "recording_resumed",
                 "still_paused"}
MODES = ("everything", "problems")
# The summaries themselves go out at their time, quiet hours or not.
SUMMARY_EVENTS = ("shift_report", "summary_csv", "summary_xlsx", "daily_report")
UNHANDLED_AFTER = timedelta(minutes=15)
SHORT_SECONDS = 60
LATE_MINUTES = 15

# A problem still there this long after the alert is sent again - to the
# collector on shift and the researchers - every STILL_AFTER, STILL_TIMES times.
STILL_AFTER = timedelta(minutes=30)
STILL_TIMES = 3
WATCH_FOR = timedelta(hours=6)
OFF_AFTER = timedelta(minutes=15)         # a PC not heard from: its problem has ended
# Unusual stops by one doctor that make a pattern worth a call.
PATTERN_STOPS = 3
PATTERN_WINDOW = timedelta(hours=2)

# Which urgent alerts are one problem that can last, and what it is about:
# a PC's Jabra, a PC, a PC's recordings, one recording, one shift.
FAMILY = {"jabra_unplugged": "jabra", "jabra_not_plugged": "jabra",
          "recording_without_jabra": "jabra", "pc_silent": "pc",
          "recording_problem": "recording", "stopped_technical": "recording",
          "recording_too_long": "long", "no_shift_started": "noshift",
          # 18 Nov: a recording the doctor left paused.
          "still_paused": "paused"}

PHONE = re.compile(r"^\+?[0-9][0-9 -]{5,20}[0-9]$")


def report_time() -> dtime:
    # 26 Nov 2026 (the owner): the evening summary at 21:00, half an hour after
    # the evening shift ends, so the last consultations are in it.
    text = os.getenv("AIMS_REPORT_AT", "").strip() or "21:00"
    try:
        return dtime.fromisoformat(text)
    except ValueError:
        return dtime(21, 0)


def _time_env(name: str, default: dtime) -> dtime:
    text = os.getenv(name, "").strip()
    try:
        return dtime.fromisoformat(text) if text else default
    except ValueError:
        return default


def midday_time() -> dtime:
    """When the morning shift's summary goes out: 14:30 unless AIMS_MIDDAY_SUMMARY_AT."""
    return _time_env("AIMS_MIDDAY_SUMMARY_AT", dtime(14, 30))


def quiet_hours_on() -> bool:
    return os.getenv("AIMS_QUIET_HOURS", "on").strip().lower() not in ("0", "off", "false",
                                                                       "no")


async def _place_rows(conn, hospital_id: Optional[str] = None,
                      place_id: Optional[int] = None) -> List[Any]:
    if place_id is not None:
        return await conn.fetch("SELECT * FROM places WHERE place_id = $1", place_id)
    if not hospital_id:
        return []
    return await conn.fetch("""
        SELECT pl.* FROM places pl JOIN place_hospitals ph ON ph.place_id = pl.place_id
         WHERE ph.hospital_id = $1 AND ph.removed_at IS NULL
    """, hospital_id)


async def quiet_for(conn, hospital_id: Optional[str] = None, place_id: Optional[int] = None,
                    now: Optional[datetime] = None) -> bool:
    """
    Quiet hours: from the evening summary (21:00) until the place's morning shift
    starts (08:00 unless set in Places). Nobody is messaged then; it waits for
    the next summary. AIMS_QUIET_HOURS=off turns this off.
    """
    if not quiet_hours_on():
        return False
    t = (now or datetime.now(timezone.utc)).astimezone(collectors.zone()).time()
    if t >= report_time():
        return True
    starts = [p["morning_start"] for p in await _place_rows(conn, hospital_id, place_id)]
    return t < (min(starts) if starts else dtime(8, 0))


async def in_shift_hours(conn, hospital_id: Optional[str],
                         now: Optional[datetime] = None) -> bool:
    """Inside a place's morning or evening shift: when a PC going quiet is news, not
    the lunch break or the day ending."""
    t = (now or datetime.now(timezone.utc)).astimezone(collectors.zone()).time()
    places = await _place_rows(conn, hospital_id)
    if not places:
        return dtime(8, 0) <= t < dtime(14, 0) or dtime(14, 30) <= t < dtime(20, 30)
    return any(p["morning_start"] <= t < p["morning_end"]
               or p["evening_start"] <= t < p["evening_end"] for p in places)


def _pool():
    return collectors._pool()


def clean_phone(text: Optional[str]) -> Optional[str]:
    """A phone number as given, or None for empty; ValueError if it is not one."""
    text = " ".join((text or "").split())
    if not text:
        return None
    if not PHONE.match(text):
        raise ValueError("a phone number has digits, spaces or dashes, and may start with +")
    return text


NAME_CHARS = re.compile(r"^[^\x00-\x1f<>{}\[\]\\|@#$%^*=+~`]{2,80}$")
CODE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,18}[A-Za-z0-9])?$")


def clean_name(text: Optional[str]) -> Optional[str]:
    """An official name as given, spaces tidied; None for empty; ValueError if not a name."""
    text = " ".join((text or "").split())
    if not text:
        return None
    if not NAME_CHARS.match(text) or not any(c.isalpha() for c in text):
        raise ValueError("an official name has 2 to 80 letters, spaces, dots or dashes")
    return text


def clean_code(text: Optional[str]) -> Optional[str]:
    """A lab ID such as DC-07, in capitals; None for empty; ValueError if not one."""
    text = (text or "").strip()
    if not text:
        return None
    if not CODE.match(text):
        raise ValueError("an ID has up to 20 letters, digits and dashes, like DC-07")
    return text.upper()


async def save_details(conn, email: str, official_name: Optional[str] = None,
                       collector_code: Optional[str] = None,
                       phone: Optional[str] = None) -> Dict[str, Any]:
    """
    Save what was given of a person's official name, lab ID and phone (None:
    leave as is; empty: clear). Two people still in the lab never hold the
    same ID. Returns the three as saved.
    """
    import asyncpg
    sets, args = [], [email]
    try:
        for column, value, clean in (("official_name", official_name, clean_name),
                                     ("collector_code", collector_code, clean_code),
                                     ("phone", phone, clean_phone)):
            if value is not None:
                args.append(clean(value))
                sets.append(f"{column} = ${len(args)}")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    if sets:
        try:
            done = await conn.fetchval(f"UPDATE portal_people SET {', '.join(sets)} "
                                       "WHERE email = $1 AND removed_at IS NULL "
                                       "RETURNING email", *args)
        except asyncpg.UniqueViolationError:
            raise HTTPException(status_code=409, detail="this ID belongs to another data "
                                "collector; check the ID the lab gave you")
        if done is None:
            raise HTTPException(status_code=404, detail="no such person")
    row = await conn.fetchrow("SELECT official_name, collector_code, phone FROM portal_people "
                              "WHERE email = $1", email)
    if row is None:
        raise HTTPException(status_code=404, detail="no such person")
    return dict(row)


def missing_details(p: Any) -> List[str]:
    """What a collector has not saved yet, of official name, ID and phone."""
    return [what for what, key in (("official name", "official_name"),
                                   ("ID", "collector_code"), ("phone", "phone"))
            if not p.get(key)]


CONTEXT_STARTS = ("Doctor: ", "PC: ", "Hospital: ", "Consultation: ",
                  "Last consultation on this PC: ")


async def fresh_context(conn, note: Dict[str, Any]) -> str:
    """The alert's body with its doctor/PC/consultation lines as they are now - a
    reminder 30 minutes on must not say "4 min so far"."""
    body = note["body"]
    main, mark, foot = body.partition(collectors._FOOTER_MARK)
    kept = [line for line in main.split("\n") if not line.startswith(CONTEXT_STARTS)]
    _, context = await collectors.consultation_context(
        conn, session_id=note.get("session_id"), device_id=note.get("device_id"),
        hospital_id=note.get("hospital_id"))
    main = "\n".join(kept).rstrip()
    if context:
        main += "\n\n" + context
    return main + (mark + foot if mark else "")


def still_keys(key: str) -> List[str]:
    """The keys of an alert's reminders (30, 60, 90 min), named exactly for the index."""
    return [f"still:{key}:{i}" for i in range(1, STILL_TIMES + 1)]


def _first_up(text: str) -> str:
    return text[:1].upper() + text[1:]


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


# 18 Nov: alerts about something that just happened. Their speed is from the
# moment it happened to the moment Telegram took the message. (A "running 60
# min" or "PC not reporting" alert is late by design, so it is not counted.)
REAL_TIME_EVENTS = ("jabra_unplugged", "jabra_back", "jabra_not_plugged", "recording_problem",
                    "recording_without_jabra", "stopped_unusually", "stopped_technical",
                    "recording_paused", "recording_resumed")


def _span(seconds: float) -> str:
    seconds = max(0, int(round(seconds)))
    return f"{seconds} s" if seconds < 90 else f"{seconds // 60} min {seconds % 60:02d} s"


async def alert_speed(conn, hospital_id: str, start: datetime, end: datetime) -> Optional[str]:
    """How fast this hospital's alerts reached the collectors, in words; None before any."""
    row = await conn.fetchrow("""
        SELECT count(*) AS n,
               percentile_cont(0.5) WITHIN GROUP (ORDER BY extract(epoch FROM
                   delivered_at - COALESCE(happened_at, created_at))) AS typical,
               max(extract(epoch FROM delivered_at - COALESCE(happened_at, created_at)))
                   AS slowest,
               count(*) FILTER (WHERE delivered_at - COALESCE(happened_at, created_at)
                                      > interval '2 minutes') AS late
          FROM collector_notifications
         WHERE hospital_id = $1 AND audience = 'collector' AND delivered_at IS NOT NULL
           AND event = ANY($4::text[]) AND created_at >= $2 AND created_at < $3
    """, hospital_id, start, end, list(REAL_TIME_EVENTS))
    if not row or not row["n"]:
        return None
    waiting = await conn.fetchval("""
        SELECT count(*) FROM collector_notifications
         WHERE hospital_id = $1 AND channel = 'waiting' AND created_at >= $2 AND created_at < $3
    """, hospital_id, start, end)
    return (f"Alert speed, from the event to the collector's Telegram: typically "
            f"{_span(row['typical'])}, slowest {_span(row['slowest'])} ({row['n']} alerts"
            + (f", {row['late']} over 2 min" if row["late"] else "") + ")"
            + (f"; {waiting} waiting for someone to connect Telegram" if waiting else ""))


async def assigned_since(conn, hospital_id: Optional[str]) -> Dict[str, datetime]:
    """When each researcher began looking after this hospital."""
    if not hospital_id:
        return {}
    rows = await conn.fetch("SELECT email, added_at FROM researcher_hospitals "
                            "WHERE hospital_id = $1 AND removed_at IS NULL", hospital_id)
    return {r["email"]: r["added_at"] for r in rows}


def _before_their_time(since: Dict[str, datetime], email: str,
                       moment: Optional[datetime]) -> bool:
    """
    17 Nov: a researcher assigned at 16:29 was sent, at once, a refusal from
    10:09 and every other alert of the day the rules still looked at - all
    at the same second. They get what happens after they were assigned.
    """
    added = since.get(email)
    return moment is not None and added is not None and moment < added


async def _hospitals_for(conn, hospital_id: Optional[str], place_id: Optional[int]) -> List[str]:
    if hospital_id:
        return [hospital_id]
    return await collectors.place_hospitals(place_id, conn) if place_id else []


# ============================================================ who to call

def _person_line(p: Dict[str, Any], *, since: bool) -> str:
    name = collectors.person_label(p)
    if not p.get("official_name") or not p.get("collector_code"):
        name += " (official name or ID not saved)"
    phone = f"phone {p['phone']}" if p.get("phone") else "no phone given"
    if p.get("username"):
        tg = f"Telegram @{p['username']}"
    elif p.get("chat_id") is not None:
        tg = "Telegram connected, no username (call instead)"
    else:
        tg = "Telegram not connected"
    line = f"- {name}: {phone}, {tg}"
    if since and p.get("started_at"):
        line += (f"\n  on the {p.get('shift') or ''} shift since "
                 f"{collectors._hhmm(p['started_at'])}")
    return line


async def on_shift(conn, hospital_id: Optional[str] = None,
                   place_id: Optional[int] = None) -> List[Dict[str, Any]]:
    """Per place holding this hospital: the collectors on shift now, or all of its
    collectors when nobody is - with phone and Telegram."""
    places = [place_id] if place_id else await collectors.places_of(conn, hospital_id)
    out = []
    for pid in places:
        name = await conn.fetchval("SELECT name FROM places WHERE place_id = $1", pid)
        duty = await conn.fetch("""
            SELECT s.email, s.started_at, s.shift, p.display_name, p.phone,
                   p.official_name, p.collector_code, t.username, t.chat_id
              FROM collector_shifts s
              JOIN portal_people p ON p.email = s.email AND p.removed_at IS NULL
              LEFT JOIN collector_telegram t ON t.email = s.email
             WHERE s.place_id = $1 AND s.ended_at IS NULL ORDER BY s.started_at
        """, pid)
        everyone = [] if duty else await conn.fetch("""
            SELECT p.email, p.display_name, p.phone, p.official_name, p.collector_code,
                   t.username, t.chat_id
              FROM portal_people p LEFT JOIN collector_telegram t ON t.email = p.email
             WHERE p.role = 'collector' AND p.place_id = $1 AND p.removed_at IS NULL
             ORDER BY p.email
        """, pid)
        out.append({"place_id": pid, "place": name or f"place {pid}",
                    "on_shift": [dict(r) for r in duty],
                    "collectors": [dict(r) for r in everyone]})
    return out


async def contact_text(conn, hospital_id: Optional[str] = None,
                       place_id: Optional[int] = None) -> str:
    """Who to call about this hospital, for the researcher's message."""
    places = await on_shift(conn, hospital_id, place_id)
    if not places:
        return "No data collector covers this hospital: no place holds it."
    parts = []
    for p in places:
        if p["on_shift"]:
            parts.append(f"Data collector on shift at {p['place']}:\n"
                         + "\n".join(_person_line(c, since=True) for c in p["on_shift"]))
        elif p["collectors"]:
            parts.append(f"Nobody is on shift at {p['place']}. Its data collectors:\n"
                         + "\n".join(_person_line(c, since=False) for c in p["collectors"]))
        else:
            parts.append(f"{p['place']} has no data collector.")
    return "\n\n".join(parts)


def researcher_body(body: str, contacts: str) -> str:
    """
    The collector's alert, rewritten for the researcher: the same facts, the
    collector's instruction as "The collector was asked to: ...", no footer
    about pressing Seen, and who to call.
    """
    main = body.split(collectors._FOOTER_MARK)[0].strip()
    main = re.sub(r"(^|\n\n)Please (\w)",
                  lambda m: m.group(1) + "The collector was asked to: " + m.group(2).lower(),
                  main)
    return main + ("\n\n" + contacts if contacts else "")


async def _seen_text(conn, keys: List[str], first_at: datetime, chain: str = "") -> str:
    row = await conn.fetchrow("""
        SELECT n.seen_at, """ + collectors.label_sql("p", "n.email") + """ AS who
          FROM collector_notifications n LEFT JOIN portal_people p ON p.email = n.email
         WHERE n.event_key = ANY($1::text[])
           AND n.audience = 'collector' AND n.seen_at IS NOT NULL
         ORDER BY n.seen_at LIMIT 1
    """, list(keys) + (still_keys(chain) if chain else []))
    if row is None:
        return "Nobody has pressed Seen."
    minutes = max(0, round((row["seen_at"] - first_at).total_seconds() / 60))
    return (f"{row['who']} pressed Seen at {collectors._hhmm(row['seen_at'])} "
            f"({minutes} min after the alert).")


async def copy_to_watchers(conn, *, hospital_id: Optional[str], event: str, subject: str,
                           body: str, event_key: str, urgent: bool = False, device_id=None,
                           session_id: Optional[str] = None,
                           happened_at: Optional[datetime] = None,
                           hold: bool = False) -> int:
    """The researchers' version of an alert; once per person and event, like the collectors'.
    On 'problems', only urgent alerts and PROBLEMS_ONLY. In quiet hours (`hold`) it
    is kept, marked held, and not sent."""
    sent = 0
    to_send: List[int] = []
    since = await assigned_since(conn, hospital_id) \
        if happened_at is not None and event not in SUMMARY_EVENTS else {}
    for email, mode in await watchers_of(conn, hospital_id):
        if mode == "problems" and event in EVERYTHING_ONLY:
            continue
        if _before_their_time(since, email, happened_at):
            continue
        note_id = await collectors._record(
            conn, email=email, event=event, subject=subject, body=body, event_key=event_key,
            urgent=urgent, hospital_id=hospital_id, device_id=device_id,
            session_id=session_id, happened_at=happened_at, audience="watcher")
        if note_id is not None:
            if hold:
                await collectors._hold(conn, note_id)
            else:
                to_send.append(note_id)
                sent += 1
    await collectors.deliver_all(conn, to_send)
    return sent


async def _to_watchers(conn, hospitals: List[str], *, since: Optional[datetime] = None,
                       first_key: Optional[str] = None, **note) -> List[str]:
    """One researcher message to everyone looking after any of these hospitals -
    with `since`, the start of the problem, not to one assigned after it who
    never had its first alert (`first_key`)."""
    told: List[str] = []
    waiting: List[int] = []
    had_first = set()
    if since is not None and first_key:
        had_first = {r["email"] for r in await conn.fetch(
            "SELECT email FROM collector_notifications WHERE event_key = $1 "
            "AND audience = 'watcher'", first_key)}
    for hid in hospitals:
        assigned = await assigned_since(conn, hid) if since is not None else {}
        for email, _mode in await watchers_of(conn, hid):
            if email in told:
                continue
            if email not in had_first and _before_their_time(assigned, email, since):
                continue
            note_id = await collectors._record(conn, email=email, hospital_id=hid,
                                               audience="watcher", **note)
            if note_id is not None:
                waiting.append(note_id)
                told.append(email)
    await collectors.deliver_all(conn, waiting)
    return told


# ============================================================ problems that last

async def _still_wrong(conn, family: str, first: Dict[str, Any],
                       now: datetime) -> Tuple[str, str]:
    """
    Is the problem behind this alert 'still' there, 'fixed', or 'ended' some
    other way (the PC switched off)? And in words, how it stands.
    """
    hhmm = collectors._hhmm
    if family in ("jabra", "pc", "recording"):
        dev = await conn.fetchrow("SELECT microphone, microphone_is_clinic, last_seen_at, "
                                  "revoked_at FROM devices WHERE device_id = $1",
                                  first["device_id"])
        if dev is None or dev["revoked_at"] is not None:
            return "ended", "the PC is no longer in use"
        seen = dev["last_seen_at"]
        if family == "pc":
            if seen is not None and seen > first["created_at"]:
                return "fixed", f"the PC reported again at {hhmm(seen)}"
            return "still", ("the PC has not reported since "
                          + (hhmm(seen) if seen else "it was set up"))
        if seen is None or seen < now - OFF_AFTER:
            return "ended", ("the PC was switched off or went offline at "
                           + (hhmm(seen) if seen else "an unknown time"))
        if family == "jabra":
            if dev["microphone_is_clinic"]:
                return "fixed", "the Jabra is back in use"
            return "still", f"the PC is still on {dev['microphone'] or 'its own microphone'}"
        # A recording problem is fixed once a recording over a minute was made
        # with no problem.
        rows = await conn.fetch("""
            SELECT s.opened_at, s.close_reason FROM sessions s
             WHERE s.device_id = $1 AND s.opened_at > $2 AND s.closed_at IS NOT NULL
               AND s.closed_at - s.opened_at >= interval '1 minute'
               AND NOT EXISTS (SELECT 1 FROM integrity_alerts a
                                WHERE a.session_id = s.session_id
                                  AND a.alert_type = ANY($3::text[]))
             ORDER BY s.opened_at LIMIT 20
        """, first["device_id"], first["created_at"], list(collectors.PROBLEMS))
        for r in rows:
            if collectors.stop_kind(r["close_reason"]) not in collectors.TECHNICAL_ENDS:
                return "fixed", f"a good recording was made at {hhmm(r['opened_at'])}"
        return "still", "no good recording on this PC since the alert"
    if family == "long":
        s = await conn.fetchrow("SELECT opened_at, closed_at, confirmation FROM sessions "
                                "WHERE session_id = $1", first["session_id"])
        if s is None:
            return "ended", "the recording is gone"
        if s["confirmation"] == "refused":
            # 18 Nov: not "still running" - the patient said no and it was deleted.
            return "ended", "the patient did not consent; the recording was deleted"
        if s["closed_at"] is not None:
            return "fixed", f"the recording was stopped at {hhmm(s['closed_at'])}"
        minutes = int((now - s["opened_at"]).total_seconds() // 60)
        return "still", f"the recording is still running ({minutes} min)"
    if family == "paused":
        s = await conn.fetchrow("SELECT closed_at, paused_since, pause_reason FROM sessions "
                                "WHERE session_id = $1", first["session_id"])
        if s is None:
            return "ended", "the recording is gone"
        if s["closed_at"] is not None:
            return "ended", f"the recording ended at {hhmm(s['closed_at'])}, still paused"
        began = first.get("happened_at")
        if s["paused_since"] is None or (began is not None and s["paused_since"] != began):
            return "fixed", "the doctor pressed Resume"
        words = (s["pause_reason"] or "no reason given").replace("_", " ")
        return "still", f"paused since {hhmm(s['paused_since'])} ({words})"
    if family == "noshift":
        r = await conn.fetchrow("""
            SELECT s.started_at, """ + collectors.label_sql("p", "s.email") + """ AS who
              FROM collector_shifts s LEFT JOIN portal_people p ON p.email = s.email
             WHERE s.place_id = $1 AND s.started_at > $2::timestamptz - interval '1 hour'
             ORDER BY s.started_at LIMIT 1
        """, first["place_id"], first["created_at"])
        if r is not None:
            return "fixed", f"{r['who']} started the shift at {hhmm(r['started_at'])}"
        return "still", "nobody has started the shift"
    return "ended", "it is no longer watched"


def _group(note: Dict[str, Any]) -> Tuple[str, str]:
    family = FAMILY[note["event"]]
    if family in ("long", "paused"):
        return family, str(note["session_id"])
    if family == "noshift":
        return family, note["event_key"]
    return family, str(note["device_id"])


async def persisting(conn, now: Optional[datetime] = None) -> None:
    """
    Every round: each problem the collectors were alerted about in the last six
    hours, looked at again. Gone: marked, and every researcher who heard about
    it is told how and when. Still there 30, 60, 90 minutes on: sent again to
    the collector on shift (with the Seen button) and to the researchers, with
    whether anyone pressed Seen and who to call.
    """
    now = now or datetime.now(timezone.utc)
    rows = await conn.fetch("""
        SELECT id, email, event, event_key, subject, body, created_at, place_id,
               hospital_id, device_id, session_id, happened_at
          FROM collector_notifications
         WHERE audience = 'collector' AND resolved_at IS NULL
           AND channel IS DISTINCT FROM 'held'
           AND (urgent OR event = 'no_shift_started')
           AND event = ANY($1::text[]) AND created_at > $2::timestamptz - $3::interval
         ORDER BY created_at, id
    """, list(FAMILY), now, WATCH_FOR)
    groups: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
    for r in rows:
        groups.setdefault(_group(dict(r)), []).append(dict(r))
    for (family, _), notes in groups.items():
        first = notes[0]
        chain = first["event_key"]
        prefix = f"still:{chain}:"
        keys = sorted({n["event_key"] for n in notes})
        state, how = await _still_wrong(conn, family, first, now)
        minutes = max(0, int((now - first["created_at"]).total_seconds() // 60))
        what = first["subject"]
        quiet = await quiet_for(conn, first["hospital_id"], first["place_id"], now)
        if state != "still":
            await conn.execute("UPDATE collector_notifications SET resolved_at = $2, "
                               "resolution = $3 WHERE id = ANY($1::bigint[])",
                               [n["id"] for n in notes], now, how[:200])
            # 18 Nov: every card of it now says how it ended.
            await collectors._refresh_card(conn, chain, (state, how, minutes))
            import cards
            if quiet or family in cards.CARD_FAMILIES:
                # At night: in the next summary. 21 Nov: a problem with a card -
                # the card turning green or white is the notice; a separate
                # "fixed" message said the same thing twice.
                continue
            heard = await conn.fetch("""
                SELECT DISTINCT ON (email) email, hospital_id FROM collector_notifications
                 WHERE audience = 'watcher' AND event_key = ANY($1::text[])
                 ORDER BY email
            """, keys + [f"esc:{k}" for k in keys] + still_keys(chain))
            for h in heard:
                note_id = await collectors._record(
                    conn, email=h["email"], event="problem_fixed",
                    subject=(f"Fixed after {minutes} min: " if state == "fixed" else
                             f"Ended after {minutes} min, not fixed: ") + what,
                    body=(f"{_first_up(how)} - by {collectors._hhmm(now)}, {minutes} min "
                          f"after the alert at {collectors._hhmm(first['created_at'])}. "
                          + await _seen_text(conn, keys, first["created_at"], chain)),
                    event_key=f"fixed:{chain}", hospital_id=h["hospital_id"],
                    device_id=first["device_id"], session_id=first["session_id"],
                    audience="watcher")
                if note_id is not None:
                    await collectors._deliver(conn, note_id)
            continue
        waited = now - first["created_at"] - STILL_AFTER
        if waited < timedelta(0) or quiet:
            continue
        times = min(STILL_TIMES, 1 + int(waited / STILL_AFTER))
        key = f"{prefix}{times}"
        if await conn.fetchval("SELECT 1 FROM collector_notifications WHERE event_key = $1 "
                               "LIMIT 1", key):
            continue
        subject = f"Still not fixed after {minutes} min: {what}"
        last = ("\n\nThis is the last reminder; it will be in the evening report."
                if times == STILL_TIMES else "")
        # The collector on shift now - or, with nobody on shift, whoever had the alert.
        # Every place that holds the hospital, not only the one the first note went to.
        places = (await collectors.places_of(conn, first["hospital_id"])
                  if first["hospital_id"] else [first["place_id"]])
        duty = {d["email"]: pid for pid in places
                for d in await collectors.on_duty(conn, pid)}
        if not duty:
            duty = {n["email"]: n["place_id"] for n in notes}
        now_body = await fresh_context(conn, first)
        reminders: List[int] = []
        import cards
        card_family = family in cards.CARD_FAMILIES
        for email, pid in sorted(duty.items()):
            # 18 Nov: under the card, which has the details as they are now.
            text = (f"{_first_up(how)}. The first alert was at "
                    f"{collectors._hhmm(first['created_at'])}, {minutes} min ago."
                    + (f"\n\n{cards.DO[family]}" if card_family else "\n\n" + now_body))
            note_id = await collectors._record(
                conn, email=email, event="still_not_fixed", subject=subject, body=text,
                event_key=key, urgent=True, place_id=pid, hospital_id=first["hospital_id"],
                device_id=first["device_id"], session_id=first["session_id"])
            if note_id is not None:
                reminders.append(note_id)
        await collectors.deliver_all(conn, reminders)
        await collectors._refresh_card(conn, chain)
        # 18 Nov: a researcher's card already shows 30 and 90 minutes; only the
        # 60-minute reminder is a message, with who to call.
        if card_family and times != 2:
            continue
        # Every place holding the hospital, not just the one this note went to.
        contacts = await contact_text(conn, first["hospital_id"],
                                      None if first["hospital_id"] else first["place_id"])
        body = (f"{_first_up(how)}. The first alert was at "
                f"{collectors._hhmm(first['created_at'])}, {minutes} min ago; the collector "
                "on shift has been reminded.\n"
                + await _seen_text(conn, keys, first["created_at"], chain) + "\n\n"
                + (contacts if card_family else researcher_body(now_body, contacts))
                + "\n\nPlease call or message them." + last)
        await _to_watchers(conn, await _hospitals_for(conn, first["hospital_id"],
                                                      first["place_id"]),
                           event="still_not_fixed", subject=subject, body=body,
                           event_key=key, urgent=True, device_id=first["device_id"],
                           session_id=first["session_id"], since=first["created_at"],
                           first_key=first["event_key"])


# 22 Nov, the lab's rule: the same unusual event three times in a day for one
# doctor - in a row or not - and the collector contacts the hospital manager at
# once. Again at 6, 9 ... The unusual events: a refusal, the patient left, an
# emergency or interruption, another reason, a pause. (A technical stop is red
# already, with its own reminders.)
REPEAT_TIMES = 3
UNUSUAL_WORDS = {"patient_did_not_consent": "Patient did not consent",
                 "patient_left": "Patient left the room",
                 "urgent_interruption": "Emergency or urgent interruption",
                 "other": "Stopped for another reason",
                 "pause": "Recording paused"}


async def repeated_unusual(conn, now: Optional[datetime] = None) -> None:
    """The third (sixth, ...) time today one doctor has the same unusual event: red."""
    import api_v2
    now = now or datetime.now(timezone.utc)
    tz = collectors.zone()
    local = now.astimezone(tz)
    day_start = datetime.combine(local.date(), dtime(0), tzinfo=tz).astimezone(timezone.utc)
    rows = await conn.fetch("""
        SELECT r.hospital_id, r.doctor_id, 'patient_did_not_consent' AS kind,
               r.refused_at AS at, r.session_id, r.device_id
          FROM session_refusals r
         WHERE r.refused_at >= $1
        UNION ALL
        SELECT s.hospital_id, s.doctor_id, s.close_reason, s.closed_at, s.session_id, s.device_id
          FROM sessions s
         WHERE s.closed_at >= $1 AND s.close_reason LIKE 'doctor_stopped:%'
           AND s.confirmation IS DISTINCT FROM 'refused'
           AND NOT EXISTS (SELECT 1 FROM session_refusals x WHERE x.session_id = s.session_id)
        UNION ALL
        SELECT s.hospital_id, s.doctor_id, 'pause', c.occurred_at, s.session_id, s.device_id
          FROM sessions s JOIN chain_entries c ON c.session_id = s.session_id
         WHERE s.opened_at >= $1::timestamptz - interval '12 hours'
           AND c.entry_type = 'pause' AND c.occurred_at >= $1
           AND COALESCE(c.payload->>'reason', '') <> ALL($2::text[])
    """, day_start, sorted(api_v2.INTERNAL_PAUSES))
    groups: Dict[Tuple[str, str, str], List[Dict[str, Any]]] = {}
    for r in rows:
        if not r["doctor_id"] or not r["hospital_id"]:
            continue
        kind = r["kind"] if r["kind"] in ("patient_did_not_consent", "pause") \
            else collectors.stop_kind(r["kind"])
        if kind is None or kind in collectors.TECHNICAL_ENDS:
            continue
        kind = kind if kind in UNUSUAL_WORDS else "other"
        groups.setdefault((r["hospital_id"], r["doctor_id"], kind), []).append(dict(r))
    day = local.date().isoformat()
    for (hid, doctor_id, kind), items in groups.items():
        if len(items) < REPEAT_TIMES:
            continue
        times = len(items) // REPEAT_TIMES * REPEAT_TIMES
        base = f"manager:{hid}:{doctor_id}:{kind}:{day}"
        items.sort(key=lambda r: r["at"])
        last = items[-1]
        when = ", ".join(collectors._hhmm(r["at"]) for r in items)
        label = UNUSUAL_WORDS[kind]
        subject = f"{label}: {len(items)} times today"
        body = (f"At {when}.\n\nPlease contact the hospital manager as soon as possible."
                + collectors._footer())
        # 26 Nov 2026: on 25 Nov one doctor refused 44 patients and each researcher
        # got this warning 14 times (at 3, 6, 9 ... 42). Now the first one is sent
        # and every later count EDITS it - one message per doctor, kind and day.
        sent = await conn.fetch("""
            SELECT id, email, event, urgent, subject, body, audience, event_key, created_at,
                   happened_at, device_id, session_id, place_id, hospital_id, seen_at,
                   tg_chat_id, tg_message_id
              FROM collector_notifications
             WHERE event = 'repeated_unusual' AND hospital_id = $1 AND created_at >= $2
               AND event_key IN (SELECT $3 || ':' || n FROM generate_series($4::int, $5::int, $4::int) n)
        """, hid, day_start, base, REPEAT_TIMES, max(times, REPEAT_TIMES))
        if not sent:
            await collectors.notify(
                conn, hospital_id=hid, event="repeated_unusual", subject=subject, body=body,
                event_key=f"{base}:{times}", urgent=True, everyone_if_nobody=True,
                device_id=last["device_id"], session_id=last["session_id"],
                happened_at=last["at"])
            continue
        await _update_repeat_warning(conn, [dict(r) for r in sent], len(items), body, last)


_TIMES_TODAY = re.compile(r"(?<![0-9])[0-9]+ times today")


async def _update_repeat_warning(conn, sent: List[Dict[str, Any]], count: int, body: str,
                                 last: Dict[str, Any]) -> None:
    """
    The repeated-event warning each person already has, edited to the new count.
    Only the count changes: each person's own wording ("Nobody on shift: ...",
    the doctor named for a researcher) is kept.
    """
    for note in sent:
        subject = _TIMES_TODAY.sub(f"{count} times today", note["subject"] or "", count=1)
        if subject == note["subject"]:
            continue                                    # this count is already shown
        await conn.execute("""
            UPDATE collector_notifications
               SET subject = $2, body = $3, happened_at = $4, session_id = $5
             WHERE id = $1
        """, note["id"], subject, body, last["at"], last["session_id"])
        if not (note.get("tg_chat_id") and note.get("tg_message_id")):
            continue                     # not delivered yet: it goes out with the new text
        note.update(subject=subject, body=body, happened_at=last["at"],
                    session_id=last["session_id"])
        payload: Dict[str, Any] = {
            "chat_id": note["tg_chat_id"], "message_id": note["tg_message_id"],
            "text": await collectors._telegram_body(conn, note),
            "disable_web_page_preview": True}
        if note["urgent"] and note["audience"] == "collector" and not note.get("seen_at"):
            # The collector keeps the Seen button an edit would otherwise remove.
            payload["reply_markup"] = {"inline_keyboard": [[
                {"text": "✅ Seen - I am on it", "callback_data": f"seen:{note['id']}"}]]}
        result, error = await collectors._telegram("editMessageText", payload)
        if result is None and not (error and "not modified" in error):
            logger.info("Repeat warning %s could not be edited: %s", note["id"], error)


async def repeated_stops(conn, now: Optional[datetime] = None) -> None:
    """Three unusual stops by one doctor in two hours: a pattern worth a call."""
    now = now or datetime.now(timezone.utc)
    rows = await conn.fetch("""
        SELECT s.session_id, s.hospital_id, s.doctor_id, s.device_id,
               s.closed_at AS at, s.close_reason AS reason
          FROM sessions s
         WHERE s.closed_at > $1::timestamptz - $2::interval AND s.close_reason IS NOT NULL
           AND s.hospital_id IN (SELECT hospital_id FROM researcher_hospitals
                                  WHERE removed_at IS NULL)
        UNION ALL
        SELECT r.session_id, r.hospital_id, r.doctor_id, r.device_id,
               r.refused_at, 'patient_did_not_consent'
          FROM session_refusals r
         WHERE r.refused_at > $1::timestamptz - $2::interval
           AND r.hospital_id IN (SELECT hospital_id FROM researcher_hospitals
                                  WHERE removed_at IS NULL)
         ORDER BY 5
    """, now, PATTERN_WINDOW)
    stops: Dict[Tuple[str, str], Dict[str, Dict[str, Any]]] = {}
    for r in rows:
        if collectors.stop_kind(r["reason"]) is None or not r["doctor_id"]:
            continue
        per = stops.setdefault((r["hospital_id"], r["doctor_id"]), {})
        if r["reason"] == "patient_did_not_consent" or r["session_id"] not in per:
            per[r["session_id"]] = dict(r)
    hours = int(PATTERN_WINDOW.total_seconds() // 3600)
    day = now.astimezone(collectors.zone()).date().isoformat()
    for (hid, doctor_id), per in stops.items():
        if len(per) < PATTERN_STOPS:
            continue
        # 18 Nov: again at 6, 9 ... - once a day hid the refusals after the third.
        key = f"pattern:{hid}:{doctor_id}:{day}:{len(per) // PATTERN_STOPS * PATTERN_STOPS}"
        if await quiet_for(conn, hid, now=now) or await conn.fetchval(
                "SELECT 1 FROM collector_notifications WHERE event_key = $1 LIMIT 1", key):
            continue
        items = sorted(per.values(), key=lambda r: r["at"])
        last = items[-1]
        doctor, context = await collectors.consultation_context(
            conn, session_id=last["session_id"], hospital_id=hid)
        listed = "\n".join(f"- {collectors._hhmm(r['at'])}: {collectors.stop_label(r['reason'])}"
                           for r in items)
        body = (f"{len(items)} recordings of {doctor or doctor_id} ended unusually in the "
                f"last {hours} hours:\n{listed}\n\n{context}\n\n"
                + await contact_text(conn, hid)
                + "\n\nPlease ask the collector to check with the doctor between patients: "
                  "the set-up, the Jabra, or how consent is asked.")
        await _to_watchers(conn, [hid], event="repeated_stops",
                           subject=f"{len(items)} unusual stops in {hours} hours - "
                                   f"{doctor or doctor_id}",
                           body=body, event_key=key, urgent=False,
                           device_id=last["device_id"], session_id=last["session_id"])


async def handled_followups(conn) -> None:
    """A collector pressed Seen: the researchers who had the alert hear who, and how fast."""
    rows = await conn.fetch("""
        SELECT w.id, w.email, w.event_key, w.subject, w.created_at, w.hospital_id,
               p.alert_mode, c.email AS by_email, c.seen_at, cp.display_name,
               cp.official_name, cp.collector_code
          FROM collector_notifications w
          JOIN portal_people p ON p.email = w.email
          JOIN LATERAL (
                SELECT m.email, m.seen_at FROM collector_notifications m
                 WHERE m.audience = 'collector' AND m.seen_at IS NOT NULL
                   AND m.event_key IN (w.event_key, 'esc:' || w.event_key)
                 ORDER BY m.seen_at LIMIT 1) c ON TRUE
          LEFT JOIN portal_people cp ON cp.email = c.email
         WHERE w.audience = 'watcher' AND w.urgent AND w.followed_up_at IS NULL
           AND w.event <> 'escalation' AND w.channel IS DISTINCT FROM 'held'
           AND w.created_at > now() - interval '12 hours'
         ORDER BY w.id LIMIT 100
    """)
    for r in rows:
        await conn.execute("UPDATE collector_notifications SET followed_up_at = now() "
                           "WHERE id = $1", r["id"])
        if (r["alert_mode"] or "everything") != "everything" \
                or await quiet_for(conn, r["hospital_id"]):
            continue
        minutes = max(0, round((r["seen_at"] - r["created_at"]).total_seconds() / 60))
        who = collectors.person_label({"official_name": r["official_name"],
                                       "collector_code": r["collector_code"],
                                       "display_name": r["display_name"],
                                       "email": r["by_email"]})
        subject = f"Seen: {r['subject']}"
        body = (f"{who} pressed Seen at {collectors._hhmm(r['seen_at'])}, "
                f"{minutes} min after the alert. You will hear again if it is still not "
                f"fixed {int(STILL_AFTER.total_seconds() // 60)} min after the alert.")
        note_id = await collectors._record(
            conn, email=r["email"], event="handled", subject=subject, body=body,
            event_key=f"handled:{r['event_key']}", hospital_id=r["hospital_id"],
            audience="watcher")
        if note_id is not None:
            await collectors._deliver(conn, note_id)


# ============================================================ the shift summaries

SHIFT_NAMES = {"morning": "Morning shift summary", "evening": "Evening shift summary",
               "day": "Daily report"}


def shift_window(day: date, shift: str) -> Tuple[datetime, datetime, str]:
    """
    The time a summary covers, in UTC, and in words. The two summaries meet, so
    nothing falls between them: the morning one runs from the evening summary
    the day before (so the night is in it), the evening one from the morning one.
    """
    tz = collectors.zone()
    midday, evening = midday_time(), report_time()
    if shift == "morning":
        start = datetime.combine(day - timedelta(days=1), evening, tzinfo=tz)
        end = datetime.combine(day, midday, tzinfo=tz)
        words = f"{evening:%H:%M} the day before to {midday:%H:%M}"
    elif shift == "evening":
        start = datetime.combine(day, midday, tzinfo=tz)
        end = datetime.combine(day, evening, tzinfo=tz)
        words = f"{midday:%H:%M} to {evening:%H:%M}"
    else:
        start = datetime.combine(day, dtime(0), tzinfo=tz)
        end = start + timedelta(days=1)
        words = "the whole day"
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc), words


async def shift_summaries(conn, now: Optional[datetime] = None) -> None:
    """
    At 14:30 and 21:00, once each: to every researcher, one message per hospital
    they look after, for that shift, then one CSV of all of it.
    """
    local = (now or datetime.now(timezone.utc)).astimezone(collectors.zone())
    day = local.date()
    for shift, at in (("morning", midday_time()), ("evening", report_time())):
        if local.time() < at:
            continue
        rows = await conn.fetch("""
            SELECT r.email, r.hospital_id FROM researcher_hospitals r
              JOIN portal_people p ON p.email = r.email AND p.removed_at IS NULL
             WHERE r.removed_at IS NULL AND p.role IN ('researcher', 'admin')
               AND NOT EXISTS (SELECT 1 FROM collector_notifications n
                                WHERE n.email = r.email
                                  AND n.event_key = 'summary:' || $1::text || ':'
                                                    || r.hospital_id)
             -- hospital by hospital, so one researcher's messages are spread out and
             -- Telegram's one-a-second per chat costs no waiting (load test, 16 Nov)
             ORDER BY r.hospital_id, r.email
        """, f"{day.isoformat()}:{shift}")
        texts: Dict[str, str] = {}
        for r in rows:
            hid = r["hospital_id"]
            if hid not in texts:
                texts[hid] = await hospital_report(conn, hid, day, shift)
            note_id = await collectors._record(
                conn, email=r["email"], event="shift_report",
                subject=f"{SHIFT_NAMES[shift]}: {hid}, {day:%d %b}", body=texts[hid],
                event_key=f"summary:{day.isoformat()}:{shift}:{hid}", hospital_id=hid,
                audience="watcher")
            if note_id is not None:
                await collectors._deliver(conn, note_id)
        # 20 Nov: then one Excel workbook per hospital they look after - only
        # their own hospitals, four sheets each (workbook.py).
        for r in rows:
            hid = r["hospital_id"]
            note_id = await collectors._record(
                conn, email=r["email"], event="summary_xlsx",
                subject=f"{SHIFT_NAMES[shift]} workbook: {hid}, {day:%d %b}",
                body=(f"{hid}, {shift_window(day, shift)[2]} on {day:%d %b}: Summary by "
                      "doctor; every audio file with its pauses and stops; what CMED did not "
                      "send; the alerts of each file. Also on the dashboard, My alerts."),
                event_key=f"summary-xlsx:{day.isoformat()}:{shift}:{hid}", hospital_id=hid,
                audience="watcher")
            if note_id is not None:
                await collectors._deliver(conn, note_id)


# Kept for anything that still calls it: the summaries replaced the one evening report.
daily_reports = shift_summaries


CSV_COLUMNS = ["type", "hospital", "date", "time", "until", "doctor", "pc", "minutes",
               "how_it_ended", "microphone", "cmed", "api2_demographics",
               "api3_prescription", "pauses", "paused_min", "collector_on_shift", "alert",
               "sent", "seen_by", "seen_at", "seen_after_min", "fixed_after_min",
               "how_it_was_fixed", "reminders", "session_id"]
MIC_WORDS = {"jabra": "Jabra all through", "mixed": "switched off the Jabra",
             "pc": "PC microphone only", "unknown": "not reported"}
ALERT_EVENTS = ("jabra_unplugged", "jabra_back", "jabra_not_plugged", "recording_problem",
                "pc_silent", "recording_without_jabra", "recording_too_long",
                "pc_not_on", "patients_not_recorded", "silent_recording",
                "stopped_unusually", "stopped_technical", "no_shift_started",
                "repeated_stops", "recording_paused", "still_paused", "repeated_unusual")


def _ended_words(close_reason: Optional[str], refused: bool, closed: bool) -> str:
    if refused:
        return collectors.STOP_LABELS.get("patient_did_not_consent", "patient did not consent")
    if not closed:
        return "still open"
    if close_reason == "superseded_by_new_patient":
        return "next patient came in"
    if close_reason == "consultation_ended":
        return "CMED closed the visit"
    if collectors.stop_kind(close_reason) is None:
        return "consultation finished"
    return collectors.stop_label(close_reason)


async def summary_rows(conn, hospitals: List[str], start: datetime,
                       end: datetime) -> List[Dict[str, Any]]:
    """Every recording and alert at these hospitals in this time, in time order."""
    tz = collectors.zone()
    if not hospitals:
        return []
    out: List[Tuple[datetime, Dict[str, Any]]] = []

    def local(moment):
        return moment.astimezone(tz) if moment else None

    recs = await conn.fetch("""
        SELECT s.session_id, s.hospital_id, s.doctor_id, s.opened_at, s.closed_at,
               s.total_duration_seconds, s.microphones, s.close_reason, s.confirmation,
               d.machine_name, doc.full_name, s.patient_id, s.paused_seconds,
               g.cmed_hospital_id AS cmed_code,
               COALESCE(s.visit_id, g.visit_id) AS cmed_visit_id,
               g.start_time AS cmed_start_time,
               (SELECT count(*) FROM chain_entries c WHERE c.session_id = s.session_id
                  AND c.entry_type = 'pause'
                  AND COALESCE(c.payload->>'reason', '') <> ALL($4::text[])) AS pauses,
               EXISTS (SELECT 1 FROM session_refusals x
                        WHERE x.session_id = s.session_id) AS refused,
               (SELECT count(*) FROM integrity_alerts a WHERE a.session_id = s.session_id
                  AND a.alert_type = 'microphone_switched') AS switches,
               EXISTS (SELECT 1 FROM integrity_alerts a WHERE a.session_id = s.session_id
                         AND a.alert_type = 'silent_session') AS no_sound,
               (SELECT string_agg(DISTINCT """ + collectors.label_sql("p", "c.email") + """, '; ')
                  FROM collector_shifts c
                  JOIN place_hospitals ph ON ph.place_id = c.place_id
                       AND ph.hospital_id = s.hospital_id AND ph.removed_at IS NULL
                  LEFT JOIN portal_people p ON p.email = c.email
                 WHERE c.started_at <= s.opened_at
                   AND COALESCE(c.ended_at, now()) > s.opened_at) AS on_shift
          FROM sessions s
          LEFT JOIN devices d ON d.device_id = s.device_id
          LEFT JOIN doctors doc ON doc.doctor_id = s.doctor_id
               AND doc.hospital_id = s.hospital_id
          LEFT JOIN grant_authorisations g ON g.jti = s.grant_jti AND g.patient_id = s.patient_id
         WHERE s.hospital_id = ANY($1::text[]) AND s.opened_at >= $2 AND s.opened_at < $3
         ORDER BY s.opened_at
    """, hospitals, start, end, sorted(api_v2.INTERNAL_PAUSES))
    times: Dict[str, Dict[str, Any]] = {}
    try:
        import dashboard
        times = await dashboard._cmed_message_times(recs)
    except Exception as exc:                      # the CSV goes without the two columns
        logger.warning("Summary: could not read when CMED's messages arrived: %s", exc)

    def arrived(r, name: str) -> str:
        t = times.get(r["session_id"], {})
        if r["refused"] or not t.get("cmed_matched", bool(r["cmed_code"])):
            return ""
        value = t.get(name)
        if not value:
            return "missing"
        moment = datetime.fromisoformat(value) if isinstance(value, str) else value
        return f"{local(moment):%H:%M}"

    for r in recs:
        at = local(r["opened_at"])
        kind = collectors.microphone_label(r["microphones"], r["switches"])["kind"]
        minutes = round(float(r["total_duration_seconds"] or 0) / 60, 1) if r["closed_at"]             else round((datetime.now(timezone.utc) - r["opened_at"]).total_seconds() / 60, 1)
        out.append((r["opened_at"], {
            "type": "recording", "hospital": r["hospital_id"], "date": f"{at:%Y-%m-%d}",
            "time": f"{at:%H:%M}", "until": f"{local(r['closed_at']):%H:%M}"
            if r["closed_at"] else "",
            "doctor": (f"{r['full_name']} ({r['doctor_id']})"
                       if r["full_name"] and r["full_name"] != r["doctor_id"]
                       else r["doctor_id"] or ""),
            "pc": r["machine_name"] or "", "minutes": minutes,
            "how_it_ended": _ended_words(r["close_reason"], r["refused"],
                                         r["closed_at"] is not None),
            "microphone": "no sound" if r["no_sound"] else MIC_WORDS.get(kind, kind),
            "cmed": r["confirmation"] or "", "collector_on_shift": r["on_shift"] or "nobody",
            "api2_demographics": arrived(r, "api2_at"),
            "api3_prescription": arrived(r, "api3_at"),
            "pauses": int(r["pauses"] or 0) or "",
            "paused_min": round(float(r["paused_seconds"] or 0) / 60, 1) if r["pauses"] else "",
            "session_id": r["session_id"]}))

    alerts = await conn.fetch("""
        SELECT n.event_key, min(n.event) AS event, min(n.subject) AS subject,
               min(n.hospital_id) AS hospital_id, min(n.created_at) AS at,
               min(n.session_id) AS session_id, min(d.machine_name) AS pc,
               bool_or(n.channel = 'held') AS held, bool_or(n.delivered) AS delivered,
               min(n.resolved_at) AS resolved_at, min(n.resolution) AS resolution
          FROM collector_notifications n
          LEFT JOIN devices d ON d.device_id = n.device_id
         WHERE n.audience IN ('collector', 'watcher') AND n.event = ANY($4::text[])
           AND (n.hospital_id = ANY($1::text[])
                OR n.place_id IN (SELECT place_id FROM place_hospitals
                                   WHERE hospital_id = ANY($1::text[]) AND removed_at IS NULL))
           AND n.created_at >= $2 AND n.created_at < $3
         GROUP BY n.event_key
         ORDER BY min(n.created_at)
    """, hospitals, start, end, list(ALERT_EVENTS))
    base_of: Dict[str, str] = {}
    for a in alerts:
        k = a["event_key"]
        for key in [k, f"esc:{k}"] + still_keys(k):
            base_of[key] = k
    seen_by: Dict[str, Any] = {}
    reminded: Counter = Counter()
    if base_of:
        for m in await conn.fetch("""
            SELECT m.event_key, m.seen_at, """ + collectors.label_sql("p", "m.email") + """ AS who
              FROM collector_notifications m LEFT JOIN portal_people p ON p.email = m.email
             WHERE m.audience = 'collector' AND m.seen_at IS NOT NULL
               AND m.event_key = ANY($1::text[])
        """, list(base_of)):
            k = base_of[m["event_key"]]
            if k not in seen_by or m["seen_at"] < seen_by[k]["seen_at"]:
                seen_by[k] = m
        for m in await conn.fetch("""
            SELECT DISTINCT event_key FROM collector_notifications
             WHERE event_key = ANY($1::text[]) AND event = 'still_not_fixed'
        """, [key for key in base_of if key.startswith("still:")]):
            reminded[base_of[m["event_key"]]] += 1
    for a in alerts:
        seen = seen_by.get(a["event_key"])
        reminders = reminded.get(a["event_key"], 0)
        at = local(a["at"])
        out.append((a["at"], {
            "type": "alert", "hospital": a["hospital_id"] or "", "date": f"{at:%Y-%m-%d}",
            "time": f"{at:%H:%M}", "pc": a["pc"] or "",
            "alert": a["subject"] or ALERT_NAMES.get(a["event"], a["event"]),
            "sent": "held (quiet hours)" if a["held"] and not a["delivered"]
            else ("sent" if a["delivered"] else "not sent"),
            "seen_by": seen["who"] if seen else "",
            "seen_at": f"{local(seen['seen_at']):%H:%M}" if seen else "",
            "seen_after_min": round((seen["seen_at"] - a["at"]).total_seconds() / 60)
            if seen else "",
            "fixed_after_min": round((a["resolved_at"] - a["at"]).total_seconds() / 60)
            if a["resolved_at"] else "",
            "how_it_was_fixed": a["resolution"] or "", "reminders": reminders or "",
            "session_id": a["session_id"] or ""}))
    out.sort(key=lambda x: x[0])
    return [row for _, row in out]


def csv_text(rows: List[Dict[str, Any]]) -> bytes:
    """With a byte-order mark, so Excel opens Bengali names and dashes right."""
    import csv
    import io
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=CSV_COLUMNS, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({k: row.get(k, "") for k in CSV_COLUMNS})
    return buf.getvalue().encode("utf-8-sig")


_CSV_CACHE: Dict[Tuple, Tuple[float, bytes]] = {}


async def summary_csv_for(conn, email: str, day: date,
                          shift: str) -> Tuple[str, bytes]:
    """The CSV a researcher gets: every hospital they look after, for that shift.
    Researchers looking after the same hospitals share one build for ten minutes."""
    import time as _time
    hospitals = await watched_hospitals(email, conn)
    start, end, _ = shift_window(day, shift)
    key = (tuple(hospitals), start, end)
    now = _time.monotonic()
    cached = _CSV_CACHE.get(key)
    if cached is None or now - cached[0] > 600:
        cached = (now, csv_text(await summary_rows(conn, hospitals, start, end)))
        for old in [k for k, v in _CSV_CACHE.items() if now - v[0] > 600]:
            _CSV_CACHE.pop(old, None)
        _CSV_CACHE[key] = cached
    return f"aimscribe_{day.isoformat()}_{shift}.csv", cached[1]


def _pct(part: int, whole: int) -> str:
    return f"{round(100 * part / whole)}%" if whole else "-"


def _minutes(seconds: Optional[float]) -> float:
    return round(float(seconds or 0) / 60, 1)


async def hospital_report(conn, hospital_id: str, day: date, shift: str = "day") -> str:
    """
    One hospital's shift (or day), as one Telegram message: a verdict, then what
    was recorded, on which microphone, what will be kept, how the collectors
    did, the PCs, and at most four things to do next.
    """
    tz = collectors.zone()
    start, end, span = shift_window(day, shift)
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
               EXISTS (SELECT 1 FROM session_refusals x
                        WHERE x.session_id = s.session_id) AS refused,
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

    lines = [f"\U0001F4CB {SHIFT_NAMES.get(shift, SHIFT_NAMES['day'])}: {hospital_id}"
             + (f" {name}" if name else "") + f", {day:%a %d %b %Y}"
             + (f" ({span})" if shift != "day" else ""), ""]
    body: List[str] = []
    body.append("RECORDINGS")
    if n:
        body.append(f"{n} recordings" + (f" (7-day average {week / 7:.0f})"
                                         if shift == "day" else "")
                    + f", {len(doctors)} doctor"
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
        body.append("No recordings today." if shift == "day" else
                    "No recordings in this shift.")
    # How they ended: the two ordinary ends, and every other one by its reason.
    if n:
        ends = Counter()
        for r in recs:
            if not r["refused"] and r["closed_at"] is None:
                continue                                  # still recording
            kind = "patient_did_not_consent" if r["refused"] else collectors.stop_kind(
                r["close_reason"])
            ends[kind or ("next_patient" if r["close_reason"] == "superseded_by_new_patient"
                          else "finished")] += 1
        unusual = {k: v for k, v in ends.items() if k not in ("next_patient", "finished")}
        body.append(f"Ended: next patient came in {ends.get('next_patient', 0)}, consultation "
                    f"finished {ends.get('finished', 0)}, stopped unusually "
                    f"{sum(unusual.values())}")
        if unusual:
            body.append("Stopped because: " + "; ".join(
                f"{collectors.STOP_LABELS.get(k, k.replace('_', ' '))} {v}"
                for k, v in sorted(unusual.items(), key=lambda kv: -kv[1])))
            technical = sum(v for k, v in unusual.items() if k in collectors.TECHNICAL_ENDS)
            if technical:
                amber.append(f"{technical} stopped by a technical problem")
                todo.append(f"{technical} recording(s) stopped because of a technical problem: "
                            "check that PC and its Jabra before the first patient.")
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
        incomplete = [r for r in closed if (r["received"] or 0) < (r["segment_count"] or 0)]
        body.append("")
        body.append("WHAT WILL BE KEPT")
        body.append(f"CMED confirmed {conf.get('confirmed', 0)}, waiting {conf.get('pending', 0)}, "
                    f"not confirmed {conf.get('unconfirmed', 0)}, refused {conf.get('refused', 0)}"
                    + (f", before CMED {conf.get('legacy', 0)}" if conf.get("legacy") else ""))
        body.append(f"Quarantined {len(quarantined)}, audio not all on the server "
                    f"{len(incomplete)}")
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
            SELECT s.*, p.display_name, p.official_name, p.collector_code
              FROM collector_shifts s
              LEFT JOIN portal_people p ON p.email = s.email
             WHERE s.place_id = $1 AND s.started_at < $3
               AND COALESCE(s.ended_at, now()) > $2 ORDER BY s.started_at
        """, place["place_id"], start, end)]
        shown = []
        for sh in shifts:
            who = collectors.person_label(sh)
            note = ""
            if sh["shift"] in ("morning", "evening"):
                due = datetime.combine(sh["shift_date"], place[f"{sh['shift']}_start"],
                                       tzinfo=tz)
                late = (sh["started_at"] - due).total_seconds() / 60
                if late > LATE_MINUTES:
                    note += f", {late:.0f} min late"
                    amber.append(f"{sh['shift']} shift started {late:.0f} min late")
                    todo.append(f"{who} started the {sh['shift']} shift {late:.0f} min late.")
            if sh["ended_by"] == "auto":
                note += ", did not press End"
            shown.append(f"{sh['shift'].title()}: {who} {collectors._hhmm(sh['started_at'])}"
                         f"-{collectors._hhmm(sh['ended_at']) or 'still on'}{note}")
        body.append(f"{place['name']}: " + ("; ".join(shown) if shown else
                                            "nobody was on shift"))
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
        # 040: a collector the researchers could not call, or name.
        for r in await conn.fetch("SELECT email, display_name, official_name, "
                                  "collector_code, phone FROM portal_people "
                                  "WHERE email = ANY($1::text[]) ORDER BY email", people):
            lack = missing_details(r)
            if lack:
                body.append(f"Details missing: {collectors.person_label(r)} has not saved "
                            + " or ".join(lack))
                amber.append("a collector without their details")
                todo.append(f"Ask {collectors.person_label(r)} to save their "
                            + ", ".join(lack) + " on My shift.")
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
         WHERE n.hospital_id = $1 AND n.audience = 'collector'
           AND n.event NOT IN ('escalation', 'still_not_fixed')
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
    speed = await alert_speed(conn, hospital_id, start, end)
    if speed:
        body.append(speed)
    # 039: problems still there 30 minutes after the alert, and how each ended.
    lasted = await conn.fetch("""
        SELECT o.event_key, o.event, o.subject, o.created_at, o.resolved_at, o.resolution
          FROM (SELECT DISTINCT ON (n.event_key) n.* FROM collector_notifications n
                 WHERE n.audience = 'collector' AND n.hospital_id = $1
                   AND n.created_at >= $2 AND n.created_at < $3
                   AND EXISTS (SELECT 1 FROM collector_notifications m
                                WHERE m.event_key = 'still:' || n.event_key || ':1')
                 ORDER BY n.event_key, n.id) o
         ORDER BY o.created_at
    """, hospital_id, start, end)
    if lasted:
        parts = []
        for a in lasted:
            what = ALERT_NAMES.get(a["event"], a["event"])
            if a["resolved_at"]:
                took = round((a["resolved_at"] - a["created_at"]).total_seconds() / 60)
                parts.append(f"{what} {collectors._hhmm(a['created_at'])}, {took} min "
                             f"({a['resolution'] or 'ended'})")
            else:
                parts.append(f"{what} {collectors._hhmm(a['created_at'])}, not fixed")
        body.append(f"Not fixed within 30 min: {len(lasted)} - " + "; ".join(parts))
        amber.append(f"{len(lasted)} problem(s) lasted over 30 min")
        todo.append(f"{len(lasted)} problem(s) lasted over 30 minutes: ask the collector "
                    "what held them up.")

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
        verdict = "⚪ No recordings today" if shift == "day" else "⚪ No recordings in this shift"
    lines.append(verdict)
    lines.append("")
    lines += body
    if todo:
        lines += ["", "TO DO THIS AFTERNOON" if shift == "morning" else "TO DO TOMORROW"]
        lines += [f"{i}. {t}" for i, t in enumerate(todo[:4], 1)]
    return "\n".join(lines)[:3900]


ALERT_NAMES = {
    "no_shift_started": "nobody started the shift", "repeated_stops": "repeated unusual stops",
    "repeated_unusual": "same unusual event 3 times: contact the manager",
    "jabra_unplugged": "Jabra unplugged", "jabra_back": "Jabra back",
    "jabra_not_plugged": "Jabra not plugged in", "recording_problem": "recording problem",
    "pc_silent": "PC not reporting", "recording_without_jabra": "recorded without the Jabra",
    "recording_too_long": "recording not stopped",
    "pc_not_on": "PC not on today", "patients_not_recorded": "patients not recorded",
    "silent_recording": "recording is silent",
    "stopped_unusually": "stopped unusually", "stopped_technical": "stopped: technical problem",
    "recording_paused": "paused by the doctor", "recording_resumed": "resumed",
    "still_paused": "still paused after 2 min",
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
                   delivered, error, channel, delivered_at
              FROM collector_notifications WHERE email = $1 AND audience = 'watcher'
             ORDER BY id DESC LIMIT 50
        """, person.email)
        # 039: who to call at each of their hospitals, right now.
        contacts = []
        for h in hospitals:
            for p in await on_shift(conn, h["hospital_id"]):
                people = p["on_shift"] or p["collectors"]
                contacts.append({
                    "hospital_id": h["hospital_id"], "place": p["place"],
                    "on_shift": bool(p["on_shift"]),
                    "people": [{"name": c.get("official_name") or c.get("display_name")
                                        or c["email"],
                                "code": c.get("collector_code"),
                                "missing": missing_details(c),
                                "email": c["email"], "phone": c.get("phone"),
                                "telegram": c.get("username"),
                                "telegram_linked": c.get("chat_id") is not None,
                                "shift": c.get("shift"),
                                "since": collectors._iso(c.get("started_at"))}
                               for c in people]})
    return {"email": person.email,
            "hospitals": [dict(h) for h in hospitals],
            "contacts": contacts,
            "mode": mode or "everything",
            "report_at": report_time().strftime("%H:%M"),
            "midday_at": midday_time().strftime("%H:%M"),
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
              f"shift summaries at {midday_time():%H:%M} and {report_time():%H:%M}, will come "
              "here.")
    return {"sent": ok, "error": error}


def _day_and_shift(day: Optional[str], shift: Optional[str]) -> Tuple[date, str]:
    try:
        when = date.fromisoformat(day) if day else datetime.now(collectors.zone()).date()
    except ValueError:
        raise HTTPException(status_code=400, detail="day must be YYYY-MM-DD")
    shift = (shift or "day").strip().lower()
    if shift not in SHIFT_NAMES:
        raise HTTPException(status_code=400, detail="shift is morning, evening or day")
    return when, shift


@router.get("/report")
async def report(request: Request, hospital_id: str, day: Optional[str] = None,
                 shift: Optional[str] = None):
    """A hospital's summary for a shift (or the whole day), as it is sent."""
    await _watcher(request)
    when, shift = _day_and_shift(day, shift)
    async with _pool().acquire() as conn:
        if not await conn.fetchval("SELECT 1 FROM hospitals WHERE hospital_id = $1", hospital_id):
            raise HTTPException(status_code=404, detail="no such hospital")
        text = await hospital_report(conn, hospital_id, when, shift)
    return {"hospital_id": hospital_id, "day": when.isoformat(), "shift": shift, "text": text}


@router.get("/summary.xlsx")
async def summary_workbook(request: Request, hospital_id: str, day: Optional[str] = None,
                           shift: Optional[str] = None):
    """20 Nov: one hospital's shift workbook - a researcher's own hospitals only; an
    admin, any."""
    from fastapi.responses import Response
    import workbook
    person = await _watcher(request)
    if not (hospital_id or "").strip():
        raise HTTPException(status_code=400, detail="Choose a hospital for the workbook.")
    when, shift = _day_and_shift(day, shift)
    async with _pool().acquire() as conn:
        if not await conn.fetchval("SELECT 1 FROM hospitals WHERE hospital_id = $1",
                                   hospital_id):
            raise HTTPException(status_code=404, detail="no such hospital")
        if person.role != "admin" and hospital_id not in await watched_hospitals(
                person.email, conn):
            raise HTTPException(status_code=403, detail="you do not look after this hospital")
        name, content = await workbook.build_cached(conn, hospital_id, when, shift)
    await portal._audit("researcher.summary_xlsx", person.email,
                        {"day": when.isoformat(), "shift": shift, "hospital_id": hospital_id})
    return Response(content=content, media_type=workbook.XLSX_TYPE,
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


@router.get("/summary.csv")
async def summary_csv(request: Request, day: Optional[str] = None,
                      shift: Optional[str] = None, hospital_id: Optional[str] = None):
    """The CSV of a shift (or day): one hospital, or every hospital this person looks after."""
    from fastapi.responses import Response
    person = await _watcher(request)
    when, shift = _day_and_shift(day, shift)
    async with _pool().acquire() as conn:
        if hospital_id:
            if not await conn.fetchval("SELECT 1 FROM hospitals WHERE hospital_id = $1",
                                       hospital_id):
                raise HTTPException(status_code=404, detail="no such hospital")
            hospitals = [hospital_id]
        else:
            hospitals = await watched_hospitals(person.email, conn)
        start, end, _ = shift_window(when, shift)
        rows = await summary_rows(conn, hospitals, start, end)
    name = f"aimscribe_{when.isoformat()}_{shift}" + (f"_{hospital_id}" if hospital_id else "")
    await portal._audit("researcher.summary_csv", person.email,
                        {"day": when.isoformat(), "shift": shift, "hospitals": hospitals,
                         "rows": len(rows)})
    return Response(content=csv_text(rows), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{name}.csv"'})


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
        # Taking a hospital off is always allowed: a researcher later made a
        # collector keeps the assignment, and hears nothing for it (18 Nov).
        if role not in ("researcher", "admin") and not body.remove:
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


class DetailsRequest(BaseModel):
    email: str = Field(..., max_length=254)
    official_name: Optional[str] = Field(None, max_length=200)
    collector_code: Optional[str] = Field(None, max_length=60)
    phone: Optional[str] = Field(None, max_length=40)


@admin_router.post("/people/details")
async def set_details(body: DetailsRequest, request: Request,
                      _: None = Depends(api_v2.require_admin)):
    """Correct a person's official name, lab ID or phone (040)."""
    email = body.email.strip().lower()
    by = await portal._actor(request)
    async with _pool().acquire() as conn:
        saved = await save_details(conn, email, body.official_name, body.collector_code,
                                   body.phone)
    await portal._audit("person.details", by, {"email": email, "id": saved["collector_code"]})
    return {"status": "saved", **saved}


class PhoneRequest(BaseModel):
    email: str = Field(..., max_length=254)
    phone: str = Field("", max_length=40)


@admin_router.post("/people/phone")
async def set_phone(body: PhoneRequest, request: Request,
                    _: None = Depends(api_v2.require_admin)):
    """A collector's phone number, for the researchers who may need to call them."""
    email = body.email.strip().lower()
    try:
        phone = clean_phone(body.phone)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    by = await portal._actor(request)
    async with _pool().acquire() as conn:
        done = await conn.fetchval("UPDATE portal_people SET phone = $2 WHERE email = $1 "
                                   "AND removed_at IS NULL RETURNING email", email, phone)
    if done is None:
        raise HTTPException(status_code=404, detail="no such person")
    await portal._audit("person.phone", by, {"email": email, "set": phone is not None})
    return {"status": "saved", "phone": phone}


__all__ = ["router", "admin_router", "watched_hospitals", "watchers_of", "copy_to_watchers",
           "persisting", "repeated_stops", "handled_followups", "contact_text", "on_shift",
           "clean_phone", "clean_name", "clean_code", "save_details", "missing_details",
           "researcher_body", "PROBLEMS_ONLY",
           "daily_reports", "shift_summaries", "shift_window", "summary_rows", "csv_text",
           "summary_csv_for", "quiet_for", "in_shift_hours", "midday_time",
           "hospital_report", "report_time"]
