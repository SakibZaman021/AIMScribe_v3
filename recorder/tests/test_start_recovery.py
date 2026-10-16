"""
A start that failed, or a recorder that died, recovered without the doctor.

CMED sends its trigger once, when the doctor opens the patient. If the
microphone is busy at that moment, or the recorder crashes mid-consultation,
the doctor is already talking to the patient - and must never have to click
the profile again. The recorder remembers what it was asked for and finishes
the job itself.
"""
from __future__ import annotations

import asyncio
import os
import time

import pytest

import core.session_controller as sc
from core.recorder import AudioCaptureError
from core.session_controller import SessionError
from core.start_keeper import StartKeeper
from tests.fakes import FakeRecorder, ScriptedUploader, make_controller, trigger
from core.uploader import AuthOutcome

GRANTED = AuthOutcome("granted", "GRANTED", "", None, "confirmed")


def flaky_microphone(monkeypatch, failures):
    """The device refuses to open `failures` times, then works."""
    left = {"n": failures}

    class Microphone(FakeRecorder):
        def start(self):
            if left["n"] > 0:
                left["n"] -= 1
                raise AudioCaptureError("device in use by another application")
            super().start()

    monkeypatch.setattr(sc, "AudioRecorder", Microphone)
    return left


@pytest.fixture
def ctl(monkeypatch, spool, device_key, tmp_path):
    controller = make_controller(monkeypatch, spool, device_key, ScriptedUploader([GRANTED]),
                                 start_retry_seconds=0.02, start_retry_limit_seconds=1.0,
                                 resume_within_seconds=600)
    controller._keeper = StartKeeper(tmp_path / "state")
    yield controller


async def wait_for(condition, seconds=1.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        await asyncio.sleep(0.01)
    return condition()


async def test_a_busy_microphone_is_retried_until_it_records(monkeypatch, ctl):
    flaky_microphone(monkeypatch, failures=3)
    with pytest.raises(SessionError) as err:
        await ctl.open_session(trigger("P1"))
    assert err.value.code == "AGENT_NOT_READY"           # the page is told, as before

    assert await wait_for(lambda: ctl.state == "recording")
    assert ctl.status()["patient_ref"] == "P1"
    assert await wait_for(lambda: any(d.get("alert_type") == "start_recovered"
                                      for n, d in ctl.events if n == "integrity_alert"))
    await ctl.close()


async def test_the_page_retrying_too_does_no_harm(monkeypatch, ctl):
    flaky_microphone(monkeypatch, failures=2)
    for _ in range(2):
        with pytest.raises(SessionError):
            await ctl.open_session(trigger("P1"))
    assert await wait_for(lambda: ctl.state == "recording")
    again = await ctl.open_session(trigger("P1"))          # the page asks once more
    assert again["continued"] is True
    assert len([d for d in ctl._spool.root.iterdir() if d.is_dir()]) == 1
    await ctl.close()


@pytest.mark.parametrize("moves_on", ["stop", "another_patient"])
async def test_the_retry_ends_when_cmed_moves_on(monkeypatch, ctl, moves_on):
    flaky_microphone(monkeypatch, failures=1000)
    with pytest.raises(SessionError):
        await ctl.open_session(trigger("P1"))
    if moves_on == "stop":
        await ctl.stop_session(reason="doctor_stopped")
        assert ctl._keeper.load() is None
    else:
        with pytest.raises(SessionError):
            await ctl.open_session(trigger("P2", "2026-09-13T10:20:00+06:00"))
        assert ctl._keeper.load()["trigger"]["patient_id"] == "P2"
    await asyncio.sleep(0.1)
    assert ctl.state == "idle"
    await ctl.close()


async def test_a_microphone_that_never_comes_back_is_given_up_and_reported(monkeypatch, ctl):
    flaky_microphone(monkeypatch, failures=1000)
    with pytest.raises(SessionError):
        await ctl.open_session(trigger("P1"))
    assert await wait_for(lambda: any(d.get("alert_type") == "start_abandoned"
                                      for n, d in ctl.events if n == "integrity_alert"),
                          seconds=2.0)
    assert ctl._keeper.load() is None
    await ctl.close()


async def test_a_consultation_live_before_a_crash_is_recorded_again(ctl):
    started = await ctl.open_session(trigger("P1"))
    await ctl.arm(patient_id="P1", session_id=started["session_id"])
    wanted = ctl._keeper.load()
    assert wanted["session_id"] == started["session_id"] and wanted["armed"] is True

    # The process dies: nothing is closed and the note stays on disk.
    ctl._active = None
    ctl.state = "idle"
    await ctl._resume_after_restart()

    assert ctl.state == "recording" and ctl.status()["patient_ref"] == "P1"
    assert ctl.status()["armed"] is True                      # the gate is as it was
    assert any(d.get("alert_type") == "resumed_after_restart" for n, d in ctl.events
               if n == "integrity_alert")
    await ctl.close()


async def test_shutting_down_keeps_the_note_but_a_stop_clears_it(ctl):
    await ctl.open_session(trigger("P1"))
    await ctl.stop_session(reason="agent_shutdown")
    assert ctl._keeper.load() is not None
    await ctl.open_session(trigger("P2", "2026-09-13T10:20:00+06:00"))
    await ctl.stop_session(reason="doctor_stopped")
    assert ctl._keeper.load() is None


async def test_nothing_old_is_resumed(ctl):
    ctl._keeper.want(trigger("P1").fields(), session_id="S-YESTERDAY")
    stale = time.time() - 3600
    os.utime(ctl._keeper._path, (stale, stale))
    await ctl._resume_after_restart()
    assert ctl.state == "idle" and ctl._keeper.load() is None


async def test_a_crash_after_a_click_resumes_the_patient_the_doctor_is_on(ctl):
    wrong = await ctl.open_session(trigger("WRONG"))
    await ctl.open_session(trigger("RIGHT", "2026-09-13T10:00:40+06:00"))
    note = ctl._keeper.load()
    assert note["trigger"]["patient_id"] == "RIGHT" and note["armed"] is False
    await ctl.arm(patient_id="RIGHT", session_id=wrong["session_id"])
    assert ctl._keeper.load()["armed"] is True

    ctl._active = None                                   # the process dies
    ctl.state = "idle"
    await ctl._resume_after_restart()
    assert ctl.status()["patient_ref"] == "RIGHT" and ctl.status()["armed"] is True
    assert ctl._active.armed_at is not None              # counselling counts from here
    await ctl.close()
