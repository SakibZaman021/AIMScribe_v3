"""
AIMScribe protocol 2 API - integrity, device identity, and the archive handshake.

Mounted alongside the existing v1 routes so the transcription and NER pipeline is
untouched. v1 endpoints stay open for now (see SECURITY note in main_fastapi);
everything here requires authentication.

The three flows:

  AGENT     enroll -> grant/mint -> session/open -> segment/authorize
                   -> segment/commit (receipt issued) -> delete local audio
                   -> session/close          (SRS 3.2: receipts on custody)

  CMED      clinical/patient-information, clinical/prescription (clinical.py)

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
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field

import integrity
from db_v2 import V2Repository
from fastapi.responses import JSONResponse

import confirmation as conf
from confirmation import FieldError
from grants import GrantIssuer, new_jti
from integrity import ChainError, ReceiptSigner, parse_entry, safe_identifier, safe_session_id

logger = logging.getLogger(__name__)

# Identifiers allowed into a filename. Anything else and the clip goes unnamed
# rather than letting a separator or a path fragment into the archive.
_NAME_SAFE = re.compile(r"^[A-Za-z0-9-]{1,64}$")
# The recording's own name (SRS-SES-05): the same characters plus the
# underscores that separate its fields.
_STEM_SAFE = re.compile(r"^[A-Za-z0-9_-]{1,160}$")

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
        self.grants: Optional[GrantIssuer] = None
        # aims_clinical (SRS 3.2 §8.7.2): CMED's clinical records.
        self.clinical = None
        # The copy bucket (SRS-STO-01): the merged lossless copy of each
        # finished recording, locked against deletion. Its own client with its
        # own credentials, because the credential that deletes pieces must not
        # be able to reach the copies (SRS-ARC-12). This one is cold storage -
        # cheap to keep for years, hours to read back - so nothing routine ever
        # reads from it.
        self.copy = None
        # Where the clinical JSON copies go. A visit's JSON is about two
        # kilobytes and is worth reading at once, while cold storage bills a
        # minimum object size and answers in hours, so the JSON is kept warm.
        # Unset, it shares the audio's bucket.
        self.copy_json = None

    @property
    def ready(self) -> bool:
        return self.repo is not None


ctx = V2Context()


def _repo() -> V2Repository:
    if not ctx.ready:
        raise HTTPException(status_code=503, detail="v2 layer is not initialised")
    return ctx.repo


def _clinical():
    if ctx.clinical is None:
        raise HTTPException(status_code=503, detail="clinical database is not initialised")
    return ctx.clinical


def _copy_bucket(kind: str = "audio"):
    """The store one kind of copy belongs in: audio cold, JSON warm."""
    if ctx.copy is None:
        raise HTTPException(status_code=503, detail="copy bucket is not configured")
    if kind == "json" and ctx.copy_json is not None:
        return ctx.copy_json
    return ctx.copy


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
    # Kept so protocol-2 recorders still validate. Consent is taken at
    # reception and is not checked here (SRS 3.2 §7.8a).
    consent_obtained: bool = True
    consent_method: str = Field("", max_length=64)
    audio: AudioSpec
    device_pubkey: str = Field("", max_length=64)
    genesis: Dict[str, Any]
    # The grant this recording was authorised under (v3 recorders). Links the
    # session to CMED's API 2; absent from protocol-2 recorders.
    grant_jti: Optional[str] = Field(None, max_length=128)


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
    try:
        session_id = safe_session_id(body.session_id)
        doctor_id = safe_identifier(body.doctor_id, field="doctor_id")
        patient_ref = safe_identifier(body.patient_ref, field="patient_ref")
        hospital_id = safe_identifier(body.hospital_id, field="hospital_id")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    if await _repo().refusal(session_id):
        raise HTTPException(status_code=409,
                            detail="this consultation was refused by the patient")

    # The clinic is the device's, always (SRS-INV-01; decision D1). A v3
    # recorder cannot get here with another clinic - its grant is refused - so a
    # disagreement comes from an old recorder. The audio already exists, so it
    # is filed under the device's clinic rather than refused, and someone is told.
    claimed_hospital = hospital_id
    hospital_id = device["hospital_id"]
    if claimed_hospital != hospital_id:
        await _repo().raise_alert(
            alert_type="clinic_mismatch", severity="critical",
            session_id=session_id, device_id=device["device_id"],
            detail={"device_hospital": hospital_id, "claimed": claimed_hospital},
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

    state = await _link_grant(session_id, body.grant_jti, patient_ref, device)

    await _repo().audit(
        event_type="session.opened", actor_type="device",
        actor_id=doctor_id, device_id=device["device_id"], session_id=session_id,
        detail={"hospital_id": hospital_id, "confirmation": state},
    )
    return {"session_id": session_id, "status": "open", "hospital_id": hospital_id,
            "confirmation": state}


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
    if session.get("confirmation") in ("refused", "expired"):
        raise HTTPException(status_code=409, detail="this consultation has been erased")

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
        await _issue_custody_receipt(session_id, body.seq_no, claimed)
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

    await _issue_custody_receipt(session_id, body.seq_no, claimed)
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
    # The copies are never deleted here (SRS-ARC-12). The credential should not
    # even reach them, but a bucket misconfigured into holding both would make
    # this the code that destroyed the only remaining copy.
    if ctx.copy is not None and (object_key.startswith("copies/")
                                 or ctx.minio.bucket == ctx.copy.bucket):
        raise RuntimeError(f"refusing to delete {object_key}: it is in the copy bucket")
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
    session = await _session_for_device(session_id, device)

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
    closed_at = _parse_time(body.closed_at)
    # The clinic's wall clock, resolved here rather than by the database
    # (SRS-SES-04). An unresolvable zone is logged and falls back to UTC; it
    # never fails the close, because a close that fails is a recording that is
    # never archived.
    opened_local = await _local_time(session["hospital_id"], session["opened_at"]) \
        if session.get("opened_at") else None
    closed_local = await _local_time(session["hospital_id"], closed_at)
    await repo.close_session(
        session_id,
        closed_at=closed_at,
        duration_seconds=body.duration_seconds,
        paused_seconds=body.paused_seconds,
        segment_count=len(stored_segments),
        chain_head=chain_head,
        manifest=body.manifest or {},
        close_reason=body.close_reason,
        local_date=opened_local.date() if opened_local else None,
        local_start=opened_local.time() if opened_local else None,
        local_end=closed_local.time(),
    )

    # The shared file name, and the clinical visit it belongs to.
    await _record_file_name(session_id)

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
    """Purge receipts the agent may act on - one per piece, issued the moment the
    piece is verified on arrival (SRS-REC-15)."""
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

    # The pieces are not deleted here. SRS-ARC-08: a finished consultation is
    # first kept in the cloud as one merged lossless copy, and only when that
    # copy is verified and recorded do the pieces go (/archive/copy/complete).
    # Where no copy bucket is configured the old behaviour stands, so an
    # existing deployment keeps working - and says so in the log.
    removed = 0
    if ctx.copy is None:
        logger.warning("No copy bucket configured; deleting the pieces for %s as "
                       "soon as it is archived (SRS-ARC-08 needs a copy bucket)",
                       session_id)
        removed = await _delete_bucket_objects(session_id)
        await repo.mark_segments_deleted(session_id)

    await repo.audit(
        event_type="session.archived", actor_type="service", actor_id="archive-worker",
        session_id=session_id,
        detail={"archive_relpath": body.archive_relpath, "receipts_issued": issued + 1,
                "objects_deleted": removed},
    )
    logger.info("Archived %s to %s; issued %s receipt(s), deleted %s object(s)",
                session_id, body.archive_relpath, issued + 1, removed)
    return {"status": "archived", "receipts_issued": issued + 1,
            "objects_deleted": removed, "copy_pending": ctx.copy is not None}


# ============================================================
# The cloud copy (SRS-ARC-08..13)
#
# The worker at UIU holds the archive and the encryption key; this server
# holds the bucket credentials and the database. So the worker compresses,
# checks and encrypts, and asks here for the places to put the result.
# ============================================================

class CopyAuthorizeRequest(BaseModel):
    session_id: str
    kind: str = Field(..., pattern="^(audio|json)$")


class CopyRecord(BaseModel):
    kind: str = Field(..., pattern="^(audio|json)$")
    object_key: str = Field(..., max_length=512)
    version: int = Field(1, ge=1)
    bytes: int = Field(..., ge=1)
    sha256: str = Field(..., min_length=64, max_length=64)
    plain_sha256: Optional[str] = Field(None, min_length=64, max_length=64)
    # The MD5 of the uploaded object - what the store returns as its ETag for
    # a single-part upload, and so what the store can be asked to prove
    # without sending the object back (SRS-ARC-09, step 5).
    md5: Optional[str] = Field(None, min_length=32, max_length=32)


class CopyCompleteRequest(BaseModel):
    session_id: str
    copies: List[CopyRecord]
    # False for a JSON written again after archiving (SRS-ARC-13): the audio
    # copy already exists and the pieces are long gone.
    final: bool = True


class CopyFailedRequest(BaseModel):
    session_id: str
    step: str = Field(..., max_length=64)
    message: str = Field("", max_length=1000)


def copy_object_key(session: Dict[str, Any], kind: str, version: int) -> str:
    """
    Where a copy lives in the copy bucket. The same shape as the archive tree,
    so a restore can walk one clinic or one day without an index.
    """
    stem = session.get("file_stem") or safe_session_id(str(session["session_id"]))
    if not _STEM_SAFE.match(str(stem)):
        raise HTTPException(status_code=409, detail="unsafe file name for this session")
    day = session.get("session_date")
    day = day.isoformat() if hasattr(day, "isoformat") else str(day or "undated")
    suffix = "flac.enc" if kind == "audio" else "json.enc"
    return (f"copies/{safe_identifier(session['hospital_id'], field='hospital_id')}"
            f"/{safe_identifier(session['doctor_id'], field='doctor_id')}"
            f"/{day}/{stem}.v{version}.{suffix}")


@router.get("/archive/copy/pending")
async def archive_copy_pending(limit: int = 5, _: None = Depends(require_worker)):
    """Recordings archived at UIU whose cloud copy has still to be made."""
    limit = max(1, min(limit, 20))
    sessions = await _repo().pending_copy(limit)
    return {"sessions": [{
        "session_id": s["session_id"],
        "file_stem": s["file_stem"],
        "hospital_id": s["hospital_id"],
        "doctor_id": s["doctor_id"],
        "session_date": s["session_date"].isoformat() if s["session_date"] else None,
        "archive_relpath": s["archive_relpath"],
        "archive_sha256": bytes(s["archive_sha256"]).hex() if s["archive_sha256"] else None,
        "archive_bytes": s["archive_bytes"],
    } for s in sessions]}


@router.get("/archive/json/pending")
async def archive_json_pending(limit: int = 5, _: None = Depends(require_worker)):
    """
    Recordings whose JSON has to be written again because the prescription
    arrived after archiving (SRS-ARC-13).
    """
    limit = max(1, min(limit, 20))
    sessions = await _repo().pending_json(limit)
    return {"sessions": [{
        "session_id": s["session_id"],
        "file_stem": s["file_stem"],
        "archive_relpath": s["archive_relpath"],
    } for s in sessions]}


@router.get("/archive/clinical/{session_id}")
async def archive_clinical_document(session_id: str,
                                    _: None = Depends(require_worker)):
    """
    The JSON that is written beside the audio and travels with it into the
    cloud copy (§8.8, SRS-CRI-05).

    Assembled here rather than at UIU: the clinical database has its own
    credentials and the worker holds none of them.
    """
    session_id = safe_session_id(session_id)
    session = await _repo().get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    return await _clinical_document(session)


async def _clinical_document(session: Dict[str, Any]) -> Dict[str, Any]:
    repo = _repo()
    session_id = str(session["session_id"])
    document: Dict[str, Any] = {"file_stem": session.get("file_stem")}

    visit = None
    if session.get("grant_jti"):
        grant = await repo.get_authorisation(session["grant_jti"])
        if grant and grant["patient_id"] != "REDACTED":
            visit = _visit_from_grant(grant)

    if visit is not None and ctx.clinical is not None:
        document.update(await ctx.clinical.json_document(visit, actor="archive-worker"))

    started = session.get("opened_at")
    ended = session.get("closed_at")
    document.setdefault("visit", {})
    document["visit"].setdefault("doctor_id", session["doctor_id"])
    document["visit"].setdefault("hospital_id", session["hospital_id"])
    document["visit"]["end_time"] = ended.isoformat() if ended else None
    document["visit"].setdefault("start_time", started.isoformat() if started else None)

    archive_sha = session.get("archive_sha256")
    document["recording"] = {
        "session_id": session_id,
        "confirmation": session.get("confirmation"),
        "opened_at": started.isoformat() if started else None,
        "closed_at": ended.isoformat() if ended else None,
        "duration_seconds": float(session["total_duration_seconds"])
                            if session.get("total_duration_seconds") is not None else None,
        "audio_sha256": bytes(archive_sha).hex() if archive_sha else None,
        "audio_bytes": session.get("archive_bytes"),
        "archive_relpath": session.get("archive_relpath"),
        "written_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    return document


@router.post("/archive/copy/authorize")
async def archive_copy_authorize(body: CopyAuthorizeRequest,
                                 _: None = Depends(require_worker)):
    """
    One place in the copy bucket to put one object, and the address to read it
    back from. The worker never holds bucket credentials (SRS-STO-02).
    """
    copy = _copy_bucket(body.kind)
    repo = _repo()
    session_id = safe_session_id(body.session_id)
    session = await repo.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    if session.get("archived_at") is None:
        raise HTTPException(status_code=409, detail="session is not archived yet")

    version = await repo.next_copy_version(session_id, body.kind)
    object_key = copy_object_key(session, body.kind, version)
    loop = asyncio.get_event_loop()
    upload = await loop.run_in_executor(
        None, copy.get_presigned_upload_url, object_key, 3600)
    return {"object_key": object_key, "version": version,
            "bucket": copy.bucket, "upload_url": upload}


async def _confirm_stored(session_id: str, copy: "CopyRecord") -> None:
    """
    Ask the store what it holds, and refuse to go on unless it matches what UIU
    sent (SRS-ARC-09, step 5).

    The copy is not downloaded to check it. The audio copy is in cold storage,
    where reading it back takes hours, and reading every copy back would cost
    more each month in traffic than keeping the copies costs at all. What the
    store will answer for nothing is the object's size and its own fingerprint
    of it, and that is what is checked here. Whether the copy can really be
    turned back into a recording is proven by the restore drill (AT-74), not by
    fetching every file twice.
    """
    store = _copy_bucket(copy.kind)
    loop = asyncio.get_event_loop()
    try:
        stored = await loop.run_in_executor(
            None, store.client.stat_object, store.bucket, copy.object_key)
    except Exception as exc:
        await _copy_refused(session_id, copy, f"the store does not hold it: {exc}")

    size = getattr(stored, "size", None)
    if size is not None and int(size) != copy.bytes:
        await _copy_refused(session_id, copy,
                            f"the store holds {size} bytes, {copy.bytes} were sent")

    etag = (getattr(stored, "etag", "") or "").strip('"').lower()
    # A multipart upload's ETag is not the object's MD5 (it ends in "-<parts>"),
    # so there is nothing to compare; the size check stands alone.
    if copy.md5 and etag and "-" not in etag and etag != copy.md5.lower():
        await _copy_refused(session_id, copy,
                            "the store's fingerprint does not match what was sent")


async def _copy_refused(session_id: str, copy: "CopyRecord", why: str):
    await _repo().raise_alert(
        alert_type="cloud_copy_unverified", severity="critical", session_id=session_id,
        detail={"object_key": copy.object_key, "kind": copy.kind, "problem": why})
    logger.error("Copy %s for %s not verified: %s", copy.object_key, session_id, why)
    raise HTTPException(status_code=409, detail=f"copy not verified: {why}")


@router.post("/archive/copy/complete")
async def archive_copy_complete(body: CopyCompleteRequest,
                                _: None = Depends(require_worker)):
    """
    The worker has uploaded the copy, read it back and matched it. Record it -
    and only now delete the pieces (SRS-ARC-09, steps 6 and 7).
    """
    repo = _repo()
    session_id = safe_session_id(body.session_id)
    session = await repo.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")

    for copy in body.copies:
        try:
            digest = bytes.fromhex(copy.sha256)
            plain = bytes.fromhex(copy.plain_sha256) if copy.plain_sha256 else None
        except ValueError:
            raise HTTPException(status_code=400, detail="sha256 must be hex")
        await _confirm_stored(session_id, copy)
        await repo.record_cloud_copy(
            session_id=session_id, kind=copy.kind, object_key=copy.object_key,
            version=copy.version, byte_length=copy.bytes, sha256=digest,
            plain_sha256=plain)

    if not body.final:
        # A JSON written again after archiving. Nothing else changes: the audio
        # copy stands and the earlier JSON objects are kept (SRS-ARC-13).
        await repo.clear_json_stale(session_id)
        await repo.audit(
            event_type="session.json_rewritten", actor_type="service",
            actor_id="archive-worker", session_id=session_id,
            detail={"objects": [c.object_key for c in body.copies]})
        return {"status": "recorded", "objects_deleted": 0}

    kinds = {c.kind for c in body.copies} | {
        c["kind"] for c in await repo.copies_for(session_id)}
    if "audio" not in kinds:
        raise HTTPException(status_code=409,
                            detail="no audio copy recorded for this session")

    await repo.mark_copied(session_id)
    removed = await _delete_bucket_objects(session_id)
    await repo.mark_segments_deleted(session_id)
    await repo.audit(
        event_type="session.copied", actor_type="service", actor_id="archive-worker",
        session_id=session_id,
        detail={"objects": [c.object_key for c in body.copies],
                "objects_deleted": removed})
    logger.info("Cloud copy complete for %s; deleted %s piece(s) from the segment "
                "bucket", session_id, removed)
    return {"status": "copied", "objects_deleted": removed}


@router.post("/archive/copy/failed")
async def archive_copy_failed(body: CopyFailedRequest,
                              _: None = Depends(require_worker)):
    """
    A step of the copy failed at UIU. The pieces stay where they are and the
    step is retried; this is how the failure becomes visible (SRS-ARC-10).
    """
    session_id = safe_session_id(body.session_id)
    await _repo().raise_alert(
        alert_type="cloud_copy_failed", severity="warning", session_id=session_id,
        detail={"step": body.step, "message": body.message[:500]})
    return {"status": "recorded"}


# ============================================================
# v3 (SRS 3.2): grants from this server, confirmation against CMED's
# API 2, receipts on custody, and refusals
# ============================================================

def _coded(status: int, code: str, message: str, **extra) -> JSONResponse:
    """A refusal the recorder can act on: status, code and a message."""
    return JSONResponse(status_code=status,
                        content={"status": status, "code": code, "message": message,
                                 **extra})


async def _clinic_for(cmed_hospital_id: str, device: Dict[str, Any]) -> Optional[str]:
    """
    The clinic a grant is issued for: always the device's own (decision D1).

    CMED's identifier must map to it (SRS-ENR-19). A clinic whose CMED
    identifier already is our code needs no mapping - that is how the dummy
    CMED app, and any site set up that way, work. Anything else is a mismatch:
    the mapping is wrong or the laptop is in the wrong building, and a recording
    filed under either clinic would be labelled wrongly (SRS-GRT-10).
    """
    own = device["hospital_id"]
    mapped = await _repo().hospital_for_cmed_id(cmed_hospital_id)
    if mapped is not None:
        return own if mapped == own else None
    return own if cmed_hospital_id == own else None


@router.post("/grant/mint")
async def mint_grant(body: Dict[str, Any], device=Depends(require_device)):
    """
    Authorise one consultation (SRS 3.2 §5.3).

    The recorder forwards CMED's five fields. The reply is a 60-second,
    single-use grant, signed with a key only this server holds, and whether
    CMED's API 2 for the same consultation has already arrived:

        confirmed   API 2 matched - the recording enters the dataset
        pending     not yet; the recorder keeps recording and asks again

    Recording never waits for this (SRS-GRT-07); the recorder calls it
    alongside capture.
    """
    repo = _repo()
    if ctx.grants is None:
        return _coded(503, "AGENT_NOT_READY", "The grant signing key is not configured.")
    try:
        visit = conf.parse_visit(body)
    except FieldError as exc:
        return _coded(400, exc.code, str(exc), field=exc.field)

    clinic = await _clinic_for(visit.cmed_hospital_id, device)
    if clinic is None:
        await repo.raise_alert(
            alert_type="clinic_mismatch", severity="critical",
            device_id=device["device_id"],
            detail={"device_hospital": device["hospital_id"],
                    "cmed_hospital_id": visit.cmed_hospital_id,
                    "mapped_to": await repo.hospital_for_cmed_id(visit.cmed_hospital_id)})
        return _coded(401, "CLINIC_MISMATCH",
                      "This PC is registered to a different clinic.")

    # Decision D4: the doctor register is a directory, not a gate. CMED
    # authenticates its own doctors, and its API 2 now proves the consultation;
    # refusing a doctor new to this server stopped real consultations before.
    try:
        await repo.upsert_doctor(doctor_id=visit.doctor_id, hospital_id=clinic,
                                 full_name=visit.doctor_id, only_if_new=True)
    except Exception as exc:
        logger.warning("Could not record doctor %s in the directory: %s",
                       visit.doctor_id, exc)

    now = datetime.now(timezone.utc)
    jti = new_jti()
    await repo.create_authorisation(
        jti=jti, device_id=device["device_id"], hospital_id=clinic, visit=visit,
        expires_at=now + timedelta(seconds=ctx.grants.lifetime))
    notice_id = await repo.find_unclaimed_notice(
        visit, hospital_id=clinic, since=now - conf.NOTICE_MATCH_WINDOW)
    confirmed = notice_id is not None and await repo.claim_notice(notice_id, jti)
    state = "confirmed" if confirmed else "pending"

    token, _ = ctx.grants.issue(
        jti=jti, patient_ref=visit.patient_id, doctor_id=visit.doctor_id,
        hospital_id=clinic, cmed_hospital_id=visit.cmed_hospital_id,
        start_time=visit.start_time, visit_date=visit.visit_date, confirmation=state)
    await repo.audit(event_type="grant.issued", actor_type="device",
                     actor_id=visit.doctor_id, device_id=device["device_id"],
                     detail={"hospital_id": clinic, "confirmation": state})
    return {"status": 200, "code": "GRANTED", "grant": token, "jti": jti,
            "confirmation": state, "hospital_id": clinic,
            "expires_in": ctx.grants.lifetime}


async def _link_grant(session_id: str, grant_jti: Optional[str], patient_ref: str,
                      device: Dict[str, Any]) -> str:
    """
    Tie a session to the grant it was opened under, and so to CMED's API 2.
    No grant means a protocol-2 recorder: 'legacy', archived as before.
    """
    repo = _repo()
    row = await repo.session_confirmation(session_id)

    if not grant_jti:
        state, jti = "legacy", None
    else:
        grant = await repo.get_authorisation(grant_jti)
        valid = (grant is not None
                 and str(grant["device_id"]) == str(device["device_id"])
                 and grant["patient_id"] == patient_ref
                 and grant.get("session_id") in (None, session_id))
        if not valid:
            await repo.raise_alert(alert_type="grant_link_failed", severity="critical",
                                   session_id=session_id, device_id=device["device_id"],
                                   detail={"grant_jti": grant_jti})
            state, jti = "unconfirmed", None
        else:
            await repo.link_authorisation(grant_jti, session_id)
            state = "confirmed" if grant.get("notice_id") else "pending"
            jti = grant_jti

    # A retried open must not step a session backwards.
    if row is not None and row.get("grant_jti"):
        if state == "confirmed" and row["confirmation"] in conf.WAITING:
            await repo.set_session_confirmation(session_id, "confirmed")
            return "confirmed"
        return row["confirmation"]

    await repo.set_session_confirmation(session_id, state, grant_jti=jti)
    return state


async def _issue_custody_receipt(session_id: str, seq_no: int, sha256: bytes) -> bool:
    """
    SRS-REC-15: the PC may delete a piece as soon as this server holds a
    verified copy. The bytes were just read back and re-hashed, so the receipt
    is issued here - not after archiving, which left audio on clinic PCs for
    days waiting on a process they could not see.
    """
    if ctx.signer is None:
        return False
    receipt = ctx.signer.sign_segment(
        session_id=session_id, seq_no=seq_no, sha256_hex=sha256.hex(),
        archived_at=datetime.now(timezone.utc))
    await _repo().store_receipt(
        session_id=session_id, scope="segment", seq_no=seq_no, sha256=sha256,
        payload=receipt["payload"], signature=bytes.fromhex(receipt["signature"]))
    return True


async def _mark_unconfirmed(session_id: str, row: Dict[str, Any]) -> None:
    repo = _repo()
    await repo.set_session_confirmation(session_id, "unconfirmed")
    await repo.raise_alert(
        alert_type="unconfirmed_recording", severity="warning",
        session_id=session_id, device_id=row.get("device_id"),
        detail={"reason": "no matching API 2 from CMED within two minutes"})


async def _current_confirmation(session_id: str) -> str:
    row = await _repo().session_confirmation(session_id)
    if row is None:
        return "unknown"
    state = conf.current_state(row["confirmation"], row["opened_at"],
                               datetime.now(timezone.utc))
    if state == "unconfirmed" and row["confirmation"] == "pending":
        await _mark_unconfirmed(session_id, row)
    return state


@router.get("/session/{session_id}/confirmation")
async def session_confirmation(session_id: str, device=Depends(require_device)):
    """
    Asked every five seconds by a recorder showing "confirming" (SRS-CNF-07).
    After two minutes without CMED's API 2 the answer becomes "unconfirmed"
    and an alert is raised (SRS-CNF-08); the recording is never cut.
    """
    sid = safe_session_id(session_id)
    await _session_for_device(sid, device)
    return {"session_id": sid, "confirmation": await _current_confirmation(sid)}


async def _erase_session(session_id: str, *, state: str):
    """
    Remove every trace of a consultation's content: its pieces in storage, its
    chain and receipts, CMED's clinical records for the visit, and the old
    transcription rows. Returns (objects deleted, the visit, if known).
    """
    repo = _repo()
    row = await repo.session_confirmation(session_id)
    visit = None
    if row and row.get("grant_jti"):
        grant = await repo.get_authorisation(row["grant_jti"])
        if grant and grant["patient_id"] != "REDACTED":
            visit = _visit_from_grant(grant)

    removed = await _delete_bucket_objects(session_id)
    if visit is not None:
        await repo.delete_notices_for(visit)
        if ctx.clinical is not None:
            await ctx.clinical.erase_visit(visit)
    await repo.erase_session(session_id, state=state)
    return removed, visit


def _visit_from_grant(grant: Dict[str, Any]) -> conf.Visit:
    visit_date = grant["visit_date"]
    return conf.Visit(
        patient_id=grant["patient_id"], doctor_id=grant["doctor_id"],
        cmed_hospital_id=grant["cmed_hospital_id"], start_time=grant["start_time"],
        visit_date=visit_date.isoformat() if hasattr(visit_date, "isoformat")
        else str(visit_date))


def file_stem_for(patient: str, doctor: str, hospital: str, local_start: datetime,
                  local_end: datetime) -> str:
    """`PatientID_DoctorID_HospitalID_HHMMSS_HHMMSS_YYYYMMDD` (SRS-SES-05)."""
    return (f"{patient}_{doctor}_{hospital}_{local_start:%H%M%S}_{local_end:%H%M%S}"
            f"_{local_start:%Y%m%d}")


async def _record_file_name(session_id: str) -> Optional[str]:
    """
    Name a closed recording, in columns (SRS-DBA-25), and tie its clinical
    visit to it by that name (SRS-DBA-21). Linking is best effort: the nightly
    reconciliation reports anything left unlinked (SRS-CRI-10).
    """
    repo = _repo()
    session = await repo.get_session(session_id)
    if not session or not session.get("opened_at") or not session.get("closed_at"):
        return None
    start = await _local_time(session["hospital_id"], session["opened_at"])
    end = await _local_time(session["hospital_id"], session["closed_at"])
    stem = await repo.set_file_names(
        session_id,
        file_stem=file_stem_for(session["patient_id"], session["doctor_id"],
                                session["hospital_id"], start, end),
        local_start=start.time().replace(tzinfo=None),
        local_end=end.time().replace(tzinfo=None))

    if session.get("grant_jti") and ctx.clinical is not None:
        grant = await repo.get_authorisation(session["grant_jti"])
        if grant and grant["patient_id"] != "REDACTED":
            try:
                await ctx.clinical.link_session(_visit_from_grant(grant),
                                                session_id=session_id, file_stem=stem,
                                                hospital_id=session["hospital_id"])
            except Exception as exc:
                logger.warning("Could not link %s to its clinical visit: %s",
                               session_id, exc)
    return stem


class RefuseRequest(BaseModel):
    session_id: str


@router.post("/session/refuse")
async def refuse_session(body: RefuseRequest, device=Depends(require_device)):
    """
    The patient did not consent (SRS 3.2 §7.8a). The doctor pressed Stop and
    chose "Patient did not consent"; the recorder has already deleted what it
    held. Here everything else goes, within the request (SRS-CNS-04, -05), and
    only an audit entry without audio or patient remains (SRS-CNS-06).

    Accepted for a session this server has never seen, so a refusal made while
    the PC was offline still stops that session being opened later.
    """
    repo = _repo()
    session_id = safe_session_id(body.session_id)
    session = await repo.get_session(session_id)
    if session is not None and str(session["device_id"]) != str(device["device_id"]):
        raise HTTPException(status_code=403, detail="session belongs to another device")

    removed, visit = 0, None
    if session is not None and session.get("confirmation") != "refused":
        removed, visit = await _erase_session(session_id, state="refused")

    first = await repo.record_refusal(
        session_id=session_id, device_id=device["device_id"],
        hospital_id=device["hospital_id"],
        doctor_id=session["doctor_id"] if session else None,
        visit_sha256=visit.digest() if visit else None)
    await repo.complete_refusal(session_id)

    if first:
        session_date = session.get("session_date") if session else None
        await repo.audit(
            event_type="session.refused", actor_type="device",
            actor_id=session["doctor_id"] if session else None,
            device_id=device["device_id"], session_id=session_id,
            detail={"hospital_id": device["hospital_id"],
                    "date": session_date.isoformat() if session_date else None,
                    "objects_deleted": removed})
    return {"status": "refused", "session_id": session_id, "objects_deleted": removed}


@router.post("/maintenance/sweep")
async def maintenance_sweep(_: None = Depends(require_worker)):
    """
    Settle confirmation deadlines. The archive worker calls this every pass.

        waiting 24 hours with no API 2  ->  erased and audited   (SRS-CNF-09)
        pending for two minutes         ->  unconfirmed, alert   (SRS-CNF-08)
    """
    repo = _repo()
    now = datetime.now(timezone.utc)

    erased = 0
    for row in await repo.sessions_in_confirmation(
            ["pending", "unconfirmed"], opened_before=now - conf.UNCONFIRMED_LIFETIME):
        removed, _ = await _erase_session(row["session_id"], state="expired")
        await repo.audit(event_type="session.expired_unconfirmed", actor_type="service",
                         actor_id="sweep", session_id=row["session_id"],
                         detail={"objects_deleted": removed})
        erased += 1

    # First, anything that is only waiting because Channel B and the recording
    # crossed: the notice is claimed, so the recording is confirmed (SRS-CNF-07).
    reconciled = await repo.reconcile_pending_confirmations()
    for session_id in reconciled:
        await repo.audit(event_type="session.confirmed", actor_type="service",
                         actor_id="sweep", session_id=session_id,
                         detail={"reconciled": True})
    if reconciled:
        logger.info("Sweep confirmed %s recording(s) whose notice had arrived",
                    len(reconciled))

    marked = 0
    for row in await repo.sessions_in_confirmation(
            ["pending"], opened_before=now - conf.CONFIRM_DEADLINE):
        await _mark_unconfirmed(row["session_id"], row)
        marked += 1

    # Clinical records that did not load first time (SRS-CRI-04).
    loaded = await ctx.clinical.load_pending() if ctx.clinical is not None else 0

    # A recording should have its cloud copy the same night (SRS-ARC-14). One
    # that still has none after a day is a step failing quietly, and the pieces
    # are rightly still in the bucket waiting for it (SRS-ARC-10).
    overdue = await repo.copies_overdue(hours=24) if ctx.copy is not None else []
    for row in overdue:
        await repo.raise_alert(
            alert_type="cloud_copy_overdue", severity="warning",
            session_id=row["session_id"],
            detail={"archived_at": row["archived_at"].isoformat()
                                   if row["archived_at"] else None})

    return {"status": "ok", "marked_unconfirmed": marked, "erased": erased,
            "clinical_loaded": loaded, "copies_overdue": len(overdue),
            "reconciled": len(reconciled)}


# ============================================================
# Administration
# ============================================================

class HospitalRequest(BaseModel):
    hospital_id: str = Field(..., max_length=64)
    name: str = Field(..., max_length=256)
    timezone: str = Field("Asia/Dhaka", max_length=64)
    # CMED's identifier for this clinic (SRS-ENR-19). Without it, only a CMED
    # that already sends our own code can be granted (decision D1).
    cmed_hospital_id: str = Field("", max_length=64)


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
    # A zone this server cannot resolve would put every recording from this
    # clinic on the wrong day, and the wall-clock columns with it, so it is
    # refused here rather than discovered later in a file name (SRS-SES-04).
    try:
        from zoneinfo import ZoneInfo
        ZoneInfo(body.timezone)
    except Exception:
        raise HTTPException(
            status_code=400,
            detail=f"timezone {body.timezone!r} is not a known zone name, "
                   f"for example Asia/Dhaka")

    hospital_id = safe_identifier(body.hospital_id, field="hospital_id")
    await _repo().upsert_hospital(hospital_id, body.name, body.timezone)
    cmed_id = (safe_identifier(body.cmed_hospital_id, field="cmed_hospital_id")
               if body.cmed_hospital_id else None)
    if cmed_id:
        await _repo().set_cmed_hospital_id(hospital_id, cmed_id)
        await _repo().audit(event_type="hospital.mapped", actor_type="admin",
                            actor_id=hospital_id, detail={"cmed_hospital_id": cmed_id})
    return {"status": "ok", "hospital_id": hospital_id, "cmed_hospital_id": cmed_id}


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


class CmedKeyRequest(BaseModel):
    label: str = Field(..., min_length=1, max_length=64)
    created_by: str = Field(..., max_length=128)
    expires_in_days: int = Field(0, ge=0, le=3650)


class CmedKeyRevokeRequest(BaseModel):
    label: str = Field(..., min_length=1, max_length=64)


@router.post("/admin/cmed-key")
async def admin_cmed_key(body: CmedKeyRequest, _: None = Depends(require_admin)):
    """
    Issue a key for CMED's server (SRS-CHB-03). Shown once; only its hash is
    kept. Several may be active at once, so a key is replaced by issuing the
    new one, letting CMED switch, then revoking the old label.
    """
    expires_at = (datetime.now(timezone.utc) + timedelta(days=body.expires_in_days)
                  if body.expires_in_days else None)
    key = await _repo().create_cmed_key(label=body.label, created_by=body.created_by,
                                        expires_at=expires_at)
    await _repo().audit(event_type="cmed_key.created", actor_type="admin",
                        actor_id=body.created_by, detail={"label": body.label})
    return {"cmed_key": key, "label": body.label,
            "expires_at": expires_at.isoformat() if expires_at else None,
            "note": "Shown once. Give it to CMED for its server configuration only."}


@router.post("/admin/cmed-key/revoke")
async def admin_cmed_key_revoke(body: CmedKeyRevokeRequest,
                                _: None = Depends(require_admin)):
    revoked = await _repo().revoke_cmed_keys(body.label)
    if not revoked:
        raise HTTPException(status_code=404, detail="no active key with that label")
    await _repo().audit(event_type="cmed_key.revoked", actor_type="admin",
                        detail={"label": body.label, "keys": revoked})
    return {"status": "revoked", "label": body.label, "keys": revoked}


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
