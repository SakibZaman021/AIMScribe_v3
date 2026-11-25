"""
The on-screen control (3.9.8): two see-through icons, menus, no clock.

The rules are tested on the model, which has no tkinter; the controller side is
tested for real; the window itself gets a smoke test that builds it hidden.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from core.session_controller import REFUSAL_REASON, STOP_HOLD_REASON, SessionError
from core.spool import SessionSpool
from core.uploader import AuthOutcome
from tests.fakes import ScriptedUploader, make_controller, trigger
from ui.overlay_model import (END_LABEL, MARK, MARK_MENU, MARK_TITLE, MARKED_OVER,
                              NEEDS_DELETE_CONFIRMATION, REFUSAL_CLOSED,
                              REMINDER_SECONDS, SENSITIVE_END, STOP, STOP_MENU, STOP_TITLE,
                              OverlayActions, OverlayState, menu)

GRANTED = AuthOutcome("granted", "GRANTED", "", None, "confirmed")


def recording(**extra):
    return {"is_recording": True, "is_paused": False, "duration_seconds": 65,
            "authorisation": "granted", "confirmation": "confirmed",
            "consent_open": True, **extra}


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


# ============================================================
# What the control shows
# ============================================================

def test_hidden_when_nothing_is_recording():
    assert OverlayState().view({}).visible is False


def test_recording_shows_a_dot_and_no_clock():
    view = OverlayState().view(recording())
    assert view.visible and view.dot == "recording"
    assert not hasattr(view, "headline"), "3.9.8 shows no clock or headline"


def test_the_stop_menu_says_recording_continues_and_offers_four_choices():
    title, items = menu(STOP, OverlayState().view(recording()))
    assert title == STOP_TITLE and "Recording continues" in title
    assert [(v, w) for v, w, _ in items] == STOP_MENU
    assert items[0][0] == REFUSAL_REASON and all(enabled for _, _, enabled in items)


def test_the_refusal_greys_out_once_api2_has_arrived():
    title, items = menu(STOP, OverlayState().view(recording(consent_open=False)))
    refusal = items[0]
    assert refusal == (REFUSAL_REASON, REFUSAL_CLOSED, False)
    assert all(enabled for _, _, enabled in items[1:])


def test_the_mark_menu_says_the_part_will_be_removed():
    title, items = menu(MARK, OverlayState().view(recording()))
    assert title == MARK_TITLE and "will be removed before anyone listens" in title
    assert [(v, w) for v, w, _ in items] == MARK_MENU
    assert [w for _, w in MARK_MENU] == ["Patient's request", "Sensitive personal matter",
                                         "Interruption"]


def test_a_marked_part_offers_to_end_it_and_asks_after_two_minutes():
    marked = recording(sensitive={"reason": "patient_request", "label": "Patient's request",
                                  "seconds": 30})
    view = OverlayState().view(marked)
    assert view.marked == "Patient's request" and not view.alarm
    title, items = menu(MARK, view)
    assert "Patient's request" in title and items == [(SENSITIVE_END, END_LABEL, True)]
    late = recording(sensitive={"reason": "patient_request", "label": "Patient's request",
                                "seconds": REMINDER_SECONDS})
    view = OverlayState().view(late)
    assert view.alarm and menu(MARK, view)[0] == MARKED_OVER


def test_the_screen_shows_no_words_whatever_happens():
    """25 Nov: no notes for the doctor; the server keeps them for the lab."""
    state = OverlayState()
    state.on_event("recording_started", {"session_id": "S1"})
    for event, data in [
        ("session_confirming", {"session_id": "S1"}),
        ("session_unconfirmed", {"session_id": "S1"}),
        ("session_held", {"session_id": "S1", "message": "Recording. The AIMS LAB server "
                          "has not accepted this recording yet, so it is kept on this PC."}),
        ("integrity_alert", {"session_id": "S1", "alert_type": "silent_session"}),
        ("integrity_alert", {"session_id": "S1", "alert_type": "microphone_unavailable"}),
        ("integrity_alert", {"session_id": "S1", "alert_type": "microphone_switched",
                             "device": "Microphone Array (Realtek)"}),
        ("doctor_marked", {"reason": "patient_left"}),
        ("sensitive_started", {}),
        ("overlay_message", {"text": "The recorder is still starting."}),
    ]:
        state.on_event(event, data)
        for status in (recording(), recording(confirmation="confirming"),
                       recording(authorisation="pending")):
            view = state.view(status)
            assert view.note == "" and view.dot == "recording", (event, status)


def test_nothing_stays_on_screen_after_a_refusal():
    state = OverlayState()
    state.on_event("session_refused", {"session_id": "S1"})
    assert state.view({}).visible is False


def test_an_open_menu_keeps_the_control_visible():
    state = OverlayState()
    state.menu_open = True
    assert state.view({}).visible


# ============================================================
# What the choices do
# ============================================================

class Calls:
    """Runs what the control submits against a stand-in controller."""

    def __init__(self):
        self.made = []

    def submit(self, make):
        make(self)

    def __getattr__(self, name):
        def record(*args, **kwargs):
            self.made.append((name, args, kwargs))
        return record


def actions():
    calls = Calls()
    return OverlayActions(calls.submit), calls


def test_stop_reasons_are_logged_not_obeyed():
    act, calls = actions()
    for value, _ in STOP_MENU[1:]:
        assert act.choose(STOP, value) is None
    assert [c[0] for c in calls.made] == ["doctor_mark"] * 3
    assert all(c[0] not in ("stop_session", "pause_session") for c in calls.made)


def test_the_refusal_asks_once_then_deletes():
    act, calls = actions()
    assert act.choose(STOP, REFUSAL_REASON) == NEEDS_DELETE_CONFIRMATION
    assert calls.made == []
    assert act.choose(STOP, REFUSAL_REASON, deletion_confirmed=True) is None
    assert calls.made == [("refuse_session", (), {})]


def test_the_refusal_cannot_be_chosen_once_closed():
    act, calls = actions()
    problem = act.choose(STOP, REFUSAL_REASON, consent_open=False, deletion_confirmed=True)
    assert problem and calls.made == []


def test_marking_and_ending_a_sensitive_part():
    act, calls = actions()
    assert act.choose(MARK, "patient_request") is None
    assert act.choose(MARK, SENSITIVE_END) is None
    assert [c[0] for c in calls.made] == ["sensitive_start", "sensitive_end"]
    assert act.choose(MARK, "made_up") == "Choose a reason from the list."


# ============================================================
# The controller (unchanged methods, still used by the tray and older paths)
# ============================================================

@pytest.fixture
def ctl(monkeypatch, spool, device_key):
    controller = make_controller(monkeypatch, spool, device_key, ScriptedUploader([GRANTED]))
    controller.cfg.pause = SimpleNamespace(
        reasons=("patient_declined", "other"), self_authorise_seconds=300)
    return controller


async def test_stop_pressed_keeps_recording_until_a_reason_is_saved(ctl):
    """3.9.4, the lab's rule (18 Nov): the recording carries on while the form is open."""
    await ctl.open_session(trigger())
    active = ctl._active
    await ctl.stop_requested()
    assert ctl.state == "recording" and active.recorder.is_running
    assert not [e for e in active.spool.chain if e.entry_type == "pause"]
    result = await ctl.stop_session(reason="doctor_stopped:patient_left")
    assert result["status"] == "stopped" and result["paused_seconds"] == 0


async def test_each_recording_keeps_its_own_log_and_sends_it(ctl, spool, device_key):
    """3.9.5: started, paused (why), resumed, Stop pressed and cancelled - in the
    recording's journal, so it survives a restart, and in its manifest."""
    await ctl.open_session(trigger())
    session = ctl._active.spool
    await ctl.pause_session(reason="patient_declined")
    await ctl.resume_session()
    await ctl.stop_requested()
    await ctl.stop_cancelled()
    events = [(e["event"], e["detail"]) for e in session.manifest()["events"]]
    assert ("recording_paused", "patient_declined") in events
    assert ("stop_pressed", "") in events and ("stop_cancelled", "") in events
    assert any(e == "recording_resumed" for e, _ in events)
    again = SessionSpool.load(session.directory, device_key=device_key, spool_key=spool._key)
    assert [e["event"] for e in again.events] == [e for e, _ in events]


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

def test_the_window_builds_and_opens_its_menus():
    """Smoke test: the real tkinter window builds, takes a view and opens both menus."""
    tk = pytest.importorskip("tkinter")
    from ui.overlay import Overlay

    runtime = SimpleNamespace(cfg=SimpleNamespace(pause=SimpleNamespace(reasons=())),
                              controller=None, loop=None)
    overlay = Overlay(runtime, visible=False)
    try:
        overlay.build()
    except tk.TclError as exc:
        pytest.skip(f"no display: {exc}")

    def texts(window):
        found = []

        def walk(widget):
            try:
                found.append(widget.cget("text"))
            except Exception:
                pass
            if widget.winfo_class() == "Canvas":       # the choices are drawn buttons
                found.extend(widget.itemcget(item, "text") for item in widget.find_all()
                             if widget.type(item) == "text")
            for child in widget.winfo_children():
                walk(child)
        walk(window)
        return found

    try:
        status = recording()
        overlay.apply(OverlayState().view(status), status)
        overlay._open_menu(STOP)
        words = texts(overlay.menu_window)
        assert STOP_TITLE in words and "Patient did not consent" in words
        assert "Emergency" in words
        overlay._close_menu()
        overlay._open_menu(MARK)
        assert MARK_TITLE in texts(overlay.menu_window)
        overlay._close_menu()
        assert overlay.menu_window is None

        # Two minutes into a marked part its menu opens by itself and asks.
        late = recording(session_id="S1", sensitive={"reason": "interruption",
                                                     "label": "Interruption",
                                                     "seconds": REMINDER_SECONDS + 1})
        overlay.apply(OverlayState().view(late), late)
        assert overlay.menu_window is not None and overlay.menu_kind == MARK
        assert MARKED_OVER in texts(overlay.menu_window)
    finally:
        overlay.root.destroy()
