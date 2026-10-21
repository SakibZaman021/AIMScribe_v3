"""
Channel B - CMED's server to this server (SRS 3.2 §6.2).

Three endpoints. The caller is proven per hospital (cmed_auth.py): a
five-minute bearer token got with a signed assertion, or - during the switch
only - a static key issued for named hospitals. Either way a message is
accepted only for a hospital that credential may speak for.

    POST /api/v2/clinical/patient-information   API 2, when a patient is opened
    POST /api/v2/clinical/prescription          API 3, when the prescription is built
    GET  /api/v2/clinical/patient/{id}/visits   what is held, so CMED sends only
                                                the history that is missing

The same messages arrive in FHIR R4 form at /fhir/r4 (fhir.py), are turned
into these bodies, and go through `process` below - one set of rules.

Every reply carries `status` and `code` (§6.2.4). The body is stored before the
reply is sent, so a `202 ACCEPTED` means the record is safe here.

Bodies go to the clinical database, aims_clinical (clinical_store.py). Only the
five fields of an API 2 are kept on the recordings side, for matching, so a
role that can search recordings never sees a patient's name (SRS-DBA-20).
"""
from __future__ import annotations

import base64
import json
import uuid
import logging
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

import api_v2
import cmed_auth
import security
import confirmation as conf
import integrity
from confirmation import FieldError, Visit

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v2/clinical", tags=["clinical"])

MAX_BODY_BYTES = 1_000_000                     # §6.2.1

MESSAGES = {
    "ACCEPTED": "Stored.",
    "ALREADY_RECEIVED": "This exact request was already stored.",
    "OK": "",
    "MALFORMED_JSON": "The body is not a JSON object.",
    "MISSING_FIELD": "A required field is missing or not in the expected format.",
    "INVALID_IDENTIFIER": "An identifier contains characters that are not allowed.",
    "INVALID_KEY": "The CMED key is missing or not valid.",
    "INVALID_TOKEN": "The access token is missing, unknown or expired.",
    "KEY_NOT_BOUND": "This key names no hospital.",
    "INSUFFICIENT_SCOPE": "This credential may not make this request.",
    "HOSPITAL_NOT_ALLOWED": "This credential may not send data for that hospital.",
    "TOO_LARGE": "The request is larger than 1 MB.",
    "SCHEMA_INVALID": "Stored in quarantine: some fields are not in the expected form.",
}


class _Refusal(Exception):
    def __init__(self, status: int, code: str, message: Optional[str] = None, **extra):
        super().__init__(message or MESSAGES.get(code, code))
        self.status = status
        self.code = code
        self.extra = extra


def _reply(status: int, code: str, message: Optional[str] = None, *,
           headers: Optional[Dict[str, str]] = None, **extra) -> JSONResponse:
    # Every reply names its request id, so a failure can be quoted to us and
    # found in our logs in one step.
    return JSONResponse(status_code=status, headers=headers, content={
        "status": status, "code": code,
        "message": message if message is not None else MESSAGES.get(code, ""),
        "request_id": security.request_id(),
        **extra,
    })


def key_digest(key: str) -> bytes:
    return integrity.sha256_bytes(key.encode("utf-8"))


async def _authenticate(request: Request, scope: str) -> cmed_auth.Principal:
    """Tokens and keys are looked up by their hash; neither is stored (SRS-CHB-02)."""
    try:
        return await cmed_auth.authenticate(request, scope)
    except cmed_auth.AuthError as exc:
        raise _Refusal(exc.status, exc.code, str(exc))


async def check_hospital(principal: cmed_auth.Principal, visit: Visit, kind: str) -> None:
    """
    A credential speaks for its own hospitals only. Anything else is refused
    and raised as an alert: it is either a CMED mistake or a stolen credential
    being tried against another hospital, and both need a person to look.
    """
    if principal.may_write_for(visit.cmed_hospital_id):
        return
    await api_v2._repo().raise_alert(
        alert_type="cmed_hospital_mismatch", severity="critical",
        detail={"client": principal.client_id, "kind": kind,
                "cmed_hospital_id": visit.cmed_hospital_id,
                "allowed": sorted(principal.hospitals)})
    raise _Refusal(403, "HOSPITAL_NOT_ALLOWED", fields=["hospital_id"])


def _no_constants(name: str):
    raise ValueError(f"{name} is not JSON")


async def _read_json(request: Request) -> tuple:
    """
    One JSON object of at most 1 MB, in strict JSON: NaN and Infinity (which
    Python accepts and PostgreSQL does not), unpaired surrogates and NUL
    characters are refused here as MALFORMED_JSON rather than failing later
    as a 500 that a sender's outbox would retry for ever.
    """
    declared = request.headers.get("content-length")
    if declared and declared.isascii() and declared.isdigit() \
            and int(declared) > MAX_BODY_BYTES:
        raise _Refusal(413, "TOO_LARGE")
    raw = await request.body()
    if len(raw) > MAX_BODY_BYTES:
        raise _Refusal(413, "TOO_LARGE")
    try:
        text = raw.decode("utf-8")
        body = json.loads(text, parse_constant=_no_constants)
        json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (UnicodeError, ValueError, RecursionError):
        raise _Refusal(400, "MALFORMED_JSON")
    if not isinstance(body, dict):
        raise _Refusal(400, "MALFORMED_JSON")
    if "\x00" in text or "\\u0000" in text:
        raise _Refusal(400, "MALFORMED_JSON", "The body contains a NUL character.")
    return body, text


# ------------------------------------------------------------------ validation

def _problem(field: str, text: str) -> Dict[str, str]:
    return {"field": field, "problem": text}


def _is_time(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return datetime.fromisoformat(value).tzinfo is not None
    except ValueError:
        return False


def _is_day(value: Any) -> bool:
    if not isinstance(value, str) or len(value) != 10:
        return False
    try:
        date.fromisoformat(value)
        return True
    except ValueError:
        return False


def problems_with(kind: str, body: Dict[str, Any]) -> List[Dict[str, str]]:
    """
    Everything beyond the five fields. Unknown fields are never a problem
    (SRS-CHB-11): they are stored with the rest of the body.
    """
    found: List[Dict[str, str]] = []
    if kind == "patient_information":
        demographics = body.get("demographics")
        if not isinstance(demographics, dict):
            found.append(_problem("demographics", "required, and must be an object"))
        elif "sex" in demographics and demographics["sex"] not in ("female", "male"):
            found.append(_problem("demographics.sex", "must be 'female' or 'male'"))
        if "paramedic" in body and body["paramedic"] is not None \
                and not isinstance(body["paramedic"], dict):
            found.append(_problem("paramedic", "must be an object"))
        if "previous_visit" not in body:
            found.append(_problem("previous_visit", "required; send null for a first visit"))
        elif body["previous_visit"] is not None and not isinstance(body["previous_visit"], dict):
            found.append(_problem("previous_visit", "must be an object or null"))
        elif isinstance(body["previous_visit"], dict):
            found.extend(_previous_visit_problems(body, body["previous_visit"]))
    else:
        if not _is_time(body.get("issued_at")):
            found.append(_problem("issued_at", "required: a date and time with its time zone"))
        for name in ("diagnoses", "investigations"):
            if not isinstance(body.get(name), list):
                found.append(_problem(name, "required: a list, empty if none"))
        items = body.get("items")
        if not isinstance(items, list):
            found.append(_problem("items", "required: a list, one entry per medicine"))
        else:
            for n, item in enumerate(items):
                if not isinstance(item, dict) or not isinstance(item.get("drug"), str) \
                        or not item["drug"].strip():
                    found.append(_problem(f"items[{n}].drug", "each item needs a drug"))
        if body.get("follow_up") not in (None, "") and not _is_day(body.get("follow_up")):
            found.append(_problem("follow_up", "must be YYYY-MM-DD"))
    return found


_PREVIOUS_IDENTITY = ("doctor_id", "hospital_id", "start_time")


def _previous_visit_problems(body: Dict[str, Any], p: Dict[str, Any]) -> List[Dict[str, str]]:
    """
    previous_visit names the earlier visit by its own five fields, or - the
    older shape, still accepted - by its date alone. Half a name is refused:
    a doctor without a time could file the prescription against the wrong
    visit, which is worse than not filing it.
    """
    found: List[Dict[str, str]] = []
    if not _is_day(p.get("date")):
        found.append(_problem("previous_visit.date", "required: YYYY-MM-DD"))
    given = [name for name in _PREVIOUS_IDENTITY if p.get(name) not in (None, "")]
    if p.get("patient_id") not in (None, "") and p.get("patient_id") != body.get("patient_id"):
        found.append(_problem("previous_visit.patient_id",
                              "must be the same patient as this visit"))
    if given and len(given) < len(_PREVIOUS_IDENTITY):
        for name in _PREVIOUS_IDENTITY:
            if name not in given:
                found.append(_problem(f"previous_visit.{name}",
                                      "send all of doctor_id, hospital_id and start_time, "
                                      "or none of them"))
    elif given and not found:
        try:
            earlier = conf.parse_visit({"patient_id": body.get("patient_id"),
                                        **{n: p.get(n) for n in _PREVIOUS_IDENTITY},
                                        "date": p.get("date")}, strict_date=True)
        except FieldError as exc:
            found.append(_problem(f"previous_visit.{exc.field}", str(exc)))
        else:
            now = (body.get("date"), body.get("start_time"))
            if (earlier.visit_date, earlier.start_time) == now:
                found.append(_problem("previous_visit", "names this visit, not an earlier one"))
            elif isinstance(body.get("date"), str) and earlier.visit_date > body["date"]:
                found.append(_problem("previous_visit.date", "is after this visit"))
    if not isinstance(p.get("prescription"), dict):
        found.append(_problem("previous_visit.prescription", "required, and must be an object"))
    return found


# ------------------------------------------------------------------ endpoints

@router.post("/patient-information")
async def patient_information(request: Request):
    """API 2. Sent at the moment the patient is opened (SRS-CNF-01)."""
    return await receive(request, "patient_information")


@router.post("/prescription")
async def prescription(request: Request):
    """API 3, part two. Sent when the prescription is built (SRS-CRI-03)."""
    return await receive(request, "prescription")


async def receive(request: Request, kind: str) -> JSONResponse:
    try:
        principal = await _authenticate(request, cmed_auth.WRITE)
        body, raw = await _read_json(request)
    except _Refusal as exc:
        return _reply(exc.status, exc.code, str(exc), **exc.extra)
    status, code, message, extra = await process(kind, body, raw, principal)
    headers = cmed_auth.legacy_headers() if principal.via == "legacy_key" else None
    return _reply(status, code, message, headers=headers, **extra)


async def process(kind: str, body: Dict[str, Any], raw: str,
                  principal: cmed_auth.Principal) -> tuple:
    """
    One API 2 or API 3 body, whichever form it came in. Returns
    (status, code, message or None, extra reply fields).
    """
    try:
        try:
            visit = conf.parse_visit(body, strict_date=True)
        except FieldError as exc:
            raise _Refusal(400, exc.code, str(exc), fields=[exc.field])
        await check_hospital(principal, visit, kind)
    except _Refusal as exc:
        return exc.status, exc.code, str(exc), exc.extra
    label = principal.actor

    repo = api_v2._repo()

    store = api_v2._clinical()

    problems = problems_with(kind, body)
    if problems:
        qid = await store.quarantine(kind=kind, raw=raw, problems=problems)
        await repo.raise_alert(alert_type="clinical_schema_invalid", severity="warning",
                               detail={"kind": kind, "quarantine_id": qid,
                                       "fields": [p["field"] for p in problems]})
        return 422, "SCHEMA_INVALID", None, {"fields": problems, "quarantine_id": str(qid)}

    # The patient refused to be recorded: keep nothing from this visit, and
    # answer as usual so CMED needs no special handling (SRS-CNS-04).
    if await repo.refused_visit(visit.digest()):
        logger.info("Dropped %s for a refused consultation", kind)
        return 202, "ACCEPTED", None, {"record_id": None, "received_at": _now_iso()}

    hospital_id = await repo.hospital_for_cmed_id(visit.cmed_hospital_id)
    if hospital_id is None and await repo.hospital_exists(visit.cmed_hospital_id):
        # CMED already uses our code for this clinic (the dummy, or a site set up
        # that way). Anything else is unmapped: stored, but it confirms nothing.
        hospital_id = visit.cmed_hospital_id
    if hospital_id is None:
        await repo.raise_alert(alert_type="unmapped_clinic", severity="warning",
                               detail={"cmed_hospital_id": visit.cmed_hospital_id,
                                       "kind": kind})

    # The same message written two ways (10:14:32+06:00, 04:14:32.000Z) is one
    # message: the fingerprint is taken with the five fields in their one form.
    digest = integrity.sha256_bytes(integrity.canonical_json(
        {**body, "start_time": visit.start_time, "date": visit.visit_date}))
    record_id, duplicate, version = await store.receive(
        kind=kind, visit=visit, hospital_id=hospital_id, body=body, body_sha256=digest)
    if duplicate:
        extra = {"version": version} if kind == "prescription" else {}
        if kind == "patient_information":
            extra["previous_visit"] = await _previous_outcome(store, body, record_id,
                                                              repeat=True)
        return 200, "ALREADY_RECEIVED", None, {
            "record_id": str(record_id) if record_id is not None else None, **extra}

    # Into the tables now. A load that fails keeps the body and is tried again
    # by the sweep; it never fails the request (SRS-CRI-04).
    await store.load(record_id)

    if kind == "patient_information" and hospital_id is not None:
        notice_id = await repo.record_notice(visit, hospital_id=hospital_id,
                                             clinical_record_id=record_id)
        await confirm_waiting(visit, hospital_id, notice_id)
    if hospital_id is not None:
        # An unmapped clinic's records attach to nothing: which recording they
        # belong to cannot be known without knowing whose clinic they came from.
        await attach_to_recording(visit, hospital_id)

    logger.info("Stored %s %s from %s", kind, record_id, label)
    extra = {"version": version} if kind == "prescription" else {}
    if kind == "patient_information":
        extra["previous_visit"] = await _previous_outcome(store, body, record_id)
    return 202, "ACCEPTED", None, {"record_id": str(record_id), "received_at": _now_iso(),
                                   **extra}


async def _previous_outcome(store, body: Dict[str, Any], record_id, *,
                            repeat: bool = False) -> Dict[str, Any]:
    """
    What became of previous_visit, so CMED knows whether that history is now
    held: ALREADY_STORED means it was here already and nothing was added.
    Never fails the request - an answer that cannot be worked out is PENDING.
    """
    if not isinstance(body.get("previous_visit"), dict):
        return {"status": "NOT_SENT"}
    try:
        outcome = await store.previous_visit_outcome(int(record_id))
    except Exception as exc:
        logger.warning("Could not report previous_visit for record %s: %s", record_id, exc)
        return {"status": "PENDING"}
    if repeat and outcome.get("status") in ("STORED", "UPDATED"):
        # This exact API 2 stored it the first time; this time nothing was added.
        outcome = dict(outcome, status="ALREADY_STORED")
    return outcome


@router.get("/patient/{patient_id}/visits")
async def patient_visits(patient_id: str, request: Request):
    """
    The visits held for one patient, with a fingerprint of each one's newest
    prescription. CMED asks before sending previous_visit, so its decision
    rests on what is held here rather than on its own memory of what it sent.
    """
    try:
        principal = await _authenticate(request, cmed_auth.READ)
        try:
            patient_id = integrity.safe_identifier(patient_id, field="patient_id")
        except ValueError:
            raise _Refusal(400, "INVALID_IDENTIFIER", fields=["patient_id"])
        limit_text = request.query_params.get("limit", "50")
        if not (limit_text.isascii() and limit_text.isdigit()) \
                or not 1 <= int(limit_text) <= 200:
            raise _Refusal(400, "MISSING_FIELD", "'limit' must be 1 to 200.",
                           fields=["limit"])
        before_text = request.query_params.get("before")
        if before_text is not None and not _is_day(before_text):
            raise _Refusal(400, "MISSING_FIELD", "'before' must be YYYY-MM-DD.",
                           fields=["before"])
        cursor = request.query_params.get("cursor")
        after_key = _read_cursor(cursor) if cursor else None
    except _Refusal as exc:
        return _reply(exc.status, exc.code, str(exc), **exc.extra)

    visits, cursor = await list_visits(principal, patient_id, int(limit_text),
                                       date.fromisoformat(before_text) if before_text else None,
                                       after_key)
    return _reply(200, "OK", patient_id=patient_id, visits=visits, next_cursor=cursor)


async def list_visits(principal: cmed_auth.Principal, patient_id: str, limit: int,
                      before: Optional[date], after_key) -> tuple:
    """
    Only visits at the hospitals this credential speaks for: a credential
    for one hospital never learns where else a patient has been seen.
    """
    visits = await api_v2._clinical().visits_for(patient_id, actor=principal.actor,
                                                 limit=limit, before=before,
                                                 after_key=after_key,
                                                 hospitals=sorted(principal.hospitals))
    keys = [v.pop("_key") for v in visits]
    more = len(visits) == limit
    return visits, (_write_cursor(keys[-1]) if more else None)


def _write_cursor(key) -> str:
    """Where a page ended, opaque to CMED: pass it back as `cursor` unchanged."""
    day, start_time, encounter_id = key
    raw = json.dumps([day.isoformat(), start_time, str(encounter_id)]).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _read_cursor(cursor: str):
    try:
        day, start_time, encounter_id = json.loads(
            base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        return date.fromisoformat(day), str(start_time), uuid.UUID(encounter_id)
    except Exception:
        raise _Refusal(400, "MISSING_FIELD", "'cursor' is not one this server gave.",
                       fields=["cursor"])


async def attach_to_recording(visit: Visit, hospital_id: Optional[str] = None
                              ) -> Optional[str]:
    """
    API 2 and API 3 may arrive hours or days after the recording closed, or
    after it was archived; they enrich it, they never decide whether it is
    kept. A late record is tied to its recording by the shared file name, and
    an archived recording's JSON is written again. Best effort: this never
    fails the request, and the sweep and the next record try again.
    """
    repo = api_v2._repo()
    try:
        found = await repo.session_for_visit(visit, hospital_id)
        if found is None:
            return None
        # A prescription arriving late is evidence of who was consulted: the
        # recording's parts are decided again (and linked) before anything else.
        parts = await api_v2.relabel_session(found["session_id"])
        session = await repo.get_session(found["session_id"]) or {}
        clinical_store = api_v2._clinical()
        if parts is not None and len(parts) > 1:
            pass                                    # each part linked by the relabel
        elif session.get("file_stem") and clinical_store is not None:
            await clinical_store.link_session(visit, session_id=found["session_id"],
                                              file_stem=session["file_stem"],
                                              hospital_id=session.get("hospital_id"))
        return await mark_json_for_rewrite(visit, hospital_id)
    except Exception as exc:
        logger.warning("Could not attach a late clinical record to its recording: %s", exc)
        return None


async def confirm_waiting(visit: Visit, hospital_id: str, notice_id: int) -> Optional[str]:
    """
    A late API 2: find the grant it belongs to and confirm that recording
    (SRS-CNF-07, SRS-CNF-09). Returns the session confirmed, if any.
    """
    repo = api_v2._repo()
    since = datetime.now(timezone.utc) - conf.UNCONFIRMED_LIFETIME
    if await api_v2._visit_contested(visit, hospital_id):
        return None                         # two PCs claim it: an administrator decides
    grant = await repo.find_waiting_authorisation(visit, hospital_id=hospital_id, since=since)
    if grant is None:
        return None                         # a grant may still ask for it (5 minutes)
    if not await repo.claim_notice(notice_id, grant["jti"]):
        return None

    # Read the grant again now the notice is claimed. The recording may have
    # opened in the moment between finding the grant and claiming it, and the
    # copy read earlier would still say there is no session - which is how a
    # recording used to end up pending for ever, with each side waiting for
    # the other (found by the 14-room simulation).
    fresh = await repo.get_authorisation(grant["jti"])
    session_id = (fresh or {}).get("session_id") or grant.get("session_id")
    if not session_id:
        # Still nothing: the session opens next, sees the claimed notice, and
        # opens confirmed. If it crosses even that, the sweep reconciles it.
        return None
    row = await repo.session_confirmation(session_id)
    if row and row.get("grant_jti") not in (None, grant["jti"]):
        # The recording was moved to another patient (a wrong profile picked
        # at the start). This API 2 is the wrong patient's; it confirms nothing.
        return None
    if row and row["confirmation"] in conf.WAITING:
        await repo.set_session_confirmation(session_id, "confirmed")
        await repo.audit(event_type="session.confirmed", actor_type="service",
                         actor_id="channel-b", session_id=session_id,
                         detail={"late": True, "was": row["confirmation"]})
        logger.info("Session %s confirmed by a late API 2", session_id)
        await api_v2.confirm_continuations(session_id)
        return session_id
    return None


async def mark_json_for_rewrite(visit: Visit, hospital_id: Optional[str] = None
                                ) -> Optional[str]:
    """
    A prescription for a consultation that is already archived: the JSON file
    beside the audio is now out of date, so it is written again and uploaded to
    the copy bucket as a new object (SRS-ARC-13). The archive worker picks this
    up on its next pass.
    """
    repo = api_v2._repo()
    session = await repo.session_for_visit(visit, hospital_id)
    if session is None or session.get("archived_at") is None:
        return None                         # not archived yet: the JSON is written
    if await repo.mark_json_stale(session["session_id"]):    # with the audio anyway
        logger.info("Clinical record arrived after archiving; %s needs its JSON again",
                    session["session_id"])
        return session["session_id"]
    return None


def _now_iso() -> str:
    return integrity.iso_utc(datetime.now(timezone.utc))


__all__ = ["router", "receive", "process", "list_visits", "check_hospital",
           "confirm_waiting", "mark_json_for_rewrite",
           "attach_to_recording", "patient_visits",
           "problems_with", "key_digest", "MAX_BODY_BYTES"]
