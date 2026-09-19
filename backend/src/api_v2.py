"""
AIMScribe protocol 2 API - integrity, device identity, and the archive handshake.

Mounted alongside the existing v1 routes so the transcription and NER pipeline is
untouched. v1 endpoints stay open for now (see SECURITY note in main_fastapi);
everything here requires authentication.

The three flows:

  AGENT     enroll -> session/open -> segment/authorize -> segment/commit
                   -> session/close -> poll receipts -> delete local audio

  WORKER    archive/pending -> download from R2 -> write the sorted tree
                            -> archive/complete -> receipts are issued

  ADMIN     create a hospital, mint enrollment tokens, revoke a device

The security property that matters most lives in `/segment/commit`: the server
re-reads the uploaded object and recomputes its SHA-256 before storing anything.
A client's claim about what it uploaded is never taken on trust.
"""
from __future__ import annotations

import asyncio
import hmac
import logging
import os
import re
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field

import integrity
from db_v2 import V2Repository
from integrity import ChainError, ReceiptSigner, parse_entry, safe_identifier, safe_session_id

logger = logging.getLogger(__name__)

# Identifiers allowed into a filename. Anything else and the clip goes unnamed
# rather than letting a separator or a path fragment into the archive.
_NAME_SAFE = re.compile(r"^[A-Za-z0-9-]{1,64}$")

router = APIRouter(prefix="/api/v2", tags=["v2"])


# ============================================================
# Context - populated by main_fastapi during startup
# ============================================================

class V2Context:
    """Shared handles. Kept out of module globals so tests can build their own."""

    def __init__(self) -> None:
        self.repo: Optional[V2Repository] = None
        self.minio = None
        self.redis = None
        self.legacy_db = None
        self.signer: Optional[ReceiptSigner] = None

    @property
    def ready(self) -> bool:
        return self.repo is not None


ctx = V2Context()


def _repo() -> V2Repository:
    if not ctx.ready:
        raise HTTPException(status_code=503, detail="v2 layer is not initialised")
    return ctx.repo


# ============================================================
# Authentication
#
# The v1 API has none at all. Every route below requires a credential.
# ============================================================

async def require_device(
    x_device_token: Optional[str] = Header(None),
) -> Dict[str, Any]:
    """Authenticate an agent by its enrollment-issued bearer token."""
    if not x_device_token:
        raise HTTPException(status_code=401, detail="device token required")

    device = await _repo().device_by_token(x_device_token)
    if device is None:
        raise HTTPException(status_code=401, detail="unknown device token")
    if device["revoked_at"] is not None:
        raise HTTPException(status_code=403, detail="device has been revoked")
    return device


def _check_static_key(supplied: Optional[str], env_var: str, label: str) -> None:
    expected = os.getenv(env_var, "")
    if not expected:
        raise HTTPException(status_code=503, detail=f"{label} key is not configured")
    if not supplied or not hmac.compare_digest(supplied, expected):
        raise HTTPException(status_code=401, detail=f"invalid {label} key")


async def require_admin(x_admin_key: Optional[str] = Header(None)) -> None:
    _check_static_key(x_admin_key, "AIMS_ADMIN_KEY", "admin")


async def require_worker(x_worker_key: Optional[str] = Header(None)) -> None:
    """The AIMS LAB archive worker. Outbound-only; it holds this key."""
    _check_static_key(x_worker_key, "AIMS_WORKER_KEY", "worker")


# ============================================================
# Models
# ============================================================

class AudioSpec(BaseModel):
    sample_rate: int = Field(..., ge=8000, le=192000)
    channels: int = Field(..., ge=1, le=2)
    sample_width: int = Field(..., ge=1, le=4)


class EnrollRequest(BaseModel):
    enrollment_token: str = Field(..., min_length=16, max_length=256)
    device_pubkey: str = Field(..., min_length=64, max_length=64)
    machine_name: str = Field("", max_length=128)
    os_version: str = Field("", max_length=256)
    app_version: str = Field("", max_length=32)
    protocol_version: int = Field(2, ge=2, le=9)
    audio: Optional[AudioSpec] = None


class OpenSessionRequest(BaseModel):
    session_id: str
    opened_at: Optional[str] = None
    doctor_id: str = Field(..., max_length=64)
    hospital_id: str = Field(..., max_length=64)
    patient_ref: str = Field(..., max_length=64)
    consent_obtained: bool
    consent_method: str = Field("", max_length=64)
    audio: AudioSpec
    device_pubkey: str = Field("", max_length=64)
    genesis: Dict[str, Any]


class AuthorizeRequest(BaseModel):
    session_id: str
    seq_no: int = Field(..., ge=1, le=10000)
    bytes: int = Field(..., ge=1)
    sha256: str = Field(..., min_length=64, max_length=64)


class CommitRequest(BaseModel):
    session_id: str
    seq_no: int = Field(..., ge=1, le=10000)
    object_key: str = Field(..., max_length=512)
    sha256: str = Field(..., min_length=64, max_length=64)
    bytes: int = Field(..., ge=1)
    duration_seconds: float = Field(..., ge=0, le=3600)
    captured_start_at: Optional[str] = None
    captured_end_at: Optional[str] = None
    rms_mean: Optional[float] = None
    is_final: bool = False
    chain_entry: Dict[str, Any]


class ChainEntryRequest(BaseModel):
    session_id: str
    chain_entry: Dict[str, Any]


class CloseRequest(BaseModel):
    session_id: str
    closed_at: Optional[str] = None
    close_reason: str = Field("", max_length=64)
    duration_seconds: float = Field(0, ge=0)
    paused_seconds: float = Field(0, ge=0)
    segment_count: int = Field(0, ge=0)
    chain_head: Optional[str] = None
    chain_entry: Optional[Dict[str, Any]] = None
    manifest: Dict[str, Any] = Field(default_factory=dict)


class HeartbeatRequest(BaseModel):
    device_id: str = ""
    app_version: str = ""
    state: str = ""
    session_id: Optional[str] = None
    spool_bytes: int = 0
    spool_pressure: str = ""
    pending_segments: int = 0
    sent_at: Optional[str] = None


class ArchiveCompleteRequest(BaseModel):
    session_id: str
    archive_relpath: str = Field(..., max_length=512)
    sha256: str = Field(..., min_length=64, max_length=64)
    bytes: int = Field(..., ge=1)


# ============================================================
# Helpers
# ============================================================

def _parse_time(value: Optional[str]) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(timezone.utc)


async def _verified_duplicate(session_id: str, entry, device) -> bool:
    """
    Is this the entry the backend already holds, arriving a second time?

    A retry is verified in full - payload hash, entry hash, device signature -
    against its *own* prev_hash. That runs every check except the comparison
    with the stored head, which is the only one a legitimate retry can fail:
    the head has moved past the entry precisely because it was already accepted.

    Verifying matters. `parse_entry` carries the entry_hash it was given rather
    than recomputing it, so a tampered payload sent with the original hash would
    otherwise be waved through on the strength of that hash alone.
    """
    if not await _repo().entry_already_stored(session_id, entry):
        return False
    verdict = integrity.verify_entry(
        entry, expected_prev=entry.prev_hash, device_pubkey=bytes(device["tpm_pubkey"]))
    if not verdict.ok:
        logger.warning("Entry %s of %s claims a stored hash but fails verification: %s",
                       entry.entry_no, session_id, verdict.reason)
    return verdict.ok


async def _local_time(hospital_id: str, moment: datetime) -> datetime:
    """The same instant on the hospital's wall clock."""
    tz_name = await _repo().hospital_timezone(hospital_id)
    try:
        from zoneinfo import ZoneInfo
        return moment.astimezone(ZoneInfo(tz_name))
    except Exception:
        logger.error("Timezone %r unresolvable; using UTC. Install tzdata.", tz_name)
        return moment.astimezone(timezone.utc)


async def _local_date(hospital_id: str, moment: datetime) -> date:
    """
    The archive folder uses the hospital's local date.

    Using UTC would scatter evening consultations across two folders, which is
    both confusing to browse and wrong on the record.
    """
    tz_name = await _repo().hospital_timezone(hospital_id)
    try:
        from zoneinfo import ZoneInfo
        return moment.astimezone(ZoneInfo(tz_name)).date()
    except Exception:
        # session_date decides which folder the recording lands in, permanently.
        # At UTC+6 an evening consultation would be filed under the previous day.
        logger.error(
            "Timezone %r could not be resolved - using the UTC date. Sessions will "
            "be filed under the wrong day for any hospital not on UTC. Install the "
            "tzdata package.", tz_name)
        return moment.astimezone(timezone.utc).date()


async def _entry_or_400(raw: Dict[str, Any]):
    try:
        return parse_entry(raw)
    except ChainError as exc:
        raise HTTPException(status_code=400, detail=f"invalid chain entry: {exc}")


async def _session_for_device(session_id: str, device: Dict[str, Any]) -> Dict[str, Any]:
    """Load a session and confirm this device owns it. Prevents cross-device writes."""
    session = await _repo().get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    if str(session["device_id"]) != str(device["device_id"]):
        raise HTTPException(status_code=403, detail="session belongs to another device")
    return session


# ============================================================
# Enrollment
# ============================================================

@router.post("/device/enroll")
async def enroll_device(body: EnrollRequest):
    """
    Exchange an administrator's one-time token for a device identity.

    Deliberately unauthenticated apart from the token itself - the device has no
    credential yet. The token is single-use, expiring, and carries both the
    hospital and the doctor, so a device can never assert its own identity.
    """
    try:
        pubkey = bytes.fromhex(body.device_pubkey)
    except ValueError:
        raise HTTPException(status_code=400, detail="device_pubkey must be hex")
    if len(pubkey) != 32:
        raise HTTPException(status_code=400, detail="device_pubkey must be 32 bytes")

    result = await _repo().enroll_device(
        token=body.enrollment_token,
        tpm_pubkey=pubkey,
        machine_name=body.machine_name,
        os_version=body.os_version,
        app_version=body.app_version,
        protocol_version=body.protocol_version,
    )
    if result is None:
        # One message for unknown, expired and already-used: an attacker learns
        # nothing about which it was.
        raise HTTPException(status_code=401, detail="enrollment token is not valid")

    await _repo().audit(
        event_type="device.enrolled", actor_type="admin",
        device_id=result["device_id"],
        detail={"machine_name": body.machine_name, "app_version": body.app_version},
    )
    logger.info("Enrolled device %s for doctor %s at hospital %s",
                result["device_id"], result["doctor_id"], result["hospital_id"])
    return result


# ============================================================
# Session lifecycle
# ============================================================

@router.post("/session/open")
async def open_session(body: OpenSessionRequest, device=Depends(require_device)):
    if not body.consent_obtained:
        raise HTTPException(status_code=400, detail="patient consent is required")

    try:
        session_id = safe_session_id(body.session_id)
        doctor_id = safe_identifier(body.doctor_id, field="doctor_id")
        patient_ref = safe_identifier(body.patient_ref, field="patient_ref")
        hospital_id = safe_identifier(body.hospital_id, field="hospital_id")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    # CMED names the hospital as well as the doctor, and both are recorded as
    # sent. Overriding the hospital with the device's meant a consultation
    # triggered for one site was filed under another, silently and in the
    # filename. A disagreement is worth knowing about - a laptop does not
    # usually move - so it is raised, but it does not change the record.
    if hospital_id != device["hospital_id"]:
        await _repo().raise_alert(
            alert_type="hospital_mismatch", severity="warning",
            session_id=session_id, device_id=device["device_id"],
            detail={"device_hospital": device["hospital_id"], "claimed": hospital_id},
        )

    # CMED decides who the doctor is, and this does not second-guess it.
    #
    # Doctors log in to CMED; it knows who is on shift, who was hired this week,
    # and who is covering another site today. Refusing a doctor this service had
    # not been told about stopped real consultations - the identity was never
    # AIMScribe's to verify. What is still enforced is the device: an unenrolled
    # laptop cannot reach this endpoint at all, which is the property that
    # actually matters.
    #
    # The register is kept as a directory rather than a gate, so names and
    # history stay readable and a doctor new to a site appears without anyone
    # filing a request. Both writes are bookkeeping and are wrapped: a site this
    # service has never heard of must not be the reason a consultation fails.
    try:
        await _repo().upsert_hospital(hospital_id, hospital_id,
                                      os.getenv("AIMS_DEFAULT_TIMEZONE", "Asia/Dhaka"),
                                      only_if_new=True)
        await _repo().upsert_doctor(doctor_id=doctor_id, hospital_id=hospital_id,
                                    full_name=doctor_id, only_if_new=True)
    except Exception as exc:
        logger.warning("Could not record %s/%s in the directory: %s",
                       hospital_id, doctor_id, exc)

    genesis = await _entry_or_400(body.genesis)
    if genesis.entry_no != 0 or genesis.entry_type != "open":
        raise HTTPException(status_code=400, detail="genesis must be entry 0 of type open")

    verdict = integrity.verify_entry(
        genesis, expected_prev=None, device_pubkey=bytes(device["tpm_pubkey"]))
    if not verdict.ok:
        await _repo().raise_alert(
            alert_type="genesis_rejected", severity="critical",
            session_id=session_id, device_id=device["device_id"],
            detail={"reason": verdict.reason})
        raise HTTPException(status_code=400, detail=f"genesis rejected: {verdict.reason}")

    opened_at = _parse_time(body.opened_at)
    local_opened = await _local_time(hospital_id, opened_at)
    await _repo().open_session(
        session_id=session_id,
        hospital_id=hospital_id,
        doctor_id=doctor_id,
        patient_id=patient_ref,
        device_id=device["device_id"],
        session_date=await _local_date(hospital_id, opened_at),
        opened_at=opened_at,
        object_prefix=object_prefix(patient_ref, doctor_id, hospital_id,
                                    local_opened, session_id),
        audio=body.audio.model_dump(),
        consent_method=body.consent_method,
        genesis=genesis,
    )

    # Keep the v1 patient row in step so the existing NER baseline lookup works.
    if ctx.legacy_db is not None:
        try:
            await ctx.legacy_db.upsert_patient({"patient_id": patient_ref})
        except Exception as exc:
            logger.debug("Legacy patient upsert skipped: %s", exc)

    await _repo().audit(
        event_type="session.opened", actor_type="device",
        actor_id=doctor_id, device_id=device["device_id"], session_id=session_id,
        detail={"hospital_id": hospital_id, "consent_method": body.consent_method},
    )
    return {"session_id": session_id, "status": "open", "hospital_id": hospital_id}


@router.post("/segment/authorize")
async def authorize_segment(body: AuthorizeRequest, device=Depends(require_device)):
    """Issue a short-lived presigned PUT for exactly one segment."""
    session = await _session_for_device(safe_session_id(body.session_id), device)

    max_bytes = int(os.getenv("AIMS_MAX_SEGMENT_BYTES", str(64 * 1024 * 1024)))
    if body.bytes > max_bytes:
        raise HTTPException(status_code=413, detail="segment exceeds the permitted size")

    # Keys are built from the opaque session ULID, never the patient reference:
    # object keys leak into access logs, metrics and error traces.
    # Readable where a human will see it. Falls back to the session ULID when
    # the prefix could not be formed, because a key that does not identify its
    # session is worse than one that cannot be read.
    prefix = session.get("object_prefix") or session["session_id"]
    name = unique_clip_name(session, body.seq_no) or f"seg_{body.seq_no:05d}.wav"
    object_key = f"audio/{prefix}/{name}"

    loop = asyncio.get_event_loop()
    upload_url = await loop.run_in_executor(
        None, ctx.minio.get_presigned_upload_url, object_key, 300)

    # Mirror v1's clip row so the existing transcription worker is unchanged.
    if ctx.legacy_db is not None:
        try:
            await ctx.legacy_db.save_clip_record(
                session["session_id"], body.seq_no, object_key)
        except Exception as exc:
            logger.debug("Legacy clip record skipped: %s", exc)

    return {"upload_url": upload_url, "object_key": object_key, "expires_in": 300}


@router.post("/segment/commit")
async def commit_segment(body: CommitRequest, device=Depends(require_device)):
    """
    Accept a segment only after re-reading it from storage and re-hashing it.

    v1 queued transcription on the client's word that the upload succeeded. Here
    the bytes are fetched back and hashed; a mismatch quarantines the session and
    the agent keeps its local copy.
    """
    repo = _repo()
    session_id = safe_session_id(body.session_id)
    session = await _session_for_device(session_id, device)

    # The client does not get to choose where its audio lands. Both forms are
    # accepted: sessions opened before readable keys still use the ULID.
    allowed = {f"audio/{session_id}/"}
    if session.get("object_prefix"):
        allowed.add(f"audio/{session['object_prefix']}/")
    if not any(body.object_key.startswith(p) for p in allowed):
        raise HTTPException(status_code=400, detail="object_key does not belong to this session")

    try:
        claimed = bytes.fromhex(body.sha256)
    except ValueError:
        raise HTTPException(status_code=400, detail="sha256 must be hex")

    # --- the verification that makes the chain meaningful ---
    loop = asyncio.get_event_loop()
    try:
        stored = await loop.run_in_executor(None, _read_object, body.object_key)
    except Exception as exc:
        logger.error("Could not read %s back from storage: %s", body.object_key, exc)
        raise HTTPException(status_code=502, detail="uploaded object could not be read back")

    actual = integrity.sha256_bytes(stored)
    if not hmac.compare_digest(actual, claimed):
        await repo.quarantine_session(session_id, "segment hash mismatch on arrival")
        await repo.raise_alert(
            alert_type="hash_mismatch", severity="critical",
            session_id=session_id, device_id=device["device_id"],
            detail={"seq_no": body.seq_no, "claimed": body.sha256, "actual": actual.hex()})
        await repo.audit(
            event_type="segment.rejected", actor_type="device",
            device_id=device["device_id"], session_id=session_id,
            detail={"seq_no": body.seq_no, "reason": "hash_mismatch"})
        return {"status": "quarantined", "reason": "uploaded bytes do not match the declared hash"}

    if len(stored) != body.bytes:
        raise HTTPException(status_code=400, detail="declared byte length does not match")

    entry = await _entry_or_400(body.chain_entry)

    # A commit whose response was lost gets retried, and the entry arriving the
    # second time is byte-identical to the one already stored. Verifying it
    # against a head that has moved past it judged an intact session a forgery
    # and quarantined it - which is exactly what happened to a real consultation:
    # ten clips, all present and correct on both sides, held out of the archive.
    #
    # A duplicate is answered as the success it is. Only the original entry
    # hashes to the stored value, so this cannot launder a tampered one.
    if await _verified_duplicate(session_id, entry, device):
        logger.info("Segment %s of %s was already committed; treating the retry "
                    "as the success it is", body.seq_no, session_id)
        return {"status": "committed", "seq_no": body.seq_no,
                "object_key": body.object_key, "duplicate": True}

    expected_prev = await repo.chain_head(session_id)
    verdict = integrity.verify_entry(
        entry, expected_prev=expected_prev, device_pubkey=bytes(device["tpm_pubkey"]))
    if not verdict.ok:
        await repo.quarantine_session(session_id, f"chain entry rejected: {verdict.reason}")
        await repo.raise_alert(
            alert_type="chain_entry_rejected", severity="critical",
            session_id=session_id, device_id=device["device_id"],
            detail={"seq_no": body.seq_no, "reason": verdict.reason})
        return {"status": "quarantined", "reason": verdict.reason}

    outcome = await repo.commit_segment(
        session_id=session_id,
        seq_no=body.seq_no,
        entry=entry,
        object_key=body.object_key,
        byte_length=body.bytes,
        duration_seconds=body.duration_seconds,
        sha256=claimed,
        rms_mean=body.rms_mean,
        captured_start_at=_parse_time(body.captured_start_at),
        captured_end_at=_parse_time(body.captured_end_at),
        is_final=body.is_final,
        clip_name=clip_name(session, body.seq_no),
    )

    if outcome == "conflict":
        # Same sequence number, different content. Either a bug or an attempt to
        # overwrite already-committed audio.
        await repo.quarantine_session(session_id, "duplicate seq_no with a different hash")
        await repo.raise_alert(
            alert_type="segment_conflict", severity="critical",
            session_id=session_id, device_id=device["device_id"],
            detail={"seq_no": body.seq_no})
        return {"status": "quarantined", "reason": "segment already committed with different content"}

    if outcome == "stored":
        await _queue_transcription(session_id, body, session)

    return {"status": "committed", "seq_no": body.seq_no, "duplicate": outcome == "duplicate"}


def object_prefix(patient: str, doctor: str, hospital: str,
                  local_opened: datetime, session_id: str) -> Optional[str]:
    """
    `{patient}_{doctor}_{hospital}_{HHMMSS}_{YYYYMMDD}_{tail}`

        10045_DR001_HOSP001_093012_20260728_X97HT

    The folder a session's clips live under in object storage, so the storage
    console can be read by a human. Clips used to sit under the session ULID,
    which is correct but leaves every folder as 26 random characters with no way
    to tell whose consultation you are about to download.

    Computed once at session open and stored, so segment authorisation and
    commit both work from the same value and a client cannot choose where its
    audio lands.

    Times are the hospital's local clock, matching the archive filename.

    Seconds and the last five characters of the session id are both here because
    a minute is not unique. Pressing Start for a new patient closes the current
    consultation and opens another in the same second; two sessions then shared
    a prefix, and since clip names carry no session either, the second session's
    first clip overwrote the first session's. Silently, in object storage, with
    both rows looking correct in the database.

    Returns None if any component is unsafe, and the caller falls back to the
    ULID: a key that does not match the session is worse than an unreadable one.
    """
    for value in (patient, doctor, hospital):
        if not value or not _NAME_SAFE.match(str(value)):
            return None
    return (f"{patient}_{doctor}_{hospital}"
            f"_{local_opened.strftime('%H%M%S')}"
            f"_{local_opened.strftime('%Y%m%d')}"
            f"_{session_id[-5:]}")


def clip_name(session: Dict[str, Any], seq_no: int) -> Optional[str]:
    """
    `{patient}_{doctor}_{hospital}_{YYYYMMDD}_{NNNN}.wav`

        10045_DR001_HOSP001_20260501_0001.wav

    The readable name for one clip, stored so the archive and the database can be
    searched by eye. Sequence numbers start at 1 and are contiguous within a
    session, so a gap in the numbering is itself evidence.

    Display only. The object key stays `audio/<ulid>/seg_00001.wav`, because keys
    reach Cloudflare's access logs and presigned URLs and a patient identifier
    must never appear there.

    Returns None rather than a partial name if any component is missing - a
    misleading filename in a clinical archive is worse than no filename.
    """
    patient = session.get("patient_id")
    doctor = session.get("doctor_id")
    hospital = session.get("hospital_id")
    date = session.get("session_date") or (
        session["opened_at"].date() if session.get("opened_at") else None)

    if not (patient and doctor and hospital and date):
        return None
    for value in (patient, doctor, hospital):
        if not _NAME_SAFE.match(str(value)):
            logger.warning("Clip name skipped: %r is not a safe identifier", value)
            return None

    return (f"{patient}_{doctor}_{hospital}"
            f"_{date.strftime('%Y%m%d')}_{seq_no:04d}.wav")


def unique_clip_name(session: Dict[str, Any], seq_no: int) -> Optional[str]:
    """
    The clip name with the session's tail appended.

    Used for the object key, where a collision overwrites audio. The readable
    name stored in segments.clip_name stays as it is - it is scoped to a session
    row and cannot collide there.
    """
    base = clip_name(session, seq_no)
    if base is None:
        return None
    return f"{base[:-len('.wav')]}_{str(session['session_id'])[-5:]}.wav"


async def _delete_bucket_objects(session_id: str) -> int:
    """
    Remove a session's clips from object storage.

    Called only after the archive copy is verified and receipts are signed. A
    failure is logged and left for the retry index rather than raised: the audio
    is already safe on the AIMS LAB server, and failing the request here would
    make the worker re-download and re-archive a session that is already done.
    """
    repo = _repo()
    loop = asyncio.get_event_loop()
    removed = 0

    for segment in await repo.segments_for(session_id):
        if segment.get("object_deleted_at") is not None:
            continue
        try:
            await loop.run_in_executor(None, _remove_object, segment["object_key"])
            await repo.mark_object_deleted(session_id, segment["seq_no"])
            removed += 1
        except Exception as exc:
            logger.warning("Could not delete %s from the bucket: %s",
                           segment["object_key"], exc)

    if removed:
        logger.info("Deleted %s clip(s) from the bucket for %s", removed, session_id)
    return removed


def _remove_object(object_key: str) -> None:
    """Delete one object. Sync; called in a thread."""
    ctx.minio.client.remove_object(ctx.minio.bucket, object_key)


def _read_object(object_key: str) -> bytes:
    """Fetch an object's bytes. Sync; called in a thread."""
    response = ctx.minio.client.get_object(ctx.minio.bucket, object_key)
    try:
        return response.read()
    finally:
        response.close()
        response.release_conn()


async def _queue_transcription(session_id: str, body: CommitRequest,
                               session: Dict[str, Any]) -> None:
    """Hand the segment to the existing v1 pipeline unchanged."""
    if ctx.redis is None:
        return
    try:
        from message_queue.redis_async import push_transcription_job_async
        await push_transcription_job_async(
            ctx.redis,
            session_id=session_id,
            clip_number=body.seq_no,
            object_key=body.object_key,
            patient_id=session["patient_id"],
            is_final=body.is_final,
        )
    except Exception as exc:
        # Transcription is valuable but not the record of truth. Losing a job must
        # not fail the commit that made the audio durable.
        logger.error("Could not queue transcription for %s seq %s: %s",
                     session_id, body.seq_no, exc)


@router.post("/session/pause")
async def pause_session(body: ChainEntryRequest, device=Depends(require_device)):
    return await _append_lifecycle_entry(body, device, "pause")


@router.post("/session/resume")
async def resume_session(body: ChainEntryRequest, device=Depends(require_device)):
    return await _append_lifecycle_entry(body, device, "resume")


async def _append_lifecycle_entry(body: ChainEntryRequest, device, expected_type: str):
    """
    Record a pause or resume in the chain.

    Best-effort from the agent's point of view - it does not block on this - so a
    duplicate or out-of-order arrival is tolerated. The authoritative copy comes
    in the manifest at close.
    """
    repo = _repo()
    session_id = safe_session_id(body.session_id)
    await _session_for_device(session_id, device)

    entry = await _entry_or_400(body.chain_entry)
    if entry.entry_type != expected_type:
        raise HTTPException(status_code=400, detail=f"expected a {expected_type} entry")

    # Same as a segment commit: a redelivered pause is not a chain violation.
    if await _verified_duplicate(session_id, entry, device):
        return {"status": "recorded", "entry_no": entry.entry_no, "duplicate": True}

    expected_prev = await repo.chain_head(session_id)
    verdict = integrity.verify_entry(
        entry, expected_prev=expected_prev, device_pubkey=bytes(device["tpm_pubkey"]))
    if not verdict.ok:
        logger.info("Deferring %s entry for %s: %s", expected_type, session_id, verdict.reason)
        return {"status": "deferred", "reason": verdict.reason}

    await repo.append_chain_entry(session_id, entry)
    await repo.audit(
        event_type=f"session.{expected_type}", actor_type="device",
        actor_id=str(entry.payload.get("authorised_by", "")),
        device_id=device["device_id"], session_id=session_id,
        detail={k: entry.payload.get(k) for k in ("reason", "reason_detail",
                                                  "authorised_by", "supervisor_required")},
    )
    return {"status": "recorded", "entry_no": entry.entry_no}


@router.post("/session/close")
async def close_session(body: CloseRequest, device=Depends(require_device)):
    """
    Close a session and verify its whole chain.

    Any gap, break or bad signature quarantines the session: it is held for review,
    never archived automatically, and the agent never gets a purge receipt for it.
    """
    repo = _repo()
    session_id = safe_session_id(body.session_id)
    await _session_for_device(session_id, device)

    if body.chain_entry:
        entry = await _entry_or_400(body.chain_entry)
        if entry.entry_type == "close":
            expected_prev = await repo.chain_head(session_id)
            verdict = integrity.verify_entry(
                entry, expected_prev=expected_prev,
                device_pubkey=bytes(device["tpm_pubkey"]))
            if verdict.ok:
                await repo.append_chain_entry(session_id, entry)

    chain = await repo.load_chain(session_id)
    verdict = integrity.verify_chain(chain, device_pubkey=bytes(device["tpm_pubkey"]))
    summary = integrity.chain_summary(chain)

    if not verdict.ok:
        await repo.quarantine_session(session_id, verdict.reason)
        await repo.raise_alert(
            alert_type="chain_invalid", severity="critical",
            session_id=session_id, device_id=device["device_id"],
            detail={"reason": verdict.reason, "failed_entry": verdict.failed_entry_no})
        await repo.audit(
            event_type="session.quarantined", actor_type="device",
            device_id=device["device_id"], session_id=session_id,
            detail={"reason": verdict.reason})
        return {"status": "quarantined", "reason": verdict.reason,
                "failed_entry_no": verdict.failed_entry_no}

    stored_segments = await repo.segments_for(session_id)
    if body.segment_count and len(stored_segments) != body.segment_count:
        # The agent recorded segments the server never received. Not fraud - most
        # likely an upload still pending - but the session is not complete.
        await repo.raise_alert(
            alert_type="segment_count_mismatch", severity="warning",
            session_id=session_id, device_id=device["device_id"],
            detail={"agent": body.segment_count, "server": len(stored_segments)})
        return {"status": "incomplete", "reason": "not all segments have been committed",
                "server_segments": len(stored_segments), "agent_segments": body.segment_count}

    chain_head = chain[-1].entry_hash if chain else None
    await repo.close_session(
        session_id,
        closed_at=_parse_time(body.closed_at),
        duration_seconds=body.duration_seconds,
        paused_seconds=body.paused_seconds,
        segment_count=len(stored_segments),
        chain_head=chain_head,
        manifest=body.manifest or {},
        close_reason=body.close_reason,
    )

    # A consultation normally ends because the doctor pressed Stop in CMED.
    # Anything else - stopped from the tray icon, superseded by the next patient,
    # recovered after the PC died mid-consultation - is worth someone's attention
    # the same morning, not a fact buried in a log file on a machine in a
    # consulting room.
    if body.close_reason and body.close_reason != "doctor_stopped":
        await repo.raise_alert(
            alert_type="abnormal_close", severity="warning",
            session_id=session_id, device_id=device["device_id"],
            detail={"reason": body.close_reason,
                    "duration_seconds": body.duration_seconds,
                    "segments": len(stored_segments)})
        logger.warning("Session %s ended abnormally: %s", session_id, body.close_reason)

    await repo.audit(
        event_type="session.closed", actor_type="device",
        device_id=device["device_id"], session_id=session_id,
        detail={"duration_seconds": body.duration_seconds,
                "paused_seconds": body.paused_seconds,
                "close_reason": body.close_reason or "doctor_stopped", **summary},
    )
    logger.info("Session %s closed and verified: %s", session_id, summary["entry_counts"])
    return {"status": "closed", "chain_ok": True, **summary}


@router.get("/session/{session_id}/receipts")
async def session_receipts(session_id: str, device=Depends(require_device)):
    """Purge receipts the agent may act on. Empty until the worker has archived."""
    sid = safe_session_id(session_id)
    await _session_for_device(sid, device)
    return {"session_id": sid, "receipts": await _repo().receipts_for(sid)}


@router.post("/heartbeat")
async def heartbeat(body: HeartbeatRequest, device=Depends(require_device)):
    """
    Liveness and spool depth.

    A missing heartbeat is how a killed agent or a stalled upload queue becomes
    visible centrally rather than being discovered weeks later.
    """
    await _repo().touch_device(
        device["device_id"],
        spool_bytes=body.spool_bytes,
        pending_segments=body.pending_segments,
        app_version=body.app_version or None,
    )
    if body.spool_pressure == "critical":
        await _repo().raise_alert(
            alert_type="spool_critical", severity="critical",
            device_id=device["device_id"], session_id=body.session_id,
            detail={"spool_bytes": body.spool_bytes,
                    "pending_segments": body.pending_segments})
    return {"status": "ok"}


# ============================================================
# Archive handshake (Option B - the AIMS LAB worker pulls)
# ============================================================

@router.get("/archive/pending")
async def archive_pending(limit: int = 10, _: None = Depends(require_worker)):
    """
    Sessions ready to be pulled into the sorted tree.

    Quarantined sessions never appear here: they are held for a human.
    """
    limit = max(1, min(limit, 50))
    sessions = await _repo().pending_archive(limit)
    loop = asyncio.get_event_loop()

    payload = []
    for session in sessions:
        segments = await _repo().segments_for(session["session_id"])

        # Presigned GETs rather than bucket credentials: the worker holds one
        # credential (its worker key) and gets time-limited access to exactly the
        # objects it needs. A compromised worker cannot enumerate the bucket.
        described = []
        for s in segments:
            url = await loop.run_in_executor(
                None, ctx.minio.get_presigned_download_url, s["object_key"], 3600)
            described.append({
                "seq_no": s["seq_no"],
                "object_key": s["object_key"],
                # Readable name for this clip. The worker never derives it, so
                # there is one implementation and it cannot drift.
                "clip_name": s.get("clip_name"),
                "download_url": url,
                "bytes": s["bytes"],
                "duration_seconds": float(s["duration_seconds"]),
                "sha256": bytes(s["sha256"]).hex(),
            })

        payload.append({
            "session_id": session["session_id"],
            "hospital_id": session["hospital_id"],
            "doctor_id": session["doctor_id"],
            "patient_ref": session["patient_id"],
            "session_date": session["session_date"].isoformat()
                            if session["session_date"] else None,
            # The worker needs this to name files by local wall-clock time, so a
            # folder's contents match the day the consultations happened.
            "timezone": await _repo().hospital_timezone(session["hospital_id"]),
            "opened_at": session["opened_at"].isoformat() if session["opened_at"] else None,
            "closed_at": session["closed_at"].isoformat() if session["closed_at"] else None,
            "audio": {
                "sample_rate": session["sample_rate"],
                "channels": session["channels"],
                "sample_width": session["sample_width"],
            },
            "manifest": session["manifest"],
            # Gaps in the audio and the reason each was authorised, taken from the
            # signed chain. Travels with the file so the AIMS LAB server can
            # explain a gap without reaching back to the cloud.
            "pauses": await _repo().pauses_for(session["session_id"]),
            "segments": described,
        })
    return {"sessions": payload}


@router.post("/archive/complete")
async def archive_complete(body: ArchiveCompleteRequest, _: None = Depends(require_worker)):
    """
    The worker reports a verified archive copy; the server issues purge receipts.

    This is the only place receipts are minted, and it happens only after the
    worker has re-read the archive file from disk and confirmed its hash. That is
    what makes deleting the agent's local copy safe.
    """
    repo = _repo()
    session_id = safe_session_id(body.session_id)
    session = await repo.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    if session["status"] == "quarantined":
        raise HTTPException(status_code=409, detail="session is quarantined")

    if ctx.signer is None:
        # Without a signing key nothing may authorise deletion. Failing here keeps
        # every local copy, which is the safe direction.
        raise HTTPException(status_code=503,
                            detail="receipt signing key is not configured")

    try:
        archive_hash = bytes.fromhex(body.sha256)
    except ValueError:
        raise HTTPException(status_code=400, detail="sha256 must be hex")

    await repo.mark_archived(session_id, relpath=body.archive_relpath,
                             sha256=archive_hash, byte_length=body.bytes)

    issued = 0
    now = datetime.now(timezone.utc)
    for segment in await repo.segments_for(session_id):
        receipt = ctx.signer.sign_segment(
            session_id=session_id,
            seq_no=segment["seq_no"],
            sha256_hex=bytes(segment["sha256"]).hex(),
            archived_at=now,
        )
        await repo.store_receipt(
            session_id=session_id, scope="segment", seq_no=segment["seq_no"],
            sha256=bytes(segment["sha256"]), payload=receipt["payload"],
            signature=bytes.fromhex(receipt["signature"]),
        )
        issued += 1

    session_receipt = ctx.signer.sign_session(
        session_id=session_id, sha256_hex=body.sha256, archived_at=now)
    await repo.store_receipt(
        session_id=session_id, scope="session", seq_no=None,
        sha256=archive_hash, payload=session_receipt["payload"],
        signature=bytes.fromhex(session_receipt["signature"]),
    )

    await repo.mark_segments_archived(session_id)

    # The bucket is transit. The AIMS LAB server holds a copy whose hash was
    # recomputed from disk, and every receipt is signed, so the clips have no
    # further purpose there. Deleting them last means a failure here costs storage,
    # never audio.
    removed = await _delete_bucket_objects(session_id)

    await repo.audit(
        event_type="session.archived", actor_type="service", actor_id="archive-worker",
        session_id=session_id,
        detail={"archive_relpath": body.archive_relpath, "receipts_issued": issued + 1,
                "objects_deleted": removed},
    )
    logger.info("Archived %s to %s; issued %s receipt(s), deleted %s object(s)",
                session_id, body.archive_relpath, issued + 1, removed)
    return {"status": "archived", "receipts_issued": issued + 1,
            "objects_deleted": removed}


# ============================================================
# Administration
# ============================================================

class HospitalRequest(BaseModel):
    hospital_id: str = Field(..., max_length=64)
    name: str = Field(..., max_length=256)
    timezone: str = Field("Asia/Dhaka", max_length=64)


class TokenRequest(BaseModel):
    hospital_id: str = Field(..., max_length=64)
    # Optional, and decides nothing. A laptop is shared across shifts, so the
    # doctor comes from CMED with each consultation; a name here only labels the
    # room in the paperwork.
    doctor_id: str = Field("", max_length=64)
    # Written into the audit trail against every device this token
    # enrols, and kept for the retention period. Name the team or the
    # role, not an individual who may leave.
    created_by: str = Field(..., max_length=128,
                            examples=["Team_AIMScribe"])
    ttl_hours: int = Field(72, ge=1, le=720)


@router.post("/admin/hospital")
async def admin_hospital(body: HospitalRequest, _: None = Depends(require_admin)):
    hospital_id = safe_identifier(body.hospital_id, field="hospital_id")
    await _repo().upsert_hospital(hospital_id, body.name, body.timezone)
    return {"status": "ok", "hospital_id": hospital_id}


class DoctorRequest(BaseModel):
    doctor_id: str = Field(..., max_length=64)
    hospital_id: str = Field(..., max_length=64)
    full_name: str = Field(..., max_length=128)
    active: bool = True


@router.post("/admin/doctor")
async def admin_doctor(body: DoctorRequest, _: None = Depends(require_admin)):
    """
    Add or update a doctor in a hospital's register.

    This is what allows a doctor to record at that hospital. Setting active to
    false stops new consultations without touching the ones already archived,
    which still resolve to a name.
    """
    doctor_id = safe_identifier(body.doctor_id, field="doctor_id")
    hospital_id = safe_identifier(body.hospital_id, field="hospital_id")
    await _repo().upsert_doctor(doctor_id=doctor_id, hospital_id=hospital_id,
                                full_name=body.full_name, active=body.active)
    await _repo().audit(
        event_type="doctor.registered", actor_type="admin",
        actor_id=doctor_id,
        detail={"hospital_id": hospital_id, "active": body.active})
    return {"status": "ok", "doctor_id": doctor_id, "hospital_id": hospital_id,
            "active": body.active}


@router.get("/doctors")
async def list_doctors(hospital_id: str, device=Depends(require_device)):
    """
    The register for one hospital, for the CMED selector.

    Device-authenticated rather than admin: the page needs it on every
    consultation, and a device may only ask about its own hospital.
    """
    hospital = safe_identifier(hospital_id, field="hospital_id")
    if hospital != device["hospital_id"]:
        raise HTTPException(status_code=403, detail="not your hospital")
    return {"hospital_id": hospital, "doctors": await _repo().doctors_at(hospital)}


@router.post("/admin/enrollment-token")
async def admin_enrollment_token(body: TokenRequest, _: None = Depends(require_admin)):
    """
    Mint a single-use enrollment token.

    Returned once and never stored in the clear - only its SHA-256 is kept, so a
    database leak cannot be used to enrol devices.
    """
    hospital_id = safe_identifier(body.hospital_id, field="hospital_id")
    # Optional. A token binds a machine to a hospital, and that is all it
    # decides - the doctor arrives from CMED with each consultation. A named
    # doctor here is only a label for the room in the paperwork, and most
    # laptops are shared across shifts and have none.
    doctor_id = (safe_identifier(body.doctor_id, field="doctor_id")
                 if body.doctor_id else "")
    token = await _repo().create_enrollment_token(
        hospital_id=hospital_id, doctor_id=doctor_id,
        created_by=body.created_by, ttl_hours=body.ttl_hours)
    await _repo().audit(
        event_type="enrollment_token.created", actor_type="admin",
        actor_id=body.created_by, detail={"hospital_id": hospital_id,
                                          "doctor_id": doctor_id,
                                          "ttl_hours": body.ttl_hours})
    return {"enrollment_token": token, "hospital_id": hospital_id,
            "doctor_id": doctor_id, "expires_in_hours": body.ttl_hours}


@router.post("/admin/device/{device_id}/revoke")
async def admin_revoke_device(device_id: str, reason: str = "",
                              _: None = Depends(require_admin)):
    """Cut off a lost or stolen machine. The token is cleared, not just flagged."""
    revoked = await _repo().revoke_device(device_id, reason or "revoked by administrator")
    if not revoked:
        raise HTTPException(status_code=404, detail="device not found or already revoked")
    await _repo().audit(event_type="device.revoked", actor_type="admin",
                        device_id=device_id, detail={"reason": reason})
    return {"status": "revoked", "device_id": device_id}


@router.get("/admin/alerts")
async def admin_alerts(limit: int = 50, _: None = Depends(require_admin)):
    """Open integrity alerts - the operator's queue."""
    limit = max(1, min(limit, 200))
    async with _repo()._pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT id, raised_at, session_id, device_id, alert_type, severity, detail
            FROM integrity_alerts WHERE resolved_at IS NULL
            ORDER BY raised_at DESC LIMIT $1
        """, limit)
    return {"alerts": [dict(r) for r in rows]}


__all__ = ["router", "ctx", "V2Context"]
