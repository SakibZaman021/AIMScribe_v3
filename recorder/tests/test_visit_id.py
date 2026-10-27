"""
Interface 4: CMED's visit_id in API 1, and no prescription_built.

With a visit_id, a start for another visit ends the running recording at
that instant and starts the new one - whether the prescription was printed
thirty seconds or ten minutes ago - and the same visit again continues it.
When no next patient comes, the server's word that API 3 has arrived (in the
heartbeat reply) starts the counselling limit, counted from when API 3
arrived, and the recording ends at a pause about two minutes later.
"""
from __future__ import annotations

import asyncio

import pytest

from api import protocol
from api.protocol import ProtocolError, Trigger
from tests.test_markers import at, closed_reason, ctl_with, wait_for


def visit(visit_id, patient="P1", start=None):
    return Trigger(patient_id=patient, doctor_id="DR0042", hospital_id="CMED-DHK-BANANI-01",
                   start_time=start or at(0), date="2026-09-13", visit_id=visit_id)


# ---------------------------------------------------------------- API 1

def test_visit_id_is_read_and_carried_with_the_five_fields():
    t = protocol.parse_trigger({"trigger": {
        "visit_id": "VIS1001", "patient_id": "P1", "doctor_id": "DR1", "hospital_id": "H1",
        "start_time": at(0), "date": "2026-09-13"}})
    assert t.visit_id == "VIS1001" and t.fields()["visit_id"] == "VIS1001"
    # A page that sends none sends the server exactly what it did before.
    old = protocol.parse_trigger({"trigger": {
        "patient_id": "P1", "doctor_id": "DR1", "hospital_id": "H1",
        "start_time": at(0), "date": "2026-09-13"}})
    assert old.visit_id == "" and "visit_id" not in old.fields()
    assert Trigger(**t.fields()) == t                  # a restart rebuilds it whole


@pytest.mark.parametrize("bad", ["VIS_1001", "VIS 1", "x" * 65, 1001])
def test_a_visit_id_that_cannot_name_a_file_is_refused(bad):
    with pytest.raises(ProtocolError) as err:
        protocol.parse_trigger({"trigger": {
            "visit_id": bad, "patient_id": "P1", "doctor_id": "DR1", "hospital_id": "H1",
            "start_time": at(0), "date": "2026-09-13"}})
    assert (err.value.code, err.value.field) == ("INVALID_IDENTIFIER", "visit_id")


# ---------------------------------------------------------------- the next patient

async def test_a_new_visit_ends_the_recording_at_once_without_a_prescription(
        monkeypatch, spool, device_key):
    ctl = ctl_with(monkeypatch, spool, device_key)
    first = await ctl.open_session(visit("VIS-A", "RAHIM"))
    second = await ctl.open_session(visit("VIS-B", "KARIM", at(0, 30)))
    assert second["session_id"] != first["session_id"]
    assert (second["previous_session_id"], second["previous_visit_id"]) == \
        (first["session_id"], "VIS-A")
    assert closed_reason(ctl) == "superseded_by_new_patient"
    assert ctl._active.visit_id == "VIS-B" and ctl.state == "recording"
    await ctl.close()


async def test_the_same_visit_again_continues_the_recording(monkeypatch, spool, device_key):
    ctl = ctl_with(monkeypatch, spool, device_key)
    first = await ctl.open_session(visit("VIS-A"))
    again = await ctl.open_session(visit("VIS-A"))
    assert again["continued"] and again["session_id"] == first["session_id"]
    assert closed_reason(ctl) is None
    await ctl.close()


async def test_a_new_visit_ends_it_even_while_the_counselling_limit_runs(
        monkeypatch, spool, device_key):
    """Prescription printed, the patient leaves, the next one arrives 30 s later."""
    ctl = ctl_with(monkeypatch, spool, device_key, counselling_limit_seconds=120.0)
    first = await ctl.open_session(visit("VIS-A"))
    assert await ctl.prescription_received(session_id=first["session_id"], seconds_ago=0)
    second = await ctl.open_session(visit("VIS-B", "P2", at(1)))
    assert second["previous_session_id"] == first["session_id"]
    assert closed_reason(ctl) == "superseded_by_new_patient"
    await ctl.close()


# ---------------------------------------------------------------- no next patient

async def test_the_servers_word_starts_the_two_minutes(monkeypatch, spool, device_key):
    ctl = ctl_with(monkeypatch, spool, device_key, counselling_limit_seconds=0.2,
                   end_grace_seconds=0.0)
    first = await ctl.open_session(visit("VIS-A"))
    await ctl._on_heartbeat_reply({"status": "ok"})       # nothing yet: keeps recording
    assert ctl.state == "recording" and not ctl._active.armed
    await ctl._on_heartbeat_reply({"status": "ok", "prescription_received": {
        "session_id": first["session_id"], "seconds_ago": 0.0}})
    assert ctl._active.armed
    assert await wait_for(lambda: ctl.state == "idle")
    assert closed_reason(ctl) == "counselling_limit"
    await ctl.close()


async def test_the_two_minutes_count_from_when_api3_arrived(monkeypatch, spool, device_key):
    """Heard 25 s late (the heartbeat's interval): only the rest is waited."""
    ctl = ctl_with(monkeypatch, spool, device_key, counselling_limit_seconds=30.0,
                   end_grace_seconds=0.0)
    first = await ctl.open_session(visit("VIS-A"))
    await ctl.prescription_received(session_id=first["session_id"], seconds_ago=29.8)
    assert await wait_for(lambda: ctl.state == "idle", 1.5)
    await ctl.close()


async def test_a_notice_for_another_recording_or_a_repeat_changes_nothing(
        monkeypatch, spool, device_key):
    ctl = ctl_with(monkeypatch, spool, device_key, counselling_limit_seconds=60.0)
    first = await ctl.open_session(visit("VIS-A"))
    assert not await ctl.prescription_received(session_id="SOMEONE-ELSE", seconds_ago=0)
    assert not ctl._active.armed
    assert await ctl.prescription_received(session_id=first["session_id"], seconds_ago=5)
    armed_at = ctl._active.armed_at
    assert not await ctl.prescription_received(session_id=first["session_id"], seconds_ago=50)
    assert ctl._active.armed_at == armed_at              # the first notice's clock stands
    await ctl.close()


# ---------------------------------------------------------------- the reply CMED's page sees

async def test_the_page_is_told_which_visit_records_and_which_it_closed(
        monkeypatch, spool, device_key, make_security):
    import json
    from types import SimpleNamespace
    from api.websocket_server import WebSocketManager

    ctl = ctl_with(monkeypatch, spool, device_key)
    manager = WebSocketManager(SimpleNamespace(security=make_security()))
    manager.set_controller(ctl)

    def start(visit_id, patient, second):
        return json.dumps({"command": "start", "request_id": f"r-{visit_id}", "trigger": {
            "visit_id": visit_id, "patient_id": patient, "doctor_id": "DR0042",
            "hospital_id": "CMED-DHK-BANANI-01", "start_time": at(0, second),
            "date": "2026-09-13"}})

    first = await manager.handle_message(None, start("VIS-A", "P1", 0))
    assert first["data"]["visit_id"] == "VIS-A" and first["data"]["closed_previous"] is None
    assert first["data"]["continued"] is False
    again = await manager.handle_message(None, start("VIS-A", "P1", 0))
    assert again["data"]["continued"] is True
    assert again["data"]["session_id"] == first["data"]["session_id"]
    nxt = await manager.handle_message(None, start("VIS-B", "P2", 30))
    closed = nxt["data"]["closed_previous"]
    assert (closed["session_id"], closed["visit_id"]) == (first["data"]["session_id"], "VIS-A")
    assert closed["ended_at"] and nxt["data"]["visit_id"] == "VIS-B"
    await ctl.close()


# ---------------------------------------------------------------- API 1 over HTTP

async def test_api1_over_http_is_the_websocket_start_with_the_same_origin_rule(
        monkeypatch, spool, device_key, make_security):
    """Postman's collections cannot carry a WebSocket; the HTTP start is the same command."""
    from types import SimpleNamespace
    import httpx
    from api import websocket_server
    from api.trigger_server import create_app
    from api.websocket_server import WebSocketManager

    monkeypatch.setattr(websocket_server, "_same_windows_session", lambda request: True)
    ctl = ctl_with(monkeypatch, spool, device_key)
    security = make_security()
    origin = sorted(security.allowed_origins)[0]
    cfg = SimpleNamespace(security=security, app_version="3.8.0")
    ws = WebSocketManager(cfg)
    ws.set_controller(ctl)

    async def nothing():
        return None

    runtime = SimpleNamespace(cfg=cfg, ws=ws, controller=ctl, startup=nothing,
                              shutdown=nothing, health=lambda: {"status": "ok"})
    app = create_app(runtime)
    transport = httpx.ASGITransport(app=app)

    def body(visit_id, patient, second):
        return {"request_id": f"h-{visit_id}", "trigger": {
            "visit_id": visit_id, "patient_id": patient, "doctor_id": "DR0042",
            "hospital_id": "CMED-DHK-BANANI-01", "start_time": at(0, second),
            "date": "2026-09-13"}}

    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:5050") as http:
        url = "/api/v1/session/start"
        # No Origin, or one not registered: refused, nothing recorded.
        r = await http.post(url, json=body("VIS-A", "P1", 0))
        assert r.status_code == 403 and r.json()["error"]["code"] == "ORIGIN_NOT_ALLOWED"
        r = await http.post(url, json=body("VIS-A", "P1", 0),
                            headers={"Origin": "https://evil.example"})
        assert r.status_code == 403 and ctl.state != "recording"

        first = await http.post(url, json=body("VIS-A", "P1", 0), headers={"Origin": origin})
        assert first.status_code == 200, first.text
        assert first.json()["code"] == "RECORDING_STARTED"
        assert first.json()["request_id"] == "h-VIS-A"
        assert first.json()["data"]["visit_id"] == "VIS-A"

        nxt = await http.post(url, json=body("VIS-B", "P2", 30), headers={"Origin": origin})
        closed = nxt.json()["data"]["closed_previous"]
        assert closed["visit_id"] == "VIS-A"
        assert closed["session_id"] == first.json()["data"]["session_id"]

        bad = await http.post(url, json=body("VIS_C", "P3", 40), headers={"Origin": origin})
        assert bad.status_code == 400 and bad.json()["code"] == "INVALID_IDENTIFIER"
    await ctl.close()


async def test_a_start_overtaken_while_it_waits_for_the_server_still_gets_its_reply(
        monkeypatch, spool, device_key):
    """
    Two patients opened a second apart: the first start is still waiting for the
    server's grant when the second ends its recording. The first must answer,
    not drop the page's connection (found on a live run, 27 Oct).
    """
    from core.session_controller import SessionController  # noqa: F401
    from core.uploader import AuthOutcome
    from tests.fakes import ScriptedUploader, make_controller
    granted = AuthOutcome("granted", "GRANTED", "", None, "confirmed")
    ctl = make_controller(monkeypatch, spool, device_key,
                          ScriptedUploader([granted, granted], delay=1.0),
                          authorise_wait_seconds=1.5, limit_poll_seconds=0.01)
    first = asyncio.create_task(ctl.open_session(visit("VIS-A", "P1")))
    await asyncio.sleep(0.2)
    second = await ctl.open_session(visit("VIS-B", "P2", at(0, 30)))
    answered = await asyncio.wait_for(first, 3)
    assert answered["status"] == "recording" and answered["visit_id"] == "VIS-A"
    assert second["previous_visit_id"] == "VIS-A"
    assert ctl._active.visit_id == "VIS-B"
    await ctl.close()


# ---------------------------------------------------------------- the microphone

def test_the_jabra_is_found_under_the_name_windows_cuts_short(monkeypatch):
    """MME lists 'Echo Cancelling Speakerphone (J'; Windows' full name has 'Jabra'."""
    from core import microphone
    from core.recorder import AudioRecorder
    monkeypatch.setattr(microphone, "endpoint_names", lambda: [
        "Echo Cancelling Speakerphone (Jabra Speak2 55 MS)",
        "Microphone Array (2- Realtek(R) Audio)"])

    class Handle:
        devices = [{"name": "Microsoft Sound Mapper - Input", "maxInputChannels": 2, "hostApi": 0},
                   {"name": "Microphone Array (2- Realtek(R)", "maxInputChannels": 2, "hostApi": 0},
                   {"name": "Echo Cancelling Speakerphone (J", "maxInputChannels": 1, "hostApi": 0}]

        def get_default_host_api_info(self):
            return {"index": 0}

        def get_device_count(self):
            return len(self.devices)

        def get_device_info_by_index(self, i):
            return self.devices[i]

    assert AudioRecorder._find(Handle(), "Jabra") == 2
    assert AudioRecorder._name_of(Handle(), 2) == "Echo Cancelling Speakerphone (Jabra Speak2 55 MS)"
    assert not microphone.same_device("Echo", "Echo Cancelling Speakerphone (Jabra Speak2 55 MS)")


async def test_the_heartbeat_says_which_microphone_records(monkeypatch, spool, device_key):
    ctl = ctl_with(monkeypatch, spool, device_key)
    await ctl.open_session(visit("VIS-A"))
    ctl.cfg.audio.preferred_microphone = "Jabra"
    ctl._active.recorder.device_name = "Echo Cancelling Speakerphone (Jabra Speak2 55 MS)"
    beat = ctl._microphone_report(ctl._active)
    assert beat["microphone"].startswith("Echo Cancelling") and beat["microphone_is_clinic"] is True
    ctl._active.recorder.device_name = "Microphone Array (2- Realtek(R) Audio)"
    assert ctl._microphone_report(ctl._active)["microphone_is_clinic"] is False
    await ctl.close()
