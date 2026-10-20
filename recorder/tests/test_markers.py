"""
Every click and prescription during an unarmed recording, written in as a marker.

CMED's finished page sends API 1 for every patient the doctor opens and API 3a
for every prescription. The recorder cannot tell, at the click, a glance from a
wrong profile corrected or the next patient after a follow-up, so it accepts
the click, keeps recording, and signs a marker with the exact second of audio.
The AIMS LAB server reads the markers afterwards and decides the labels.

Also the two limits that end a recording nobody ends: two minutes after the
prescription (closing at a quiet moment), and fifteen minutes with nothing from
CMED.
"""
from __future__ import annotations

import asyncio
import time

import pytest

from core.session_controller import SessionError
from core.uploader import AuthOutcome
from tests.fakes import ScriptedUploader, make_controller, trigger
from tests.test_authorisation import (TRIGGER, open_spooled, server_grant,  # noqa: F401
                                      server_key, uploader_for)

GRANTED = AuthOutcome("granted", "GRANTED", "", None, "confirmed")


def ctl_with(monkeypatch, spool, device_key, **knobs):
    knobs.setdefault("limit_poll_seconds", 0.01)
    return make_controller(monkeypatch, spool, device_key, ScriptedUploader([GRANTED]), **knobs)


def at(minute, second=0):
    return f"2026-09-13T10:{minute:02d}:{second:02d}+06:00"


def markers(ctl, session=None):
    chain = (session or ctl._active.spool).chain
    return [(e.payload["event"], e.payload["patient"]["patient_id"])
            for e in chain if e.entry_type == "marker"]


def heard(ctl, seconds):
    """The segmenter has been given this much audio."""
    ctl._active.segmenter.received_bytes = int(seconds * 88200)


async def wait_for(condition, seconds=2.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        await asyncio.sleep(0.01)
    return condition()


def closed_reason(ctl):
    stops = [d for n, d in ctl.events if n == "recording_stopped"]
    return stops[-1]["reason"] if stops else None


# ---------------------------------------------------------------- the scenarios

async def test_a_wrong_pick_at_the_start(monkeypatch, spool, device_key):
    """Rahim opened by mistake, Karim ten seconds later, Karim's prescription."""
    ctl = ctl_with(monkeypatch, spool, device_key)
    first = await ctl.open_session(trigger("RAHIM", at(0)))
    heard(ctl, 10.0)
    again = await ctl.open_session(trigger("KARIM", at(0, 10)))
    assert again["continued"] and again["session_id"] == first["session_id"]
    heard(ctl, 400.25)
    armed = await ctl.arm(patient_id="KARIM", session_id=first["session_id"])
    assert armed == {"session_id": first["session_id"], "armed": True, "already": False}

    chain = ctl._active.spool.chain
    assert markers(ctl) == [("patient_opened", "KARIM"), ("prescription_built", "KARIM")]
    opened = [e for e in chain if e.entry_type == "marker"][0].payload
    assert opened["audio_seconds"] == 10.0
    assert opened["patient"] == trigger("KARIM", at(0, 10)).fields()
    assert [e for e in chain if e.entry_type == "marker"][1].payload["audio_seconds"] == 400.25
    assert all(e.entry_type != "reassign" for e in chain)        # nothing is moved here
    assert ctl._active.spool.verify_chain().ok
    assert ctl.status()["patient_ref"] == "RAHIM"               # the server relabels

    nxt = await ctl.open_session(trigger("NEXT", at(9)))
    assert nxt["previous_session_id"] == first["session_id"]    # the room is not locked
    await ctl.close()


async def test_a_deliberate_click_and_a_return(monkeypatch, spool, device_key):
    """Sakib is right; the doctor opens Salma, goes back to Sakib, writes his prescription."""
    ctl = ctl_with(monkeypatch, spool, device_key)
    first = await ctl.open_session(trigger("SAKIB", at(0)))
    await ctl.open_session(trigger("SALMA", at(0, 40)))
    await ctl.open_session(trigger("SAKIB", at(0)))
    await ctl.open_session(trigger("SAKIB", at(0)))                 # a double click
    await ctl.arm(patient_id="SAKIB", session_id=first["session_id"])
    assert markers(ctl) == [("patient_opened", "SALMA"), ("patient_opened", "SAKIB"),
                            ("prescription_built", "SAKIB")]
    opened_again = [d for n, d in ctl.events if n == "patient_opened"]
    assert [d["first_time"] for d in opened_again] == [True, False]
    await ctl.close()


async def test_a_glance_then_the_prescription(monkeypatch, spool, device_key):
    ctl = ctl_with(monkeypatch, spool, device_key)
    first = await ctl.open_session(trigger("SAKIB", at(0)))
    await ctl.open_session(trigger("SALMA", at(4)))
    await ctl.arm(patient_id="SAKIB", session_id=first["session_id"])
    assert markers(ctl) == [("patient_opened", "SALMA"), ("prescription_built", "SAKIB")]
    assert ctl.status()["focus_patient"] == "SAKIB"
    await ctl.close()


async def test_a_follow_up_then_the_next_patient(monkeypatch, spool, device_key):
    """Sakib needs no prescription; Salma is next and gets one; Rina closes it."""
    ctl = ctl_with(monkeypatch, spool, device_key)
    first = await ctl.open_session(trigger("SAKIB", at(0)))
    heard(ctl, 360.0)
    await ctl.open_session(trigger("SALMA", at(6)))
    heard(ctl, 540.0)
    await ctl.arm(patient_id="SALMA", session_id=first["session_id"])
    session = ctl._active.spool
    rina = await ctl.open_session(trigger("RINA", at(11)))
    assert rina["previous_session_id"] == first["session_id"]
    assert markers(ctl, session) == [("patient_opened", "SALMA"),
                                     ("prescription_built", "SALMA")]
    assert [e.payload["audio_seconds"] for e in session.chain if e.entry_type == "marker"] == [
        360.0, 540.0]
    assert session.chain[-1].entry_type == "close"
    await ctl.close()


async def test_a_prescription_for_a_patient_never_opened_is_refused(monkeypatch, spool,
                                                                     device_key):
    ctl = ctl_with(monkeypatch, spool, device_key)
    first = await ctl.open_session(trigger("P1", at(0)))
    with pytest.raises(SessionError) as err:
        await ctl.arm(patient_id="NOBODY", session_id=first["session_id"])
    assert err.value.code == "PATIENT_MISMATCH" and markers(ctl) == []
    await ctl.close()


async def test_a_second_prescription_is_written_in_but_a_resend_is_not(monkeypatch, spool,
                                                                       device_key):
    ctl = ctl_with(monkeypatch, spool, device_key)
    first = await ctl.open_session(trigger("P1", at(0)))
    await ctl.open_session(trigger("P2", at(1)))
    sid = first["session_id"]
    await ctl.arm(patient_id="P1", session_id=sid)
    assert (await ctl.arm(patient_id="P1", session_id=sid))["already"] is True
    assert (await ctl.arm(patient_id="P2", session_id=sid))["already"] is True
    assert markers(ctl) == [("patient_opened", "P2"), ("prescription_built", "P1"),
                            ("prescription_built", "P2")]
    await ctl.close()


async def test_markers_survive_a_restart_and_wait_to_be_sent(monkeypatch, spool, device_key):
    from core.spool import SessionSpool
    ctl = ctl_with(monkeypatch, spool, device_key)
    await ctl.open_session(trigger("P1", at(0)))
    await ctl.open_session(trigger("P2", at(1)))
    session = ctl._active.spool
    entry = [e for e in session.chain if e.entry_type == "marker"][0]
    assert entry in session.pending_notifications()
    again = SessionSpool.load(session.directory, device_key=device_key, spool_key=spool._key)
    assert [e.entry_type for e in again.chain].count("marker") == 1
    assert again.verify_chain().ok
    await ctl.close()


# ---------------------------------------------------------------- the limits

async def test_the_counselling_limit_ends_it_after_the_prescription(monkeypatch, spool,
                                                                    device_key):
    ctl = ctl_with(monkeypatch, spool, device_key, counselling_limit_seconds=0.15,
                   end_grace_seconds=0.0)
    first = await ctl.open_session(trigger("P1", at(0)))
    await asyncio.sleep(0.3)
    assert ctl.state == "recording"                      # nothing until the prescription
    await ctl.arm(patient_id="P1", session_id=first["session_id"])
    assert await wait_for(lambda: ctl.state == "idle")
    assert closed_reason(ctl) == "counselling_limit"
    assert ctl._keeper.load() is None                    # nothing to resume
    await ctl.close()


async def test_the_grace_closes_at_the_first_quiet_moment(monkeypatch, spool, device_key):
    ctl = ctl_with(monkeypatch, spool, device_key, counselling_limit_seconds=0.05,
                   end_grace_seconds=5.0)
    first = await ctl.open_session(trigger("P1", at(0)))
    segmenter = ctl._active.segmenter
    segmenter.live_level = 4000.0                        # the doctor is mid-sentence
    await ctl.arm(patient_id="P1", session_id=first["session_id"])
    await asyncio.sleep(0.3)
    assert ctl.state == "recording"                      # within the grace, still talking
    segmenter.live_level = 10.0                          # a pause
    assert await wait_for(lambda: ctl.state == "idle", 1.0)
    assert closed_reason(ctl) == "counselling_limit"
    await ctl.close()


async def test_the_grace_runs_out_if_nobody_pauses(monkeypatch, spool, device_key):
    ctl = ctl_with(monkeypatch, spool, device_key, counselling_limit_seconds=0.05,
                   end_grace_seconds=0.2)
    first = await ctl.open_session(trigger("P1", at(0)))
    ctl._active.segmenter.live_level = 4000.0
    await ctl.arm(patient_id="P1", session_id=first["session_id"])
    assert await wait_for(lambda: ctl.state == "idle", 1.5)
    await ctl.close()


async def test_the_next_patient_still_ends_it_before_the_limit(monkeypatch, spool, device_key):
    ctl = ctl_with(monkeypatch, spool, device_key, counselling_limit_seconds=5.0)
    first = await ctl.open_session(trigger("P1", at(0)))
    await ctl.arm(patient_id="P1", session_id=first["session_id"])
    second = await ctl.open_session(trigger("P2", at(3)))
    assert second["previous_session_id"] == first["session_id"]
    assert closed_reason(ctl) == "superseded_by_new_patient"
    await ctl.close()


async def test_the_no_activity_limit_ends_a_follow_up_nobody_follows(monkeypatch, spool,
                                                                     device_key):
    ctl = ctl_with(monkeypatch, spool, device_key, no_activity_limit_seconds=0.3)
    await ctl.open_session(trigger("P1", at(0)))
    await asyncio.sleep(0.2)
    await ctl.open_session(trigger("P2", at(5)))         # CMED activity: the clock restarts
    await asyncio.sleep(0.2)
    assert ctl.state == "recording"
    assert await wait_for(lambda: ctl.state == "idle", 1.0)
    assert closed_reason(ctl) == "no_activity_limit"
    await ctl.close()


async def test_no_limit_ends_a_paused_recording(monkeypatch, spool, device_key):
    ctl = ctl_with(monkeypatch, spool, device_key, no_activity_limit_seconds=0.4)
    ctl.cfg.pause.reasons = ("other", "phone_call")
    await ctl.open_session(trigger("P1", at(0)))
    await ctl.pause_session(reason="phone_call")
    await asyncio.sleep(0.6)                             # longer than the limit
    assert ctl.state == "paused"                         # the Stop form may be open
    await ctl.resume_session()
    assert ctl.state == "recording"                      # the pause did not count
    assert await wait_for(lambda: ctl.state == "idle", 1.0)
    await ctl.close()


async def test_a_limit_of_zero_is_off(monkeypatch, spool, device_key):
    ctl = ctl_with(monkeypatch, spool, device_key, no_activity_limit_seconds=0,
                   counselling_limit_seconds=0)
    first = await ctl.open_session(trigger("P1", at(0)))
    await ctl.arm(patient_id="P1", session_id=first["session_id"])
    await asyncio.sleep(0.2)
    assert ctl.state == "recording"
    await ctl.close()


# ---------------------------------------------------------------- delivery

def _marker_session(spool, device_key, *events):
    """A spooled recording of P0012345 with markers for (event, patient_id)."""
    from core import crypto
    from datetime import datetime, timezone
    session = open_spooled(spool, device_key)
    for n, (event, patient) in enumerate(events):
        fields = {**TRIGGER, "patient_id": patient, "start_time": at(n + 1)}
        session.append_chain_entry("marker", crypto.marker_payload(
            event=event, patient=fields, audio_seconds=10.0 * (n + 1),
            at=datetime.now(timezone.utc)))
    return session


def _posting(uploader):
    posted = []

    async def post(endpoint, payload, attempts=None):
        posted.append((endpoint, payload))
        return {"status": "recorded", "entry_no": payload["chain_entry"]["entry_no"]}

    uploader._post = post
    return posted


async def test_a_new_patient_marker_carries_that_patients_grant(spool, device_key, server_key):
    session = _marker_session(spool, device_key, ("patient_opened", "SALMA"),
                              ("patient_opened", "P0012345"),
                              ("patient_opened", "SALMA"),
                              ("prescription_built", "SALMA"))
    uploader = uploader_for(spool, device_key, server_key, [
        (200, {"grant": server_grant(server_key, patient="SALMA", jti="jti-salma")})])
    posted = _posting(uploader)
    for entry in session.pending_notifications():
        assert await uploader._notify_chain_entry(session, entry)
    assert [c[1] for c in uploader.calls] == ["/grant/mint"]       # one grant, for SALMA
    assert uploader.calls[0][2]["patient_id"] == "SALMA"
    assert [(e, p["grant_jti"]) for e, p in posted] == [
        ("/session/marker", "jti-salma"), ("/session/marker", None),
        ("/session/marker", None), ("/session/marker", None)]
    assert session.pending_notifications() == []


async def test_an_unreachable_server_keeps_the_marker_waiting(spool, device_key, server_key):
    session = _marker_session(spool, device_key, ("patient_opened", "SALMA"))
    uploader = uploader_for(spool, device_key, server_key, [(None, None)])
    posted = _posting(uploader)
    entry = session.pending_notifications()[0]
    assert not await uploader._notify_chain_entry(session, entry)
    assert posted == [] and session.pending_notifications() == [entry]


async def test_a_refused_grant_is_asked_again_then_sent_without_one(spool, device_key,
                                                                    server_key):
    session = _marker_session(spool, device_key, ("patient_opened", "SALMA"))
    uploader = uploader_for(spool, device_key, server_key,
                            [(404, {"code": "DOCTOR_NOT_AT_CLINIC"})])
    uploader.cfg.security.authorise_hold_retry_seconds = 0.2
    posted = _posting(uploader)
    entry = session.pending_notifications()[0]
    assert not await uploader._notify_chain_entry(session, entry)    # held, asked again
    assert posted == []
    await asyncio.sleep(0.25)
    assert await uploader._notify_chain_entry(session, entry)        # then sent, no grant
    assert posted[0][1]["grant_jti"] is None
    assert any(d.get("alert_type") == "marker_without_grant"
               for n, d in uploader.events if n == "integrity_alert")
    assert session.pending_notifications() == []
