"""
Confirming a recording against CMED's server (SRS 3.2 §5.6).

When a doctor opens a patient, CMED's page sends the trigger to the recorder
and CMED's server sends API 2 to us. Both carry the same five fields. A
recording is admitted to the dataset only when the two meet.

This module holds the rules that do not need a database: what a valid set of
five fields is, and the time limits.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Dict, Optional

import integrity

NOTICE_MATCH_WINDOW = timedelta(minutes=5)     # SRS-CNF-03: an unclaimed API 2 expires
CONFIRM_DEADLINE = timedelta(minutes=2)        # SRS-CNF-08: then "unconfirmed"
UNCONFIRMED_LIFETIME = timedelta(hours=24)     # SRS-CNF-09: then deleted

ARCHIVABLE = frozenset({"confirmed", "legacy"})
WAITING = frozenset({"pending", "unconfirmed"})

_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
FIVE_FIELDS = ("patient_id", "doctor_id", "hospital_id", "start_time", "date")


class FieldError(ValueError):
    """A five-field problem, carrying the reply code the caller will see."""

    def __init__(self, code: str, field: str, message: str):
        super().__init__(message)
        self.code = code
        self.field = field


@dataclass(frozen=True)
class Visit:
    """The five fields that name one consultation, exactly as CMED sent them."""
    patient_id: str
    doctor_id: str
    cmed_hospital_id: str
    start_time: str
    visit_date: str

    @property
    def day(self) -> date:
        return date.fromisoformat(self.visit_date)

    def digest(self) -> bytes:
        """Stands in for the visit where no patient identifier may be kept."""
        return integrity.sha256_bytes(integrity.canonical_json({
            "patient_id": self.patient_id, "doctor_id": self.doctor_id,
            "hospital_id": self.cmed_hospital_id, "start_time": self.start_time,
            "date": self.visit_date,
        }))


def _text(body: Dict[str, Any], name: str) -> str:
    value = body.get(name)
    if not isinstance(value, str) or not value.strip():
        raise FieldError("MISSING_FIELD", name, f"'{name}' is required")
    return value.strip()


def parse_visit(body: Any) -> Visit:
    """Read the five fields from a trigger forwarded by a recorder, or from API 2/3."""
    if not isinstance(body, dict):
        raise FieldError("MISSING_FIELD", "body", "expected a JSON object")

    ids = {}
    for name in ("patient_id", "doctor_id", "hospital_id"):
        value = _text(body, name)
        try:
            ids[name] = integrity.safe_identifier(value, field=name)
        except ValueError:
            raise FieldError("INVALID_IDENTIFIER", name,
                             f"'{name}' may contain only letters, digits, '_' and '-', "
                             f"up to 64 characters")

    start_time = _text(body, "start_time")
    try:
        parsed: Optional[datetime] = datetime.fromisoformat(start_time)
    except ValueError:
        parsed = None
    if parsed is None or parsed.tzinfo is None:
        raise FieldError("MISSING_FIELD", "start_time",
                         "'start_time' must be a date and time with its time zone")

    visit_date = _text(body, "date")
    try:
        ok = bool(_DATE.match(visit_date)) and date.fromisoformat(visit_date) is not None
    except ValueError:
        ok = False
    if not ok:
        raise FieldError("MISSING_FIELD", "date", "'date' must be YYYY-MM-DD")

    return Visit(patient_id=ids["patient_id"], doctor_id=ids["doctor_id"],
                 cmed_hospital_id=ids["hospital_id"], start_time=start_time,
                 visit_date=visit_date)


def current_state(stored: str, opened_at: Optional[datetime], now: datetime) -> str:
    """A pending recording becomes unconfirmed two minutes after it opened."""
    if stored == "pending" and opened_at is not None and now - opened_at >= CONFIRM_DEADLINE:
        return "unconfirmed"
    return stored


__all__ = ["Visit", "FieldError", "parse_visit", "current_state", "FIVE_FIELDS",
           "NOTICE_MATCH_WINDOW", "CONFIRM_DEADLINE", "UNCONFIRMED_LIFETIME",
           "ARCHIVABLE", "WAITING"]
