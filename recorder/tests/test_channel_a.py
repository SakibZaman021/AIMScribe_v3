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

from tests.fakes import ScriptedUploader, make_controller, trigger as _trigger  # noqa: E402


class FakeController:
    def __init__(self, authorisation="granted"):
        self.opened = []
        self.armed = []
        self.stopped = []
        self.open_error = None
        self.authorisation = authorisation
        self.arm_result = {"session_id": "S1", "armed": True, "already": False}

    async def open_session(self, trigger, *, grant=None):
        if self.open_error:
            raise self.open_error
        self.opened.append((trigger, grant))
        return {"session_id": "S1", "started_at": "2026-09-13T04:14:32Z",
                "previous_session_id": None, "authorisation": self.authorisation,
                "confirmation": "confirming"}

    async def arm(self, *, patient_id, session_id):
        self.armed.append((patient_id, session_id))
        if isinstance(self.arm_result, Exception):
            raise self.arm_result
        return self.arm_result

    async def stop_session(self, *, reason="doctor_stopped"):
        self.stopped.append(reason)
        return {"status": "stopped", "reason": reason}

    def status(self):
        return {"state": "recording", "armed": False}


def _manager(make_security, *, authorisation="granted", **security):
    cfg = SimpleNamespace(security=make_security(**security))
    manager = WebSocketManager(cfg)
    controller = FakeController(authorisation)
    manager.set_controller(controller)
    return manager, controller


async def _send(manager, message) -> dict:
    return await manager.handle_message(None, json.dumps(message))


async def test_start_granted_is_200_and_echoes_request_id(make_security):
    """AT-01 shape: the server said yes within the wait."""
    manager, controller = _manager(make_security)
    reply = await _send(manager, _start())
    assert (reply["status"], reply["code"]) == (200, "RECORDING_STARTED")
    assert reply["request_id"] == "cmed-1"
    assert reply["data"]["session_id"] == "S1" and reply["data"]["armed"] is False
    trigger, grant = controller.opened[0]
    assert trigger.fields() == TRIGGER
    assert grant is None                 # production: the server is asked, not the page


async def test_start_still_being_authorised_is_202(make_security):
    """SRS-GRT-07: recording, while the server is still being asked."""
    manager, _ = _manager(make_security, authorisation="pending")
    reply = await _send(manager, _start())
    assert (reply["status"], reply["code"]) == (202, "RECORDING_PROVISIONAL")
    assert reply["event"] == "ack"


async def test_missing_doctor_is_refused_before_anything_else(make_security):
    """AT-02: nothing is recorded for a trigger with no doctor."""
    manager, controller = _manager(make_security)
    message = _start()
    del message["trigger"]["doctor_id"]
    reply = await _send(manager, message)
    assert (reply["status"], reply["code"]) == (400, "MISSING_FIELD")
    assert controller.opened == []


@pytest.mark.parametrize("code,status", [("GATE_NOT_ARMED", 409), ("CLINIC_MISMATCH", 401),
                                         ("DEVICE_NOT_ENROLLED", 423)])
async def test_controller_refusal_keeps_its_code(make_security, code, status):
    manager, controller = _manager(make_security)
    controller.open_error = SessionError("refused", code=code)
    reply = await _send(manager, _start())
    assert (reply["status"], reply["code"]) == (status, code)


async def test_generic_refusal_on_start_stays_inside_appendix_a(make_security):
    manager, controller = _manager(make_security)
    controller.open_error = SessionError("something")          # default code REFUSED
    assert (await _send(manager, _start()))["code"] == "AGENT_NOT_READY"


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


async def test_stop_carries_its_reason(make_security):
    """The refusal reason reaches the controller (SRS 3.2 §7.8a)."""
    manager, controller = _manager(make_security)
    await _send(manager, {"command": "stop", "reason": "patient_did_not_consent"})
    await _send(manager, {"command": "stop"})
    assert controller.stopped == ["patient_did_not_consent", "doctor_stopped"]


async def test_oversized_and_malformed_frames(make_security):
    """AT-31: refused, with a reply, and the connection kept."""
    manager, _ = _manager(make_security)
    assert (await manager.handle_message(None, "x" * (65 * 1024)))["code"] == "MALFORMED_MESSAGE"
    assert (await manager.handle_message(None, "{not json"))["code"] == "MALFORMED_MESSAGE"
    assert (await manager.handle_message(None, "[1,2]"))["code"] == "MALFORMED_MESSAGE"


async def test_unknown_extra_fields_are_ignored(make_security):
    """AT-32."""
    manager, _ = _manager(make_security)
    message = _start()
    message["trigger"]["ward"] = "OPD-2"
    message["something_new"] = {"a": 1}
    assert (await _send(manager, message))["code"] == "RECORDING_STARTED"


async def test_development_mode_supplies_its_own_grant(make_security):
    manager, controller = _manager(make_security, require_grant=False)
    assert (await _send(manager, _start()))["code"] == "RECORDING_STARTED"
    grant = controller.opened[0][1]
    # The page's hospital is never used as the clinic: that is the PC's.
    assert grant.hospital_id == "" and grant.patient_ref == "P0012345"


# ============================================================
# The controller: the gate, and the clinic
# ============================================================

@pytest.fixture
def controller(monkeypatch, spool, device_key):
    return make_controller(monkeypatch, spool, device_key, ScriptedUploader())


def _g(patient="P1", *, hospital="HOSP003") -> crypto.Grant:
    return crypto.Grant(jti=f"j-{time.time_ns()}", doctor_id="DR0042", doctor_name="",
                        hospital_id=hospital, patient_ref=patient, consent_obtained=True,
                        consent_method="reception", expires_at=int(time.time()) + 60, raw="")


async def _open(controller, patient="P1", start="2026-09-13T10:00:00+06:00", **grant):
    return await controller.open_session(_trigger(patient, start), grant=_g(patient, **grant))


async def test_a_session_starts_unarmed(controller):
    result = await _open(controller)
    assert (result["armed"], result["authorisation"]) == (False, "granted")
    assert controller.status()["armed"] is False


async def test_unarmed_session_refuses_the_next_patient(controller):
    """AT-09: a stray trigger is refused and the recording continues."""
    first = await _open(controller, "P1")
    with pytest.raises(SessionError) as err:
        await _open(controller, "P2", "2026-09-13T10:05:00+06:00")
    assert err.value.code == "GATE_NOT_ARMED"
    assert controller.status()["session_id"] == first["session_id"]
    assert controller.state == "recording"
    assert any(name == "trigger_refused" for name, _ in controller.events)


async def test_armed_session_hands_over(controller):
    """AT-10: after prescription_built, the next trigger closes one and opens the next."""
    first = await _open(controller, "P1")
    armed = await controller.arm(patient_id="P1", session_id=first["session_id"])
    assert armed["already"] is False and controller.status()["armed"] is True

    second = await _open(controller, "P2", "2026-09-13T10:12:00+06:00")
    assert second["previous_session_id"] == first["session_id"]
    assert controller.status()["session_id"] == second["session_id"]
    assert controller.status()["armed"] is False


async def test_arming_for_another_patient_is_refused(controller):
    """AT-11: the gate stays unarmed."""
    first = await _open(controller, "P1")
    for patient, session in (("P9", first["session_id"]), ("P1", "NOT-THIS-ONE")):
        with pytest.raises(SessionError) as err:
            await controller.arm(patient_id=patient, session_id=session)
        assert err.value.code == "PATIENT_MISMATCH"
    assert controller.status()["armed"] is False


async def test_arming_twice_is_harmless(controller):
    """AT-12."""
    first = await _open(controller, "P1")
    await controller.arm(patient_id="P1", session_id=first["session_id"])
    assert (await controller.arm(patient_id="P1", session_id=first["session_id"]))["already"]


async def test_arming_with_nothing_recording(controller):
    with pytest.raises(SessionError) as err:
        await controller.arm(patient_id="P1", session_id="S")
    assert err.value.code == "NO_ACTIVE_SESSION"


async def test_the_same_trigger_twice_is_not_a_new_patient(controller):
    await _open(controller, "P1")
    with pytest.raises(SessionError) as err:
        await _open(controller, "P1")
    assert err.value.code == "SESSION_ALREADY_ACTIVE"


async def test_clinic_mismatch_is_refused(controller):
    """Decision D1 / SRS-GRT-10: never filed, never warned-and-recorded."""
    with pytest.raises(SessionError) as err:
        await _open(controller, hospital="HOSP001")
    assert err.value.code == "CLINIC_MISMATCH"
    assert controller.state == "idle"


async def test_recording_is_filed_under_the_pcs_clinic(controller):
    await _open(controller, hospital="")
    assert controller.status()["hospital_id"] == "HOSP003"


async def test_unenrolled_pc_cannot_record(controller):
    controller.device_id = ""
    with pytest.raises(SessionError) as err:
        await _open(controller)
    assert err.value.code == "DEVICE_NOT_ENROLLED"


async def test_stop_works_whether_or_not_armed(controller):
    """SRS-GAT-05: the doctor's Stop is never blocked by the gate."""
    await _open(controller, "P1")
    result = await controller.stop_session(reason="doctor_stopped")
    assert result["status"] == "stopped" and controller.state == "idle"
