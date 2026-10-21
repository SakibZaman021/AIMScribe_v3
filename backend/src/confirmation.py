"""
Confirming a recording against CMED's server (SRS 3.2 §5.6).

When a doctor opens a patient, CMED's page sends the trigger to the recorder
and CMED's server sends API 2 to us. Both carry the same five fields, and
when the two meet the recording is tied to CMED's clinical record.

Meeting is enrichment, never admission. A recording started by CMED's trigger
is an authentic consultation whatever happens to API 2: it is archived and kept
whether API 2 arrives in a second, a month later or never. Deleting it for want
of API 2 turned every CMED outage into lost consultations.

This module holds the rules that do not need a database: what a valid set of
five fields is, and the time limits.
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from typing import Any, Dict, Optional, Tuple
from zoneinfo import ZoneInfo

import integrity

logger = logging.getLogger(__name__)

# How far back either side looks for the other. The five fields name one
# consultation to the second, so a long look-back cannot pair the wrong ones;
# a short one only loses the pairing when a laptop or CMED was offline. Both
# directions use the same bound, so which message arrives first never matters.
MATCH_LOOKBACK = timedelta(days=3650)
NOTICE_MATCH_WINDOW = MATCH_LOOKBACK           # SRS-CNF-03: API 2 first, recording later
UNCONFIRMED_LIFETIME = MATCH_LOOKBACK          # SRS-CNF-09: recording first, API 2 later
CONFIRM_DEADLINE = timedelta(minutes=2)        # SRS-CNF-08: then shown "unconfirmed"

# What enters the dataset: a recording CMED's own server vouched for (API 2,
# sent with that hospital's token, for the consultation this PC signed). A PC's
# signature proves which PC recorded; only CMED proves a real consultation took
# place. Everything else is kept - nothing is erased but a patient's refusal
# (SRS-CNS-04) - and held in the segment store until API 2 arrives or an
# administrator confirms it by name. "expired" remains for rows erased by
# older servers.
ARCHIVABLE = frozenset({"confirmed"})
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


@lru_cache(maxsize=4)
def _zone(name: str):
    try:
        return ZoneInfo(name)
    except Exception:
        # No time-zone database on this machine (Windows without tzdata). The
        # clinics are in Bangladesh, which has kept +06:00 all year since 2009.
        logger.warning("Time zone %s is not available here; using +06:00", name)
        return timezone(timedelta(hours=6))


def clinic_zone():
    return _zone(os.getenv("AIMS_DEFAULT_TIMEZONE", "Asia/Dhaka") or "Asia/Dhaka")


def canonical_start(value: str) -> Tuple[str, str]:
    """
    One way to write one moment: the clinic's own offset, whole seconds.

        2026-09-13T10:14:32+06:00        unchanged
        2026-09-13T04:14:32.000Z         -> 2026-09-13T10:14:32+06:00
        2026-09-13T05:14:32+07:00        -> 2026-09-13T10:14:32+06:00

    The five fields are compared as text everywhere (they name the visit), so
    a FHIR library that re-serialises the time must not make it another visit.
    Values already in this form - every one stored so far - do not change.
    Returns (start_time, the clinic-local date). ValueError if not a time with
    an offset.
    """
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise ValueError("must be a date and time with its time zone")
    if parsed.tzinfo is None:
        raise ValueError("must be a date and time with its time zone")
    local = parsed.astimezone(clinic_zone()).replace(microsecond=0)
    return local.isoformat(timespec="seconds"), local.date().isoformat()


def parse_visit(body: Any, *, strict_date: bool = False) -> Visit:
    """
    Read the five fields from a trigger forwarded by a recorder, or from API 2/3.

    start_time is written in its canonical form (canonical_start), and the date
    is the clinic-local day of that moment. A different date in the message is
    refused when `strict_date` (CMED's server messages, where a wrong date is a
    bug to fix) and corrected otherwise (the recorder's trigger: refusing it
    would refuse the recording, and every message for this visit gets the same
    derived date, so they still meet).
    """
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

    try:
        start_time, local_day = canonical_start(_text(body, "start_time"))
    except ValueError:
        raise FieldError("MISSING_FIELD", "start_time",
                         "'start_time' must be a date and time with its time zone")

    visit_date = _text(body, "date")
    try:
        ok = bool(_DATE.match(visit_date)) and date.fromisoformat(visit_date) is not None
    except ValueError:
        ok = False
    if not ok:
        raise FieldError("MISSING_FIELD", "date", "'date' must be YYYY-MM-DD")
    if visit_date != local_day:
        if strict_date:
            raise FieldError("MISSING_FIELD", "date",
                             f"'date' must be the clinic's local date of start_time "
                             f"({local_day}), not {visit_date}")
        logger.warning("A trigger's date %s is not the local day of its start_time; "
                       "using %s", visit_date, local_day)
        visit_date = local_day

    return Visit(patient_id=ids["patient_id"], doctor_id=ids["doctor_id"],
                 cmed_hospital_id=ids["hospital_id"], start_time=start_time,
                 visit_date=visit_date)


def current_state(stored: str, opened_at: Optional[datetime], now: datetime) -> str:
    """A pending recording becomes unconfirmed two minutes after it opened."""
    if stored == "pending" and opened_at is not None and now - opened_at >= CONFIRM_DEADLINE:
        return "unconfirmed"
    return stored


__all__ = ["Visit", "FieldError", "parse_visit", "canonical_start", "clinic_zone", "current_state", "FIVE_FIELDS",
           "MATCH_LOOKBACK", "NOTICE_MATCH_WINDOW", "CONFIRM_DEADLINE",
           "UNCONFIRMED_LIFETIME",
           "ARCHIVABLE", "WAITING"]
