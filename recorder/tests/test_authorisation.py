"""
Phase 3 of the v3 upgrade: the recorder asks the AIMS LAB server for each
consultation's grant while it records (SRS 3.2 §5, §5.6, §7.8a).

* the buffer remembers where each consultation stands, across a restart;
* the uploader checks every grant, and sends nothing before one;
* the controller records first, and stops and deletes on a hard refusal;
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


def test_grant_and_refusal_survive_a_restart(spool, device_key):
    granted = open_spooled(spool, device_key)
    granted.record_authorised("jti-1", "confirmed")
    again = reload(spool, device_key, granted)
    assert (again.authorisation, again.grant_jti, again.confirmation) == (
        "granted", "jti-1", "confirmed")

    refused = open_spooled(spool, device_key)
    refused.record_authorisation_refused("CLINIC_MISMATCH")
    again = reload(spool, device_key, refused)
    assert (again.authorisation, again.refusal_code) == ("refused", "CLINIC_MISMATCH")


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
async def test_a_grant_for_anything_else_is_refused(spool, device_key, server_key,
                                                    claims, why):
    session = open_spooled(spool, device_key)
    uploader = uploader_for(spool, device_key, server_key,
                            [(200, {"grant": server_grant(server_key, **claims)})])
    outcome = await uploader.authorise(session)
    assert (outcome.status, outcome.code) == ("refused", "AUTHORISATION_FAILED"), why
    assert session.authorisation == "refused"


async def test_a_grant_is_used_once(spool, device_key, server_key):
    """AT-07."""
    token = server_grant(server_key, jti="same")
    uploader = uploader_for(spool, device_key, server_key, [(200, {"grant": token})])
    assert (await uploader.authorise(open_spooled(spool, device_key))).status == "granted"
    assert (await uploader.authorise(open_spooled(spool, device_key))).status == "refused"


async def test_a_grant_signed_by_another_key_is_refused(spool, device_key, server_key):
    """AT-08's cousin: only the pinned key's grants count."""
    impostor = Ed25519PrivateKey.generate()
    uploader = uploader_for(spool, device_key, server_key,
                            [(200, {"grant": server_grant(impostor)})])
    assert (await uploader.authorise(open_spooled(spool, device_key))).status == "refused"


@pytest.mark.parametrize("reply,code", [
    ((401, {"code": "CLINIC_MISMATCH", "message": "other clinic"}), "CLINIC_MISMATCH"),
    ((401, {"detail": "unknown device token"}), "DEVICE_NOT_ENROLLED"),
    ((403, {"detail": "device has been revoked"}), "DEVICE_NOT_ENROLLED"),
    ((400, {"code": "MISSING_FIELD"}), "MISSING_FIELD"),
])
async def test_hard_refusals_carry_their_code(spool, device_key, server_key, reply, code):
    session = open_spooled(spool, device_key)
    uploader = uploader_for(spool, device_key, server_key, [reply])
    outcome = await uploader.authorise(session)
    assert (outcome.status, outcome.code) == ("refused", code)
    assert any(d.get("alert_type") == "authorisation_refused" for _, d in uploader.events)


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
    assert (await uploader.authorise(open_spooled(spool, device_key))).status == "refused"


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


async def test_a_refused_recording_is_deleted_unsent(spool, device_key, server_key):
    """AT-04, recorder side / SRS-GRT-08."""
    session = open_spooled(spool, device_key)
    seal(session)
    uploader = uploader_for(spool, device_key, server_key,
                            [(401, {"code": "CLINIC_MISMATCH"})])
    _no_upload(uploader)
    await uploader.track(session)
    await uploader._drain_once()
    assert not session.directory.exists()
    assert uploader.status()["tracked_sessions"] == 0


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
MISMATCH = AuthOutcome("refused", "CLINIC_MISMATCH", "other clinic")


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
    ctl = make_controller(monkeypatch, spool, device_key, uploader,
                          authorise_wait_seconds=0.02)
    result = await ctl.open_session(trigger())
    assert result["authorisation"] == "pending" and ctl.state == "recording"
    await settle()
    assert ctl.status()["authorisation"] == "granted"
    assert uploader.calls >= 3


async def test_hard_refusal_within_the_wait(monkeypatch, spool, device_key):
    """SRS-GRT-08: stopped, deleted, and the page is told why."""
    ctl = make_controller(monkeypatch, spool, device_key, ScriptedUploader([MISMATCH]))
    with pytest.raises(SessionError) as err:
        await ctl.open_session(trigger())
    assert err.value.code == "CLINIC_MISMATCH"
    assert ctl.state == "idle"
    assert [d for d in spool.root.iterdir() if d.is_dir()] == []


async def test_hard_refusal_after_the_wait(monkeypatch, spool, device_key):
    ctl = make_controller(monkeypatch, spool, device_key,
                          ScriptedUploader([MISMATCH], delay=0.05),
                          authorise_wait_seconds=0.01)
    result = await ctl.open_session(trigger())
    assert result["authorisation"] == "pending"
    await settle()
    assert ctl.state == "idle"
    assert [d for d in spool.root.iterdir() if d.is_dir()] == []
    stopped = [d for name, d in ctl.events if name == "recording_stopped"]
    assert stopped[-1]["reason"] == "authorisation_refused"


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
