"""
CMED's clinical messages, turned into rows (SRS 3.2 §8.6, §8.7.2).

Pure functions, no database, so every rule can be tested alone. The original
JSON is always kept as well (SRS-DBA-11): anything this module does not
recognise is still in intake_records, and a field is never guessed. A value
that cannot be read becomes NULL rather than an error, because one odd
measurement must not stop a prescription being stored.

Values are read the way clinics in Bangladesh write them, not only the way
the contract spells them: Bangla digits (৯৮.৬), units (58 kg, 98.6 F, 5'2",
120/80 mmHg, 98%, 34 years), day-first dates (13/10/2026), a follow-up as
"7 days" or "৭ দিন পর", sex as F / Male / মহিলা. Only what is certain is
converted - a temperature of 98.6 can only be Fahrenheit; a height of 5.2
could be feet-and-inches or metres, so it is left empty - and a value that
is not understood is kept beside its column (paramedic `other`) as well as
in the stored message. `quality_notes` says what was converted or set
aside; the reply carries it to CMED as warnings, so the message is accepted
and the sender still learns what to fix.
"""
from __future__ import annotations

import calendar
import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

_BP = re.compile(r"^\s*(\d{2,3})\s*[/\\-]\s*(\d{2,3})\s*(?:mm\s*hg|mmhg)?\.?\s*$", re.I)
# Bangla and Arabic-Indic digits, as a clinic's keyboard may type them.
_DIGITS = str.maketrans("০১২৩৪৫৬৭৮৯٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "0123456789" * 3)
_READING = re.compile(r"^\s*([-+]?\d+(?:[.,]\d+)?)\s*(.*?)\s*$")
# Every clinic is in Bangladesh: a time sent without its zone is clinic time.
CLINIC_ZONE = timezone(timedelta(hours=6))

# The paramedic fields that have their own column. The full list is OD-16;
# anything else CMED sends is kept in `other`.
PARAMEDIC_COLUMNS = ("recorded_at", "weight_kg", "height_cm", "blood_pressure",
                     "pulse_bpm", "temperature_c", "spo2_percent", "notes")


def text(value: Any, limit: int = 2000) -> Optional[str]:
    if value is None:
        return None
    value = str(value).strip()
    return value[:limit] if value else None


def plain(value: Any) -> str:
    """The text of a value, with Bangla or Arabic-Indic digits as 0-9."""
    return str(value).translate(_DIGITS).strip()


def reading(value: Any) -> Tuple[Optional[float], str]:
    """(the number, its unit in lower case) of 58, "58", "58 kg", "৯৮.৬°F"."""
    if isinstance(value, bool) or value is None:
        return None, ""
    if isinstance(value, (int, float)):
        return float(value), ""
    found = _READING.match(plain(value))
    if not found:
        return None, ""
    unit = found.group(2).lower().replace("°", "").replace("º", "").strip(" .")
    return float(found.group(1).replace(",", ".")), unit


def number(value: Any, low: float, high: float) -> Optional[float]:
    n, unit = reading(value)
    if n is None or unit:
        return None
    return n if low <= n <= high else None


def whole(value: Any, low: int, high: int) -> Optional[int]:
    n = number(value, low, high)
    return int(round(n)) if n is not None else None


_MONTHS = {name.lower(): n for n, name in enumerate(calendar.month_name) if name}
_MONTHS.update({name.lower(): n for n, name in enumerate(calendar.month_abbr) if name})


def day(value: Any) -> Optional[date]:
    """
    A date: 2026-09-13, 13/09/2026, 13-09-2026, 13.09.2026, 2026/09/13,
    13 Sep 2026, September 13, 2026 - Bangla digits too. Slashed dates are
    read day first, as Bangladesh writes them.
    """
    if not value or isinstance(value, bool):
        return None
    text_ = plain(value)
    try:
        return date.fromisoformat(text_[:10])
    except ValueError:
        pass
    found = re.match(r"^(\d{4})[/.](\d{1,2})[/.](\d{1,2})$", text_)
    if found:
        y, m, d = (int(x) for x in found.groups())
        return _date(y, m, d)
    found = re.match(r"^(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})$", text_)
    if found:
        d, m, y = (int(x) for x in found.groups())
        return _date(y, m, d)
    found = re.match(r"^(\d{1,2})\s+([A-Za-z]+)\.?,?\s+(\d{4})$", text_)
    if found and found.group(2).lower() in _MONTHS:
        return _date(int(found.group(3)), _MONTHS[found.group(2).lower()], int(found.group(1)))
    found = re.match(r"^([A-Za-z]+)\.?\s+(\d{1,2}),?\s+(\d{4})$", text_)
    if found and found.group(1).lower() in _MONTHS:
        return _date(int(found.group(3)), _MONTHS[found.group(1).lower()], int(found.group(2)))
    return None


def _date(y: int, m: int, d: int) -> Optional[date]:
    try:
        return date(y, m, d)
    except ValueError:
        return None


def moment(value: Any) -> Optional[datetime]:
    """A date and time; one sent without its zone is clinic time (+06:00)."""
    if not value or isinstance(value, bool):
        return None
    try:
        parsed = datetime.fromisoformat(plain(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=CLINIC_ZONE)


_FEMALE = {"female", "f", "woman", "w", "girl", "মহিলা", "নারী", "স্ত্রী", "মেয়ে", "মেয়ে"}
_MALE = {"male", "m", "man", "boy", "পুরুষ", "ছেলে"}


def sex_of(value: Any) -> str:
    value = str(value or "").strip().lower().rstrip(".")
    if value in _FEMALE:
        return "female"
    if value in _MALE:
        return "male"
    return "unknown"


def blood_pressure(value: Any) -> Tuple[Optional[int], Optional[int]]:
    match = _BP.match(plain(value or ""))
    if not match:
        return None, None
    return (whole(match.group(1), 30, 300), whole(match.group(2), 10, 250))


# ------------------------------------------------------------------ measurements
#
# Each returns (the value in our unit, or None; a note when something was
# converted or could not be read). The note goes back to CMED as a warning.

_KG = {"": 1, "kg": 1, "kgs": 1, "kilogram": 1, "kilograms": 1, "কেজি": 1,
       "g": 0.001, "gm": 0.001, "gram": 0.001, "grams": 0.001,
       "lb": 0.45359237, "lbs": 0.45359237, "pound": 0.45359237, "pounds": 0.45359237}
_CM = {"cm": 1, "centimetre": 1, "centimeter": 1, "সেমি": 1, "m": 100, "metre": 100,
       "meter": 100, "mm": 0.1, "in": 2.54, "inch": 2.54, "inches": 2.54, '"': 2.54,
       "ইঞ্চি": 2.54, "ft": 30.48, "feet": 30.48, "foot": 30.48, "'": 30.48, "ফুট": 30.48}
_FEET_INCHES = re.compile(r"^(\d+)\s*(?:'|ft|feet|foot|ফুট)\s*(\d+(?:\.\d+)?)?\s*"
                          r"(?:\"|''|in|inch|inches|ইঞ্চি)?$", re.I)
_PER_MIN = {"", "bpm", "/min", "per min", "beats/min", "b/min", "beats per minute", "/m"}
_PERCENT = {"", "%", "percent", "pct"}


def weight_kg(value: Any) -> Tuple[Optional[float], Optional[str]]:
    n, unit = reading(value)
    if n is None or unit not in _KG:
        return None, None if value in (None, "") else "could not be read as a weight; kept as sent"
    kg = round(n * _KG[unit], 1)           # the column keeps one decimal
    if not 0.5 <= kg <= 499:
        return None, f"{plain(value)} is outside 0.5-499 kg; kept as sent"
    return kg, (f"converted from {plain(value)}" if unit not in ("", "kg", "kgs", "kilogram",
                                                                  "kilograms", "কেজি") else None)


def height_cm(value: Any) -> Tuple[Optional[float], Optional[str]]:
    if value in (None, ""):
        return None, None
    feet = _FEET_INCHES.match(plain(value))
    if feet and not isinstance(value, (int, float)):
        cm = round((int(feet.group(1)) * 12 + float(feet.group(2) or 0)) * 2.54, 1)
        return (cm, f"converted from {plain(value)}") if 20 <= cm <= 299 else \
            (None, f"{plain(value)} is outside 20-299 cm; kept as sent")
    n, unit = reading(value)
    if n is None:
        return None, "could not be read as a height; kept as sent"
    if unit == "":
        if 20 <= n <= 299:
            return n, None
        # 1.57 can only be metres. 5.2 might be 5 ft 2 in or 5.2 ft: not guessed.
        if 0.4 <= n < 2.5:
            return round(n * 100, 1), f"read {plain(value)} as metres"
        return None, f"{plain(value)} has no unit and is not a height in cm; kept as sent"
    if unit not in _CM:
        return None, "could not be read as a height; kept as sent"
    cm = round(n * _CM[unit], 1)
    if not 20 <= cm <= 299:
        return None, f"{plain(value)} is outside 20-299 cm; kept as sent"
    return cm, (f"converted from {plain(value)}" if unit not in ("cm", "centimetre",
                                                                  "centimeter", "সেমি") else None)


def temperature_c(value: Any) -> Tuple[Optional[float], Optional[str]]:
    n, unit = reading(value)
    if n is None:
        return None, None if value in (None, "") else "could not be read as a temperature; kept as sent"
    if unit in ("f", "fahrenheit", "degf", "deg f", "ফারেনহাইট") or (unit == "" and 86 <= n <= 113):
        c = round((n - 32) * 5 / 9, 1)
        note = f"converted from {plain(value)}{'' if unit else ' (Fahrenheit)'}"
    elif unit in ("", "c", "celsius", "degc", "deg c", "সেলসিয়াস"):
        c, note = n, None
    else:
        return None, "could not be read as a temperature; kept as sent"
    if not 25 <= c <= 45:
        return None, f"{plain(value)} is not a body temperature; kept as sent"
    return c, note


def per_minute(value: Any, what: str = "pulse") -> Tuple[Optional[int], Optional[str]]:
    n, unit = reading(value)
    if n is None or unit not in _PER_MIN:
        return None, None if value in (None, "") else f"could not be read as a {what}; kept as sent"
    if not 10 <= n <= 300:
        return None, f"{plain(value)} is outside 10-300 per minute; kept as sent"
    return int(round(n)), None


def percent(value: Any) -> Tuple[Optional[int], Optional[str]]:
    n, unit = reading(value)
    if n is None or unit not in _PERCENT:
        return None, None if value in (None, "") else "could not be read as a percentage; kept as sent"
    if unit == "" and 0 < n <= 1 and not float(n).is_integer():
        return int(round(n * 100)), f"read {plain(value)} as a fraction"
    if not 0 <= n <= 100:
        return None, f"{plain(value)} is outside 0-100 %; kept as sent"
    return int(round(n)), None


_AGE = re.compile(r"^(\d+(?:\.\d+)?)\s*(y|yr|yrs|year|years|বছর|বৎসর|"
                  r"m|mo|mos|month|months|মাস|d|day|days|দিন|w|wk|week|weeks|সপ্তাহ)?\b", re.I)


def age_years(value: Any) -> Tuple[Optional[int], Optional[str]]:
    """34, "34", "34 years", "34Y", "2y 3m" (2), "6 months" (0), "১০ দিন" (0)."""
    if value in (None, "") or isinstance(value, bool):
        return None, None
    if isinstance(value, (int, float)):
        return (int(value), None) if 0 <= value <= 130 else (None, f"{value} is outside 0-130")
    found = _AGE.match(plain(value).lower())
    if not found:
        return None, "could not be read as an age; kept as sent"
    n, unit = float(found.group(1)), (found.group(2) or "y").lower()
    if unit in ("m", "mo", "mos", "month", "months", "মাস"):
        years = int(n // 12)
    elif unit in ("d", "day", "days", "দিন", "w", "wk", "week", "weeks", "সপ্তাহ"):
        years = 0
    else:
        years = int(n)
    if not 0 <= years <= 130:
        return None, f"{plain(value)} is outside 0-130 years; kept as sent"
    in_years = unit in ("y", "yr", "yrs", "year", "years", "বছর", "বৎসর") and n == years
    return years, (None if in_years else f"read {plain(value)} as {years} years")


_AFTER = re.compile(r"(\d+)\s*(day|days|d|দিন|week|weeks|wk|w|সপ্তাহ|month|months|mo|m|মাস)", re.I)


def follow_up_of(value: Any, base: Optional[date]) -> Tuple[Optional[date], Optional[str]]:
    """A date, or "7 days" / "2 weeks" / "1 month" / "৭ দিন পর" after the visit."""
    if value in (None, "") or isinstance(value, bool):
        return None, None
    when = day(value)
    if when is not None:
        return when, (None if str(value)[:10] == when.isoformat() else
                      f"read {plain(value)} as {when.isoformat()}")
    found = _AFTER.search(plain(value))
    if found and base is not None:
        n, unit = int(found.group(1)), found.group(2).lower()
        if unit in ("day", "days", "d", "দিন"):
            when = base + timedelta(days=n)
        elif unit in ("week", "weeks", "wk", "w", "সপ্তাহ"):
            when = base + timedelta(weeks=n)
        else:
            month = base.month - 1 + n
            year, month = base.year + month // 12, month % 12 + 1
            when = date(year, month, min(base.day, calendar.monthrange(year, month)[1]))
        return when, f"read {plain(value)} as {when.isoformat()} (after the visit date)"
    return None, "could not be read as a date; kept as sent"


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
class Finding:
    """One thing the doctor recorded: a complaint, a history, an examination finding."""
    section: str
    item: str
    value: Optional[str]
    detail: Dict[str, Any] = field(default_factory=dict)


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
    # The doctor's findings, which come with the prescription (API 3).
    findings: List[Finding] = field(default_factory=list)


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
        sex=sex_of(d.get("sex") or d.get("gender")),
        age_years=age_years(d.get("age_years") if d.get("age_years") not in (None, "")
                            else d.get("age"))[0],
        date_of_birth=day(d.get("date_of_birth") or d.get("dob")),
        phone=text(d.get("phone"), 40),
        address=text(d.get("address"), 500),
    )


def paramedic(body: Dict[str, Any]) -> Optional[Paramedic]:
    p = body.get("paramedic")
    if not isinstance(p, dict) or not p:
        return None
    systolic, diastolic = blood_pressure(p.get("blood_pressure"))
    values = {"weight_kg": weight_kg(p.get("weight_kg"))[0],
              "height_cm": height_cm(p.get("height_cm"))[0],
              "pulse_bpm": per_minute(p.get("pulse_bpm"))[0],
              "temperature_c": temperature_c(p.get("temperature_c"))[0],
              "spo2_percent": percent(p.get("spo2_percent"))[0]}
    other = {k: v for k, v in p.items() if k not in PARAMEDIC_COLUMNS}
    # A value its column could not take stays beside it, as sent.
    for name, value in values.items():
        if value is None and p.get(name) not in (None, ""):
            other[name] = p.get(name)
    if systolic is None and p.get("blood_pressure") not in (None, ""):
        other["blood_pressure"] = p.get("blood_pressure")
    return Paramedic(
        recorded_at=moment(p.get("recorded_at")),
        weight_kg=values["weight_kg"],
        height_cm=values["height_cm"],
        blood_pressure=text(p.get("blood_pressure"), 20),
        systolic=systolic,
        diastolic=diastolic,
        pulse_bpm=values["pulse_bpm"],
        temperature_c=values["temperature_c"],
        spo2_percent=values["spo2_percent"],
        notes=text(p.get("notes")),
        other=other,
    )


def _strings(value: Any) -> List[str]:
    if isinstance(value, (str, dict)):
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


# Other names a prescription line's fields are commonly sent under.
ITEM_NAMES = {"drug": ("drug", "medicine", "medicine_name", "medication", "name", "brand"),
              "dose": ("dose", "dosage", "strength"),
              "frequency": ("frequency", "schedule", "times"),
              "duration": ("duration", "days", "course"),
              "instructions": ("instructions", "instruction", "note", "remarks")}


def _field(entry: Dict[str, Any], name: str) -> Any:
    for key in ITEM_NAMES[name]:
        if entry.get(key) not in (None, ""):
            return entry[key]
    return None


def item_entries(value: Any) -> List[Any]:
    """The lines of a prescription: a list, one object, or medicine names alone."""
    if isinstance(value, dict):
        return [value]
    if isinstance(value, str) and value.strip():
        return [{"drug": value}]
    if isinstance(value, list):
        return [{"drug": v} if isinstance(v, str) else v for v in value]
    return []


# ------------------------------------------------------------------ the doctor's findings
#
# They come with the prescription, in API 3: API 2 is sent the moment the
# patient is opened, before the doctor has written anything. Read from friendly
# keys (below) or from a generic list, `findings`, of {section, item, value,
# detail} - the form a FHIR message is turned into.

SECTIONS = ("chief_complaint", "history", "allergy", "on_examination",
            "systemic_examination", "gynae_obstetric")
# section -> the keys it may be sent under, at the top of the prescription.
SECTION_KEYS = {
    "chief_complaint": ("chief_complaints", "chief_complaint", "complaints", "cc"),
    "history": ("history", "histories"),
    "allergy": ("allergies", "allergy", "allergic_history"),
    "on_examination": ("on_examination", "oe", "o_e", "general_examination"),
    "systemic_examination": ("systemic_examination", "se", "s_e"),
    "gynae_obstetric": ("gynae_obstetric", "gynae_obstetric_history", "gh_oh",
                        "obstetric_history"),
}
# Histories also sent as their own keys: family_history -> history / family.
HISTORY_KEYS = {"family_history": "family", "personal_history": "personal",
                "past_history": "past", "past_medical_history": "past",
                "drug_history": "drug", "social_history": "social",
                "gynaecological_history": "gynaecological"}
_COMPLAINT_NAMES = ("complaint", "name", "text", "problem", "symptom")


def _finding_text(value: Any) -> Optional[str]:
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (list, tuple)):
        parts = [_finding_text(v) for v in value]
        return text("; ".join(p for p in parts if p), 2000)
    if isinstance(value, dict):
        return None
    return text(value, 2000)


def _section_entries(section: str, value: Any) -> List[Finding]:
    """One section's findings, from a dict, a list or a single text."""
    out: List[Finding] = []
    if value in (None, "", [], {}):
        return out
    if section == "chief_complaint":
        for entry in (value if isinstance(value, list) else [value]):
            if isinstance(entry, dict):
                name = next((text(entry.get(k), 500) for k in _COMPLAINT_NAMES
                             if text(entry.get(k))), None)
                if name:
                    out.append(Finding(section, name, _finding_text(entry.get("value")),
                                       {k: v for k, v in entry.items()
                                        if k not in _COMPLAINT_NAMES and k != "value"}))
            elif _finding_text(entry):
                out.append(Finding(section, _finding_text(entry)[:500], None))
        return out
    if isinstance(value, dict):
        for key, entry in value.items():
            if not text(key):
                continue
            if isinstance(entry, dict):
                out.append(Finding(section, text(key, 200), _finding_text(entry.get("value")
                                                                        or entry.get("text")),
                                   {k: v for k, v in entry.items() if k not in ("value", "text")}))
            elif _finding_text(entry):
                out.append(Finding(section, text(key, 200), _finding_text(entry)))
        return out
    for entry in (value if isinstance(value, list) else [value]):
        if isinstance(entry, dict):
            item = text(entry.get("item") or entry.get("type") or entry.get("name")
                        or entry.get("substance"), 200)
            found = _finding_text(entry.get("value") or entry.get("text")
                                  or entry.get("reaction"))
            if item or found:
                out.append(Finding(section, item or section, found,
                                   {k: v for k, v in entry.items()
                                    if k not in ("item", "type", "name", "substance", "value",
                                                 "text")}))
        elif _finding_text(entry):
            out.append(Finding(section, section, _finding_text(entry)))
    return out


def findings_of(body: Dict[str, Any]) -> List[Finding]:
    """Every finding in a prescription, in the order the sections are listed."""
    out: List[Finding] = []
    for section in SECTIONS:
        for key in SECTION_KEYS[section]:
            out.extend(_section_entries(section, body.get(key)))
    for key, item in HISTORY_KEYS.items():
        if _finding_text(body.get(key)):
            out.append(Finding("history", item, _finding_text(body.get(key))))
    for entry in body.get("findings") or []:
        if not isinstance(entry, dict):
            continue
        section = str(entry.get("section") or "").strip().lower().replace("-", "_")
        item = text(entry.get("item"), 500)
        if section in SECTIONS and item:
            out.append(Finding(section, item, _finding_text(entry.get("value")),
                               entry.get("detail") if isinstance(entry.get("detail"), dict)
                               else {}))
    return out


# Who the patient is, beyond sex and date of birth, is not needed for the
# research and is not kept: CMED need not send it, and if it does it is
# removed before the message is stored.
NOT_KEPT = ("name", "full_name", "phone", "mobile", "address", "email")


def minimise(body: Dict[str, Any]) -> Tuple[Dict[str, Any], List[str]]:
    """API 2 without the patient's name, phone or address; and which were sent."""
    d = body.get("demographics")
    if not isinstance(d, dict):
        return body, []
    dropped = [k for k in (d.get("not_stored") or []) if isinstance(k, str)]
    dropped += [k for k in NOT_KEPT if d.get(k) not in (None, "") and k not in dropped]
    if not dropped and "not_stored" not in d:
        return body, []
    kept = {k: v for k, v in d.items() if k not in NOT_KEPT and k != "not_stored"}
    return {**body, "demographics": kept}, dropped


def has_findings(body: Dict[str, Any]) -> bool:
    return bool(findings_of(body))


def prescription(body: Dict[str, Any], base: Optional[date] = None) -> Prescription:
    """API 3's prescription, or the one inside API 2's previous_visit."""
    items = []
    for entry in item_entries(body.get("items")):
        if not isinstance(entry, dict) or not text(_field(entry, "drug")):
            continue                     # a line with no medicine name: reported, not stored
        items.append(Item(line_no=len(items) + 1, drug=text(_field(entry, "drug"), 300),
                          dose=text(_field(entry, "dose"), 200),
                          frequency=text(_field(entry, "frequency"), 200),
                          duration=text(_field(entry, "duration"), 200),
                          instructions=text(_field(entry, "instructions"), 1000)))
    if base is None:
        base = day(body.get("date"))
    return Prescription(
        issued_at=moment(body.get("issued_at")),
        advice=text(body.get("advice")),
        follow_up_date=follow_up_of(body.get("follow_up"), base)[0],
        notes=text(body.get("notes")),
        items=items,
        diagnoses=_strings(body.get("diagnoses")),
        investigations=_strings(body.get("investigations")),
        raw=body,
        findings=findings_of(body),
    )


_IDENTITY = ("doctor_id", "hospital_id", "start_time")
_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _canonical(start: Any) -> Optional[str]:
    """A start time in the one form visits are matched by, or None."""
    import confirmation
    try:
        return confirmation.canonical_start(str(start))[0]
    except (ValueError, TypeError):
        return None


def previous_visit_issue(body: Dict[str, Any]) -> Optional[str]:
    """
    Why API 2's previous_visit cannot be used, or None. It is set aside, never
    the reason today's information is refused: today's visit is what confirms
    the recording.
    """
    p = body.get("previous_visit")
    if not isinstance(p, dict):
        return None
    if day(p.get("date")) is None and _canonical(p.get("start_time")) is None:
        return "previous_visit.date is missing or not a date"
    if not isinstance(p.get("prescription"), dict) and not p.get("diagnoses"):
        return "previous_visit.prescription is missing"
    if p.get("patient_id") not in (None, "") and str(p.get("patient_id")).strip() != \
            str(body.get("patient_id") or "").strip():
        return "previous_visit.patient_id is another patient"
    start = _canonical(p.get("start_time")) if p.get("start_time") else None
    now = _canonical(body.get("start_time"))
    if start and now and start >= now:
        return "previous_visit is this visit or a later one, not an earlier one"
    when = day(p.get("date"))
    today = day(body.get("date"))
    if when and today and when > today:
        return "previous_visit.date is after this visit"
    return None


def previous_visit(body: Dict[str, Any]) -> Optional[PreviousVisit]:
    """API 2's previous_visit: null for a first visit (SRS-CRI-02)."""
    p = body.get("previous_visit")
    if not isinstance(p, dict) or previous_visit_issue(body):
        return None
    start = _canonical(p.get("start_time")) if p.get("start_time") else None
    when = day(p.get("date")) or (date.fromisoformat(start[:10]) if start else None)
    if when is None:
        return None
    inner = p.get("prescription") if isinstance(p.get("prescription"), dict) else {}
    merged = dict(inner)
    # Diagnoses and notes may sit beside the prescription rather than in it.
    merged.setdefault("diagnoses", p.get("diagnoses"))
    merged.setdefault("notes", p.get("notes"))
    doctor = text(p.get("doctor_id"), 64)
    clinic = text(p.get("hospital_id"), 64)
    # Half a name is never used: a doctor without a time could file the
    # prescription against the wrong visit. Without the whole name the visit
    # is kept by its date alone, under UNKNOWN, as the older shape is.
    whole_name = bool(doctor and clinic and start and _SAFE_ID.match(doctor)
                      and _SAFE_ID.match(clinic))
    return PreviousVisit(visit_date=when, prescription=prescription(merged, base=when),
                         doctor_id=doctor if whole_name else None,
                         cmed_hospital_id=clinic if whole_name else None,
                         start_time=start if whole_name else None)


# ------------------------------------------------------------------ what was adjusted

def quality_notes(kind: str, body: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Every value that was converted, read in another form, or could not be
    used - for the reply's warnings. The message is accepted either way.
    """
    notes: List[Dict[str, Any]] = []

    def add(where: str, sent: Any, note: Optional[str]) -> None:
        if note:
            notes.append({"field": where, "sent": sent, "note": note})

    if kind == "patient_information":
        d = body.get("demographics")
        if not isinstance(d, dict):
            add("demographics", d, "missing; today's visit is stored without demographics")
            d = {}
        sex = d.get("sex") if d.get("sex") not in (None, "") else d.get("gender")
        if sex not in (None, "") and sex_of(sex) == "unknown":
            add("demographics.sex", sex, "not female or male; stored as unknown")
        elif sex not in (None, "") and str(sex) != sex_of(sex):
            add("demographics.sex", sex, f"read as {sex_of(sex)}")
        age = d.get("age_years") if d.get("age_years") not in (None, "") else d.get("age")
        add("demographics.age_years", age, age_years(age)[1])
        dob = d.get("date_of_birth") or d.get("dob")
        if dob and day(dob) is None:
            add("demographics.date_of_birth", dob, "could not be read as a date; kept as sent")
        elif dob and str(dob)[:10] != day(dob).isoformat():
            add("demographics.date_of_birth", dob, f"read as {day(dob).isoformat()}")
        p = body.get("paramedic")
        if p is not None and not isinstance(p, dict):
            add("paramedic", p, "not an object; kept as sent")
        if isinstance(p, dict):
            for name, reader in (("weight_kg", weight_kg), ("height_cm", height_cm),
                                 ("temperature_c", temperature_c), ("pulse_bpm", per_minute),
                                 ("spo2_percent", percent)):
                if p.get(name) not in (None, ""):
                    add(f"paramedic.{name}", p.get(name), reader(p.get(name))[1])
            bp = p.get("blood_pressure")
            if bp not in (None, "") and blood_pressure(bp) == (None, None):
                add("paramedic.blood_pressure", bp,
                    "could not be read as systolic/diastolic; kept as text")
            if p.get("recorded_at") and moment(p.get("recorded_at")) is None:
                add("paramedic.recorded_at", p.get("recorded_at"), "could not be read as a time")
        if has_findings(body):
            add("findings", [f.section for f in findings_of(body)],
                "the doctor's findings belong in API 3, with the prescription; here they "
                "are kept in the stored message only")
        if "previous_visit" not in body:
            add("previous_visit", None, "missing; read as null (no previous visit). "
                                        "Send null when there is none")
        issue = previous_visit_issue(body)
        if issue:
            add("previous_visit", None, issue + "; the previous visit was not used, "
                                                "today's information was stored")
        elif isinstance(body.get("previous_visit"), dict):
            pv = body["previous_visit"]
            given = [n for n in _IDENTITY if pv.get(n) not in (None, "")]
            if given and len(given) < len(_IDENTITY):
                add("previous_visit", {n: pv.get(n) for n in given},
                    "send doctor_id, hospital_id and start_time together; kept by its date "
                    "alone")
            if isinstance(pv.get("prescription"), dict):
                notes.extend(_prescription_notes(pv["prescription"], "previous_visit.prescription",
                                                 day(pv.get("date")), previous=True))
    else:
        if body.get("issued_at") in (None, ""):
            add("issued_at", None, "missing; the prescription is stored without an issue time")
        elif moment(body.get("issued_at")) is None:
            add("issued_at", body.get("issued_at"), "could not be read as a time; the "
                                                    "prescription is stored without it")
        elif not re.search(r"(Z|[+-]\d{2}:?\d{2})$", plain(body.get("issued_at"))):
            add("issued_at", body.get("issued_at"), "no time zone; read as clinic time (+06:00)")
        notes.extend(_prescription_notes(body, "", day(body.get("date"))))
    return notes


def _prescription_notes(p: Dict[str, Any], prefix: str, base: Optional[date],
                        previous: bool = False) -> List[Dict[str, Any]]:
    notes: List[Dict[str, Any]] = []
    where = (prefix + ".") if prefix else ""
    for name in ("items", "diagnoses", "investigations"):
        value = p.get(name)
        if value is None and not previous:
            notes.append({"field": where + name, "sent": None,
                          "note": "missing; read as none. Send [] when there are none"})
        elif isinstance(value, (str, dict)):
            notes.append({"field": where + name, "sent": value, "note": "read as a list of one"})
    for n, entry in enumerate(item_entries(p.get("items"))):
        if not isinstance(entry, dict) or not text(_field(entry, "drug")):
            notes.append({"field": f"{where}items[{n}]", "sent": entry,
                          "note": "no medicine name; this line was not stored, the rest was"})
        elif not entry.get("drug"):
            notes.append({"field": f"{where}items[{n}]", "sent": None,
                          "note": "the medicine name was read from another field; send 'drug'"})
    fu = p.get("follow_up")
    note = follow_up_of(fu, base)[1]
    if note:
        notes.append({"field": where + "follow_up", "sent": fu, "note": note})
    return notes


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


def content_fingerprint(p: Prescription) -> str:
    """
    The fingerprint, and the doctor's findings when there are any: what decides
    whether a prescription sent again is a new version. Without findings it is
    the fingerprint itself, so nothing CMED computes changes; with them, a
    corrected complaint or examination is a new version too.
    """
    fp = fingerprint(p)
    if not p.findings:
        return fp
    findings = [[f.section, f.item, f.value, f.detail] for f in p.findings]
    data = json.dumps({"fingerprint": fp, "findings": findings}, sort_keys=True,
                      separators=(",", ":"), ensure_ascii=False, default=str)
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


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
           "fingerprint", "fingerprint_of", "UNKNOWN", "quality_notes", "previous_visit_issue",
           "day", "moment", "follow_up_of", "weight_kg", "height_cm", "temperature_c",
           "per_minute", "percent", "age_years", "plain", "reading", "Finding",
           "findings_of", "content_fingerprint", "SECTIONS", "minimise", "NOT_KEPT"]
