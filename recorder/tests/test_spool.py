"""
The spool: encryption at rest, crash recovery, and the rules around deletion.

These cover requirement R4 - audio is removed from the doctor's PC automatically,
but only once the archive copy is proven to exist.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from core import crypto
from core.spool import (COMMITTED, PENDING, PURGED, RECEIPTED, SessionSpool,
                        Spool, new_ulid, wav_bytes)
from tests.conftest import pcm


def _open(spool: Spool, device_key, audio_params, **overrides) -> SessionSpool:
    kwargs = dict(
        device_id="dev-1", doctor_id="DR001", hospital_id="HOSP001",
        patient_ref="P12345", consent_method="verbal_at_reception",
        audio=audio_params,
    )
    kwargs.update(overrides)
    return spool.open_session(device_key=device_key, **kwargs)


def _seal(session: SessionSpool, seconds: float = 1.0, **overrides):
    start = overrides.pop("start", datetime(2026, 7, 26, 9, 30, tzinfo=timezone.utc))
    return session.seal_segment(
        pcm(seconds, value=1000),
        captured_start_at=start,
        captured_end_at=start + timedelta(seconds=seconds),
        rms_mean=overrides.pop("rms_mean", 1000.0),
        is_final=overrides.pop("is_final", False),
    )


# ============================================================
# WAV framing
# ============================================================

def test_wav_header_is_canonical(audio_params):
    """
    The server re-hashes exactly these bytes, so the header must be byte-stable.
    """
    data = wav_bytes(pcm(1.0), **audio_params)
    assert data[:4] == b"RIFF"
    assert data[8:12] == b"WAVE"
    assert len(data) == 44 + 44100 * 2
    assert int.from_bytes(data[24:28], "little") == 44100
    assert int.from_bytes(data[34:36], "little") == 16          # bits per sample
    assert wav_bytes(pcm(1.0), **audio_params) == data          # deterministic


# ============================================================
# Sealing
# ============================================================

def test_sealed_segment_round_trips(spool, device_key, audio_params):
    session = _open(spool, device_key, audio_params)
    segment = _seal(session, seconds=2.0)

    assert segment.seq_no == 1
    assert segment.state == PENDING
    assert pytest.approx(segment.duration_seconds, abs=0.01) == 2.0

    audio = session.read_segment(1)
    assert crypto.sha256_bytes(audio) == segment.sha256
    assert audio[:4] == b"RIFF"


def test_segment_is_encrypted_on_disk(spool, device_key, audio_params):
    """A doctor with file access must not be able to read buffered audio."""
    session = _open(spool, device_key, audio_params)
    _seal(session, seconds=1.0)

    raw = (session.directory / "seg-00001.aimspl").read_bytes()
    assert raw.startswith(b"AIMSPL")
    assert b"RIFF" not in raw          # no plaintext WAV header
    assert b"data" not in raw


def test_tampered_spool_file_fails_authentication(spool, device_key, audio_params):
    session = _open(spool, device_key, audio_params)
    _seal(session, seconds=1.0)

    path = session.directory / "seg-00001.aimspl"
    blob = bytearray(path.read_bytes())
    blob[-1] ^= 0xFF
    path.write_bytes(bytes(blob))

    with pytest.raises(ValueError):
        session.read_segment(1)


def test_chain_grows_with_each_segment(spool, device_key, audio_params):
    session = _open(spool, device_key, audio_params)
    for _ in range(3):
        _seal(session)

    assert [e.entry_type for e in session.chain] == ["open", "segment", "segment", "segment"]
    assert session.verify_chain()


# ============================================================
# Crash recovery
# ============================================================

def test_session_recovers_from_journal(spool, device_key, audio_params):
    """A power cut must cost at most the segment still being captured."""
    session = _open(spool, device_key, audio_params)
    _seal(session, seconds=1.0)
    _seal(session, seconds=1.0)
    original_head = session.head_hash

    # Simulate a restart: nothing in memory, only what reached disk.
    recovered = SessionSpool.load(
        session.directory, device_key=device_key, spool_key=spool._key)

    assert recovered is not None
    assert recovered.session_id == session.session_id
    assert len(recovered.segments) == 2
    assert recovered.head_hash == original_head
    assert recovered.meta["patient_ref"] == "P12345"
    assert recovered.verify_chain()
    assert crypto.sha256_bytes(recovered.read_segment(1)) == session.segments[1].sha256


def test_truncated_journal_line_is_survivable(spool, device_key, audio_params):
    """The last line of a journal is exactly what a power cut truncates."""
    session = _open(spool, device_key, audio_params)
    _seal(session, seconds=1.0)

    journal = session.directory / "journal.jsonl"
    with open(journal, "a", encoding="utf-8") as handle:
        handle.write('{"rec": "segment", "seq_no": 2, "fi')

    recovered = SessionSpool.load(
        session.directory, device_key=device_key, spool_key=spool._key)
    assert recovered is not None
    assert len(recovered.segments) == 1


def test_recover_closes_interrupted_sessions(spool, device_key, audio_params):
    session = _open(spool, device_key, audio_params)
    _seal(session, seconds=1.0)

    found = spool.recover(device_key)
    assert len(found) == 1
    assert found[0].session_id == session.session_id
    assert found[0].closed_at is None       # controller closes it and records the gap


# ============================================================
# Deletion rules - R4
# ============================================================

def test_pending_segment_cannot_be_purged(spool, device_key, audio_params):
    session = _open(spool, device_key, audio_params)
    _seal(session)
    assert not session.purge_segment(1)
    assert (session.directory / "seg-00001.aimspl").exists()


def test_committed_segment_cannot_be_purged(spool, device_key, audio_params):
    """
    Reaching object storage is not grounds for deleting the only other copy.
    Only a verified purge receipt is.
    """
    session = _open(spool, device_key, audio_params)
    _seal(session)
    session.set_state(1, COMMITTED, object_key="audio/x/clip_1.wav")

    assert not session.purge_segment(1)
    assert (session.directory / "seg-00001.aimspl").exists()


def test_receipted_segment_is_purged(spool, device_key, audio_params):
    session = _open(spool, device_key, audio_params)
    segment = _seal(session)
    session.set_state(1, COMMITTED)
    session.record_receipt(1, {"scope": "segment", "seq_no": 1,
                               "sha256": segment.sha256.hex()}, b"\x00" * 64)

    assert session.segments[1].state == RECEIPTED
    assert session.purge_segment(1)
    assert not (session.directory / "seg-00001.aimspl").exists()
    assert session.segments[1].state == PURGED


def test_session_not_complete_until_close_is_reported(spool, device_key, audio_params):
    """
    Guards a bug where the directory was deleted before the backend had accepted
    the close, losing the journal for a session nobody had accounted for.
    """
    session = _open(spool, device_key, audio_params)
    segment = _seal(session)
    session.set_state(1, COMMITTED)
    session.record_receipt(1, {"sha256": segment.sha256.hex()}, b"\x00" * 64)
    session.purge_segment(1)

    assert not session.is_complete            # never acknowledged, never closed

    session.mark_acknowledged()
    assert not session.is_complete            # still open

    session.close(duration_seconds=1.0, paused_seconds=0.0)
    assert not session.is_complete            # close not yet delivered

    session.mark_close_reported()
    assert session.is_complete


@pytest.mark.asyncio
@pytest.mark.parametrize("response,expect_reported", [
    ({"status": "closed", "chain_ok": True}, True),
    # A human reviews it, but the server has accounted for the session.
    ({"status": "quarantined", "reason": "chain broken"}, True),
    # Segments still in flight. Must NOT be marked, or the session is stranded.
    ({"status": "incomplete", "server_segments": 11, "agent_segments": 12}, False),
])
async def test_close_only_marked_when_the_server_really_closed_it(
    spool, device_key, audio_params, response, expect_reported
):
    """
    Guards a bug that stranded a real 33-minute consultation.

    Stopping posts /session/close immediately, which can overtake the final
    segment's upload. The backend then answers 'incomplete' - an HTTP 200. The
    agent treated any 200 as delivered, journaled close_reported and never
    retried, so the session stayed open forever: uploaded, never archived, and
    the local audio never released.
    """
    from core.uploader import UploadManager

    session = _open(spool, device_key, audio_params)
    _seal(session)
    session.mark_acknowledged()
    session.close(duration_seconds=1.0, paused_seconds=0.0)

    # cfg is only consulted inside _post, which is stubbed below.
    manager = UploadManager(SimpleNamespace(), device_key=device_key,
                            spool=spool, receipt_public_key=None)

    async def fake_post(endpoint, payload, **kwargs):
        assert endpoint == "/session/close"
        return response

    manager._post = fake_post

    delivered = await manager.close_remote(
        session, duration_seconds=1.0, paused_seconds=0.0)

    assert session.close_reported is expect_reported
    assert delivered is expect_reported

    # Whatever the answer, the audio stays until a receipt authorises deletion.
    assert not session.is_complete


def test_close_reported_survives_restart(spool, device_key, audio_params):
    session = _open(spool, device_key, audio_params)
    _seal(session)
    session.mark_acknowledged()
    session.close(duration_seconds=1.0, paused_seconds=0.0)
    session.mark_close_reported()

    recovered = SessionSpool.load(
        session.directory, device_key=device_key, spool_key=spool._key)
    assert recovered.close_reported
    assert recovered.server_acknowledged
    assert pytest.approx(recovered.duration_seconds) == 1.0


# ============================================================
# Capacity and identifiers
# ============================================================

def test_spool_reports_pressure(spool, device_key, audio_params):
    assert spool.pressure() == "ok"
    session = _open(spool, device_key, audio_params)
    _seal(session, seconds=2.0)
    assert spool.total_bytes() > 0


def test_manifest_is_self_describing(spool, device_key, audio_params):
    """The archive must be verifiable even if the database is lost."""
    session = _open(spool, device_key, audio_params)
    _seal(session)
    session.close(duration_seconds=1.0, paused_seconds=0.0)

    manifest = json.loads(json.dumps(session.manifest()))
    assert manifest["audio"]["sample_rate"] == 44100
    assert manifest["audio"]["codec"] == "pcm_s16le"
    assert manifest["hospital_id"] == "HOSP001"
    assert manifest["device_pubkey"] == device_key.public_bytes_raw().hex()
    assert len(manifest["segments"]) == 1
    assert manifest["chain_head"] == session.head_hash.hex()


def test_ulid_is_sortable_and_unique():
    ulids = [new_ulid() for _ in range(200)]
    assert len(set(ulids)) == 200
    assert all(len(value) == 26 for value in ulids)
    # Crockford base32 excludes I, L, O and U to avoid transcription errors.
    assert not set("ILOU") & set("".join(ulids))


def test_session_id_does_not_leak_patient_id(spool, device_key, audio_params):
    """Object keys are built from the session ID, which must carry no PHI."""
    session = _open(spool, device_key, audio_params, patient_ref="P12345")
    assert "P12345" not in session.session_id


@pytest.mark.asyncio
async def test_a_failed_segment_holds_back_the_rest(spool, device_key, audio_params):
    """
    Guards a bug that quarantined a real consultation.

    The chain is sequential - every entry's prev_hash is the previous entry's
    hash - so a segment arriving before its predecessor cannot verify, and the
    backend quarantines the session. The drain loop used to discard the result
    of each upload and continue, so one failed clip reordered everything after
    it. Six clips went to storage as 2,3,4,5,6,1 and the session was lost.

    Nothing is dropped: the remaining segments stay sealed on disk and the next
    tick retries from the one that failed.
    """
    from core.uploader import UploadManager, UploadOutcome

    session = _open(spool, device_key, audio_params)
    for _ in range(4):
        _seal(session)
    session.mark_acknowledged()

    cfg = SimpleNamespace(spool=SimpleNamespace(purge_grace_hours=24))
    manager = UploadManager(cfg, device_key=device_key, spool=spool,
                            receipt_public_key=None)
    await manager.track(session)

    attempted = []

    async def flaky(sess, segment):
        attempted.append(segment.seq_no)
        ok = segment.seq_no != 2          # clip 2 cannot be delivered
        if ok:
            sess.set_state(segment.seq_no, COMMITTED)
        return UploadOutcome(session_id=sess.session_id, seq_no=segment.seq_no, ok=ok)

    manager._send_segment = flaky
    manager._collect_receipts = lambda s: asyncio.sleep(0)
    # _drain_once bails out immediately unless the manager is running.
    manager._running = True

    await manager._drain_once()

    # Stops at the failure rather than delivering 3 and 4 ahead of 2.
    assert attempted == [1, 2]
    assert [s.seq_no for s in session.pending_segments()] == [2, 3, 4]


@pytest.mark.asyncio
async def test_one_failing_session_does_not_block_the_others(spool, device_key, audio_params):
    """
    Guards a stall that hid three consultations.

    A 500 from the backend is a 5xx, so it was retried with the full backoff -
    2+8+30+120+600, nearly 13 minutes - inside _drain_once, which works through
    sessions sequentially. One failing clip therefore blocked every session
    behind it, including /session/open for newly started recordings: they never
    appeared in the database or in storage, and nothing in the agent said why.

    The drain loop runs every 10 seconds and is itself the retry, so a failure
    must cost this session its turn and nothing more.
    """
    from core.uploader import UploadManager, UploadOutcome

    stuck = _open(spool, device_key, audio_params, patient_ref="STUCK")
    _seal(stuck)
    stuck.mark_acknowledged()

    later = _open(spool, device_key, audio_params, patient_ref="LATER")
    _seal(later)

    cfg = SimpleNamespace(spool=SimpleNamespace(purge_grace_hours=24))
    manager = UploadManager(cfg, device_key=device_key, spool=spool,
                            receipt_public_key=None)
    await manager.track(stuck)
    await manager.track(later)
    manager._running = True

    opened = []

    async def failing_send(sess, segment):
        return UploadOutcome(session_id=sess.session_id, seq_no=segment.seq_no,
                             ok=False, error="500")

    async def open_remote(sess):
        opened.append(sess.meta.get("patient_ref"))
        return True

    manager._send_segment = failing_send
    manager._open_remote = open_remote
    manager._collect_receipts = lambda s: asyncio.sleep(0)

    await manager._drain_once()

    # The second session still got its chance to register.
    assert "LATER" in opened


# ============================================================
# Pause and resume are chain links, not status updates
#
# They were delivered once, best-effort, on the theory that the chain copy would
# arrive with the manifest at close. The backend verifies each entry as it
# lands, so a dropped pause left every later segment carrying a prev_hash the
# server had never seen, and the whole consultation was quarantined. It happened
# to a real one.
# ============================================================

def test_pause_and_resume_are_pending_until_delivered(spool, device_key, audio_params):
    session = _open(spool, device_key, audio_params)
    pause = session.append_chain_entry("pause", crypto.pause_payload(
        reason="sensitive_personal_matter", reason_detail="", authorised_by="DR001",
        supervisor_required=False, at=datetime(2026, 7, 29, 1, 31, tzinfo=timezone.utc)))
    resume = session.append_chain_entry("resume", {"type": "resume"})

    assert [e.entry_no for e in session.pending_notifications()] == [
        pause.entry_no, resume.entry_no]

    session.mark_entry_reported(pause.entry_no)
    assert [e.entry_no for e in session.pending_notifications()] == [resume.entry_no]

    session.mark_entry_reported(resume.entry_no)
    assert session.pending_notifications() == []


def test_undelivered_pause_survives_a_restart(spool, device_key, audio_params):
    """The agent dying must not be what loses the entry - that is when it matters."""
    session = _open(spool, device_key, audio_params)
    pause = session.append_chain_entry("pause", crypto.pause_payload(
        reason="clinical_examination", reason_detail="", authorised_by="DR001",
        supervisor_required=False, at=datetime(2026, 7, 29, 1, 31, tzinfo=timezone.utc)))
    resume = session.append_chain_entry("resume", {"type": "resume"})
    session.mark_entry_reported(pause.entry_no)

    recovered = [s for s in spool.recover(device_key) if s.session_id == session.session_id]
    assert len(recovered) == 1
    assert [e.entry_no for e in recovered[0].pending_notifications()] == [resume.entry_no]


def test_a_pause_is_ordered_before_the_segments_that_follow_it(spool, device_key, audio_params):
    """
    The drain loop sorts its work by entry number. A pause recorded between two
    segments has to be delivered between them, or the second segment's prev_hash
    refers to something the backend has not stored.
    """
    session = _open(spool, device_key, audio_params)
    first = _seal(session, 1.0)
    pause = session.append_chain_entry("pause", crypto.pause_payload(
        reason="patient_request", reason_detail="", authorised_by="DR001",
        supervisor_required=False, at=datetime(2026, 7, 29, 1, 31, tzinfo=timezone.utc)))
    second = _seal(session, 1.0)

    work = [(s.entry_no, "segment") for s in session.pending_segments()]
    work += [(e.entry_no, "notify") for e in session.pending_notifications()]

    assert [kind for _, kind in sorted(work)] == ["segment", "notify", "segment"]
    assert first.entry_no < pause.entry_no < second.entry_no


def test_close_can_be_stamped_at_a_given_instant(spool, device_key, audio_params):
    """
    CMED treats consultations as contiguous: the next patient's start time is
    this patient's end time. Reading the clock twice put the microphone teardown
    and the final segment seal in between, so the two records disagreed.
    """
    session = _open(spool, device_key, audio_params)
    _seal(session, 1.0)
    boundary = datetime(2026, 7, 29, 10, 45, 30, tzinfo=timezone.utc)

    session.close(duration_seconds=1.0, paused_seconds=0.0,
                  reason="superseded_by_new_patient", at=boundary)

    assert session.closed_at == boundary
    close_entry = session.chain[-1]
    assert close_entry.entry_type == "close"
    assert close_entry.payload["closed_at"] == crypto.iso_utc(boundary)
    assert close_entry.payload["reason"] == "superseded_by_new_patient"
