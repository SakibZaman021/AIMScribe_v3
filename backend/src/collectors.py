"""
Data collectors (migration 036).

A data collector is a third portal role beside admin and researcher. They
sign in with Google like everyone else, and:

    see       the dashboard for their place's hospitals only (`hospital_scope`,
              applied by every dashboard view in dashboard.py)
    never     download, play audio, open the database viewer or an admin page
              (portal.py /auth/check, downloads.py, bulk.py)
    shift     press Start my shift on arrival and End my shift on leaving; each
              press pair is an attendance row an admin downloads as a CSV for
              salaries (`shifts_csv`)
    alerts    while on shift, a Telegram message within about a minute of each
              unusual event at their place (037): the Jabra unplugged or back,
              a new patient recorded without the Jabra, a recording that could
              not start or failed, a PC gone silent. A recording that finished
              normally is shown on the dashboard only - no message. Email
              carries the invitation and the end-of-shift summary, and any
              alert of a collector who has not connected Telegram.

A place is a named group of hospitals with two six-hour shifts. Nothing is
ever deleted: a hospital taken out of a place is marked, a shift left open is
closed by the server and marked 'auto', and every mail is kept.

The mailer runs in one API process at a time (a lease in
collector_notifier_state). Mail goes through Resend's HTTPS API, because
DigitalOcean closes the mail ports; Telegram through its Bot API. A message
that could not be sent is tried again with growing waits for six hours.
"""
from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
import os
import secrets
import time
import uuid
from datetime import date, datetime, time as dtime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

import api_v2
import portal

logger = logging.getLogger(__name__)
# httpx logs every request URL at INFO, and a Telegram URL holds the bot token.
logging.getLogger("httpx").setLevel(logging.WARNING)

router = APIRouter(prefix="/api/v2/dashboard/collector", tags=["collectors"])
admin_router = APIRouter(prefix="/api/v2/admin", tags=["collectors"])

RESEND_URL = "https://api.resend.com/emails"
TICK_SECONDS = 20
LEASE_SECONDS = 90
THROTTLE = timedelta(minutes=10)          # one PC, one kind of event: one mail per 10 min
ESCALATE_AFTER = timedelta(minutes=15)    # an urgent mail nobody marked seen
NO_SHIFT_AFTER = timedelta(minutes=30)    # nobody pressed Start shift
SILENT_AFTER = timedelta(minutes=3)       # three missed heartbeats
AUTO_CLOSE_AFTER = timedelta(hours=1)     # a shift nobody ended
TOO_LONG = timedelta(minutes=60)          # a recording nobody stopped (038)
OTHER_SHIFT = timedelta(hours=6)          # a shift started outside both windows

TELEGRAM_API = "https://api.telegram.org"
# Minutes to wait before each new try of a message that could not be sent.
RETRY_WAITS = (1, 2, 5, 10, 20, 40, 60, 60, 60)
RETRY_FOR = timedelta(hours=6)
# Never faster than the services allow: Resend a few mails a second,
# Telegram about thirty messages a second.
SEND_GAP = {"email": 0.6, "telegram": 0.05}
FAILURES_ALARM = 5                        # failed sends in 30 minutes: tell the lab
LINK_CODE_LASTS = timedelta(days=1)
# Sent by email first (long, read later); everything else by Telegram first.
EMAIL_FIRST = {"shift_summary"}

# Recorder alerts that mean a consultation may be missing or damaged
# (api_v2.RECORDER_CRITICAL): mailed at once, marked urgent.
PROBLEMS = {
    "capture_failed": "the recording could not capture audio",
    "microphone_unavailable": "the microphone could not be opened",
    "start_abandoned": "a recording could not start",
    "disk_full": "the PC's disk is full",
    "local_chain_invalid": "a recording's audio did not check out on the PC",
    "local_segment_unreadable": "a piece of a recording could not be read on the PC",
    "invalid_purge_receipt": "the recorder refused a server receipt",
    "unexpected_agent_exit": "the recorder stopped unexpectedly",
    # 038: two clips in a row of digital silence - muted, or not working.
    "silent_session": "the microphone gives no sound (muted, or not working)",
}

# How a recording ends. Two ends are ordinary and need nobody: the next
# patient came in (superseded_by_new_patient), or the consultation finished
# (the doctor's Stop in CMED or "Consultation finished" on the PC, the
# counselling time after the prescription running out). Every other end is
# told to the collectors on shift and the researchers: a technical one as
# urgent, the rest as information. The recorder's form sends
# "doctor_stopped:<reason>" for anything but "Consultation finished".
ORDINARY_ENDS = {"doctor_stopped", "consultation_finished", "superseded_by_new_patient",
                 "counselling_limit", "prescription_built", "software_update", ""}
TECHNICAL_ENDS = {"technical_problem", "recovered_after_unclean_shutdown", "pc_shutdown",
                  "agent_shutdown"}
STOP_LABELS = {
    "patient_did_not_consent": "the patient did not consent (the recording was deleted)",
    "patient_left": "the patient left the room",
    "urgent_interruption": "an emergency or urgent interruption",
    "technical_problem": "a problem with the recorder or microphone",
    "other": "another reason the doctor gave",
    "recovered_after_unclean_shutdown": "the recorder stopped unexpectedly (crash or power "
                                        "cut); what was recorded was recovered",
    "pc_shutdown": "the PC was shut down during the consultation",
    "agent_shutdown": "the recorder was closed during the consultation",
    "no_activity_limit": "nobody pressed Stop; it ended after the no-activity limit",
}


def stop_kind(reason: Optional[str]) -> Optional[str]:
    """None for an ordinary end; otherwise the reason, without "doctor_stopped:"."""
    text = (reason or "").strip()
    if text in ORDINARY_ENDS:
        return None
    return text.split(":", 1)[1] if text.startswith("doctor_stopped:") else text


def stop_label(reason: Optional[str]) -> str:
    kind = stop_kind(reason)
    if kind is None:
        return "ended normally"
    return STOP_LABELS.get(kind, kind.replace("_", " "))


# How Windows names the Jabra Speak2 55, including the 31-character form.
CLINIC_MIC_WORDS = ("jabra", "echo cancelling speakerphone (j")


# ============================================================ basics

def _pool():
    return api_v2._repo()._pool


def zone() -> ZoneInfo:
    name = os.getenv("TZ", "").strip() or "Asia/Dhaka"
    try:
        return ZoneInfo(name)
    except Exception:
        return ZoneInfo("Asia/Dhaka")


def _local(moment: Optional[datetime]) -> Optional[datetime]:
    if moment is None:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(zone())


def _hhmm(moment: Optional[datetime]) -> str:
    local = _local(moment)
    return local.strftime("%H:%M") if local else ""


def _iso(value: Any) -> Any:
    return value.isoformat() if hasattr(value, "isoformat") else value


def is_clinic_mic(name: Optional[str]) -> bool:
    text = (name or "").lower()
    return any(word in text for word in CLINIC_MIC_WORDS)


def person_label(p: Any) -> str:
    """A collector as the lab names them (040): "Md. Sakib Zaman (DC-07)" - or,
    until they save their details, their Google name or email."""
    name = p.get("official_name") or p.get("display_name") or p.get("email") or ""
    code = p.get("collector_code")
    return f"{name} ({code})" if code else name


def label_sql(alias: str = "p", email: Optional[str] = None) -> str:
    """The same name in SQL, for a portal_people row under this alias."""
    email = email or f"{alias}.email"
    return (f"(COALESCE({alias}.official_name, {alias}.display_name, {email}) "
            f"|| COALESCE(' (' || {alias}.collector_code || ')', ''))")


def microphone_label(names: Optional[List[str]], switches: int = 0) -> Dict[str, Any]:
    """What the Microphone column shows for one recording."""
    names = [n for n in (names or []) if n]
    if not names:
        return {"label": "not reported", "kind": "unknown", "names": [], "switches": switches}
    kinds = list(dict.fromkeys("Jabra" if is_clinic_mic(n) else "PC microphone" for n in names))
    kind = "jabra" if kinds == ["Jabra"] else "pc" if "Jabra" not in kinds else "mixed"
    label = ", then ".join(kinds)
    if switches:
        label += f" ({switches} switch{'es' if switches > 1 else ''})"
    return {"label": label, "kind": kind, "names": names, "switches": switches}


# ============================================================ who sees what

async def place_hospitals(place_id: Optional[int], conn=None) -> List[str]:
    if place_id is None:
        return []
    query = ("SELECT hospital_id FROM place_hospitals WHERE place_id = $1 "
             "AND removed_at IS NULL ORDER BY hospital_id")
    if conn is not None:
        return [r["hospital_id"] for r in await conn.fetch(query, place_id)]
    async with _pool().acquire() as own:
        return [r["hospital_id"] for r in await own.fetch(query, place_id)]


async def hospital_scope(request: Request) -> Optional[List[str]]:
    """
    The hospitals this request may see: None for the admin key, an admin or a
    researcher (everything); the place's hospitals for a data collector (an
    empty list when their place has none yet - they then see nothing).
    """
    if request.headers.get("x-admin-key"):
        return None
    person = await portal.person_for(request)
    if person is None:
        return None
    if person.role == "collector":
        return await place_hospitals(person.place_id)
    if request.query_params.get("mine") == "1":       # 038: "Only my hospitals"
        import watchers
        return await watchers.watched_hospitals(person.email) or None
    return None


def narrow(scope: Any, hospital_id: Optional[str]) -> Optional[List[str]]:
    """A filter for one view: the hospital asked for, within what this person may see."""
    if not isinstance(scope, list):          # called directly, as the tests do: no limit
        scope = None
    if hospital_id:
        if scope is None or hospital_id in scope:
            return [hospital_id]
        return []
    return scope


# ============================================================ shifts

def _minutes(t: dtime) -> int:
    return t.hour * 60 + t.minute


def shift_for(place: Dict[str, Any], local_now: datetime) -> str:
    """Morning, evening or other: the shift whose start is nearest, while it lasts."""
    now = _minutes(local_now.time())
    best, best_gap = "other", None
    for kind in ("morning", "evening"):
        start, end = _minutes(place[f"{kind}_start"]), _minutes(place[f"{kind}_end"])
        if now < end + 60:
            gap = abs(now - start)
            if best_gap is None or gap < best_gap:
                best, best_gap = kind, gap
    return best


def scheduled_end(place: Dict[str, Any], shift: Dict[str, Any]) -> datetime:
    """When this shift should end, in UTC."""
    if shift["shift"] in ("morning", "evening"):
        local = datetime.combine(shift["shift_date"], place[f"{shift['shift']}_end"],
                                 tzinfo=zone())
        return local.astimezone(timezone.utc)
    return shift["started_at"] + OTHER_SHIFT


async def _collector(request: Request, *, write: bool = False) -> portal.Person:
    person = await portal.person_for(request)
    if person is None:
        raise HTTPException(status_code=401, detail="sign in first")
    if person.role != "collector":
        raise HTTPException(status_code=403, detail="this is for data collectors")
    if write:
        portal.check_csrf(request)
    if person.place_id is None:
        raise HTTPException(status_code=409, detail="you have no place yet; ask an admin")
    return person


async def _place(conn, place_id: int) -> Dict[str, Any]:
    row = await conn.fetchrow("SELECT * FROM places WHERE place_id = $1", place_id)
    if row is None:
        raise HTTPException(status_code=404, detail="no such place")
    return dict(row)


def _shift_view(row) -> Optional[Dict[str, Any]]:
    if row is None:
        return None
    return {"id": row["id"], "shift": row["shift"], "date": _iso(row["shift_date"]),
            "started_at": _iso(row["started_at"]), "ended_at": _iso(row["ended_at"]),
            "ended_by": row["ended_by"]}


@router.get("/me")
async def me(request: Request):
    """The collector's place, hospitals, open shift, and their last 50 mails."""
    person = await _collector(request)
    async with _pool().acquire() as conn:
        place = await _place(conn, person.place_id)
        hospitals = await place_hospitals(person.place_id, conn)
        shift = await conn.fetchrow("SELECT * FROM collector_shifts WHERE email = $1 "
                                    "AND ended_at IS NULL", person.email)
        notes = await conn.fetch("""
            SELECT id, created_at, happened_at, event, urgent, subject, hospital_id,
                   delivered, error, seen_at, channel
              FROM collector_notifications WHERE email = $1 ORDER BY id DESC LIMIT 50
        """, person.email)
        chat = await conn.fetchrow("SELECT username, linked_at FROM collector_telegram "
                                   "WHERE email = $1", person.email)
        mine = await conn.fetchrow("SELECT phone, official_name, collector_code "
                                   "FROM portal_people WHERE email = $1", person.email)
    return {"email": person.email, "phone": mine["phone"],
            "official_name": mine["official_name"], "collector_code": mine["collector_code"],
            "place": {"place_id": place["place_id"], "name": place["name"],
                      "morning": [place["morning_start"].strftime("%H:%M"),
                                  place["morning_end"].strftime("%H:%M")],
                      "evening": [place["evening_start"].strftime("%H:%M"),
                                  place["evening_end"].strftime("%H:%M")]},
            "hospitals": hospitals, "shift": _shift_view(shift),
            "mail_ready": mail_status()["ready"], "mail": mail_status(),
            "telegram": {"ready": telegram_ready(), "linked": chat is not None,
                         "username": chat["username"] if chat else None,
                         "linked_at": _iso(chat["linked_at"]) if chat else None},
            "notifications": [{k: _iso(v) for k, v in dict(n).items()} for n in notes]}


@router.post("/shift/start")
async def start_shift(request: Request):
    person = await _collector(request, write=True)
    async with _pool().acquire() as conn:
        open_shift = await conn.fetchrow("SELECT * FROM collector_shifts WHERE email = $1 "
                                         "AND ended_at IS NULL", person.email)
        if open_shift is not None:
            return {"status": "already on shift", "shift": _shift_view(open_shift)}
        place = await _place(conn, person.place_id)
        local = datetime.now(zone())
        row = await conn.fetchrow("""
            INSERT INTO collector_shifts (email, place_id, shift, shift_date)
            VALUES ($1, $2, $3, $4) RETURNING *
        """, person.email, person.place_id, shift_for(place, local), local.date())
    await portal._audit("collector.shift_started", person.email,
                        {"shift": row["shift"], "place_id": person.place_id})
    return {"status": "started", "shift": _shift_view(row)}


@router.post("/shift/end")
async def end_shift(request: Request):
    person = await _collector(request, write=True)
    async with _pool().acquire() as conn:
        row = await conn.fetchrow("""
            UPDATE collector_shifts SET ended_at = now(), ended_by = 'collector'
             WHERE email = $1 AND ended_at IS NULL RETURNING *
        """, person.email)
        if row is None:
            raise HTTPException(status_code=409, detail="you are not on shift")
        await send_shift_summary(conn, dict(row))
    await portal._audit("collector.shift_ended", person.email, {"shift_id": row["id"]})
    return {"status": "ended", "shift": _shift_view(row)}


@router.post("/notifications/{note_id}/seen")
async def mark_seen(note_id: int, request: Request):
    person = await _collector(request, write=True)
    async with _pool().acquire() as conn:
        done = await conn.execute("""
            UPDATE collector_notifications SET seen_at = now()
             WHERE id = $1 AND email = $2 AND seen_at IS NULL
        """, note_id, person.email)
    return {"status": "seen" if done.endswith("1") else "unchanged"}


@router.post("/telegram/link")
async def telegram_link(request: Request):
    """A one-time t.me link: pressing Start in Telegram connects this collector's chat."""
    person = await _collector(request, write=True)
    bot = await telegram_bot()
    if bot is None:
        raise HTTPException(status_code=409, detail=telegram_status_now()["problem"]
                            or "Telegram is not set up on this server yet")
    code = secrets.token_urlsafe(18)
    async with _pool().acquire() as conn:
        await conn.execute("INSERT INTO collector_telegram_codes (code, email) VALUES ($1, $2)",
                           code, person.email)
    return {"url": f"https://t.me/{bot}?start={code}", "bot": bot}


@router.post("/telegram/test")
async def telegram_test(request: Request):
    """One test alert to the collector's own Telegram, with Telegram's exact answer."""
    person = await _collector(request, write=True)
    async with _pool().acquire() as conn:
        chat = await conn.fetchval("SELECT chat_id FROM collector_telegram WHERE email = $1",
                                   person.email)
    if chat is None:
        raise HTTPException(status_code=409, detail="connect Telegram first")
    ok, error = await send_telegram(chat, "Test alert from AIMScribe.\n\nIf your phone "
                                    "made a sound, alerts will reach you during your shift.")
    return {"sent": ok, "error": error}


class OwnPhoneRequest(BaseModel):
    phone: str = Field("", max_length=40)


class DetailsRequest(BaseModel):
    official_name: Optional[str] = Field(None, max_length=200)
    collector_code: Optional[str] = Field(None, max_length=60)
    phone: Optional[str] = Field(None, max_length=40)


@router.post("/details")
async def own_details(body: DetailsRequest, request: Request):
    """The collector's official name, lab ID and phone (040): researchers see them
    with every alert, and the attendance CSV carries them."""
    import watchers
    person = await _collector(request, write=True)
    async with _pool().acquire() as conn:
        saved = await watchers.save_details(conn, person.email, body.official_name,
                                            body.collector_code, body.phone)
    await portal._audit("collector.details", person.email,
                        {"name": bool(saved["official_name"]),
                         "id": saved["collector_code"], "phone": bool(saved["phone"])})
    return {"status": "saved", **saved}


@router.post("/phone")
async def own_phone(body: OwnPhoneRequest, request: Request):
    """The collector's phone number, given to their hospitals' researchers (039)."""
    import watchers
    person = await _collector(request, write=True)
    try:
        phone = watchers.clean_phone(body.phone)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    async with _pool().acquire() as conn:
        await conn.execute("UPDATE portal_people SET phone = $2 WHERE email = $1",
                           person.email, phone)
    await portal._audit("collector.phone", person.email, {"set": phone is not None})
    return {"status": "saved", "phone": phone}


# ============================================================ admin: places

class PlaceRequest(BaseModel):
    place_id: Optional[int] = None
    name: str = Field(..., min_length=1, max_length=100)
    morning_start: str = Field("08:00", pattern=r"^\d{2}:\d{2}$")
    morning_end: str = Field("14:00", pattern=r"^\d{2}:\d{2}$")
    evening_start: str = Field("14:30", pattern=r"^\d{2}:\d{2}$")
    evening_end: str = Field("20:30", pattern=r"^\d{2}:\d{2}$")


class PlaceHospitalRequest(BaseModel):
    hospital_id: str = Field(..., min_length=1, max_length=64)
    remove: bool = False


def _time(text: str) -> dtime:
    try:
        return dtime.fromisoformat(text)
    except ValueError:
        raise HTTPException(status_code=422, detail=f"{text} is not a time (HH:MM)")


@admin_router.get("/places")
async def list_places(_: None = Depends(api_v2.require_admin)):
    async with _pool().acquire() as conn:
        places = await conn.fetch("SELECT * FROM places ORDER BY name")
        links = await conn.fetch("SELECT place_id, hospital_id FROM place_hospitals "
                                 "WHERE removed_at IS NULL ORDER BY hospital_id")
        people = await conn.fetch("""
            SELECT place_id, email, display_name, official_name, collector_code
              FROM portal_people
             WHERE role = 'collector' AND removed_at IS NULL ORDER BY email
        """)
        on_shift = await conn.fetch("SELECT place_id, email, shift, started_at "
                                    "FROM collector_shifts WHERE ended_at IS NULL")
        hospitals = await conn.fetch("SELECT hospital_id, name FROM hospitals ORDER BY hospital_id")
    out = []
    for p in places:
        pid = p["place_id"]
        out.append({
            "place_id": pid, "name": p["name"],
            "morning": [p["morning_start"].strftime("%H:%M"), p["morning_end"].strftime("%H:%M")],
            "evening": [p["evening_start"].strftime("%H:%M"), p["evening_end"].strftime("%H:%M")],
            "hospitals": [r["hospital_id"] for r in links if r["place_id"] == pid],
            "collectors": [{"email": r["email"], "name": person_label(r)}
                           for r in people if r["place_id"] == pid],
            "on_shift": [{"email": r["email"], "shift": r["shift"],
                          "since": _iso(r["started_at"])} for r in on_shift
                         if r["place_id"] == pid]})
    return {"places": out, "hospitals": [dict(h) for h in hospitals],
            "mail_ready": mail_status()["ready"], "mail": mail_status(),
            "telegram": await telegram_status()}


class MailTestRequest(BaseModel):
    to: Optional[str] = Field(None, max_length=254, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


@admin_router.post("/mail-test")
async def mail_test(body: MailTestRequest, request: Request,
                    _: None = Depends(api_v2.require_admin)):
    """One test mail, to the admin or to the address given, with Resend's exact answer."""
    person = await portal.person_for(request)
    to = (body.to or (person.email if person else "")).strip().lower()
    if not to:
        raise HTTPException(status_code=422, detail="give an address to send the test to")
    ok, error = await send_mail(to, "Test mail", "This is a test mail from the AIMScribe "
                                "server. If you can read it, mail to data collectors works.")
    return {"to": to, "sent": ok, "error": error, "mail": mail_status()}


@admin_router.post("/places")
async def save_place(body: PlaceRequest, request: Request,
                     _: None = Depends(api_v2.require_admin)):
    times = [_time(t) for t in (body.morning_start, body.morning_end,
                                body.evening_start, body.evening_end)]
    if not (times[0] < times[1] and times[2] < times[3]):
        raise HTTPException(status_code=422, detail="each shift must end after it starts")
    by = await portal._actor(request)
    async with _pool().acquire() as conn:
        try:
            if body.place_id is None:
                row = await conn.fetchrow("""
                    INSERT INTO places (name, morning_start, morning_end, evening_start,
                                        evening_end, created_by)
                    VALUES ($1, $2, $3, $4, $5, $6) RETURNING place_id
                """, body.name.strip(), *times, by)
            else:
                row = await conn.fetchrow("""
                    UPDATE places SET name = $2, morning_start = $3, morning_end = $4,
                                      evening_start = $5, evening_end = $6
                     WHERE place_id = $1 RETURNING place_id
                """, body.place_id, body.name.strip(), *times)
        except Exception as exc:
            if "unique" in str(exc).lower():
                raise HTTPException(status_code=409, detail="a place with that name exists")
            raise
    if row is None:
        raise HTTPException(status_code=404, detail="no such place")
    await portal._audit("collector.place_saved", by, {"place_id": row["place_id"],
                                                      "name": body.name.strip()})
    return {"status": "saved", "place_id": row["place_id"]}


@admin_router.post("/places/{place_id}/hospitals")
async def place_hospital(place_id: int, body: PlaceHospitalRequest, request: Request,
                         _: None = Depends(api_v2.require_admin)):
    """Put a hospital in a place, or take it out (marked, never deleted)."""
    by = await portal._actor(request)
    async with _pool().acquire() as conn:
        await _place(conn, place_id)
        if not await conn.fetchval("SELECT 1 FROM hospitals WHERE hospital_id = $1",
                                   body.hospital_id):
            raise HTTPException(status_code=404, detail="no such hospital")
        if body.remove:
            await conn.execute("""
                UPDATE place_hospitals SET removed_at = now()
                 WHERE place_id = $1 AND hospital_id = $2 AND removed_at IS NULL
            """, place_id, body.hospital_id)
        else:
            await conn.execute("""
                INSERT INTO place_hospitals (place_id, hospital_id) VALUES ($1, $2)
                ON CONFLICT (place_id, hospital_id) DO UPDATE SET removed_at = NULL
            """, place_id, body.hospital_id)
    await portal._audit("collector.place_hospital", by, {
        "place_id": place_id, "hospital_id": body.hospital_id, "removed": body.remove})
    return {"status": "removed" if body.remove else "added"}


# ============================================================ admin: the shift log

CSV_COLUMNS = ["date", "collector_id", "official_name", "collector", "email", "place",
               "shift", "started", "ended", "hours", "ended_by", "recordings_in_shift",
               "alerts_in_shift"]


def _month(text: Optional[str]) -> Tuple[date, date]:
    if not isinstance(text, str) or not text:
        today = datetime.now(zone()).date()
        text = today.strftime("%Y-%m")
    try:
        first = date.fromisoformat(text + "-01")
    except ValueError:
        raise HTTPException(status_code=400, detail="month must be YYYY-MM")
    nxt = date(first.year + (first.month == 12), first.month % 12 + 1, 1)
    return first, nxt


async def shift_rows(conn, first: date, nxt: date) -> List[Dict[str, Any]]:
    rows = await conn.fetch("""
        SELECT s.*, p.name AS place, pp.display_name, pp.official_name, pp.collector_code,
               (SELECT count(*) FROM sessions x
                 WHERE x.hospital_id IN (SELECT hospital_id FROM place_hospitals h
                                          WHERE h.place_id = s.place_id AND h.removed_at IS NULL)
                   AND x.opened_at >= s.started_at
                   AND x.opened_at < COALESCE(s.ended_at, now())) AS recordings,
               (SELECT count(*) FROM integrity_alerts a JOIN devices d ON d.device_id = a.device_id
                 WHERE d.hospital_id IN (SELECT hospital_id FROM place_hospitals h
                                          WHERE h.place_id = s.place_id AND h.removed_at IS NULL)
                   AND a.raised_at >= s.started_at
                   AND a.raised_at < COALESCE(s.ended_at, now())) AS alerts
          FROM collector_shifts s
          JOIN places p ON p.place_id = s.place_id
          LEFT JOIN portal_people pp ON pp.email = s.email
         WHERE s.shift_date >= $1 AND s.shift_date < $2
         ORDER BY s.shift_date, p.name, s.started_at
    """, first, nxt)
    out = []
    for r in rows:
        end = r["ended_at"]
        hours = round(((end or datetime.now(timezone.utc)) - r["started_at"]).total_seconds()
                      / 3600, 2)
        out.append({"date": r["shift_date"].isoformat(),
                    "collector_id": r["collector_code"] or "",
                    "official_name": r["official_name"] or "",
                    "collector": r["display_name"] or "",
                    "email": r["email"], "place": r["place"], "shift": r["shift"],
                    "started": _hhmm(r["started_at"]), "ended": _hhmm(end),
                    "hours": f"{hours:.2f}", "ended_by": r["ended_by"] or "still on shift",
                    "recordings_in_shift": r["recordings"], "alerts_in_shift": r["alerts"]})
    return out


@admin_router.get("/shifts.csv")
async def shifts_csv(month: Optional[str] = None, _: None = Depends(api_v2.require_admin)):
    """One month of attendance as a CSV for salaries: one row per shift."""
    first, nxt = _month(month)
    async with _pool().acquire() as conn:
        rows = await shift_rows(conn, first, nxt)
    text = io.StringIO()
    writer = csv.DictWriter(text, fieldnames=CSV_COLUMNS, lineterminator="\r\n")
    writer.writeheader()
    writer.writerows(rows)
    # The byte-order mark makes Excel read Bangla names correctly.
    return Response("﻿" + text.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition":
                             f'attachment; filename="shift-log-{first:%Y-%m}.csv"',
                             "Cache-Control": "no-store"})


# ============================================================ mail

def mail_ready() -> bool:
    return bool(os.getenv("AIMS_RESEND_API_KEY", "").strip())


def mail_status() -> Dict[str, Any]:
    """Whether mail can reach people, and if not, exactly what to set on the server."""
    if not mail_ready():
        return {"ready": False, "sender": None,
                "problem": "The server has no Resend key, so no email is sent. On the server, "
                           "in deploy/uiu, run ./set-alerts.sh (or set AIMS_RESEND_API_KEY in "
                           ".env), then: docker compose up -d --no-deps api"}
    sender = _sender()
    if "resend.dev" in sender.lower():
        return {"ready": False, "sender": sender,
                "problem": "Email goes only to the Resend account's own address. To reach "
                           "everyone, verify a domain in Resend and set AIMS_COLLECTOR_MAIL_FROM "
                           "(e.g. AIMScribe <alerts@your-domain>) in .env, then: "
                           "docker compose up -d --no-deps api"}
    return {"ready": True, "sender": sender, "problem": None}


def _sender() -> str:
    return (os.getenv("AIMS_COLLECTOR_MAIL_FROM", "").strip()
            or os.getenv("AIMS_ALERT_FROM", "").strip()
            or "AIMScribe server <onboarding@resend.dev>")


def lab_addresses() -> List[str]:
    return [a.strip() for a in os.getenv("AIMS_ALERT_EMAIL", "").split(",") if a.strip()]


async def _pace(channel: str) -> None:
    """Space sends out so neither service is ever asked too fast."""
    now = time.monotonic()
    slot = max(now, _NEXT_SEND.get(channel, 0.0))
    _NEXT_SEND[channel] = slot + SEND_GAP[channel]
    if slot > now:
        await asyncio.sleep(slot - now)


_NEXT_SEND: Dict[str, float] = {}

# Telegram asks a bot to send one chat at most about one message a second, and
# refuses for a while past that. When many alerts arrive at once (a clinic's
# internet coming back), a researcher who looks after every hospital would get
# several a second: so each chat is spaced on its own as well (load test, 16 Nov).
CHAT_GAP = 1.05
_NEXT_TO_CHAT: Dict[int, float] = {}


async def _pace_chat(chat_id: int) -> None:
    now = time.monotonic()
    slot = max(now, _NEXT_TO_CHAT.get(chat_id, 0.0))
    _NEXT_TO_CHAT[chat_id] = slot + CHAT_GAP
    if len(_NEXT_TO_CHAT) > 5000:                  # forget chats long quiet
        for old in [c for c, t in _NEXT_TO_CHAT.items() if t < now]:
            _NEXT_TO_CHAT.pop(old, None)
    if slot > now:
        await asyncio.sleep(slot - now)


async def send_mail(to: str, subject: str, text: str) -> Tuple[bool, Optional[str]]:
    """One mail through Resend. Never raises: the reason is kept with the mail."""
    key = os.getenv("AIMS_RESEND_API_KEY", "").strip()
    if not key:
        return False, "mail is not set up on this server (AIMS_RESEND_API_KEY)"
    message = {"from": _sender(), "to": [to], "subject": f"[AIMScribe] {subject}"[:200],
               "text": text}
    if lab_addresses():
        message["reply_to"] = lab_addresses()      # replies reach AIMS LAB, not a dead address
    await _pace("email")
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            reply = await client.post(RESEND_URL, headers={"Authorization": f"Bearer {key}"},
                                      json=message)
        if reply.status_code in (200, 201, 202):
            return True, None
        return False, f"Resend answered {reply.status_code}: {reply.text[:300]}"
    except Exception as exc:
        return False, f"Resend could not be reached: {exc}"[:500]


# ------------------------------------------------------------ Telegram

def _token() -> str:
    return os.getenv("AIMS_TELEGRAM_BOT_TOKEN", "").strip()


def telegram_ready() -> bool:
    return bool(_token())


def _hide(text: str) -> str:
    token = _token()
    return text.replace(token, "<token>") if token else text


async def _telegram(method: str, payload: Dict[str, Any],
                    timeout: float = 15) -> Tuple[Optional[Any], Optional[str]]:
    """One Bot API call: (result, None) or (None, why). Never raises; never shows the token."""
    token = _token()
    if not token:
        return None, ("Telegram is not set up on this server (AIMS_TELEGRAM_BOT_TOKEN): "
                      "run ./set-telegram.sh in deploy/uiu")
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            reply = await client.post(f"{TELEGRAM_API}/bot{token}/{method}", json=payload)
        try:
            body = reply.json()
        except ValueError:
            body = {}
        if reply.status_code == 200 and body.get("ok"):
            return body.get("result"), None
        return None, _hide(f"Telegram answered {reply.status_code}: "
                           f"{body.get('description') or reply.text[:200]}")[:500]
    except Exception as exc:
        return None, _hide(f"Telegram could not be reached: {type(exc).__name__} {exc}")[:500]


_BOT: Dict[str, Optional[str]] = {"token": None, "username": None}


async def telegram_bot() -> Optional[str]:
    """The bot's @username (asked once per token), or None when Telegram is not working."""
    token = _token()
    if not token:
        return None
    if _BOT["token"] == token and _BOT["username"]:
        return _BOT["username"]
    configured = os.getenv("AIMS_TELEGRAM_BOT_USERNAME", "").strip().lstrip("@")
    result, error = await _telegram("getMe", {})
    if result and result.get("username"):
        _BOT.update(token=token, username=result["username"])
        return result["username"]
    _BOT.update(token=token, username=None)
    _BOT["problem"] = error
    return configured or None


def telegram_status_now() -> Dict[str, Any]:
    if not telegram_ready():
        return {"ready": False, "bot": None,
                "problem": "Telegram is not set up on this server. On the server, in "
                           "deploy/uiu, run ./set-telegram.sh with the token from @BotFather."}
    if _BOT.get("username") and _BOT.get("token") == _token():
        return {"ready": True, "bot": _BOT["username"], "problem": None}
    return {"ready": False, "bot": None,
            "problem": f"Telegram refused the bot token: {_BOT.get('problem') or 'unknown'}. "
                       "Run ./set-telegram.sh again with the token from @BotFather."}


async def telegram_status() -> Dict[str, Any]:
    await telegram_bot()
    return telegram_status_now()


def _telegram_text(subject: str, text: str, urgent: bool) -> str:
    return (("\U0001F534 " if urgent else "") + subject + "\n\n" + text)[:4000]


async def send_telegram(chat_id: int, text: str,
                        note_id: Optional[int] = None) -> Tuple[bool, Optional[str]]:
    """One message to one chat; an urgent one carries a Seen button. Never raises."""
    payload: Dict[str, Any] = {"chat_id": chat_id, "text": text[:4000],
                               "disable_web_page_preview": True}
    if note_id is not None:
        payload["reply_markup"] = {"inline_keyboard": [[
            {"text": "\u2705 Seen - I am on it", "callback_data": f"seen:{note_id}"}]]}
    await _pace_chat(chat_id)
    await _pace("telegram")
    result, error = await _telegram("sendMessage", payload)
    return (True, None) if result is not None else (False, error)


async def _telegram_upload(method: str, fields: Dict[str, Any], filename: str,
                           content: bytes, timeout: float = 60) -> Tuple[Optional[Any],
                                                                      Optional[str]]:
    """One Bot API call carrying a file (sendDocument). Never raises; never shows the token."""
    token = _token()
    if not token:
        return None, "Telegram is not set up on this server (AIMS_TELEGRAM_BOT_TOKEN)"
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            reply = await client.post(
                f"{TELEGRAM_API}/bot{token}/{method}",
                data={k: str(v) for k, v in fields.items()},
                files={"document": (filename, content, "text/csv")})
        try:
            body = reply.json()
        except ValueError:
            body = {}
        if reply.status_code == 200 and body.get("ok"):
            return body.get("result"), None
        return None, _hide(f"Telegram answered {reply.status_code}: "
                           f"{body.get('description') or reply.text[:200]}")[:500]
    except Exception as exc:
        return None, _hide(f"Telegram could not be reached: {type(exc).__name__} {exc}")[:500]


async def send_telegram_file(chat_id: int, filename: str, content: bytes,
                             caption: str) -> Tuple[bool, Optional[str]]:
    await _pace_chat(chat_id)
    await _pace("telegram")
    result, error = await _telegram_upload("sendDocument", {"chat_id": chat_id,
                                                            "caption": caption[:1000]},
                                           filename, content)
    return (True, None) if result is not None else (False, error)


async def _summary_file(conn, note: Dict[str, Any]) -> Tuple[str, bytes]:
    """The researcher's shift CSV, built when it is sent (and again if it is retried)."""
    import watchers
    _, day, shift = note["event_key"].split(":", 2)
    return await watchers.summary_csv_for(conn, note["email"], date.fromisoformat(day), shift)


# ------------------------------------------------------------ one alert, any channel

def _transient(error: Optional[str]) -> bool:
    """Worth trying again: the service was busy, down or unreachable."""
    text = (error or "").lower()
    return ("could not be reached" in text or "answered 429" in text
            or "answered 5" in text)


async def _send_to(conn, note: Dict[str, Any]) -> Tuple[bool, Optional[str], Optional[str]]:
    """Telegram first (email first for a summary); the other if the first fails."""
    chat = await conn.fetchval("SELECT chat_id FROM collector_telegram WHERE email = $1",
                               note["email"])
    order = ["email", "telegram"] if note["event"] in EMAIL_FIRST else ["telegram", "email"]
    errors: List[Tuple[str, str]] = []
    for channel in order:
        if channel == "telegram":
            if chat is None or not telegram_ready():
                continue
            if note["event"] == "summary_csv":
                filename, content = await _summary_file(conn, note)
                ok, error = await send_telegram_file(
                    chat, filename, content, f"{note['subject']}\n\n{note['body']}")
                if ok:
                    return True, None, channel
                errors.append((channel, error or "not sent"))
                continue
            ok, error = await send_telegram(
                chat, _telegram_text(note["subject"], note["body"], note["urgent"]),
                # Only the collector, who must act, gets the Seen button.
                note_id=note["id"] if note["urgent"] and note["audience"] == "collector"
                else None)
        else:
            ok, error = await send_mail(note["email"], note["subject"], note["body"])
        if ok:
            return True, None, channel
        errors.append((channel, error or "not sent"))
    if not errors:
        return False, "no way to reach this person", None
    return False, "; ".join(f"{c}: {e}" for c, e in errors)[:500], errors[0][0]


async def _deliver(conn, note_id: int, to: str = "", subject: str = "", text: str = "") -> bool:
    """Send one kept alert and keep how it went; an alert that failed for a passing reason
    gets its next try."""
    note = await conn.fetchrow("SELECT id, email, event, urgent, subject, body, audience, "
                               "event_key "
                               "FROM collector_notifications WHERE id = $1", note_id)
    if note is None:
        return False
    ok, error, channel = await _send_to(conn, dict(note))
    attempts = await conn.fetchval("""
        UPDATE collector_notifications
           SET delivered = $2, error = $3, channel = COALESCE($4, channel),
               attempts = attempts + 1, next_try_at = NULL
         WHERE id = $1 RETURNING attempts
    """, note_id, ok, error, channel)
    if not ok and _transient(error) and attempts <= len(RETRY_WAITS):
        await conn.execute("UPDATE collector_notifications SET next_try_at = now() + $2::interval "
                           "WHERE id = $1", note_id,
                           timedelta(minutes=RETRY_WAITS[attempts - 1]))
    return ok


async def _hold(conn, note_id: int) -> None:
    """Quiet hours: the alert is kept for the summary and the dashboard, and not sent."""
    await conn.execute("UPDATE collector_notifications SET channel = 'held', delivered = FALSE, "
                       "error = NULL, next_try_at = NULL WHERE id = $1", note_id)


async def _record(conn, *, email: str, event: str, subject: str, body: str, event_key: str,
                  urgent: bool = False, place_id: Optional[int] = None,
                  hospital_id: Optional[str] = None, device_id=None,
                  session_id: Optional[str] = None,
                  happened_at: Optional[datetime] = None,
                  audience: str = "collector") -> Optional[int]:
    """Keep the alert, once per person and event; returns its id, or None if already kept."""
    return await conn.fetchval("""
        INSERT INTO collector_notifications
            (email, place_id, hospital_id, device_id, session_id, event, urgent, subject,
             body, happened_at, event_key, audience)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
        ON CONFLICT (email, event_key) DO NOTHING
        RETURNING id
    """, email, place_id, hospital_id, device_id, session_id, event, urgent, subject[:300],
        body, happened_at, event_key[:200], audience)


async def places_of(conn, hospital_id: Optional[str]) -> List[int]:
    if not hospital_id:
        return []
    rows = await conn.fetch("SELECT place_id FROM place_hospitals WHERE hospital_id = $1 "
                            "AND removed_at IS NULL", hospital_id)
    return [r["place_id"] for r in rows]


async def on_duty(conn, place_id: int) -> List[Dict[str, Any]]:
    rows = await conn.fetch("""
        SELECT s.email, s.started_at FROM collector_shifts s
          JOIN portal_people p ON p.email = s.email AND p.removed_at IS NULL
         WHERE s.place_id = $1 AND s.ended_at IS NULL
    """, place_id)
    return [dict(r) for r in rows]


async def collectors_of(conn, place_id: int) -> List[str]:
    rows = await conn.fetch("SELECT email FROM portal_people WHERE role = 'collector' "
                            "AND place_id = $1 AND removed_at IS NULL", place_id)
    return [r["email"] for r in rows]


async def consultation_context(conn, *, session_id: Optional[str] = None, device_id=None,
                               hospital_id: Optional[str] = None) -> Tuple[Optional[str], str]:
    """
    Which doctor, which PC, which hospital and which consultation an alert is
    about - so a collector knows which room to go to when two doctors share a
    clinic. Returns (the doctor's name or ID for the subject, the lines).
    No patient details: Telegram keeps its messages on its own servers.
    """
    s = None
    explicit = False
    if session_id:
        s = await conn.fetchrow("SELECT doctor_id, hospital_id, device_id, opened_at, closed_at "
                                "FROM sessions WHERE session_id = $1", session_id)
        explicit = s is not None
    if s is None and device_id is not None:
        s = await conn.fetchrow("""
            SELECT doctor_id, hospital_id, device_id, opened_at, closed_at FROM sessions
             WHERE device_id = $1 AND opened_at > now() - interval '12 hours'
             ORDER BY opened_at DESC LIMIT 1
        """, device_id)
    dev_id = (s["device_id"] if s else None) or device_id
    dev = await conn.fetchrow("SELECT machine_name, hospital_id, doctor_id FROM devices "
                              "WHERE device_id = $1", dev_id) if dev_id is not None else None
    hid = (s["hospital_id"] if s else None) or (dev["hospital_id"] if dev else None) or hospital_id
    doctor_id = (s["doctor_id"] if s else None) or (dev["doctor_id"] if dev else None)
    name = await conn.fetchval("SELECT full_name FROM doctors WHERE doctor_id = $1 "
                               "AND hospital_id = $2", doctor_id, hid) \
        if doctor_id and hid else None
    hospital = await conn.fetchval("SELECT name FROM hospitals WHERE hospital_id = $1",
                                   hid) if hid else None
    lines = []
    if doctor_id:
        lines.append(f"Doctor: {name} ({doctor_id})" if name else f"Doctor: {doctor_id}")
    if dev is not None:
        lines.append(f"PC: {dev['machine_name'] or 'unnamed'}")
    if hid:
        lines.append(f"Hospital: {hid}" + (f" {hospital}" if hospital else ""))
    if s is not None and s["opened_at"]:
        what = "Consultation" if explicit else "Last consultation on this PC"
        if s["closed_at"]:
            minutes = round((s["closed_at"] - s["opened_at"]).total_seconds() / 60)
            lines.append(f"{what}: {_hhmm(s['opened_at'])} to {_hhmm(s['closed_at'])} "
                         f"({minutes} min)")
        else:
            minutes = round((datetime.now(timezone.utc) - s["opened_at"]).total_seconds() / 60)
            lines.append(f"{what}: started {_hhmm(s['opened_at'])}, {minutes} min so far"
                         if explicit else
                         f"{what}: started {_hhmm(s['opened_at'])}, not ended yet")
    return (name or doctor_id), "\n".join(lines)


async def notify(conn, *, hospital_id: Optional[str], event: str, subject: str, body: str,
                 event_key: str, urgent: bool = False, everyone_if_nobody: bool = False,
                 device_id=None, session_id: Optional[str] = None,
                 happened_at: Optional[datetime] = None,
                 not_before: Optional[datetime] = None) -> int:
    """
    Mail an event to the collector(s) on shift at every place holding this
    hospital - or, for what must not wait, to all the place's collectors when
    nobody is on shift. Returns how many mails were sent.
    """
    if device_id is not None and event in THROTTLED:
        recent = await conn.fetchval("""
            SELECT 1 FROM collector_notifications
             WHERE device_id = $1 AND event = $2 AND created_at > now() - $3::interval
             LIMIT 1
        """, device_id, event, THROTTLE)
        if recent:
            return 0
    # Which doctor, PC and consultation: in the subject and above the footer.
    doctor, context = await consultation_context(conn, session_id=session_id,
                                                 device_id=device_id, hospital_id=hospital_id)
    if doctor:
        subject = f"{subject} - {doctor}"
    if context:
        main, mark, foot = body.partition(_FOOTER_MARK)
        body = main.rstrip() + "\n\n" + context + (mark + foot if mark else "")
    import watchers
    # 16 Nov: from the evening summary until the morning shift, nobody is
    # messaged - it is kept, marked held, and is in the next summary.
    quiet = await watchers.quiet_for(conn, hospital_id)
    sent = 0
    told: List[str] = []
    on_shift = False
    for place_id in await places_of(conn, hospital_id):
        duty = await on_duty(conn, place_id)
        if not_before is not None:
            duty = [d for d in duty if d["started_at"] <= not_before]
        recipients = [d["email"] for d in duty]
        on_shift = on_shift or bool(recipients)
        if not recipients and everyone_if_nobody:
            recipients = await collectors_of(conn, place_id)
        for email in recipients:
            note_id = await _record(conn, email=email, event=event, subject=subject, body=body,
                                    event_key=event_key, urgent=urgent, place_id=place_id,
                                    hospital_id=hospital_id, device_id=device_id,
                                    session_id=session_id, happened_at=happened_at)
            if note_id is not None:
                told.append(email)
                if quiet:
                    await _hold(conn, note_id)
                    continue
                await _deliver(conn, note_id, email, subject, body)
                sent += 1
    # 038/039: the researchers who look after this hospital get it too, written
    # for supervision: what the collector was asked to do, and the collector on
    # shift - or, with nobody on shift, all the place's collectors - with phone
    # and Telegram, so they can call them.
    contacts = await watchers.contact_text(conn, hospital_id)
    sent += await watchers.copy_to_watchers(
        conn, hospital_id=hospital_id, event=event,
        subject=(subject if on_shift else f"Nobody on shift: {subject}"),
        body=watchers.researcher_body(body, contacts), event_key=event_key,
        urgent=urgent, device_id=device_id, session_id=session_id, happened_at=happened_at,
        hold=quiet)
    return sent


THROTTLED = {"jabra_unplugged", "jabra_back", "jabra_not_plugged", "recording_problem",
             "recording_without_jabra"}


_FOOTER_MARK = "\n\nOpen "


def _footer() -> str:
    host = os.getenv("AIMS_PUBLIC_HOST", "").strip()
    link = f"https://{host}/dashboard" if host else "the AIMScribe dashboard"
    return (f"\n\nOpen {link} to see the details and mark this as seen.\n"
            "You get this because you are on shift. - AIMScribe")


# ============================================================ the shift summary

async def send_shift_summary(conn, shift: Dict[str, Any]) -> None:
    """One mail when a shift ends: every recording, its microphone, every interruption."""
    if shift.get("summary_sent_at"):
        return
    place = await _place(conn, shift["place_id"])
    hospitals = await place_hospitals(shift["place_id"], conn)
    end = shift["ended_at"] or datetime.now(timezone.utc)
    recordings = await conn.fetch("""
        SELECT s.session_id, s.hospital_id, s.doctor_id, s.opened_at, s.closed_at,
               s.total_duration_seconds, s.microphones, s.quarantine_reason, s.close_reason,
               (SELECT count(*) FROM integrity_alerts a WHERE a.session_id = s.session_id
                  AND a.alert_type = 'microphone_switched') AS switches
          FROM sessions s
         WHERE s.hospital_id = ANY($1::text[]) AND s.opened_at >= $2 AND s.opened_at < $3
         ORDER BY s.opened_at
    """, hospitals, shift["started_at"], end)
    alerts = await conn.fetch("""
        SELECT a.alert_type, count(*) AS n FROM integrity_alerts a
          JOIN devices d ON d.device_id = a.device_id
         WHERE d.hospital_id = ANY($1::text[]) AND a.raised_at >= $2 AND a.raised_at < $3
         GROUP BY a.alert_type ORDER BY n DESC
    """, hospitals, shift["started_at"], end)
    on_jabra = sum(1 for r in recordings
                   if microphone_label(r["microphones"], r["switches"])["kind"] == "jabra")
    lines = [f"Shift summary: {place['name']}, {shift['shift']} shift, "
             f"{_local(shift['started_at']):%d %b %Y}",
             f"From {_hhmm(shift['started_at'])} to {_hhmm(end)}.",
             "",
             f"Recordings: {len(recordings)} ({on_jabra} on the Jabra only).",
             f"Alerts: {sum(a['n'] for a in alerts)}"
             + (" - " + ", ".join(f"{a['alert_type']} {a['n']}" for a in alerts) if alerts else ""),
             ""]
    for r in recordings:
        mic = microphone_label(r["microphones"], r["switches"])["label"]
        minutes = round(float(r["total_duration_seconds"] or 0) / 60, 1)
        state = "quarantined" if r["quarantine_reason"] else \
            ("still open" if not r["closed_at"] else
             "finished" if stop_kind(r["close_reason"]) is None else
             "stopped: " + stop_label(r["close_reason"]))
        lines.append(f"{_hhmm(r['opened_at'])}  {r['hospital_id']}  doctor {r['doctor_id']}  "
                     f"{minutes} min  {mic}  {state}")
    body = "\n".join(lines) + "\n\n- AIMScribe"
    subject = f"Shift summary: {place['name']}, {len(recordings)} recordings"
    note_id = await _record(conn, email=shift["email"], event="shift_summary", subject=subject,
                            body=body, event_key=f"summary:{shift['id']}",
                            place_id=shift["place_id"])
    if note_id is not None:
        import watchers
        if shift.get("ended_by") == "auto" and await watchers.quiet_for(
                conn, place_id=shift["place_id"]):
            await _hold(conn, note_id)            # night: on My shift, not in the inbox
        else:
            await _deliver(conn, note_id, shift["email"], subject, body)
    await conn.execute("UPDATE collector_shifts SET summary_sent_at = now() WHERE id = $1",
                       shift["id"])


# ============================================================ the mailer's rounds

class Notifier:
    """One round every TICK_SECONDS, in whichever API process holds the lease."""

    def __init__(self) -> None:
        self.owner = f"{os.getpid()}-{uuid.uuid4().hex[:8]}"
        self.mic_state: Dict[Any, bool] = {}
        self.task: Optional[asyncio.Task] = None

    async def lease(self, conn) -> bool:
        await conn.execute("INSERT INTO collector_notifier_state (name) VALUES ('lease') "
                           "ON CONFLICT (name) DO NOTHING")
        got = await conn.fetchval("""
            UPDATE collector_notifier_state SET owner = $1, updated_at = now()
             WHERE name = 'lease'
               AND (owner IS NULL OR owner = $1 OR updated_at < now() - $2::interval)
            RETURNING 1
        """, self.owner, timedelta(seconds=LEASE_SECONDS))
        return bool(got)

    async def keep_lease(self, conn) -> None:
        await conn.execute("UPDATE collector_notifier_state SET updated_at = now() "
                           "WHERE name = 'lease' AND owner = $1", self.owner)

    async def run(self) -> None:
        while True:
            try:
                async with _pool().acquire() as conn:
                    if await self.lease(conn):
                        await self.round(conn)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("The collector mailer's round failed: %s", exc)
            await asyncio.sleep(TICK_SECONDS)

    async def round(self, conn) -> None:
        import watchers
        for step in (self.telegram_updates, self.close_forgotten_shifts, self.alerts,
                     self.idle_microphones, self.recordings_without_jabra,
                     self.recordings_too_long, self.refusals, self.silent_pcs,
                     self.no_shift_started,
                     self.escalate, watchers.handled_followups, watchers.persisting,
                     watchers.repeated_stops, watchers.shift_summaries,
                     self.retry_failed, self.failure_alarm):
            try:
                await self.keep_lease(conn)
                await step(conn)
            except Exception as exc:
                logger.warning("Collector mailer step %s failed: %s", step.__name__, exc)

    # -- a shift nobody ended
    async def close_forgotten_shifts(self, conn) -> None:
        rows = await conn.fetch("SELECT * FROM collector_shifts WHERE ended_at IS NULL")
        now = datetime.now(timezone.utc)
        for row in rows:
            place = await _place(conn, row["place_id"])
            end = scheduled_end(place, dict(row))
            if now >= end + AUTO_CLOSE_AFTER:
                closed = await conn.fetchrow("""
                    UPDATE collector_shifts SET ended_at = $2, ended_by = 'auto'
                     WHERE id = $1 AND ended_at IS NULL RETURNING *
                """, row["id"], max(end, row["started_at"]))
                if closed is not None:
                    await send_shift_summary(conn, dict(closed))

    # -- what the recorders reported
    async def alerts(self, conn) -> None:
        await conn.execute("INSERT INTO collector_notifier_state (name, last_id) "
                           "SELECT 'alerts', COALESCE(max(id), 0) FROM integrity_alerts "
                           "ON CONFLICT (name) DO NOTHING")
        last = await conn.fetchval("SELECT last_id FROM collector_notifier_state "
                                   "WHERE name = 'alerts'")
        rows = await conn.fetch("""
            SELECT a.id, a.raised_at, a.alert_type, a.session_id, a.device_id, a.detail,
                   COALESCE(d.hospital_id, s.hospital_id) AS hospital_id, d.machine_name,
                   s.doctor_id
              FROM integrity_alerts a
              LEFT JOIN devices d ON d.device_id = a.device_id
              LEFT JOIN sessions s ON s.session_id = a.session_id
             WHERE a.id > $1 ORDER BY a.id LIMIT 200
        """, last)
        for row in rows:
            await self.keep_lease(conn)
            await self.one_alert(conn, row)
            await conn.execute("UPDATE collector_notifier_state SET last_id = $1, "
                               "updated_at = now() WHERE name = 'alerts'", row["id"])

    async def one_alert(self, conn, row) -> None:
        kind = row["alert_type"]
        detail = row["detail"]
        if isinstance(detail, str):
            try:
                detail = json.loads(detail)
            except ValueError:
                detail = {}
        detail = detail or {}
        happened = row["raised_at"]
        try:
            if detail.get("occurred_at"):
                happened = datetime.fromisoformat(str(detail["occurred_at"]))
        except ValueError:
            pass
        pc = row["machine_name"] or "a PC"
        where = f"{row['hospital_id'] or 'unknown hospital'}, {pc}"
        late = ""
        if detail.get("reported_late"):
            late = f" (happened {_hhmm(happened)}, reported {_hhmm(row['raised_at'])})"
        if kind == "microphone_switched":
            text = str(detail.get("detail") or "")
            to = detail.get("microphone")
            back = is_clinic_mic(to) if to else "plugged back in" in text
            if back:
                event, urgent = "jabra_back", False
                subject = f"Jabra back in use: {where}"
                body = (f"The Jabra is recording again at {where}, from {_hhmm(happened)}"
                        f"{late}.\nNothing more to do.")
            else:
                event, urgent = "jabra_unplugged", True
                subject = f"Jabra unplugged: {where}"
                body = (f"At {_hhmm(happened)}{late} the recording at {where} moved off the "
                        f"Jabra" + (f" to {to}" if to else f": {text}" if text else "") + "."
                        "\n\nPlease go to the room and plug the Jabra back in. "
                        "The recording carries on meanwhile.")
        elif kind == "abnormal_close":
            reason = detail.get("reason")
            stopped = stop_kind(reason)
            if stopped is None:
                return                       # an ordinary end the server also flagged
            technical = stopped in TECHNICAL_ENDS
            event = "stopped_technical" if technical else "stopped_unusually"
            urgent = technical
            minutes = round(float(detail.get("duration_seconds") or 0) / 60)
            subject = f"Recording stopped: {stop_label(reason)}"
            body = (f"At {_hhmm(happened)}{late} the recording at {where} was stopped, after "
                    f"{minutes} min, because {stop_label(reason)}."
                    + ("\n\nPlease check the PC and the Jabra before the next patient, and "
                       "call AIMS LAB if it happens again." if technical else
                       "\n\nNothing to do now; it is kept for the review."))
        elif kind in PROBLEMS:
            event, urgent = "recording_problem", True
            subject = f"Recording problem: {where}"
            body = (f"At {_hhmm(happened)}{late}, {PROBLEMS[kind]} at {where}.\n"
                    f"{detail.get('detail') or ''}\n\nPlease check the PC and the Jabra, "
                    "and call AIMS LAB if it does not recover.")
        else:
            return
        await notify(conn, hospital_id=row["hospital_id"], event=event, subject=subject,
                     body=body + _footer(), event_key=f"alert:{row['id']}", urgent=urgent,
                     everyone_if_nobody=urgent, device_id=row["device_id"],
                     session_id=row["session_id"], happened_at=happened)

    # -- the Jabra unplugged while the PC waits for the next patient
    async def idle_microphones(self, conn) -> None:
        rows = await conn.fetch("""
            SELECT d.device_id, d.hospital_id, d.machine_name, d.microphone,
                   d.microphone_is_clinic,
                   EXISTS (SELECT 1 FROM sessions s WHERE s.device_id = d.device_id
                             AND s.closed_at IS NULL
                             AND s.opened_at > now() - interval '3 hours') AS recording
              FROM devices d
             WHERE d.revoked_at IS NULL AND d.microphone_is_clinic IS NOT NULL
               AND d.last_seen_at > now() - $1::interval
        """, SILENT_AFTER)
        for row in rows:
            now_clinic = bool(row["microphone_is_clinic"])
            before = self.mic_state.get(row["device_id"])
            self.mic_state[row["device_id"]] = now_clinic
            if before is None or before == now_clinic or row["recording"]:
                continue                     # first sight, no change, or the alert covers it
            where = f"{row['hospital_id']}, {row['machine_name'] or 'a PC'}"
            stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M")
            if now_clinic:
                event, subject = "jabra_back", f"Jabra plugged in: {where}"
                body = f"The Jabra is connected again at {where}. Nothing more to do."
            else:
                event, subject = "jabra_not_plugged", f"Jabra not plugged in: {where}"
                body = (f"The PC at {where} is waiting for the next patient on "
                        f"{row['microphone'] or 'its own microphone'}, not the Jabra.\n\n"
                        "Please plug the Jabra in before the next consultation.")
            await notify(conn, hospital_id=row["hospital_id"], event=event, subject=subject,
                         body=body + _footer(), event_key=f"idle:{row['device_id']}:{stamp}",
                         urgent=not now_clinic, everyone_if_nobody=False,
                         device_id=row["device_id"])

    # -- a PC that stopped reporting during a shift
    async def silent_pcs(self, conn) -> None:
        rows = await conn.fetch("""
            SELECT device_id, hospital_id, machine_name, last_seen_at FROM devices
             WHERE revoked_at IS NULL
               AND last_seen_at < now() - $1::interval
               AND last_seen_at > now() - interval '30 minutes'
        """, SILENT_AFTER)
        import watchers
        for row in rows:
            # 16 Nov: a PC switched off after the shift is the day ending, not news.
            if not await watchers.in_shift_hours(conn, row["hospital_id"]):
                continue
            where = f"{row['hospital_id']}, {row['machine_name'] or 'a PC'}"
            await notify(conn, hospital_id=row["hospital_id"], event="pc_silent",
                         subject=f"PC not reporting: {where}",
                         body=(f"The PC at {where} has not reported since "
                               f"{_hhmm(row['last_seen_at'])}. It may be off, asleep, or "
                               "without internet. Please check it: recordings are kept on "
                               "the PC and sent when it is back.") + _footer(),
                         event_key=f"silent:{row['device_id']}:{row['last_seen_at'].isoformat()}",
                         urgent=True, everyone_if_nobody=False, device_id=row["device_id"],
                         not_before=row["last_seen_at"])

    # -- a new patient recorded while the Jabra is still unplugged
    async def recordings_without_jabra(self, conn) -> None:
        """
        The doctor unplugs the Jabra for a sensitive patient and does not plug it
        back: every later consultation is on the laptop's microphone, and nothing
        changes that would raise an alert. A recording that started (and has been
        heard from since) on a non-Jabra microphone, with no switch inside it, is
        that case.
        """
        rows = await conn.fetch("""
            SELECT s.session_id, s.opened_at, s.hospital_id, s.device_id,
                   d.machine_name, d.microphone
              FROM sessions s JOIN devices d ON d.device_id = s.device_id
             WHERE s.closed_at IS NULL AND s.opened_at > now() - interval '3 hours'
               AND d.revoked_at IS NULL AND d.microphone_is_clinic = FALSE
               AND d.last_seen_at > s.opened_at + interval '5 seconds'
               AND NOT EXISTS (SELECT 1 FROM integrity_alerts a
                                WHERE a.session_id = s.session_id
                                  AND a.alert_type = 'microphone_switched')
               AND NOT EXISTS (SELECT 1 FROM collector_notifications n
                                WHERE n.event_key = 'nojabra:' || s.session_id)
             ORDER BY s.opened_at LIMIT 100
        """)
        for r in rows:
            where = f"{r['hospital_id']}, {r['machine_name'] or 'a PC'}"
            await notify(conn, hospital_id=r["hospital_id"], event="recording_without_jabra",
                         subject=f"Patient recorded without the Jabra: {where}",
                         body=(f"A new recording started at {_hhmm(r['opened_at'])} at {where} on "
                               f"{r['microphone'] or 'the PC microphone'}, not the Jabra. The "
                               "Jabra has not been plugged back in.\n\nPlease ask the doctor, "
                               "between patients, to plug the Jabra in.") + _footer(),
                         event_key=f"nojabra:{r['session_id']}", urgent=True,
                         everyone_if_nobody=True, device_id=r["device_id"],
                         session_id=r["session_id"], happened_at=r["opened_at"])

    # -- the patient did not consent: the recording was deleted
    async def refusals(self, conn) -> None:
        rows = await conn.fetch("""
            SELECT r.session_id, r.device_id, r.hospital_id, r.refused_at
              FROM session_refusals r
             WHERE r.refused_at > now() - interval '12 hours'
               AND NOT EXISTS (SELECT 1 FROM collector_notifications n
                                WHERE n.event_key = 'refused:' || r.session_id)
             ORDER BY r.refused_at LIMIT 100
        """)
        for r in rows:
            label = STOP_LABELS["patient_did_not_consent"]
            await notify(conn, hospital_id=r["hospital_id"], event="stopped_unusually",
                         subject=f"Recording stopped: {label}",
                         body=(f"At {_hhmm(r['refused_at'])} the doctor stopped the recording "
                               f"because {label}. Its audio is erased everywhere, as the "
                               "patient asked.\n\nNothing to do now.") + _footer(),
                         event_key=f"refused:{r['session_id']}", urgent=False,
                         device_id=r["device_id"], session_id=r["session_id"],
                         happened_at=r["refused_at"])

    # -- a recording left running: the doctor forgot to press Stop (038)
    async def recordings_too_long(self, conn) -> None:
        rows = await conn.fetch("""
            SELECT s.session_id, s.opened_at, s.hospital_id, s.device_id, d.machine_name
              FROM sessions s LEFT JOIN devices d ON d.device_id = s.device_id
             WHERE s.closed_at IS NULL
               AND s.opened_at < now() - $1::interval
               AND s.opened_at > now() - interval '12 hours'
               AND NOT EXISTS (SELECT 1 FROM collector_notifications n
                                WHERE n.event_key = 'long:' || s.session_id)
             ORDER BY s.opened_at LIMIT 100
        """, TOO_LONG)
        for r in rows:
            where = f"{r['hospital_id']}, {r['machine_name'] or 'a PC'}"
            minutes = int((datetime.now(timezone.utc) - r["opened_at"]).total_seconds() // 60)
            await notify(conn, hospital_id=r["hospital_id"], event="recording_too_long",
                         subject=f"Recording running {minutes} min: {where}",
                         body=(f"A recording at {where} has been running since "
                               f"{_hhmm(r['opened_at'])} ({minutes} min). The doctor may have "
                               "forgotten to press Stop, so the next patients would be mixed "
                               "into it.\n\nPlease ask the doctor, between patients, to stop "
                               "it.") + _footer(),
                         event_key=f"long:{r['session_id']}", urgent=True,
                         everyone_if_nobody=True, device_id=r["device_id"],
                         session_id=r["session_id"], happened_at=r["opened_at"])

    # -- a shift nobody started, while the place's PCs are on
    async def no_shift_started(self, conn) -> None:
        local = datetime.now(zone())
        for place in await conn.fetch("SELECT * FROM places"):
            place = dict(place)
            hospitals = await place_hospitals(place["place_id"], conn)
            if not hospitals:
                continue
            for kind in ("morning", "evening"):
                start = datetime.combine(local.date(), place[f"{kind}_start"], tzinfo=zone())
                end = datetime.combine(local.date(), place[f"{kind}_end"], tzinfo=zone())
                if not (start + NO_SHIFT_AFTER <= local < end):
                    continue
                started = await conn.fetchval("""
                    SELECT 1 FROM collector_shifts WHERE place_id = $1 AND shift_date = $2
                       AND (shift = $3 OR (started_at <= $4 AND (ended_at IS NULL OR ended_at > $5)))
                     LIMIT 1
                """, place["place_id"], local.date(), kind, end.astimezone(timezone.utc),
                    start.astimezone(timezone.utc))
                open_now = await conn.fetchval("""
                    SELECT 1 FROM devices WHERE hospital_id = ANY($1::text[])
                       AND revoked_at IS NULL AND last_seen_at > now() - interval '30 minutes'
                     LIMIT 1
                """, hospitals)
                if started or not open_now:
                    continue
                subject = f"Nobody started the {kind} shift: {place['name']}"
                body = (f"The {kind} shift at {place['name']} began at "
                        f"{place[f'{kind}_start']:%H:%M} and nobody has pressed Start my shift. "
                        "The PCs are on. If you are at the clinic, open the dashboard and "
                        "press Start my shift.") + _footer()
                key = f"noshift:{place['place_id']}:{local.date()}:{kind}"
                for email in await collectors_of(conn, place["place_id"]):
                    note_id = await _record(
                        conn, email=email, event="no_shift_started", subject=subject,
                        body=body, place_id=place["place_id"], event_key=key)
                    if note_id is not None:
                        await _deliver(conn, note_id, email, subject, body)
                import watchers
                watch_body = (f"The {kind} shift at {place['name']} began at "
                              f"{place[f'{kind}_start']:%H:%M} and no data collector has "
                              "pressed Start my shift. The PCs are on.\n\n"
                              + await watchers.contact_text(conn, place_id=place["place_id"]))
                for hospital_id in hospitals:
                    await watchers.copy_to_watchers(
                        conn, hospital_id=hospital_id, event="no_shift_started",
                        subject=subject, body=watch_body, event_key=key, urgent=True)

    # -- an urgent mail nobody marked seen
    async def escalate(self, conn) -> None:
        rows = await conn.fetch("""
            SELECT DISTINCT ON (n.event_key) n.*
              FROM collector_notifications n
             WHERE n.urgent AND n.escalated_at IS NULL
               AND n.event NOT IN ('escalation', 'still_not_fixed')
               AND n.channel IS DISTINCT FROM 'held'
               AND n.audience = 'collector'
               AND n.created_at < now() - $1::interval
               AND n.created_at > now() - interval '12 hours'
               AND NOT EXISTS (SELECT 1 FROM collector_notifications m
                                WHERE m.event_key = n.event_key AND m.audience = 'collector'
                                  AND m.seen_at IS NOT NULL)
             ORDER BY n.event_key, n.id
        """, ESCALATE_AFTER)
        import watchers
        for n in rows:
            if await watchers.quiet_for(conn, n["hospital_id"], n["place_id"]):
                # Night: not escalated now, nor stale in the morning; it is in the summary.
                await conn.execute("UPDATE collector_notifications SET escalated_at = now() "
                                   "WHERE event_key = $1 AND escalated_at IS NULL",
                                   n["event_key"])
                continue
            already = {r["email"] for r in await conn.fetch(
                "SELECT email FROM collector_notifications WHERE event_key = $1", n["event_key"])}
            others = [e for e in await collectors_of(conn, n["place_id"]) if e not in already] \
                if n["place_id"] else []
            now_body = await watchers.fresh_context(conn, dict(n))
            subject = f"Not yet seen: {n['subject']}"
            body = (f"This urgent alert was sent at {_hhmm(n['created_at'])} and nobody has "
                    f"marked it seen in 15 minutes.\n\n{now_body}")
            hour = datetime.now(timezone.utc).strftime("%Y%m%d%H")
            targets = [(e, f"esc:{n['event_key']}", "collector") for e in others]
            # The lab is mailed at most once an hour; the rest are on the dashboard.
            targets += [(e, f"esc-lab:{hour}", "lab") for e in lab_addresses()]
            for email, key, audience in targets:
                note_id = await _record(conn, email=email, event="escalation", subject=subject,
                                        body=body, event_key=key, urgent=True,
                                        place_id=n["place_id"], hospital_id=n["hospital_id"],
                                        device_id=n["device_id"], session_id=n["session_id"],
                                        audience=audience)
                if note_id is not None:
                    await _deliver(conn, note_id, email, subject, body)
            # 038/039: the hospital's researchers hear that nobody pressed Seen,
            # with who to call.
            await watchers.copy_to_watchers(
                conn, hospital_id=n["hospital_id"], event="escalation",
                subject=f"Not seen in 15 min: {n['subject']}",
                body=(f"The alert went out at {_hhmm(n['created_at'])} and nobody has pressed "
                      "Seen in 15 minutes. The place's other collectors and AIMS LAB are now "
                      "told.\n\n" + watchers.researcher_body(
                          now_body, await watchers.contact_text(conn, n["hospital_id"]))),
                event_key=f"esc:{n['event_key']}", urgent=True, device_id=n["device_id"],
                session_id=n["session_id"])
            await conn.execute("UPDATE collector_notifications SET escalated_at = now() "
                               "WHERE event_key = $1 AND escalated_at IS NULL", n["event_key"])


    # -- a message that could not be sent: try again, with growing waits
    async def retry_failed(self, conn) -> None:
        rows = await conn.fetch("""
            SELECT id, event, hospital_id, place_id FROM collector_notifications
             WHERE delivered = FALSE AND next_try_at IS NOT NULL AND next_try_at <= now()
               AND created_at > now() - $1::interval
             ORDER BY id LIMIT 50
        """, RETRY_FOR)
        import watchers
        for r in rows:
            # Quiet hours: nothing reaches anyone after the evening summary but the
            # summary itself; a retry waits for the morning shift.
            if r["event"] not in watchers.SUMMARY_EVENTS and \
                    await watchers.quiet_for(conn, r["hospital_id"], r["place_id"]):
                continue
            await _deliver(conn, r["id"])
        await conn.execute("""
            UPDATE collector_notifications SET next_try_at = NULL
             WHERE delivered = FALSE AND next_try_at IS NOT NULL
               AND created_at <= now() - $1::interval
        """, RETRY_FOR)

    # -- sending keeps failing: tell AIMS LAB, at most every three hours
    async def failure_alarm(self, conn) -> None:
        failed = await conn.fetchval("""
            SELECT count(*) FROM collector_notifications
             WHERE delivered = FALSE AND created_at > now() - interval '30 minutes'
               AND event <> 'send_failures' AND channel IS DISTINCT FROM 'held'
        """)
        if failed < FAILURES_ALARM:
            return
        now = datetime.now(timezone.utc)
        key = f"sendfail:{now:%Y%m%d}:{now.hour // 3}"
        if getattr(self, "_alarm_key", None) == key:
            return                                   # already raised in this three hours
        self._alarm_key = key
        last = await conn.fetchval("""
            SELECT error FROM collector_notifications
             WHERE delivered = FALSE AND event <> 'send_failures'
               AND channel IS DISTINCT FROM 'held' ORDER BY id DESC LIMIT 1
        """)
        logger.warning("%s alerts to data collectors failed in 30 minutes: %s", failed, last)
        subject = f"{failed} alerts to data collectors could not be sent"
        body = (f"In the last 30 minutes {failed} alerts to data collectors were not sent. "
                f"The last reason:\n{last}\n\nThey are tried again for six hours, and every "
                "alert is on the collectors' My shift page.")
        for email in lab_addresses():
            note_id = await _record(conn, email=email, event="send_failures", subject=subject,
                                    body=body, event_key=key, urgent=False)
            if note_id is not None:
                await _deliver(conn, note_id)

    # -- what collectors pressed in Telegram: Start (connect) and Seen
    async def telegram_updates(self, conn) -> None:
        if not telegram_ready():
            return
        await conn.execute("INSERT INTO collector_notifier_state (name, last_id) "
                           "VALUES ('telegram', 0) ON CONFLICT (name) DO NOTHING")
        last = await conn.fetchval("SELECT last_id FROM collector_notifier_state "
                                   "WHERE name = 'telegram'")
        updates, error = await _telegram("getUpdates", {
            "offset": int(last) + 1, "timeout": 0, "limit": 100,
            "allowed_updates": ["message", "callback_query"]})
        if updates is None:
            if error and "answered 409" in error:      # a webhook takes the updates: drop it
                await _telegram("deleteWebhook", {})
            return
        for update in updates:
            try:
                if update.get("callback_query"):
                    await self._seen_pressed(conn, update["callback_query"])
                elif update.get("message"):
                    await self._message(conn, update["message"])
            except Exception as exc:
                logger.warning("A Telegram update could not be handled: %s", _hide(str(exc)))
            await conn.execute("UPDATE collector_notifier_state SET last_id = $1, "
                               "updated_at = now() WHERE name = 'telegram'",
                               int(update["update_id"]))

    async def _message(self, conn, message: Dict[str, Any]) -> None:
        chat = (message.get("chat") or {})
        if chat.get("type") != "private":
            return
        text = (message.get("text") or "").strip()
        if not text.startswith("/start"):
            return
        parts = text.split()
        code = parts[1] if len(parts) > 1 else ""
        row = await conn.fetchrow("""
            UPDATE collector_telegram_codes SET used_at = now()
             WHERE code = $1 AND used_at IS NULL AND created_at > now() - $2::interval
            RETURNING email
        """, code, LINK_CODE_LASTS) if code else None
        if row is None:
            await send_telegram(chat["id"], "This link has expired or was already used. Open "
                                "the AIMScribe dashboard, go to My shift and press Connect "
                                "Telegram again.")
            return
        username = ((message.get("from") or {}).get("username") or "")[:64] or None
        await conn.execute("""
            INSERT INTO collector_telegram (email, chat_id, username) VALUES ($1, $2, $3)
            ON CONFLICT (email) DO UPDATE SET chat_id = EXCLUDED.chat_id,
                username = EXCLUDED.username, linked_at = now()
        """, row["email"], int(chat["id"]), username)
        person = await conn.fetchrow("""
            SELECT p.role, pl.name AS place FROM portal_people p
              LEFT JOIN places pl ON pl.place_id = p.place_id WHERE p.email = $1
        """, row["email"])
        place = person["place"] if person else None
        await portal._audit("collector.telegram_linked", row["email"], {"username": username})
        if person is not None and person["role"] != "collector":
            import watchers
            mine = await watchers.watched_hospitals(row["email"], conn)
            await send_telegram(chat["id"], f"Connected: {row['email']}.\n\nAlerts for the "
                                "hospitals you look after come here"
                                + (f" ({', '.join(mine)})" if mine else
                                   " (none assigned yet: ask an admin)")
                                + ", each with the data collector on shift and their phone "
                                "and Telegram, so you can call them. You also hear when they "
                                "press Seen, when a problem is still not fixed after 30 "
                                "minutes and when it is fixed, and get a summary of each shift "
                                f"at {watchers.midday_time():%H:%M} and "
                                f"{watchers.report_time():%H:%M}, with a CSV. Nothing comes "
                                "at night: it waits for the next summary.")
            return
        await send_telegram(chat["id"], f"Connected: {row['email']}.\n\nWhile you are on "
                            f"shift{' at ' + place if place else ''}, AIMScribe alerts come here. "
                            "Keep notifications on for Telegram. For an urgent alert, press "
                            "Seen when you are dealing with it.")

    async def _seen_pressed(self, conn, query: Dict[str, Any]) -> None:
        data = str(query.get("data") or "")
        chat_id = ((query.get("message") or {}).get("chat") or {}).get("id")
        answer = "Already marked seen" if data == "done" else "Not found"
        if data.startswith("seen:") and data[5:].isdigit() and chat_id is not None:
            done = await conn.fetchval("""
                UPDATE collector_notifications n SET seen_at = COALESCE(n.seen_at, now())
                  FROM collector_telegram t
                 WHERE n.id = $1 AND t.email = n.email AND t.chat_id = $2
                RETURNING n.id
            """, int(data[5:]), int(chat_id))
            if done:
                answer = "Marked seen"
                message = query.get("message") or {}
                await _telegram("editMessageReplyMarkup", {
                    "chat_id": chat_id, "message_id": message.get("message_id"),
                    "reply_markup": {"inline_keyboard": [[
                        {"text": "\u2705 Seen", "callback_data": "done"}]]}})
        await _telegram("answerCallbackQuery", {"callback_query_id": query.get("id"),
                                                "text": answer})


_notifier: Optional[Notifier] = None


def start() -> Optional[asyncio.Task]:
    """Start the mailer in this process (main_fastapi's lifespan). AIMS_COLLECTOR_MAILER=off stops it."""
    global _notifier
    if os.getenv("AIMS_COLLECTOR_MAILER", "on").strip().lower() in ("off", "0", "false", "no"):
        logger.info("The collector mailer is off (AIMS_COLLECTOR_MAILER)")
        return None
    _notifier = Notifier()
    _notifier.task = asyncio.get_running_loop().create_task(_notifier.run())
    return _notifier.task


async def stop() -> None:
    if _notifier is not None and _notifier.task is not None:
        _notifier.task.cancel()
        try:
            await _notifier.task
        except (asyncio.CancelledError, Exception):
            pass


__all__ = ["router", "admin_router", "hospital_scope", "narrow", "microphone_label",
           "is_clinic_mic", "Notifier", "start", "stop", "shift_for", "send_mail",
           "send_telegram", "telegram_ready", "telegram_status"]
