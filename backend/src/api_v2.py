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
import base64
import io
import json
import hmac
import logging
import time
import os
import uuid
import re
import wave
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field

import integrity
from db_v2 import V2Repository
from fastapi.responses import JSONResponse

import confirmation as conf
import labelling
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
    request: Request,
    x_device_token: Optional[str] = Header(None),
) -> Dict[str, Any]:
    """
    Authenticate an agent: its enrolment-issued bearer token, and - from
    recorder 3.5 - a signature by the PC's own Ed25519 key over the request.

    The token alone is a bearer secret: anyone who copied it (a backup, a
    memory dump, another user of the PC) could act as the PC. The signature
    proves possession of the key the PC's chain is signed with, binds the
    method, path, body and time, and its nonce is used once. A PC that has
    signed one request must sign every request after it, so a stolen token
    stops working the day the PC is upgraded.
    """
    if not x_device_token:
        raise HTTPException(status_code=401, detail="device token required")

    device = await _repo().device_by_token(x_device_token)
    if device is None:
        raise HTTPException(status_code=401, detail="unknown device token")
    if device["revoked_at"] is not None:
        raise HTTPException(status_code=403, detail="device has been revoked")
    await _check_device_signature(request, device)
    return device


DEVICE_SIGNATURE_WINDOW = 300            # seconds either side of the server's clock
_NONCE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")


def device_signatures_required() -> bool:
    return os.getenv("AIMS_REQUIRE_DEVICE_SIGNATURES", "").strip().lower() in (
        "1", "on", "true", "yes")


def _b64url_decode(text: str) -> bytes:
    if not isinstance(text, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", text or ""):
        raise ValueError("not base64url")
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _signed_path(request) -> bytes:
    """
    The path exactly as the PC sent it - raw, not decoded - with "?query" when
    there is one: what the recorder signs (including the /api/v2 prefix).
    """
    scope = getattr(request, "scope", None) or {}
    raw = scope.get("raw_path")
    if raw is None:
        raw = request.url.path.encode("utf-8")
    query = scope.get("query_string")
    if query is None:
        query = (request.url.query or "").encode("utf-8")
    return raw + (b"?" + query if query else b"")


async def _check_device_signature(request: Request, device: Dict[str, Any]) -> None:
    headers = request.headers
    stamp = headers.get("x-device-timestamp")
    nonce = headers.get("x-device-nonce")
    signature = headers.get("x-device-signature")
    if stamp is None and nonce is None and signature is None:
        if device.get("signs_requests") or device_signatures_required():
            raise HTTPException(status_code=401, detail="this PC's requests must be signed")
        return
    if not (stamp and nonce and signature):
        raise HTTPException(status_code=401, detail="incomplete request signature")
    if not (stamp.isascii() and stamp.isdigit()) or len(stamp) > 12:
        raise HTTPException(status_code=401, detail="X-Device-Timestamp must be unix seconds")
    if abs(time.time() - int(stamp)) > DEVICE_SIGNATURE_WINDOW:
        raise HTTPException(status_code=401,
                            detail="request signature is outside the 5-minute window; "
                                   "check the PC's clock")
    if not _NONCE.match(nonce):
        raise HTTPException(status_code=401, detail="X-Device-Nonce must be 16-64 "
                                                    "characters of A-Z a-z 0-9 _ -")
    try:
        raw_signature = _b64url_decode(signature)
    except Exception:
        raise HTTPException(status_code=401, detail="X-Device-Signature is not base64url")
    body = await request.body()
    message = integrity.device_request_message(
        method=request.method, path=_signed_path(request), timestamp=stamp, nonce=nonce,
        body=body)
    if not integrity.verify_device_signature(bytes(device["tpm_pubkey"]), message,
                                             raw_signature):
        raise HTTPException(status_code=401, detail="request signature does not verify")
    if not await _repo().use_device_nonce(device["device_id"], nonce):
        raise HTTPException(status_code=401, detail="request nonce was already used")
    if not device.get("signs_requests"):
        await _repo().mark_device_signs(device["device_id"])
        logger.info("Device %s now signs its requests; unsigned ones are refused from "
                    "here on", device["device_id"])


def _check_static_key(supplied: Optional[str], env_var: str, label: str) -> None:
    expected = os.getenv(env_var, "")
    if not expected:
        raise HTTPException(status_code=503, detail=f"{label} key is not configured")
    if not supplied or not hmac.compare_digest(supplied, expected):
        raise HTTPException(status_code=401, detail=f"invalid {label} key")


async def require_admin(request: Request, x_admin_key: Optional[str] = Header(None)) -> None:
    """
    The admin key (the tools, the loopback door), or a browser signed in to the
    portal as an admin (portal.py) - which must also send X-AIMS-Portal on
    anything but a GET, so the session cannot be used from another site.
    """
    if not x_admin_key:
        import portal
        person = await portal.person_for(request)
        if person is not None:
            if person.role != "admin":
                raise HTTPException(status_code=403, detail="this needs an administrator")
            portal.check_csrf(request)
            return
    _check_static_key(x_admin_key, "AIMS_ADMIN_KEY", "admin")


async def require_viewer(request: Request, x_admin_key: Optional[str] = Header(None)) -> None:
    """Reading only: the admin key, or anyone signed in to the portal, researchers too."""
    if not x_admin_key:
        import portal
        if await portal.person_for(request) is not None:
            return
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
    # 64 hex characters: an Ed25519 key in software (up to 3.5). 130: a P-256
    # key inside the PC's TPM (3.6 on), which must also prove it holds it.
    device_pubkey: str = Field(..., min_length=64, max_length=130)
    key_proof: Optional[str] = Field(None, max_length=200)
    key_storage: str = Field("software", max_length=16)
    machine_name: str = Field("", max_length=128)
    os_version: str = Field("", max_length=256)
    app_version: str = Field("", max_length=32)
    protocol_version: int = Field(2, ge=2, le=9)
    audio: Optional[AudioSpec] = None


class RegisterRequest(BaseModel):
    # Only a P-256 key inside the PC's TPM (65 bytes, 130 hex) may register
    # itself: it cannot be copied off the PC. A software key needs a token.
    device_pubkey: str = Field(..., min_length=130, max_length=130)
    key_proof: str = Field(..., min_length=40, max_length=200)
    # CMED's code for the clinic, from CMED's own trigger on this PC.
    cmed_hospital_id: str = Field(..., pattern=r"^[A-Za-z0-9_-]{1,64}$")
    timestamp: int
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
    # For the record only: the key checked is always the one enrolled. 64 hex
    # characters for Ed25519, 130 for a TPM P-256 key (3.6 on).
    device_pubkey: str = Field("", max_length=130)
    genesis: Dict[str, Any]
    # The grant this recording was authorised under (v3 recorders). Links the
    # session to CMED's API 2; absent from protocol-2 recorders.
    grant_jti: Optional[str] = Field(None, max_length=128)
    # A consultation resumed after the recorder crashed or the PC restarted:
    # the recording it continues (S4).
    continues_session_id: Optional[str] = Field(None, max_length=64)


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


class ReassignRequest(BaseModel):
    session_id: str
    chain_entry: Dict[str, Any]
    # A grant minted for the patient the recording moves to, as for any start.
    grant_jti: Optional[str] = None


class MarkerRequest(BaseModel):
    session_id: str
    chain_entry: Dict[str, Any]
    # A grant for the patient clicked, when the recorder has one: it is how
    # that patient's API 2 and 3b find this recording.
    grant_jti: Optional[str] = Field(None, max_length=128)


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
    # Server time minus the PC's time, measured by the recorder from our replies.
    clock_offset_seconds: Optional[float] = None
    # Recordings this PC is holding because this server said no (U3).
    held: Optional[List[Dict[str, Any]]] = None
    held_count: Optional[int] = Field(None, ge=0)
    # BitLocker on the drive holding the PC's audio; None when unknown (X1).
    disk_encrypted: Optional[bool] = None


class ArchivedPart(BaseModel):
    part_no: int = Field(..., ge=1, le=1000)
    archive_relpath: str = Field(..., max_length=512)
    sha256: str = Field(..., min_length=64, max_length=64)
    bytes: int = Field(..., ge=1)
    start_seconds: float = Field(0.0, ge=0)
    end_seconds: Optional[float] = Field(None, ge=0)


class JoinedSegment(BaseModel):
    seq_no: int = Field(..., ge=1, le=10000)
    sha256: str = Field(..., min_length=64, max_length=64)


class ArchiveCompleteRequest(BaseModel):
    session_id: str
    archive_relpath: str = Field(..., max_length=512)
    # Of the FULL joined audio, also when it was cut into parts: the chain
    # check and the session receipt do not change.
    sha256: str = Field(..., min_length=64, max_length=64)
    bytes: int = Field(..., ge=1)
    parts: Optional[List[ArchivedPart]] = None
    # The pieces the worker actually joined into that audio. Receipts, the
    # "archived" mark and deleting the pieces from the bucket cover these and
    # nothing else, so a piece that arrived after the worker fetched the list
    # can never be counted as archived without being in the file.
    segments: Optional[List[JoinedSegment]] = Field(None, max_length=10000)


# ============================================================
# Helpers
# ============================================================

def _parse_time(value: Optional[str]) -> datetime:
    """
    A time the recorder sent. Missing or unreadable is "now", as it always
    was. A time with no offset is read as UTC, which is what every recorder
    writes (crypto.iso_utc): it used to come back naive, and subtracting it
    from an aware time raised TypeError - a close that failed with a 500 on
    every retry - while a naive opened_at was filed under the container's
    idea of the date.
    """
    if not value:
        return datetime.now(timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(timezone.utc)
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _parse_time_strict(value: Any, field: str) -> datetime:
    """For fields new in 3.5: an ISO-8601 time with its offset, or 400."""
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail=f"{field} must be an ISO-8601 time")
    if parsed.tzinfo is None:
        raise HTTPException(status_code=400, detail=f"{field} must carry its time zone")
    return parsed


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


def _sid(value: Any) -> str:
    """A session id from a request: a ULID, or 400 (it was a ValueError, a 500)."""
    try:
        return safe_session_id(value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@asynccontextmanager
async def _session_lock(session_id: str):
    """The recording's transaction (db_v2.session_tx), when the store has one."""
    tx = getattr(_repo(), "session_tx", None)
    if tx is None:
        yield None
        return
    async with tx(session_id) as conn:
        yield conn


def _closed_to_writes(session: Dict[str, Any]) -> Optional[str]:
    """Why nothing more may be added to this recording's chain, or None."""
    if session.get("confirmation") in ("refused", "expired"):
        return "this consultation has been erased"
    if session.get("status") == "quarantined" or session.get("quarantine_reason"):
        return "this recording is quarantined and held for review"
    if session.get("closed_at") is not None or session.get("status") == "archived":
        return "this recording is closed"
    return None


async def _chain_append(session_id: str, entry, device, expected_type: str) -> Tuple[str, str]:
    """
    Add one signed entry to a recording's chain, under the recording's lock.
    Returns (outcome, reason): 'recorded', 'duplicate', 'deferred' (an
    earlier entry has not arrived yet; the PC sends it again), or
    'quarantined' (another entry already holds this number).
    """
    repo = _repo()
    if entry.entry_type != expected_type or (
            isinstance(entry.payload, dict)
            and entry.payload.get("type") not in (None, expected_type)):
        raise HTTPException(status_code=400, detail=f"expected a {expected_type} entry")
    if await _verified_duplicate(session_id, entry, device):
        return "duplicate", ""
    session = await repo.get_session(session_id) or {}
    if session.get("confirmation") in ("refused", "expired"):
        raise HTTPException(status_code=409, detail="this consultation has been erased")
    tip = await repo.chain_tip(session_id) if hasattr(repo, "chain_tip") else None
    if tip is not None:
        expected_no = tip[0] + 1
        if entry.entry_no > expected_no:
            return "deferred", "an earlier chain entry has not arrived yet"
        if entry.entry_no < expected_no:
            await _quarantine(session_id, device,
                              f"entry {entry.entry_no} is already taken by another entry",
                              alert="chain_entry_conflict")
            return "quarantined", "another entry already holds this number"
    expected_prev = tip[1] if tip is not None else await repo.chain_head(session_id)
    verdict = integrity.verify_entry(
        entry, expected_prev=expected_prev, device_pubkey=bytes(device["tpm_pubkey"]))
    if not verdict.ok:
        return "deferred", verdict.reason
    stored = await repo.append_chain_entry(session_id, entry)
    if stored == "conflict":
        await _quarantine(session_id, device,
                          f"entry {entry.entry_no} conflicts with the stored chain",
                          alert="chain_entry_conflict")
        return "quarantined", "another entry already holds this number"
    return ("duplicate" if stored == "duplicate" else "recorded"), ""


async def _quarantine(session_id: str, device, reason: str, *, alert: str,
                      detail: Optional[Dict[str, Any]] = None) -> None:
    repo = _repo()
    await repo.quarantine_session(session_id, reason)
    await repo.raise_alert(alert_type=alert, severity="critical", session_id=session_id,
                           device_id=device["device_id"] if device else None,
                           detail={"reason": reason, **(detail or {})})


def _genesis_problem(genesis, device, body) -> Optional[str]:
    payload = genesis.payload if isinstance(genesis.payload, dict) else {}
    if payload.get("type") != "open":
        return "genesis payload is not an open entry"
    if str(payload.get("device_id", "")).lower() != str(device["device_id"]).lower():
        return "genesis was made by another PC"
    if body.opened_at and payload.get("opened_at"):
        said = _parse_time(body.opened_at)
        signed = _parse_time(payload.get("opened_at"))
        if abs((said - signed).total_seconds()) > 1:
            return "opened_at does not match the signed genesis"
    return None


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
    problem = integrity.device_key_problem(pubkey)
    if problem is not None:
        raise HTTPException(status_code=400, detail=problem)
    kind = integrity.device_key_kind(pubkey)
    # A TPM key proves the PC holds it. Required for P-256 (every recorder that
    # makes one also signs); checked for Ed25519 whenever it is sent.
    if body.key_proof is not None or kind == "p256":
        try:
            proof = _b64url_decode(body.key_proof or "")
        except Exception:
            raise HTTPException(status_code=400, detail="key_proof is not base64url")
        if not integrity.verify_by_device_key(
                pubkey, proof, integrity.enrolment_proof_message(body.enrollment_token,
                                                                 pubkey)):
            raise HTTPException(status_code=400,
                                detail="key_proof does not verify: this PC did not show it "
                                       "holds the key it is registering")

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

    if kind == "p256":
        # A key that cannot leave the PC signs from its first request, so its
        # bearer token alone is worth nothing to anyone who copies it.
        await _repo().mark_device_signs(result["device_id"])
    await _repo().audit(
        event_type="device.enrolled", actor_type="admin",
        device_id=result["device_id"],
        detail={"machine_name": body.machine_name, "app_version": body.app_version,
                "key": kind, "key_storage": body.key_storage[:16]},
    )
    logger.info("Enrolled device %s for doctor %s at hospital %s",
                result["device_id"], result["doctor_id"], result["hospital_id"])
    return result


# A PC registered without a token is pending until CMED's server confirms
# this many of its recordings (the trigger in 025 approves it; keep the two
# equal).
SELF_APPROVAL_CONFIRMATIONS = 2


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)).strip())
    except ValueError:
        return default


def self_registration_enabled() -> bool:
    return os.getenv("AIMS_SELF_REGISTRATION", "on").strip().lower() not in (
        "0", "off", "false", "no")


def pending_upload_quota() -> int:
    return max(_env_int("AIMS_PENDING_UPLOAD_MB", 500), 0) * 1024 * 1024


_REGISTER_REFUSALS = {
    "revoked": (403, "DEVICE_REVOKED", "this PC was revoked or rejected; an administrator "
                                       "has to enrol it again"),
    "other_clinic": (409, "OTHER_CLINIC", "this PC is registered at another clinic; an "
                                          "administrator has to move it"),
    "already_registered": (409, "ALREADY_REGISTERED", "this PC is registered and has "
                                                      "been heard from; it keeps its "
                                                      "token"),
    "key_mismatch": (409, "KEY_MISMATCH", "a different key is registered under this "
                                          "PC's id"),
    "too_many_pending": (429, "TOO_MANY_PENDING", "this clinic already has the most PCs "
                                                  "waiting for approval; an "
                                                  "administrator can approve or reject "
                                                  "them on the dashboard"),
    "too_many_from_address": (429, "TOO_MANY_REGISTRATIONS", "too many PCs registered "
                                                             "from this address today"),
}


@router.post("/device/register")
async def register_device(body: RegisterRequest, request: Request):
    """
    A clinic PC joins without an enrolment token.

    It shows it holds a key that lives in its TPM, and names the clinic CMED's
    own trigger named on it. It joins as *pending*: it records and uploads as
    any PC does, up to AIMS_PENDING_UPLOAD_MB, and nothing it records is kept
    unless CMED's server confirms that visit. Once CMED has confirmed two of
    its recordings, it is approved - in the database, whatever confirmed them.
    An outsider can register, but cannot get past pending: approval needs
    CMED's server to name, to the second, visits that PC really recorded.
    """
    if not self_registration_enabled():
        return _coded(403, "SELF_REGISTRATION_OFF",
                      "this server enrols PCs with an enrolment token only")
    try:
        pubkey = bytes.fromhex(body.device_pubkey)
    except ValueError:
        return _coded(400, "BAD_KEY", "device_pubkey must be hex")
    problem = integrity.device_key_problem(pubkey)
    if problem is not None:
        return _coded(400, "BAD_KEY", problem)
    if integrity.device_key_kind(pubkey) != "p256":
        return _coded(400, "TPM_KEY_REQUIRED",
                      "only a key held in the PC's TPM may register without a token")
    if abs(time.time() - body.timestamp) > DEVICE_SIGNATURE_WINDOW:
        return _coded(401, "CLOCK", "the registration's time is more than five minutes "
                                    "from the server's; check the PC's clock")
    try:
        proof = _b64url_decode(body.key_proof)
    except Exception:
        return _coded(400, "BAD_PROOF", "key_proof is not base64url")
    if not integrity.verify_by_device_key(
            pubkey, proof, integrity.registration_proof_message(
                body.cmed_hospital_id, pubkey, body.timestamp)):
        return _coded(401, "KEY_PROOF_INVALID",
                      "key_proof does not verify: this PC did not show it holds the key")

    repo = _repo()
    hospital_id = await repo.active_clinic_for_cmed(body.cmed_hospital_id)
    if hospital_id is None:
        return _coded(403, "UNKNOWN_CLINIC",
                      "CMED's clinic code is not a clinic this server runs")

    import security
    address = security.client_ip(request)
    device_id = integrity.device_id_for_key(pubkey)
    result = await repo.register_device(
        device_id=device_id, tpm_pubkey=pubkey, hospital_id=hospital_id,
        machine_name=body.machine_name, os_version=body.os_version,
        app_version=body.app_version, protocol_version=body.protocol_version,
        registered_ip=address,
        pending_per_clinic=_env_int("AIMS_PENDING_DEVICES_PER_CLINIC", 5),
        per_ip_per_day=_env_int("AIMS_REGISTRATIONS_PER_ADDRESS_PER_DAY", 10))
    if "refused" in result:
        status, code, message = _REGISTER_REFUSALS[result["refused"]]
        logger.warning("Registration of %s at %s refused: %s", device_id, hospital_id, code)
        return _coded(status, code, message)

    await repo.audit(event_type="device.registered", actor_type="device",
                     device_id=device_id,
                     detail={"machine_name": body.machine_name, "hospital_id": hospital_id,
                             "app_version": body.app_version, "address": address,
                             "approval": result["approval"]})
    await repo.alert_once(
        alert_type="device_registered", group=f"device_registered:{device_id}",
        window=timedelta(days=7), severity="info",
        detail={"device_id": str(device_id), "hospital_id": hospital_id,
                "machine_name": body.machine_name, "address": address,
                "note": "a PC registered itself; it is approved when CMED confirms "
                        f"{SELF_APPROVAL_CONFIRMATIONS} of its recordings, or by hand"})
    logger.info("PC %s (%s) registered itself at %s: %s", device_id, body.machine_name,
                hospital_id, result["approval"])
    return {**result, "pending_upload_mb": pending_upload_quota() // (1024 * 1024),
            "approved_after_confirmations": SELF_APPROVAL_CONFIRMATIONS}


# ============================================================
# Session lifecycle
# ============================================================

@router.post("/session/open")
async def open_session(body: OpenSessionRequest, device=Depends(require_device)):
    try:
        session_id = _sid(body.session_id)
        doctor_id = safe_identifier(body.doctor_id, field="doctor_id")
        patient_ref = safe_identifier(body.patient_ref, field="patient_ref")
        hospital_id = safe_identifier(body.hospital_id, field="hospital_id")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    if await _repo().refusal(session_id):
        raise HTTPException(status_code=409,
                            detail="this consultation was refused by the patient")

    # A retried open is the same PC sending again. Another PC naming a
    # recording it does not own is refused: it would otherwise reach that
    # recording's grant link and continuation, and the audit would show it
    # as an open by the owner.
    existing = await _repo().get_session(session_id)
    if existing is not None and str(existing.get("device_id")) != str(device["device_id"]):
        await _repo().raise_alert(
            alert_type="session_owner_mismatch", severity="critical",
            session_id=session_id, device_id=device["device_id"],
            detail={"owner": str(existing.get("device_id"))})
        raise HTTPException(status_code=409, detail="this recording belongs to another PC")

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
    # The genesis carries no session id, so it is bound here: it must be this
    # PC's, opened at the moment this request says, and never one that has
    # opened another recording. Otherwise a genesis and the entries signed
    # after it could be replayed under a new session id with other audio.
    problem = _genesis_problem(genesis, device, body)
    if problem is None and hasattr(_repo(), "genesis_in_use") \
            and await _repo().genesis_in_use(genesis.entry_hash, session_id):
        problem = "this genesis entry already opened another recording"
    if problem is not None:
        await _repo().raise_alert(
            alert_type="genesis_rejected", severity="critical",
            session_id=None, device_id=device["device_id"],
            detail={"session_id": session_id, "reason": problem})
        raise HTTPException(status_code=400, detail=f"genesis rejected: {problem}")

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

    state = await _link_grant(session_id, body.grant_jti, patient_ref, device,
                              doctor_id=doctor_id)
    if body.continues_session_id:
        state = await _link_continuation(session_id, body.continues_session_id,
                                         patient_ref, device, state)

    await _repo().audit(
        event_type="session.opened", actor_type="device",
        actor_id=doctor_id, device_id=device["device_id"], session_id=session_id,
        detail={"hospital_id": hospital_id, "confirmation": state,
                **({"continues": body.continues_session_id}
                   if body.continues_session_id else {})},
    )
    return {"session_id": session_id, "status": "open", "hospital_id": hospital_id,
            "confirmation": state}


@router.post("/segment/authorize")
async def authorize_segment(body: AuthorizeRequest, device=Depends(require_device)):
    """Issue a short-lived presigned PUT for exactly one segment."""
    session = await _session_for_device(_sid(body.session_id), device)

    max_bytes = int(os.getenv("AIMS_MAX_SEGMENT_BYTES", str(64 * 1024 * 1024)))
    if body.bytes > max_bytes:
        raise HTTPException(status_code=413, detail="segment exceeds the permitted size")

    # A PC that registered itself uploads up to its allowance until CMED has
    # confirmed its first recordings: an outsider who registers can never put
    # more than this on the server, and none of it is kept.
    if device.get("approval") == "pending":
        quota = pending_upload_quota()
        if await _repo().device_upload_bytes(device["device_id"]) + body.bytes > quota:
            refusal = _coded(429, "AWAITING_APPROVAL",
                             "this PC is waiting for CMED to confirm its first "
                             "recordings; it uploads more once approved")
            refusal.headers["Retry-After"] = "300"
            return refusal

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
    Accept a segment only after re-reading it from storage and re-hashing it,
    and only with a signed chain entry that names exactly this piece.

    v1 queued transcription on the client's word that the upload succeeded. Here
    the bytes are fetched back and hashed; a mismatch quarantines the session and
    the agent keeps its local copy. The entry must be a segment entry for this
    seq_no, these bytes and this hash, the next number in the chain - a
    signature alone proves only that the PC signed something.
    """
    repo = _repo()
    session_id = _sid(body.session_id)
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
    #
    # Object storage fails transiently: a dropped connection, a moment of
    # throttling, a read that arrives before the write has settled. Refusing
    # on the first failure makes the recorder upload the whole piece again, so
    # a busy minute costs the network several times what it should. One short
    # retry absorbs that; a piece that still cannot be read is refused, and
    # refusing is right - the server must never record a piece it has not
    # verified. (Seen in the 28-room load test.) Done before the recording's
    # lock is taken: it is the slow part, and it changes nothing.
    loop = asyncio.get_event_loop()
    stored = None
    for attempt, pause in enumerate((0.25, 0.75)):
        try:
            stored = await loop.run_in_executor(None, _read_object, body.object_key)
            break
        except Exception as exc:
            logger.warning("Could not read %s back from storage (attempt %s): %s",
                           body.object_key, attempt + 1, exc)
            await asyncio.sleep(pause)
    if stored is None:
        try:
            stored = await loop.run_in_executor(None, _read_object, body.object_key)
        except Exception as exc:
            logger.error("Could not read %s back from storage: %s",
                         body.object_key, exc)
            raise HTTPException(status_code=502,
                                detail="uploaded object could not be read back")

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

    # The hash proves the bytes arrived intact, not that they are audio. A piece
    # that is not the WAV this recording declared - its rate, channels and
    # sample width - is held, never archived: it would stop the archive worker
    # and could be anything at all. The duration is the server's own reading.
    heard, wav_problem = _wav_check(stored, session)
    if wav_problem is not None:
        async with _session_lock(session_id):
            await _quarantine(session_id, device, f"segment {body.seq_no} is not the "
                              f"declared audio: {wav_problem}", alert="segment_not_audio",
                              detail={"seq_no": body.seq_no})
        return {"status": "quarantined", "reason": wav_problem}

    entry = await _entry_or_400(body.chain_entry)
    binding = integrity.segment_binding_problem(
        entry, seq_no=body.seq_no, sha256_hex=body.sha256, byte_length=body.bytes)
    if binding is not None:
        async with _session_lock(session_id):
            await _quarantine(session_id, device, f"chain entry does not match the "
                              f"segment: {binding}", alert="segment_entry_mismatch",
                              detail={"seq_no": body.seq_no})
        return {"status": "quarantined", "reason": binding}

    async with _session_lock(session_id):
        current = await repo.get_session(session_id) or session

        # A commit whose response was lost gets retried, and the entry arriving
        # the second time is byte-identical to the one already stored. It is
        # answered as the success it is - even after the close, which it may
        # well have raced - because only the original entry hashes to the
        # stored value, so this cannot launder a tampered one.
        if await _verified_duplicate(session_id, entry, device):
            outcome = "duplicate"
        else:
            closed = _closed_to_writes(current)
            if closed is not None:
                # After the close nothing joins a recording: it would get a
                # receipt (so the PC deletes it) without ever being in the file.
                raise HTTPException(status_code=409, detail=closed)
            tip = await repo.chain_tip(session_id) if hasattr(repo, "chain_tip") else None
            expected_prev = tip[1] if tip is not None else await repo.chain_head(session_id)
            if tip is not None and entry.entry_no > tip[0] + 1:
                raise HTTPException(status_code=409,
                                    detail="an earlier chain entry has not arrived yet")
            if tip is not None and entry.entry_no <= tip[0]:
                await _quarantine(session_id, device,
                                  f"entry {entry.entry_no} is already taken by another entry",
                                  alert="chain_entry_conflict", detail={"seq_no": body.seq_no})
                return {"status": "quarantined",
                        "reason": "another entry already holds this number"}
            verdict = integrity.verify_entry(
                entry, expected_prev=expected_prev, device_pubkey=bytes(device["tpm_pubkey"]))
            if not verdict.ok:
                await _quarantine(session_id, device,
                                  f"chain entry rejected: {verdict.reason}",
                                  alert="chain_entry_rejected", detail={"seq_no": body.seq_no})
                return {"status": "quarantined", "reason": verdict.reason}

            outcome = await repo.commit_segment(
                session_id=session_id,
                seq_no=body.seq_no,
                entry=entry,
                object_key=body.object_key,
                byte_length=body.bytes,
                duration_seconds=heard,
                sha256=claimed,
                rms_mean=body.rms_mean,
                captured_start_at=_parse_time(body.captured_start_at),
                captured_end_at=_parse_time(body.captured_end_at),
                is_final=body.is_final,
                clip_name=clip_name(current, body.seq_no),
            )

            if outcome == "conflict":
                # Same sequence number or entry number, different content. Either
                # a bug or an attempt to overwrite already-committed audio.
                await _quarantine(session_id, device,
                                  "duplicate seq_no with a different hash",
                                  alert="segment_conflict", detail={"seq_no": body.seq_no})
                return {"status": "quarantined",
                        "reason": "segment already committed with different content"}
        quarantined = (current.get("status") == "quarantined"
                       or bool(current.get("quarantine_reason")))

    if outcome == "stored":
        await _queue_transcription(session_id, body, session)
    if quarantined:
        # Held for review: the PC keeps its copy, so no receipt.
        return {"status": "quarantined", "reason": "this recording is held for review"}
    if outcome == "duplicate":
        logger.info("Segment %s of %s was already committed; treating the retry "
                    "as the success it is", body.seq_no, session_id)
    await _issue_custody_receipt(session_id, body.seq_no, claimed)
    return {"status": "committed", "seq_no": body.seq_no, "object_key": body.object_key,
            "duplicate": outcome == "duplicate"}


def _wav_check(data: bytes, session: Dict[str, Any]) -> Tuple[float, Optional[str]]:
    """
    (seconds of audio, None) for a WAV matching the recording's declared
    format, else (0, why not). Read from the bytes already in hand.
    """
    try:
        with wave.open(io.BytesIO(data), "rb") as w:
            rate, channels, width = w.getframerate(), w.getnchannels(), w.getsampwidth()
            frames = w.getnframes()
    except (wave.Error, EOFError, ValueError) as exc:
        return 0.0, f"not a WAV file ({exc})"
    if rate <= 0 or frames <= 0:
        return 0.0, "the WAV holds no audio"
    if len(data) < 44 + frames * channels * width:
        return 0.0, "the WAV is shorter than its header says"
    for name, got in (("sample_rate", rate), ("channels", channels),
                      ("sample_width", width)):
        want = session.get(name)
        if want is not None and int(want) != got:
            return 0.0, f"{name} is {got}, the recording declared {want}"
    return frames / rate, None


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


async def _delete_bucket_objects(session_id: str, *, only_archived: bool = True) -> int:
    """
    Remove a session's clips from object storage.

    Called only after the archive copy is verified and receipts are signed, and
    then only for pieces marked archived - the ones the worker joined. A piece
    that is not in the archive file is never deleted from the bucket on the
    strength of its neighbours. (An erase removes everything: only_archived=False.)
    A failure is logged and left for the retry index rather than raised: the
    audio is already safe on the AIMS LAB server.
    """
    repo = _repo()
    loop = asyncio.get_event_loop()
    removed = 0

    for segment in await repo.segments_for(session_id):
        if segment.get("object_deleted_at") is not None:
            continue
        if only_archived and segment.get("state") not in (None, "archived"):
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
    duplicate or out-of-order arrival is tolerated: an entry whose predecessor
    has not landed is "deferred" and sent again.
    """
    repo = _repo()
    session_id = _sid(body.session_id)
    await _session_for_device(session_id, device)

    entry = await _entry_or_400(body.chain_entry)
    async with _session_lock(session_id):
        outcome, reason = await _chain_append(session_id, entry, device, expected_type)
    if outcome == "duplicate":
        return {"status": "recorded", "entry_no": entry.entry_no, "duplicate": True}
    if outcome != "recorded":
        logger.info("%s %s entry for %s: %s", outcome.capitalize(), expected_type,
                    session_id, reason)
        return {"status": outcome, "reason": reason}

    await repo.audit(
        event_type=f"session.{expected_type}", actor_type="device",
        actor_id=str(entry.payload.get("authorised_by", "")),
        device_id=device["device_id"], session_id=session_id,
        detail={k: entry.payload.get(k) for k in ("reason", "reason_detail",
                                                  "authorised_by", "supervisor_required")},
    )
    return {"status": "recorded", "entry_no": entry.entry_no}


MARKER_EVENTS = frozenset({labelling.PATIENT_OPENED, labelling.PRESCRIPTION_BUILT})


@router.post("/session/marker")
async def session_marker(body: MarkerRequest, device=Depends(require_device)):
    """
    A patient opened, or a prescription built, during a recording - signed into
    its chain with the second of audio it happened at.

    Nothing is decided here. The recording carries on, and when it closes the
    rules in labelling.py read every marker with the prescriptions and say who
    each stretch belongs to. The entry is always kept once it verifies - it is a
    link in the chain - and a problem with what it says is alerted, not refused.
    """
    repo = _repo()
    session_id = _sid(body.session_id)
    session = await _session_for_device(session_id, device)

    entry = await _entry_or_400(body.chain_entry)
    async with _session_lock(session_id):
        outcome, reason = await _chain_append(session_id, entry, device, "marker")
    if outcome not in ("recorded", "duplicate"):
        logger.info("%s marker for %s: %s", outcome.capitalize(), session_id, reason)
        return {"status": outcome, "reason": reason}
    duplicate = outcome == "duplicate"

    payload = entry.payload
    try:
        event = payload.get("event")
        if event not in MARKER_EVENTS:
            raise FieldError("MISSING_FIELD", "event", f"unknown marker event {event!r}")
        visit = conf.parse_visit(payload.get("patient") or {})
        seconds = float(payload.get("audio_seconds"))
        if not seconds >= 0:
            raise FieldError("MISSING_FIELD", "audio_seconds", "must be 0 or more")
        at = _parse_time(payload.get("at"))
    except (FieldError, TypeError, ValueError) as exc:
        await repo.raise_alert(
            alert_type="marker_unreadable", severity="warning",
            session_id=session_id, device_id=device["device_id"],
            detail={"entry_no": entry.entry_no, "problem": str(exc)})
        return {"status": "recorded", "entry_no": entry.entry_no, "used": False,
                **({"duplicate": True} if duplicate else {})}

    jti = None
    if body.grant_jti:
        grant = await repo.get_authorisation(body.grant_jti)
        if (grant is not None
                and str(grant["device_id"]) == str(device["device_id"])
                and grant["patient_id"] == visit.patient_id
                and grant["start_time"] == visit.start_time
                and grant.get("session_id") in (None, session_id)):
            await repo.link_authorisation(grant["jti"], session_id)
            jti = grant["jti"]
        else:
            await repo.raise_alert(
                alert_type="marker_grant_invalid", severity="warning",
                session_id=session_id, device_id=device["device_id"],
                detail={"entry_no": entry.entry_no, "event": event})

    await repo.record_click(session_id, entry_no=entry.entry_no, event=event,
                            audio_seconds=round(seconds, 3), occurred_at=at,
                            visit=visit, grant_jti=jti)
    if not duplicate:
        await repo.audit(
            event_type=f"session.{event}", actor_type="device", actor_id=visit.doctor_id,
            device_id=device["device_id"], session_id=session_id,
            detail={"entry_no": entry.entry_no, "audio_seconds": round(seconds, 3),
                    "grant_linked": jti is not None})
    # A marker the PC sent after the recording closed (it was offline) is
    # evidence too: decide again.
    if session.get("closed_at"):
        await relabel_session(session_id)
    return {"status": "recorded", "entry_no": entry.entry_no, "used": True,
            "grant_linked": jti is not None,
            **({"duplicate": True} if duplicate else {})}


# A wrong profile corrected within the first minutes is the expected case. A
# move made much later probably means two consultations ran together - the
# doctor skipped one prescription - and a person should split the recording.
REASSIGN_REVIEW_AFTER = timedelta(minutes=5)


@router.post("/session/reassign")
async def reassign_session(body: ReassignRequest, device=Depends(require_device)):
    """
    The prescription was built for a patient opened during this recording, not
    the one it started under: the doctor picked the wrong profile first. The
    whole recording moves to the right patient.

    The entry is always kept - it is a link in the chain, and dropping it would
    break every entry after it. The move itself is applied only when the
    target is a valid consultation at this PC's clinic with a grant from this
    device; otherwise the recording stays where it was, intact, and a person
    is alerted. Nothing is ever deleted here.
    """
    repo = _repo()
    session_id = _sid(body.session_id)
    session = await _session_for_device(session_id, device)

    entry = await _entry_or_400(body.chain_entry)
    async with _session_lock(session_id):
        outcome, reason = await _chain_append(session_id, entry, device, "reassign")
    if outcome == "duplicate":
        return {"status": "recorded", "entry_no": entry.entry_no, "duplicate": True}
    if outcome != "recorded":
        logger.info("%s reassign entry for %s: %s", outcome.capitalize(), session_id, reason)
        return {"status": outcome, "reason": reason}

    problem = None
    try:
        target = conf.parse_visit(entry.payload.get("to") or {})
    except FieldError as exc:
        target, problem = None, f"target is not a valid consultation: {exc}"
    clinic = None
    if target is not None:
        clinic = await _clinic_for(target.cmed_hospital_id, device)
        if clinic is None:
            problem = "target consultation is at another clinic"
    grant = await repo.get_authorisation(body.grant_jti) if body.grant_jti else None
    if problem is None and not (
            grant is not None
            and str(grant["device_id"]) == str(device["device_id"])
            and grant["patient_id"] == target.patient_id
            and grant["start_time"] == target.start_time
            and grant.get("session_id") in (None, session_id)):
        problem = "no valid grant for the target consultation"
    if problem is not None:
        await repo.raise_alert(
            alert_type="reassign_not_applied", severity="critical",
            session_id=session_id, device_id=device["device_id"],
            detail={"reason": problem, "entry_no": entry.entry_no})
        logger.warning("Reassign of %s recorded but not applied: %s", session_id, problem)
        return {"status": "recorded", "entry_no": entry.entry_no, "reassigned": False,
                "reason": problem}

    await repo.reassign_session(session_id, patient_id=target.patient_id,
                                doctor_id=target.doctor_id)
    await repo.link_authorisation(grant["jti"], session_id)
    state = "confirmed" if grant.get("notice_id") else "pending"
    await repo.set_session_confirmation(session_id, state, grant_jti=grant["jti"])

    # Usually the move arrives while the recording is still open, and the file
    # is named at close with the right patient. If the PC was offline and the
    # recording has already closed, it was named after the wrong patient: name
    # it again now. If it was archived too, its JSON is rewritten and a person
    # is told, because the archived folder still carries the first name.
    if session.get("closed_at"):
        await _record_file_name(session_id)
        if await repo.mark_json_stale(session_id):
            await repo.raise_alert(
                alert_type="reassigned_after_archive", severity="warning",
                session_id=session_id, device_id=device["device_id"],
                detail={"patient_id": target.patient_id})

    opened_at = session.get("opened_at")
    raw_opened = entry.payload.get("patient_opened_at")
    target_opened = _parse_time(raw_opened) if raw_opened else None
    gap = ((target_opened - opened_at).total_seconds()
           if opened_at is not None and target_opened is not None else 0.0)
    if gap > REASSIGN_REVIEW_AFTER.total_seconds():
        await repo.raise_alert(
            alert_type="possibly_two_consultations", severity="warning",
            session_id=session_id, device_id=device["device_id"],
            detail={"split_at": entry.payload.get("patient_opened_at"),
                    "minutes_into_recording": round(gap / 60, 1)})
    await repo.audit(
        event_type="session.reassigned", actor_type="device",
        actor_id=target.doctor_id, device_id=device["device_id"], session_id=session_id,
        detail={"seconds_after_start": round(gap, 1), "confirmation": state,
                "previous_doctor": session.get("doctor_id")})
    logger.info("Session %s moved to the patient its prescription was built for", session_id)
    return {"status": "recorded", "entry_no": entry.entry_no, "reassigned": True,
            "confirmation": state}


# How a consultation ends in v3, none of which needs anyone's attention: the
# doctor's Stop, the next patient after a prescription, the counselling time
# after it running out, no CMED activity for a long time, or the PC shut down.
NORMAL_CLOSE_REASONS = frozenset({
    "doctor_stopped", "superseded_by_new_patient", "counselling_limit",
    "no_activity_limit", "pc_shutdown",
    # The recorder closes its recording itself while an installer upgrades it.
    "software_update",
})


@router.post("/session/close")
async def close_session(body: CloseRequest, device=Depends(require_device)):
    """
    Close a session and verify its whole chain.

    Any gap, break or bad signature quarantines the session: it is held for review,
    never archived automatically, and the agent never gets a purge receipt for it.

    Completeness is proven by the chain, not by the count the PC sends: the
    close entry names the segment count and is signed, and it can only be
    added once every entry before it is here. Every segment row must match
    its own chain entry. A repeated close of a recording already closed,
    archived or quarantined changes nothing and says where it stands.
    """
    repo = _repo()
    session_id = _sid(body.session_id)
    await _session_for_device(session_id, device)

    async with _session_lock(session_id):
        session = await repo.get_session(session_id) or {}
        if session.get("confirmation") in ("refused", "expired"):
            raise HTTPException(status_code=409, detail="this consultation has been erased")
        if session.get("status") == "quarantined" or session.get("quarantine_reason"):
            return {"status": "quarantined",
                    "reason": session.get("quarantine_reason") or "held for review"}
        if session.get("closed_at") is not None:
            chain = await repo.load_chain(session_id)
            return {"status": "closed", "chain_ok": True, "already": True,
                    **integrity.chain_summary(chain)}

        close_entry = None
        if body.chain_entry:
            close_entry = await _entry_or_400(body.chain_entry)
            if close_entry.entry_type != "close":
                raise HTTPException(status_code=400, detail="expected a close entry")
            outcome, reason = await _chain_append(session_id, close_entry, device, "close")
            if outcome == "quarantined":
                return {"status": "quarantined", "reason": reason}
            if outcome == "deferred":
                tip = await repo.chain_tip(session_id) if hasattr(repo, "chain_tip") else None
                if tip is not None and close_entry.entry_no == tip[0] + 1:
                    # Next in line yet it does not verify: a forged or
                    # damaged close, not one still waiting for its clips.
                    await _quarantine(session_id, device,
                                      f"close entry rejected: {reason}",
                                      alert="chain_invalid")
                    return {"status": "quarantined", "reason": reason}
                return await _incomplete(session_id, device, body, session,
                                         agent_segments=_signed_count(close_entry, body))

        chain = await repo.load_chain(session_id)
        verdict = integrity.verify_chain(chain, device_pubkey=bytes(device["tpm_pubkey"]))
        summary = integrity.chain_summary(chain)

        if not verdict.ok:
            await _quarantine(session_id, device, verdict.reason, alert="chain_invalid",
                              detail={"failed_entry": verdict.failed_entry_no})
            await repo.audit(
                event_type="session.quarantined", actor_type="device",
                device_id=device["device_id"], session_id=session_id,
                detail={"reason": verdict.reason})
            return {"status": "quarantined", "reason": verdict.reason,
                    "failed_entry_no": verdict.failed_entry_no}

        stored_segments = await repo.segments_for(session_id)
        mismatch = _segments_against_chain(chain, stored_segments)
        if mismatch is not None:
            await _quarantine(session_id, device, mismatch, alert="chain_invalid")
            return {"status": "quarantined", "reason": mismatch}

        expected = _signed_count(close_entry, body)
        if len(stored_segments) != expected:
            if close_entry is not None:
                # The close entry is in the chain, so nothing is still on its
                # way: the signed count and the chain disagree.
                reason = (f"the signed close names {expected} segment(s); the chain "
                          f"holds {len(stored_segments)}")
                await _quarantine(session_id, device, reason, alert="chain_invalid")
                return {"status": "quarantined", "reason": reason}
            return await _incomplete(session_id, device, body, session,
                                     agent_segments=expected,
                                     server_segments=len(stored_segments))

        chain_head = chain[-1].entry_hash if chain else None
        closed_at = _parse_time(body.closed_at)
        # The clinic's wall clock, resolved here rather than by the database
        # (SRS-SES-04). An unresolvable zone is logged and falls back to UTC; it
        # never fails the close, because a close that fails is a recording that
        # is never archived.
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

    # Who each stretch belongs to, then the shared file name(s) and the
    # clinical visit(s) they belong to.
    if await relabel_session(session_id) is None:
        await _record_file_name(session_id)

    # A consultation normally ends because the doctor pressed Stop in CMED.
    # Anything else - stopped from the tray icon, superseded by the next patient,
    # recovered after the PC died mid-consultation - is worth someone's attention
    # the same morning, not a fact buried in a log file on a machine in a
    # consulting room.
    if body.close_reason and body.close_reason not in NORMAL_CLOSE_REASONS:
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


def _signed_count(close_entry, body: "CloseRequest") -> int:
    """The segment count the PC signed into its close entry, else the one it sent."""
    if close_entry is not None and isinstance(close_entry.payload, dict):
        value = close_entry.payload.get("segment_count")
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
    return body.segment_count


def _segments_against_chain(chain, segments) -> Optional[str]:
    """Every stored piece matches its own chain entry, and every segment entry a piece."""
    by_no = {e.entry_no: e for e in chain}
    seen = set()
    for row in segments:
        entry = by_no.get(row.get("entry_no")) if row.get("entry_no") is not None else None
        if entry is None:
            entry = next((e for e in chain if e.entry_type == "segment"
                          and isinstance(e.payload, dict)
                          and e.payload.get("seq_no") == row["seq_no"]), None)
        if entry is None:
            return f"segment {row['seq_no']} has no chain entry"
        problem = integrity.segment_binding_problem(
            entry, seq_no=row["seq_no"], sha256_hex=bytes(row["sha256"]).hex(),
            byte_length=row.get("bytes"))
        if problem is not None:
            return problem
        seen.add(entry.entry_no)
    extra = [e.entry_no for e in chain if e.entry_type == "segment" and e.entry_no not in seen]
    if extra:
        return f"chain segment entries {extra[:5]} have no stored piece"
    return None


async def _incomplete(session_id: str, device, body: "CloseRequest", session,
                      *, agent_segments: int, server_segments: Optional[int] = None):
    """
    The PC recorded segments the server has not received yet - almost always
    the last clip still uploading. Answered "incomplete" and sent again; a
    person is told once, and only if they are still missing ten minutes on.
    """
    repo = _repo()
    if server_segments is None:
        server_segments = len(await repo.segments_for(session_id))
    closed_for = (datetime.now(timezone.utc) - _parse_time(body.closed_at)).total_seconds()
    if closed_for >= SEGMENTS_LATE_AFTER.total_seconds():
        first = (await repo.mark_late_alerted(session_id)
                 if hasattr(repo, "mark_late_alerted") else True)
        if first:
            await repo.raise_alert(
                alert_type="segment_count_mismatch", severity="warning",
                session_id=session_id, device_id=device["device_id"],
                detail={"agent": agent_segments, "server": server_segments,
                        "minutes_since_close": round(closed_for / 60, 1)})
    return {"status": "incomplete", "reason": "not all segments have been committed",
            "server_segments": server_segments, "agent_segments": agent_segments}


@router.get("/session/{session_id}/receipts")
async def session_receipts(session_id: str, device=Depends(require_device)):
    """Purge receipts the agent may act on - one per piece, issued the moment the
    piece is verified on arrival (SRS-REC-15)."""
    sid = _sid(session_id)
    await _session_for_device(sid, device)
    return {"session_id": sid, "receipts": await _repo().receipts_for(sid)}


@router.post("/heartbeat")
async def heartbeat(body: HeartbeatRequest, device=Depends(require_device)):
    """
    Liveness and spool depth.

    A missing heartbeat is how a killed agent or a stalled upload queue becomes
    visible centrally rather than being discovered weeks later.
    """
    held = None
    if body.held is not None:
        held = [{"session_id": str(h.get("session_id", ""))[:64],
                 "code": str(h.get("code", ""))[:64],
                 "since": str(h.get("since", ""))[:40]}
                for h in body.held[:20] if isinstance(h, dict)]
    await _repo().touch_device(
        device["device_id"],
        spool_bytes=body.spool_bytes,
        pending_segments=body.pending_segments,
        app_version=body.app_version or None,
        held_sessions=held,
        held_count=body.held_count if body.held_count is not None
        else (len(held) if held is not None else None),
        disk_encrypted=body.disk_encrypted,
        clock_offset_seconds=body.clock_offset_seconds,
    )
    await _check_recorder(device, body)
    if body.spool_pressure == "critical":
        await _repo().raise_alert(
            alert_type="spool_critical", severity="critical",
            device_id=device["device_id"], session_id=body.session_id,
            detail={"spool_bytes": body.spool_bytes,
                    "pending_segments": body.pending_segments})
    await _check_clock(device, body)
    return {"status": "ok"}


def min_recorder_version() -> str:
    return os.getenv("AIMS_MIN_RECORDER_VERSION", "3.4.0").strip() or "3.4.0"


def version_tuple(value: Optional[str]) -> Tuple[int, ...]:
    """'3.4.0' -> (3, 4, 0); anything unreadable sorts below every real version."""
    parts = []
    for piece in str(value or "").strip().lstrip("v").split("."):
        digits = "".join(ch for ch in piece if ch.isdigit())
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts) or (0,)


def recorder_outdated(app_version: Optional[str]) -> bool:
    return bool(app_version) and version_tuple(app_version) < version_tuple(min_recorder_version())


RECORDER_ALERT_REPEAT = timedelta(days=1)


async def _check_recorder(device, body: "HeartbeatRequest") -> None:
    """
    V2 and X1: a PC still on an old build keeps the old failure modes, and a
    PC whose disk is not encrypted gives up its audio with the laptop. Each
    is said once a day per PC, not on every heartbeat.
    """
    repo = _repo()
    since = datetime.now(timezone.utc) - RECORDER_ALERT_REPEAT
    if recorder_outdated(body.app_version) and not await repo.alert_raised_since(
            "recorder_outdated", device["device_id"], since):
        await repo.raise_alert(
            alert_type="recorder_outdated", severity="warning", device_id=device["device_id"],
            detail={"app_version": body.app_version, "minimum": min_recorder_version(),
                    "detail": "Reinstall this PC with the current AIMScribeSetup.exe"})
    if body.disk_encrypted is False and not await repo.alert_raised_since(
            "disk_not_encrypted", device["device_id"], since):
        await repo.raise_alert(
            alert_type="disk_not_encrypted", severity="warning", device_id=device["device_id"],
            detail={"detail": "Turn on BitLocker on this PC: a stolen laptop's audio is "
                              "otherwise readable by anyone who can sign in to it"})


# A close answered "incomplete" is normal for a few seconds; this long is not.
SEGMENTS_LATE_AFTER = timedelta(minutes=10)


# A PC clock this far off is reported. Recording and uploads still work - the
# recorder checks grants against the server's time - but the times it stamps
# (and so file names) follow its own clock, so someone should fix time sync.
CLOCK_SKEW_ALERT_SECONDS = 120
CLOCK_SKEW_REPEAT = timedelta(hours=6)
_CLOCK_ALERTED: Dict[str, datetime] = {}


async def _check_clock(device, body: HeartbeatRequest) -> None:
    offset = body.clock_offset_seconds
    if offset is None and body.sent_at:
        sent = _parse_time(body.sent_at)
        if sent.tzinfo is not None:
            offset = (datetime.now(timezone.utc) - sent).total_seconds()
    if offset is None or abs(offset) < CLOCK_SKEW_ALERT_SECONDS:
        return
    key, now = str(device["device_id"]), datetime.now(timezone.utc)
    last = _CLOCK_ALERTED.get(key)
    if last is not None and now - last < CLOCK_SKEW_REPEAT:
        return
    _CLOCK_ALERTED[key] = now
    await _repo().raise_alert(
        alert_type="clock_skew", severity="warning", device_id=device["device_id"],
        session_id=body.session_id,
        detail={"offset_seconds": round(offset, 1),
                "pc_clock": "behind" if offset > 0 else "ahead",
                "detail": "Turn on Windows time sync on this PC"})


# ============================================================
# Archive handshake (Option B - the AIMS LAB worker pulls)
# ============================================================

class HealthEntry(BaseModel):
    seq: int = Field(..., ge=0)
    kind: str = Field(..., pattern="^(heartbeat|alert)$")
    at: str
    data: Dict[str, Any] = Field(default_factory=dict)


class HealthLogRequest(BaseModel):
    entries: List[HealthEntry] = Field(..., max_length=500)


# Recorder alerts that mean a consultation may be missing or damaged. Anything
# else is a warning: worth reading the same day, not worth waking anyone.
RECORDER_CRITICAL = frozenset({
    "capture_failed", "microphone_unavailable", "start_abandoned", "disk_full",
    "local_chain_invalid", "local_segment_unreadable", "invalid_purge_receipt",
    "unexpected_agent_exit",
})


@router.post("/device/health")
async def device_health(body: HealthLogRequest, device=Depends(require_device)):
    """
    What a recorder saw while it could not reach this server - a line a minute
    and every alert - delivered when the network is back, and also each alert
    the moment it happens when it can.

    Stored before the reply, so a "stored" answer is what lets the recorder
    delete its copy. A batch sent twice stores nothing twice, and an alert is
    raised here only the first time it arrives.
    """
    entries = []
    for e in body.entries:
        at = _parse_time(e.at)
        data = e.data if len(json.dumps(e.data, default=str)) <= 8_000 else {"truncated": True}
        entries.append({"seq": e.seq, "kind": e.kind, "at": at, "data": data})
    if not entries:
        return {"status": "stored", "stored": 0, "last_seq": None}

    repo = _repo()
    new = set(await repo.store_health(device["device_id"], entries))
    now = datetime.now(timezone.utc)
    for e in entries:
        if e["kind"] != "alert" or e["seq"] not in new:
            continue
        kind = str(e["data"].get("alert_type") or "recorder_alert")[:64]
        late = (now - e["at"]).total_seconds() > 120
        await repo.raise_alert(
            alert_type=kind,
            severity="critical" if kind in RECORDER_CRITICAL else "warning",
            session_id=e["data"].get("session_id"), device_id=device["device_id"],
            detail={"detail": e["data"].get("detail"), "from": "recorder",
                    "occurred_at": e["at"].isoformat(), "reported_late": late})
    return {"status": "stored", "stored": len(new), "last_seq": max(e["seq"] for e in entries)}


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
            # The name this recording was given when it closed (SRS-SES-05).
            # Without it the worker falls back to the older form and the file
            # on disk no longer matches what the database, the clinical record
            # and the cloud copy all call it - which is exactly what happened
            # on the bench before this line existed.
            "file_stem": session.get("file_stem"),
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
            **(await _parts_payload(session)),
            **(await _continuation_payload(session["session_id"])),
        })
    return {"sessions": payload}


async def _continuation_payload(session_id: str) -> Dict[str, Any]:
    """S4: {"continues": {...}} and/or {"continued_by": {...}}, each
    {session_id, file_stem}; nothing for a recording that stands alone."""
    links = await _repo().continuation_links(session_id)
    return {k: v for k, v in links.items() if v}


async def _parts_payload(session: Dict[str, Any]) -> Dict[str, Any]:
    """The cut, for a recording that is more than one patient's."""
    if int(session.get("parts_count") or 1) <= 1:
        return {}
    return {"parts": [{
        "part_no": p["part_no"],
        "start_seconds": float(p["start_seconds"]),
        "end_seconds": float(p["end_seconds"]) if p["end_seconds"] is not None else None,
        "file_stem": p["file_stem"],
        "patient_id": p["patient_id"],
        "doctor_id": p["doctor_id"],
        "rule": p["rule"],
        "needs_review": p["needs_review"],
        "archive_relpath": p.get("archive_relpath"),
        "sha256": bytes(p["archive_sha256"]).hex() if p.get("archive_sha256") else None,
    } for p in await _repo().parts_for(str(session["session_id"]))]}


@router.get("/archive/recut/pending")
async def archive_recut_pending(limit: int = 5, _: None = Depends(require_worker)):
    """
    Archived recordings whose labels changed where the files are cut - a
    prescription arrived after archiving. The worker rebuilds the audio from
    the parts it holds and cuts it again.
    """
    limit = max(1, min(limit, 20))
    out = []
    for s in await _repo().pending_recut(limit):
        out.append({
            "session_id": s["session_id"],
            "hospital_id": s["hospital_id"],
            "doctor_id": s["doctor_id"],
            "patient_ref": s["patient_id"],
            "session_date": s["session_date"].isoformat() if s["session_date"] else None,
            "file_stem": s["file_stem"],
            "timezone": await _repo().hospital_timezone(s["hospital_id"]),
            "opened_at": s["opened_at"].isoformat() if s["opened_at"] else None,
            "closed_at": s["closed_at"].isoformat() if s["closed_at"] else None,
            "audio": {"sample_rate": s.get("sample_rate"), "channels": s.get("channels"),
                      "sample_width": s.get("sample_width")},
            "archive_relpath": s["archive_relpath"],
            "archive_sha256": bytes(s["archive_sha256"]).hex() if s["archive_sha256"] else None,
            "archive_bytes": s["archive_bytes"],
            # The files on disk now; `parts` is the new cut.
            "existing_parts": [{"part_no": p["part_no"],
                                "archive_relpath": p["archive_relpath"]}
                               for p in await _repo().archived_parts_for(s["session_id"])],
            "parts": (await _parts_payload({**s, "parts_count": 2})).get("parts", []),
        })
    return {"sessions": out}


@router.post("/archive/complete")
async def archive_complete(body: ArchiveCompleteRequest, _: None = Depends(require_worker)):
    """
    The worker reports a verified archive copy; the server issues purge receipts.

    This is the only place receipts are minted, and it happens only after the
    worker has re-read the archive file from disk and confirmed its hash. That is
    what makes deleting the agent's local copy safe. The worker names the
    pieces it joined; they must be exactly the pieces held for the recording,
    and only they are receipted, marked archived and later deleted.
    """
    repo = _repo()
    session_id = _sid(body.session_id)
    if ctx.signer is None:
        # Without a signing key nothing may authorise deletion. Failing here keeps
        # every local copy, which is the safe direction.
        raise HTTPException(status_code=503,
                            detail="receipt signing key is not configured")
    try:
        archive_hash = bytes.fromhex(body.sha256)
        joined = {(j.seq_no, bytes.fromhex(j.sha256)) for j in body.segments or []}
    except ValueError:
        raise HTTPException(status_code=400, detail="sha256 must be hex")

    async with _session_lock(session_id):
        session = await repo.get_session(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="session not found")
        if session.get("confirmation") in ("refused", "expired"):
            raise HTTPException(status_code=409, detail="this consultation has been erased")
        if session["status"] == "quarantined" or session.get("quarantine_reason"):
            raise HTTPException(status_code=409, detail="session is quarantined")

        recut = session.get("archived_at") is not None and bool(session.get("parts_stale"))
        if session.get("archived_at") is not None and not recut:
            # Reported twice (the worker was interrupted after the first): the
            # receipts were issued then.
            return {"status": "archived", "already": True, "receipts_issued": 0,
                    "objects_deleted": 0, "copy_pending": ctx.copy is not None}

        if not recut:
            if body.segments is None:
                logger.error("Archive worker reported %s without the pieces it joined; "
                             "refused (update the worker)", session_id)
                raise HTTPException(status_code=409,
                                    detail="report the segments joined (seq_no, sha256)")
            held = {(s["seq_no"], bytes(s["sha256"])) for s in await repo.segments_for(session_id)
                    if s.get("state") in (None, "committed", "archived")}
            if joined != held:
                await repo.raise_alert(
                    alert_type="archive_segments_mismatch", severity="critical",
                    session_id=session_id,
                    detail={"joined": sorted(n for n, _ in joined),
                            "held": sorted(n for n, _ in held)})
                raise HTTPException(status_code=409,
                                    detail="the pieces joined are not the pieces held; "
                                           "archive it again")

        if not await repo.mark_archived(session_id, relpath=body.archive_relpath,
                                        sha256=archive_hash, byte_length=body.bytes):
            raise HTTPException(status_code=409, detail="the recording can no longer be "
                                                        "archived")
        if body.parts:
            for part in body.parts:
                try:
                    part_hash = bytes.fromhex(part.sha256)
                except ValueError:
                    raise HTTPException(status_code=400, detail="part sha256 must be hex")
                await repo.mark_part_archived(session_id, part.part_no,
                                              relpath=part.archive_relpath,
                                              sha256=part_hash, byte_length=part.bytes)
            await repo.set_parts_stale(session_id, False)

        if not recut:
            issued = 0
            now = datetime.now(timezone.utc)
            for seq_no, digest in sorted(joined):
                receipt = ctx.signer.sign_segment(
                    session_id=session_id, seq_no=seq_no, sha256_hex=digest.hex(),
                    archived_at=now)
                await repo.store_receipt(
                    session_id=session_id, scope="segment", seq_no=seq_no,
                    sha256=digest, payload=receipt["payload"],
                    signature=bytes.fromhex(receipt["signature"]))
                issued += 1
            session_receipt = ctx.signer.sign_session(
                session_id=session_id, sha256_hex=body.sha256, archived_at=now)
            await repo.store_receipt(
                session_id=session_id, scope="session", seq_no=None,
                sha256=archive_hash, payload=session_receipt["payload"],
                signature=bytes.fromhex(session_receipt["signature"]))
            await repo.mark_segments_archived(session_id, [n for n, _ in joined])

    if recut:
        # Cut again after a late prescription moved a boundary. The receipts
        # were issued the first time; the pieces may already be gone.
        await repo.audit(
            event_type="session.recut", actor_type="service", actor_id="archive-worker",
            session_id=session_id,
            detail={"parts": [p.archive_relpath for p in body.parts or []]})
        return {"status": "recut", "receipts_issued": 0, "objects_deleted": 0,
                "copy_pending": ctx.copy is not None}

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
    # One patient's part of a recording cut into several.
    part_no: Optional[int] = Field(None, ge=1, le=1000)


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
    part_no: Optional[int] = Field(None, ge=1, le=1000)
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
    # The raw WAV, encrypted (decided with CMED and the study team): no
    # transcoding stands between the archive and its copy. Older copies made
    # as FLAC keep their .flac.enc names and still restore.
    suffix = "wav.enc" if kind == "audio" else "json.enc"
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
        **(await _parts_payload(s)),
        **(await _continuation_payload(s["session_id"])),
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
        **(await _parts_payload(s)),
        **(await _continuation_payload(s["session_id"])),
    } for s in sessions]}


@router.get("/archive/clinical/{session_id}")
async def archive_clinical_document(session_id: str, part: Optional[int] = None,
                                    _: None = Depends(require_worker)):
    """
    The JSON that is written beside the audio and travels with it into the
    cloud copy (§8.8, SRS-CRI-05).

    Assembled here rather than at UIU: the clinical database has its own
    credentials and the worker holds none of them.
    """
    session_id = _sid(session_id)
    session = await _repo().get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    if part is None:
        return await _clinical_document(session)
    found = next((p for p in await _repo().parts_for(session_id)
                  if p["part_no"] == part), None)
    if found is None:
        raise HTTPException(status_code=404, detail="no such part")
    return await _clinical_document(session, part=found)


async def _marking(session: Dict[str, Any]) -> Dict[str, Any]:
    """
    Whether a stored recording is in the dataset, and if not, why not - in
    words a person reading the JSON understands.
    """
    state = session.get("confirmation")
    if state in conf.DATASET:
        return {"cmed_confirmed": True, "reason": None}
    reason = {"legacy": "recorded by an old recorder, outside CMED's confirmation",
              "pending": "waiting for CMED's patient information (API 2)",
              "unconfirmed": "CMED's patient information (API 2) has not arrived"}.get(
                  state, f"not confirmed ({state})")
    grant = None
    if session.get("grant_jti"):
        try:
            grant = await _repo().get_authorisation(session["grant_jti"])
        except Exception:
            grant = None
    if grant is not None and grant.get("cmed_hospital_id"):
        cmed_id, own = grant["cmed_hospital_id"], grant.get("hospital_id")
        mapped = await _cmed_clinic(cmed_id)
        if not (mapped == own or (mapped is None and cmed_id == own)):
            reason = (f"CMED named hospital {cmed_id!r}, which is not this PC's "
                      f"hospital {own!r}")
    return {"cmed_confirmed": False, "reason": reason}


async def _clinical_document(session: Dict[str, Any],
                             part: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    repo = _repo()
    session_id = str(session["session_id"])
    document: Dict[str, Any] = {
        "file_stem": part["file_stem"] if part else session.get("file_stem")}

    visit = None
    if part is not None:
        visit = conf.Visit(
            patient_id=part["patient_id"], doctor_id=part["doctor_id"],
            cmed_hospital_id=part["cmed_hospital_id"], start_time=part["start_time"],
            visit_date=part["visit_date"].isoformat()
            if hasattr(part["visit_date"], "isoformat") else str(part["visit_date"]))
    elif session.get("grant_jti"):
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
    links = await _repo().continuation_links(session_id)
    marking = await _marking(session)
    document["recording"] = {
        "session_id": session_id,
        # The marking: whether this recording belongs in the dataset.
        "cmed_confirmed": marking["cmed_confirmed"],
        "confirmation": session.get("confirmation"),
        **({"unconfirmed_reason": marking["reason"]} if marking["reason"] else {}),
        "opened_at": started.isoformat() if started else None,
        "closed_at": ended.isoformat() if ended else None,
        "duration_seconds": float(session["total_duration_seconds"])
                            if session.get("total_duration_seconds") is not None else None,
        "audio_sha256": bytes(archive_sha).hex() if archive_sha else None,
        "audio_bytes": session.get("archive_bytes"),
        "archive_relpath": session.get("archive_relpath"),
        "written_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        # A consultation resumed after a crash or restart is two recordings of
        # one visit; each names the other (S4).
        **{k: v for k, v in links.items() if v},
    }
    if part is not None:
        document["visit"]["doctor_id"] = part["doctor_id"]
        part_sha = part.get("archive_sha256")
        document["recording"].update({
            "audio_sha256": bytes(part_sha).hex() if part_sha else None,
            "audio_bytes": part.get("archive_bytes"),
            "archive_relpath": part.get("archive_relpath"),
            "derived_from": {
                "session_id": session_id,
                "start_seconds": float(part["start_seconds"]),
                "end_seconds": float(part["end_seconds"])
                               if part["end_seconds"] is not None else None,
                "rule": part["rule"],
                "needs_review": part["needs_review"],
                # The whole recording, as signed: join the parts in order and
                # this is its fingerprint.
                "recording_sha256": bytes(archive_sha).hex() if archive_sha else None,
            },
        })
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
    session_id = _sid(body.session_id)
    session = await repo.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    if session.get("archived_at") is None:
        raise HTTPException(status_code=409, detail="session is not archived yet")

    version = await repo.next_copy_version(session_id, body.kind)
    named = session
    if body.part_no is not None:
        part = next((p for p in await repo.parts_for(session_id)
                     if p["part_no"] == body.part_no), None)
        if part is None or not part.get("file_stem"):
            raise HTTPException(status_code=404, detail="no such part")
        named = {**session, "file_stem": part["file_stem"],
                 "doctor_id": part["doctor_id"]}
    object_key = copy_object_key(named, body.kind, version)
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
    session_id = _sid(body.session_id)
    async with _session_lock(session_id):
        session = await repo.get_session(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="session not found")
        if session.get("confirmation") in ("refused", "expired"):
            # Refused while the copy was being made: the copy is deleted, not
            # recorded (the erase task holds no row for it to find).
            for copy in body.copies:
                await _delete_cloud_objects([{"kind": copy.kind,
                                              "object_key": copy.object_key}])
            raise HTTPException(status_code=409, detail="this consultation has been erased")
        if session.get("status") == "quarantined" or session.get("quarantine_reason"):
            raise HTTPException(status_code=409, detail="session is quarantined")
        return await _record_copies(body, session_id, session)


async def _record_copies(body: "CopyCompleteRequest", session_id: str, session):
    repo = _repo()
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

    if body.part_no is not None:
        # One patient's part of a recording cut into several. The recording
        # counts as copied - and its pieces go - only on the last part
        # (`final`), and only when every part is recorded as copied.
        all_done = await repo.mark_part_copied(session_id, body.part_no)
        if not (body.final and all_done):
            if session.get("copied_at"):
                await repo.clear_json_stale(session_id)     # a JSON written again
            return {"status": "part_copied", "objects_deleted": 0}

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
    session_id = _sid(body.session_id)
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


# CMED's code for a clinic, and the doctors already in the register. Both are
# read on the way to every grant, and neither changes during a clinic: the
# mapping is set when a hospital is registered, and a doctor is added once.
#
# It matters because a grant is promised inside 0.3 s (SRS 9.1) and the path
# does its work one query at a time: with 28 rooms opening together, every
# extra query is 28 more waits for a free connection.
_CLINIC_CACHE = {}
_CLINIC_CACHE_SECONDS = 60.0
_KNOWN_DOCTORS = set()
_DIRECTORY_WRITES = set()


async def directory_writes_done() -> None:
    """Wait for everything started behind a reply. For tests."""
    if _audit_writer_alive():
        await _AUDIT_QUEUE.join()
    pending = [t for t in _DIRECTORY_WRITES if not t.done()
               and t.get_coro().__name__ != "_audit_writer"]
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


def forget_directory() -> None:
    """Drop what this process remembers about clinics and doctors. For tests."""
    _CLINIC_CACHE.clear()
    _KNOWN_DOCTORS.clear()


_AUDIT_QUEUE: "Optional[asyncio.Queue]" = None
_AUDIT_WRITER: "Optional[asyncio.Task]" = None


def _audit_writer_alive() -> bool:
    """The writer is running, on the loop this code is running on."""
    if _AUDIT_WRITER is None or _AUDIT_WRITER.done():
        return False
    try:
        return _AUDIT_WRITER.get_loop() is asyncio.get_running_loop()
    except RuntimeError:
        return False


def _audit_behind(repo, **entry) -> None:
    """
    Write an audit entry after the reply has gone, through one writer.

    Only for paths promised in milliseconds that already hold a durable record
    of what happened - a grant has its authorisation row before this is called.

    One writer, not one task each. The audit log is hash-chained and every
    writer takes the same global lock to keep the chain straight, so handing
    twenty-eight entries to twenty-eight tasks only moves the queue off the
    request and into the pool. Queued here, they are written in order by a
    single consumer, and each request pays nothing.
    """
    global _AUDIT_QUEUE, _AUDIT_WRITER
    # Started on first use, and again if it has stopped. A writer that died -
    # or one left on an event loop that has since closed - would otherwise
    # leave every later entry queued for ever, with nothing reading the queue.
    if not _audit_writer_alive():
        if _AUDIT_QUEUE is not None and _AUDIT_QUEUE.qsize():
            logger.error("Audit writer had stopped with %s entries unwritten",
                         _AUDIT_QUEUE.qsize())
        _AUDIT_QUEUE = asyncio.Queue(maxsize=10000)
        _AUDIT_WRITER = asyncio.create_task(_audit_writer(repo, _AUDIT_QUEUE))
        _DIRECTORY_WRITES.add(_AUDIT_WRITER)
        _AUDIT_WRITER.add_done_callback(_DIRECTORY_WRITES.discard)
    try:
        _AUDIT_QUEUE.put_nowait(entry)
    except asyncio.QueueFull:
        # Ten thousand behind: something is very wrong with the database, and
        # dropping a line of bookkeeping is better than holding up a clinic.
        logger.error("Audit queue is full; %s was not recorded",
                     entry.get("event_type"))


async def _audit_writer(repo, queue: "asyncio.Queue") -> None:
    """Drain the queue, in order, for as long as the server runs."""
    while True:
        entry = await queue.get()
        try:
            await repo.audit(**entry)
        except Exception as exc:
            logger.warning("Audit entry %s could not be written: %s",
                           entry.get("event_type"), exc)
        finally:
            queue.task_done()


async def _remember_doctor(repo, clinic: str, doctor_id: str) -> None:
    """
    Add a doctor to the clinic's directory, once per doctor per server.

    On the request, not behind it. Moving it behind the reply was tried and
    measured worse: the background write still wants a connection, so a burst
    of twenty-eight rooms asked the pool for fifty-six at once and every
    foreground query queued - 43 ms became 585 ms. Guarded by the cache it
    happens once per doctor, and once is cheap.
    """
    key = (clinic, doctor_id)
    if key in _KNOWN_DOCTORS:
        return
    try:
        await repo.upsert_doctor(doctor_id=doctor_id, hospital_id=clinic,
                                 full_name=doctor_id, only_if_new=True)
        _KNOWN_DOCTORS.add(key)
    except Exception as exc:
        logger.warning("Could not record doctor %s in the directory: %s",
                       doctor_id, exc)


async def _cmed_clinic(cmed_hospital_id):
    """Which clinic CMED's code means, remembered for a minute."""
    cached = _CLINIC_CACHE.get(cmed_hospital_id)
    now = time.monotonic()
    if cached is not None and now - cached[0] < _CLINIC_CACHE_SECONDS:
        return cached[1]
    mapped = await _repo().hospital_for_cmed_id(cmed_hospital_id)
    _CLINIC_CACHE[cmed_hospital_id] = (now, mapped)
    return mapped


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
    mapped = await _cmed_clinic(cmed_hospital_id)
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
    mismatch = clinic is None
    if mismatch:
        # CMED named a hospital this PC is not enrolled at - a mistyped code, a
        # mapping not yet made. Refusing left the audio on the laptop with no
        # way off it. It is taken instead, filed under the PC's own clinic and
        # marked unconfirmed: it cannot be matched to CMED's API 2 until the
        # hospital is sorted out, so it never enters the dataset by itself.
        await repo.raise_alert(
            alert_type="clinic_mismatch", severity="critical",
            device_id=device["device_id"],
            detail={"device_hospital": device["hospital_id"],
                    "cmed_hospital_id": visit.cmed_hospital_id,
                    "mapped_to": await repo.hospital_for_cmed_id(visit.cmed_hospital_id),
                    "kept": "uploaded and marked unconfirmed"})
        clinic = device["hospital_id"]

    # Decision D4: the doctor register is a directory, not a gate. CMED
    # authenticates its own doctors, and its API 2 now proves the consultation;
    # refusing a doctor new to this server stopped real consultations before.
    # Not awaited. The directory is a convenience (decision D4), while the
    # grant is promised inside 0.3 s - and when a clinic opens, every room
    # asks at once. Twenty-eight rooms writing the same doctor's row together
    # queue behind one another's locks: measured, 13 ms became 402 ms, and
    # under upload load it broke the promise outright. Written behind the
    # reply instead, once per doctor per server.
    await _remember_doctor(repo, clinic, visit.doctor_id)

    now = datetime.now(timezone.utc)
    jti = new_jti()
    await repo.create_authorisation(
        jti=jti, device_id=device["device_id"], hospital_id=clinic, visit=visit,
        expires_at=now + timedelta(seconds=ctx.grants.lifetime))
    confirmed = False
    if not mismatch and not await _visit_contested(visit, clinic):
        notice_id = await repo.find_unclaimed_notice(
            visit, hospital_id=clinic, since=now - conf.NOTICE_MATCH_WINDOW)
        confirmed = notice_id is not None and await repo.claim_notice(notice_id, jti)
    state = "confirmed" if confirmed else "pending"

    token, _ = ctx.grants.issue(
        jti=jti, patient_ref=visit.patient_id, doctor_id=visit.doctor_id,
        hospital_id=clinic, cmed_hospital_id=visit.cmed_hospital_id,
        start_time=visit.start_time, visit_date=visit.visit_date, confirmation=state)
    # Behind the reply, like the directory above. The audit log is hash-chained
    # and every writer takes one global lock to keep the chain straight, so
    # twenty-eight rooms opening together queue on it: measured, that lock alone
    # took a grant from 43 ms to 587 ms and broke the 0.3 s promise (SRS 9.1).
    # Nothing is lost by moving it: the grant's durable record is the
    # authorisation row written above, and the audit entry follows it within
    # milliseconds. Every other path still audits before answering.
    _audit_behind(repo, event_type="grant.issued", actor_type="device",
                  actor_id=visit.doctor_id, device_id=device["device_id"],
                  detail={"hospital_id": clinic, "confirmation": state})
    return {"status": 200, "code": "GRANTED", "grant": token, "jti": jti,
            "confirmation": state, "hospital_id": clinic,
            **({"clinic_mismatch": True} if mismatch else {}),
            "expires_in": ctx.grants.lifetime}


async def _visit_contested(visit, clinic: str) -> bool:
    """
    One consultation is recorded by one PC. When a second PC asks for the same
    five fields, one of them is not where it says it is - a copied key, or a
    PC that saw another's screen - and which one cannot be told from here. API
    2 confirms neither from then on; both wait, and an administrator decides -
    the admin confirm, or a legal hold - before their erase_after, or they are
    erased as never confirmed.
    """
    repo = _repo()
    if not hasattr(repo, "visit_devices"):
        return False
    devices = await repo.visit_devices(visit, hospital_id=clinic)
    if len(devices) < 2:
        return False
    await repo.raise_alert(
        alert_type="consultation_claimed_by_two_pcs", severity="critical",
        detail={"hospital_id": clinic, "devices": devices,
                "start_time": visit.start_time, "date": visit.visit_date})
    return True


async def _link_grant(session_id: str, grant_jti: Optional[str], patient_ref: str,
                      device: Dict[str, Any], *, doctor_id: Optional[str] = None) -> str:
    """
    Tie a session to the grant it was opened under, and so to CMED's API 2.
    No grant means a protocol-2 recorder: 'legacy', kept but held out of the
    dataset, since nothing ties it to a consultation CMED knows of.
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
                 and (doctor_id is None or grant.get("doctor_id") in (None, doctor_id))
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


async def _link_continuation(session_id: str, previous_id: str, patient_ref: str,
                             device: Dict[str, Any], state: str) -> str:
    """
    S4: a consultation resumed after the recorder crashed or the PC restarted.
    It is the same visit, so it takes the first recording's confirmation and
    both are filed together. Only a recording from this PC, for this patient,
    is accepted as the one continued.
    """
    repo = _repo()
    try:
        previous_id = safe_session_id(previous_id)
    except ValueError:
        previous_id = ""
    previous = await repo.get_session(previous_id) if previous_id else None
    if (previous is None or previous_id == session_id
            or str(previous.get("device_id")) != str(device["device_id"])
            or previous.get("patient_id") != patient_ref):
        await repo.raise_alert(alert_type="continuation_rejected", severity="warning",
                               session_id=session_id, device_id=device["device_id"],
                               detail={"continues": previous_id or None})
        return state
    await repo.set_continuation(session_id, previous_id)
    # The first recording's JSON was written without continued_by if it was
    # already archived: write it again.
    if previous.get("archived_at"):
        await repo.mark_json_stale(previous_id)
    if previous.get("confirmation") == "confirmed" and state in conf.WAITING:
        await repo.set_session_confirmation(session_id, "confirmed")
        return "confirmed"
    return state


async def confirm_continuations(session_id: str) -> List[str]:
    """A recording was confirmed: so is any recording that continues it."""
    repo = _repo()
    confirmed = []
    for later in await repo.continuations_of(session_id):
        row = await repo.session_confirmation(later)
        if row and row.get("confirmation") in conf.WAITING:
            await repo.set_session_confirmation(later, "confirmed")
            confirmed.append(later)
            confirmed += await confirm_continuations(later)
    return confirmed


# A CMED outage leaves every recording unconfirmed at once. One alert per clinic
# in this window, counting them, instead of one per recording (D4).
UNCONFIRMED_GROUP_WINDOW = timedelta(hours=6)


async def _mark_unconfirmed(session_id: str, row: Dict[str, Any]) -> None:
    repo = _repo()
    session = await repo.get_session(session_id) or {}
    previous_id = session.get("continues_session_id")
    if previous_id:
        previous = await repo.session_confirmation(previous_id)
        if previous and previous.get("confirmation") == "confirmed":
            await repo.set_session_confirmation(session_id, "confirmed")
            return
    await repo.set_session_confirmation(session_id, "unconfirmed")
    hospital = session.get("hospital_id") or row.get("hospital_id") or "unknown"
    await repo.group_alert(
        alert_type="unconfirmed_recording", group=f"unconfirmed:{hospital}",
        session_id=session_id, window=UNCONFIRMED_GROUP_WINDOW,
        device_id=row.get("device_id") or session.get("device_id"),
        detail={"hospital_id": hospital,
                "reason": "no matching API 2 or API 3 from CMED within two minutes; "
                          "the recordings wait on the server and are confirmed when it "
                          "arrives, or erased on their erase_after if it never does"})


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
    sid = _sid(session_id)
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

    removed = await _delete_bucket_objects(session_id, only_archived=False)
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

    # A resumed recording leaves the visit linked to the first one (S4); it is
    # found from there through continuation_links.
    if (session.get("grant_jti") and ctx.clinical is not None
            and not session.get("continues_session_id")):
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


async def relabel_session(session_id: str) -> Optional[List[Dict[str, Any]]]:
    """
    Decide who each stretch of a closed recording belongs to (labelling.py),
    name the result, and tie each part to its clinical visit.

    None when there is nothing to decide - no marker in the recording, which
    is every recording made by an older recorder - and the caller names the
    recording as it always did. Run at close, again when a marker or a
    prescription arrives late. Never deletes anything; a change after the
    files were cut marks them to be cut again.
    """
    repo = _repo()
    session = await repo.get_session(session_id)
    if not session or not session.get("closed_at") or not session.get("opened_at"):
        return None
    clicks = await repo.clicks_for(session_id)
    if not clicks:
        return None

    first_visit, first_jti = await _first_visit(session)
    events = [labelling.Event(seconds=0.0, kind=labelling.OPEN, visit=first_visit,
                              at=session["opened_at"], grant_jti=first_jti, entry_no=0)]
    for c in clicks:
        events.append(labelling.Event(
            seconds=float(c["audio_seconds"]), kind=c["event"],
            visit=conf.Visit(patient_id=c["patient_id"], doctor_id=c["doctor_id"],
                             cmed_hospital_id=c["cmed_hospital_id"],
                             start_time=c["start_time"],
                             visit_date=c["visit_date"].isoformat()
                             if hasattr(c["visit_date"], "isoformat")
                             else str(c["visit_date"])),
            at=c["occurred_at"], grant_jti=c["grant_jti"], entry_no=c["entry_no"]))

    prescribed = set()
    if ctx.clinical is not None:
        distinct = {e.visit.patient_id: e.visit for e in events}
        try:
            prescribed = await ctx.clinical.prescribed(list(distinct.values()))
        except Exception as exc:
            logger.warning("Could not read prescriptions for %s: %s", session_id, exc)

    total = sum(float(s["duration_seconds"]) for s in await repo.segments_for(session_id))
    parts = labelling.resolve(events, total_seconds=total, prescribed=prescribed,
                              closed_at=session["closed_at"])

    if len(parts) == 1:
        only = parts[0]
        await repo.label_session(session_id, patient_id=only.visit.patient_id,
                                 doctor_id=only.visit.doctor_id, grant_jti=only.grant_jti)
        if only.grant_jti and only.grant_jti != session.get("grant_jti"):
            grant = await repo.get_authorisation(only.grant_jti)
            if grant and grant.get("notice_id"):
                await repo.set_session_confirmation(session_id, "confirmed",
                                                    grant_jti=only.grant_jti)
    session_stem = await _record_file_name(session_id)

    rows = []
    for p in parts:
        start_at = session["opened_at"] if p.part_no == 1 else (p.start_at or session["opened_at"])
        end_at = session["closed_at"] if p.end_seconds is None else (p.end_at or session["closed_at"])
        local_start = await _local_time(session["hospital_id"], start_at)
        local_end = await _local_time(session["hospital_id"], end_at)
        stem = session_stem if len(parts) == 1 else file_stem_for(
            p.visit.patient_id, p.visit.doctor_id, session["hospital_id"],
            local_start, local_end)
        rows.append({
            "part_no": p.part_no, "start_seconds": p.start_seconds,
            "end_seconds": p.end_seconds, "patient_id": p.visit.patient_id,
            "doctor_id": p.visit.doctor_id, "cmed_hospital_id": p.visit.cmed_hospital_id,
            "start_time": p.visit.start_time, "visit_date": p.visit.day,
            "grant_jti": p.grant_jti, "rule": p.rule, "needs_review": p.needs_review,
            "file_stem": stem, "local_start": local_start.time().replace(tzinfo=None),
            "local_end": local_end.time().replace(tzinfo=None),
        })

    old = await repo.parts_for(session_id)
    layout = [(r["part_no"], round(float(r["start_seconds"]), 3),
               None if r["end_seconds"] is None else round(float(r["end_seconds"]), 3),
               r["patient_id"]) for r in rows]
    old_layout = [(r["part_no"], round(float(r["start_seconds"]), 3),
                   None if r["end_seconds"] is None else round(float(r["end_seconds"]), 3),
                   r["patient_id"]) for r in old]
    same_cut = bool(old) and layout == old_layout
    changed = not old or not same_cut or [(r["rule"], r["needs_review"]) for r in rows] != [
        (r["rule"], r["needs_review"]) for r in old]
    await repo.store_parts(session_id, rows, keep_archive=same_cut)

    if len(parts) > 1 and ctx.clinical is not None:
        for r, p in zip(rows, parts):
            try:
                await ctx.clinical.link_session(p.visit, session_id=session_id,
                                                file_stem=r["file_stem"],
                                                hospital_id=session["hospital_id"])
            except Exception as exc:
                logger.warning("Could not link part %s of %s: %s", p.part_no,
                               session_id, exc)

    archived = session.get("archived_at") is not None
    if archived and old and not same_cut:
        await repo.set_parts_stale(session_id, True)
        await repo.raise_alert(
            alert_type="relabelled_after_archive", severity="warning",
            session_id=session_id, device_id=session.get("device_id"),
            detail={"parts": [(r["patient_id"], r["start_seconds"], r["end_seconds"],
                               r["rule"]) for r in rows]})
    elif archived and changed:
        await repo.mark_json_stale(session_id)
    if changed and any(r["needs_review"] for r in rows):
        await repo.raise_alert(
            alert_type="labelling_review", severity="warning",
            session_id=session_id, device_id=session.get("device_id"),
            detail={"parts": [{"part_no": r["part_no"], "patient_id": r["patient_id"],
                               "start_seconds": r["start_seconds"],
                               "end_seconds": r["end_seconds"], "rule": r["rule"],
                               "needs_review": r["needs_review"]} for r in rows]})
    if changed:
        await repo.audit(
            event_type="session.labelled", actor_type="service", actor_id="labelling",
            session_id=session_id,
            detail={"parts": [(r["patient_id"], r["start_seconds"], r["end_seconds"],
                               r["rule"]) for r in rows]})
    return rows


async def _first_visit(session: Dict[str, Any]):
    """
    The patient the recording was opened for, as CMED named them - from the
    grant it started under, which a relabel never changes. Falls back to what
    the session row holds for a recording that was never granted.
    """
    repo = _repo()
    chain = await repo.load_chain(str(session["session_id"]))
    opened_for = (chain[0].payload.get("patient_ref") if chain else None) \
        or session["patient_id"]
    for grant in await repo.grants_for_session(str(session["session_id"])):
        if grant["patient_id"] == opened_for:
            return _visit_from_grant(grant), grant["jti"]
    day = session.get("session_date") or session["opened_at"].date()
    return conf.Visit(patient_id=opened_for, doctor_id=session["doctor_id"],
                      cmed_hospital_id=session["hospital_id"],
                      start_time=session["opened_at"].isoformat(),
                      visit_date=day.isoformat()), None


class RefuseRequest(BaseModel):
    session_id: str
    # Recorder 3.5 and later: {"session_id", "device_id", "refused_at"} signed
    # by the PC's own key (base64url, no padding) over its canonical JSON.
    refusal: Optional[Dict[str, Any]] = None
    signature: Optional[str] = Field(None, max_length=200)


SIGNED_REFUSAL_WINDOW = timedelta(days=7)
UNSIGNED_REFUSAL_WINDOW = timedelta(hours=2)
REFUSAL_BURST = 3                         # more than this in an hour is alerted


def signed_refusals_required() -> bool:
    return os.getenv("AIMS_REQUIRE_SIGNED_REFUSAL", "").strip().lower() in (
        "1", "on", "true", "yes")


def _refusal_signed(body: RefuseRequest, device, session_id: str) -> bool:
    """True for a valid signed refusal, False when unsigned; 400/401 when it is bad."""
    if body.refusal is None and body.signature is None:
        return False
    if not isinstance(body.refusal, dict) or not body.signature:
        raise HTTPException(status_code=400, detail="a signed refusal needs both "
                                                    "refusal and signature")
    refusal = body.refusal
    if refusal.get("session_id") != session_id:
        raise HTTPException(status_code=400, detail="the signed refusal names another session")
    if str(refusal.get("device_id", "")).lower() != str(device["device_id"]).lower():
        raise HTTPException(status_code=401, detail="the refusal was signed for another PC")
    refused_at = _parse_time_strict(refusal.get("refused_at"), "refusal.refused_at")
    if refused_at > datetime.now(timezone.utc) + timedelta(minutes=5):
        raise HTTPException(status_code=400, detail="refusal.refused_at is in the future")
    try:
        raw = _b64url_decode(body.signature)
    except Exception:
        raise HTTPException(status_code=400, detail="signature is not base64url")
    if not integrity.verify_device_signature(bytes(device["tpm_pubkey"]),
                                             integrity.canonical_json(refusal), raw):
        raise HTTPException(status_code=401, detail="the refusal signature does not verify")
    return True


@router.post("/session/refuse")
async def refuse_session(body: RefuseRequest, device=Depends(require_device)):
    """
    The patient did not consent (SRS 3.2 §7.8a). The doctor pressed Stop and
    chose "Patient did not consent"; the recorder has already deleted what it
    held. Here everything else goes, within the request (SRS-CNS-04, -05) -
    the pieces, the chain, CMED's records for the visit, and for a recording
    already archived its files on the server and its cloud copies - and only
    an audit entry without audio or patient remains (SRS-CNS-06).

    The most destructive call there is, so it is bounded. A refusal signed by
    the PC's own key is accepted for a recording opened in the last 7 days.
    An unsigned one (recorder 3.4) only within 2 hours of opening and before
    archiving, and not at all with AIMS_REQUIRE_SIGNED_REFUSAL=on. Older
    recordings are erased by an administrator. Accepted for a session this
    server has never seen, so a refusal made while the PC was offline still
    stops that session being opened later.
    """
    repo = _repo()
    session_id = _sid(body.session_id)
    signed = _refusal_signed(body, device, session_id)
    if not signed and signed_refusals_required():
        raise HTTPException(status_code=401, detail="refusals must be signed by the PC")

    now = datetime.now(timezone.utc)
    removed, visit, first = 0, None, False
    async with _session_lock(session_id):
        session = await repo.get_session(session_id)
        if session is not None and str(session["device_id"]) != str(device["device_id"]):
            raise HTTPException(status_code=403, detail="session belongs to another device")
        if session is not None and session.get("confirmation") != "refused":
            opened = session.get("opened_at") or now
            if signed and now - opened > SIGNED_REFUSAL_WINDOW:
                raise HTTPException(status_code=403,
                                    detail="opened more than 7 days ago; an administrator "
                                           "must erase it")
            if not signed and (now - opened > UNSIGNED_REFUSAL_WINDOW
                               or session.get("archived_at") is not None):
                raise HTTPException(status_code=403,
                                    detail="an unsigned refusal is accepted only within "
                                           "2 hours and before archiving; update the "
                                           "recorder, or an administrator must erase it")
            removed, visit = await _erase_session(session_id, state="refused")

        first = await repo.record_refusal(
            session_id=session_id, device_id=device["device_id"],
            hospital_id=device["hospital_id"],
            doctor_id=session["doctor_id"] if session else None,
            visit_sha256=visit.digest() if visit else None, signed=signed)
        await repo.complete_refusal(session_id)

    await _finish_erase(session_id)
    if not signed:
        await repo.raise_alert(
            alert_type="unsigned_refusal", severity="warning", session_id=session_id,
            device_id=device["device_id"],
            detail={"note": "refusal from a recorder that does not sign them (before 3.5)"})
    if first and hasattr(repo, "refusals_since"):
        recent = await repo.refusals_since(device["device_id"], now - timedelta(hours=1))
        if recent > REFUSAL_BURST:
            await repo.raise_alert(
                alert_type="refusal_burst", severity="critical",
                device_id=device["device_id"],
                detail={"refusals_last_hour": recent})

    if first:
        session_date = session.get("session_date") if session else None
        await repo.audit(
            event_type="session.refused", actor_type="device",
            actor_id=session["doctor_id"] if session else None,
            device_id=device["device_id"], session_id=session_id,
            detail={"hospital_id": device["hospital_id"],
                    "date": session_date.isoformat() if session_date else None,
                    "objects_deleted": removed, "signed": signed})
    return {"status": "refused", "session_id": session_id, "objects_deleted": removed}


@router.post("/admin/session/{session_id}/erase")
async def admin_erase_session(session_id: str, actor: str = "",
                              _: None = Depends(require_admin)):
    """A refusal that came too late for the PC to make: erased by a person."""
    repo = _repo()
    sid = _sid(session_id)
    async with _session_lock(sid):
        session = await repo.get_session(sid)
        if session is None:
            raise HTTPException(status_code=404, detail="session not found")
        removed, visit = (0, None)
        if session.get("confirmation") != "refused":
            removed, visit = await _erase_session(sid, state="refused")
        await repo.record_refusal(session_id=sid, device_id=session["device_id"],
                                  hospital_id=session["hospital_id"],
                                  doctor_id=session.get("doctor_id"),
                                  visit_sha256=visit.digest() if visit else None,
                                  signed=None)
        await repo.complete_refusal(sid)
    await _finish_erase(sid)
    await repo.audit(event_type="session.erased", actor_type="admin", actor_id=actor or None,
                     session_id=sid, detail={"objects_deleted": removed})
    return {"status": "refused", "session_id": sid, "objects_deleted": removed}


# ---- what an erase leaves for later: archive files (the worker) and cloud copies (here)

def _copy_store_for(kind: str):
    if kind == "json" and ctx.copy_json is not None:
        return ctx.copy_json
    return ctx.copy


async def _delete_cloud_objects(keys: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Delete cloud copies; returns the ones that could not be deleted yet."""
    loop = asyncio.get_event_loop()
    left = []
    for key in keys:
        store = _copy_store_for(str(key.get("kind")))
        if store is None:
            left.append(key)
            continue
        try:
            await loop.run_in_executor(None, store.client.remove_object, store.bucket,
                                       key["object_key"])
        except Exception as exc:
            logger.warning("Could not delete cloud copy %s yet: %s", key.get("object_key"), exc)
            left.append(key)
    return left


async def _finish_erase(session_id: str) -> Optional[Dict[str, Any]]:
    """Try the cloud deletions an erase left; the files are the worker's to remove."""
    repo = _repo()
    if not hasattr(repo, "erase_task"):
        return None
    task = await repo.erase_task(session_id)
    if task is None or task.get("completed_at"):
        return task
    if task.get("cloud_keys"):
        left = await _delete_cloud_objects(task["cloud_keys"])
        await repo.set_erase_cloud_keys(session_id, left)
        task["cloud_keys"] = left
    if not task.get("files") and not task.get("cloud_keys"):
        await repo.complete_erase(session_id)
    return task


class EraseCompleteRequest(BaseModel):
    session_id: str
    removed: List[str] = Field(default_factory=list, max_length=1000)


@router.get("/archive/erase/pending")
async def archive_erase_pending(limit: int = 20, _: None = Depends(require_worker)):
    """Recordings refused after archiving: the files the worker must remove."""
    repo = _repo()
    out = []
    for task in await repo.pending_erase(max(1, min(limit, 100))):
        task = await _finish_erase(task["session_id"]) or task
        if task.get("completed_at"):
            continue
        out.append({"session_id": task["session_id"], "files": task.get("files") or []})
    return {"tasks": out}


@router.post("/archive/erase/complete")
async def archive_erase_complete(body: EraseCompleteRequest,
                                 _: None = Depends(require_worker)):
    repo = _repo()
    session_id = _sid(body.session_id)
    task = await repo.erase_task(session_id)
    if task is None:
        raise HTTPException(status_code=404, detail="no erase task for this session")
    left = await _delete_cloud_objects(task.get("cloud_keys") or [])
    await repo.set_erase_cloud_keys(session_id, left)
    done = not left and await repo.complete_erase(session_id)
    await repo.audit(event_type="session.erase_files", actor_type="service",
                     actor_id="archive-worker", session_id=session_id,
                     detail={"files_removed": len(body.removed), "cloud_left": len(left)})
    return {"status": "erased" if done else "pending", "cloud_left": len(left)}


@router.post("/maintenance/sweep")
async def maintenance_sweep(_: None = Depends(require_worker)):
    """
    Settle confirmation deadlines. The archive worker calls this every pass.

        pending for two minutes          ->  unconfirmed, alert   (SRS-CNF-08)
        not confirmed: erase_after set   ->  opened + AIMS_UNCONFIRMED_ERASE_DAYS
        erase_after passed, still not    ->  erased ("expired"), as a refusal is
          confirmed by CMED
        within 48 hours of erase_after   ->  one alert per clinic a day
        a self-registered PC pending     ->  rejected
          for AIMS_PENDING_DEVICE_DAYS

    Only what CMED's server confirms is kept, but an outage never erases: a
    clinic CMED has sent nothing for in a day has its erasures held, with an
    alert, until CMED is heard from again. A legal hold is never erased.
    """
    repo = _repo()
    now = datetime.now(timezone.utc)

    # A hospital's CMED client that stops working within 30 days: one alert a
    # day, so it is renewed before CMED's messages start being refused.
    try:
        import cmed_auth
        await cmed_auth.expiring_clients()
    except Exception as exc:                    # never stops the sweep
        logger.warning("Could not check CMED client expiry: %s", exc)

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

    erased, held, rejected, approved, stopped = await _settle_unconfirmed()

    return {"status": "ok", "marked_unconfirmed": marked, "erased": erased,
            "erase_held_clinics": held, "erase_stopped_clinics": stopped,
            "devices_rejected": rejected,
            "devices_approved": approved, "clinical_loaded": loaded,
            "copies_overdue": len(overdue), "reconciled": len(reconciled)}


ERASE_BATCH = 100                       # per sweep; the worker sweeps every 30 s
ERASE_WARNING = timedelta(hours=48)


def erase_paused() -> bool:
    """AIMS_ERASE_PAUSED=true: nothing is erased automatically, by anyone's rule."""
    return os.getenv("AIMS_ERASE_PAUSED", "").strip().lower() in ("1", "true", "yes", "on")


def breaker_limits() -> Tuple[float, int]:
    """
    (share, count): a clinic's erasing stops when more than this share of a
    week's recordings, and at least this many, would go. A stray click is a
    few a week; a third of a clinic is a fault.
    """
    try:
        share = float(os.getenv("AIMS_ERASE_BREAKER_SHARE", "0.3"))
    except ValueError:
        share = 0.3
    return min(max(share, 0.01), 1.0), max(1, _env_int("AIMS_ERASE_BREAKER_MIN", 20))


async def _erase_breaker(due: List[Dict[str, Any]], days: int) -> Set[str]:
    """
    The clinics whose erasing stops now: far more of their recordings would
    be erased than a stray click explains, and no admin has released them.
    One critical alert a day for each.
    """
    repo = _repo()
    if not due or not hasattr(repo, "erase_share"):
        return set()
    share_limit, count_limit = breaker_limits()
    clinics = sorted({str(r["hospital_id"]) for r in due})
    shares = await repo.erase_share(clinics, days=days)
    stopped: Set[str] = set()
    for clinic in clinics:
        s = shares.get(clinic, {"unconfirmed": 0, "total": 0})
        if s["unconfirmed"] < count_limit or not s["total"] \
                or s["unconfirmed"] / s["total"] <= share_limit:
            continue
        if await repo.erase_released(clinic):
            continue
        stopped.add(clinic)
        await repo.alert_once(
            alert_type="erase_breaker", group=f"erase_breaker:{clinic}",
            window=timedelta(hours=24), severity="critical",
            detail={"hospital_id": clinic, "unconfirmed": s["unconfirmed"],
                    "recordings": s["total"],
                    "share": round(s["unconfirmed"] / s["total"], 2),
                    "reason": "far more of this clinic's recordings would be erased than "
                              "stray clicks explain - a fault in confirmation, or CMED "
                              "sending another clinic's code. Nothing of this clinic is "
                              "erased until an admin checks and releases it "
                              "(POST /api/v2/admin/erase/release)"})
    if stopped:
        logger.error("Erasing stopped by the circuit breaker for %s", ", ".join(sorted(stopped)))
    return stopped


async def _settle_unconfirmed() -> Tuple[int, int, int, int, int]:
    """
    Keep only what CMED confirms: give each waiting recording its date, erase
    the ones past it, warn before, and settle self-registered PCs. Returns
    (erased, clinics held back, PCs rejected, PCs approved, clinics stopped
    by the circuit breaker).
    """
    repo = _repo()
    if not hasattr(repo, "set_erase_deadlines"):
        return 0, 0, 0, 0, 0
    days = conf.unconfirmed_erase_days()
    await repo.set_erase_deadlines(days)

    plan = await repo.sessions_due_for_erasure(limit=ERASE_BATCH,
                                               silent_after=conf.CMED_SILENT_AFTER)
    stopped: Set[str] = set()
    if erase_paused():
        if plan["due"]:
            await repo.alert_once(
                alert_type="erase_paused", group="erase_paused", window=timedelta(hours=24),
                severity="warning",
                detail={"due": len(plan["due"]),
                        "reason": "AIMS_ERASE_PAUSED is set: recordings past their date are "
                                  "kept. Unset it when the reason for pausing is settled."})
        plan["due"] = []
    else:
        stopped = await _erase_breaker(plan["due"], days)
        plan["due"] = [r for r in plan["due"] if str(r["hospital_id"]) not in stopped]
    erased = 0
    for row in plan["due"]:
        sid = row["session_id"]
        async with _session_lock(sid):
            # Confirmed in the moment since it was listed: kept.
            current = await repo.session_confirmation(sid)
            if not current or current.get("confirmation") not in conf.ERASABLE:
                continue
            removed, _visit = await _erase_session(sid, state="expired")
        await _finish_erase(sid)
        await repo.audit(event_type="session.expired", actor_type="service",
                         actor_id="sweep", session_id=sid,
                         detail={"reason": "not confirmed by CMED", "was": row["confirmation"],
                                 "days": days, "objects_deleted": removed})
        erased += 1
    if erased:
        logger.info("Erased %s recording(s) CMED never confirmed in %s days", erased, days)

    for clinic in plan["held"]:
        await repo.alert_once(
            alert_type="erase_held_cmed_silent", group=f"erase_held:{clinic['hospital_id']}",
            window=timedelta(hours=24), severity="warning",
            detail={"hospital_id": clinic["hospital_id"], "count": clinic["n"],
                    "cmed_last_heard": clinic["last_heard"],
                    "reason": "these recordings are past their date, but CMED has sent "
                              "nothing for this clinic in a day: they are kept until it "
                              "does, so an outage loses nothing"})

    for clinic in await repo.erasing_soon(within=ERASE_WARNING):
        await repo.alert_once(
            alert_type="erase_soon", group=f"erase_soon:{clinic['hospital_id']}",
            window=timedelta(hours=24), severity="warning",
            detail={"hospital_id": clinic["hospital_id"], "count": clinic["n"],
                    "first": clinic["first"],
                    "reason": "will be erased within 48 hours unless CMED confirms them; "
                              "v_audio_problems lists them. A legal hold or the admin "
                              "confirm keeps one"})

    rejected = await repo.expire_pending_devices(_env_int("AIMS_PENDING_DEVICE_DAYS", 7))
    for pc in rejected:
        await repo.audit(event_type="device.rejected", actor_type="service",
                         actor_id="sweep", device_id=pc["device_id"],
                         detail={"reason": "not confirmed by CMED in time"})
        await repo.raise_alert(alert_type="device_rejected", severity="warning",
                               device_id=pc["device_id"],
                               detail={"hospital_id": pc["hospital_id"],
                                       "machine_name": pc["machine_name"]})
    approved = await repo.approvals_to_audit()
    for pc in approved:
        await repo.audit(event_type="device.approved", actor_type="service",
                         actor_id=pc.get("approved_by") or "cmed", device_id=pc["device_id"],
                         detail={"hospital_id": pc["hospital_id"],
                                 "machine_name": pc["machine_name"],
                                 "confirmations": SELF_APPROVAL_CONFIRMATIONS})
    return erased, len(plan["held"]), len(rejected), len(approved), len(stopped)


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
async def admin_revoke_device(device_id: str, reason: str = "", hold: bool = False,
                              _: None = Depends(require_admin)):
    """
    Cut off a lost or stolen machine. The token is cleared, not just flagged.

    hold=true is for a PC no longer trusted: every recording of it not yet in
    the dataset is held for review as well, instead of being archived.
    """
    revoked = await _repo().revoke_device(device_id, reason or "revoked by administrator")
    if not revoked:
        raise HTTPException(status_code=404, detail="device not found or already revoked")
    held = 0
    if hold:
        held = await _repo().quarantine_device_sessions(
            device_id, f"PC revoked as untrusted: {reason or 'no reason given'}")
    await _repo().audit(event_type="device.revoked", actor_type="admin",
                        device_id=device_id, detail={"reason": reason, "held": held})
    return {"status": "revoked", "device_id": device_id, "recordings_held": held}


class DeviceDecision(BaseModel):
    decided_by: str = Field(..., min_length=2, max_length=128)
    reason: str = Field("", max_length=500)


def _device_uuid(device_id: str) -> uuid.UUID:
    try:
        return uuid.UUID(device_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="device not found")


@router.post("/admin/device/{device_id}/approve")
async def admin_approve_device(device_id: str, body: DeviceDecision,
                               _: None = Depends(require_admin)):
    """A PC that registered itself, approved by a person without waiting for CMED."""
    did = _device_uuid(device_id)
    if not await _repo().set_device_approval(did, "approved", by=body.decided_by):
        raise HTTPException(status_code=404, detail="no pending PC with that id")
    await _repo().audit(event_type="device.approved", actor_type="admin",
                        actor_id=body.decided_by, device_id=did,
                        detail={"reason": body.reason, "by_hand": True})
    return {"status": "approved", "device_id": str(did)}


@router.post("/admin/device/{device_id}/reject")
async def admin_reject_device(device_id: str, body: DeviceDecision,
                              _: None = Depends(require_admin)):
    """
    A PC that registered itself and is not ours: revoked at once. What it
    uploaded is not confirmed by CMED, so it is erased on its date.
    """
    did = _device_uuid(device_id)
    if not await _repo().set_device_approval(did, "rejected", by=body.decided_by):
        raise HTTPException(status_code=404, detail="no pending PC with that id")
    await _repo().audit(event_type="device.rejected", actor_type="admin",
                        actor_id=body.decided_by, device_id=did,
                        detail={"reason": body.reason})
    return {"status": "rejected", "device_id": str(did)}


class ConfirmRequest(BaseModel):
    confirmed_by: str = Field(..., min_length=2, max_length=128)
    reason: str = Field(..., min_length=10, max_length=500)


@router.post("/admin/session/{session_id}/confirm")
async def admin_confirm_session(session_id: str, body: ConfirmRequest,
                                _: None = Depends(require_admin)):
    """
    Let a held recording into the dataset when CMED never sent its API 2 - an
    outage, a wrong clinic mapping - and a person has checked it is real. By
    name and with a reason, in the audit log; never automatic.
    """
    session_id = _sid(session_id)
    repo = _repo()
    row = await repo.session_confirmation(session_id)
    if row is None:
        raise HTTPException(status_code=404, detail="session not found")
    if row["confirmation"] == "confirmed":
        return {"status": "confirmed", "session_id": session_id, "already": True}
    if row["confirmation"] in ("refused", "expired"):
        raise HTTPException(status_code=409, detail="this consultation has been erased")
    await repo.set_session_confirmation(session_id, "confirmed")
    await repo.audit(event_type="session.confirmed", actor_type="admin",
                     actor_id=body.confirmed_by, session_id=session_id,
                     detail={"by_hand": True, "was": row["confirmation"],
                             "reason": body.reason})
    await confirm_continuations(session_id)
    return {"status": "confirmed", "session_id": session_id, "was": row["confirmation"]}


class EraseReleaseRequest(BaseModel):
    hospital_id: str = Field(..., max_length=64)
    released_by: str = Field(..., min_length=2, max_length=128)
    reason: str = Field(..., min_length=10, max_length=500)
    hours: int = Field(24, ge=1, le=72)


@router.get("/admin/erase/status")
async def admin_erase_status(_: None = Depends(require_admin)):
    """
    What the automatic erasing would do now, per clinic: due, held for a CMED
    outage, stopped by the circuit breaker, released by an admin - and whether
    it is paused altogether.
    """
    repo = _repo()
    days = conf.unconfirmed_erase_days()
    plan = await repo.sessions_due_for_erasure(limit=10_000, silent_after=conf.CMED_SILENT_AFTER)
    clinics = sorted({str(r["hospital_id"]) for r in plan["due"]})
    shares = await repo.erase_share(clinics, days=days)
    share_limit, count_limit = breaker_limits()
    out = []
    for clinic in clinics:
        s = shares.get(clinic, {"unconfirmed": 0, "total": 0})
        tripped = (s["unconfirmed"] >= count_limit and s["total"] > 0
                   and s["unconfirmed"] / s["total"] > share_limit)
        released = await repo.erase_released(clinic)
        out.append({"hospital_id": clinic,
                    "due": sum(1 for r in plan["due"] if str(r["hospital_id"]) == clinic),
                    "unconfirmed_this_week": s["unconfirmed"], "recordings_this_week": s["total"],
                    "breaker": "released" if tripped and released else
                               "stopped" if tripped else "ok",
                    "released_until": released["until"].isoformat() if released else None})
    return {"paused": erase_paused(), "erase_after_days": days,
            "breaker": {"share": share_limit, "min": count_limit},
            "clinics": out,
            "held_for_cmed_outage": [{"hospital_id": h["hospital_id"], "count": h["n"]}
                                     for h in plan["held"]]}


@router.post("/admin/erase/release")
async def admin_erase_release(body: EraseReleaseRequest, _: None = Depends(require_admin)):
    """
    A person has looked at a clinic the circuit breaker stopped, and the
    recordings really are strays CMED will never confirm: let the erasing go
    on for that clinic, for a few hours. By name and with a reason.
    """
    try:
        clinic = safe_identifier(body.hospital_id, field="hospital_id")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    row = await _repo().release_erase(clinic, by=body.released_by, reason=body.reason,
                                      hours=body.hours)
    await _repo().audit(event_type="erase.released", actor_type="admin",
                        actor_id=body.released_by,
                        detail={"hospital_id": clinic, "hours": body.hours,
                                "reason": body.reason})
    return {"status": "released", "hospital_id": clinic, "until": row["until"].isoformat()}


class CmedKeyRequest(BaseModel):
    # The interim static key. It names the hospitals it may speak for and it
    # expires: a key for every hospital that never ends is what the token
    # method (cmed_auth.py) replaces.
    label: str = Field(..., min_length=1, max_length=64)
    created_by: str = Field(..., max_length=128)
    cmed_hospital_ids: List[str] = Field(..., min_length=1, max_length=20)
    expires_in_days: int = Field(90, ge=1, le=365)


class CmedKeyRevokeRequest(BaseModel):
    label: str = Field(..., min_length=1, max_length=64)


@router.post("/admin/cmed-key")
async def admin_cmed_key(body: CmedKeyRequest, _: None = Depends(require_admin)):
    """
    Issue a key for CMED's server (SRS-CHB-03). Shown once; only its hash is
    kept. Several may be active at once, so a key is replaced by issuing the
    new one, letting CMED switch, then revoking the old label.
    """
    hospitals = []
    for value in body.cmed_hospital_ids:
        try:
            hospitals.append(integrity.safe_identifier(value, field="cmed_hospital_id"))
        except ValueError:
            raise HTTPException(status_code=422, detail=f"'{value}' is not a valid hospital id")
    expires_at = datetime.now(timezone.utc) + timedelta(days=body.expires_in_days)
    key = await _repo().create_cmed_key(label=body.label, created_by=body.created_by,
                                        expires_at=expires_at,
                                        cmed_hospital_ids=sorted(set(hospitals)))
    await _repo().audit(event_type="cmed_key.created", actor_type="admin",
                        actor_id=body.created_by,
                        detail={"label": body.label, "hospitals": sorted(set(hospitals))})
    return {"cmed_key": key, "label": body.label, "cmed_hospital_ids": sorted(set(hospitals)),
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
