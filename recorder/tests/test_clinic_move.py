"""
3.9.3: a PC moved to another clinic on the dashboard learns it by itself.

17 Nov 2026, live: CMED's PC joined at TEST and was moved to AALO_KARAIL on
the dashboard. The server then minted every grant for AALO_KARAIL, while the
PC still held TEST in its identity file and refused each one as "for another
clinic". Recording stopped until the PC was made to join again by hand, and
the recordings made in between stayed held on the PC.

Now the server names the PC's clinic in every heartbeat reply; the PC saves
it, records the next patient there, and asks again at once for the recordings
held because their grant named the new clinic.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from api import trigger_server as ts
from core import enrollment
from core.enrollment import DeviceIdentity
from tests.fakes import ScriptedUploader, make_controller
from tests.test_authorisation import open_spooled, server_grant, server_key, uploader_for  # noqa: F401


# ------------------------------------------------------------------ the grant check

async def test_a_recording_held_across_a_move_is_granted_for_the_new_clinic(
        spool, device_key, server_key):
    session = open_spooled(spool, device_key)            # made at HOSP003
    uploader = uploader_for(spool, device_key, server_key,
                            [(200, {"grant": server_grant(server_key, hospital="HOSP009")})])
    uploader.hospital_id = "HOSP009"                     # the server moved the PC
    outcome = await uploader.authorise(session)
    assert outcome.status == "granted"


async def test_a_grant_for_a_third_clinic_is_still_refused(spool, device_key, server_key):
    session = open_spooled(spool, device_key)
    uploader = uploader_for(spool, device_key, server_key,
                            [(200, {"grant": server_grant(server_key, hospital="HOSP001")})])
    uploader.hospital_id = "HOSP009"
    outcome = await uploader.authorise(session)
    assert (outcome.status, outcome.code) == ("held", "AUTHORISATION_FAILED")


async def test_without_a_move_the_old_rule_holds(spool, device_key, server_key):
    session = open_spooled(spool, device_key)
    uploader = uploader_for(spool, device_key, server_key,
                            [(200, {"grant": server_grant(server_key, hospital="HOSP009")})])
    uploader.hospital_id = "HOSP003"
    assert (await uploader.authorise(session)).status == "held"


def test_a_move_releases_only_the_recordings_held_for_their_grant(spool, device_key):
    uploader = uploader_for(spool, device_key)
    by_grant, by_clinic = open_spooled(spool, device_key), open_spooled(spool, device_key)
    by_grant.record_authorisation_held("AUTHORISATION_FAILED")
    by_clinic.record_authorisation_held("CLINIC_MISMATCH")
    uploader._sessions = [by_grant, by_clinic]
    assert uploader.clinic_changed("HOSP009") == 1
    assert by_grant.held_at is None and by_clinic.held_at is not None
    assert uploader.hospital_id == "HOSP009"


# ------------------------------------------------------------------ the heartbeat

async def test_the_heartbeat_reply_moves_the_pc(monkeypatch, spool, device_key):
    ctl = make_controller(monkeypatch, spool, device_key, ScriptedUploader())
    told = []
    ctl.on_clinic = told.append
    await ctl._on_heartbeat_reply({"status": "ok", "hospital_id": "HOSP003"})
    assert told == []                                    # same clinic: nothing
    await ctl._on_heartbeat_reply({"status": "ok"})
    assert told == []                                    # an older server: nothing
    await ctl._on_heartbeat_reply({"status": "ok", "hospital_id": "HOSP009"})
    assert told == ["HOSP009"]


async def test_a_pc_that_has_not_joined_ignores_it(monkeypatch, spool, device_key):
    ctl = make_controller(monkeypatch, spool, device_key, ScriptedUploader())
    ctl.device_id, told = "", []
    ctl.on_clinic = told.append
    await ctl._on_heartbeat_reply({"hospital_id": "HOSP009"})
    assert told == []


# ------------------------------------------------------------------ the runtime

def identity(hospital="TEST"):
    return DeviceIdentity(device_id="D1", hospital_id=hospital, doctor_id="",
                          enrolled_at="2026-11-17T12:05:31Z",
                          backend_url="https://server", key_fingerprint="fb394ff7ec")


def test_save_clinic_changes_the_clinic_and_nothing_else(tmp_path):
    cfg = SimpleNamespace(paths=SimpleNamespace(state_dir=tmp_path))
    moved = enrollment.save_clinic(cfg, identity(), "AALO_KARAIL")
    stored = json.loads((tmp_path / enrollment.IDENTITY_FILE).read_text(encoding="utf-8"))
    assert stored == {**json.loads(identity().to_json()), "hospital_id": "AALO_KARAIL"}
    assert moved.hospital_id == "AALO_KARAIL"


@pytest.fixture
def runtime(tmp_path):
    rt = object.__new__(ts.Runtime)
    rt.cfg = SimpleNamespace(paths=SimpleNamespace(state_dir=tmp_path))
    rt.identity = identity()
    rt.controller = SimpleNamespace(hospital_id="TEST")
    sources = []
    rt.ws = SimpleNamespace(set_register_source=lambda up, h: sources.append(h))
    rt.ws.sources = sources
    released = []
    rt.uploader = SimpleNamespace(clinic_changed=lambda h: released.append(h) or 4)
    rt.uploader.released = released
    return rt


def test_the_runtime_saves_and_applies_the_new_clinic(runtime, tmp_path):
    runtime.adopt_clinic("AALO_KARAIL")
    assert runtime.identity.hospital_id == "AALO_KARAIL"
    assert runtime.controller.hospital_id == "AALO_KARAIL"
    assert runtime.ws.sources == ["AALO_KARAIL"]
    assert runtime.uploader.released == ["AALO_KARAIL"]
    stored = json.loads((tmp_path / enrollment.IDENTITY_FILE).read_text(encoding="utf-8"))
    assert stored["hospital_id"] == "AALO_KARAIL" and stored["device_id"] == "D1"


def test_the_same_clinic_changes_nothing(runtime, tmp_path):
    runtime.adopt_clinic("TEST")
    assert runtime.uploader.released == [] and not (tmp_path / enrollment.IDENTITY_FILE).exists()
