"""
3.9.8: CMED's API 4 ends a recording; the doctor's on-screen choices are logged.

API 4 is sent like API 1, from the EHR page, and ends the recording of the visit
it names, kept whole. A resend, or one for a visit the next patient has already
ended, changes nothing; one for another visit is refused and the recording
carries on. 25 Nov 2026: whether API 4 ends recordings is the clinic's rule,
sent by the server in each heartbeat reply - "api4" where CMED's API 4 is live,
"api3" (two minutes after the prescription) elsewhere, Amader Susastho always.

The doctor's Stop reasons and sensitive-part marks are written into the
recording's own log, with the second of audio, and the recording carries on.
"Patient did not consent" stays real until CMED's API 2 reaches the server.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from api import protocol
from api.protocol import ProtocolError, Trigger, VisitEnd
from core.session_controller import API4_END_REASON, SessionError
from tests.test_markers import at, closed_reason, ctl_with, wait_for


def visit(visit_id, patient="P1", start=None, doctor="DR0042"):
    return Trigger(patient_id=patient, doctor_id=doctor, hospital_id="CMED-DHK-BANANI-01",
                   start_time=start or at(0), date="2026-09-13", visit_id=visit_id)


def end(visit_id="VIS-A", patient="P1", doctor="DR0042", when=None):
    return VisitEnd(patient_id=patient, doctor_id=doctor, end_time=when or at(9),
                    visit_id=visit_id)


def api4_ctl(*args, **kwargs):
    """A PC whose clinic ends recordings on CMED's API 4 (the server said so)."""
    ctl = ctl_with(*args, **kwargs)
    ctl._end_rule = "api4"
    return ctl


def logged(ctl, session=None):
    spool = session or ctl._active.spool
    return [(e["event"], e.get("detail", ""), e.get("audio_seconds")) for e in spool.events]


# ---------------------------------------------------------------- the message

def test_api4_is_read_like_api1():
    e = protocol.parse_end({"end": {
        "visit_id": "VIS-1", "patient_id": "P1", "doctor_id": "DR1", "hospital_id": "H1",
        "end_time": "2026-11-26T10:31:05+06:00", "date": "2026-11-26"}})
    assert (e.visit_id, e.patient_id, e.doctor_id, e.hospital_id, e.date) == \
        ("VIS-1", "P1", "DR1", "H1", "2026-11-26")
    assert "end" in protocol.CMED_COMMANDS


@pytest.mark.parametrize("body,field", [
    ({"patient_id": "P1", "doctor_id": "DR1"}, "end_time"),
    ({"patient_id": "P1", "doctor_id": "DR1", "end_time": "2026-11-26T10:31:05"}, "end_time"),
    ({"doctor_id": "DR1", "end_time": "2026-11-26T10:31:05+06:00"}, "patient_id"),
    ({"patient_id": "P1", "doctor_id": "DR1", "end_time": "2026-11-26T10:31:05+06:00",
      "visit_id": "VIS_1"}, "visit_id"),
])
def test_an_api4_missing_what_it_needs_is_refused(body, field):
    with pytest.raises(ProtocolError) as err:
        protocol.parse_end({"end": body})
    assert err.value.field == field


# ---------------------------------------------------------------- ending

async def test_api4_ends_the_visits_recording_and_keeps_it(monkeypatch, spool, device_key):
    ctl = api4_ctl(monkeypatch, spool, device_key)
    first = await ctl.open_session(visit("VIS-A"))
    session = ctl._active.spool
    result = await ctl.end_visit(end("VIS-A"))
    assert result["status"] == "stopped" and ctl.state == "idle"
    assert closed_reason(ctl) == API4_END_REASON == "consultation_ended"
    assert session.close_reason == "consultation_ended"
    assert session.close_detail == f"end_time={at(9)}"       # kept for labelling
    assert any(e == "api4_end" for e, _, _ in logged(ctl, session))
    assert result["end_time"] == at(9) and result["stopped_at"]
    # A resend changes nothing, and confirms the same stop again.
    again = await ctl.end_visit(end("VIS-A"))
    assert again == {"status": "already_ended", "session_id": first["session_id"],
                     "stopped_at": result["stopped_at"], "end_time": at(9)}
    await ctl.close()


async def test_api4_for_a_visit_the_next_patient_ended_leaves_the_new_one(
        monkeypatch, spool, device_key):
    ctl = api4_ctl(monkeypatch, spool, device_key)
    await ctl.open_session(visit("VIS-A", "RAHIM"))
    second = await ctl.open_session(visit("VIS-B", "KARIM", at(1)))
    result = await ctl.end_visit(end("VIS-A", "RAHIM"))
    assert result["status"] == "already_ended"
    assert ctl.state == "recording" and ctl._active.session_id == second["session_id"]
    await ctl.close()


@pytest.mark.parametrize("wrong", [
    dict(visit_id="VIS-Z"), dict(patient="SOMEONE"), dict(doctor="DR9999"),
    dict(visit_id=""),
])
async def test_api4_for_another_visit_is_refused_and_recording_continues(
        monkeypatch, spool, device_key, wrong):
    ctl = api4_ctl(monkeypatch, spool, device_key)
    await ctl.open_session(visit("VIS-A"))
    with pytest.raises(SessionError) as err:
        await ctl.end_visit(end(**{"visit_id": "VIS-A", **wrong}))
    assert err.value.code == "VISIT_MISMATCH"
    assert ctl.state == "recording" and closed_reason(ctl) is None
    assert any(e == "api4_mismatch" for e, _, _ in logged(ctl))
    await ctl.close()


async def test_api4_with_nothing_recording(monkeypatch, spool, device_key):
    ctl = api4_ctl(monkeypatch, spool, device_key)
    with pytest.raises(SessionError) as err:
        await ctl.end_visit(end())
    assert err.value.code == "NO_ACTIVE_SESSION"


async def test_the_rule_comes_from_the_server_and_defaults_to_api3(
        monkeypatch, spool, device_key):
    ctl = ctl_with(monkeypatch, spool, device_key)
    assert ctl.status()["end_rule"] == "api3" and ctl.status()["ends_on_api4"] is False
    await ctl._on_heartbeat_reply({"status": "ok", "end_rule": "api4"})
    assert ctl.status()["end_rule"] == "api4"
    await ctl._on_heartbeat_reply({"status": "ok", "end_rule": "anything else"})
    assert ctl.status()["end_rule"] == "api4", "an unknown rule changes nothing"
    await ctl._on_heartbeat_reply({"status": "ok", "end_rule": "api3"})
    assert ctl.status()["end_rule"] == "api3"
    await ctl.close()


async def test_an_api3_clinic_logs_api4_and_still_ends_two_minutes_after_api3(
        monkeypatch, spool, device_key):
    """Amader Susastho: API 4 never ends a recording; API 3 + two minutes does."""
    ctl = ctl_with(monkeypatch, spool, device_key, counselling_limit_seconds=0.3,
                   end_grace_seconds=0.0)
    opened = await ctl.open_session(visit("VIS-A"))
    session = ctl._active.spool
    result = await ctl.end_visit(end("VIS-A"))
    assert result == {"status": "continues", "session_id": opened["session_id"],
                      "end_time": at(9)}
    assert ctl.state == "recording"
    assert any(e == "api4_not_used" and d == f"end_time={at(9)}"
               for e, d, _ in logged(ctl, session))
    assert await ctl.prescription_received(session_id=opened["session_id"], seconds_ago=0)
    assert await wait_for(lambda: ctl.state == "idle")
    assert closed_reason(ctl) == "counselling_limit"
    await ctl.close()


async def test_an_old_api4_flag_on_disk_changes_nothing(monkeypatch, spool, device_key,
                                                        tmp_path):
    """3.9.8 before 25 Nov kept a flag that turned the two-minute rule off for good."""
    (tmp_path / "api4_seen").write_text("2026-11-25T08:56:48Z\n", encoding="utf-8")
    ctl = ctl_with(monkeypatch, spool, device_key, counselling_limit_seconds=0.05,
                   end_grace_seconds=0.0)
    ctl._state_dir = tmp_path
    opened = await ctl.open_session(visit("VIS-A"))
    assert await ctl.prescription_received(session_id=opened["session_id"], seconds_ago=0)
    assert await wait_for(lambda: ctl.state == "idle")
    assert closed_reason(ctl) == "counselling_limit"
    await ctl.close()


async def test_an_api4_clinic_waits_for_api4_then_ends_15_minutes_after_api3(
        monkeypatch, spool, device_key):
    ctl = api4_ctl(monkeypatch, spool, device_key, counselling_limit_seconds=0.05,
                   api4_wait_seconds=0.6, end_grace_seconds=0.0)
    opened = await ctl.open_session(visit("VIS-A"))
    assert await ctl.prescription_received(session_id=opened["session_id"], seconds_ago=0)
    await asyncio.sleep(0.3)
    assert ctl.state == "recording", "two minutes after API 3 is not the rule here"
    assert await wait_for(lambda: ctl.state == "idle")
    assert closed_reason(ctl) == "counselling_limit"
    await ctl.close()


async def test_a_recording_with_no_cmed_message_ends_by_itself(
        monkeypatch, spool, device_key):
    """No API 3, no API 4, no next patient: it no longer runs for hours."""
    ctl = ctl_with(monkeypatch, spool, device_key, no_activity_limit_seconds=0.3,
                   end_grace_seconds=0.0)
    await ctl.open_session(visit("VIS-A"))
    assert await wait_for(lambda: ctl.state == "idle")
    assert closed_reason(ctl) == "no_activity_limit"
    await ctl.close()


def test_the_safety_end_is_on_by_default():
    from config import SecurityConfig
    assert SecurityConfig.no_activity_limit_seconds == 2700.0
    assert SecurityConfig.api4_wait_seconds == 900.0


# ---------------------------------------------------------------- over the socket and HTTP

def end_message(visit_id="VIS-A", patient="P1"):
    return json.dumps({"command": "end", "request_id": "r4", "end": {
        "visit_id": visit_id, "patient_id": patient, "doctor_id": "DR0042",
        "hospital_id": "CMED-DHK-BANANI-01", "end_time": at(9), "date": "2026-09-13"}})


async def test_api4_over_the_socket(monkeypatch, spool, device_key, make_security):
    from types import SimpleNamespace
    from api.websocket_server import WebSocketManager
    ctl = api4_ctl(monkeypatch, spool, device_key)
    manager = WebSocketManager(SimpleNamespace(security=make_security(), app_version="3.9.8"))
    manager.set_controller(ctl)
    await ctl.open_session(visit("VIS-A"))
    wrong = await manager.handle_message(None, end_message("VIS-Z"))
    assert (wrong["status"], wrong["code"]) == (409, "VISIT_MISMATCH")
    assert ctl.state == "recording"
    ended = await manager.handle_message(None, end_message())
    assert (ended["status"], ended["code"], ended["request_id"]) == \
        (200, "RECORDING_ENDED", "r4")
    data = ended["data"]
    assert data["visit_id"] == "VIS-A"
    # 25 Nov: CMED's assurance that the recording has stopped.
    assert data["recording_stopped"] is True and data["audio_saved"] is True
    assert data["end_time"] == at(9) and data["stopped_at"]
    assert data["session_id"] and data["segment_count"] is not None
    again = await manager.handle_message(None, end_message())
    assert again["code"] == "VISIT_ALREADY_ENDED"
    assert again["data"]["recording_stopped"] is True
    assert (again["data"]["stopped_at"], again["data"]["end_time"]) == \
        (data["stopped_at"], at(9))
    none = await manager.handle_message(None, end_message("VIS-Q", "P7"))
    assert none["code"] == "NO_ACTIVE_SESSION"
    await ctl.close()


async def test_api4_at_an_api3_clinic_over_the_socket(monkeypatch, spool, device_key,
                                                      make_security):
    from types import SimpleNamespace
    from api.websocket_server import WebSocketManager
    ctl = ctl_with(monkeypatch, spool, device_key)
    manager = WebSocketManager(SimpleNamespace(security=make_security(), app_version="3.9.8"))
    manager.set_controller(ctl)
    await ctl.open_session(visit("VIS-A"))
    reply = await manager.handle_message(None, end_message())
    assert (reply["status"], reply["code"]) == (200, "RECORDING_CONTINUES")
    assert reply["data"]["recording_stopped"] is False and reply["data"]["end_time"] == at(9)
    assert ctl.state == "recording"
    await ctl.close()


async def test_api4_over_http_needs_a_trusted_page(monkeypatch, spool, device_key,
                                                   make_security):
    from types import SimpleNamespace
    import httpx
    from api import websocket_server
    from api.trigger_server import create_app
    from api.websocket_server import WebSocketManager

    monkeypatch.setattr(websocket_server, "_same_windows_session", lambda request: True)
    ctl = api4_ctl(monkeypatch, spool, device_key)
    security = make_security()
    origin = sorted(security.allowed_origins)[0]
    cfg = SimpleNamespace(security=security, app_version="3.9.8")
    ws = WebSocketManager(cfg)
    ws.set_controller(ctl)

    async def nothing():
        return None

    runtime = SimpleNamespace(cfg=cfg, ws=ws, controller=ctl, startup=nothing,
                              shutdown=nothing, health=lambda: {"status": "ok"})
    transport = httpx.ASGITransport(app=create_app(runtime))
    body = json.loads(end_message())
    del body["command"]
    await ctl.open_session(visit("VIS-A"))
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:5050") as http:
        url = "/api/v1/session/end"
        r = await http.post(url, json=body)
        assert r.status_code == 403 and ctl.state == "recording"
        r = await http.post(url, json=body, headers={"Origin": "https://evil.example"})
        assert r.status_code == 403 and ctl.state == "recording"
        r = await http.post(url, json=body, headers={"Origin": origin})
        assert r.status_code == 200 and r.json()["code"] == "RECORDING_ENDED", r.text
        assert ctl.state == "idle"
    await ctl.close()


# ---------------------------------------------------------------- the doctor's choices

async def test_a_stop_reason_is_logged_and_recording_continues(monkeypatch, spool, device_key):
    ctl = api4_ctl(monkeypatch, spool, device_key)
    await ctl.open_session(visit("VIS-A"))
    for reason in ("consultation_finished", "patient_left", "urgent_interruption"):
        result = await ctl.doctor_mark(reason)
        assert result["status"] == "logged"
    assert ctl.state == "recording" and ctl._active.recorder.is_running
    assert closed_reason(ctl) is None
    marks = [(d, a) for e, d, a in logged(ctl) if e == "doctor_mark"]
    assert [d for d, _ in marks] == ["Consultation finished", "Patient left the room",
                                     "Emergency"]
    assert all(a is not None for _, a in marks)
    with pytest.raises(SessionError):
        await ctl.doctor_mark("made_up")
    await ctl.close()


async def test_a_sensitive_part_is_logged_from_start_to_end(monkeypatch, spool, device_key):
    ctl = api4_ctl(monkeypatch, spool, device_key)
    await ctl.open_session(visit("VIS-A"))
    assert (await ctl.sensitive_start("patient_request"))["status"] == "marked"
    status = ctl.status()
    assert status["sensitive"]["label"] == "Patient's request" and ctl.state == "recording"
    assert (await ctl.sensitive_start("interruption"))["status"] == "already_marked"
    ended = await ctl.sensitive_end()
    assert ended["status"] == "ended" and ended["seconds"] >= 0
    assert "sensitive" not in ctl.status()
    events = [e for e, _, _ in logged(ctl)]
    assert events.count("sensitive_start") == 1 and events.count("sensitive_end") == 1
    assert ctl._active.recorder.is_running
    await ctl.close()


async def test_a_sensitive_part_still_open_ends_with_the_recording(
        monkeypatch, spool, device_key):
    ctl = api4_ctl(monkeypatch, spool, device_key)
    await ctl.open_session(visit("VIS-A"))
    session = ctl._active.spool
    await ctl.sensitive_start("sensitive_personal_matter")
    await ctl.end_visit(end("VIS-A"))
    last = [(e, d) for e, d, _ in logged(ctl, session) if e == "sensitive_end"]
    assert last and "ended by ended with the recording" in last[0][1]
    await ctl.close()


async def test_the_refusal_report_is_offered_in_the_first_minute(monkeypatch, spool,
                                                                  device_key):
    """3.9.12: offered for the first minute of the recording, whatever API 2 does."""
    ctl = api4_ctl(monkeypatch, spool, device_key)
    opened = await ctl.open_session(visit("VIS-A"))
    assert ctl.status()["consent_open"] is True
    await ctl._on_heartbeat_reply({"status": "ok", "demographics_received":
                                   {"session_id": opened["session_id"]}})
    assert ctl.status()["consent_open"] is True                  # API 2 no longer closes it
    assert any(e == "api2_received" for e, _, _ in logged(ctl))
    monkeypatch.setattr(type(ctl), "_audio_position", lambda self, active: 61.0)
    assert ctl.status()["consent_open"] is False                 # after the first minute
    await ctl.close()


async def test_the_refusal_report_is_logged_and_the_recording_continues(
        monkeypatch, spool, device_key):
    """3.9.12, the owner's decision: nothing stops and nothing is deleted."""
    from core.session_controller import REFUSAL_REASON
    ctl = api4_ctl(monkeypatch, spool, device_key)
    await ctl.open_session(visit("VIS-A"))
    session = ctl._active.spool
    result = await ctl.doctor_mark(REFUSAL_REASON)
    assert result["status"] == "logged" and ctl.state == "recording"
    assert [d for e, d, _ in logged(ctl, session) if e == "doctor_mark"] == [
        "Patient did not consent (reported by the doctor)"]
    assert not session.refused
    await ctl.close()


async def test_the_refusal_still_deletes_while_it_is_open(monkeypatch, spool, device_key):
    ctl = api4_ctl(monkeypatch, spool, device_key)
    await ctl.open_session(visit("VIS-A"))
    result = await ctl.refuse_session()
    assert result["status"] == "refused" and ctl.state == "idle"
