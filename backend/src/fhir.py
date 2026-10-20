"""
API 2 and API 3b in HL7 FHIR R4 (FHIR messaging).

    POST /fhir/r4/$process-message      a Bundle of type "message"; its
                                        MessageHeader.eventCoding.code says which:
                                          patient-information   (API 2)
                                          prescription          (API 3b)
    GET  /fhir/r4/Encounter?patient.identifier=[system|]id
                                        the visits held, as a searchset Bundle
    GET  /fhir/r4/Encounter/{id}        one of them
    GET  /fhir/r4/MessageDefinition/{id}  what each message must carry
    GET  /fhir/r4/metadata              CapabilityStatement (no token needed)
    GET  /fhir/r4/.well-known/smart-configuration   where to get a token

How the message maps (the full table is in the CMED integration document):

    MessageHeader.focus         -> this visit's Encounter
    Encounter.subject           -> Patient          .identifier = patient_id
    Encounter.participant       -> Practitioner     .identifier = doctor_id
    Encounter.serviceProvider   -> Organization     .identifier = hospital_id
    Encounter.period.start      -> start_time; date = its day in the clinic's zone
    Patient                     -> demographics
    Observation (LOINC vitals)  -> paramedic        (weight, height, BP, pulse,
                                                     temperature, SpO2; others kept)
    any other Encounter         -> previous_visit, with the MedicationRequest,
                                   Condition, ServiceRequest and CarePlan that
                                   point at it as its prescription
    MedicationRequest           -> items[]
    Condition                   -> diagnoses[]
    ServiceRequest              -> investigations[]
    CarePlan                    -> advice (description), follow_up (period.start),
                                   notes (note)

Each message becomes the same body the JSON endpoints take and goes through
clinical.process: one set of rules, one store, one place to fix a bug.

Nothing a sender writes can crash this: every element is read through a
type check, so a malformed message is a 400 OperationOutcome naming the
element - never a 500, which CMED's outbox would rightly retry for ever.
"""
from __future__ import annotations

import logging
import math
import os
import uuid
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

import api_v2
import clinical
import cmed_auth
import confirmation as conf
import integrity
import security

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/fhir/r4", tags=["fhir"])
well_known = APIRouter(tags=["fhir"])

FHIR_JSON = "application/fhir+json"
EVENTS = {"patient-information": "patient_information", "prescription": "prescription"}
EVENT_SYSTEM = "urn:aimscribe:fhir:event"
EXT = "urn:aimscribe:fhir:extension:"
MESSAGE_DEFINITIONS = {"patient-information": "aimscribe-patient-information",
                       "prescription": "aimscribe-prescription"}

# The identifier systems proposed to CMED. A hospital's client may register its
# own (cmed_auth.client_systems); a resource with exactly one identifier, or
# one marked use="official", is read whatever its system.
SYSTEMS = {
    "Patient": os.getenv("AIMS_FHIR_SYSTEM_PATIENT", "urn:aimscribe:cmed:patient"),
    "Practitioner": os.getenv("AIMS_FHIR_SYSTEM_PRACTITIONER", "urn:aimscribe:cmed:practitioner"),
    "Organization": os.getenv("AIMS_FHIR_SYSTEM_ORGANIZATION", "urn:aimscribe:cmed:hospital"),
}

LOINC = "http://loinc.org"
VITALS = {                      # LOINC code -> paramedic field
    "29463-7": "weight_kg", "3141-9": "weight_kg",
    "8302-2": "height_cm", "8306-3": "height_cm",
    "8867-4": "pulse_bpm",
    "8310-5": "temperature_c", "8331-1": "temperature_c",
    "59408-5": "spo2_percent", "2708-6": "spo2_percent",
}
BP_PANEL, SYSTOLIC, DIASTOLIC = "85354-9", "8480-6", "8462-4"
AGE = "30525-0"
UNIT_SCALE = {                  # (field, unit) -> multiply to reach our unit
    ("weight_kg", "g"): 0.001, ("weight_kg", "[lb_av]"): 0.45359237,
    ("height_cm", "m"): 100.0, ("height_cm", "[in_i]"): 2.54, ("height_cm", "mm"): 0.1,
}
MAX_NUMBER = 1e9                # no measurement or dose is larger; anything bigger is noise


class FhirError(Exception):
    """A message that cannot be read: answered 400 with the element named."""

    def __init__(self, where: str, problem: str, status: int = 400, code: str = "invalid"):
        super().__init__(f"{where}: {problem}")
        self.where, self.status, self.code = where, status, code


# ------------------------------------------------------------------ safe readers

def _list(x: Any) -> list:
    return x if isinstance(x, list) else []


def _dict(x: Any) -> dict:
    return x if isinstance(x, dict) else {}


def _str(x: Any) -> Optional[str]:
    return x.strip() if isinstance(x, str) and x.strip() else None


def _first(x: Any) -> dict:
    """The first element of a list, if it is an object."""
    items = _list(x)
    return items[0] if items and isinstance(items[0], dict) else {}


def _finite(x: Any) -> Optional[float]:
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        return None
    try:
        value = float(x)
    except (OverflowError, ValueError):
        return None
    return value if math.isfinite(value) and abs(value) <= MAX_NUMBER else None


# ------------------------------------------------------------------ replies

_ISSUE = {400: "invalid", 401: "login", 403: "forbidden", 404: "not-found",
          413: "too-costly", 422: "processing", 429: "throttled"}


def outcome(status: int, code: str, text: str, *, where: Optional[List[str]] = None,
            severity: Optional[str] = None) -> Dict[str, Any]:
    issue = {
        "severity": severity or ("information" if status < 400 else "error"),
        "code": "informational" if status < 400 else _ISSUE.get(status, "exception"),
        "details": {"coding": [{"system": "urn:aimscribe:fhir:reply-code", "code": code}],
                    "text": text},
        "diagnostics": f"request_id={security.request_id()}",
    }
    if where:
        issue["expression"] = where
    return {"resourceType": "OperationOutcome", "issue": [issue]}


def _fhir(status: int, resource: Dict[str, Any], headers: Optional[Dict[str, str]] = None):
    return JSONResponse(status_code=status, content=resource, media_type=FHIR_JSON,
                        headers=headers)


def refused(status: int, code: str, text: str, where: Optional[List[str]] = None,
            headers: Optional[Dict[str, str]] = None):
    """An OperationOutcome reply; also used by security.py for FHIR paths."""
    headers = dict(headers or {})
    if status == 401:
        headers["WWW-Authenticate"] = 'Bearer error="invalid_token"'
    return _fhir(status, outcome(status, code, text, where=where), headers or None)


_refused = refused


# ------------------------------------------------------------------ reading a Bundle

class Bundle:
    """The entries of one message Bundle, findable by reference."""

    def __init__(self, bundle: Any):
        if not isinstance(bundle, dict) or bundle.get("resourceType") != "Bundle":
            raise FhirError("Bundle", "the body must be a FHIR Bundle")
        if bundle.get("type") != "message":
            raise FhirError("Bundle.type", "must be 'message'")
        entries = bundle.get("entry")
        if not isinstance(entries, list) or not entries:
            raise FhirError("Bundle.entry", "a message needs entries")
        if len(entries) > 2000:
            raise FhirError("Bundle.entry", "more than 2000 entries")
        self.bundle = bundle
        self.resources: List[Dict[str, Any]] = []
        self._by_ref: Dict[str, Dict[str, Any]] = {}
        for n, entry in enumerate(entries):
            res = entry.get("resource") if isinstance(entry, dict) else None
            if not isinstance(res, dict) or not isinstance(res.get("resourceType"), str):
                raise FhirError(f"Bundle.entry[{n}].resource", "each entry needs a resource")
            self.resources.append(res)
            if isinstance(entry.get("fullUrl"), str):
                self._by_ref[entry["fullUrl"]] = res
            if isinstance(res.get("id"), str):
                self._by_ref[f"{res['resourceType']}/{res['id']}"] = res
        header = self.resources[0]
        if header.get("resourceType") != "MessageHeader":
            raise FhirError("Bundle.entry[0]", "the first entry must be the MessageHeader")
        self.header = header

    def of(self, kind: str) -> List[Dict[str, Any]]:
        return [r for r in self.resources if r["resourceType"] == kind]

    def resolve(self, ref: Any, where: str, kind: Optional[str] = None) -> Dict[str, Any]:
        target = ref.get("reference") if isinstance(ref, dict) else None
        res = self._by_ref.get(target) if isinstance(target, str) else None
        if res is None and isinstance(target, str) and "/" in target:
            res = self._by_ref.get("/".join(target.split("/")[-2:]))
        if res is None:
            raise FhirError(where, "must reference a resource inside this Bundle")
        if kind and res["resourceType"] != kind:
            raise FhirError(where, f"must reference a {kind}")
        return res

    def refers_to(self, res: Dict[str, Any], encounter: Dict[str, Any]) -> bool:
        ref = res.get("encounter") or res.get("context")
        if not isinstance(ref, dict):
            return False
        try:
            return self.resolve(ref, "encounter") is encounter
        except FhirError:
            return False


def identifier(res: Dict[str, Any], where: str,
               systems: Optional[Dict[str, str]] = None) -> str:
    """
    The id we file this resource under. In order: the identifier with the
    hospital's own registered system, then with the agreed system, then the
    one marked use="official", then the only one there is. Several, none of
    them chosen by those rules, is genuinely ambiguous and refused by name.
    """
    kind = res["resourceType"]
    ids = [i for i in _list(res.get("identifier")) if isinstance(i, dict) and _str(i.get("value"))]
    for system in ((systems or {}).get(kind), SYSTEMS.get(kind)):
        if system:
            chosen = {_str(i["value"]) for i in ids if i.get("system") == system}
            if len(chosen) == 1:
                return chosen.pop()
            if len(chosen) > 1:
                raise FhirError(f"{where}.identifier",
                                f"has several identifiers with system {system}")
    official = {_str(i["value"]) for i in ids if i.get("use") == "official"}
    if len(official) == 1:
        return official.pop()
    values = {_str(i["value"]) for i in ids}
    if len(values) == 1:
        return values.pop()
    if not values:
        raise FhirError(f"{where}.identifier", "needs an identifier")
    raise FhirError(f"{where}.identifier",
                    f"has {len(values)} identifiers and none is marked: use system "
                    f"{(systems or {}).get(kind) or SYSTEMS.get(kind)} or use='official' "
                    "for the one this message is about")


def _text(concept: Any) -> Optional[str]:
    concept = _dict(concept)
    if _str(concept.get("text")):
        return _str(concept["text"])
    for c in _list(concept.get("coding")):
        if isinstance(c, dict):
            for key in ("display", "code"):
                if _str(c.get(key)):
                    return _str(c[key])
    return None


def _codes(concept: Any) -> List[str]:
    return [c["code"] for c in _list(_dict(concept).get("coding"))
            if isinstance(c, dict) and isinstance(c.get("code"), str)]


def _quantity(q: Any) -> Tuple[Optional[float], Optional[str]]:
    q = _dict(q)
    value = _finite(q.get("value"))
    if value is None:
        return None, None
    unit = q.get("code") if isinstance(q.get("code"), str) else \
        q.get("unit") if isinstance(q.get("unit"), str) else None
    return value, unit


def _value(res: Dict[str, Any]) -> Any:
    if "valueQuantity" in res:
        return _quantity(res["valueQuantity"])[0]
    for key in ("valueInteger", "valueDecimal"):
        if key in res:
            return _finite(res[key])
    for key in ("valueString", "valueDateTime"):
        if key in res:
            return res[key] if isinstance(res[key], str) else None
    if "valueBoolean" in res:
        return res["valueBoolean"] if isinstance(res["valueBoolean"], bool) else None
    if "valueCodeableConcept" in res:
        return _text(res["valueCodeableConcept"])
    return None


def _number(v: Optional[float]) -> Any:
    return int(v) if isinstance(v, float) and v.is_integer() else v


def _notes(res: Dict[str, Any]) -> Optional[str]:
    texts = [_str(n.get("text")) for n in _list(res.get("note")) if isinstance(n, dict)]
    texts = [t for t in texts if t]
    return "\n".join(texts) or None


def _start(encounter: Dict[str, Any], where: str) -> Tuple[Optional[str], str]:
    """(canonical start_time, or None for a date-only start; the visit's local date)."""
    start = _dict(encounter.get("period")).get("start")
    if not isinstance(start, str) or len(start) < 10:
        raise FhirError(f"{where}.period.start", "required")
    if "T" not in start:
        try:
            return None, date.fromisoformat(start[:10]).isoformat()
        except ValueError:
            raise FhirError(f"{where}.period.start", "is not a date")
    try:
        return conf.canonical_start(start)
    except ValueError as exc:
        raise FhirError(f"{where}.period.start", str(exc))


# ------------------------------------------------------------------ FHIR -> our body

def five_fields(b: Bundle, encounter: Dict[str, Any], where: str,
                systems: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    patient = b.resolve(encounter.get("subject"), f"{where}.subject", "Patient")
    doctor = None
    for n, p in enumerate(_list(encounter.get("participant"))):
        if not isinstance(p, dict) or not isinstance(p.get("individual"), dict):
            continue
        who = b.resolve(p["individual"], f"{where}.participant[{n}].individual")
        if who["resourceType"] == "PractitionerRole":
            who = b.resolve(who.get("practitioner"), f"{where}.participant[{n}]"
                            ".individual.practitioner", "Practitioner")
        if who["resourceType"] == "Practitioner":
            doctor = who
            break
    if doctor is None:
        raise FhirError(f"{where}.participant", "needs the doctor, as a Practitioner")
    org = b.resolve(encounter.get("serviceProvider"), f"{where}.serviceProvider",
                    "Organization")
    start_time, day = _start(encounter, where)
    if start_time is None:
        raise FhirError(f"{where}.period.start", "needs a time with its offset")
    return {"patient_id": identifier(patient, "Patient", systems),
            "doctor_id": identifier(doctor, "Practitioner", systems),
            "hospital_id": identifier(org, "Organization", systems),
            "start_time": start_time, "date": day}


def demographics(patient: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    n = _first(patient.get("name"))
    if n:
        text = _str(n.get("text")) or " ".join(
            [*[g for g in _list(n.get("given")) if isinstance(g, str)],
             *([n["family"]] if isinstance(n.get("family"), str) else [])]).strip()
        if text:
            out["name"] = text
    if patient.get("gender") in ("female", "male"):
        out["sex"] = patient["gender"]
    if isinstance(patient.get("birthDate"), str) and len(patient["birthDate"]) == 10:
        out["date_of_birth"] = patient["birthDate"]
    for t in _list(patient.get("telecom")):
        if isinstance(t, dict) and t.get("system") == "phone" and isinstance(t.get("value"), str):
            out["phone"] = t["value"]
            break
    for a in _list(patient.get("address")):
        if isinstance(a, dict):
            text = _str(a.get("text")) or ", ".join(
                [*[x for x in _list(a.get("line")) if isinstance(x, str)],
                 *[a[k] for k in ("city", "district", "state", "postalCode")
                   if isinstance(a.get(k), str)]])
            if text:
                out["address"] = text
                break
    return out


def observations(b: Bundle, encounter: Dict[str, Any], focus: Dict[str, Any]
                 ) -> Tuple[Dict[str, Any], Optional[float], Dict[str, Dict[str, Any]]]:
    """(paramedic, age in years if sent, {"female_details": .., "male_details": ..})."""
    paramedic: Dict[str, Any] = {}
    details: Dict[str, Dict[str, Any]] = {}
    age = None
    notes = []
    for obs in b.of("Observation"):
        mine = b.refers_to(obs, encounter) or (encounter is focus and not obs.get("encounter"))
        if not mine:
            continue
        codes = _codes(obs.get("code"))
        categories = [c for cat in _list(obs.get("category")) for c in _codes(cat)]
        group = next((c.replace("-", "_") for c in categories
                      if c in ("female-details", "male-details")), None)
        if group:
            key = _text(obs.get("code")) or "value"
            details.setdefault(group, {})[key] = _value(obs)
            continue
        if AGE in codes:
            age = _quantity(obs.get("valueQuantity"))[0]
            continue
        when = obs.get("effectiveDateTime")
        if isinstance(when, str) and "recorded_at" not in paramedic:
            paramedic["recorded_at"] = when
        if _notes(obs):
            notes.append(_notes(obs))
        if BP_PANEL in codes or SYSTOLIC in codes:
            parts = {c: _quantity(comp.get("valueQuantity"))[0]
                     for comp in _list(obs.get("component")) if isinstance(comp, dict)
                     for c in _codes(comp.get("code"))}
            s, d = parts.get(SYSTOLIC), parts.get(DIASTOLIC)
            if s is not None and d is not None:
                paramedic["blood_pressure"] = f"{_number(s)}/{_number(d)}"
            continue
        field = next((VITALS[c] for c in codes if c in VITALS), None)
        if field:
            value, unit = _quantity(obs.get("valueQuantity"))
            if value is not None:
                if field == "temperature_c" and unit in ("[degF]", "degF"):
                    value = (value - 32) * 5 / 9
                value *= UNIT_SCALE.get((field, unit), 1.0)
                paramedic[field] = _number(round(value, 2))
            continue
        # A measurement we have no column for is kept, as the contract promises.
        # One with no value is only a note (the paramedic's remarks).
        key = _text(obs.get("code"))
        if key and _value(obs) is not None:
            paramedic[key] = _value(obs)
    if notes:
        paramedic["notes"] = "\n".join(notes)
    return paramedic, age, details


def _with_unit(value: Optional[float], unit: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    return f"{_number(value)} {unit or ''}".strip()


def prescription_of(b: Bundle, encounter: Dict[str, Any], focus: Dict[str, Any]
                    ) -> Dict[str, Any]:
    def mine(res):
        return b.refers_to(res, encounter) or (encounter is focus and not res.get("encounter"))

    items = []
    for n, mr in enumerate(r for r in b.of("MedicationRequest") if mine(r)):
        drug = _text(mr.get("medicationCodeableConcept"))
        if drug is None and isinstance(mr.get("medicationReference"), dict):
            drug = _text(b.resolve(mr["medicationReference"],
                                   f"MedicationRequest[{n}].medicationReference",
                                   "Medication").get("code"))
        item: Dict[str, Any] = {"drug": drug or ""}
        dosage = _first(mr.get("dosageInstruction"))
        rate = _first(dosage.get("doseAndRate"))
        dose = _with_unit(*_quantity(rate.get("doseQuantity")))
        if dose:
            item["dose"] = dose
        timing = _dict(dosage.get("timing"))
        repeat = _dict(timing.get("repeat"))
        frequency = _text(timing.get("code"))
        how_often, period = _finite(repeat.get("frequency")), _finite(repeat.get("period", 1))
        if frequency is None and how_often and period and isinstance(repeat.get("periodUnit"),
                                                                     str):
            frequency = f"{_number(how_often)} per {_number(period)} {repeat['periodUnit']}"
        if frequency:
            item["frequency"] = frequency
        bounds = repeat.get("boundsDuration") or \
            _dict(mr.get("dispenseRequest")).get("expectedSupplyDuration")
        duration = _with_unit(*_quantity(bounds))
        if duration:
            item["duration"] = duration
        instructions = [dosage.get("patientInstruction"), dosage.get("text"),
                        *[_text(a) for a in _list(dosage.get("additionalInstruction"))]]
        instructions = [i.strip() for i in instructions if isinstance(i, str) and i.strip()]
        if instructions:
            item["instructions"] = "; ".join(instructions)
        items.append(item)
    out: Dict[str, Any] = {
        "items": items,
        "diagnoses": [t for t in (_text(c.get("code")) for c in b.of("Condition") if mine(c))
                      if t],
        "investigations": [t for t in (_text(s.get("code"))
                                       for s in b.of("ServiceRequest") if mine(s)) if t],
    }
    authored = [m.get("authoredOn") for m in b.of("MedicationRequest")
                if mine(m) and isinstance(m.get("authoredOn"), str)]
    if authored:
        out["issued_at"] = authored[0]
    for plan in (p for p in b.of("CarePlan") if mine(p)):
        if isinstance(plan.get("description"), str):
            out["advice"] = plan["description"]
        start = _dict(plan.get("period")).get("start")
        if isinstance(start, str) and len(start) >= 10:
            out["follow_up"] = start[:10]
        if _notes(plan):
            out["notes"] = _notes(plan)
        break
    return out


def to_body(b: Bundle, systems: Optional[Dict[str, str]] = None) -> Tuple[str, Dict[str, Any]]:
    """(kind, the body clinical.process takes) for one message."""
    event = b.header.get("eventCoding")
    code = event.get("code") if isinstance(event, dict) else None
    kind = EVENTS.get(code) if isinstance(code, str) else None
    if kind is None:
        raise FhirError("MessageHeader.eventCoding.code",
                        f"must be one of {', '.join(EVENTS)}")
    focus_refs = b.header.get("focus")
    if not isinstance(focus_refs, list) or not focus_refs:
        raise FhirError("MessageHeader.focus", "must reference this visit's Encounter")
    focus = b.resolve(focus_refs[0], "MessageHeader.focus[0]", "Encounter")
    body = five_fields(b, focus, "Encounter", systems)

    if kind == "prescription":
        presc = prescription_of(b, focus, focus)
        if "issued_at" not in presc and isinstance(b.bundle.get("timestamp"), str):
            presc["issued_at"] = b.bundle["timestamp"]
        body.update(presc)
        return kind, body

    patient = b.resolve(focus.get("subject"), "Encounter.subject", "Patient")
    body["demographics"] = demographics(patient)
    paramedic, age, details = observations(b, focus, focus)
    if age is not None:
        body["demographics"]["age_years"] = _number(age)
    if paramedic:
        body["paramedic"] = paramedic
    body.update(details)

    earlier = [e for e in b.of("Encounter") if e is not focus]
    if len(earlier) > 1:
        raise FhirError("Encounter", "at most one previous visit per message")
    if not earlier:
        body["previous_visit"] = None
        return kind, body
    prev = earlier[0]
    start_time, day = _start(prev, "previous Encounter")
    pv: Dict[str, Any] = {"date": day}
    if start_time is not None:
        pv.update({k: v for k, v in five_fields(b, prev, "previous Encounter", systems).items()
                   if k in ("doctor_id", "hospital_id", "start_time")})
    if prev.get("subject"):
        # The same patient is the rule; only a different one is passed on, so
        # the check in clinical.problems_with refuses it by name.
        who = identifier(b.resolve(prev["subject"], "previous Encounter.subject", "Patient"),
                         "Patient", systems)
        if who != body["patient_id"]:
            pv["patient_id"] = who
    presc = prescription_of(b, prev, focus)
    presc.pop("issued_at", None)
    pv["prescription"] = presc
    body["previous_visit"] = pv
    return kind, body


# ------------------------------------------------------------------ endpoints

def _base(request: Request) -> str:
    base = os.getenv("AIMS_PUBLIC_BASE_URL", "").strip().rstrip("/")
    if base:
        return base
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    return f"{proto}://{request.headers.get('host', request.url.netloc)}"


def _response_message(request_bundle: Dict[str, Any], status: int, code: str,
                      message: Optional[str], extra: Dict[str, Any]) -> Dict[str, Any]:
    params = [{"name": "code", "valueCode": code},
              {"name": "request_id", "valueString": security.request_id()}]
    for key in ("record_id", "received_at", "quarantine_id"):
        if extra.get(key):
            params.append({"name": key, "valueString": str(extra[key])})
    if extra.get("version") is not None:
        params.append({"name": "version", "valueInteger": int(extra["version"])})
    pv = extra.get("previous_visit")
    if isinstance(pv, dict):
        params.append({"name": "previous_visit", "part": [
            {"name": k, **({"valueInteger": v} if isinstance(v, int) and not isinstance(v, bool)
                           else {"valueBoolean": v} if isinstance(v, bool)
                           else {"valueString": str(v)})}
            for k, v in pv.items() if v is not None]})
    oo = outcome(status, code, message or clinical.MESSAGES.get(code, code),
                 where=[f["field"] if isinstance(f, dict) else str(f)
                        for f in extra.get("fields") or []] or None)
    header = _dict(_first(request_bundle.get("entry")).get("resource"))
    return {
        "resourceType": "Bundle", "id": str(uuid.uuid4()), "type": "message",
        "timestamp": integrity.iso_utc(datetime.now(timezone.utc)),
        "entry": [
            {"fullUrl": "urn:uuid:" + str(uuid.uuid4()), "resource": {
                "resourceType": "MessageHeader",
                "eventCoding": header.get("eventCoding"),
                "source": {"name": "AIMScribe v3", "endpoint": "urn:aimscribe:server"},
                "response": {"identifier": str(header.get("id") or request_bundle.get("id")
                                               or ""),
                             "code": "ok" if status < 400 else
                             ("transient-error" if status >= 500 else "fatal-error")}}},
            {"fullUrl": "urn:uuid:" + str(uuid.uuid4()), "resource": oo},
            {"fullUrl": "urn:uuid:" + str(uuid.uuid4()),
             "resource": {"resourceType": "Parameters", "parameter": params}},
        ],
    }


async def _note_bundle_id(principal, bundle: Dict[str, Any], body: Dict[str, Any]) -> None:
    """
    FHIR messaging names each message by Bundle.id. Duplicates are decided by
    content (clinical.process), but the same id carrying different content is
    a sender bug worth a person's attention.
    """
    bundle_id = bundle.get("id")
    if not isinstance(bundle_id, str) or not 1 <= len(bundle_id) <= 128:
        return
    digest = integrity.sha256_bytes(integrity.canonical_json(body))
    try:
        async with api_v2._repo()._pool.acquire() as conn:
            held = await conn.fetchval("""
                INSERT INTO fhir_messages (client_id, bundle_id, body_sha256)
                VALUES ($1, $2, $3)
                ON CONFLICT (client_id, bundle_id) DO UPDATE SET bundle_id = EXCLUDED.bundle_id
                RETURNING body_sha256
            """, principal.client_id, bundle_id, digest)
        if held is not None and bytes(held) != digest:
            await api_v2._repo().raise_alert(
                alert_type="fhir_bundle_id_reused", severity="warning",
                detail={"client": principal.client_id, "bundle_id": bundle_id})
    except Exception as exc:                              # bookkeeping only
        logger.debug("Bundle.id not recorded: %s", exc)


@router.post("/$process-message")
async def process_message(request: Request):
    try:
        principal = await cmed_auth.authenticate(request, cmed_auth.WRITE)
    except cmed_auth.AuthError as exc:
        return refused(exc.status, exc.code, str(exc))
    try:
        bundle, raw = await clinical._read_json(request)
    except clinical._Refusal as exc:
        return refused(exc.status, exc.code, str(exc))
    try:
        systems = await cmed_auth.client_systems(principal.client_id) \
            if principal.via == "token" else {}
        kind, body = to_body(Bundle(bundle), systems)
    except FhirError as exc:
        return refused(exc.status, "MALFORMED_FHIR", str(exc), where=[exc.where])
    except Exception as exc:                  # a shape no rule above foresaw: still the sender's
        logger.warning("Unreadable FHIR message from %s: %r", principal.client_id, exc)
        return refused(400, "MALFORMED_FHIR", "The message could not be read.")
    status, code, message, extra = await clinical.process(kind, body, raw, principal)
    if status < 300:
        await _note_bundle_id(principal, bundle, body)
    # Synchronous FHIR messaging answers 200 with the response message; our own
    # code (ACCEPTED, ALREADY_RECEIVED) is in its Parameters.
    http_status = 200 if status < 300 else status
    headers = cmed_auth.legacy_headers() if principal.via == "legacy_key" else None
    return _fhir(http_status, _response_message(bundle, status, code, message, extra), headers)


def _encounter_resource(request: Request, v: Dict[str, Any], encounter_id: str) -> Dict[str, Any]:
    ext = [{"url": EXT + "source", "valueString": v["source"]}]
    if v["prescription_version"] is not None:
        ext += [{"url": EXT + "prescription-version",
                 "valueInteger": v["prescription_version"]},
                {"url": EXT + "prescription-fingerprint",
                 "valueString": v["prescription_fingerprint"]}]
    if v.get("received_at"):
        ext.append({"url": EXT + "received-at", "valueString": str(v["received_at"])})
    return {
        "resourceType": "Encounter", "id": encounter_id, "status": "finished",
        "class": {"system": "http://terminology.hl7.org/CodeSystem/v3-ActCode", "code": "AMB"},
        "subject": {"identifier": {"system": SYSTEMS["Patient"], "value": v["patient_id"]}},
        "participant": [{"individual": {"identifier": {
            "system": SYSTEMS["Practitioner"], "value": v["doctor_id"]}}}],
        "serviceProvider": {"identifier": {"system": SYSTEMS["Organization"],
                                           "value": v["hospital_id"]}},
        "period": {"start": v["start_time"]},
        "extension": ext}


def _digits(text: str) -> bool:
    return text.isascii() and text.isdigit()


@router.get("/Encounter")
async def search_encounters(request: Request):
    try:
        principal = await cmed_auth.authenticate(request, cmed_auth.READ)
    except cmed_auth.AuthError as exc:
        return refused(exc.status, exc.code, str(exc))
    q = request.query_params
    wanted = q.get("patient.identifier") or q.get("subject:Patient.identifier") or ""
    system, _, patient_id = wanted.rpartition("|")
    try:
        patient_id = integrity.safe_identifier(patient_id, field="patient_id")
    except ValueError:
        return refused(400, "INVALID_IDENTIFIER",
                       "Search with patient.identifier=[system|]patient_id.",
                       where=["patient.identifier"])
    count = q.get("_count", "50")
    if not _digits(count) or not 1 <= int(count) <= 200:
        return refused(400, "MISSING_FIELD", "_count must be 1 to 200.", where=["_count"])
    before = None
    when = q.get("date")
    if when:
        if not (when.startswith("lt") and len(when) == 12):
            return refused(400, "MISSING_FIELD", "Only date=ltYYYY-MM-DD is supported.",
                           where=["date"])
        try:
            before = date.fromisoformat(when[2:])
        except ValueError:
            return refused(400, "MISSING_FIELD", "date=ltYYYY-MM-DD", where=["date"])
    after_key = None
    if q.get("_cursor"):
        try:
            after_key = clinical._read_cursor(q["_cursor"])
        except clinical._Refusal as exc:
            return refused(exc.status, exc.code, str(exc), where=["_cursor"])

    bundle = {"resourceType": "Bundle", "type": "searchset", "total": 0,
              "link": [{"relation": "self", "url": str(request.url)}], "entry": []}
    # A system that is not the one patients are filed under names someone else.
    accepted = {SYSTEMS["Patient"]}
    if principal.via == "token":
        own = (await cmed_auth.client_systems(principal.client_id)).get("Patient")
        if own:
            accepted.add(own)
    if system and system not in accepted:
        return _fhir(200, bundle)

    visits = await api_v2._clinical().visits_for(
        patient_id, actor=principal.actor, limit=int(count), before=before,
        after_key=after_key, hospitals=sorted(principal.hospitals))
    for v in visits:
        key = v.pop("_key")
        bundle["entry"].append({"fullUrl": f"{_base(request)}/fhir/r4/Encounter/{key[2]}",
                                "search": {"mode": "match"},
                                "resource": _encounter_resource(request, v, str(key[2]))})
    bundle["total"] = len(bundle["entry"])
    if len(visits) == int(count) and visits:
        last = visits[-1]
        cursor = clinical._write_cursor((date.fromisoformat(last["date"]), last["start_time"],
                                         uuid.UUID(bundle["entry"][-1]["resource"]["id"])))
        bundle["link"].append({"relation": "next", "url": f"{_base(request)}/fhir/r4/Encounter?"
                               + urlencode({"patient.identifier": wanted, "_count": count,
                                            **({"date": when} if when else {}),
                                            "_cursor": cursor})})
    return _fhir(200, bundle)


@router.get("/Encounter/{encounter_id}")
async def read_encounter(encounter_id: str, request: Request):
    try:
        principal = await cmed_auth.authenticate(request, cmed_auth.READ)
    except cmed_auth.AuthError as exc:
        return refused(exc.status, exc.code, str(exc))
    try:
        uuid.UUID(encounter_id)
    except ValueError:
        return refused(404, "NOT_FOUND", "No such Encounter.")
    found = await api_v2._clinical().encounter_by_id(
        encounter_id, actor=principal.actor, hospitals=sorted(principal.hospitals))
    if found is None:
        # Another hospital's visit is answered exactly like one that does not exist.
        return refused(404, "NOT_FOUND", "No such Encounter.")
    found.pop("_key", None)
    return _fhir(200, _encounter_resource(request, found, encounter_id))


def message_definition(request: Request, event: str) -> Dict[str, Any]:
    ident = MESSAGE_DEFINITIONS[event]
    return {
        "resourceType": "MessageDefinition", "id": ident,
        "url": f"{_base(request)}/fhir/r4/MessageDefinition/{ident}",
        "version": "1", "name": ident.replace("-", "_"), "status": "active",
        "date": "2026-10-20", "publisher": "AIMS LAB, United International University",
        "description": ("API 2: a patient was opened - the visit, demographics, paramedic "
                        "measurements and the previous visit." if event == "patient-information"
                        else "API 3b: the prescription for a visit."),
        "eventCoding": {"system": EVENT_SYSTEM, "code": event},
        "category": "notification",
        "focus": [{"code": "Encounter", "min": 1, "max": "1"}],
        "responseRequired": "always",
    }


@router.get("/MessageDefinition/{ident}")
async def read_message_definition(ident: str, request: Request):
    event = next((e for e, i in MESSAGE_DEFINITIONS.items() if i == ident), None)
    if event is None:
        return refused(404, "NOT_FOUND", "No such MessageDefinition.")
    return _fhir(200, message_definition(request, event))


def smart_configuration(request: Request) -> Dict[str, Any]:
    return {
        "token_endpoint": _base(request) + cmed_auth.TOKEN_PATH,
        "token_endpoint_auth_methods_supported": ["private_key_jwt"],
        "token_endpoint_auth_signing_alg_values_supported": list(cmed_auth.ALGORITHMS),
        "grant_types_supported": ["client_credentials"],
        "scopes_supported": list(cmed_auth.ALL_SCOPES),
        "capabilities": ["client-confidential-asymmetric", "permission-v2"],
    }


@router.get("/.well-known/smart-configuration")
async def smart_config_fhir(request: Request):
    return JSONResponse(smart_configuration(request))


@well_known.get("/.well-known/smart-configuration")
async def smart_config_root(request: Request):
    return JSONResponse(smart_configuration(request))


@router.get("/metadata")
async def metadata(request: Request):
    base = _base(request)
    return _fhir(200, {
        "resourceType": "CapabilityStatement", "status": "active", "kind": "instance",
        "date": "2026-10-20", "fhirVersion": "4.0.1", "format": ["json"],
        "software": {"name": "AIMScribe v3", "version": "3.5.0"},
        "implementation": {"description": "AIMS LAB recordings server", "url": base + "/fhir/r4"},
        "rest": [{
            "mode": "server",
            "security": {
                "service": [{"coding": [{
                    "system": "http://terminology.hl7.org/CodeSystem/restful-security-service",
                    "code": "SMART-on-FHIR"}]}],
                "extension": [{
                    "url": "http://fhir-registry.smarthealthit.org/StructureDefinition/oauth-uris",
                    "extension": [{"url": "token",
                                   "valueUri": base + cmed_auth.TOKEN_PATH}]}],
                "description": "SMART Backend Services: private_key_jwt client assertion, "
                               "one client per hospital, 5-minute bearer tokens, scopes "
                               + " ".join(cmed_auth.ALL_SCOPES) + "."},
            "resource": [
                {"type": "Encounter",
                 "interaction": [{"code": "read"}, {"code": "search-type"}],
                 "searchParam": [
                     {"name": "patient.identifier", "type": "token"},
                     {"name": "date", "type": "date"},
                     {"name": "_count", "type": "number"}]},
                {"type": "MessageDefinition", "interaction": [{"code": "read"}]}],
            "operation": [{"name": "process-message",
                           "definition": "http://hl7.org/fhir/OperationDefinition/"
                                         "MessageHeader-process-message"}],
        }],
        "messaging": [{"supportedMessage": [
            {"mode": "receiver",
             "definition": f"{base}/fhir/r4/MessageDefinition/{MESSAGE_DEFINITIONS[e]}"}
            for e in EVENTS]}],
    })


__all__ = ["router", "well_known", "to_body", "Bundle", "FhirError", "outcome", "refused",
           "SYSTEMS", "EVENTS", "MESSAGE_DEFINITIONS", "identifier"]
