"""
Channel B, interface version 4 - CMED's server to this server in plain JSON,
keyed by CMED's visit_id (CMED_CONNECTION_GUIDE_v4.pdf).

    POST /api/v4/clinical/patient-information    API 2, when a patient is opened
    POST /api/v4/clinical/prescription           API 3, when the prescription is built
    GET  /api/v4/clinical/visit/{visit_id}/status what has reached us for one visit

A message is filed in two places. First in AIMS LAB's own layout (patient,
patient_visit, visit_messages - clinical/004_patient_visit.sql), exactly as
sent, with each section as rows (visit_drug_order, visit_diagnosis, ... -
clinical/005_visit_sections.sql). Then, when its six fields can be read, it is turned into the body
clinical.process takes and goes through that one pipeline, as a FHIR message
does: that is what confirms and links the recording, writes the archive JSON
and fills the tables the dashboard reads. So interface 4 changes nothing for
the recordings.

Only the three link keys - visit_id, patient_id, hospital_id - can stop a
message: without them it cannot be filed, and it is kept for review. Every
other field may be missing, empty or in any format; what was stored as null is
named in the reply's warnings.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Request

import api_v2
import clinical
import clinical_model as cm
import cmed_auth
import confirmation as conf
import integrity
from confirmation import FieldError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v4/clinical", tags=["clinical-v4"])

# A visit_id is part of the audio file's name, whose fields are joined by "_":
# so no underscore in it.
VISIT_ID = re.compile(r"^[A-Za-z0-9-]{1,64}$")
SECTIONS = ("chief_complaints", "observations", "diagnosis", "lab_orders",
            "radiology_orders", "drug_orders", "advice")

clinical.MESSAGES.update({
    "MISSING_KEY": "visit_id, patient_id and hospital_id are required. Kept for review; "
                   "fix and send again.",
    "VISIT_CONFLICT": "This visit_id already belongs to another patient. Kept for review.",
    "NOT_FOUND": "Nothing has been received for this visit.",
})


class _Stop(Exception):
    """A message that cannot be filed: kept in quarantine, answered with `code`."""

    def __init__(self, status: int, code: str, fields: List[str], problems: List[Dict]):
        super().__init__(code)
        self.status, self.code, self.fields, self.problems = status, code, fields, problems


# ------------------------------------------------------------------ reading a message

def _link_keys(body: Dict[str, Any]) -> Tuple[str, str, str]:
    problems, fields = [], []
    values = {}
    for name in ("visit_id", "patient_id", "hospital_id"):
        raw = body.get(name)
        text = raw.strip() if isinstance(raw, str) else (str(raw) if isinstance(raw, int)
                                                         and not isinstance(raw, bool) else "")
        if not text:
            problems.append({"field": name, "problem": "missing"})
            fields.append(name)
            continue
        ok = bool(VISIT_ID.match(text)) if name == "visit_id" else True
        if ok and name != "visit_id":
            try:
                text = integrity.safe_identifier(text, field=name)
            except ValueError:
                ok = False
        if not ok:
            problems.append({"field": name, "problem": "not allowed: " + (
                "letters, digits and '-', up to 64" if name == "visit_id"
                else "letters, digits, '_' and '-', up to 64")})
            fields.append(name)
        values[name] = text
    if fields:
        code = "MISSING_KEY" if any(p["problem"] == "missing" for p in problems) \
            else "INVALID_IDENTIFIER"
        raise _Stop(422, code, fields, problems)
    return values["visit_id"], values["patient_id"], values["hospital_id"]


def _section(body: Dict[str, Any], name: str, warnings: List[Dict]) -> List[Any]:
    value = body.get(name)
    if value is None or value == "":
        return []
    if isinstance(value, list):
        return value
    warnings.append({"field": name, "sent": value, "note": "not a list: stored as a list of one"})
    return [value]


def _note(warnings: List[Dict], field: str, sent: Any, note: str) -> None:
    warnings.append({"field": field, "sent": sent, "note": note})


def read(kind: str, body: Dict[str, Any]) -> Dict[str, Any]:
    """
    One message as the columns it fills, and the warnings for what was stored
    as null or kept as text. Never raises for a value: only the link keys can
    stop a message (checked before this).
    """
    w: List[Dict[str, Any]] = []
    out: Dict[str, Any] = {}

    doctor = body.get("doctor_id")
    out["practitioner_id"] = None
    if doctor in (None, ""):
        _note(w, "doctor_id", doctor, "missing: stored as null")
    else:
        try:
            out["practitioner_id"] = integrity.safe_identifier(str(doctor), field="doctor_id")
        except ValueError:
            _note(w, "doctor_id", doctor, "not a CMED identifier: stored as null")

    start = cm.moment(body.get("start_time"))
    out["start_time"] = start
    if start is None:
        _note(w, "start_time", body.get("start_time"),
              "missing or not a time with its offset: stored as null")
    out["visit_date"] = cm.day(body.get("date"))
    if out["visit_date"] is None:
        _note(w, "date", body.get("date"), "missing or not a date: stored as null")

    if kind == "patient_information":
        d = body.get("demographics") if isinstance(body.get("demographics"), dict) else {}
        if body.get("demographics") not in (None, {}) and not d:
            _note(w, "demographics", body.get("demographics"), "not an object: ignored")
        sex = d.get("sex")
        out["sex"] = None if sex in (None, "") else cm.sex_of(sex)
        out["date_of_birth"] = cm.day(d.get("date_of_birth"))
        if d.get("date_of_birth") not in (None, "") and out["date_of_birth"] is None:
            _note(w, "demographics.date_of_birth", d.get("date_of_birth"),
                  "not a date: stored as null, the text kept")
        age = d.get("age") if d.get("age") not in (None, "") else d.get("age_years")
        out["age"] = cm.text(age, 40)
        out["age_years"] = cm.age_years(age)[0]
        out["blood_group"] = cm.text(d.get("blood_group"), 16)
        out["vitals"] = _section(body, "observations", w)
    else:
        for name in SECTIONS:
            out[name] = _section(body, name, w)
        issued = body.get("issued_at")
        out["issued_at"] = cm.moment(issued)
        if issued not in (None, "") and out["issued_at"] is None:
            _note(w, "issued_at", issued, "not a time: stored as null")
        follow = body.get("follow_up")
        out["follow_up"] = cm.day(follow)
        out["follow_up_text"] = None if follow in (None, "") or out["follow_up"] \
            else cm.text(follow, 200)
        if out["follow_up_text"]:
            _note(w, "follow_up", follow, "not a date: kept as text")
    out["warnings"] = w
    return out


# ------------------------------------------------------------------ the old pipeline

def _value_text(item: Dict[str, Any]) -> Optional[str]:
    value, unit = item.get("value"), item.get("unit")
    if value in (None, ""):
        return None
    text = cm.plain(value)
    if unit not in (None, "") and str(unit).lower() not in ("ft/in",) \
            and str(unit).lower() not in text.lower():
        text = f"{text} {cm.plain(unit)}"
    return text


_PARAMEDIC = {"blood pressure": "blood_pressure", "bp": "blood_pressure",
              "pulse": "pulse_bpm", "pulse rate": "pulse_bpm", "heart rate": "pulse_bpm",
              "temperature": "temperature_c", "temp": "temperature_c",
              "weight": "weight_kg", "height": "height_cm",
              "spo2": "spo2_percent", "oxygen saturation": "spo2_percent"}


def _paramedic(observations: List[Any]) -> Optional[Dict[str, Any]]:
    """API 2's vitals as the paramedic object of the older body."""
    out: Dict[str, Any] = {}
    for group in observations:
        if not isinstance(group, dict):
            continue
        if group.get("recorded_at") and "recorded_at" not in out:
            out["recorded_at"] = group["recorded_at"]
        for item in group.get("items") or []:
            if not isinstance(item, dict) or not cm.text(item.get("name")):
                continue
            name = cm.plain(item["name"]).strip().lower()
            key = _PARAMEDIC.get(name, name.replace(" ", "_")[:60])
            if key == "blood_pressure":
                value = cm.plain(item.get("value")) if item.get("value") not in (None, "") else None
            else:
                value = _value_text(item)
            if value is not None and key not in out:
                out[key] = value
    return out or None


# observations category -> (section of the older body, label for the item)
_FINDING = {"personal_history": ("history", "personal"), "family_history": ("history", "family"),
            "comorbidity": ("history", "comorbidity"), "drug_history": ("history", "drug"),
            "allergy": ("allergy", None), "on_examination": ("on_examination", None),
            "systemic_examination": ("systemic_examination", None),
            "obs_gyne": ("gynae_obstetric", None), "anc": ("gynae_obstetric", "ANC"),
            "pnc": ("gynae_obstetric", "PNC")}


def _findings(body: Dict[str, Any]) -> List[Dict[str, Any]]:
    out = []
    for c in body.get("chief_complaints") or []:
        if isinstance(c, dict) and cm.text(c.get("complaint")):
            detail = {k: c[k] for k in ("duration", "severity", "note") if c.get(k) not in (None, "")}
            out.append({"section": "chief_complaint", "item": cm.text(c["complaint"], 500),
                        "value": None, "detail": detail})
        elif isinstance(c, str) and c.strip():
            out.append({"section": "chief_complaint", "item": c.strip()[:500], "value": None,
                        "detail": {}})
    for group in body.get("observations") or []:
        if not isinstance(group, dict):
            continue
        category = cm.plain(group.get("category") or "").strip().lower()
        section, label = _FINDING.get(category, ("history", category or "other"))
        for item in group.get("items") or []:
            if not isinstance(item, dict) or not cm.text(item.get("name")):
                continue
            name = cm.text(item["name"], 200)
            out.append({"section": section, "item": f"{label}: {name}" if label else name,
                        "value": _value_text(item), "detail": {}})
        if cm.text(group.get("note")):
            out.append({"section": section, "item": label or category or "note",
                        "value": cm.text(group["note"], 2000), "detail": {"note": True}})
    return out


def to_older_body(kind: str, body: Dict[str, Any]) -> Dict[str, Any]:
    """The body clinical.process takes, for the same message."""
    five = {k: body.get(k) for k in ("patient_id", "doctor_id", "hospital_id",
                                     "start_time", "date")}
    if kind == "patient_information":
        d = body.get("demographics") if isinstance(body.get("demographics"), dict) else {}
        demographics = {k: d[k] for k in ("sex", "date_of_birth") if d.get(k) not in (None, "")}
        age = d.get("age") if d.get("age") not in (None, "") else d.get("age_years")
        if age not in (None, ""):
            demographics["age_years"] = age
        for k in cm.NOT_KEPT:
            if d.get(k) not in (None, ""):
                demographics[k] = d[k]          # removed by clinical.process, and warned
        return {**five, "demographics": demographics,
                "paramedic": _paramedic(body.get("observations") or []),
                "previous_visit": None}
    items = []
    for d in body.get("drug_orders") or []:
        if not isinstance(d, dict):
            continue
        name = cm.text(d.get("medication_name")) or cm.text(d.get("generic_name"))
        if not name:
            continue
        strength = cm.text(d.get("strength"))
        drug = " ".join(p for p in (cm.text(d.get("form")), name,
                                    f"({strength})" if strength else None) if p)
        items.append({"drug": drug, "dose": d.get("dose"), "frequency": d.get("frequency"),
                      "duration": d.get("duration"),
                      "instructions": "; ".join(p for p in (cm.text(d.get("timing")),
                                                            cm.text(d.get("instruction"))) if p)
                      or None})
    diagnoses = [cm.text(x.get("display")) or cm.text(x.get("code"))
                 for x in body.get("diagnosis") or [] if isinstance(x, dict)]
    tests = [cm.text(x.get("test_name")) or cm.text(x.get("test_code"))
             for x in body.get("lab_orders") or [] if isinstance(x, dict)]
    tests += [cm.text(x.get("procedure_name")) or cm.text(x.get("procedure_code"))
              for x in body.get("radiology_orders") or [] if isinstance(x, dict)]
    advice = [cm.text(x.get("instruction")) if isinstance(x, dict) else cm.text(x)
              for x in body.get("advice") or []]
    return {**five, "issued_at": body.get("issued_at"), "items": items,
            "diagnoses": [x for x in diagnoses if x], "investigations": [x for x in tests if x],
            "advice": "\n".join(a for a in advice if a) or None,
            "follow_up": body.get("follow_up"), "notes": None, "findings": _findings(body)}


def _visit_of(body: Dict[str, Any]):
    try:
        return conf.parse_visit({k: body.get(k) for k in ("patient_id", "doctor_id",
                                                           "hospital_id", "start_time", "date")},
                                strict_date=True)
    except FieldError:
        return None


# ------------------------------------------------------------------ filing

def _content(kind: str, body: Dict[str, Any]) -> bytes:
    """What a correction is measured on: the message without the moment it was sent."""
    if kind == "prescription":
        body = {k: v for k, v in body.items() if k != "issued_at"}
    return integrity.sha256_bytes(integrity.canonical_json(body))


async def _quarantine(kind: str, raw: str, problems: List[Dict], client: Optional[str]) -> None:
    try:
        async with api_v2._clinical()._pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO visit_quarantine (kind, raw, problems, client) VALUES ($1,$2,$3,$4)",
                kind, raw, json.dumps(problems, ensure_ascii=False), client)
    except Exception as exc:
        logger.error("Could not quarantine an interface-4 %s: %s", kind, exc)


async def file_message(kind: str, body: Dict[str, Any], hospital_id: str, visit_id: str,
                       patient_id: str, parsed: Dict[str, Any], client: str
                       ) -> Tuple[int, bool, int]:
    """
    Store one message and update its visit. Returns (visit row id, already
    received, prescription version). Raises _Stop when the visit_id belongs
    to another patient.
    """
    body_sha = integrity.sha256_bytes(integrity.canonical_json(body))
    content_sha = _content(kind, body)
    async with api_v2._clinical()._pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext($1))",
                               f"v4|{hospital_id}|{visit_id}")
            row = await conn.fetchrow(
                "SELECT id, hospital_patient_id, prescription_version, issued_at "
                "FROM patient_visit WHERE hospital_id = $1 AND visit_id = $2",
                hospital_id, visit_id)
            if row is not None and row["hospital_patient_id"] != patient_id:
                raise _Stop(409, "VISIT_CONFLICT", ["visit_id", "patient_id"],
                            [{"field": "visit_id", "problem": "already belongs to another "
                                                              "patient"}])
            # The same bytes again are always a resend. The same prescription
            # with a new issued_at is a resend only of the latest version: a
            # doctor who corrects A to B and then back to A has made a third
            # version, and it is the one the visit must show.
            latest_only = ("AND version = (SELECT max(version) FROM visit_messages "
                           "WHERE kind = $1 AND hospital_id = $2 AND visit_id = $3)"
                           if kind == "prescription" else "")
            seen = await conn.fetchrow(
                "SELECT version FROM visit_messages WHERE kind = $1 AND hospital_id = $2 "
                "AND visit_id = $3 AND (body_sha256 = $4 OR (content_sha256 = $5 "
                f"{latest_only})) ORDER BY version DESC LIMIT 1",
                kind, hospital_id, visit_id, body_sha, content_sha)
            if seen is not None and row is not None:
                return int(row["id"]), True, int(seen["version"])

            version = 1
            if kind == "prescription" and row is not None:
                version = int(row["prescription_version"]) + 1
            await conn.execute("""
                INSERT INTO patient (hospital_id, hospital_patient_id, sex, date_of_birth,
                                     blood_group)
                VALUES ($1, $2, $3, $4, $5)
                ON CONFLICT (hospital_id, hospital_patient_id) DO UPDATE SET
                    sex = COALESCE(EXCLUDED.sex, patient.sex),
                    date_of_birth = COALESCE(EXCLUDED.date_of_birth, patient.date_of_birth),
                    blood_group = COALESCE(EXCLUDED.blood_group, patient.blood_group),
                    updated_at = now()
            """, hospital_id, patient_id, parsed.get("sex"), parsed.get("date_of_birth"),
                parsed.get("blood_group"))
            visit_row = await conn.fetchval("""
                INSERT INTO patient_visit (visit_id, hospital_id, hospital_patient_id,
                                           practitioner_id, start_time, visit_date)
                VALUES ($1, $2, $3, $4, $5, $6)
                ON CONFLICT ON CONSTRAINT patient_visit_once DO UPDATE SET
                    practitioner_id = COALESCE(EXCLUDED.practitioner_id,
                                               patient_visit.practitioner_id),
                    start_time = COALESCE(EXCLUDED.start_time, patient_visit.start_time),
                    visit_date = COALESCE(EXCLUDED.visit_date, patient_visit.visit_date),
                    updated_at = now()
                RETURNING id
            """, visit_id, hospital_id, patient_id, parsed["practitioner_id"],
                parsed["start_time"], parsed["visit_date"])

            newest = False
            if kind == "patient_information":
                await conn.execute("""
                    UPDATE patient_visit SET age = $2, age_years = $3, vitals = $4,
                           patient_information_at = now(), updated_at = now()
                     WHERE id = $1
                """, visit_row, parsed["age"], parsed["age_years"],
                    json.dumps(parsed["vitals"], ensure_ascii=False))
            else:
                newest = row is None or row["issued_at"] is None or parsed["issued_at"] is None \
                    or parsed["issued_at"] >= row["issued_at"]
                if newest:
                    # An older prescription replayed after a newer one is kept as
                    # its version, and never rolls the visit back.
                    await conn.execute("""
                        UPDATE patient_visit SET chief_complaints = $2, observations = $3,
                               diagnosis = $4, lab_orders = $5, radiology_orders = $6,
                               drug_orders = $7, advice = $8, follow_up = $9,
                               follow_up_text = $10, issued_at = $11
                         WHERE id = $1
                    """, visit_row, *[json.dumps(parsed[s], ensure_ascii=False)
                                      for s in SECTIONS],
                        parsed["follow_up"], parsed["follow_up_text"], parsed["issued_at"])
                else:
                    parsed["warnings"].append({"field": "issued_at", "sent": body.get("issued_at"),
                                               "note": "older than the prescription held: kept "
                                                       "as a version, not applied"})
                await conn.execute("""
                    UPDATE patient_visit SET prescription_version = $2,
                           prescription_at = now(), updated_at = now() WHERE id = $1
                """, visit_row, version)
            message = await conn.fetchval("""
                INSERT INTO visit_messages (kind, hospital_id, visit_id, version, body,
                                            body_sha256, content_sha256, warnings, client)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
                ON CONFLICT ON CONSTRAINT visit_message_once DO NOTHING
                RETURNING id
            """, kind, hospital_id, visit_id, version, json.dumps(body, ensure_ascii=False),
                body_sha, content_sha, json.dumps(parsed["warnings"], ensure_ascii=False,
                                                  default=str), client)
            if message is not None:
                # Its sections as rows (clinical/005_visit_sections.sql), and the
                # version the visit now shows.
                await conn.execute("SELECT load_visit_sections($1)", message)
                if kind == "patient_information":
                    await conn.execute("UPDATE patient_visit SET patient_information_message_id"
                                       " = $2 WHERE id = $1", visit_row, message)
                elif newest:
                    await conn.execute("UPDATE patient_visit SET prescription_message_id = $2 "
                                       "WHERE id = $1", visit_row, message)
            return int(visit_row), False, version


async def _recording_of(visit, hospital_id: Optional[str]) -> Optional[Dict[str, Any]]:
    """The recording this visit's six fields name, if one reached us."""
    if visit is None:
        return None
    repo = api_v2._repo()
    found = await repo.session_for_visit(visit, hospital_id)
    if found is None:
        return None
    async with repo._acquire() as conn:
        row = await conn.fetchrow("""
            SELECT session_id, opened_at, closed_at, close_reason, segment_count,
                   total_duration_seconds, confirmation
              FROM sessions WHERE session_id = $1
        """, found["session_id"])
    return dict(row) if row else None


async def receive(request: Request, kind: str):
    try:
        principal = await clinical._authenticate(request, cmed_auth.WRITE)
        body, raw = await clinical._read_json(request)
    except clinical._Refusal as exc:
        return clinical._reply(exc.status, exc.code, str(exc), **exc.extra)

    try:
        visit_id, patient_id, hospital_id = _link_keys(body)
    except _Stop as stop:
        await _quarantine(kind, raw, stop.problems, principal.actor)
        return clinical._reply(stop.status, stop.code, fields=stop.fields)
    try:
        await clinical.check_hospital(principal, SimpleNamespace(cmed_hospital_id=hospital_id),
                                      kind)
    except clinical._Refusal as exc:
        return clinical._reply(exc.status, exc.code, str(exc), **exc.extra)

    repo = api_v2._repo()
    visit = _visit_of(body)
    # The patient refused to be recorded: keep nothing from this visit, and
    # answer as usual so CMED needs no special handling (SRS-CNS-04).
    if visit is not None and await repo.refused_visit(visit.digest()):
        return clinical._reply(202, "ACCEPTED", visit_id=visit_id, record_id=None,
                               received_at=clinical._now_iso(), recording="waiting",
                               warnings=[])

    if kind == "patient_information":
        body, dropped = cm.minimise(body)
    else:
        dropped = []
    parsed = read(kind, body)
    if dropped:
        parsed["warnings"].append({"field": "demographics", "sent": dropped,
                                   "note": "not needed and not stored"})
    try:
        record_id, duplicate, version = await file_message(
            kind, body, hospital_id, visit_id, patient_id, parsed, principal.actor)
    except _Stop as stop:
        await _quarantine(kind, raw, stop.problems, principal.actor)
        return clinical._reply(stop.status, stop.code, fields=stop.fields)

    # The older pipeline: confirms and links the recording, writes its archive
    # JSON and fills the dashboard's tables. A message it cannot take (its six
    # fields are incomplete) is still filed above.
    internal_hospital = None
    if visit is not None:
        try:
            await clinical.process(kind, to_older_body(kind, body), raw, principal)
            internal_hospital = await repo.hospital_for_cmed_id(visit.cmed_hospital_id) \
                or visit.cmed_hospital_id
        except Exception as exc:
            logger.warning("Interface-4 %s for %s could not go through the recording "
                           "pipeline: %s", kind, visit_id, exc)
    else:
        parsed["warnings"].append({"field": "start_time", "sent": body.get("start_time"),
                                   "note": "this visit can be joined to its recording only "
                                           "once doctor_id, start_time and date are sent"})

    recording = None
    try:
        recording = await _recording_of(visit, internal_hospital)
        if recording is not None:
            async with api_v2._clinical()._pool.acquire() as conn:
                await conn.execute("UPDATE patient_visit SET aims_id = $2 WHERE id = $1 "
                                   "AND aims_id IS DISTINCT FROM $2", record_id,
                                   recording["session_id"])
            if kind == "prescription" and recording.get("closed_at") is None:
                # The recorder ends this recording about two minutes from now,
                # unless the next patient is opened first (the heartbeat tells it).
                await api_v2._repo().note_prescription(recording["session_id"])
    except Exception as exc:
        logger.warning("Could not look up the recording of %s: %s", visit_id, exc)

    extra = {"visit_id": visit_id, "record_id": str(record_id)}
    if kind == "prescription":
        extra["version"] = version
    extra.update(received_at=clinical._now_iso(),
                 recording="linked" if recording and recording.get("segment_count")
                 else "waiting",
                 warnings=parsed["warnings"])
    extra = json.loads(json.dumps(extra, ensure_ascii=False, default=str))
    if duplicate:
        return clinical._reply(200, "ALREADY_RECEIVED", **extra)
    logger.info("Stored interface-4 %s %s (visit %s) from %s", kind, record_id, visit_id,
                principal.actor)
    return clinical._reply(202, "ACCEPTED", **extra)


@router.post("/patient-information")
async def patient_information(request: Request):
    """API 2. Sent at the moment the patient is opened."""
    return await receive(request, "patient_information")


@router.post("/prescription")
async def prescription(request: Request):
    """API 3. Sent when the prescription is built, and again when it is corrected."""
    return await receive(request, "prescription")


def _iso(value) -> Optional[str]:
    return integrity.iso_utc(value) if isinstance(value, datetime) else None


@router.get("/visit/{visit_id}/status")
async def visit_status(visit_id: str, request: Request):
    """
    What has reached us for one visit - never a clinical value. Only a visit
    at a hospital this credential speaks for; any other answers 404.
    """
    try:
        principal = await clinical._authenticate(request, cmed_auth.READ)
    except clinical._Refusal as exc:
        return clinical._reply(exc.status, exc.code, str(exc), **exc.extra)
    if not VISIT_ID.match(visit_id or ""):
        return clinical._reply(400, "INVALID_IDENTIFIER", fields=["visit_id"])
    async with api_v2._clinical()._pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT v.*, (SELECT jsonb_array_length(warnings) FROM visit_messages m
                          WHERE m.hospital_id = v.hospital_id AND m.visit_id = v.visit_id
                            AND m.kind = 'patient_information'
                          ORDER BY m.id DESC LIMIT 1) AS api2_warnings,
                        (SELECT jsonb_array_length(warnings) FROM visit_messages m
                          WHERE m.hospital_id = v.hospital_id AND m.visit_id = v.visit_id
                            AND m.kind = 'prescription'
                          ORDER BY m.id DESC LIMIT 1) AS api3_warnings
              FROM patient_visit v
             WHERE v.visit_id = $1 AND v.hospital_id = ANY($2::text[])
             ORDER BY v.id DESC LIMIT 1
        """, visit_id, sorted(principal.hospitals))
    if row is None:
        return clinical._reply(404, "NOT_FOUND", visit_id=visit_id)

    recording = None
    if row["practitioner_id"] and row["start_time"] and row["visit_date"]:
        visit = _visit_of({"patient_id": row["hospital_patient_id"],
                           "doctor_id": row["practitioner_id"],
                           "hospital_id": row["hospital_id"],
                           "start_time": row["start_time"].isoformat(),
                           "date": row["visit_date"].isoformat()})
        try:
            internal = await api_v2._repo().hospital_for_cmed_id(row["hospital_id"]) \
                or row["hospital_id"]
            recording = await _recording_of(visit, internal)
        except Exception as exc:
            logger.warning("Status of %s: recording lookup failed: %s", visit_id, exc)

    api1 = None
    if recording is not None:
        api1 = {"session_id": recording["session_id"],
                "started_at": _iso(recording["opened_at"]),
                "ended_at": _iso(recording["closed_at"]),
                "ended_by": recording["close_reason"] or None,
                "audio_received": bool(recording["segment_count"]),
                "seconds": float(recording["total_duration_seconds"] or 0) or None,
                "confirmation": recording["confirmation"]}
    api2 = {"received_at": _iso(row["patient_information_at"]),
            "warnings": row["api2_warnings"] or 0} if row["patient_information_at"] else None
    api3 = {"received_at": _iso(row["prescription_at"]),
            "version": row["prescription_version"],
            "warnings": row["api3_warnings"] or 0} if row["prescription_at"] else None
    return clinical._reply(200, "OK", visit_id=visit_id, hospital_id=row["hospital_id"],
                           patient_id=row["hospital_patient_id"], api1_recording=api1,
                           api2=api2, api3=api3,
                           complete=bool(api1 and api1["audio_received"] and api2 and api3))


__all__ = ["router", "read", "to_older_body", "file_message", "VISIT_ID"]
