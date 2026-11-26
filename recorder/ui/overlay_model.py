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
REFUSAL_CLOSED = "Patient did not consent (only in the first minute)"

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

# 25 Nov 2026: the doctor's screen shows NO words of its own - no "Checking
# with CMED", no microphone or server notes, no "Noted". Only the dot and the
# two icons with their menus. Everything such a note used to say is already
# sent to the AIMS LAB server (alerts and the health log); the data collectors
# and researchers act on it there.
MESSAGE_SECONDS = 0.0           # kept for older callers; nothing is shown


@dataclass
class OverlayView:
    visible: bool
    note: str                       # always "": the screen shows no words
    consent_open: bool              # the refusal can still be chosen
    marked: Optional[str]           # the open sensitive part's reason, words
    alarm: bool                     # marked two minutes or more: blink and ask
    dot: str                        # "recording" | "off" (3.9.11: not drawn)
    pulse: bool = False             # 3.9.11: a part is marked, the Mark button pulses
    stopped: bool = False           # 3.9.11: a Stop reason was logged this recording


class OverlayState:
    """Turns the controller's status into what the control shows."""

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self.session_id: Optional[str] = None
        self.menu_open = False
        # 3.9.11 (26 Nov 2026, the owner's design): once the doctor has given a
        # Stop reason, both buttons go for the rest of that recording - the
        # reason is logged and the menu said the recording continues until CMED
        # closes the visit. They come back with the next patient's recording.
        self.stopped_session: Optional[str] = None

    def on_event(self, event: str, data: Dict[str, Any]) -> None:
        """Events change nothing on the screen; they reach the server, not the doctor."""
        if event == "recording_started":
            self.session_id = data.get("session_id")

    def stop_chosen(self, session_id: Optional[str]) -> None:
        """A Stop reason was logged for this recording: hide both buttons for it."""
        self.stopped_session = session_id or "?"

    def view(self, status: Dict[str, Any]) -> OverlayView:
        active = bool(status.get("is_recording") or status.get("is_paused"))
        session = status.get("session_id")
        if self.stopped_session is not None and session and session != self.stopped_session:
            self.stopped_session = None             # the next patient: buttons back
        stopped = active and self.stopped_session is not None
        sensitive = status.get("sensitive") or None
        marked = (sensitive.get("label") or sensitive.get("reason")) if sensitive else None
        alarm = bool(sensitive) and float(sensitive.get("seconds") or 0) >= REMINDER_SECONDS
        return OverlayView(
            visible=(active and not stopped) or self.menu_open,
            note="",
            consent_open=active and status.get("consent_open", True) is not False,
            marked=marked if active else None,
            alarm=alarm and active,
            dot="recording" if active else "off",
            pulse=bool(marked) and active,
            stopped=stopped,
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
        refusal still needs its one confirmation; otherwise why it was not
        sent (logged, never shown).
        """
        if kind == STOP:
            if value == REFUSAL:
                # 3.9.12: logged like the other reasons; the recording continues.
                if not consent_open:
                    return "Offered in the first minute of the recording only."
                self._submit(lambda c: c.doctor_mark(REFUSAL))
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
