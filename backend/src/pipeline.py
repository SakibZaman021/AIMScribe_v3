"""
The analysis pipeline (053, 27 Nov 2026).

Each confirmed recording's audio copy goes to the lab's GPU machine for a
Bengali transcript and named-entity recognition (NER); the NER and CMED's
prescription then go to the quality (metrics) service. Neither machine holds
a bucket key or a database login. They work through this narrow interface:

    machine   POST /api/v2/pipeline/claim                 jobs + signed tickets
              POST /api/v2/pipeline/jobs/{id}/lease       keep working on it
              GET  /api/v2/pipeline/jobs/{id}/audio       lab: the WAV, sealed to it
              GET  /api/v2/pipeline/jobs/{id}/inputs      metrics: prescription + NER
              POST /api/v2/pipeline/jobs/{id}/results/{transcript|ner|metrics}
              POST /api/v2/pipeline/jobs/{id}/fail
              GET  /api/v2/pipeline/metrics               metrics: results for its dashboard
              GET  /api/v2/pipeline/recordings/{rec}/transcript
    worker    GET  /api/v2/pipeline/results/pending       results to seal into the copies
              POST /api/v2/pipeline/results/authorize     where to put one
              POST /api/v2/pipeline/results/complete      it is there; checked here
    machine   POST /api/v2/pipeline/rotate                 ask for new keys (054)
    admin     POST /api/v2/pipeline/admin/machines        enrol a machine (a lab machine
                                                          must name its fixed IP address)
              POST /api/v2/pipeline/admin/machines/{id}/revoke
              POST /api/v2/pipeline/admin/machines/{id}/rotation/{approve|reject}
              GET  /api/v2/pipeline/admin/machines | /admin/status
              POST /api/v2/pipeline/admin/sweep

Every machine request is signed with the machine's Ed25519 key over method,
path, body hash, time and a single-use nonce (the recorders' scheme, its own
domain string). A job is handed out with a ticket signed by this server's
grant key: job, pseudonymous recording, steps, the archived audio's SHA-256,
the machine and an expiry. A result is accepted only with a valid ticket for
that machine and job, the audio hash the machine computed matching the
ticket, a step not already accepted, and a body of the agreed format. The
machine never names a path: the folder is worked out here from the job.

Every refused request is logged as an integrity alert; one that names an
enrolled machine (a bad signature, another address, a replayed nonce, a
revoked machine) is critical, which the server monitor pages to the
administrators at once. So is a machine fetching far more audio than usual
(AIMS_PIPELINE_AUDIO_PER_HOUR, default 300).

Switched on with AIMS_PIPELINE=on; off, every route answers 404.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import ipaddress
import json
import logging
import os
import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, ValidationError

import api_v2
from api_v2 import require_admin, require_viewer, require_worker

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v2/pipeline", tags=["pipeline"])

SIGNATURE_DOMAIN = b"AIMS-PIPELINE-SIG-V1\n"
SIGNATURE_WINDOW = 300
TICKET_AUDIENCE = "aimscribe-pipeline"
TICKET_SECONDS = 24 * 3600
LEASE = timedelta(minutes=30)
BACKOFF_SECONDS = (300, 1800, 7200)          # after the 1st, 2nd and 3rd failure
MAX_ATTEMPTS = 4                             # then parked for a person
SWEEP_EVERY = 30.0
STEPS = {"lab": ("transcript", "ner"), "compare": ("metrics",)}
ROLE_KIND = {"lab": "lab", "metrics": "compare"}
LIMITS = {"transcript": 2_000_000, "ner": 1_000_000, "metrics": 256_000}
_NONCE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
ROTATE_DOMAIN = b"AIMS-PIPELINE-ROTATE-V1\n"
KEY_AGE_DAYS = 90
REFUSAL_WINDOW = timedelta(hours=1)
# Refusals that mean someone holds, or is trying, an enrolled machine's identity.
ALARMING = {"signature", "address", "replay", "revoked"}
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_JOB = re.compile(r"^(lab|cmp)_[0-9a-f]{32}$")
_REC = re.compile(r"^rec_[0-9a-f]{32}$")
# Never handed to the metrics service, wherever they sit in the prescription.
_PERSONAL = {"patient_id", "hospital_patient_id", "encounter_id", "patient_information_id",
             "patient_name", "name", "mobile", "phone", "address", "nid", "email"}


def enabled() -> bool:
    return os.getenv("AIMS_PIPELINE", "off").strip().lower() in ("1", "on", "true", "yes")


def _on() -> None:
    if not enabled():
        raise HTTPException(status_code=404, detail="not found")


def _repo():
    return api_v2._repo()


# ============================================================
# Machines and their signed requests
# ============================================================

def request_message(*, method: str, path: bytes, timestamp: str, nonce: str,
                    body: bytes) -> bytes:
    """What a pipeline machine signs, one field a line (like the recorders')."""
    return b"".join([
        SIGNATURE_DOMAIN, method.upper().encode("ascii"), b"\n", path, b"\n",
        str(timestamp).encode("ascii"), b"\n", nonce.encode("ascii"), b"\n",
        hashlib.sha256(body or b"").hexdigest().encode("ascii")])


async def _refused(reason: str, *, status: int, detail: str, address: str,
                   machine: Optional[Dict[str, Any]] = None,
                   machine_id: Optional[str] = None) -> None:
    """Log a refused request; one naming an enrolled machine pages the administrators."""
    known = machine is not None
    severity = "critical" if known and reason in ALARMING else "warning"
    try:
        await _repo().alert_once(
            alert_type="pipeline_access_refused",
            group=f"pipeline_refused:{machine['machine_id'] if known else address}:{reason}",
            window=REFUSAL_WINDOW, severity=severity,
            detail={"reason": reason, "detail": detail, "address": address,
                    "machine": machine["name"] if known else None,
                    "machine_id": str(machine["machine_id"]) if known else machine_id})
    except Exception as exc:                        # the refusal stands regardless
        logger.warning("Could not log a refused pipeline request: %s", exc)
    logger.warning("Pipeline request refused (%s) from %s%s", reason, address,
                   f" as {machine['name']}" if known else "")
    raise HTTPException(status_code=status, detail=detail)


async def require_machine(request: Request,
                          x_pipeline_machine: Optional[str] = Header(None),
                          x_pipeline_timestamp: Optional[str] = Header(None),
                          x_pipeline_nonce: Optional[str] = Header(None),
                          x_pipeline_signature: Optional[str] = Header(None)) -> Dict[str, Any]:
    """An enrolled, unrevoked machine, from its allowed address, signing this request."""
    _on()
    import security
    address = security.client_ip(request)
    try:
        machine_id = uuid.UUID(str(x_pipeline_machine))
    except ValueError:
        await _refused("no machine", status=401, detail="X-Pipeline-Machine is required",
                       address=address)
    async with _repo()._acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM pipeline_machines WHERE machine_id = $1",
                                  machine_id)
    if row is None:
        await _refused("unknown machine", status=401, detail="unknown machine",
                       address=address, machine_id=str(machine_id))
    machine = dict(row)
    if machine["revoked_at"] is not None:
        await _refused("revoked", status=403, detail="this machine was revoked",
                       address=address, machine=machine)
    if machine["allowed_ips"] and address not in machine["allowed_ips"]:
        await _refused("address", status=403, detail="not from this machine's address",
                       address=address, machine=machine)
    if not (x_pipeline_timestamp and x_pipeline_nonce and x_pipeline_signature):
        await _refused("unsigned", status=401, detail="this request must be signed",
                       address=address, machine=machine)
    if not (x_pipeline_timestamp.isascii() and x_pipeline_timestamp.isdigit()) \
            or abs(time.time() - int(x_pipeline_timestamp)) > SIGNATURE_WINDOW:
        await _refused("clock", status=401, detail="request time is outside 5 minutes; "
                                                   "check the machine's clock",
                       address=address, machine=machine)
    if not _NONCE.match(x_pipeline_nonce):
        await _refused("nonce", status=401, detail="X-Pipeline-Nonce is not valid",
                       address=address, machine=machine)
    try:
        signature = api_v2._b64url_decode(x_pipeline_signature)
    except ValueError:
        await _refused("signature", status=401, detail="X-Pipeline-Signature is not base64url",
                       address=address, machine=machine)
    body = await request.body()
    message = request_message(method=request.method, path=api_v2._signed_path(request),
                              timestamp=x_pipeline_timestamp, nonce=x_pipeline_nonce,
                              body=body)
    import integrity
    if not integrity.verify_device_signature(bytes(machine["sign_pubkey"]), message,
                                             signature):
        await _refused("signature", status=401, detail="request signature does not verify",
                       address=address, machine=machine)
    async with _repo()._acquire() as conn:
        used = await conn.fetchval("""
            INSERT INTO pipeline_nonces (machine_id, nonce) VALUES ($1, $2)
            ON CONFLICT DO NOTHING RETURNING 1
        """, machine_id, x_pipeline_nonce)
        if used:
            await conn.execute("UPDATE pipeline_machines SET last_seen_at = now(), "
                               "last_address = $2 WHERE machine_id = $1", machine_id, address)
    if not used:
        await _refused("replay", status=401, detail="request nonce was already used",
                       address=address, machine=machine)
    return machine


def _role(machine: Dict[str, Any], role: str) -> None:
    if machine["role"] != role:
        raise HTTPException(status_code=403, detail=f"this needs a {role} machine")


# ============================================================
# Tickets
# ============================================================

def _ticket_key():
    if api_v2.ctx.grants is None:
        raise HTTPException(status_code=503, detail="the signing key is not configured")
    return api_v2.ctx.grants._key


def issue_ticket(job: Dict[str, Any], machine: Dict[str, Any]) -> str:
    import jwt
    now = int(time.time())
    return jwt.encode({
        "iss": "aimslab", "aud": TICKET_AUDIENCE, "sub": str(machine["machine_id"]),
        "jti": job["job_id"], "rec": job["rec"], "steps": list(STEPS[job["kind"]]),
        "audio_sha256": bytes(job["audio_sha256"]).hex(), "iat": now,
        "exp": now + TICKET_SECONDS}, _ticket_key(), algorithm="EdDSA")


def check_ticket(token: str, job: Dict[str, Any], machine: Dict[str, Any],
                 step: str) -> Dict[str, Any]:
    import jwt
    try:
        claims = jwt.decode(token, _ticket_key().public_key(), algorithms=["EdDSA"],
                            audience=TICKET_AUDIENCE, issuer="aimslab",
                            options={"require": ["exp", "jti", "sub", "aud"]})
    except jwt.PyJWTError as exc:
        raise HTTPException(status_code=403, detail=f"ticket refused: {exc}")
    if claims.get("jti") != job["job_id"] or claims.get("sub") != str(machine["machine_id"]):
        raise HTTPException(status_code=403, detail="ticket is for another job or machine")
    if step not in claims.get("steps", ()):
        raise HTTPException(status_code=403, detail=f"ticket does not cover {step}")
    if claims.get("audio_sha256") != bytes(job["audio_sha256"]).hex():
        raise HTTPException(status_code=403, detail="ticket is for other audio")
    return claims


# ============================================================
# The queue
# ============================================================

_last_sweep = 0.0
_sweep_lock = asyncio.Lock()


def _stem(object_key: str) -> str:
    name = object_key.rsplit("/", 1)[-1]
    return re.sub(r"\.v\d+\.[a-z.]+$", "", name)


async def sweep(force: bool = False) -> Dict[str, int]:
    """
    Return expired leases, queue every confirmed audio copy that has no lab job,
    and a compare job for every finished lab job. Safe to run at any time and
    from more than one process: every insert is ON CONFLICT DO NOTHING.
    """
    global _last_sweep
    if not force and time.monotonic() - _last_sweep < SWEEP_EVERY:
        return {}
    async with _sweep_lock:
        _last_sweep = time.monotonic()
        repo = _repo()
        counts = {"returned": 0, "queued": 0, "compare": 0}
        async with repo._acquire() as conn:
            counts["returned"] = int((await conn.execute("""
                UPDATE pipeline_jobs
                   SET state = CASE WHEN attempts >= $1 THEN 'parked' ELSE 'ready' END,
                       machine_id = NULL, lease_until = NULL, updated_at = now(),
                       next_try_at = now(), last_error = 'lease ran out'
                 WHERE state = 'leased' AND lease_until < now()
            """, MAX_ATTEMPTS)).split()[-1])
            candidates = await conn.fetch("""
                SELECT c.session_id, c.object_key, c.version, c.plain_sha256
                  FROM cloud_copies c JOIN sessions s ON s.session_id = c.session_id
                 WHERE c.kind = 'audio' AND c.plain_sha256 IS NOT NULL
                   AND s.confirmation = 'confirmed'
                   AND NOT EXISTS (SELECT 1 FROM pipeline_jobs j
                                    WHERE j.kind = 'lab' AND j.copy_object_key = c.object_key)
                 ORDER BY c.uploaded_at LIMIT 200
            """)
            await conn.execute("DELETE FROM pipeline_nonces "
                               "WHERE used_at < now() - interval '1 day'")
            stale = await conn.fetch("SELECT machine_id, name, keys_since FROM "
                                     "pipeline_machines WHERE revoked_at IS NULL AND "
                                     "keys_since < now() - make_interval(days => $1)",
                                     KEY_AGE_DAYS)
        for old in stale:
            await repo.alert_once(
                alert_type="pipeline_keys_old", group=f"pipeline_keys:{old['machine_id']}",
                window=timedelta(days=7), severity="warning",
                detail={"machine": old["name"], "keys_since": old["keys_since"].isoformat(),
                        "fix": "run agent.py rotate on the machine, then approve it"})
        per_session: Dict[str, List[Dict[str, Any]]] = {}
        for row in candidates:
            per_session.setdefault(row["session_id"], []).append(dict(row))
        for session_id, rows in per_session.items():
            copies = [c for c in await repo.copies_for(session_id) if c["kind"] == "audio"]
            newest = {}
            for c in copies:
                stem = _stem(c["object_key"])
                if c["version"] > newest.get(stem, 0):
                    newest[stem] = c["version"]
            parts = {p.get("file_stem"): p for p in await repo.parts_for(session_id)}
            session = await repo.get_session(session_id)
            for row in rows:
                stem = _stem(row["object_key"])
                if row["version"] < newest.get(stem, 0):
                    continue                     # an older copy of a file re-copied since
                part = parts.get(stem)
                part_no = part["part_no"] if part else None
                size = (part or session or {}).get("archive_bytes")
                async with repo._acquire() as conn:
                    rec = await conn.fetchval("""
                        SELECT rec FROM pipeline_jobs WHERE session_id = $1
                           AND part_no IS NOT DISTINCT FROM $2 LIMIT 1
                    """, session_id, part_no) or f"rec_{uuid.uuid4().hex}"
                    inserted = await conn.fetchval("""
                        INSERT INTO pipeline_jobs (job_id, kind, rec, session_id, part_no,
                                                   copy_object_key, audio_sha256, audio_bytes)
                        VALUES ($1, 'lab', $2, $3, $4, $5, $6, $7)
                        ON CONFLICT DO NOTHING RETURNING 1
                    """, f"lab_{uuid.uuid4().hex}", rec, session_id, part_no,
                        row["object_key"], bytes(row["plain_sha256"]), size)
                counts["queued"] += 1 if inserted else 0
        async with repo._acquire() as conn:
            counts["compare"] = int((await conn.execute("""
                INSERT INTO pipeline_jobs (job_id, kind, rec, session_id, part_no,
                                           copy_object_key, audio_sha256, audio_bytes, run)
                SELECT 'cmp_' || replace(gen_random_uuid()::text, '-', ''), 'compare', j.rec,
                       j.session_id, j.part_no, j.copy_object_key, j.audio_sha256,
                       j.audio_bytes, j.run
                  FROM pipeline_jobs j
                 WHERE j.kind = 'lab' AND j.state = 'done'
                   AND NOT EXISTS (SELECT 1 FROM pipeline_jobs c WHERE c.kind = 'compare'
                                      AND c.copy_object_key = j.copy_object_key
                                      AND c.run = j.run)
                ON CONFLICT DO NOTHING
            """)).split()[-1])
        if any(counts.values()):
            logger.info("Pipeline sweep: %s", counts)
        return counts


async def _job_for(job_id: str, machine: Dict[str, Any], *, kind: str) -> Dict[str, Any]:
    if not _JOB.match(job_id or ""):
        raise HTTPException(status_code=404, detail="no such job")
    async with _repo()._acquire() as conn:
        job = await conn.fetchrow("SELECT * FROM pipeline_jobs WHERE job_id = $1", job_id)
    if job is None or job["kind"] != kind:
        raise HTTPException(status_code=404, detail="no such job")
    return dict(job)


def _holds(job: Dict[str, Any], machine: Dict[str, Any]) -> None:
    if job["state"] != "leased" or job["machine_id"] != machine["machine_id"]:
        raise HTTPException(status_code=409, detail="this machine does not hold the job "
                                                    "(its lease ran out or it was not claimed)")


# ============================================================
# Machine routes
# ============================================================

class ClaimRequest(BaseModel):
    max: int = Field(4, ge=1, le=16)
    wait: int = Field(20, ge=0, le=25)


@router.post("/claim")
async def claim(body: ClaimRequest, machine=Depends(require_machine)):
    """
    Up to `max` jobs of this machine's kind, each with its ticket. Waits up to
    `wait` seconds for one to be ready (long poll) and answers an empty list
    when none came. A machine never holds more than its max_inflight.
    """
    kind = ROLE_KIND[machine["role"]]
    deadline = time.monotonic() + body.wait
    while True:
        await sweep()
        async with _repo()._acquire() as conn:
            held = await conn.fetchval("SELECT count(*) FROM pipeline_jobs WHERE state = "
                                       "'leased' AND machine_id = $1", machine["machine_id"])
            room = min(body.max, machine["max_inflight"] - int(held))
            rows = [] if room <= 0 else await conn.fetch("""
                UPDATE pipeline_jobs SET state = 'leased', machine_id = $1,
                       lease_until = now() + $2::interval, attempts = attempts + 1,
                       updated_at = now()
                 WHERE job_id IN (SELECT job_id FROM pipeline_jobs
                                   WHERE kind = $3 AND state = 'ready' AND next_try_at <= now()
                                   ORDER BY created_at
                                   FOR UPDATE SKIP LOCKED LIMIT $4)
                RETURNING *
            """, machine["machine_id"], LEASE, kind, room)
        if rows or time.monotonic() >= deadline or room <= 0:
            break
        await asyncio.sleep(1.0)
    jobs = []
    for row in rows:
        job = dict(row)
        jobs.append({"job_id": job["job_id"], "kind": kind, "rec": job["rec"],
                     "ticket": issue_ticket(job, machine),
                     "audio_sha256": bytes(job["audio_sha256"]).hex(),
                     "audio_bytes": job["audio_bytes"], "steps": list(STEPS[kind]),
                     "lease_until": job["lease_until"].isoformat()})
    if jobs:
        await _repo().audit(event_type="pipeline.claimed", actor_type="device",
                            actor_id=machine["name"],
                            detail={"jobs": [j["job_id"] for j in jobs]})
    return {"jobs": jobs}


class TicketBody(BaseModel):
    ticket: str = Field(..., max_length=4096)


@router.post("/jobs/{job_id}/lease")
async def extend_lease(job_id: str, body: TicketBody, machine=Depends(require_machine)):
    job = await _job_for(job_id, machine, kind=ROLE_KIND[machine["role"]])
    check_ticket(body.ticket, job, machine, STEPS[job["kind"]][0])
    _holds(job, machine)
    async with _repo()._acquire() as conn:
        until = await conn.fetchval("UPDATE pipeline_jobs SET lease_until = now() + "
                                    "$2::interval, updated_at = now() WHERE job_id = $1 "
                                    "RETURNING lease_until", job_id, LEASE)
    return {"job_id": job_id, "lease_until": until.isoformat()}


async def _audio_copy(job: Dict[str, Any]) -> Dict[str, Any]:
    """The copy's binding, as downloads check it."""
    repo = _repo()
    stem = _stem(job["copy_object_key"])
    expect: Dict[str, Any] = {"session_id": job["session_id"], "kind": "audio",
                              "stem_sha256": hashlib.sha256(stem.encode("utf-8")).hexdigest()}
    if job["part_no"] is not None:
        expect["part_no"] = job["part_no"]
    copy = next((c for c in await repo.copies_for(job["session_id"])
                 if c["object_key"] == job["copy_object_key"]), None)
    if copy is None:
        raise HTTPException(status_code=409, detail="the audio copy is not there any more")
    return expect


@router.get("/jobs/{job_id}/audio")
async def job_audio(job_id: str, machine=Depends(require_machine),
                    x_pipeline_ticket: Optional[str] = Header(None)):
    """The archived WAV, sealed to this machine's key by the archive worker."""
    _role(machine, "lab")
    job = await _job_for(job_id, machine, kind="lab")
    check_ticket(x_pipeline_ticket or "", job, machine, "transcript")
    _holds(job, machine)
    import downloads
    if not downloads._decrypt_url():
        raise HTTPException(status_code=503, detail="the archive worker is not set up")
    expect = await _audio_copy(job)
    store = api_v2._copy_bucket("audio")
    link = await asyncio.get_event_loop().run_in_executor(
        None, store.get_presigned_download_url, job["copy_object_key"], 300)
    client = downloads._client()
    try:
        answer = await client.send(client.build_request(
            "POST", downloads._decrypt_url() + "/seal",
            headers={"X-Worker-Key": os.getenv("AIMS_WORKER_KEY", "")},
            json={"url": link, "expect": expect,
                  "plain_sha256": bytes(job["audio_sha256"]).hex(),
                  "job_id": job_id, "recipient": bytes(machine["box_pubkey"]).hex()}),
            stream=True)
    except Exception as exc:
        await client.aclose()
        raise HTTPException(status_code=503, detail=f"the archive worker could not be "
                                                    f"reached: {type(exc).__name__}")
    if answer.status_code != 200:
        detail = (await answer.aread()).decode("utf-8", "replace")[:300]
        await answer.aclose()
        await client.aclose()
        raise HTTPException(status_code=502 if answer.status_code != 429 else 503,
                            detail=f"the archive worker refused: {detail}")
    await _repo().audit(event_type="pipeline.audio_sent", actor_type="device",
                        actor_id=machine["name"], session_id=job["session_id"],
                        detail={"job": job_id, "bytes": answer.headers.get("content-length")})
    await _check_volume(machine)

    async def stream():
        try:
            async for block in answer.aiter_bytes():
                yield block
        finally:
            await answer.aclose()
            await client.aclose()

    headers = {"Content-Length": answer.headers.get("content-length", "")}
    return StreamingResponse(stream(), media_type="application/octet-stream",
                             headers={k: v for k, v in headers.items() if v})


# ---- result formats (v1) ----

class Segment(BaseModel):
    start: float = Field(..., ge=0, le=86400)
    end: float = Field(..., ge=0, le=86400)
    text: str = Field(..., max_length=4000)
    speaker: Optional[str] = Field(None, max_length=32)


class TranscriptData(BaseModel):
    language: Literal["bn", "en", "mixed"] = "bn"
    text: str = Field(..., max_length=400_000)
    segments: List[Segment] = Field(default_factory=list, max_length=20_000)
    english_text: Optional[str] = Field(None, max_length=400_000)


class Entity(BaseModel):
    type: str = Field(..., pattern=r"^[A-Za-z_]{1,32}$")
    text: str = Field(..., max_length=500)
    start: Optional[int] = Field(None, ge=0)
    end: Optional[int] = Field(None, ge=0)
    normalized: Optional[str] = Field(None, max_length=500)
    confidence: Optional[float] = Field(None, ge=0, le=1)


class NerData(BaseModel):
    entities: List[Entity] = Field(..., max_length=10_000)


class MetricsData(BaseModel):
    scores: Dict[str, float] = Field(..., max_length=200)
    details: Optional[Dict[str, Any]] = None


DATA_MODELS = {"transcript": TranscriptData, "ner": NerData, "metrics": MetricsData}


class ResultEnvelope(BaseModel):
    ticket: str = Field(..., max_length=4096)
    audio_sha256: Optional[str] = Field(None, pattern=r"^[0-9a-f]{64}$")
    model: str = Field(..., min_length=1, max_length=120)
    model_version: str = Field(..., min_length=1, max_length=120)
    weights_sha256: Optional[str] = Field(None, pattern=r"^[0-9a-f]{64}$")
    data: Dict[str, Any]


@router.post("/jobs/{job_id}/results/{kind}")
async def post_result(job_id: str, kind: str, request: Request,
                      machine=Depends(require_machine)):
    if kind not in DATA_MODELS:
        raise HTTPException(status_code=404, detail="no such result kind")
    raw = await request.body()
    if len(raw) > LIMITS[kind] + 8192:
        raise HTTPException(status_code=413, detail=f"a {kind} result is at most "
                                                    f"{LIMITS[kind]} bytes")
    try:
        envelope = ResultEnvelope.model_validate_json(raw)
        data = DATA_MODELS[kind].model_validate(envelope.data)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=f"not the agreed {kind} format: "
                                                    f"{exc.errors()[:3]}")
    job_kind = "compare" if kind == "metrics" else "lab"
    _role(machine, "metrics" if job_kind == "compare" else "lab")
    job = await _job_for(job_id, machine, kind=job_kind)
    check_ticket(envelope.ticket, job, machine, kind)
    if job_kind == "lab" and envelope.audio_sha256 != bytes(job["audio_sha256"]).hex():
        raise HTTPException(status_code=409, detail="the audio this machine processed is "
                                                    "not the archived recording")
    body = data.model_dump(mode="json", exclude_none=True)
    canonical = json.dumps(body, sort_keys=True, ensure_ascii=False,
                           separators=(",", ":")).encode("utf-8")
    digest = hashlib.sha256(canonical).digest()
    async with _repo()._acquire() as conn:
        async with conn.transaction():
            locked = await conn.fetchrow("SELECT * FROM pipeline_jobs WHERE job_id = $1 "
                                         "FOR UPDATE", job_id)
            existing = await conn.fetchrow("SELECT body_sha256 FROM pipeline_results "
                                           "WHERE job_id = $1 AND kind = $2", job_id, kind)
            if existing is not None:
                if bytes(existing["body_sha256"]) == digest:
                    return {"status": "already accepted", "job_id": job_id, "kind": kind}
                raise HTTPException(status_code=409, detail=f"a different {kind} was "
                                                            f"already accepted for this job")
            _holds(dict(locked), machine)
            if kind == "ner" and not await conn.fetchval(
                    "SELECT 1 FROM pipeline_results WHERE job_id = $1 AND kind = "
                    "'transcript'", job_id):
                raise HTTPException(status_code=409, detail="post the transcript first")
            await conn.execute("""
                INSERT INTO pipeline_results (job_id, kind, body, body_sha256, model,
                                              model_version, weights_sha256, machine_id)
                VALUES ($1, $2, $3::jsonb, $4, $5, $6, $7, $8)
            """, job_id, kind, canonical.decode("utf-8"), digest, envelope.model,
                envelope.model_version, envelope.weights_sha256, machine["machine_id"])
            last = STEPS[job_kind][-1]
            if kind == last:
                await conn.execute("UPDATE pipeline_jobs SET state = 'done', done_at = now(),"
                                   " lease_until = NULL, updated_at = now(), last_error = "
                                   "NULL WHERE job_id = $1", job_id)
    await _repo().audit(event_type=f"pipeline.{kind}_accepted", actor_type="device",
                        actor_id=machine["name"], session_id=job["session_id"],
                        detail={"job": job_id, "model": envelope.model,
                                "model_version": envelope.model_version,
                                "sha256": digest.hex()})
    if kind == STEPS[job_kind][-1]:
        await sweep(force=True)                  # the compare job, at once
    return {"status": "accepted", "job_id": job_id, "kind": kind}


class FailBody(TicketBody):
    reason: str = Field(..., min_length=1, max_length=500)


@router.post("/jobs/{job_id}/fail")
async def fail_job(job_id: str, body: FailBody, machine=Depends(require_machine)):
    job = await _job_for(job_id, machine, kind=ROLE_KIND[machine["role"]])
    check_ticket(body.ticket, job, machine, STEPS[job["kind"]][0])
    _holds(job, machine)
    attempts = job["attempts"]
    parked = attempts >= MAX_ATTEMPTS
    wait = BACKOFF_SECONDS[min(attempts, len(BACKOFF_SECONDS)) - 1] if attempts else 0
    async with _repo()._acquire() as conn:
        await conn.execute("""
            UPDATE pipeline_jobs SET state = $2, machine_id = NULL, lease_until = NULL,
                   next_try_at = now() + make_interval(secs => $3), last_error = $4,
                   updated_at = now()
             WHERE job_id = $1
        """, job_id, "parked" if parked else "ready", float(wait), body.reason)
    if parked:
        await _repo().raise_alert(alert_type="pipeline_job_parked", severity="warning",
                                  session_id=job["session_id"],
                                  detail={"job": job_id, "reason": body.reason,
                                          "attempts": attempts})
    return {"job_id": job_id, "state": "parked" if parked else "ready",
            "next_try_in_seconds": 0 if parked else wait}


def _canonical(body: Any) -> str:
    """The exact text a result was fingerprinted as (Postgres re-formats JSONB)."""
    value = json.loads(body) if isinstance(body, str) else body
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


async def _check_volume(machine: Dict[str, Any]) -> None:
    """Page the administrators when a machine fetches far more audio than usual."""
    try:
        limit = int(os.getenv("AIMS_PIPELINE_AUDIO_PER_HOUR", "300"))
    except ValueError:
        limit = 300
    try:
        async with _repo()._acquire() as conn:
            fetched = await conn.fetchval("""
                SELECT count(*) FROM audit_log WHERE event_type = 'pipeline.audio_sent'
                   AND actor_id = $1 AND occurred_at > now() - interval '1 hour'
            """, machine["name"])
        if int(fetched) > limit:
            await _repo().alert_once(
                alert_type="pipeline_unusual_volume",
                group=f"pipeline_volume:{machine['machine_id']}", window=REFUSAL_WINDOW,
                severity="critical",
                detail={"machine": machine["name"], "audio_last_hour": int(fetched),
                        "limit": limit, "fix": "revoke the machine if this is not expected"})
    except Exception as exc:
        logger.warning("Could not check %s's audio volume: %s", machine["name"], exc)


def _without_personal(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _without_personal(v) for k, v in value.items() if k not in _PERSONAL}
    if isinstance(value, list):
        return [_without_personal(v) for v in value]
    return value


@router.get("/jobs/{job_id}/inputs")
async def compare_inputs(job_id: str, machine=Depends(require_machine),
                         x_pipeline_ticket: Optional[str] = Header(None)):
    """CMED's prescription and the lab's NER for one compare job. Never audio."""
    _role(machine, "metrics")
    job = await _job_for(job_id, machine, kind="compare")
    check_ticket(x_pipeline_ticket or "", job, machine, "metrics")
    _holds(job, machine)
    repo = _repo()
    session = await repo.get_session(job["session_id"])
    part = None
    if job["part_no"] is not None:
        part = next((p for p in await repo.parts_for(job["session_id"])
                     if p["part_no"] == job["part_no"]), None)
    document = await api_v2._clinical_document(session, part=part) if session else {}
    async with repo._acquire() as conn:
        ner = await conn.fetchval("""
            SELECT r.body FROM pipeline_results r JOIN pipeline_jobs j ON j.job_id = r.job_id
             WHERE j.kind = 'lab' AND j.copy_object_key = $1 AND j.run = $2
               AND r.kind = 'ner'
        """, job["copy_object_key"], job["run"])
    await repo.audit(event_type="pipeline.inputs_sent", actor_type="device",
                     actor_id=machine["name"], session_id=job["session_id"],
                     detail={"job": job_id})
    return {"job_id": job_id, "rec": job["rec"],
            "prescription": _without_personal(document.get("prescription")),
            "ner": json.loads(ner) if isinstance(ner, str) else ner}


@router.get("/metrics")
async def metrics_feed(since: Optional[str] = None, limit: int = 500,
                       machine=Depends(require_machine)):
    """Every metrics result, newest last, for the quality dashboard. No patient IDs."""
    _role(machine, "metrics")
    limit = max(1, min(limit, 2000))
    try:
        after = datetime.fromisoformat(since) if since else datetime(2000, 1, 1,
                                                                     tzinfo=timezone.utc)
    except ValueError:
        raise HTTPException(status_code=400, detail="since must be an ISO time")
    async with _repo()._acquire() as conn:
        rows = await conn.fetch("""
            SELECT j.rec, j.part_no, s.hospital_id, s.doctor_id, s.session_date,
                   r.body, r.model, r.model_version, r.received_at
              FROM pipeline_results r JOIN pipeline_jobs j ON j.job_id = r.job_id
              JOIN sessions s ON s.session_id = j.session_id
             WHERE r.kind = 'metrics' AND r.received_at > $1
             ORDER BY r.received_at LIMIT $2
        """, after, limit)
    return {"results": [{
        "rec": r["rec"], "part_no": r["part_no"], "hospital_id": r["hospital_id"],
        "doctor_id": r["doctor_id"],
        "date": r["session_date"].isoformat() if r["session_date"] else None,
        "metrics": json.loads(r["body"]) if isinstance(r["body"], str) else r["body"],
        "model": r["model"], "model_version": r["model_version"],
        "received_at": r["received_at"].isoformat()} for r in rows]}


@router.get("/recordings/{rec}/transcript")
async def recording_transcript(rec: str, machine=Depends(require_machine)):
    """One recording's transcript, when the quality dashboard opens it. Logged."""
    _role(machine, "metrics")
    if not _REC.match(rec):
        raise HTTPException(status_code=404, detail="no such recording")
    async with _repo()._acquire() as conn:
        row = await conn.fetchrow("""
            SELECT r.body, r.model, r.model_version, j.session_id
              FROM pipeline_results r JOIN pipeline_jobs j ON j.job_id = r.job_id
             WHERE j.rec = $1 AND r.kind = 'transcript'
             ORDER BY j.run DESC LIMIT 1
        """, rec)
    if row is None:
        raise HTTPException(status_code=404, detail="no transcript for this recording yet")
    await _repo().audit(event_type="pipeline.transcript_read", actor_type="device",
                        actor_id=machine["name"], session_id=row["session_id"],
                        detail={"rec": rec})
    body = row["body"]
    return {"rec": rec, "model": row["model"], "model_version": row["model_version"],
            "transcript": json.loads(body) if isinstance(body, str) else body}


# ============================================================
# The archive worker: results sealed into the recording's folder
# ============================================================

COPY_SUFFIX = {"transcript": "transcript.json.enc", "ner": "ner.json.enc",
               "metrics": "metrics.json.enc"}


async def _result_place(job: Dict[str, Any], kind: str) -> Dict[str, Any]:
    """Beside the audio copy it came from, in the same folder: <stem>.v<run>.<kind>.json.enc."""
    key = job["copy_object_key"]
    folder, _, name = key.rpartition("/")
    stem = _stem(key)
    return {"object_key": f"{folder}/{stem}.v{job['run']}.{COPY_SUFFIX[kind]}" if folder
            else f"{stem}.v{job['run']}.{COPY_SUFFIX[kind]}",
            "stem": stem, "session_id": job["session_id"], "part_no": job["part_no"]}


@router.get("/results/pending")
async def results_pending(limit: int = 10, _: None = Depends(require_worker)):
    _on()
    limit = max(1, min(limit, 50))
    async with _repo()._acquire() as conn:
        rows = await conn.fetch("""
            SELECT r.job_id, r.kind, r.body, encode(r.body_sha256, 'hex') AS sha,
                   j.session_id, j.part_no, j.run, j.copy_object_key
              FROM pipeline_results r JOIN pipeline_jobs j ON j.job_id = r.job_id
             WHERE r.copied_at IS NULL ORDER BY r.received_at LIMIT $1
        """, limit)
    out = []
    for r in rows:
        out.append({"job_id": r["job_id"], "kind": r["kind"], "session_id": r["session_id"],
                    "part_no": r["part_no"], "stem": _stem(r["copy_object_key"]),
                    "body": _canonical(r["body"]), "body_sha256": r["sha"]})
    return {"sessions": out}


class ResultPlaceRequest(BaseModel):
    job_id: str = Field(..., pattern=r"^(lab|cmp)_[0-9a-f]{32}$")
    kind: Literal["transcript", "ner", "metrics"]


@router.post("/results/authorize")
async def results_authorize(body: ResultPlaceRequest, _: None = Depends(require_worker)):
    _on()
    async with _repo()._acquire() as conn:
        job = await conn.fetchrow("SELECT * FROM pipeline_jobs WHERE job_id = $1", body.job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="no such job")
    place = await _result_place(dict(job), body.kind)
    store = api_v2._copy_bucket("audio")
    url = await asyncio.get_event_loop().run_in_executor(
        None, store.get_presigned_upload_url, place["object_key"], 3600)
    return {**place, "upload_url": url}


class ResultCopied(ResultPlaceRequest):
    object_key: str = Field(..., max_length=512)
    bytes: int = Field(..., ge=1)
    sha256: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    plain_sha256: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    md5: Optional[str] = Field(None, pattern=r"^[0-9a-f]{32}$")


@router.post("/results/complete")
async def results_complete(body: ResultCopied, _: None = Depends(require_worker)):
    """The sealed result is in the recording's folder: checked against the store."""
    _on()
    async with _repo()._acquire() as conn:
        job = await conn.fetchrow("SELECT * FROM pipeline_jobs WHERE job_id = $1", body.job_id)
        result = await conn.fetchrow("SELECT * FROM pipeline_results WHERE job_id = $1 "
                                     "AND kind = $2", body.job_id, body.kind)
    if job is None or result is None:
        raise HTTPException(status_code=404, detail="no such result")
    place = await _result_place(dict(job), body.kind)
    if body.object_key != place["object_key"]:
        raise HTTPException(status_code=409, detail="not this result's place")
    if body.plain_sha256 != bytes(result["body_sha256"]).hex():
        raise HTTPException(status_code=409, detail="the sealed file is not this result")
    store = api_v2._copy_bucket("audio")
    try:
        stored = await asyncio.get_event_loop().run_in_executor(
            None, store.client.stat_object, store.bucket, body.object_key)
    except Exception as exc:
        raise HTTPException(status_code=409, detail=f"the store does not hold it: {exc}")
    if int(getattr(stored, "size", -1)) != body.bytes:
        raise HTTPException(status_code=409, detail="the store holds another size")
    async with _repo()._acquire() as conn:
        await conn.execute("UPDATE pipeline_results SET copy_object_key = $3, copied_at = "
                           "now() WHERE job_id = $1 AND kind = $2",
                           body.job_id, body.kind, body.object_key)
    return {"status": "recorded", "object_key": body.object_key}


# ============================================================
# Changing a machine's keys (054)
# ============================================================

class RevokeRequest(BaseModel):
    by: str = Field(..., min_length=2, max_length=128)


def rotation_message(machine_id: str, sign_hex: str, box_hex: str) -> bytes:
    """What the new signing key signs, to show the machine holds it."""
    return ROTATE_DOMAIN + f"{machine_id}\n{sign_hex}\n{box_hex}".encode("ascii")


class RotateRequest(BaseModel):
    sign_pubkey: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    box_pubkey: Optional[str] = Field(None, pattern=r"^[0-9a-f]{64}$")
    proof: str = Field(..., max_length=200)


@router.post("/rotate")
async def request_rotation(body: RotateRequest, machine=Depends(require_machine)):
    """
    New keys, asked for with the current key and proven with the new one.
    Nothing changes until an administrator approves; the current keys keep
    working until then.
    """
    if machine["role"] == "lab" and not body.box_pubkey:
        raise HTTPException(status_code=422, detail="a lab machine needs a new box_pubkey")
    box_hex = body.box_pubkey or ""
    if body.sign_pubkey == bytes(machine["sign_pubkey"]).hex():
        raise HTTPException(status_code=422, detail="that is the key already in use")
    import integrity
    try:
        proof = api_v2._b64url_decode(body.proof)
    except ValueError:
        raise HTTPException(status_code=422, detail="proof is not base64url")
    if not integrity.verify_device_signature(
            bytes.fromhex(body.sign_pubkey),
            rotation_message(str(machine["machine_id"]), body.sign_pubkey, box_hex), proof):
        raise HTTPException(status_code=403, detail="the proof does not verify with the "
                                                    "new key")
    async with _repo()._acquire() as conn:
        await conn.execute("""
            UPDATE pipeline_machines SET pending_sign_pubkey = $2, pending_box_pubkey = $3,
                   rotation_requested_at = now() WHERE machine_id = $1
        """, machine["machine_id"], bytes.fromhex(body.sign_pubkey),
            bytes.fromhex(box_hex) if box_hex else None)
    await _repo().audit(event_type="pipeline.rotation_requested", actor_type="device",
                        actor_id=machine["name"], detail={"machine": str(machine["machine_id"])})
    await _repo().alert_once(
        alert_type="pipeline_rotation_waiting",
        group=f"pipeline_rotation:{machine['machine_id']}", window=timedelta(days=1),
        severity="warning",
        detail={"machine": machine["name"], "fix": "approve or reject it"})
    return {"status": "waiting for an administrator", "machine_id": str(machine["machine_id"])}


@router.post("/admin/machines/{machine_id}/rotation/{decision}")
async def decide_rotation(machine_id: str, decision: Literal["approve", "reject"],
                          body: RevokeRequest, _: None = Depends(require_admin)):
    """Approve: the new keys replace the old at once. Reject: the request is dropped."""
    _on()
    try:
        mid = uuid.UUID(machine_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="no such machine")
    async with _repo()._acquire() as conn:
        if decision == "approve":
            done = await conn.fetchval("""
                UPDATE pipeline_machines
                   SET sign_pubkey = pending_sign_pubkey,
                       box_pubkey = COALESCE(pending_box_pubkey, box_pubkey),
                       pending_sign_pubkey = NULL, pending_box_pubkey = NULL,
                       rotation_requested_at = NULL, keys_since = now()
                 WHERE machine_id = $1 AND pending_sign_pubkey IS NOT NULL
                   AND revoked_at IS NULL
                RETURNING 1
            """, mid)
        else:
            done = await conn.fetchval("""
                UPDATE pipeline_machines SET pending_sign_pubkey = NULL,
                       pending_box_pubkey = NULL, rotation_requested_at = NULL
                 WHERE machine_id = $1 AND pending_sign_pubkey IS NOT NULL
                RETURNING 1
            """, mid)
    if not done:
        raise HTTPException(status_code=404, detail="no key change is waiting for this "
                                                    "machine")
    await _repo().audit(event_type=f"pipeline.rotation_{decision}d", actor_type="admin",
                        actor_id=body.by, detail={"machine": machine_id})
    return {"machine_id": machine_id, "status": f"{decision}d"}


# ============================================================
# Administration
# ============================================================

class EnrolRequest(BaseModel):
    name: str = Field(..., pattern=r"^[A-Za-z0-9 ._-]{3,64}$")
    role: Literal["lab", "metrics"]
    sign_pubkey: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    box_pubkey: Optional[str] = Field(None, pattern=r"^[0-9a-f]{64}$")
    allowed_ips: Optional[List[str]] = Field(None, max_length=16)
    max_inflight: int = Field(32, ge=1, le=256)
    by: str = Field(..., min_length=2, max_length=128)


@router.post("/admin/machines")
async def enrol_machine(body: EnrolRequest, _: None = Depends(require_admin)):
    _on()
    if body.role == "lab" and not body.box_pubkey:
        raise HTTPException(status_code=422, detail="a lab machine needs its box_pubkey")
    if body.role == "lab" and not body.allowed_ips:
        raise HTTPException(status_code=422, detail="a lab machine must name its fixed IP "
                                                    "address (allowed_ips)")
    ips = []
    for ip in body.allowed_ips or []:
        try:
            ips.append(str(ipaddress.ip_address(ip.strip())))
        except ValueError:
            raise HTTPException(status_code=422, detail=f"not an IP address: {ip!r}")
    body.allowed_ips = ips or None
    async with _repo()._acquire() as conn:
        try:
            machine_id = await conn.fetchval("""
                INSERT INTO pipeline_machines (name, role, sign_pubkey, box_pubkey,
                                               allowed_ips, max_inflight, enrolled_by)
                VALUES ($1, $2, $3, $4, $5, $6, $7) RETURNING machine_id
            """, body.name, body.role, bytes.fromhex(body.sign_pubkey),
                bytes.fromhex(body.box_pubkey) if body.box_pubkey else None,
                body.allowed_ips, body.max_inflight, body.by)
        except Exception as exc:
            if "unique" in str(exc).lower():
                raise HTTPException(status_code=409, detail="that name is taken")
            raise
    await _repo().audit(event_type="pipeline.machine_enrolled", actor_type="admin",
                        actor_id=body.by, detail={"machine": str(machine_id),
                                                  "name": body.name, "role": body.role})
    return {"machine_id": str(machine_id), "name": body.name, "role": body.role}


@router.post("/admin/machines/{machine_id}/revoke")
async def revoke_machine(machine_id: str, body: RevokeRequest,
                         _: None = Depends(require_admin)):
    _on()
    try:
        mid = uuid.UUID(machine_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="no such machine")
    async with _repo()._acquire() as conn:
        done = await conn.execute("UPDATE pipeline_machines SET revoked_at = now(), "
                                  "revoked_by = $2, pending_sign_pubkey = NULL, "
                                  "pending_box_pubkey = NULL WHERE machine_id = $1 AND "
                                  "revoked_at IS NULL", mid, body.by)
        await conn.execute("UPDATE pipeline_jobs SET state = 'ready', machine_id = NULL, "
                           "lease_until = NULL, updated_at = now() WHERE state = 'leased' "
                           "AND machine_id = $1", mid)
    if done.endswith("0"):
        raise HTTPException(status_code=404, detail="no such machine, or already revoked")
    await _repo().audit(event_type="pipeline.machine_revoked", actor_type="admin",
                        actor_id=body.by, detail={"machine": machine_id})
    return {"machine_id": machine_id, "status": "revoked"}


@router.get("/admin/machines")
async def list_machines(_: None = Depends(require_admin)):
    _on()
    async with _repo()._acquire() as conn:
        rows = await conn.fetch("SELECT machine_id, name, role, allowed_ips, max_inflight, "
                                "enrolled_by, enrolled_at, revoked_at, last_seen_at, "
                                "last_address, keys_since, rotation_requested_at "
                                "FROM pipeline_machines ORDER BY enrolled_at")
    return {"machines": [{k: (str(v) if isinstance(v, uuid.UUID) else
                              v.isoformat() if isinstance(v, datetime) else v)
                          for k, v in dict(r).items()} for r in rows]}


@router.get("/admin/status")
async def pipeline_status(_: None = Depends(require_viewer)):
    """For the dashboard: how full each stage is, and what is stuck."""
    _on()
    async with _repo()._acquire() as conn:
        states = await conn.fetch("SELECT kind, state, count(*) AS n FROM pipeline_jobs "
                                  "GROUP BY 1, 2")
        oldest = await conn.fetch("""
            SELECT kind, extract(epoch FROM now() - min(created_at))::int AS seconds
              FROM pipeline_jobs WHERE state IN ('ready', 'leased') GROUP BY 1
        """)
        hour = await conn.fetch("SELECT kind, count(*) AS n FROM pipeline_jobs WHERE "
                                "done_at > now() - interval '1 hour' GROUP BY 1")
        parked = await conn.fetch("SELECT job_id, kind, session_id, last_error, updated_at "
                                  "FROM pipeline_jobs WHERE state = 'parked' "
                                  "ORDER BY updated_at DESC LIMIT 50")
        seen = await conn.fetch("SELECT name, role, last_seen_at, keys_since, "
                                "rotation_requested_at FROM pipeline_machines "
                                "WHERE revoked_at IS NULL")
    by_kind: Dict[str, Dict[str, int]] = {}
    for r in states:
        by_kind.setdefault(r["kind"], {})[r["state"]] = r["n"]
    return {"jobs": by_kind,
            "oldest_waiting_seconds": {r["kind"]: r["seconds"] for r in oldest},
            "done_last_hour": {r["kind"]: r["n"] for r in hour},
            "parked": [{"job_id": r["job_id"], "kind": r["kind"],
                        "session_id": r["session_id"], "reason": r["last_error"],
                        "at": r["updated_at"].isoformat()} for r in parked],
            "machines": [{"name": r["name"], "role": r["role"],
                          "last_seen_at": r["last_seen_at"].isoformat()
                          if r["last_seen_at"] else None,
                          "keys_age_days": (datetime.now(timezone.utc) - r["keys_since"]).days,
                          "rotation_waiting": r["rotation_requested_at"] is not None}
                         for r in seen]}


@router.post("/admin/sweep")
async def sweep_now(_: None = Depends(require_admin)):
    _on()
    return await sweep(force=True)


__all__ = ["router", "enabled", "sweep", "request_message", "issue_ticket", "check_ticket"]
