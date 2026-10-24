"""
Downloading many recordings at once, in numbered batches.

A researcher picks a selection - a clinic, a range of days, some or all of its
doctors - and the server hands it out in batches, oldest first:

    600 files in the selection
    batch 1   files   1-200   one ZIP
    batch 2   files 201-400   one ZIP, chosen from what batch 1 did not send
    batch 3   files 401-600   ...

A file counts as downloaded by a person once a batch of theirs that held it
finished and the file arrived whole (or they downloaded it on its own,
downloads.py). The next batch is always chosen from what is left, so a
batch never repeats a file and never skips one, whatever happened before: a
batch that broke off leaves its files for the next, a file that failed its
check in an otherwise good batch comes back in the next, and a recording
confirmed late for a day already downloaded is simply in the next batch.

Each file goes the way a single download does - a five-minute link to the
sealed copy, decrypted by the archive worker, checked against the archived
file's SHA-256 - and is written straight into the ZIP as it streams. Nothing
decrypted is written to any disk. The ZIP holds a README, the WAVs in
clinic/day/doctor folders, and manifest.csv with each file's session,
length, SHA-256 and whether it arrived. No file name carries a patient
identifier.

Limits (an admin has none of the first three):

    AIMS_BULK_HOURS          when a researcher may start one, clinic time
                             ("22:00-07:00"; empty: any time)
    AIMS_BULK_BATCH_SIZE     the largest batch a researcher may ask for (200)
    AIMS_BULK_DAILY_FILES    files a researcher may take in 24 hours (600)
    AIMS_BULK_ADMIN_BATCH_SIZE  the largest batch for an admin (1000)
    AIMS_BULK_AT_ONCE        batches running on the server at once (2)
    AIMS_BULK_STREAM_MBPS    each batch's speed, MB/s (10), so the clinics'
                             uploads always have room
"""
from __future__ import annotations

import asyncio
import csv
import hashlib
import io
import json
import logging
import os
import time
import zipfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple
from zoneinfo import ZoneInfo

import anyio
import httpx
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

import api_v2
import dashboard
import downloads
import portal

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v2/dashboard/bulk", tags=["downloads"])

# A running batch that has not moved for this long has stopped (the browser
# went away, or the server restarted): it no longer counts as running, and
# its files are left for the next batch.
STALE = timedelta(minutes=10)
PROGRESS_EVERY = 30.0          # seconds between progress writes while streaming
MAX_DAYS = 366
BUSY_TRIES = 40                # the worker busy with other downloads: ~2 minutes
BUSY_WAIT = 3.0
SIZES = (50, 100, 150, 200, 500, 1000)


# ------------------------------------------------------------------ settings

def _number(name: str, default: float) -> float:
    try:
        value = float(os.getenv(name, "").strip() or default)
    except ValueError:
        return default
    return value if value > 0 else default


@dataclass(frozen=True)
class Limits:
    batch_max: int
    daily_files: Optional[int]       # None: no daily limit
    window: Optional[Tuple[int, int]]  # minutes after midnight; None: any time
    at_once: int
    stream_bytes_per_second: float


def limits_for(role: str) -> Limits:
    admin = role == "admin"
    return Limits(
        batch_max=int(_number("AIMS_BULK_ADMIN_BATCH_SIZE" if admin else "AIMS_BULK_BATCH_SIZE",
                              1000 if admin else 200)),
        daily_files=None if admin else int(_number("AIMS_BULK_DAILY_FILES", 600)),
        window=None if admin else hours(os.getenv("AIMS_BULK_HOURS", "")),
        at_once=int(_number("AIMS_BULK_AT_ONCE", 2)),
        stream_bytes_per_second=_number("AIMS_BULK_STREAM_MBPS", 10) * 1_000_000,
    )


def hours(text: str) -> Optional[Tuple[int, int]]:
    """'22:00-07:00' -> (1320, 420). Empty or unreadable: any time."""
    text = (text or "").strip()
    if not text:
        return None
    try:
        a, b = (part.strip() for part in text.split("-", 1))
        start = [int(x) for x in a.split(":")]
        end = [int(x) for x in b.split(":")]
        window = (start[0] * 60 + (start[1] if len(start) > 1 else 0),
                  end[0] * 60 + (end[1] if len(end) > 1 else 0))
    except (ValueError, IndexError):
        logger.warning("AIMS_BULK_HOURS=%r is not HH:MM-HH:MM; bulk downloads are "
                       "open at any time", text)
        return None
    if not all(0 <= m < 24 * 60 for m in window) or window[0] == window[1]:
        return None
    return window


def _now_local() -> datetime:
    """Now where the clinics are (TZ, as the dashboard reads it)."""
    try:
        return datetime.now(ZoneInfo(os.getenv("TZ", "").strip()))
    except Exception:
        return datetime.now().astimezone()


def window_state(window: Optional[Tuple[int, int]],
                 now: Optional[datetime] = None) -> Dict[str, Any]:
    """Is the window open now, and if not, when does it open (clinic time)?"""
    if window is None:
        return {"open": True, "hours": None}
    now = now or _now_local()
    start, end = window
    minute = now.hour * 60 + now.minute
    inside = (start <= minute < end) if start < end else (minute >= start or minute < end)
    label = f"{start // 60:02d}:{start % 60:02d}-{end // 60:02d}:{end % 60:02d}"
    if inside:
        return {"open": True, "hours": label}
    wait = (start - minute) % (24 * 60)
    opens = (now + timedelta(minutes=wait)).replace(second=0, microsecond=0)
    return {"open": False, "hours": label, "opens_at": opens.isoformat(),
            "opens_in_minutes": wait}


# ------------------------------------------------------------------ the selection

@dataclass(frozen=True)
class Selection:
    hospital_id: Optional[str]
    start: date
    end: date
    doctors: Optional[Tuple[str, ...]]      # None: every doctor

    @property
    def key(self) -> str:
        return hashlib.sha256(json.dumps(
            [self.hospital_id, self.start.isoformat(), self.end.isoformat(),
             list(self.doctors) if self.doctors else None]).encode("utf-8")).hexdigest()

    def describe(self, clinic_name: Optional[str] = None) -> str:
        where = clinic_name or self.hospital_id or "every clinic"
        days = (self.start.isoformat() if self.start == self.end
                else f"{self.start.isoformat()} to {self.end.isoformat()}")
        who = ("every doctor" if not self.doctors
               else ", ".join(self.doctors) if len(self.doctors) <= 6
               else f"{len(self.doctors)} doctors")
        return f"{where}, {days}, {who}"


def selection_of(hospital_id: Optional[str], start: Optional[str], end: Optional[str],
                 doctors: Optional[Sequence[str]]) -> Selection:
    today = dashboard.clinic_today()
    first = dashboard._day(start, today)
    last = dashboard._day(end, first)
    if last < first:
        raise HTTPException(status_code=400, detail="'to' is before 'from'")
    if (last - first).days >= MAX_DAYS:
        raise HTTPException(status_code=400,
                            detail=f"choose at most {MAX_DAYS} days at a time")
    clinic = dashboard._clinic(hospital_id)
    chosen = sorted({dashboard._clinic(d) for d in (doctors or []) if d})
    return Selection(clinic, first, last, tuple(chosen) or None)


# Every file in a selection that can be downloaded: confirmed, copied, with a
# fingerprinted copy. A recording cut into patient files is one file per part;
# one never cut is one file (part 0). Oldest first, always in the same order.
_FILES = """
SELECT f.session_id, f.part_no, f.hospital_id, f.doctor_id, f.session_date,
       f.local_start, f.seconds, f.parts, c.object_key, c.plain_sha256, c.bytes, f.stem
  FROM (
    SELECT s.session_id, COALESCE(p.part_no, 0) AS part_no,
           COALESCE(p.file_stem, s.file_stem) AS stem, s.hospital_id,
           COALESCE(p.doctor_id, s.doctor_id) AS doctor_id, s.session_date,
           COALESCE(p.local_start, s.local_start) AS local_start, s.opened_at,
           CASE WHEN p.part_no IS NULL THEN s.total_duration_seconds
                ELSE COALESCE(p.end_seconds, s.total_duration_seconds) - p.start_seconds
           END AS seconds,
           s.parts_count AS parts
      FROM sessions s
      LEFT JOIN consultation_parts p ON p.session_id = s.session_id
     WHERE s.confirmation = 'confirmed' AND s.copied_at IS NOT NULL
       AND s.session_date BETWEEN $1 AND $2
       AND ($3::text IS NULL OR s.hospital_id = $3)
  ) f
  JOIN LATERAL (
    SELECT c.object_key, c.plain_sha256, c.bytes FROM cloud_copies c
     WHERE c.session_id = f.session_id AND c.kind = 'audio' AND c.plain_sha256 IS NOT NULL
       AND regexp_replace(c.object_key, '^.*/', '') = f.stem || '.v' || c.version || '.wav.enc'
     ORDER BY c.version DESC LIMIT 1
  ) c ON true
 ORDER BY f.session_date, f.opened_at NULLS LAST, f.session_id, f.part_no
"""

# What this person already has: whole files in their finished batches, and
# single downloads that arrived.
_MINE = """
SELECT bf.session_id, bf.part_no
  FROM portal_batch_files bf JOIN portal_batches b ON b.id = bf.batch_id
 WHERE b.email = $1 AND bf.outcome = 'sent'
UNION
SELECT session_id, COALESCE(part_no, 0) FROM portal_downloads
 WHERE email = $1 AND outcome = 'completed'
"""


async def _files(conn, sel: Selection, *, any_doctor: bool = False) -> List[Dict[str, Any]]:
    rows = [dict(r) for r in await conn.fetch(_FILES, sel.start, sel.end, sel.hospital_id)]
    if sel.doctors and not any_doctor:
        wanted = set(sel.doctors)
        rows = [r for r in rows if r["doctor_id"] in wanted]
    return rows


async def _mine(conn, email: str) -> Set[Tuple[str, int]]:
    return {(r["session_id"], r["part_no"]) for r in await conn.fetch(_MINE, email)}


# A file is marked sent only when its batch's ZIP reached its end, so a batch
# that stops leaves its files as they were: never sent for a first attempt,
# still sent for a batch being downloaded again after it once arrived whole.
_ENDED = """
    outcome = CASE WHEN EXISTS (SELECT 1 FROM portal_batch_files x
                                 WHERE x.batch_id = portal_batches.id AND x.outcome = 'sent')
                   THEN 'completed' ELSE 'failed' END,
    files_done = (SELECT count(*) FROM portal_batch_files x
                   WHERE x.batch_id = portal_batches.id AND x.outcome = 'sent'),
    files_failed = (SELECT count(*) FROM portal_batch_files x
                     WHERE x.batch_id = portal_batches.id AND x.outcome = 'failed'),
    finished_at = now()
"""


async def _tidy(conn) -> None:
    """A batch that has stopped moving has stopped: it no longer counts as running."""
    await conn.execute(f"""
        UPDATE portal_batches
           SET {_ENDED},
               reason = 'the download stopped part way (the browser closed, or the '
                        'server restarted)'
         WHERE outcome = 'started' AND progress_at < now() - make_interval(secs => $1)
    """, STALE.total_seconds())


async def _used_today(conn, email: str) -> Tuple[int, Optional[datetime]]:
    """Distinct files in batches this person started in 24 hours, and when the oldest frees."""
    row = await conn.fetchrow("""
        SELECT count(DISTINCT (bf.session_id, bf.part_no)) AS used, min(b.started_at) AS oldest
          FROM portal_batches b JOIN portal_batch_files bf ON bf.batch_id = b.id
         WHERE b.email = $1 AND b.started_at > now() - interval '1 day'
    """, email)
    return int(row["used"] or 0), row["oldest"]


async def _running(conn, *, email: Optional[str] = None,
                   other_than: Optional[int] = None) -> int:
    return int(await conn.fetchval("""
        SELECT count(*) FROM portal_batches
         WHERE outcome = 'started' AND ($1::text IS NULL OR email = $1)
           AND ($2::bigint IS NULL OR id <> $2)
    """, email, other_than))


def _batch_json(b) -> Dict[str, Any]:
    def moment(v):
        return v.isoformat() if hasattr(v, "isoformat") else v
    return {
        "id": b["id"], "email": b["email"], "batch_no": b["batch_no"],
        "hospital_id": b["hospital_id"], "from": moment(b["date_from"]),
        "to": moment(b["date_to"]), "doctors": list(b["doctors"]) if b["doctors"] else None,
        "first_no": b["first_no"], "last_no": b["last_no"],
        "selection_total": b["selection_total"], "files": b["files"],
        "bytes_estimate": b["bytes_estimate"], "created_at": moment(b["created_at"]),
        "started_at": moment(b["started_at"]), "finished_at": moment(b["finished_at"]),
        "files_done": b["files_done"], "files_failed": b["files_failed"],
        "bytes_sent": b["bytes_sent"], "outcome": b["outcome"], "reason": b["reason"],
        "attempts": b["attempts"],
        "url": f"/api/v2/dashboard/bulk/batches/{b['id']}/zip",
    }


async def _person(request: Request, *, write: bool = False) -> portal.Person:
    person = await portal.person_for(request)
    if person is None:
        raise HTTPException(status_code=401, detail="Sign in with Google to download recordings.")
    if write:
        portal.check_csrf(request)
    return person


# ------------------------------------------------------------------ what is left

@router.get("/preview")
async def preview(request: Request,
                  hospital_id: Optional[str] = None,
                  from_date: Optional[str] = Query(None, alias="from"),
                  to_date: Optional[str] = Query(None, alias="to"),
                  doctor: Optional[List[str]] = Query(None),
                  size: int = 200):
    """
    The selection as the person stands with it: how many files, how many they
    already have, what the next batch would be - and each doctor and day, so
    the page can offer them with their counts.
    """
    person = await _person(request)
    sel = selection_of(hospital_id, from_date, to_date, doctor)
    lim = limits_for(person.role)
    async with portal._pool().acquire() as conn:
        await _tidy(conn)
        everyone = await _files(conn, sel, any_doctor=True)
        mine = await _mine(conn, person.email)
        used, oldest = await _used_today(conn, person.email)
        running = await conn.fetchrow("""
            SELECT * FROM portal_batches WHERE email = $1 AND outcome = 'started'
             ORDER BY id DESC LIMIT 1
        """, person.email)
        # A batch prepared and never started is replaced by the next one asked for.
        batches_before = int(await conn.fetchval("""
            SELECT count(*) FROM portal_batches
             WHERE email = $1 AND selection_key = $2 AND outcome NOT IN ('abandoned', 'ready')
        """, person.email, sel.key))

    wanted = set(sel.doctors) if sel.doctors else None
    chosen = [f for f in everyone if wanted is None or f["doctor_id"] in wanted]
    have = [f for f in chosen if (f["session_id"], f["part_no"]) in mine]
    left = [f for f in chosen if (f["session_id"], f["part_no"]) not in mine]

    by_doctor: Dict[str, Dict[str, Any]] = {}
    by_day: Dict[str, Dict[str, Any]] = {}
    for f in everyone:
        got = (f["session_id"], f["part_no"]) in mine
        d = by_doctor.setdefault(f["doctor_id"], {"doctor_id": f["doctor_id"],
                                                  "files": 0, "downloaded": 0})
        d["files"] += 1
        d["downloaded"] += got
        if wanted is None or f["doctor_id"] in wanted:
            day = f["session_date"].isoformat()
            y = by_day.setdefault(day, {"date": day, "files": 0, "downloaded": 0})
            y["files"] += 1
            y["downloaded"] += got

    size = max(1, min(int(size or lim.batch_max), lim.batch_max))
    left_today = None if lim.daily_files is None else max(0, lim.daily_files - used)
    take = min(size, len(left), left_today if left_today is not None else size)
    return {
        "selection": {"hospital_id": sel.hospital_id, "from": sel.start.isoformat(),
                      "to": sel.end.isoformat(),
                      "doctors": list(sel.doctors) if sel.doctors else None},
        "total": len(chosen), "downloaded": len(have), "remaining": len(left),
        "bytes_total": sum(int(f["bytes"] or 0) for f in chosen),
        "bytes_remaining": sum(int(f["bytes"] or 0) for f in left),
        "doctors": sorted(by_doctor.values(), key=lambda d: d["doctor_id"]),
        "days": sorted(by_day.values(), key=lambda d: d["date"]),
        "next": None if not take else {
            "batch_no": batches_before + 1, "files": take,
            "first_no": len(have) + 1, "last_no": len(have) + take,
            "bytes": sum(int(f["bytes"] or 0) for f in left[:take])},
        "limits": {
            "role": person.role, "batch_max": lim.batch_max,
            "sizes": [s for s in SIZES if s <= lim.batch_max] or [lim.batch_max],
            "daily_files": lim.daily_files, "used_today": used, "left_today": left_today,
            "frees_at": (oldest + timedelta(days=1)).isoformat()
                        if oldest and left_today == 0 else None,
            "window": window_state(lim.window),
        },
        "running": _batch_json(running) if running else None,
    }


# ------------------------------------------------------------------ a new batch

class BatchRequest(BaseModel):
    hospital_id: Optional[str] = None
    from_date: str = Field(..., alias="from")
    to_date: str = Field(..., alias="to")
    doctors: Optional[List[str]] = None
    size: int = Field(200, ge=1, le=10_000)


def _refuse(status: int, message: str):
    raise HTTPException(status_code=status, detail=message)


def _window_message(state: Dict[str, Any]) -> str:
    opens = state.get("opens_in_minutes") or 0
    return (f"Bulk downloads for researchers are open {state['hours']} (clinic time), after "
            f"the clinics close. They open in {opens // 60} h {opens % 60} min. "
            f"An admin can change the hours (AIMS_BULK_HOURS).")


@router.post("/batches")
async def create_batch(body: BatchRequest, request: Request):
    """
    The next batch of a selection: the oldest files this person does not have
    yet, up to the size asked for and what is left of their day.
    """
    person = await _person(request, write=True)
    sel = selection_of(body.hospital_id, body.from_date, body.to_date, body.doctors)
    lim = limits_for(person.role)
    state = window_state(lim.window)
    if not state["open"]:
        _refuse(403, _window_message(state))
    size = max(1, min(body.size, lim.batch_max))

    async with portal._pool().acquire() as conn:
        async with conn.transaction():
            # One person's two clicks must not pick the same files twice.
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext('portal-batch:' || $1))",
                               person.email)
            await _tidy(conn)
            if await _running(conn, email=person.email):
                _refuse(409, "One of your batches is downloading now. Wait for it to finish; "
                             "the next batch is chosen from what it did not send.")
            # A batch asked for and never started is replaced by this one.
            await conn.execute("""
                UPDATE portal_batches SET outcome = 'abandoned', finished_at = now()
                 WHERE email = $1 AND outcome = 'ready'
            """, person.email)
            if lim.daily_files is not None:
                used, oldest = await _used_today(conn, person.email)
                left_today = lim.daily_files - used
                if left_today <= 0:
                    frees = (oldest + timedelta(days=1)) if oldest else None
                    _refuse(429, f"You have downloaded {used} files in the last 24 hours, "
                                 f"the limit of {lim.daily_files}. More can be downloaded "
                                 + (f"from {frees.astimezone().strftime('%d %b %H:%M')}."
                                    if frees else "tomorrow."))
                size = min(size, left_today)

            chosen = await _files(conn, sel)
            mine = await _mine(conn, person.email)
            left = [f for f in chosen if (f["session_id"], f["part_no"]) not in mine]
            if not left:
                _refuse(409, f"Nothing left to download: you already have all "
                             f"{len(chosen)} files of this selection." if chosen else
                             "No downloadable recordings in this selection.")
            take = left[:size]
            have = len(chosen) - len(left)
            batch_no = int(await conn.fetchval("""
                SELECT count(*) FROM portal_batches
                 WHERE email = $1 AND selection_key = $2 AND outcome <> 'abandoned'
            """, person.email, sel.key)) + 1
            batch = await conn.fetchrow("""
                INSERT INTO portal_batches
                    (email, hospital_id, date_from, date_to, doctors, selection_key,
                     batch_no, first_no, last_no, selection_total, files, bytes_estimate,
                     address)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
                RETURNING *
            """, person.email, sel.hospital_id, sel.start, sel.end,
                list(sel.doctors) if sel.doctors else None, sel.key, batch_no,
                have + 1, have + len(take), len(chosen), len(take),
                sum(int(f["bytes"] or 0) for f in take),
                request.client.host if request.client else None)
            await conn.executemany("""
                INSERT INTO portal_batch_files (batch_id, position, session_id, part_no, object_key)
                VALUES ($1, $2, $3, $4, $5)
            """, [(batch["id"], n, f["session_id"], f["part_no"], f["object_key"])
                  for n, f in enumerate(take, start=1)])
    await api_v2._repo().audit(
        event_type="recordings.batch_prepared", actor_type="admin", actor_id=person.email,
        detail={"batch": batch["id"], "batch_no": batch_no, "files": len(take),
                "selection": sel.describe(), "first_no": have + 1,
                "last_no": have + len(take), "of": len(chosen)})
    return _batch_json(batch)


@router.get("/batches")
async def list_batches(request: Request, everyone: bool = False, limit: int = 30):
    """This person's batches, newest first. An admin may see everyone's."""
    person = await _person(request)
    if everyone and person.role != "admin":
        everyone = False
    limit = max(1, min(int(limit), 200))
    async with portal._pool().acquire() as conn:
        await _tidy(conn)
        rows = await conn.fetch("""
            SELECT * FROM portal_batches
             WHERE ($1 OR email = $2) AND outcome <> 'abandoned'
             ORDER BY id DESC LIMIT $3
        """, everyone, person.email, limit)
    return {"batches": [_batch_json(r) for r in rows]}


# ------------------------------------------------------------------ the ZIP

class _Sink:
    """What zipfile writes to: kept in memory until the response takes it."""

    def __init__(self) -> None:
        self._parts: List[bytes] = []

    def write(self, data) -> int:
        self._parts.append(bytes(data))
        return len(data)

    def flush(self) -> None:
        pass

    def take(self) -> bytes:
        out, self._parts = b"".join(self._parts), []
        return out


def _clock(value) -> Tuple[int, int, int]:
    if value is None:
        return 0, 0, 0
    return value.hour, value.minute, value.second


def entry_name(f: Dict[str, Any]) -> str:
    """clinic/day/doctor/clinic_doctor_day_HHMM_session[_pN].wav - no patient id."""
    safe = downloads._SAFE_NAME
    day = f["session_date"].isoformat()
    h, m, _ = _clock(f["local_start"])
    stem = "_".join(str(x) for x in (f["hospital_id"], f["doctor_id"], day,
                                      f"{h:02d}{m:02d}", f["session_id"]) if x)
    if f["part_no"] and (f["parts"] or 1) > 1:
        stem += f"_p{f['part_no']}"
    folder = "/".join(safe.sub("-", str(x))[:64] for x in (f["hospital_id"], day, f["doctor_id"]))
    return f"{folder}/{safe.sub('-', stem)[:150]}.wav"


def readme(batch, sel: Selection, files: int) -> str:
    return "\n".join([
        f"AIMScribe recordings - batch {batch['batch_no']} of this selection",
        "",
        f"Selection:   {sel.describe()}",
        f"This batch:  files {batch['first_no']}-{batch['last_no']} of "
        f"{batch['selection_total']} in the selection, oldest first ({files} files)",
        f"Prepared for {batch['email']} on {datetime.now().astimezone():%Y-%m-%d %H:%M %Z}",
        "",
        "Each .wav file is one patient's consultation, whole. Folders are",
        "clinic / day / doctor. No file name carries a patient identifier: the",
        "session_id in manifest.csv finds the recording in the database viewer.",
        "",
        "manifest.csv lists every file with its SHA-256. To check one on Windows:",
        "    certutil -hashfile <file>.wav SHA256",
        "",
        "A file marked FAILED in manifest.csv did not arrive whole. It is not",
        "counted as downloaded and comes again in your next batch.",
        "",
        "These recordings are patients' consultations. Keep them only where your",
        "ethics approval allows, and delete them when the study ends.",
        "",
    ])


class _Pace:
    """Keeps one batch at or under its speed."""

    def __init__(self, bytes_per_second: float) -> None:
        self.rate = bytes_per_second
        self.began = time.monotonic()
        self.sent = 0

    async def after(self, n: int) -> None:
        self.sent += n
        ahead = self.sent / self.rate - (time.monotonic() - self.began)
        if ahead > 0.005:
            await asyncio.sleep(ahead)


async def _open_copy(f: Dict[str, Any]):
    """The archive worker's answer for one file, waiting while it is busy."""
    store = api_v2._copy_bucket("audio")
    expect: Dict[str, Any] = {
        "session_id": f["session_id"], "kind": "audio",
        "stem_sha256": hashlib.sha256(f["stem"].encode("utf-8")).hexdigest()}
    if f["part_no"]:
        expect["part_no"] = f["part_no"]
    last = "the archive worker was busy"
    for _ in range(BUSY_TRIES):
        link = await asyncio.get_event_loop().run_in_executor(
            None, store.get_presigned_download_url, f["object_key"], 300)
        client = downloads._client()
        try:
            answer = await client.send(client.build_request(
                "POST", downloads._decrypt_url() + "/decrypt",
                headers={"X-Worker-Key": os.getenv("AIMS_WORKER_KEY", "")},
                json={"url": link, "expect": expect,
                      "plain_sha256": bytes(f["plain_sha256"]).hex()}), stream=True)
        except httpx.HTTPError as exc:
            await client.aclose()
            last = f"the archive worker could not be reached: {type(exc).__name__}"
            await asyncio.sleep(BUSY_WAIT)
            continue
        if answer.status_code == 200:
            return client, answer, ""
        try:
            detail = (await answer.aread()).decode("utf-8", "replace")
            detail = json.loads(detail).get("detail", detail)
        except Exception:
            detail = f"status {answer.status_code}"
        await answer.aclose()
        await client.aclose()
        if answer.status_code != 429:
            return None, None, str(detail)[:300]
        last = "the archive worker was busy with other downloads"
        await asyncio.sleep(BUSY_WAIT)
    return None, None, last


async def _load_batch(conn, batch_id: int):
    batch = await conn.fetchrow("SELECT * FROM portal_batches WHERE id = $1", batch_id)
    if batch is None:
        return None, []
    rows = await conn.fetch("""
        SELECT bf.position, bf.session_id, bf.part_no, bf.object_key
          FROM portal_batch_files bf WHERE bf.batch_id = $1 ORDER BY bf.position
    """, batch_id)
    sel = Selection(batch["hospital_id"], batch["date_from"], batch["date_to"],
                    tuple(batch["doctors"]) if batch["doctors"] else None)
    known = {(f["session_id"], f["part_no"]): f for f in await _files(conn, sel, any_doctor=True)}
    files = []
    for r in rows:
        f = known.get((r["session_id"], r["part_no"]))
        if f is not None and f["object_key"] == r["object_key"]:
            files.append({**f, "position": r["position"]})
        else:
            # Gone from the selection since (a newer copy, a recording re-cut):
            # listed as failed, and the next batch takes what is there now.
            files.append({"position": r["position"], "session_id": r["session_id"],
                          "part_no": r["part_no"], "missing": True})
    return batch, files


async def _may_start(conn, person: portal.Person, batch_id: int, lim: Limits):
    """The batch, if this person may start downloading it now; else _Refused, saying why."""
    await _tidy(conn)
    batch, files = await _load_batch(conn, batch_id)
    if batch is None or batch["email"] != person.email:
        raise downloads._Refused(404, "No such batch of yours.")
    if batch["outcome"] == "started":
        raise downloads._Refused(409, "This batch is downloading now.")
    if batch["outcome"] == "abandoned":
        raise downloads._Refused(409, "This batch was replaced by a newer one. "
                                      "Start the next batch from the dashboard.")
    state = window_state(lim.window)
    if not state["open"]:
        raise downloads._Refused(403, _window_message(state))
    if await _running(conn, other_than=batch_id) >= lim.at_once:
        raise downloads._Refused(429, f"{lim.at_once} batches are downloading on the "
                                      f"server now. Try again in a few minutes.")
    if lim.daily_files is not None:
        used = int(await conn.fetchval("""
            SELECT count(DISTINCT (bf.session_id, bf.part_no))
              FROM portal_batch_files bf JOIN portal_batches b ON b.id = bf.batch_id
             WHERE b.email = $1 AND (b.id = $2 OR b.started_at > now() - interval '1 day')
        """, person.email, batch_id))
        if used > lim.daily_files:
            raise downloads._Refused(429, f"This batch would take you past "
                                          f"{lim.daily_files} files in 24 hours.")
    return batch, files


@router.get("/batches/{batch_id}/check")
async def check_batch(batch_id: int, request: Request):
    """Whether the ZIP would start now - asked first, so a refusal shows on the page."""
    person = await _person(request)
    try:
        async with portal._pool().acquire() as conn:
            await _may_start(conn, person, batch_id, limits_for(person.role))
    except downloads._Refused as refused:
        return {"ok": False, "status": refused.status, "detail": refused.message}
    return {"ok": True}


@router.get("/batches/{batch_id}/zip")
async def download_batch(batch_id: int, request: Request):
    person = await portal.person_for(request)
    if person is None:
        return downloads._refusal(request, 401, "Sign in with Google to download recordings.")
    if (request.headers.get("sec-fetch-site") or "same-origin") not in ("same-origin", "none"):
        return downloads._refusal(request, 403, "Downloads start from the dashboard only.")
    if not downloads._decrypt_url():
        return downloads._refusal(request, 503, "Downloads are not set up on this server "
                                                "(AIMS_DECRYPT_URL).")
    lim = limits_for(person.role)
    try:
        async with portal._pool().acquire() as conn:
            async with conn.transaction():
                await conn.execute("SELECT pg_advisory_xact_lock(hashtext('portal-batch:' || $1))",
                                   person.email)
                batch, files = await _may_start(conn, person, batch_id, lim)
                await conn.execute("""
                    UPDATE portal_batches
                       SET outcome = 'started', started_at = now(), progress_at = now(),
                           finished_at = NULL, attempts = attempts + 1, files_done = 0,
                           files_failed = 0, bytes_sent = 0, reason = NULL
                     WHERE id = $1
                """, batch_id)
    except downloads._Refused as refused:
        return downloads._refusal(request, refused.status, refused.message)

    sel = Selection(batch["hospital_id"], batch["date_from"], batch["date_to"],
                    tuple(batch["doctors"]) if batch["doctors"] else None)
    await api_v2._repo().audit(
        event_type="recordings.batch_started", actor_type="admin", actor_id=person.email,
        detail={"batch": batch_id, "batch_no": batch["batch_no"], "files": len(files),
                "attempt": batch["attempts"] + 1, "selection": sel.describe()})
    name = downloads._SAFE_NAME.sub("-", "_".join(
        str(x) for x in ("AIMScribe", batch["hospital_id"] or "all-clinics",
                         batch["date_from"].isoformat(), batch["date_to"].isoformat(),
                         f"batch{batch['batch_no']}",
                         f"files{batch['first_no']}-{batch['last_no']}")))[:180] + ".zip"
    return StreamingResponse(_stream(batch, sel, files, lim), media_type="application/zip",
                             headers={"Content-Disposition": f'attachment; filename="{name}"',
                                      "Cache-Control": "no-store",
                                      "X-Content-Type-Options": "nosniff"})


async def _stream(batch, sel: Selection, files: List[Dict[str, Any]], lim: Limits):
    sink = _Sink()
    zf = zipfile.ZipFile(sink, "w", compression=zipfile.ZIP_STORED, allowZip64=True)
    pace = _Pace(lim.stream_bytes_per_second)
    results: Dict[int, Tuple[str, str]] = {}      # position -> (status, note)
    sent_bytes, finished, reason = 0, False, ""
    last_progress = time.monotonic()

    async def progress(done: int, failed: int) -> None:
        nonlocal last_progress
        last_progress = time.monotonic()
        try:
            async with portal._pool().acquire() as conn:
                await conn.execute("""
                    UPDATE portal_batches SET progress_at = now(), files_done = $2,
                                              files_failed = $3, bytes_sent = $4
                     WHERE id = $1 AND outcome = 'started'
                """, batch["id"], done, failed, sent_bytes)
        except Exception as exc:
            logger.warning("Could not record progress of batch %s: %s", batch["id"], exc)

    try:
        zf.writestr(zipfile.ZipInfo("README.txt", time.localtime()[:6]),
                    readme(batch, sel, len(files)))
        yield sink.take()
        for f in files:
            if f.get("missing"):
                results[f["position"]] = ("FAILED", "no longer in the selection as it was; "
                                                    "the next batch takes it as it is now")
                continue
            client, answer, problem = await _open_copy(f)
            if answer is None:
                results[f["position"]] = ("FAILED", problem)
                await progress(sum(1 for s, _ in results.values() if s == "OK"),
                               sum(1 for s, _ in results.values() if s == "FAILED"))
                continue
            size = int(answer.headers.get("content-length") or 0)
            got = 0
            day = f["session_date"]
            h, m, s = _clock(f["local_start"])
            info = zipfile.ZipInfo(entry_name(f), (max(day.year, 1980), day.month, day.day, h, m, s))
            info.compress_type = zipfile.ZIP_STORED
            try:
                with zf.open(info, "w") as out:
                    async for block in answer.aiter_bytes():
                        out.write(block)
                        got += len(block)
                        chunk = sink.take()
                        sent_bytes += len(chunk)
                        yield chunk
                        await pace.after(len(chunk))
                        if time.monotonic() - last_progress >= PROGRESS_EVERY:
                            await progress(sum(1 for s, _ in results.values() if s == "OK"),
                                           sum(1 for s, _ in results.values() if s == "FAILED"))
            finally:
                await answer.aclose()
                await client.aclose()
            tail = sink.take()
            sent_bytes += len(tail)
            yield tail
            if size and got == size:
                results[f["position"]] = ("OK", "")
            else:
                results[f["position"]] = ("FAILED", "did not pass its check and was cut "
                                                    "short; this copy is incomplete")
            await progress(sum(1 for s, _ in results.values() if s == "OK"),
                           sum(1 for s, _ in results.values() if s == "FAILED"))

        zf.writestr(zipfile.ZipInfo("manifest.csv", time.localtime()[:6]),
                    manifest(files, results))
        zf.close()
        tail = sink.take()
        sent_bytes += len(tail)
        yield tail
        finished = True
    except BaseException as exc:
        reason = f"stopped: {type(exc).__name__}"
        raise
    finally:
        with anyio.CancelScope(shield=True):
            await _finish(batch, results, finished, sent_bytes, reason)


def manifest(files: List[Dict[str, Any]], results: Dict[int, Tuple[str, str]]) -> str:
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["no", "file", "status", "session_id", "part", "clinic", "doctor", "date",
                "start", "minutes", "sha256", "note"])
    for f in files:
        status, note = results.get(f["position"], ("FAILED", "not reached"))
        if f.get("missing"):
            w.writerow([f["position"], "", status, f["session_id"], f["part_no"],
                        "", "", "", "", "", "", note])
            continue
        h, m, s = _clock(f["local_start"])
        w.writerow([f["position"], entry_name(f), status, f["session_id"], f["part_no"],
                    f["hospital_id"], f["doctor_id"], f["session_date"].isoformat(),
                    f"{h:02d}:{m:02d}:{s:02d}" if f["local_start"] else "",
                    round(float(f["seconds"] or 0) / 60, 1),
                    bytes(f["plain_sha256"]).hex(), note])
    return out.getvalue()


async def _finish(batch, results: Dict[int, Tuple[str, str]], finished: bool,
                  sent_bytes: int, reason: str) -> None:
    ok = [p for p, (s, _) in results.items() if s == "OK"]
    failed = [p for p, (s, _) in results.items() if s == "FAILED"]
    try:
        async with portal._pool().acquire() as conn:
            async with conn.transaction():
                if finished:
                    # This ZIP reached its end: it is what the person has now.
                    await conn.execute("""
                        UPDATE portal_batch_files
                           SET outcome = CASE WHEN position = ANY($2::int[]) THEN 'sent'
                                              ELSE 'failed' END
                         WHERE batch_id = $1
                    """, batch["id"], ok)
                await conn.execute(f"""
                    UPDATE portal_batches
                       SET {_ENDED}, progress_at = now(), bytes_sent = $2,
                           reason = NULLIF($3, '')
                     WHERE id = $1
                """, batch["id"], sent_bytes,
                    reason if not finished else
                    (f"{len(failed)} file(s) did not arrive whole; they come again in the "
                     f"next batch" if failed else ""))
                if finished:
                    # The ZIP arrived, even if every file in it failed its check.
                    await conn.execute("UPDATE portal_batches SET outcome = 'completed' "
                                       "WHERE id = $1", batch["id"])
        await api_v2._repo().audit(
            event_type="recordings.batch_downloaded" if finished
            else "recordings.batch_failed",
            actor_type="admin", actor_id=batch["email"],
            detail={"batch": batch["id"], "batch_no": batch["batch_no"],
                    "files_sent": len(ok) if finished else 0, "files_failed": len(failed),
                    "bytes": sent_bytes, **({"reason": reason[:200]} if reason else {})})
    except Exception as exc:                       # never breaks the download itself
        logger.warning("Could not record the end of batch %s: %s", batch["id"], exc)


__all__ = ["router", "hours", "window_state", "entry_name", "manifest", "limits_for"]
