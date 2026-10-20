"""
What the dashboard needs from each PC (U3, X1, S4): recordings held because the
server said no, whether the disk is encrypted, and which recording a resumed
one continues.
"""
from __future__ import annotations

from types import SimpleNamespace

from core.start_keeper import StartKeeper
from core.uploader import AuthOutcome, UploadManager
from tests.fakes import ScriptedUploader, make_controller, trigger

GRANTED = AuthOutcome("granted", "GRANTED", "", None, "confirmed")
AUDIO = {"sample_rate": 4000, "channels": 1, "sample_width": 2}


def a_session(spool, device_key, **kw):
    return spool.open_session(device_key=device_key, device_id="D", doctor_id="DR",
                              hospital_id="H", patient_ref="P", consent_method="reception",
                              audio=AUDIO, trigger={"patient_id": "P"}, **kw)


def uploader(spool, device_key):
    cfg = SimpleNamespace(
        security=SimpleNamespace(grant_issuer="aimslab", grant_audience="aimscribe-recorder"),
        backend=SimpleNamespace(url=lambda e: e, request_timeout=5, retry_backoff=(0.0,)),
        spool=SimpleNamespace(purge_grace_hours=0), app_version="3.4.0", protocol_version=2)
    return UploadManager(cfg, device_key=device_key, spool=spool, receipt_public_key=None)


def test_held_recordings_are_listed_with_why_and_since_when(spool, device_key):
    up = uploader(spool, device_key)
    held, fine = a_session(spool, device_key), a_session(spool, device_key)
    held.record_authorisation_held("CLINIC_NOT_MAPPED")
    held.record_authorisation_held("CLINIC_NOT_MAPPED")      # asked again: same since
    up._sessions = [held, fine]
    listed = up.held_sessions()
    assert [(h["session_id"], h["code"]) for h in listed] == [
        (held.session_id, "CLINIC_NOT_MAPPED")]
    assert listed[0]["since"].endswith("Z")

    # Across a restart the reason and its first moment come back from the journal.
    again = next(s for s in spool.recover(device_key) if s.session_id == held.session_id)
    up._sessions = [again]
    assert up.held_sessions()[0]["since"] == listed[0]["since"]

    held.record_authorised("jti-1", "confirmed")             # the server said yes after all
    up._sessions = [held]
    assert up.held_sessions() == []


async def test_the_heartbeat_carries_held_and_disk_facts(monkeypatch, spool, device_key):
    class Holding(ScriptedUploader):
        def held_sessions(self):
            return [{"session_id": f"S{n}", "code": "DEVICE_REVOKED", "since": None}
                    for n in range(25)]

    ctl = make_controller(monkeypatch, spool, device_key, Holding([GRANTED]))
    ctl.cfg.app_version, ctl.cfg.protocol_version = "3.4.0", 2
    ctl._drive.encrypted = False
    payload = ctl.heartbeat_payload()
    assert payload["held_count"] == 25 and len(payload["held"]) == 20
    assert payload["disk_encrypted"] is False
    assert payload["app_version"] == "3.4.0"
    assert isinstance(payload["disk_free_bytes"], int)
    await ctl.close()


async def test_a_resumed_recording_says_which_one_it_continues(monkeypatch, spool, device_key,
                                                               tmp_path):
    ctl = make_controller(monkeypatch, spool, device_key, ScriptedUploader([GRANTED, GRANTED]),
                          resume_within_seconds=600)
    ctl._keeper = StartKeeper(tmp_path / "state")
    first = await ctl.open_session(trigger("P1"))
    ctl._active, ctl.state = None, "idle"                    # the process dies
    await ctl._resume_after_restart()
    resumed = ctl._active.spool
    assert resumed.session_id != first["session_id"]
    assert resumed.meta["continues_session_id"] == first["session_id"]

    # It survives a restart of its own, and goes to the server with the open.
    again = next(s for s in spool.recover(device_key) if s.session_id == resumed.session_id)
    assert again.meta["continues_session_id"] == first["session_id"]
    up = uploader(spool, device_key)
    sent = {}

    async def post(endpoint, payload, attempts=None):
        sent[endpoint] = payload
        return {"status": "opened"}

    up._post = post
    assert await up._open_remote(again)
    assert sent["/session/open"]["continues_session_id"] == first["session_id"]
    await ctl.close()


def test_a_fresh_recording_continues_nothing(spool, device_key):
    session = a_session(spool, device_key)
    assert "continues_session_id" not in session.meta
