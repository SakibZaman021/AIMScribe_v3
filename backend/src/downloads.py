"""
Downloading a recording from the portal.

The audio in the cloud is encrypted with a key only the archive worker holds
(SRS-DAT-08), and only this server holds the bucket credentials. A download
needs both, so neither moves:

    browser --GET--> this server: signed in? within the limits? logged
                     -> a five-minute link to the one sealed object
            this server --POST /decrypt--> archive worker (internal network)
                     worker reads the object, checks it is this recording,
                     decrypts it chunk by chunk
    browser <--WAV-- this server <--WAV-- archive worker

Nothing decrypted is written to any disk. The worker holds the last chunk
back until the whole file's SHA-256 is the archived file's, so a download
that completes is that file, byte for byte.

Anyone on the portal list may download the audio - researchers and admins -
and each download is a row in portal_downloads and an entry in the audit log:
who, which recording, when, and whether it all arrived. The file is named by
clinic, doctor, date and session, without the patient's identifier.

The clinical record (17 Nov) - the JSON CMED sent, kept as its own sealed
copy - comes the same way, to the same people (researchers and admins; a data
collector neither), under the same daily limit, logged the same way. Before
this, reading one meant the copy key and restore.py.

Who may take what (049, 23 Nov 2026) - may_download() below, used by the
single downloads here and by the batches in bulk.py:

    admin (and the owner)   audio and clinical record, every hospital
    cmed (CMED staff)       the clinical record of every hospital; never audio
    researcher              audio and clinical record of the hospitals assigned
                            to them (researcher_hospitals) only; they still SEE
                            every hospital
    collector               nothing

Listening (26 Nov 2026): the same people play the audio in the dashboard,
a byte range at a time, decrypted chunk by chunk by the worker - see listen().
"""
from __future__ import annotations

import asyncio
import hashlib
import html
import json
import logging
import os
import re
from typing import Any, Dict, List, Optional

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response, StreamingResponse

import api_v2
import portal

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v2/dashboard", tags=["downloads"])

_KEY_NAME = re.compile(r"^(?P<stem>.+)\.v(?P<version>\d+)\.(?:wav|json)\.enc$")
# What each kind of copy is downloaded as.
_KINDS = {"audio": ("wav", "audio/wav", "the WAV"),
          "json": ("json", "application/json", "the clinical record")}
_SAFE_NAME = re.compile(r"[^A-Za-z0-9_.-]+")


def _limit(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, "").strip() or default))
    except ValueError:
        return default


def _decrypt_url() -> str:
    return os.getenv("AIMS_DECRYPT_URL", "").strip().rstrip("/")


def _client() -> httpx.AsyncClient:
    """To the archive worker. A long read timeout: a chunk is 4 MB of audio."""
    return httpx.AsyncClient(timeout=httpx.Timeout(30.0, read=120.0))


class _Refused(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status, self.message = status, message


def _refusal(request: Request, status: int, message: str):
    """A page for a browser following the link; JSON for everything else."""
    if "text/html" in (request.headers.get("accept") or ""):
        return portal._page("Download not available",
                            f"<p>{html.escape(message)}</p>"
                            "<p><a href='/dashboard'>Back to the dashboard</a></p>",
                            status)
    raise HTTPException(status_code=status, detail=message)


COLLECTOR_NO_FILES = "A data collector can view recordings but not download or play them."
CMED_NO_AUDIO = ("CMED staff can download the clinical record (JSON), not the audio.")


async def may_download(person: "portal.Person", kind: str,
                       hospital_id: Optional[str]) -> None:
    """
    Raise _Refused unless this person may download this kind of file - "audio"
    or "json" - of a recording from this hospital. hospital_id None means a
    selection of every hospital (a batch of "All clinics").
    """
    if person.role == "collector":
        raise _Refused(403, COLLECTOR_NO_FILES)
    if person.role == "cmed":
        if kind != "json":
            raise _Refused(403, CMED_NO_AUDIO)
        return
    if person.role == "researcher":
        import watchers
        mine = await watchers.watched_hospitals(person.email)
        if not mine:
            raise _Refused(403, "Not your hospital: no hospital is assigned to you, so you "
                                "can view recordings but not download them. An admin "
                                "assigns hospitals on the People page.")
        if hospital_id is None:
            raise _Refused(403, "Choose one of your hospitals: " + ", ".join(mine) + ".")
        if hospital_id not in mine:
            raise _Refused(403, f"Not your hospital: {hospital_id}. You can download the "
                                f"recordings of " + ", ".join(mine) + " only.")
        return
    if person.role != "admin":
        raise _Refused(403, "This account cannot download recordings.")


def _stem_of(object_key: str) -> Optional[str]:
    """`copies/.../<stem>.v2.wav.enc` (or `.json.enc`) -> `<stem>`, as restore.py reads it."""
    found = _KEY_NAME.match(object_key.rsplit("/", 1)[-1])
    return found.group("stem") if found else None


async def _the_copy(session_id: str, part: Optional[int],
                    kind: str = "audio") -> Dict[str, Any]:
    """The newest copy of one kind - audio or the clinical JSON - of this
    recording, or of one of its patient files."""
    repo = api_v2._repo()
    session = await repo.get_session(session_id)
    if session is None:
        raise _Refused(404, "No such recording.")
    if session.get("confirmation") != "confirmed":
        raise _Refused(409, "Only recordings CMED has confirmed are kept, and only "
                            "they can be downloaded.")
    parts: List[Dict[str, Any]] = await repo.parts_for(session_id)
    chosen = None
    if part is not None:
        chosen = next((p for p in parts if p["part_no"] == part), None)
        if chosen is None:
            raise _Refused(404, f"This recording has no patient file {part}.")
    elif len(parts) > 1:
        raise _Refused(400, f"This recording was cut into {len(parts)} patient files: "
                            f"choose one.")
    elif len(parts) == 1:
        chosen = parts[0]
    stem = (chosen or {}).get("file_stem") or session.get("file_stem")
    copies = [c for c in await repo.copies_for(session_id)
              if c["kind"] == kind and _stem_of(c["object_key"]) == stem]
    if not stem or not copies:
        if kind == "json":
            raise _Refused(409, "This recording has no copy of its clinical record yet. "
                                "It is made once CMED has sent the record; try again later.")
        raise _Refused(409, "This recording has no cloud copy yet. It is made within "
                            "minutes of archiving; try again later.")
    copy = max(copies, key=lambda c: c["version"])
    if not copy.get("plain_sha256"):
        raise _Refused(409, "This copy has no recorded fingerprint, so a download "
                            "could not be checked.")
    expect: Dict[str, Any] = {"session_id": session_id, "kind": kind,
                              "stem_sha256": hashlib.sha256(stem.encode("utf-8")).hexdigest()}
    if chosen is not None:
        expect["part_no"] = chosen["part_no"]
    day = session.get("session_date")
    day = day.isoformat() if hasattr(day, "isoformat") else str(day or "undated")
    doctor = (chosen or {}).get("doctor_id") or session.get("doctor_id") or ""
    name = "_".join(str(x) for x in (session.get("hospital_id"), doctor, day, session_id) if x)
    if chosen is not None and len(parts) > 1:
        name += f"_part{chosen['part_no']}"
    return {"object_key": copy["object_key"], "plain_sha256": bytes(copy["plain_sha256"]).hex(),
            "expect": expect, "part_no": (chosen or {}).get("part_no"),
            "filename": _SAFE_NAME.sub("-", name)[:150] + "." + _KINDS[kind][0]}


async def _within_limits(conn, email: str, role: str = "researcher") -> None:
    """At most a few at once for anyone; a day's limit for researchers, none for an admin."""
    per_day = _limit("AIMS_DOWNLOAD_DAILY_LIMIT", 50) if role != "admin" else 10**9
    at_once = _limit("AIMS_DOWNLOAD_AT_ONCE", 2)
    today, running = await conn.fetchrow("""
        SELECT count(*) FILTER (WHERE started_at > now() - interval '1 day'),
               count(*) FILTER (WHERE outcome = 'started'
                                  AND started_at > now() - interval '2 hours')
          FROM portal_downloads WHERE email = $1
    """, email)
    if running >= at_once:
        raise _Refused(429, f"You already have {running} download(s) running. Wait for "
                            f"one to finish.")
    if today >= per_day:
        raise _Refused(429, f"You have reached today's limit of {per_day} downloads. "
                            f"An admin can raise it (AIMS_DOWNLOAD_DAILY_LIMIT).")


async def _finish(row_id: int, email: str, session_id: str, part_no: Optional[int],
                  ok: bool, sent: int, reason: str = "") -> None:
    try:
        async with portal._pool().acquire() as conn:
            await conn.execute("""
                UPDATE portal_downloads
                   SET finished_at = now(), outcome = $2, bytes = $3, reason = NULLIF($4, '')
                 WHERE id = $1
            """, row_id, "completed" if ok else "failed", sent, reason[:500])
        await api_v2._repo().audit(
            event_type="recording.downloaded" if ok else "recording.download_failed",
            actor_type="admin", actor_id=email, session_id=session_id,
            detail={"download": row_id, "part_no": part_no, "bytes": sent,
                    **({"reason": reason[:200]} if reason else {})})
    except Exception as exc:                       # never breaks the download itself
        logger.warning("Could not record the end of download %s: %s", row_id, exc)


@router.get("/recordings/{session_id}/audio")
async def download_audio(session_id: str, request: Request, part: Optional[int] = None):
    return await _download(session_id, request, part, "audio")


@router.get("/recordings/{session_id}/record")
async def download_record(session_id: str, request: Request, part: Optional[int] = None):
    """The clinical record CMED sent, decrypted from its sealed copy, as the audio is."""
    return await _download(session_id, request, part, "json")


async def _download(session_id: str, request: Request, part: Optional[int], kind: str):
    person = await portal.person_for(request)
    if person is None:
        return _refusal(request, 401, "Sign in with Google to download recordings.")
    if person.role in ("collector", "cmed"):
        # 036: data collectors view only - no file, no batch, no playback.
        # 049: CMED staff the clinical record only. Refused before any lookup.
        try:
            await may_download(person, kind, None)
        except _Refused as refused:
            return _refusal(request, refused.status, refused.message)
    # A link on another site may not start a download in this browser's name.
    if (request.headers.get("sec-fetch-site") or "same-origin") not in ("same-origin", "none"):
        return _refusal(request, 403, "Downloads start from the dashboard only.")
    if not _decrypt_url():
        return _refusal(request, 503, "Downloads are not set up on this server "
                                      "(AIMS_DECRYPT_URL).")
    sid = api_v2._sid(session_id)
    try:
        if person.role == "researcher":
            # 049: only the hospitals assigned to them - asked before anything
            # about the recording's copies is told.
            session = await api_v2._repo().get_session(sid)
            if session is None:
                raise _Refused(404, "No such recording.")
            await may_download(person, kind, session.get("hospital_id"))
        copy = await _the_copy(sid, part, kind)
        async with portal._pool().acquire() as conn:
            await _within_limits(conn, person.email, person.role)
            row_id = await conn.fetchval("""
                INSERT INTO portal_downloads (email, session_id, part_no, object_key, address)
                VALUES ($1, $2, $3, $4, $5) RETURNING id
            """, person.email, sid, copy["part_no"], copy["object_key"],
                request.client.host if request.client else None)
    except _Refused as refused:
        return _refusal(request, refused.status, refused.message)

    store = api_v2._copy_bucket(kind)
    link = await asyncio.get_event_loop().run_in_executor(
        None, store.get_presigned_download_url, copy["object_key"], 300)
    client = _client()
    try:
        answer = await client.send(client.build_request(
            "POST", _decrypt_url() + "/decrypt",
            headers={"X-Worker-Key": os.getenv("AIMS_WORKER_KEY", "")},
            json={"url": link, "expect": copy["expect"],
                  "plain_sha256": copy["plain_sha256"]}), stream=True)
    except httpx.HTTPError as exc:
        await client.aclose()
        await _finish(row_id, person.email, sid, copy["part_no"], False, 0,
                      f"the archive worker could not be reached: {type(exc).__name__}")
        return _refusal(request, 503, "The archive worker is not answering. Try again "
                                      "in a few minutes.")
    if answer.status_code != 200:
        try:
            detail = (await answer.aread()).decode("utf-8", "replace")
            detail = json.loads(detail).get("detail", detail)
        except Exception:
            detail = f"status {answer.status_code}"
        await answer.aclose()
        await client.aclose()
        await _finish(row_id, person.email, sid, copy["part_no"], False, 0, str(detail))
        if answer.status_code == 429:
            return _refusal(request, 503, "The server is decrypting other downloads. "
                                          "Try again in a minute.")
        if answer.status_code == 409:
            return _refusal(request, 409, "This copy did not pass its check and was not "
                                          "sent. The administrators have been told: "
                                          + str(detail))
        return _refusal(request, 502, "The copy could not be read from the cloud store.")

    size = int(answer.headers.get("content-length") or 0)
    await api_v2._repo().audit(
        event_type="recording.download_started", actor_type="admin", actor_id=person.email,
        session_id=sid, detail={"download": row_id, "part_no": copy["part_no"],
                                "object_key": copy["object_key"], "bytes": size})

    async def body():
        sent, ok, reason = 0, False, ""
        try:
            async for block in answer.aiter_bytes():
                sent += len(block)
                yield block
            ok = sent == size
            if not ok:
                reason = "cut short by the archive worker: it did not pass its check"
        except Exception as exc:
            reason = f"stopped: {type(exc).__name__}"
            raise
        finally:
            try:
                await answer.aclose()
                await client.aclose()
            finally:
                await _finish(row_id, person.email, sid, copy["part_no"], ok, sent, reason)

    return StreamingResponse(body(), media_type=_KINDS[kind][1], headers={
        "Content-Length": str(size),
        "Content-Disposition": f'attachment; filename="{copy["filename"]}"',
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
    })


# ============================================================
# Listening on the dashboard (26 Nov 2026)
# ============================================================
#
# A researcher or an admin plays a recording in the page - play, pause, drag
# to any point - without downloading it. The browser's player asks for byte
# ranges; each is answered by the archive worker's /range, which fetches and
# decrypts only the 4 MB chunks that hold it. So a jump to minute 7 costs one
# or two chunks, and nothing decrypted is written to any disk or kept in the
# browser's cache. The same people as a download (may_download, "audio"), the
# same same-site rule; every listen is in the audit log (recording.listened,
# once per person, recording and half hour). Listening is not a download: it
# is not in portal_downloads and does not count towards the daily limit.

LISTEN_AUDIT_SECONDS = 30 * 60
_COPY_SECONDS = 300.0
_LINK_SECONDS = 240.0
_listen_copies: Dict[Any, Any] = {}       # (session, part) -> (until, session row, copy)
_listen_links: Dict[str, Any] = {}        # object key -> (until, presigned link)
_listen_sizes: Dict[str, int] = {}        # object key -> the WAV's size
_listen_heard: Dict[Any, float] = {}      # (email, session, part) -> when logged
_listening: Dict[str, int] = {}           # email -> answers being sent now
_RANGE = re.compile(r"^bytes=(\d*)-(\d*)$")


class _Gone(Exception):
    """The worker could not answer a part of the audio."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status, self.message = status, message


def _now() -> float:
    import time
    return time.monotonic()


async def _listen_target(sid: str, part: Optional[int], person: "portal.Person"):
    """The recording's row and its newest audio copy, after the person is checked."""
    key = (sid, part)
    held = _listen_copies.get(key)
    if held is None or held[0] < _now():
        session = await api_v2._repo().get_session(sid)
        if session is None:
            raise _Refused(404, "No such recording.")
        # 049: whether this person may hear it is asked before anything about
        # the recording's copies is told.
        await may_download(person, "audio", session.get("hospital_id"))
        copy = await _the_copy(sid, part, "audio")
        held = (_now() + _COPY_SECONDS, session, copy)
        _listen_copies[key] = held
        if len(_listen_copies) > 2000:
            _listen_copies.clear()
    else:
        await may_download(person, "audio", held[1].get("hospital_id"))
    return held[1], held[2]


async def _listen_link(object_key: str) -> str:
    held = _listen_links.get(object_key)
    if held is None or held[0] < _now():
        store = api_v2._copy_bucket("audio")
        link = await asyncio.get_event_loop().run_in_executor(
            None, store.get_presigned_download_url, object_key, 300)
        held = (_now() + _LINK_SECONDS, link)
        _listen_links[object_key] = held
        if len(_listen_links) > 2000:
            _listen_links.clear()
    return held[1]


async def _window(client: httpx.AsyncClient, copy: Dict[str, Any], start: int,
                  end: Optional[int]):
    """(the WAV's size, first byte, last byte, the bytes) from the worker's /range."""
    body = {"url": await _listen_link(copy["object_key"]), "expect": copy["expect"],
            "start": start, "end": end}
    for attempt in range(3):
        try:
            answer = await client.post(_decrypt_url() + "/range", json=body, headers={
                "X-Worker-Key": os.getenv("AIMS_WORKER_KEY", "")})
        except httpx.HTTPError as exc:
            raise _Gone(503, f"the archive worker could not be reached: "
                             f"{type(exc).__name__}")
        if answer.status_code == 429 and attempt < 2:
            await asyncio.sleep(0.4)
            continue
        break
    size = answer.headers.get("x-plain-size")
    if size is not None:
        _listen_sizes[copy["object_key"]] = int(size)
    if answer.status_code == 200:
        return (int(size), int(answer.headers["x-start"]), int(answer.headers["x-end"]),
                answer.content)
    if answer.status_code == 416:
        raise _Gone(416, "past the end of the audio")
    try:
        detail = answer.json().get("detail", "")
    except Exception:
        detail = f"status {answer.status_code}"
    if answer.status_code == 429:
        raise _Gone(503, "The server is busy playing other recordings. Try again in a "
                         "moment.")
    if answer.status_code == 409:
        logger.error("Listening to %s refused by the worker: %s",
                     copy["expect"].get("session_id"), detail)
        raise _Gone(409, "This copy did not pass its check and cannot be played: "
                         + str(detail))
    raise _Gone(502, "The copy could not be read from the cloud store.")


def _asked_range(header: Optional[str], size: Optional[int]):
    """(start, end or None) from a Range header; None when it is not one we answer."""
    found = _RANGE.match((header or "").strip())
    if not found or (found.group(1) == "" and found.group(2) == ""):
        return None
    if found.group(1) == "":                      # the last n bytes
        if size is None:
            return "suffix", int(found.group(2))
        n = int(found.group(2))
        return max(0, size - n), None
    start = int(found.group(1))
    end = int(found.group(2)) if found.group(2) else None
    if end is not None and end < start:
        return None
    return start, end


@router.get("/recordings/{session_id}/listen")
async def listen(session_id: str, request: Request, part: Optional[int] = None,
                 check: bool = False):
    """
    The recording's audio for the dashboard's player, a byte range at a time.
    `check=1`: may this person play it, and is there a copy - asked by the page
    before the player starts, so a refusal is shown in words.
    """
    person = await portal.person_for(request)
    if person is None:
        return _refusal(request, 401, "Sign in with Google to listen to recordings.")
    if (request.headers.get("sec-fetch-site") or "same-origin") not in ("same-origin", "none"):
        return _refusal(request, 403, "Recordings play from the dashboard only.")
    try:
        if person.role in ("collector", "cmed"):
            await may_download(person, "audio", None)
        if not _decrypt_url():
            raise _Refused(503, "Listening is not set up on this server (AIMS_DECRYPT_URL).")
        sid = api_v2._sid(session_id)
        session, copy = await _listen_target(sid, part, person)
    except _Refused as refused:
        return _refusal(request, refused.status, refused.message)
    if check:
        return {"ok": True, "session_id": sid, "part_no": copy["part_no"],
                "filename": copy["filename"],
                "bytes": _listen_sizes.get(copy["object_key"])}

    at_once = _limit("AIMS_LISTEN_AT_ONCE", 4)
    if _listening.get(person.email, 0) >= at_once:
        raise HTTPException(status_code=429, detail="Too many recordings playing at once "
                                                    "in your name. Stop one first.")
    size = _listen_sizes.get(copy["object_key"])
    asked = request.headers.get("range")
    wanted = _asked_range(asked, size) if asked else (0, None)
    if wanted is None:
        raise HTTPException(status_code=416, detail="Only one byte range is answered.")
    client = _client()
    try:
        if wanted[0] == "suffix":                 # the size first, from the first byte
            size = (await _window(client, copy, 0, 0))[0]
            wanted = (max(0, size - wanted[1]), None)
        start, end = wanted
        size, first, last, block = await _window(client, copy, start, end)
    except _Gone as gone:
        await client.aclose()
        if gone.status == 416:
            return Response(status_code=416, headers={
                "Content-Range": f"bytes */{_listen_sizes.get(copy['object_key'], 0)}"})
        return _refusal(request, gone.status, gone.message)
    end = size - 1 if end is None else min(end, size - 1)

    heard = (person.email, sid, copy["part_no"])
    if _listen_heard.get(heard, 0.0) < _now() - LISTEN_AUDIT_SECONDS:
        _listen_heard[heard] = _now()
        try:
            await api_v2._repo().audit(
                event_type="recording.listened", actor_type="admin", actor_id=person.email,
                session_id=sid, detail={"part_no": copy["part_no"],
                                        "object_key": copy["object_key"], "from_byte": start})
        except Exception as exc:                   # never stops the audio itself
            logger.warning("Could not log a listen to %s: %s", sid, exc)

    async def body():
        _listening[person.email] = _listening.get(person.email, 0) + 1
        try:
            yield block
            following = last + 1
            while following <= end:
                _, _, upto, more = await _window(client, copy, following, end)
                yield more
                following = upto + 1
        except _Gone as gone:
            logger.warning("Listening to %s stopped: %s", sid, gone.message)
            raise
        finally:
            _listening[person.email] = max(0, _listening.get(person.email, 1) - 1)
            await client.aclose()

    headers = {"Accept-Ranges": "bytes", "Content-Length": str(end - start + 1),
               "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
               "Content-Disposition": "inline"}
    if asked:
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    return StreamingResponse(body(), status_code=206 if asked else 200,
                             media_type="audio/wav", headers=headers)


__all__ = ["router"]
