"""
The Channel A contract with CMED: what the page sends, and what it gets back.

SRS 3.2 §6.1 and Appendix A. Everything the page sends is parsed here and every
reply is shaped here, so the wire format lives in one file and the rest of the
recorder deals in checked values.

The page is untrusted input (CON-09). Parsing therefore refuses rather than
repairs: a trigger with a missing or malformed field is answered with a code
the page can act on, and nothing is recorded under a guessed value.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date as Date, datetime
from typing import Any, Dict, Optional, Tuple

IDENTIFIER = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
REQUEST_ID_MAX = 128

# code -> (status, message a doctor may see). Pages act on the code, never the
# message (SRS-IF1-09), so the wording here can change without notice.
CODES: Dict[str, Tuple[int, str]] = {
    # Appendix A.1 and A.2 - the codes CMED sees for its two commands.
    "RECORDING_STARTED": (200, "Recording started."),
    "RECORDING_PROVISIONAL": (202, "Recording. Checking permission with the server."),
    "GATE_ARMED": (200, "This consultation will end when the next patient is opened."),
    "GATE_ALREADY_ARMED": (200, "This consultation was already marked as finished."),
    "MISSING_FIELD": (400, "A required field is missing or not in the expected format."),
    "INVALID_IDENTIFIER": (400, "An identifier contains characters that are not allowed."),
    "AUTHORISATION_FAILED": (401, "This recording could not be authorised."),
    "CLINIC_MISMATCH": (401, "This PC is registered to a different clinic."),
    "ORIGIN_NOT_ALLOWED": (403, "This page is not allowed to control the recorder."),
    "DOCTOR_NOT_AT_CLINIC": (404, "This doctor is not registered at this clinic."),
    "GATE_NOT_ARMED": (409, "The current consultation has not been completed yet."),
    "SESSION_ALREADY_ACTIVE": (409, "This consultation is already being recorded."),
    "NO_ACTIVE_SESSION": (409, "Nothing is being recorded."),
    "PATIENT_MISMATCH": (409, "That signal belongs to a different consultation."),
    "DEVICE_NOT_ENROLLED": (423, "This PC is not registered with AIMS LAB."),
    "AGENT_NOT_READY": (503, "The recorder is not ready. Try again in a moment."),
    # Outside the CMED contract: replies to the commands CMED does not use
    # (status, stop, pause, resume, doctors) and to frames that are not commands.
    "OK": (200, ""),
    "REFUSED": (409, "The recorder refused that request."),
    "UNKNOWN_COMMAND": (400, "Unknown command."),
    "MALFORMED_MESSAGE": (400, "The message could not be read."),
}

# The commands whose replies must stay inside Appendix A (SRS-IF1-10).
CMED_COMMANDS = frozenset({"start", "prescription_built"})


class ProtocolError(Exception):
    """A request the recorder refuses, carrying the code the page will see."""

    def __init__(self, code: str, message: Optional[str] = None, *,
                 field: Optional[str] = None):
        if code not in CODES:
            raise ValueError(f"unknown reply code {code!r}")
        self.code = code
        self.status = CODES[code][0]
        self.field = field
        super().__init__(message or CODES[code][1])


@dataclass(frozen=True)
class Trigger:
    """API 1: the five fields that identify one consultation (SRS §6.1.3)."""
    patient_id: str
    doctor_id: str
    hospital_id: str          # CMED's clinic identifier; mapped by the server
    start_time: str           # exactly as CMED's server wrote it (SRS-CNF-02)
    date: str

    @property
    def start_at(self) -> datetime:
        return datetime.fromisoformat(self.start_time)

    def fields(self) -> Dict[str, str]:
        return {"patient_id": self.patient_id, "doctor_id": self.doctor_id,
                "hospital_id": self.hospital_id, "start_time": self.start_time,
                "date": self.date}


@dataclass(frozen=True)
class PrescriptionBuilt:
    """API 3, part one: arm the gate for the open consultation (SRS §6.1.5)."""
    patient_id: str
    session_id: str
    occurred_at: Optional[str]


def _required_text(body: Dict[str, Any], name: str) -> str:
    value = body.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ProtocolError("MISSING_FIELD", f"'{name}' is required.", field=name)
    return value.strip()


def _identifier(body: Dict[str, Any], name: str) -> str:
    value = _required_text(body, name)
    if not IDENTIFIER.match(value):
        raise ProtocolError("INVALID_IDENTIFIER",
                            f"'{name}' may contain only letters, digits, '_' and '-', "
                            f"up to 64 characters.", field=name)
    return value


def parse_trigger(message: Dict[str, Any]) -> Trigger:
    """
    Read API 1. Refuses anything that is not exactly five usable fields.

    `start_time` is kept character for character: it is matched against the
    same value in CMED's API 2, and any normalisation here would break that
    match for a recording that is perfectly genuine.
    """
    body = message.get("trigger")
    if not isinstance(body, dict):
        raise ProtocolError("MISSING_FIELD", "'trigger' is required.", field="trigger")

    patient_id = _identifier(body, "patient_id")
    doctor_id = _identifier(body, "doctor_id")
    hospital_id = _identifier(body, "hospital_id")

    start_time = _required_text(body, "start_time")
    try:
        parsed = datetime.fromisoformat(start_time)
    except ValueError:
        parsed = None
    if parsed is None or parsed.tzinfo is None:
        raise ProtocolError("MISSING_FIELD",
                            "'start_time' must be a date and time with its time zone, "
                            "for example 2026-09-13T10:14:32+06:00.", field="start_time")

    day = _required_text(body, "date")
    try:
        valid_day = bool(_DATE.match(day)) and Date.fromisoformat(day) is not None
    except ValueError:
        valid_day = False
    if not valid_day:
        raise ProtocolError("MISSING_FIELD", "'date' must be YYYY-MM-DD.", field="date")

    return Trigger(patient_id=patient_id, doctor_id=doctor_id, hospital_id=hospital_id,
                   start_time=start_time, date=day)


def parse_prescription_built(message: Dict[str, Any]) -> PrescriptionBuilt:
    patient_id = _identifier(message, "patient_id")
    session_id = _identifier(message, "session_id")
    occurred = message.get("occurred_at")
    return PrescriptionBuilt(patient_id=patient_id, session_id=session_id,
                             occurred_at=occurred if isinstance(occurred, str) else None)


def request_id_of(message: Any) -> Optional[str]:
    """The page's own correlation id, echoed back untouched when it is sane."""
    if not isinstance(message, dict):
        return None
    value = message.get("request_id")
    if isinstance(value, str) and 0 < len(value) <= REQUEST_ID_MAX:
        return value
    return None


def reply(command: str, code: str, *, request_id: Optional[str] = None,
          data: Optional[Dict[str, Any]] = None,
          message: Optional[str] = None) -> Dict[str, Any]:
    """
    One reply per command (SRS-IF1-07): `ack` below 400, `error` from 400 up.

    For CMED's two commands the code must come from Appendix A (SRS-IF1-10);
    that is asserted here so a stray code fails in tests, not in a clinic.
    """
    status, default = CODES[code]
    if command in CMED_COMMANDS and code in ("OK", "REFUSED", "UNKNOWN_COMMAND",
                                             "MALFORMED_MESSAGE"):
        raise ValueError(f"{code} is not an Appendix A code for {command!r}")
    payload: Dict[str, Any] = {
        "event": "ack" if status < 400 else "error",
        "command": command,
        "status": status,
        "code": code,
        "message": message if message is not None else default,
    }
    if request_id:
        payload["request_id"] = request_id
    if data is not None:
        payload["data"] = data
    return payload


__all__ = ["CODES", "CMED_COMMANDS", "IDENTIFIER", "ProtocolError", "Trigger",
           "PrescriptionBuilt", "parse_trigger", "parse_prescription_built",
           "request_id_of", "reply"]
