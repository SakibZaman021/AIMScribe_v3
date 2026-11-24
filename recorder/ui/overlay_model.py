"""
What the on-screen control shows, and what its two icons do (3.9.8).

Two small see-through icons, one above the other, and no clock. One tap or a
hover opens an icon's menu; one more click chooses. A tap alone never does
anything.

    Stop   - a square in a circle.
             "Patient did not consent" is offered until CMED's API 2
             (demographics) reaches the server; it really stops the recording
             and deletes it, after one confirmation (SRS-CNS-02, -08).
             "Consultation finished", "Patient left the room" and "Emergency"
             are logged at this second of the audio. The recording continues
             until CMED's API 4 or the next patient, and the menu says so.
    Mark   - two bars in a circle.
             Marks the start of a sensitive part, with a reason. The recording
             continues; the lab removes the part before anyone listens to it,
             and the menu says so. While a part is marked the same icon ends
             it; after two minutes it blinks and asks whether the part is over.

No tkinter here, so every rule can be tested; ui/overlay.py only draws what
this decides.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from core.session_controller import REFUSAL_REASON

REFUSAL = REFUSAL_REASON

STOP = "stop"
MARK = "mark"

STOP_TITLE = ("Your reason is logged. Recording continues until the visit is "
              "closed in CMED.")
STOP_MENU: List[Tuple[str, str]] = [
    (REFUSAL, "Patient did not consent"),
    ("consultation_finished", "Consultation finished"),
    ("patient_left", "Patient left the room"),
    ("urgent_interruption", "Emergency"),
]
# Shown under the refusal once it is closed, so a doctor knows why it is grey.
REFUSAL_CLOSED = "Patient did not consent (closed once CMED sent the patient's details)"

MARK_TITLE = ("Mark sensitive part. Recording continues. This part will be removed "
              "before anyone listens to it.")
MARK_MENU: List[Tuple[str, str]] = [
    ("patient_request", "Patient's request"),
    ("sensitive_personal_matter", "Sensitive personal matter"),
    ("interruption", "Interruption"),
]
SENSITIVE_END = "sensitive_end"
MARKED_TITLE = ("Sensitive part marked ({label}). Recording continues; this part will "
                "be removed before anyone listens to it.")
MARKED_OVER = "Sensitive part over?"
END_LABEL = "Yes, it is over"
# After this long the Mark icon blinks and its menu opens by itself, once.
REMINDER_SECONDS = 120

DELETE_QUESTION = ("Delete this recording?",
                   "The patient did not consent. The recording is deleted and cannot "
                   "be recovered.")
# Returned by choose() when the refusal still needs its one confirmation.
NEEDS_DELETE_CONFIRMATION = "needs_delete_confirmation"

# Plain words for the doctor (SRS-NFU-04), shown briefly beside the icons.
NOTES = {
    "session_confirming": "Checking with CMED…",
    "session_confirmed": "Confirmed",
    "session_unconfirmed": "Not confirmed by CMED yet - still recording",
}
MESSAGE_SECONDS = 6.0


@dataclass
class OverlayView:
    visible: bool
    note: str                       # one line to show beside the icons, or ""
    consent_open: bool              # the refusal can still be chosen
    marked: Optional[str]           # the open sensitive part's reason, words
    alarm: bool                     # marked two minutes or more: blink and ask
    dot: str                        # "recording" | "waiting" | "problem" | "off"


class OverlayState:
    """Turns the controller's status and events into what the control shows."""

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self.session_id: Optional[str] = None
        self.note = ""
        self.shown: set = set()
        self.menu_open = False
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
        if event == "doctor_marked":
            words = dict(STOP_MENU).get(data.get("reason"), "Reason")
            self._say(f"Noted: {words}. Recording continues.")
            return
        if event == "sensitive_started":
            self._say("Marked. This part will be removed before anyone listens to it.")
            return
        if event == "sensitive_ended":
            self._say("Sensitive part ended.")
            return
        if event == "sound_restored" and session_id in (None, self.session_id):
            self.shown.discard("silent_session")
            if self.note.startswith("Microphone gives no signal"):
                self.note = ""
            return
        if event == "session_refused":
            self._say("Recording stopped and deleted - the patient did not consent.")
            return
        if event == "session_held" and session_id in (None, self.session_id):
            self.note = data.get("message") or "Recording. Kept on this PC for now."
            return
        if event == "integrity_alert":
            kind = data.get("alert_type")
            if kind in self.shown:
                return
            if kind == "silent_session":
                self.shown.add(kind)
                self.note = "Microphone gives no signal - is it muted?"
            elif kind in ("microphone_unavailable", "capture_failed"):
                self.shown.add(kind)
                self.note = "The microphone is not working. Check that it is connected."
            elif kind == "microphone_switched":
                device = str(data.get("device") or "")
                self.note = ("Back on the Jabra speakerphone." if "jabra" in device.lower()
                             else "Jabra unplugged - recording on this PC's microphone.")
            return
        if event == "overlay_message":
            self._say(str(data.get("text", ""))[:200])

    def view(self, status: Dict[str, Any]) -> OverlayView:
        active = bool(status.get("is_recording") or status.get("is_paused"))
        sensitive = status.get("sensitive") or None
        marked = (sensitive.get("label") or sensitive.get("reason")) if sensitive else None
        alarm = bool(sensitive) and float(sensitive.get("seconds") or 0) >= REMINDER_SECONDS

        note = self.note
        if active and not note:
            if status.get("authorisation") == "pending":
                note = "Recording - waiting for the AIMS LAB server"
            elif status.get("confirmation") == "confirming":
                note = NOTES["session_confirming"]
        message_on = self._clock() < self.message_until
        if message_on:
            note = self.message

        if not active:
            dot = "off"
        elif "no signal" in self.note or "not working" in self.note:
            dot = "problem"
        elif status.get("authorisation") == "pending":
            dot = "waiting"
        else:
            dot = "recording"

        return OverlayView(
            visible=active or self.menu_open or message_on,
            note=note if (active or message_on) else "",
            consent_open=active and status.get("consent_open", True) is not False,
            marked=marked if active else None,
            alarm=alarm and active,
            dot=dot,
        )


def menu(kind: str, view: OverlayView) -> Tuple[str, List[Tuple[str, str, bool]]]:
    """
    The title and the (value, words, enabled) choices an icon's menu offers now.
    """
    if kind == STOP:
        items = []
        for value, words in STOP_MENU:
            if value == REFUSAL:
                items.append((value, words if view.consent_open else REFUSAL_CLOSED,
                              view.consent_open))
            else:
                items.append((value, words, True))
        return STOP_TITLE, items
    if view.marked:
        title = MARKED_OVER if view.alarm else MARKED_TITLE.format(label=view.marked)
        return title, [(SENSITIVE_END, END_LABEL, True)]
    return MARK_TITLE, [(value, words, True) for value, words in MARK_MENU]


Submit = Callable[[Callable[[Any], Awaitable[Any]]], None]


class OverlayActions:
    """What each choice asks the controller to do."""

    def __init__(self, submit: Submit):
        self._submit = submit

    def choose(self, kind: str, value: str, *, consent_open: bool = True,
               deletion_confirmed: bool = False) -> Optional[str]:
        """
        None when the choice was sent; NEEDS_DELETE_CONFIRMATION when the
        refusal still needs its one confirmation; otherwise what to tell the
        doctor.
        """
        if kind == STOP:
            if value == REFUSAL:
                if not consent_open:
                    return "Closed once CMED sent the patient's details."
                if not deletion_confirmed:
                    return NEEDS_DELETE_CONFIRMATION
                self._submit(lambda c: c.refuse_session())
                return None
            if value not in dict(STOP_MENU):
                return "Choose a reason from the list."
            self._submit(lambda c: c.doctor_mark(value))
            return None
        if kind == MARK:
            if value == SENSITIVE_END:
                self._submit(lambda c: c.sensitive_end())
                return None
            if value not in dict(MARK_MENU):
                return "Choose a reason from the list."
            self._submit(lambda c: c.sensitive_start(value))
            return None
        return "Choose a reason from the list."


__all__ = ["STOP", "MARK", "STOP_TITLE", "STOP_MENU", "MARK_TITLE", "MARK_MENU",
           "MARKED_TITLE", "MARKED_OVER", "END_LABEL", "SENSITIVE_END", "REFUSAL",
           "REFUSAL_CLOSED", "DELETE_QUESTION", "NEEDS_DELETE_CONFIRMATION",
           "REMINDER_SECONDS", "MESSAGE_SECONDS", "OverlayView", "OverlayState",
           "OverlayActions", "menu"]
