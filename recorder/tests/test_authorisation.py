"""
Phase 3 of the v3 upgrade: the recorder asks the AIMS LAB server for each
consultation's grant while it records (SRS 3.2 §5, §5.6, §7.8a).

* the buffer remembers where each consultation stands, across a restart;
* the uploader checks every grant, and sends nothing before one;
* the controller records first; a "no" from the server holds the recording
  on the PC and asks again - it never stops or deletes it;
* a patient's refusal deletes the consultation and reaches the server.
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from core import crypto
from core.session_controller import REFUSAL_REASON, SessionError
from core.spool import PURGED, SessionSpool
from core.uploader import AuthOutcome, UploadManager
from tests.conftest import pcm
from tests.fakes import ScriptedUploader, make_controller, trigger

TRIGGER = trigger("P0012345", "2026-09-13T10:14:32+06:00").fields()


# ============================================================
# Helpers
# ============================================================

@pytest.fixture
def server_key():
    return Ed25519PrivateKey.generate()


def server_grant(key, *, patient="P0012345", doctor="DR0042", hospital="HOSP003",
                 jti=None, issuer="aimslab"):
    now = int(time.time())
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption())
    return jwt.encode({"iss": issuer, "aud": "aimscribe-recorder", "sub": doctor,
                       "iat": now, "exp": now + 60, "jti": jti or f"jti-{time.time_ns()}",
                       "patient_ref": patient, "hospital_id": hospital},
                      pem, algorithm="EdDSA")


def open_spooled(spool, device_key, *, trigger_fields=TRIGGER) -> SessionSpool:
    return spool.open_session(
        device_key=device_key, device_id="DEV1", doctor_id="DR0042", hospital_id="HOSP003",
        patient_ref="P0012345", consent_method="reception",
        audio={"sample_rate": 44100, "channels": 1, "sample_width": 2},
        trigger=trigger_fields)


def seal(session, seconds=1.0, *, ended_ago=timedelta(seconds=1)):
    end = datetime.now(timezone.utc) - ended_ago
    return session.seal_segment(pcm(seconds, value=500),
                                captured_start_at=end - timedelta(seconds=seconds),
                                captured_end_at=end, rms_mean=500.0, is_final=False)


def reload(spool, device_key, session) -> SessionSpool:
    return SessionSpool.load(session.directory, device_key=device_key, spool_key=spool._key)


def uploader_for(spool, device_key, server_key=None, responses=()):
    cfg = SimpleNamespace(
        security=SimpleNamespace(grant_issuer="aimslab", grant_audience="aimscribe-recorder"),
        backend=SimpleNamespace(url=lambda e: f"https://server{e}", request_timeout=5,
                                retry_backoff=(0.0,)),
        spool=SimpleNamespace(purge_grace_hours=0),
        app_version="3.0.0", protocol_version=2,
    )
    events = []
    uploader = UploadManager(cfg, device_key=device_key, spool=spool,
                             receipt_public_key=None,
                             on_event=lambda name, data: events.append((name, data)))
    uploader.events = events
    if server_key is not None:
        uploader.set_grant_key(server_key.public_key())
    uploader.calls = []
    script = list(responses)

    async def request_raw(method, endpoint, body):
        uploader.calls.append((method, endpoint, body))
        reply = script.pop(0) if len(script) > 1 else script[0]
        return reply() if callable(reply) else reply

    uploader._request_raw = request_raw
    uploader._running = True
    return uploader


# ============================================================
# The buffer remembers
# ============================================================

def test_trigger_is_kept_and_survives_a_restart(spool, device_key):
    session = open_spooled(spool, device_key)
    assert session.needs_authorisation
    again = reload(spool, device_key, session)
    assert again.needs_authorisation and again.meta["trigger"] == TRIGGER


def test_grant_and_hold_survive_a_restart(spool, device_key):
    granted = open_spooled(spool, device_key)
    granted.record_authorised("jti-1", "confirmed")
    again = reload(spool, device_key, granted)
    assert (again.authorisation, again.grant_jti, again.confirmation) == (
        "granted", "jti-1", "confirmed")

    held = open_spooled(spool, device_key)
    assert held.record_authorisation_held("CLINIC_MISMATCH")
    assert not held.record_authorisation_held("CLINIC_MISMATCH")     # journalled once
    again = reload(spool, device_key, held)
    # Still waiting for a grant, and asked again at once after a restart.
    assert (again.authorisation, again.hold_code, again.held_at) == (
        "pending", "CLINIC_MISMATCH", None)
    assert again.needs_authorisation


def test_a_session_an_older_version_refused_is_held_not_lost(spool, device_key):
    """Journals written before this change say "refused"; they load as held."""
    session = open_spooled(spool, device_key)
    session._append_journal({"rec": "authorisation_refused", "code": "AUTHORISATION_FAILED"})
    again = reload(spool, device_key, session)
    assert again.needs_authorisation and again.hold_code == "AUTHORISATION_FAILED"


def test_sessions_from_before_v3_load_as_granted(spool, device_key):
    """No trigger in the journal: an old recording uploads exactly as before."""
    old = open_spooled(spool, device_key, trigger_fields=None)
    assert reload(spool, device_key, old).authorisation == "granted"


def test_patient_refusal_deletes_pieces_and_is_remembered(spool, device_key):
    """SRS-CNS-03, -05: journalled first, then every piece deleted."""
    session = open_spooled(spool, device_key)
    for _ in range(3):
        seal(session)
    assert session.refuse() == 3
    assert list(session.directory.glob("*.aimspl")) == []
    assert all(s.state == PURGED for s in session.segments.values())

    again = reload(spool, device_key, session)
    assert again.refused and not again.needs_authorisation
    assert not again.is_complete                  # until the server acknowledges
    again.mark_refusal_reported()
    assert reload(spool, device_key, again).is_complete


# ============================================================
# The uploader checks every grant
# ============================================================

async def test_a_verified_grant_authorises(spool, device_key, server_key):
    session = open_spooled(spool, device_key)
    uploader = uploader_for(spool, device_key, server_key, [
        (200, {"grant": server_grant(server_key), "confirmation": "confirmed"})])
    outcome = await uploader.authorise(session)
    assert (outcome.status, outcome.confirmation) == ("granted", "confirmed")
    assert session.grant_jti == outcome.grant.jti
    assert uploader.calls[0][1] == "/grant/mint" and uploader.calls[0][2] == TRIGGER


@pytest.mark.parametrize("claims,why", [
    ({"patient": "SOMEONE_ELSE"}, "another patient"),
    ({"doctor": "DR9"}, "another doctor"),
    ({"hospital": "HOSP001"}, "another clinic"),
    ({"issuer": "cmed"}, "a CMED-signed grant"),
])
async def test_a_grant_for_anything_else_is_not_used(spool, device_key, server_key,
                                                     claims, why):
    session = open_spooled(spool, device_key)
    uploader = uploader_for(spool, device_key, server_key,
                            [(200, {"grant": server_grant(server_key, **claims)})])
    outcome = await uploader.authorise(session)
    assert (outcome.status, outcome.code) == ("held", "AUTHORISATION_FAILED"), why
    assert session.needs_authorisation and session.grant_jti is None


async def test_a_grant_is_used_once(spool, device_key, server_key):
    """AT-07."""
    token = server_grant(server_key, jti="same")
    uploader = uploader_for(spool, device_key, server_key, [(200, {"grant": token})])
    assert (await uploader.authorise(open_spooled(spool, device_key))).status == "granted"
    assert (await uploader.authorise(open_spooled(spool, device_key))).status == "held"


async def test_a_grant_signed_by_another_key_is_refused(spool, device_key, server_key):
    """AT-08's cousin: only the pinned key's grants count."""
    impostor = Ed25519PrivateKey.generate()
    uploader = uploader_for(spool, device_key, server_key,
                            [(200, {"grant": server_grant(impostor)})])
    assert (await uploader.authorise(open_spooled(spool, device_key))).status == "held"


@pytest.mark.parametrize("reply,code", [
    ((401, {"code": "CLINIC_MISMATCH", "message": "other clinic"}), "CLINIC_MISMATCH"),
    ((401, {"detail": "unknown device token"}), "DEVICE_NOT_ENROLLED"),
    ((403, {"detail": "device has been revoked"}), "DEVICE_NOT_ENROLLED"),
    ((400, {"code": "MISSING_FIELD"}), "MISSING_FIELD"),
])
async def test_a_no_from_the_server_holds_with_its_code(spool, device_key, server_key,
                                                       reply, code):
    session = open_spooled(spool, device_key)
    uploader = uploader_for(spool, device_key, server_key, [reply])
    outcome = await uploader.authorise(session)
    assert (outcome.status, outcome.code) == ("held", code)
    assert any(d.get("alert_type") == "authorisation_held" for _, d in uploader.events)
    assert session.needs_authorisation


async def test_a_held_recording_is_asked_again_later_not_at_once(spool, device_key,
                                                                 server_key):
    session = open_spooled(spool, device_key)
    uploader = uploader_for(spool, device_key, server_key, [
        (401, {"code": "CLINIC_MISMATCH"}),
        (200, {"grant": server_grant(server_key)})])
    assert (await uploader.authorise(session)).status == "held"
    assert (await uploader.authorise(session)).status == "held"     # too soon to ask
    assert len(uploader.calls) == 1
    session.held_at -= 301                                          # five minutes on
    assert (await uploader.authorise(session)).status == "granted"


@pytest.mark.parametrize("reply", [(None, None), (503, {"code": "AGENT_NOT_READY"}),
                                   (500, {}), (429, {})])
async def test_an_unreachable_server_leaves_it_pending(spool, device_key, server_key, reply):
    session = open_spooled(spool, device_key)
    uploader = uploader_for(spool, device_key, server_key, [reply])
    assert (await uploader.authorise(session)).status == "pending"
    assert session.needs_authorisation


async def test_without_the_pinned_key_nothing_is_authorised(spool, device_key, server_key):
    """SRS-GRT-06."""
    uploader = uploader_for(spool, device_key, None,
                            [(200, {"grant": server_grant(server_key)})])
    assert (await uploader.authorise(open_spooled(spool, device_key))).status == "held"


async def test_session_open_carries_the_grant(spool, device_key, server_key):
    session = open_spooled(spool, device_key)
    uploader = uploader_for(spool, device_key, server_key,
                            [(200, {"grant": server_grant(server_key)})])
    await uploader.authorise(session)
    sent = {}

    async def post(endpoint, payload, attempts=None):
        sent[endpoint] = payload
        return {"status": "open", "confirmation": "pending"}

    uploader._post = post
    assert await uploader._open_remote(session)
    assert sent["/session/open"]["grant_jti"] == session.grant_jti
    assert session.confirmation == "pending"


# ============================================================
# The upload loop: nothing before a grant
# ============================================================

def _no_upload(uploader):
    async def refuse(*args, **kwargs):
        raise AssertionError("nothing may be sent before a grant")
    uploader._open_remote = refuse
    uploader._send_segment = refuse


async def test_offline_recordings_wait_for_their_grant(spool, device_key, server_key):
    """SRS-UPL-06: recorded offline, nothing sent until the server answers."""
    session = open_spooled(spool, device_key)
    seal(session)
    uploader = uploader_for(spool, device_key, server_key, [(None, None)])
    _no_upload(uploader)
    await uploader.track(session)
    await uploader._drain_once()
    assert session.needs_authorisation and session.directory.exists()


async def test_a_recording_the_server_says_no_to_is_kept_unsent(spool, device_key,
                                                                server_key):
    """
    Nothing is sent without a grant - and nothing is deleted without one either.
    Once the server is fixed, the same recording uploads.
    """
    session = open_spooled(spool, device_key)
    seal(session)
    uploader = uploader_for(spool, device_key, server_key,
                            [(401, {"code": "CLINIC_MISMATCH"})])
    _no_upload(uploader)
    await uploader.track(session)
    await uploader._drain_once()
    assert session.directory.exists() and list(session.directory.glob("*.aimspl"))
    assert uploader.status()["tracked_sessions"] == 1
    assert session.needs_authorisation


async def test_the_live_consultation_is_left_to_the_controller(spool, device_key, server_key):
    session = open_spooled(spool, device_key)
    session.live = True
    uploader = uploader_for(spool, device_key, server_key, [(200, {})])
    _no_upload(uploader)
    await uploader.track(session)
    await uploader._drain_once()
    assert uploader.calls == []


async def test_a_refusal_is_delivered_then_forgotten(spool, device_key, server_key):
    """AT-77: kept until the server acknowledges, then removed."""
    session = open_spooled(spool, device_key)
    seal(session)
    session.refuse()
    uploader = uploader_for(spool, device_key, server_key, [(None, None)])
    _no_upload(uploader)
    await uploader.track(session)

    await uploader._drain_once()                              # offline
    assert session.directory.exists() and not session.refusal_reported

    async def online(method, endpoint, body):
        uploader.calls.append((method, endpoint, body))
        return 200, {"status": "refused"}

    uploader._request_raw = online
    await uploader._drain_once()
    assert uploader.calls[-1][1] == "/session/refuse"
    assert not session.directory.exists()


async def test_a_piece_waiting_fifteen_minutes_raises_one_alert(spool, device_key):
    """SRS-SPL-07."""
    session = open_spooled(spool, device_key, trigger_fields=None)
    seal(session, ended_ago=timedelta(minutes=20))
    uploader = uploader_for(spool, device_key, None, [(None, None)])
    await uploader.track(session)
    uploader._check_stall()
    uploader._check_stall()
    stalls = [d for _, d in uploader.events if d.get("alert_type") == "delivery_stalled"]
    assert len(stalls) == 1
    assert uploader.status()["oldest_pending_seconds"] >= 20 * 60


async def test_confirmation_is_asked_only_once_registered(spool, device_key):
    session = open_spooled(spool, device_key)
    uploader = uploader_for(spool, device_key, None,
                            [(200, {"confirmation": "confirmed"})])
    assert await uploader.check_confirmation(session) is None
    session.mark_acknowledged()
    assert await uploader.check_confirmation(session) == "confirmed"
    assert reload(spool, device_key, session).confirmation == "confirmed"


# ============================================================
# The controller: record first, ask alongside
# ============================================================

GRANTED = AuthOutcome("granted", "GRANTED", "", None, "pending")
CONFIRMED = AuthOutcome("granted", "GRANTED", "", None, "confirmed")
PENDING = AuthOutcome("pending", "AGENT_NOT_READY", "unreachable")
MISMATCH = AuthOutcome("held", "CLINIC_MISMATCH", "other clinic")


async def settle(seconds=0.1):
    await asyncio.sleep(seconds)


async def test_granted_within_the_wait(monkeypatch, spool, device_key):
    """AT-01 / AT-57: recording, authorised and confirmed."""
    ctl = make_controller(monkeypatch, spool, device_key, ScriptedUploader([CONFIRMED]))
    result = await ctl.open_session(trigger())
    assert (result["authorisation"], result["confirmation"]) == ("granted", "confirmed")
    assert ctl.status()["authorisation"] == "granted"


async def test_unreachable_server_records_provisionally(monkeypatch, spool, device_key):
    """SRS-GRT-07: capture never waits; the grant arrives later."""
    uploader = ScriptedUploader([PENDING, PENDING, GRANTED])
    # The second question comes after the first answer is given, as it does in
    # a clinic (10 s apart); with both at 10 ms the grant could win the race.
    ctl = make_controller(monkeypatch, spool, device_key, uploader,
                          authorise_wait_seconds=0.02, authorise_retry_seconds=0.06)
    result = await ctl.open_session(trigger())
    assert result["authorisation"] == "pending" and ctl.state == "recording"
    await settle(0.4)
    assert ctl.status()["authorisation"] == "granted"
    assert uploader.calls >= 3


async def test_a_no_within_the_wait_keeps_recording(monkeypatch, spool, device_key):
    """A server "no" never stops the consultation or deletes it."""
    ctl = make_controller(monkeypatch, spool, device_key, ScriptedUploader([MISMATCH]),
                          authorise_hold_retry_seconds=10)
    result = await ctl.open_session(trigger())
    assert result["authorisation"] == "held" and ctl.state == "recording"
    assert [d for d in spool.root.iterdir() if d.is_dir()] != []
    await ctl.close()
    assert not ctl._background                     # the asking stopped with it


async def test_microphone_unavailable_leaves_nothing_behind(monkeypatch, spool, device_key):
    """
    A microphone that will not open leaves no session in the spool.

    The session directory is created before the device is opened. Left behind,
    it was recovered at the next start as an interrupted consultation: a false
    "agent stopped unexpectedly" alert, and an empty recording uploaded to show
    up as a discrepancy. A page retrying AGENT_NOT_READY - which it should -
    made one of those per retry.
    """
    import core.session_controller as sc
    from core.recorder import AudioCaptureError

    class DeadMicrophone:
        def __init__(self, **kwargs):
            pass

        def start(self):
            raise AudioCaptureError("device in use by another application")

    ctl = make_controller(monkeypatch, spool, device_key, ScriptedUploader([CONFIRMED]))
    monkeypatch.setattr(sc, "AudioRecorder", DeadMicrophone)

    for _ in range(3):                                  # the page retries
        with pytest.raises(SessionError) as err:
            await ctl.open_session(trigger())
        assert err.value.code == "AGENT_NOT_READY"

    assert ctl.state == "idle"
    assert [d for d in spool.root.iterdir() if d.is_dir()] == []
    assert spool.recover(device_key) == []              # nothing to resurrect


async def test_a_no_after_the_wait_keeps_recording_and_asks_again(monkeypatch, spool,
                                                                  device_key):
    uploader = ScriptedUploader([MISMATCH, GRANTED], delay=0.02)
    ctl = make_controller(monkeypatch, spool, device_key, uploader,
                          authorise_wait_seconds=0.01, authorise_hold_retry_seconds=0.05)
    result = await ctl.open_session(trigger())
    assert result["authorisation"] == "pending"
    await settle(0.2)
    assert ctl.state == "recording"
    assert [name for name, _ in ctl.events].count("session_held") == 1
    assert ctl.status()["authorisation"] == "granted"         # the fix released it
    assert not any(name == "recording_stopped" for name, _ in ctl.events)
    await ctl.close()


async def test_confirmation_arrives_while_recording(monkeypatch, spool, device_key):
    """AT-59: shown as confirming, then confirmed."""
    uploader = ScriptedUploader([GRANTED])
    uploader.confirmations = [None, "confirmed"]
    ctl = make_controller(monkeypatch, spool, device_key, uploader)
    result = await ctl.open_session(trigger())
    assert result["confirmation"] == "confirming"
    await settle()
    assert ctl.status()["confirmation"] == "confirmed"
    assert any(name == "session_confirmed" for name, _ in ctl.events)


async def test_no_confirmation_is_shown_as_unconfirmed(monkeypatch, spool, device_key):
    """AT-58: never cut; shown as unconfirmed at the deadline."""
    ctl = make_controller(monkeypatch, spool, device_key, ScriptedUploader([GRANTED]),
                          confirm_deadline_seconds=0.03)
    await ctl.open_session(trigger())
    await settle()
    assert ctl.status()["confirmation"] == "unconfirmed"
    assert ctl.state == "recording"


async def test_a_late_api2_turns_unconfirmed_into_confirmed(monkeypatch, spool, device_key):
    """API 2 after the two minutes: the screen must not stay on 'not confirmed'."""
    uploader = ScriptedUploader([GRANTED])
    uploader.confirmations = ["unconfirmed", None, "confirmed"]
    ctl = make_controller(monkeypatch, spool, device_key, uploader,
                          confirm_late_poll_seconds=0.01)
    await ctl.open_session(trigger())
    await settle()
    assert ctl.status()["confirmation"] == "confirmed"
    names = [name for name, _ in ctl.events]
    assert names.index("session_unconfirmed") < names.index("session_confirmed")
    assert ctl.state == "recording"
    await ctl.close()


# ============================================================
# The patient did not consent (SRS §7.8a)
# ============================================================

async def test_refusal_stops_and_deletes_everything_local(monkeypatch, spool, device_key):
    """AT-03, recorder side."""
    ctl = make_controller(monkeypatch, spool, device_key, ScriptedUploader([CONFIRMED]))
    result = await ctl.open_session(trigger())
    session = ctl._active.spool
    seal(session)
    seal(session)

    refused = await ctl.stop_session(reason=REFUSAL_REASON)
    assert (refused["status"], refused["pieces_deleted"]) == ("refused", 2)
    assert ctl.state == "idle"
    assert list(session.directory.glob("*.aimspl")) == []
    assert reload(spool, device_key, session).refused
    assert any(name == "session_refused" for name, _ in ctl.events)
    assert result["session_id"] == refused["session_id"]


async def test_refusal_with_nothing_recording(monkeypatch, spool, device_key):
    ctl = make_controller(monkeypatch, spool, device_key, ScriptedUploader([GRANTED]))
    assert (await ctl.stop_session(reason=REFUSAL_REASON))["status"] == "not_recording"


# ============================================================
# A recording moved to the patient its prescription was built for
# ============================================================

async def test_a_reassign_is_delivered_with_a_grant_for_the_right_patient(
        spool, device_key, server_key):
    session = open_spooled(spool, device_key)
    right = {**TRIGGER, "patient_id": "RIGHT", "start_time": "2026-09-13T10:15:12+06:00"}
    entry = session.reassign(to_fields=right, patient_opened_at=datetime.now(timezone.utc))
    assert session.pending_notifications() == [entry]

    uploader = uploader_for(spool, device_key, server_key, [
        (200, {"grant": server_grant(server_key, patient="RIGHT", jti="jti-right")})])
    posted = {}

    async def post(endpoint, payload, attempts=None):
        posted[endpoint] = payload
        return {"status": "recorded", "reassigned": True}

    uploader._post = post
    assert await uploader._notify_chain_entry(session, entry)
    assert uploader.calls[0][1:] == ("/grant/mint", right)
    assert posted["/session/reassign"]["grant_jti"] == "jti-right"
    assert session.pending_notifications() == []
    assert reload(spool, device_key, session).meta["reassigned_to"] == right


async def test_a_reassign_the_server_says_no_to_is_held_not_dropped(
        spool, device_key, server_key):
    session = open_spooled(spool, device_key)
    right = {**TRIGGER, "patient_id": "RIGHT"}
    entry = session.reassign(to_fields=right, patient_opened_at=datetime.now(timezone.utc))
    uploader = uploader_for(spool, device_key, server_key, [(401, {"code": "CLINIC_MISMATCH"})])

    async def post(endpoint, payload, attempts=None):
        raise AssertionError("nothing is sent without the right patient's grant")

    uploader._post = post
    assert not await uploader._notify_chain_entry(session, entry)
    assert session.pending_notifications() == [entry]        # still waiting, in order
    assert session.hold_code == "CLINIC_MISMATCH"
