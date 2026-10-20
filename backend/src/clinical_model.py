"""
CMED's clinical messages, turned into rows (SRS 3.2 §8.6, §8.7.2).

Pure functions, no database, so every rule can be tested alone. The original
JSON is always kept as well (SRS-DBA-11): anything this module does not
recognise is still in intake_records, and a field is never guessed. A value
that cannot be read becomes NULL rather than an error, because one odd
measurement must not stop a prescription being stored.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple

_BP = re.compile(r"^\s*(\d{2,3})\s*/\s*(\d{2,3})\s*$")

# The paramedic fields that have their own column. The full list is OD-16;
# anything else CMED sends is kept in `other`.
PARAMEDIC_COLUMNS = ("recorded_at", "weight_kg", "height_cm", "blood_pressure",
                     "pulse_bpm", "temperature_c", "spo2_percent", "notes")


def text(value: Any, limit: int = 2000) -> Optional[str]:
    if value is None:
        return None
    value = str(value).strip()
    return value[:limit] if value else None


def number(value: Any, low: float, high: float) -> Optional[float]:
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    return n if low <= n <= high else None


def whole(value: Any, low: int, high: int) -> Optional[int]:
    n = number(value, low, high)
    return int(round(n)) if n is not None else None


def day(value: Any) -> Optional[date]:
    try:
        return date.fromisoformat(str(value)[:10]) if value else None
    except ValueError:
        return None


def moment(value: Any) -> Optional[datetime]:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo is not None else None


def sex_of(value: Any) -> str:
    value = str(value or "").strip().lower()
    return value if value in ("female", "male") else "unknown"


def blood_pressure(value: Any) -> Tuple[Optional[int], Optional[int]]:
    match = _BP.match(str(value or ""))
    if not match:
        return None, None
    return (whole(match.group(1), 30, 300), whole(match.group(2), 10, 250))


# ------------------------------------------------------------------ rows

@dataclass
class Demographics:
    full_name: Optional[str]
    sex: str
    age_years: Optional[int]
    date_of_birth: Optional[date]
    phone: Optional[str]
    address: Optional[str]


@dataclass
class Paramedic:
    recorded_at: Optional[datetime]
    weight_kg: Optional[float]
    height_cm: Optional[float]
    blood_pressure: Optional[str]
    systolic: Optional[int]
    diastolic: Optional[int]
    pulse_bpm: Optional[int]
    temperature_c: Optional[float]
    spo2_percent: Optional[int]
    notes: Optional[str]
    other: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Item:
    line_no: int
    drug: str
    dose: Optional[str]
    frequency: Optional[str]
    duration: Optional[str]
    instructions: Optional[str]


@dataclass
class Prescription:
    issued_at: Optional[datetime]
    advice: Optional[str]
    follow_up_date: Optional[date]
    notes: Optional[str]
    items: List[Item]
    diagnoses: List[str]
    investigations: List[str]
    raw: Dict[str, Any]


@dataclass
class PreviousVisit:
    visit_date: date
    prescription: Prescription
    # The earlier visit's own five fields. CMED sent them when that visit
    # happened, so they name it exactly; without them (the older shape, a date
    # only) the visit cannot be told apart from another on the same day, and is
    # filed under UNKNOWN rather than under today's doctor.
    doctor_id: Optional[str] = None
    cmed_hospital_id: Optional[str] = None
    start_time: Optional[str] = None

    @property
    def identified(self) -> bool:
        return bool(self.doctor_id and self.cmed_hospital_id and self.start_time)


@dataclass
class PatientInformation:
    demographics: Demographics
    paramedic: Optional[Paramedic]
    previous: Optional[PreviousVisit]
    female_details: Dict[str, Any]
    male_details: Dict[str, Any]


# ------------------------------------------------------------------ readers

def demographics(body: Dict[str, Any]) -> Demographics:
    d = body.get("demographics") if isinstance(body.get("demographics"), dict) else {}
    return Demographics(
        full_name=text(d.get("name") or d.get("full_name"), 200),
        sex=sex_of(d.get("sex")),
        age_years=whole(d.get("age_years"), 0, 130),
        date_of_birth=day(d.get("date_of_birth")),
        phone=text(d.get("phone"), 40),
        address=text(d.get("address"), 500),
    )


def paramedic(body: Dict[str, Any]) -> Optional[Paramedic]:
    p = body.get("paramedic")
    if not isinstance(p, dict) or not p:
        return None
    systolic, diastolic = blood_pressure(p.get("blood_pressure"))
    return Paramedic(
        recorded_at=moment(p.get("recorded_at")),
        weight_kg=number(p.get("weight_kg"), 0.5, 499),
        height_cm=number(p.get("height_cm"), 20, 299),
        blood_pressure=text(p.get("blood_pressure"), 20),
        systolic=systolic,
        diastolic=diastolic,
        pulse_bpm=whole(p.get("pulse_bpm"), 10, 300),
        temperature_c=number(p.get("temperature_c"), 25, 45),
        spo2_percent=whole(p.get("spo2_percent"), 0, 100),
        notes=text(p.get("notes")),
        other={k: v for k, v in p.items() if k not in PARAMEDIC_COLUMNS},
    )


def _strings(value: Any) -> List[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    out = []
    for entry in value:
        if isinstance(entry, dict):
            entry = entry.get("text") or entry.get("name") or ""
        entry = text(entry, 500)
        if entry:
            out.append(entry)
    return out


def prescription(body: Dict[str, Any]) -> Prescription:
    """API 3's prescription, or the one inside API 2's previous_visit."""
    items = []
    for entry in body.get("items") or []:
        if not isinstance(entry, dict) or not text(entry.get("drug")):
            continue
        items.append(Item(line_no=len(items) + 1, drug=text(entry.get("drug"), 300),
                          dose=text(entry.get("dose"), 200),
                          frequency=text(entry.get("frequency"), 200),
                          duration=text(entry.get("duration"), 200),
                          instructions=text(entry.get("instructions"), 1000)))
    return Prescription(
        issued_at=moment(body.get("issued_at")),
        advice=text(body.get("advice")),
        follow_up_date=day(body.get("follow_up")),
        notes=text(body.get("notes")),
        items=items,
        diagnoses=_strings(body.get("diagnoses")),
        investigations=_strings(body.get("investigations")),
        raw=body,
    )


def previous_visit(body: Dict[str, Any]) -> Optional[PreviousVisit]:
    """API 2's previous_visit: null for a first visit (SRS-CRI-02)."""
    p = body.get("previous_visit")
    if not isinstance(p, dict):
        return None
    when = day(p.get("date"))
    if when is None:
        return None
    inner = p.get("prescription") if isinstance(p.get("prescription"), dict) else {}
    merged = dict(inner)
    # Diagnoses and notes may sit beside the prescription rather than in it.
    merged.setdefault("diagnoses", p.get("diagnoses"))
    merged.setdefault("notes", p.get("notes"))
    start = text(p.get("start_time"), 64)
    if start:
        # The same canonical form as the live visit's own start_time, so the
        # earlier visit is found however the sender wrote the moment.
        import confirmation
        try:
            start = confirmation.canonical_start(start)[0]
        except ValueError:
            pass
    return PreviousVisit(visit_date=when, prescription=prescription(merged),
                         doctor_id=text(p.get("doctor_id"), 64),
                         cmed_hospital_id=text(p.get("hospital_id"), 64),
                         start_time=start)


def patient_information(body: Dict[str, Any]) -> PatientInformation:
    return PatientInformation(
        demographics=demographics(body),
        paramedic=paramedic(body),
        previous=previous_visit(body),
        female_details=body.get("female_details") if isinstance(
            body.get("female_details"), dict) else {},
        male_details=body.get("male_details") if isinstance(
            body.get("male_details"), dict) else {},
    )


def previous_start_time(visit_date: date, live_start_time: str) -> str:
    """
    An older visit has a date but no time. It is filed at midnight in the
    same zone as the live visit, so it always sorts before any real visit.
    """
    offset = live_start_time[19:] if len(live_start_time) > 19 else "+06:00"
    return f"{visit_date.isoformat()}T00:00:00{offset}"


# ------------------------------------------------------------------ fingerprint

# Who an older visit is filed under when CMED did not say (the date-only shape).
UNKNOWN = "UNKNOWN"


def fingerprint_of(*, items: List[Dict[str, Any]], diagnoses: List[str],
                   investigations: List[str], advice: Optional[str],
                   follow_up: Optional[str], notes: Optional[str]) -> str:
    """
    What a prescription says, as 64 hex characters (CMED_DATA_CONTRACT.md).

    Only what the doctor prescribed counts: the medicines in their order, the
    diagnoses, the investigations, the advice, the follow-up date and the
    notes. When it was issued, the order of the keys, spacing and any field not
    listed here do not, so a prescription sent again is recognised as the same
    one and a real change is never mistaken for a repeat.
    """
    def line(i: Dict[str, Any]) -> Dict[str, Any]:
        return {k: i.get(k) for k in ("drug", "dose", "frequency", "duration", "instructions")}

    canonical = {
        "items": [line(i) for i in items],
        "diagnoses": list(diagnoses),
        "investigations": list(investigations),
        "advice": advice,
        "follow_up": follow_up,
        "notes": notes,
    }
    data = json.dumps(canonical, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def fingerprint(p: Prescription) -> str:
    return fingerprint_of(
        items=[{"drug": i.drug, "dose": i.dose, "frequency": i.frequency,
                "duration": i.duration, "instructions": i.instructions} for i in p.items],
        diagnoses=p.diagnoses, investigations=p.investigations, advice=p.advice,
        follow_up=p.follow_up_date.isoformat() if p.follow_up_date else None,
        notes=p.notes)


__all__ = ["Demographics", "Paramedic", "Item", "Prescription", "PreviousVisit",
           "PatientInformation", "patient_information", "prescription", "previous_visit",
           "demographics", "paramedic", "previous_start_time", "blood_pressure", "sex_of",
           "fingerprint", "fingerprint_of", "UNKNOWN"]
