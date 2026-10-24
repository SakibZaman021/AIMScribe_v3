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

Anyone on the portal list may download - researchers and admins - and each
download is a row in portal_downloads and an entry in the audit log: who,
which recording, when, and whether it all arrived. The file is named by
clinic, doctor, date and session, without the patient's identifier.
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
from fastapi.responses import StreamingResponse

import api_v2
import portal

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v2/dashboard", tags=["downloads"])

_KEY_NAME = re.compile(r"^(?P<stem>.+)\.v(?P<version>\d+)\.wav\.enc$")
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


def _stem_of(object_key: str) -> Optional[str]:
    """`copies/.../<stem>.v2.wav.enc` -> `<stem>`, as restore.py reads it."""
    found = _KEY_NAME.match(object_key.rsplit("/", 1)[-1])
    return found.group("stem") if found else None


async def _the_copy(session_id: str, part: Optional[int]) -> Dict[str, Any]:
    """The newest audio copy of this recording, or of one of its patient files."""
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
              if c["kind"] == "audio" and _stem_of(c["object_key"]) == stem]
    if not stem or not copies:
        raise _Refused(409, "This recording has no cloud copy yet. It is made within "
                            "minutes of archiving; try again later.")
    copy = max(copies, key=lambda c: c["version"])
    if not copy.get("plain_sha256"):
        raise _Refused(409, "This copy has no recorded fingerprint, so a download "
                            "could not be checked.")
    expect: Dict[str, Any] = {"session_id": session_id, "kind": "audio",
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
            "filename": _SAFE_NAME.sub("-", name)[:150] + ".wav"}


async def _within_limits(conn, email: str) -> None:
    per_day = _limit("AIMS_DOWNLOAD_DAILY_LIMIT", 50)
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
    person = await portal.person_for(request)
    if person is None:
        return _refusal(request, 401, "Sign in with Google to download recordings.")
    # A link on another site may not start a download in this browser's name.
    if (request.headers.get("sec-fetch-site") or "same-origin") not in ("same-origin", "none"):
        return _refusal(request, 403, "Downloads start from the dashboard only.")
    if not _decrypt_url():
        return _refusal(request, 503, "Downloads are not set up on this server "
                                      "(AIMS_DECRYPT_URL).")
    sid = api_v2._sid(session_id)
    try:
        copy = await _the_copy(sid, part)
        async with portal._pool().acquire() as conn:
            await _within_limits(conn, person.email)
            row_id = await conn.fetchval("""
                INSERT INTO portal_downloads (email, session_id, part_no, object_key, address)
                VALUES ($1, $2, $3, $4, $5) RETURNING id
            """, person.email, sid, copy["part_no"], copy["object_key"],
                request.client.host if request.client else None)
    except _Refused as refused:
        return _refusal(request, refused.status, refused.message)

    store = api_v2._copy_bucket("audio")
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

    return StreamingResponse(body(), media_type="audio/wav", headers={
        "Content-Length": str(size),
        "Content-Disposition": f'attachment; filename="{copy["filename"]}"',
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
    })


__all__ = ["router"]
