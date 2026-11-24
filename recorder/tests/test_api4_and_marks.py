"""
3.9.8: CMED's API 4 ends a recording; the doctor's on-screen choices are logged.

API 4 is sent like API 1, from the EHR page, and ends the recording of the visit
it names, kept whole. A resend, or one for a visit the next patient has already
ended, changes nothing; one for another visit is refused and the recording
carries on. Once a PC has received API 4, the two-minute rule after API 3 is no
longer used there.

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
    ctl = ctl_with(monkeypatch, spool, device_key)
    first = await ctl.open_session(visit("VIS-A"))
    session = ctl._active.spool
    result = await ctl.end_visit(end("VIS-A"))
    assert result["status"] == "stopped" and ctl.state == "idle"
    assert closed_reason(ctl) == API4_END_REASON == "consultation_ended"
    assert session.close_reason == "consultation_ended"
    assert any(e == "api4_end" for e, _, _ in logged(ctl, session))
    # A resend changes nothing.
    again = await ctl.end_visit(end("VIS-A"))
    assert again == {"status": "already_ended", "session_id": first["session_id"]}
    await ctl.close()


async def test_api4_for_a_visit_the_next_patient_ended_leaves_the_new_one(
        monkeypatch, spool, device_key):
    ctl = ctl_with(monkeypatch, spool, device_key)
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
    ctl = ctl_with(monkeypatch, spool, device_key)
    await ctl.open_session(visit("VIS-A"))
    with pytest.raises(SessionError) as err:
        await ctl.end_visit(end(**{"visit_id": "VIS-A", **wrong}))
    assert err.value.code == "VISIT_MISMATCH"
    assert ctl.state == "recording" and closed_reason(ctl) is None
    assert any(e == "api4_mismatch" for e, _, _ in logged(ctl))
    await ctl.close()


async def test_api4_with_nothing_recording(monkeypatch, spool, device_key):
    ctl = ctl_with(monkeypatch, spool, device_key)
    with pytest.raises(SessionError) as err:
        await ctl.end_visit(end())
    assert err.value.code == "NO_ACTIVE_SESSION"


async def test_after_the_first_api4_the_two_minute_rule_is_not_used(
        monkeypatch, spool, device_key):
    ctl = ctl_with(monkeypatch, spool, device_key, counselling_limit_seconds=0.05,
                   end_grace_seconds=0.0)
    first = await ctl.open_session(visit("VIS-A"))
    assert ctl.status()["ends_on_api4"] is False
    await ctl.end_visit(end("VIS-A"))
    assert ctl.status()["ends_on_api4"] is True
    second = await ctl.open_session(visit("VIS-B", "P2", at(2)))
    assert await ctl.prescription_received(session_id=second["session_id"], seconds_ago=0)
    await asyncio.sleep(0.3)
    assert ctl.state == "recording", "API 3 + two minutes must no longer end it"
    assert first["session_id"] != second["session_id"]
    await ctl.close()


async def test_before_any_api4_the_two_minute_rule_still_ends_it(
        monkeypatch, spool, device_key):
    ctl = ctl_with(monkeypatch, spool, device_key, counselling_limit_seconds=0.05,
                   end_grace_seconds=0.0)
    opened = await ctl.open_session(visit("VIS-A"))
    assert await ctl.prescription_received(session_id=opened["session_id"], seconds_ago=0)
    assert await wait_for(lambda: ctl.state == "idle")
    assert closed_reason(ctl) == "counselling_limit"
    await ctl.close()


async def test_the_flag_is_kept_on_disk(monkeypatch, spool, device_key, tmp_path):
    from types import SimpleNamespace
    ctl = ctl_with(monkeypatch, spool, device_key)
    ctl._state_dir = tmp_path
    await ctl.open_session(visit("VIS-A"))
    await ctl.end_visit(end("VIS-A"))
    assert (tmp_path / "api4_seen").exists()
    ctl.cfg.paths = SimpleNamespace(state_dir=tmp_path)
    assert ctl._read_api4_flag() is True
    await ctl.close()


# ---------------------------------------------------------------- over the socket and HTTP

def end_message(visit_id="VIS-A", patient="P1"):
    return json.dumps({"command": "end", "request_id": "r4", "end": {
        "visit_id": visit_id, "patient_id": patient, "doctor_id": "DR0042",
        "hospital_id": "CMED-DHK-BANANI-01", "end_time": at(9), "date": "2026-09-13"}})


async def test_api4_over_the_socket(monkeypatch, spool, device_key, make_security):
    from types import SimpleNamespace
    from api.websocket_server import WebSocketManager
    ctl = ctl_with(monkeypatch, spool, device_key)
    manager = WebSocketManager(SimpleNamespace(security=make_security(), app_version="3.9.8"))
    manager.set_controller(ctl)
    await ctl.open_session(visit("VIS-A"))
    wrong = await manager.handle_message(None, end_message("VIS-Z"))
    assert (wrong["status"], wrong["code"]) == (409, "VISIT_MISMATCH")
    assert ctl.state == "recording"
    ended = await manager.handle_message(None, end_message())
    assert (ended["status"], ended["code"], ended["request_id"]) == \
        (200, "RECORDING_ENDED", "r4")
    assert ended["data"]["visit_id"] == "VIS-A"
    again = await manager.handle_message(None, end_message())
    assert again["code"] == "VISIT_ALREADY_ENDED"
    none = await manager.handle_message(None, end_message("VIS-Q", "P7"))
    assert none["code"] == "NO_ACTIVE_SESSION"
    await ctl.close()


async def test_api4_over_http_needs_a_trusted_page(monkeypatch, spool, device_key,
                                                   make_security):
    from types import SimpleNamespace
    import httpx
    from api import websocket_server
    from api.trigger_server import create_app
    from api.websocket_server import WebSocketManager

    monkeypatch.setattr(websocket_server, "_same_windows_session", lambda request: True)
    ctl = ctl_with(monkeypatch, spool, device_key)
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
    ctl = ctl_with(monkeypatch, spool, device_key)
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
    ctl = ctl_with(monkeypatch, spool, device_key)
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
    ctl = ctl_with(monkeypatch, spool, device_key)
    await ctl.open_session(visit("VIS-A"))
    session = ctl._active.spool
    await ctl.sensitive_start("sensitive_personal_matter")
    await ctl.end_visit(end("VIS-A"))
    last = [(e, d) for e, d, _ in logged(ctl, session) if e == "sensitive_end"]
    assert last and "ended by ended with the recording" in last[0][1]
    await ctl.close()


async def test_the_refusal_is_open_until_api2_arrives(monkeypatch, spool, device_key):
    ctl = ctl_with(monkeypatch, spool, device_key)
    opened = await ctl.open_session(visit("VIS-A"))
    assert ctl.status()["consent_open"] is True
    await ctl._on_heartbeat_reply({"status": "ok",
                                   "demographics_received": {"session_id": "OTHER"}})
    assert ctl.status()["consent_open"] is True
    await ctl._on_heartbeat_reply({"status": "ok", "demographics_received":
                                   {"session_id": opened["session_id"]}})
    assert ctl.status()["consent_open"] is False
    assert any(e == "api2_received" for e, _, _ in logged(ctl))
    await ctl.close()


async def test_the_refusal_still_deletes_while_it_is_open(monkeypatch, spool, device_key):
    ctl = ctl_with(monkeypatch, spool, device_key)
    await ctl.open_session(visit("VIS-A"))
    result = await ctl.refuse_session()
    assert result["status"] == "refused" and ctl.state == "idle"
