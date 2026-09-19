"""
Channel A - the CMED page to the recorder (SRS 3.2 §6.1, §7.7, Appendix A).

Three layers, tested separately:

* protocol.py   - what a trigger or prescription_built must look like, and the
                  reply envelope;
* the manager   - one reply per command, with request_id, status and code;
* the controller - the gate: a new patient cannot end an unfinished one.
"""
from __future__ import annotations

import json
import time
from types import SimpleNamespace

import pytest

from api import protocol
from api.protocol import ProtocolError, parse_prescription_built, parse_trigger
from api.websocket_server import WebSocketManager
from core import crypto
from core.crypto import GrantError
from core.session_controller import SessionController, SessionError

TRIGGER = {
    "patient_id": "P0012345",
    "doctor_id": "DR0042",
    "hospital_id": "CMED-DHK-BANANI-01",
    "start_time": "2026-09-13T10:14:32+06:00",
    "date": "2026-09-13",
}


def _start(**changes) -> dict:
    trigger = {**TRIGGER, **changes}
    return {"command": "start", "request_id": "cmed-1", "trigger": trigger}


# ============================================================
# protocol.py
# ============================================================

def test_trigger_with_five_fields_is_read_exactly():
    trigger = parse_trigger(_start())
    assert trigger.fields() == TRIGGER
    # Kept character for character: it is matched against CMED's API 2.
    assert trigger.start_time == "2026-09-13T10:14:32+06:00"


@pytest.mark.parametrize("field", list(TRIGGER))
def test_each_missing_field_is_refused(field):
    message = _start()
    del message["trigger"][field]
    with pytest.raises(ProtocolError) as err:
        parse_trigger(message)
    assert err.value.code == "MISSING_FIELD" and err.value.status == 400
    assert err.value.field == field


@pytest.mark.parametrize("field", ["patient_id", "doctor_id", "hospital_id"])
@pytest.mark.parametrize("value", ["../etc", "P 123", "x" * 65, "P;1"])
def test_bad_identifiers_are_refused(field, value):
    with pytest.raises(ProtocolError) as err:
        parse_trigger(_start(**{field: value}))
    assert err.value.code == "INVALID_IDENTIFIER"


@pytest.mark.parametrize("value", ["2026-09-13T10:14:32", "yesterday", "10:14"])
def test_start_time_needs_a_time_zone(value):
    with pytest.raises(ProtocolError) as err:
        parse_trigger(_start(start_time=value))
    assert err.value.field == "start_time"


@pytest.mark.parametrize("value", ["13-09-2026", "2026-9-13", "2026-02-30"])
def test_date_must_be_iso(value):
    with pytest.raises(ProtocolError) as err:
        parse_trigger(_start(date=value))
    assert err.value.field == "date"


def test_trigger_object_is_required():
    with pytest.raises(ProtocolError) as err:
        parse_trigger({"command": "start", "patient_id": "P1"})
    assert err.value.field == "trigger"


def test_prescription_built_needs_patient_and_session():
    built = parse_prescription_built({"patient_id": "P1", "session_id": "01JB8X",
                                      "occurred_at": "2026-09-13T10:26:11+06:00"})
    assert (built.patient_id, built.session_id) == ("P1", "01JB8X")
    with pytest.raises(ProtocolError):
        parse_prescription_built({"patient_id": "P1"})


def test_reply_envelope_carries_request_id_status_and_code():
    reply = protocol.reply("start", "GATE_NOT_ARMED", request_id="cmed-7")
    assert reply["event"] == "error"
    assert (reply["status"], reply["code"], reply["request_id"]) == (409, "GATE_NOT_ARMED",
                                                                     "cmed-7")
    assert protocol.reply("start", "RECORDING_STARTED")["event"] == "ack"


def test_cmed_commands_cannot_reply_outside_appendix_a():
    with pytest.raises(ValueError):
        protocol.reply("start", "REFUSED")
    with pytest.raises(ValueError):
        protocol.reply("prescription_built", "OK")


# ============================================================
# The manager: one reply per command
# ============================================================

def _grant(trigger, *, jti=None, hospital_id="HOSP003", patient_ref=None) -> crypto.Grant:
    return crypto.Grant(
        jti=jti or f"jti-{time.time_ns()}", doctor_id=trigger.doctor_id, doctor_name="",
        hospital_id=hospital_id, patient_ref=patient_ref or trigger.patient_id,
        consent_obtained=True, consent_method="reception",
        expires_at=int(time.time()) + 60, raw="token")


class FakeController:
    def __init__(self):
        self.opened = []
        self.armed = []
        self.open_error = None
        self.arm_result = {"session_id": "S1", "armed": True, "already": False}

    async def open_session(self, grant, *, trigger_start_time=""):
        if self.open_error:
            raise self.open_error
        self.opened.append((grant, trigger_start_time))
        return {"session_id": "S1", "started_at": "2026-09-13T04:14:32Z",
                "previous_session_id": None}

    async def arm(self, *, patient_id, session_id):
        self.armed.append((patient_id, session_id))
        if isinstance(self.arm_result, Exception):
            raise self.arm_result
        return self.arm_result

    def status(self):
        return {"state": "recording", "armed": False}


def _manager(make_security, *, authoriser=None, **security):
    cfg = SimpleNamespace(security=make_security(**security))
    manager = WebSocketManager(cfg, authoriser=authoriser)
    controller = FakeController()
    manager.set_controller(controller)
    return manager, controller


async def _send(manager, message) -> dict:
    return await manager.handle_message(None, json.dumps(message))


async def test_start_records_and_echoes_request_id(make_security):
    calls = []

    async def authorise(trigger):
        calls.append(trigger)
        return _grant(trigger)

    manager, controller = _manager(make_security, authoriser=authorise)
    reply = await _send(manager, _start())

    assert (reply["status"], reply["code"]) == (200, "RECORDING_STARTED")
    assert reply["request_id"] == "cmed-1"
    assert reply["data"]["session_id"] == "S1" and reply["data"]["armed"] is False
    # The five fields went to the server, and start_time reached the controller.
    assert calls[0].fields() == TRIGGER
    assert controller.opened[0][1] == TRIGGER["start_time"]


async def test_missing_doctor_is_refused_before_anything_else(make_security):
    """AT-02: nothing is authorised or recorded for a trigger with no doctor."""
    async def authorise(trigger):
        raise AssertionError("must not be called")

    manager, controller = _manager(make_security, authoriser=authorise)
    message = _start()
    del message["trigger"]["doctor_id"]
    reply = await _send(manager, message)
    assert (reply["status"], reply["code"]) == (400, "MISSING_FIELD")
    assert controller.opened == []


async def test_no_authoriser_means_not_ready_not_unauthorised(make_security):
    manager, controller = _manager(make_security)          # require_grant=True
    reply = await _send(manager, _start())
    assert (reply["status"], reply["code"]) == (503, "AGENT_NOT_READY")
    assert controller.opened == []


async def test_refused_authorisation_is_401(make_security):
    async def authorise(trigger):
        raise GrantError("server refused")

    manager, controller = _manager(make_security, authoriser=authorise)
    reply = await _send(manager, _start())
    assert (reply["status"], reply["code"]) == (401, "AUTHORISATION_FAILED")
    assert controller.opened == []


async def test_grant_for_another_patient_is_refused(make_security):
    async def authorise(trigger):
        return _grant(trigger, patient_ref="SOMEONE_ELSE")

    manager, controller = _manager(make_security, authoriser=authorise)
    reply = await _send(manager, _start())
    assert reply["code"] == "AUTHORISATION_FAILED"
    assert controller.opened == []


async def test_a_grant_works_once(make_security):
    """AT-07: the same grant presented twice is refused the second time."""
    async def authorise(trigger):
        return _grant(trigger, jti="same")

    manager, _ = _manager(make_security, authoriser=authorise)
    assert (await _send(manager, _start()))["code"] == "RECORDING_STARTED"
    assert (await _send(manager, _start()))["code"] == "AUTHORISATION_FAILED"


async def test_controller_refusal_keeps_its_code(make_security):
    async def authorise(trigger):
        return _grant(trigger)

    manager, controller = _manager(make_security, authoriser=authorise)
    controller.open_error = SessionError("not finished", code="GATE_NOT_ARMED")
    reply = await _send(manager, _start())
    assert (reply["status"], reply["code"]) == (409, "GATE_NOT_ARMED")


async def test_generic_refusal_on_start_stays_inside_appendix_a(make_security):
    async def authorise(trigger):
        return _grant(trigger)

    manager, controller = _manager(make_security, authoriser=authorise)
    controller.open_error = SessionError("something")          # default code REFUSED
    reply = await _send(manager, _start())
    assert reply["code"] in {"AGENT_NOT_READY"}


async def test_prescription_built_arms(make_security):
    manager, controller = _manager(make_security)
    reply = await _send(manager, {"command": "prescription_built", "request_id": "cmed-2",
                                  "patient_id": "P0012345", "session_id": "S1"})
    assert (reply["status"], reply["code"], reply["request_id"]) == (200, "GATE_ARMED",
                                                                     "cmed-2")
    assert controller.armed == [("P0012345", "S1")]

    controller.arm_result = {"session_id": "S1", "armed": True, "already": True}
    reply = await _send(manager, {"command": "prescription_built",
                                  "patient_id": "P0012345", "session_id": "S1"})
    assert reply["code"] == "GATE_ALREADY_ARMED"


async def test_prescription_built_for_another_patient(make_security):
    manager, controller = _manager(make_security)
    controller.arm_result = SessionError("different", code="PATIENT_MISMATCH")
    reply = await _send(manager, {"command": "prescription_built",
                                  "patient_id": "P9", "session_id": "S1"})
    assert (reply["status"], reply["code"]) == (409, "PATIENT_MISMATCH")


async def test_oversized_and_malformed_frames(make_security):
    """AT-31: refused, with a reply, and the connection kept."""
    manager, _ = _manager(make_security)
    assert (await manager.handle_message(None, "x" * (65 * 1024)))["code"] == "MALFORMED_MESSAGE"
    assert (await manager.handle_message(None, "{not json"))["code"] == "MALFORMED_MESSAGE"
    assert (await manager.handle_message(None, "[1,2]"))["code"] == "MALFORMED_MESSAGE"


async def test_unknown_extra_fields_are_ignored(make_security):
    """AT-32: a field the recorder does not know does not break the command."""
    async def authorise(trigger):
        return _grant(trigger)

    manager, _ = _manager(make_security, authoriser=authorise)
    message = _start()
    message["trigger"]["ward"] = "OPD-2"
    message["something_new"] = {"a": 1}
    assert (await _send(manager, message))["code"] == "RECORDING_STARTED"


async def test_development_mode_needs_no_server(make_security):
    manager, controller = _manager(make_security, require_grant=False)
    reply = await _send(manager, _start())
    assert reply["code"] == "RECORDING_STARTED"
    grant = controller.opened[0][0]
    # The page's hospital is never used as the clinic: that is the PC's.
    assert grant.hospital_id == "" and grant.patient_ref == "P0012345"


# ============================================================
# The controller: the gate, and the clinic
# ============================================================

class FakeRecorder:
    def __init__(self, **kwargs):
        self.is_running = False
        self.bytes_per_second = 88200
        self.duration_seconds = 0.0

    def start(self):
        self.is_running = True

    def stop(self):
        self.is_running = False
        return SimpleNamespace(bytes_captured=0, overruns=0, read_errors=0)


class FakeSegmenter:
    def __init__(self, **kwargs):
        pass

    def start(self, at):
        pass

    def stop(self, seal_remaining=True):
        pass

    def flush(self, is_final=False):
        pass

    def set_segment_start(self, at):
        pass

    def submit(self, chunk):
        pass


class FakeUploader:
    def __init__(self):
        self.closed = []

    async def track(self, session):
        pass

    def nudge(self):
        pass

    async def close_remote(self, session, **kwargs):
        self.closed.append(session.session_id)

    def status(self):
        return {"spool_bytes": 0, "spool_pressure": "ok", "pending_segments": 0}


@pytest.fixture
def controller(monkeypatch, spool, device_key):
    import core.session_controller as sc
    monkeypatch.setattr(sc, "AudioRecorder", FakeRecorder)
    monkeypatch.setattr(sc, "Segmenter", FakeSegmenter)

    cfg = SimpleNamespace(
        audio=SimpleNamespace(sample_rate=44100, channels=1, sample_width=2,
                              bytes_per_second=88200, frames_per_buffer=4096,
                              input_device_index=None),
        segment=SimpleNamespace(min_seconds=30, max_seconds=60, grace_seconds=15,
                                silence_rms=320, silence_hold_seconds=3.0),
        ops=SimpleNamespace(redact_logs=True, heartbeat_seconds=30),
        pause=SimpleNamespace(reasons=("other",), self_authorise_seconds=300),
        spool_seconds=lambda: 4 * 1024 ** 3 / 88200,
    )
    events = []
    ctl = SessionController(cfg, device_key=device_key, spool=spool,
                            uploader=FakeUploader(),
                            on_event=lambda name, data: events.append((name, data)))
    ctl.device_id = "DEV1"
    ctl.hospital_id = "HOSP003"
    ctl.events = events
    return ctl


def _g(patient="P1", *, hospital="HOSP003", jti=None) -> crypto.Grant:
    return crypto.Grant(jti=jti or f"j-{time.time_ns()}", doctor_id="DR0042", doctor_name="",
                        hospital_id=hospital, patient_ref=patient, consent_obtained=True,
                        consent_method="reception", expires_at=int(time.time()) + 60, raw="")


async def test_a_session_starts_unarmed(controller):
    result = await controller.open_session(_g(), trigger_start_time="T1")
    assert result["armed"] is False
    assert controller.status()["armed"] is False


async def test_unarmed_session_refuses_the_next_patient(controller):
    """AT-09: a stray trigger is refused and the recording continues."""
    first = await controller.open_session(_g("P1"), trigger_start_time="T1")
    with pytest.raises(SessionError) as err:
        await controller.open_session(_g("P2"), trigger_start_time="T2")
    assert err.value.code == "GATE_NOT_ARMED"
    assert controller.status()["session_id"] == first["session_id"]
    assert controller.state == "recording"
    assert any(name == "trigger_refused" for name, _ in controller.events)


async def test_armed_session_hands_over(controller):
    """AT-10: after prescription_built, the next trigger closes one and opens the next."""
    first = await controller.open_session(_g("P1"), trigger_start_time="T1")
    armed = await controller.arm(patient_id="P1", session_id=first["session_id"])
    assert armed["already"] is False
    assert controller.status()["armed"] is True

    second = await controller.open_session(_g("P2"), trigger_start_time="T2")
    assert second["previous_session_id"] == first["session_id"]
    assert controller.status()["session_id"] == second["session_id"]
    assert controller.status()["armed"] is False


async def test_arming_for_another_patient_is_refused(controller):
    """AT-11: the gate stays unarmed."""
    first = await controller.open_session(_g("P1"), trigger_start_time="T1")
    with pytest.raises(SessionError) as err:
        await controller.arm(patient_id="P9", session_id=first["session_id"])
    assert err.value.code == "PATIENT_MISMATCH"
    with pytest.raises(SessionError) as err:
        await controller.arm(patient_id="P1", session_id="NOT-THIS-ONE")
    assert err.value.code == "PATIENT_MISMATCH"
    assert controller.status()["armed"] is False


async def test_arming_twice_is_harmless(controller):
    """AT-12."""
    first = await controller.open_session(_g("P1"), trigger_start_time="T1")
    await controller.arm(patient_id="P1", session_id=first["session_id"])
    again = await controller.arm(patient_id="P1", session_id=first["session_id"])
    assert again["already"] is True


async def test_arming_with_nothing_recording(controller):
    with pytest.raises(SessionError) as err:
        await controller.arm(patient_id="P1", session_id="S")
    assert err.value.code == "NO_ACTIVE_SESSION"


async def test_the_same_trigger_twice_is_not_a_new_patient(controller):
    await controller.open_session(_g("P1"), trigger_start_time="T1")
    with pytest.raises(SessionError) as err:
        await controller.open_session(_g("P1"), trigger_start_time="T1")
    assert err.value.code == "SESSION_ALREADY_ACTIVE"


async def test_clinic_mismatch_is_refused(controller):
    """Decision D1 / SRS-GRT-10: never filed, never warned-and-recorded."""
    with pytest.raises(SessionError) as err:
        await controller.open_session(_g(hospital="HOSP001"), trigger_start_time="T1")
    assert err.value.code == "CLINIC_MISMATCH"
    assert controller.state == "idle"
    assert any(data.get("alert_type") == "clinic_mismatch"
               for name, data in controller.events if name == "integrity_alert")


async def test_recording_is_filed_under_the_pcs_clinic(controller):
    result = await controller.open_session(_g(hospital=""), trigger_start_time="T1")
    assert controller.status()["hospital_id"] == "HOSP003"
    assert result["session_id"]


async def test_unenrolled_pc_cannot_record(controller):
    controller.device_id = ""
    with pytest.raises(SessionError) as err:
        await controller.open_session(_g(), trigger_start_time="T1")
    assert err.value.code == "DEVICE_NOT_ENROLLED"


async def test_stop_works_whether_or_not_armed(controller):
    """SRS-GAT-05: the doctor's Stop is never blocked by the gate."""
    await controller.open_session(_g("P1"), trigger_start_time="T1")
    result = await controller.stop_session(reason="doctor_stopped")
    assert result["status"] == "stopped"
    assert controller.state == "idle"
