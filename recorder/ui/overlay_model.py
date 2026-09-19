"""
What the on-screen control shows, and what its two buttons do.

SRS 3.2 §7.8 (the control) and §7.8a (a patient's refusal). No tkinter here, so
every rule can be tested; ui/overlay.py only draws what this decides.

    Stop   - red circle.     The microphone cuts on the press (SRS-UIX-08); the
                             reason form then closes the session, or Cancel
                             resumes it. "Patient did not consent" comes first
                             and asks once before deleting (SRS-CNS-02, -08).
    Pause  - blue rectangle. Takes effect only after a reason (SRS-UIX-05).
                             While paused the same button resumes.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Optional, Tuple

from core.session_controller import REFUSAL_REASON, STOP_HOLD_REASON

REFUSAL = REFUSAL_REASON

# First is the refusal (SRS-CNS-02). The list itself is OD-07, to be agreed
# with the clinical team; changing it changes no code but this table.
STOP_REASONS: List[Tuple[str, str]] = [
    (REFUSAL, "Patient did not consent"),
    ("consultation_finished", "Consultation finished"),
    ("patient_left", "Patient left the room"),
    ("urgent_interruption", "Emergency or urgent interruption"),
    ("technical_problem", "Problem with the recorder or microphone"),
    ("other", "Other - please describe"),
]

PAUSE_LABELS: Dict[str, str] = {
    "patient_declined": "Patient asked to pause",
    "sensitive_personal_matter": "Sensitive personal matter",
    "non_clinical_interruption": "Interruption not about the patient",
    "other": "Other - please describe",
}

DELETE_QUESTION = ("Delete this recording?", "It cannot be recovered.")

# Returned by confirm_stop when the refusal still needs its one confirmation.
NEEDS_DELETE_CONFIRMATION = "needs_delete_confirmation"

# Plain words for the doctor (SRS-NFU-04). Shown once per consultation and
# never modal (SRS-LVL-06).
NOTES = {
    "session_confirming": "Checking with CMED…",
    "session_confirmed": "Confirmed",
    "session_unconfirmed": "Not confirmed by CMED yet - still recording",
}
MESSAGE_SECONDS = 8.0


def pause_reasons(configured: Iterable[str]) -> List[Tuple[str, str]]:
    return [(r, PAUSE_LABELS.get(r, r.replace("_", " ").capitalize())) for r in configured]


def close_reason_for(reason: str) -> str:
    """
    What the session is closed with. A finished consultation is the normal
    close; anything else is marked so the server raises it for review (SRS-SES-08).
    """
    return "doctor_stopped" if reason == "consultation_finished" else f"doctor_stopped:{reason}"


def check_reason(reason: Optional[str], detail: str,
                 choices: List[Tuple[str, str]]) -> Optional[str]:
    """None if the form may be confirmed, otherwise what to tell the doctor."""
    if not reason:
        return "Choose a reason."
    if reason not in {value for value, _ in choices}:
        return "Choose a reason from the list."
    if reason == "other" and not (detail or "").strip():
        return "Please describe the reason."
    return None


def format_duration(seconds: float) -> str:
    total = int(seconds or 0)
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes:02d}:{secs:02d}"


@dataclass
class OverlayView:
    visible: bool
    headline: str
    note: str
    pause_label: str          # "Pause" or "Resume"
    stop_enabled: bool
    pause_enabled: bool


class OverlayState:
    """Turns the controller's status and events into what the control shows."""

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self.session_id: Optional[str] = None
        self.note = ""
        self.shown: set = set()
        self.form_open = False
        self.message = ""
        self.message_until = 0.0

    def _say(self, text: str) -> None:
        """A message that outlives the recording, such as why it stopped."""
        self.message = text
        self.message_until = self._clock() + MESSAGE_SECONDS

    def on_event(self, event: str, data: Dict[str, Any]) -> None:
        session_id = data.get("session_id")
        if event == "recording_started":
            self.session_id, self.note, self.shown = session_id, "", set()
            self.message_until = 0.0
            return
        if event in NOTES and session_id in (None, self.session_id):
            self.note = NOTES[event]
            return
        if event == "session_refused":
            self._say("Recording stopped and deleted - the patient did not consent.")
            return
        if event == "recording_stopped" and data.get("reason") == "authorisation_refused":
            self._say("Not recorded - AIMS LAB did not authorise this recording, "
                      "and it has been deleted.")
            return
        if event == "integrity_alert":
            kind = data.get("alert_type")
            if kind in self.shown:
                return
            if kind == "silent_session":
                self.shown.add(kind)
                self.note = "The microphone may be muted or unplugged."
            elif kind in ("microphone_unavailable", "capture_failed"):
                self.shown.add(kind)
                self.note = "The microphone is not working. Check that it is connected."
            return
        if event == "overlay_message":
            self._say(str(data.get("text", ""))[:200])

    def view(self, status: Dict[str, Any]) -> OverlayView:
        recording = bool(status.get("is_recording"))
        paused = bool(status.get("is_paused"))
        active = recording or paused
        pause = status.get("pause") or {}
        holding = paused and pause.get("reason") == STOP_HOLD_REASON

        if holding:
            headline = "Stopped - choose a reason"
        elif paused:
            headline = "Paused"
        elif recording:
            headline = f"Recording  {format_duration(status.get('duration_seconds', 0))}"
        else:
            headline = ""

        note = self.note
        if active and not note:
            if status.get("authorisation") == "pending":
                note = "Recording - waiting for the AIMS LAB server"
            elif status.get("confirmation") == "confirming":
                note = NOTES["session_confirming"]

        message_on = self._clock() < self.message_until
        if not active and message_on:
            headline, note = self.message, ""
        elif active and message_on:
            note = self.message

        return OverlayView(
            visible=active or self.form_open or message_on,
            headline=headline,
            note=note,
            pause_label="Resume" if paused and not holding else "Pause",
            stop_enabled=active,
            pause_enabled=active and not holding,
        )


Submit = Callable[[Callable[[Any], Awaitable[Any]]], None]


class OverlayActions:
    """What each press and each confirmed form asks the controller to do."""

    def __init__(self, submit: Submit, pause_choices: List[Tuple[str, str]]):
        self._submit = submit
        self.pause_choices = pause_choices
        self.stop_choices = STOP_REASONS

    def press_stop(self) -> None:
        """The microphone cuts now; the form follows (SRS-UIX-08)."""
        self._submit(lambda c: c.hold_for_stop())

    def cancel_stop(self) -> None:
        """Cancelling the whole action resumes recording (SRS-UIX-07)."""
        self._submit(lambda c: c.release_stop_hold())

    def confirm_stop(self, reason: Optional[str], detail: str = "", *,
                     deletion_confirmed: bool = False) -> Optional[str]:
        problem = check_reason(reason, detail, self.stop_choices)
        if problem:
            return problem
        if reason == REFUSAL:
            if not deletion_confirmed:
                return NEEDS_DELETE_CONFIRMATION
            self._submit(lambda c: c.refuse_session())
            return None
        close_reason = close_reason_for(reason)
        text = (detail or "").strip()[:500]
        self._submit(lambda c: c.stop_session(reason=close_reason, detail=text))
        return None

    def confirm_pause(self, reason: Optional[str], detail: str = "") -> Optional[str]:
        problem = check_reason(reason, detail, self.pause_choices)
        if problem:
            return problem
        text = (detail or "").strip()[:500]
        self._submit(lambda c: c.pause_session(reason=reason, reason_detail=text,
                                               authorised_by="", expected_seconds=0))
        return None

    def resume(self) -> None:
        self._submit(lambda c: c.resume_session())


__all__ = ["STOP_REASONS", "PAUSE_LABELS", "DELETE_QUESTION", "NEEDS_DELETE_CONFIRMATION",
           "OverlayView", "OverlayState", "OverlayActions", "pause_reasons",
           "close_reason_for", "check_reason", "format_duration", "REFUSAL"]
