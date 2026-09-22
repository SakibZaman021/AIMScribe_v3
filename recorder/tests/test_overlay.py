"""
The on-screen Stop and Pause control (SRS 3.2 §7.8, §7.8a).

The rules are tested on the model, which has no tkinter; the controller side is
tested for real; the window itself gets one smoke test that builds it hidden.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from core.session_controller import REFUSAL_REASON, STOP_HOLD_REASON, SessionError
from core.spool import SessionSpool
from core.uploader import AuthOutcome
from tests.fakes import ScriptedUploader, make_controller, trigger
from ui.overlay_model import (MESSAGE_SECONDS, NEEDS_DELETE_CONFIRMATION, STOP_REASONS,
                              OverlayActions, OverlayState, pause_reasons)

PAUSES = pause_reasons(("patient_declined", "sensitive_personal_matter",
                        "non_clinical_interruption", "other"))


def recording(**extra):
    return {"is_recording": True, "is_paused": False, "duration_seconds": 65,
            "authorisation": "granted", "confirmation": "confirmed", **extra}


def paused(reason="patient_declined"):
    return {"is_recording": False, "is_paused": True, "pause": {"reason": reason},
            "authorisation": "granted", "confirmation": "confirmed"}


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


# ============================================================
# What the control shows
# ============================================================

def test_hidden_when_nothing_is_recording():
    """SRS-UIX-01."""
    assert OverlayState().view({}).visible is False


def test_recording_shows_both_controls():
    view = OverlayState().view(recording())
    assert view.visible and view.headline == "Recording  01:05"
    assert (view.stop_enabled, view.pause_enabled, view.pause_label) == (True, True, "Pause")


def test_paused_offers_resume():
    view = OverlayState().view(paused())
    assert (view.headline, view.pause_label, view.pause_enabled) == ("Paused", "Resume", True)


def test_holding_for_stop_offers_only_the_form():
    view = OverlayState().view(paused(STOP_HOLD_REASON))
    assert view.headline == "Stopped - choose a reason"
    assert (view.stop_enabled, view.pause_enabled, view.pause_label) == (True, False, "Pause")


def test_confirming_is_said_in_plain_words():
    """SRS-UIX-13: no error, no sound - just what is happening."""
    state = OverlayState()
    assert state.view(recording(confirmation="confirming")).note == "Checking with CMED…"
    assert "waiting for the AIMS LAB server" in state.view(
        recording(authorisation="pending")).note


def test_confirmation_events_update_the_note():
    state = OverlayState()
    state.on_event("recording_started", {"session_id": "S1"})
    state.on_event("session_unconfirmed", {"session_id": "S1"})
    assert state.view(recording()).note == "Not confirmed by CMED yet - still recording"
    state.on_event("session_confirmed", {"session_id": "OTHER"})       # not this one
    assert "Not confirmed" in state.view(recording()).note
    state.on_event("session_confirmed", {"session_id": "S1"})
    assert state.view(recording()).note == "Confirmed"


def test_a_warning_appears_once_per_consultation():
    """SRS-LVL-06."""
    state = OverlayState()
    state.on_event("recording_started", {"session_id": "S1"})
    state.on_event("integrity_alert", {"alert_type": "silent_session"})
    assert "muted or unplugged" in state.view(recording()).note
    state.note = "Confirmed"
    state.on_event("integrity_alert", {"alert_type": "silent_session"})
    assert state.view(recording()).note == "Confirmed"
    state.on_event("recording_started", {"session_id": "S2"})
    state.on_event("integrity_alert", {"alert_type": "silent_session"})
    assert "muted" in state.view(recording()).note


def test_why_a_recording_ended_stays_on_screen_briefly():
    clock = Clock()
    state = OverlayState(clock)
    state.on_event("recording_stopped", {"reason": "authorisation_refused"})
    view = state.view({})
    assert view.visible and "did not authorise" in view.headline
    clock.now += MESSAGE_SECONDS + 1
    assert state.view({}).visible is False

    state.on_event("session_refused", {"session_id": "S1"})
    assert "deleted" in state.view({}).headline


def test_an_open_form_keeps_the_control_visible():
    state = OverlayState()
    state.form_open = True
    assert state.view({}).visible


# ============================================================
# What the buttons do
# ============================================================

class Calls:
    """Runs what the control submits against a stand-in controller."""

    def __init__(self):
        self.made = []

    def submit(self, make):
        make(self)

    def __getattr__(self, name):
        def record(**kwargs):
            self.made.append((name, kwargs))
        return record


def actions():
    calls = Calls()
    return OverlayActions(calls.submit, PAUSES), calls


def test_refusal_is_the_first_stop_reason():
    """SRS-CNS-02."""
    assert STOP_REASONS[0] == (REFUSAL_REASON, "Patient did not consent")


def test_stop_press_and_cancel():
    act, calls = actions()
    act.press_stop()
    act.cancel_stop()
    assert [name for name, _ in calls.made] == ["hold_for_stop", "release_stop_hold"]


@pytest.mark.parametrize("reason,detail,problem", [
    (None, "", "Choose a reason."),
    ("made_up", "", "Choose a reason from the list."),
    ("other", "   ", "Please describe the reason."),
])
def test_stop_needs_a_reason(reason, detail, problem):
    """SRS-UIX-06, -07: nothing closes without a reason."""
    act, calls = actions()
    assert act.confirm_stop(reason, detail) == problem
    assert calls.made == []


def test_refusal_asks_once_before_deleting():
    """SRS-CNS-08."""
    act, calls = actions()
    assert act.confirm_stop(REFUSAL_REASON) == NEEDS_DELETE_CONFIRMATION
    assert calls.made == []
    assert act.confirm_stop(REFUSAL_REASON, deletion_confirmed=True) is None
    assert calls.made == [("refuse_session", {})]


def test_stop_reasons_become_close_reasons():
    act, calls = actions()
    act.confirm_stop("consultation_finished")
    act.confirm_stop("patient_left", "  went for an X-ray  ")
    assert calls.made == [
        ("stop_session", {"reason": "doctor_stopped", "detail": ""}),
        ("stop_session", {"reason": "doctor_stopped:patient_left",
                          "detail": "went for an X-ray"}),
    ]


def test_pause_needs_a_reason_then_pauses():
    """SRS-UIX-05."""
    act, calls = actions()
    assert act.confirm_pause(None) == "Choose a reason."
    assert act.confirm_pause("other") == "Please describe the reason."
    assert act.confirm_pause(STOP_HOLD_REASON) == "Choose a reason from the list."
    assert act.confirm_pause("sensitive_personal_matter") is None
    assert calls.made[-1] == ("pause_session", {"reason": "sensitive_personal_matter",
                                                "reason_detail": "", "authorised_by": "",
                                                "expected_seconds": 0})


# ============================================================
# The controller: hold for Stop
# ============================================================

GRANTED = AuthOutcome("granted", "GRANTED", "", None, "confirmed")


@pytest.fixture
def ctl(monkeypatch, spool, device_key):
    controller = make_controller(monkeypatch, spool, device_key, ScriptedUploader([GRANTED]))
    controller.cfg.pause = SimpleNamespace(
        reasons=("patient_declined", "other"), self_authorise_seconds=300)
    return controller


async def test_stop_press_cuts_the_microphone_at_once(ctl):
    """SRS-UIX-08: capture stops on the press, and the gap is in the chain."""
    await ctl.open_session(trigger())
    active = ctl._active
    held = await ctl.hold_for_stop()
    assert held["held"] and ctl.state == "paused"
    assert not active.recorder.is_running
    pause = [e for e in active.spool.chain if e.entry_type == "pause"][-1]
    assert pause.payload["reason"] == STOP_HOLD_REASON


async def test_cancelling_stop_carries_on_recording(ctl):
    await ctl.open_session(trigger())
    await ctl.hold_for_stop()
    await ctl.release_stop_hold()
    assert ctl.state == "recording" and ctl._active.recorder.is_running
    assert ctl._active.stop_hold is False


async def test_a_confirmed_stop_keeps_the_doctors_words(ctl, spool, device_key):
    await ctl.open_session(trigger())
    session = ctl._active.spool
    await ctl.hold_for_stop()
    result = await ctl.stop_session(reason="doctor_stopped:patient_left",
                                    detail="went for an X-ray")
    assert result["status"] == "stopped" and ctl.state == "idle"
    again = SessionSpool.load(session.directory, device_key=device_key, spool_key=spool._key)
    assert (again.close_reason, again.close_detail) == ("doctor_stopped:patient_left",
                                                       "went for an X-ray")
    assert again.manifest()["close_detail"] == "went for an X-ray"


async def test_refusal_from_the_hold(ctl):
    await ctl.open_session(trigger())
    await ctl.hold_for_stop()
    assert (await ctl.refuse_session())["status"] == "refused"
    assert ctl.state == "idle"


async def test_stop_while_already_paused_holds_nothing(ctl):
    await ctl.open_session(trigger())
    await ctl.pause_session(reason="patient_declined", reason_detail="")
    assert (await ctl.hold_for_stop())["held"] is False
    await ctl.release_stop_hold()
    assert ctl.state == "paused"


async def test_the_hold_reason_cannot_be_chosen_from_outside(ctl):
    await ctl.open_session(trigger())
    with pytest.raises(SessionError):
        await ctl.pause_session(reason=STOP_HOLD_REASON, reason_detail="")


# ============================================================
# The window itself
# ============================================================

def test_the_window_builds(tmp_path):
    """Smoke test: the real tkinter window builds and takes a view, hidden."""
    tk = pytest.importorskip("tkinter")
    from ui.overlay import Overlay

    runtime = SimpleNamespace(cfg=SimpleNamespace(pause=SimpleNamespace(
        reasons=("patient_declined", "other"))), controller=None, loop=None)
    overlay = Overlay(runtime, visible=False)
    try:
        overlay.build()
    except tk.TclError as exc:
        pytest.skip(f"no display: {exc}")
    def drawn(overlay):
        """Every piece of text currently on the card."""
        canvas = overlay.canvas
        return [canvas.itemcget(item, "text") for item in canvas.find_all()
                if canvas.type(item) == "text"]

    try:
        status = recording()
        overlay.apply(OverlayState().view(status), status)
        words = drawn(overlay)
        assert "Recording" in words
        assert "01:05" in words              # the elapsed time, read across a desk

        status = paused()
        overlay.apply(OverlayState().view(status), status)
        assert "Paused" in drawn(overlay)
        assert overlay._pause_text == "Resume"
        assert overlay.window.state() == "withdrawn"

        # The meter's whole purpose, in one line of words: a muted or
        # unplugged microphone is invisible until somebody plays the
        # recording back, and by then the consultation is over. Not on the
        # first tick - a pause between words is not a fault.
        silent = dict(recording(), level=0.0)
        for _ in range(10):
            overlay.apply(OverlayState().view(silent), silent)
        assert "no sound from the microphone" in drawn(overlay)

        loud = dict(recording(), level=0.6)
        overlay.apply(OverlayState().view(loud), loud)
        assert "no sound from the microphone" not in drawn(overlay)
    finally:
        overlay.root.destroy()
