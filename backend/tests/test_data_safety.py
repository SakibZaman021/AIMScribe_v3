"""
The data-safety review, as tests. Each one failed before its fix.

  - a signed chain entry vouches only for the piece it names
  - nothing joins a recording after its close, and a quarantined one gets no receipt
  - the archive receipts, marks and deletes only the pieces it joined
  - a refusal is signed and bounded; one after archiving removes the files too
  - a PC that signs its requests must sign them all; nonces are used once
  - a close never undoes a quarantine; times without an offset are UTC
  - the genesis belongs to this PC and opens one recording only

The entries are built and signed by the recorder's own code, so the two
halves are tested against each other. The PostgreSQL tests at the end run
the same rules against the real queries and locks.
"""
from __future__ import annotations

import asyncio
import base64
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio
from fastapi import HTTPException

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND / "src"))
sys.path.insert(0, str(BACKEND / "tests"))
sys.path.insert(0, str(BACKEND.parent / "recorder"))

import api_v2                                   # noqa: E402
import integrity                                # noqa: E402
from core import crypto as agent                # noqa: E402

from test_reassign import device, device_key, started  # noqa: E402,F401
from test_v3_server import (                    # noqa: E402,F401
    DEVICE, NOW, joined, open_session, server)

try:
    from test_db_integration import dbs, pg     # noqa: E402,F401
    HAVE_POSTGRES = True
except pytest.skip.Exception:
    HAVE_POSTGRES = False

asyncio_mark = pytest.mark.asyncio


def postgres(test):
    return pytest.mark.skipif(not HAVE_POSTGRES, reason="pgserver is not installed")(
        asyncio_mark(test))


AUDIO = b"simulated audio, one piece"


# ============================================================
# Helpers: a recording the server holds, and pieces for it
# ============================================================

def segment_entry(server, sid, key, *, seq_no=1, audio=AUDIO, entry_no=None,
                  prev=None):
    head = server.repo.chains[sid][-1]
    return agent.build_entry(
        entry_no=head.entry_no + 1 if entry_no is None else entry_no,
        entry_type="segment",
        payload=agent.segment_payload(
            seq_no=seq_no, audio_sha256=integrity.sha256_bytes(audio),
            byte_length=len(audio), duration_seconds=1.0,
            captured_start_at=NOW(), captured_end_at=NOW(), rms_mean=900.0,
            is_final=False),
        prev_hash=head.entry_hash if prev is None else prev, signer=key)


async def commit(sid, entry, device, *, seq_no=1, audio=AUDIO, monkeypatch):
    monkeypatch.setattr(api_v2, "_read_object", lambda key: audio)
    return await api_v2.commit_segment(api_v2.CommitRequest(
        session_id=sid, seq_no=seq_no, object_key=f"audio/{sid}/{seq_no}.wav",
        sha256=integrity.sha256_bytes(audio).hex(), bytes=len(audio),
        duration_seconds=1.0, captured_start_at=NOW().isoformat(),
        captured_end_at=NOW().isoformat(), is_final=False,
        chain_entry=entry.to_wire()), device=device)


def receipts_for(server, sid):
    return [r for r in server.repo.receipts if r["session_id"] == sid]


# ============================================================
# 1. A signed entry vouches only for the piece it names
# ============================================================

@asyncio_mark
async def test_a_piece_is_committed_with_its_own_entry(server, device_key, device,
                                                       monkeypatch):
    sid = started(server, device_key)
    reply = await commit(sid, segment_entry(server, sid, device_key), device,
                         monkeypatch=monkeypatch)
    assert reply["status"] == "committed"
    assert len(receipts_for(server, sid)) == 1


@asyncio_mark
async def test_an_entry_for_other_audio_cannot_carry_this_upload(server, device_key, device,
                                                                monkeypatch):
    """Before: any valid signed entry let any bytes be committed under it."""
    sid = started(server, device_key)
    entry = segment_entry(server, sid, device_key, audio=b"what was really recorded")
    reply = await commit(sid, entry, device, audio=b"something else entirely",
                         monkeypatch=monkeypatch)
    assert reply["status"] == "quarantined"
    assert server.repo.segments.get(sid, []) == []
    assert receipts_for(server, sid) == []
    assert server.repo.alerts[-1]["alert_type"] == "segment_entry_mismatch"


@asyncio_mark
async def test_an_entry_for_another_seq_no_is_refused(server, device_key, device,
                                                     monkeypatch):
    sid = started(server, device_key)
    entry = segment_entry(server, sid, device_key, seq_no=7)
    reply = await commit(sid, entry, device, seq_no=1, monkeypatch=monkeypatch)
    assert reply["status"] == "quarantined"
    assert server.repo.segments.get(sid, []) == []


@asyncio_mark
async def test_a_pause_entry_cannot_be_committed_as_audio(server, device_key, device,
                                                         monkeypatch):
    sid = started(server, device_key)
    head = server.repo.chains[sid][-1]
    pause = agent.build_entry(
        entry_no=1, entry_type="pause",
        payload=agent.pause_payload(reason="x", reason_detail="", authorised_by="d",
                                    supervisor_required=False, at=NOW()),
        prev_hash=head.entry_hash, signer=device_key)
    reply = await commit(sid, pause, device, monkeypatch=monkeypatch)
    assert reply["status"] == "quarantined"


@asyncio_mark
async def test_an_entry_number_already_taken_quarantines(server, device_key, device,
                                                        monkeypatch):
    sid = started(server, device_key)
    first = segment_entry(server, sid, device_key)
    assert (await commit(sid, first, device, monkeypatch=monkeypatch))["status"] == \
        "committed"
    rival = segment_entry(server, sid, device_key, seq_no=2, entry_no=1,
                          prev=server.repo.chains[sid][0].entry_hash, audio=b"rival")
    reply = await commit(sid, rival, device, seq_no=2, audio=b"rival",
                         monkeypatch=monkeypatch)
    assert reply["status"] == "quarantined"
    assert [e.entry_no for e in server.repo.chains[sid]] == [0, 1]


@asyncio_mark
async def test_a_gap_in_the_chain_waits_rather_than_quarantining(server, device_key, device,
                                                                monkeypatch):
    sid = started(server, device_key)
    ahead = segment_entry(server, sid, device_key, entry_no=3)
    with pytest.raises(HTTPException) as held:
        await commit(sid, ahead, device, monkeypatch=monkeypatch)
    assert held.value.status_code == 409
    assert server.repo.sessions[sid].get("status") != "quarantined"


@asyncio_mark
async def test_a_retried_commit_is_still_the_success_it_was(server, device_key, device,
                                                           monkeypatch):
    sid = started(server, device_key)
    entry = segment_entry(server, sid, device_key)
    await commit(sid, entry, device, monkeypatch=monkeypatch)
    again = await commit(sid, entry, device, monkeypatch=monkeypatch)
    assert (again["status"], again["duplicate"]) == ("committed", True)


# ============================================================
# 2. Nothing joins a recording after its close
# ============================================================

@asyncio_mark
async def test_a_piece_after_the_close_is_refused_and_gets_no_receipt(
        server, device_key, device, monkeypatch):
    """Before: it was receipted (the PC deleted it) and never in the archive."""
    sid = started(server, device_key)
    server.repo.sessions[sid]["closed_at"] = NOW()
    with pytest.raises(HTTPException) as refused:
        await commit(sid, segment_entry(server, sid, device_key), device,
                     monkeypatch=monkeypatch)
    assert refused.value.status_code == 409
    assert receipts_for(server, sid) == []


@asyncio_mark
async def test_a_quarantined_recording_gives_no_receipt(server, device_key, device,
                                                       monkeypatch):
    sid = started(server, device_key)
    server.repo.sessions[sid].update(status="quarantined", quarantine_reason="review")
    with pytest.raises(HTTPException) as refused:
        await commit(sid, segment_entry(server, sid, device_key), device,
                     monkeypatch=monkeypatch)
    assert refused.value.status_code == 409
    assert receipts_for(server, sid) == []


@asyncio_mark
async def test_a_pause_for_an_erased_recording_is_refused(server, device_key, device):
    sid = started(server, device_key)
    server.repo.sessions[sid]["confirmation"] = "refused"
    head = server.repo.chains[sid][-1]
    pause = agent.build_entry(
        entry_no=1, entry_type="pause",
        payload=agent.pause_payload(reason="x", reason_detail="", authorised_by="d",
                                    supervisor_required=False, at=NOW()),
        prev_hash=head.entry_hash, signer=device_key)
    with pytest.raises(HTTPException) as refused:
        await api_v2.pause_session(api_v2.ChainEntryRequest(
            session_id=sid, chain_entry=pause.to_wire()), device=device)
    assert refused.value.status_code == 409


@asyncio_mark
async def test_a_lifecycle_entry_of_the_wrong_kind_is_refused(server, device_key, device):
    sid = started(server, device_key)
    entry = segment_entry(server, sid, device_key)
    with pytest.raises(HTTPException) as refused:
        await api_v2.pause_session(api_v2.ChainEntryRequest(
            session_id=sid, chain_entry=entry.to_wire()), device=device)
    assert refused.value.status_code == 400


# ============================================================
# 3. The archive covers exactly what it joined
# ============================================================

def closed_with_pieces(server, n=3):
    sid = open_session(server, segments=n)
    server.repo.sessions[sid].update(closed_at=NOW(), file_stem="P_STEM")
    return sid


async def report(server, sid, segments):
    return await api_v2.archive_complete(api_v2.ArchiveCompleteRequest(
        session_id=sid, archive_relpath="H/D/2026-09-13/P_STEM/P_STEM.wav",
        sha256="ab" * 32, bytes=1_000, segments=segments))


@asyncio_mark
async def test_an_archive_report_must_name_its_pieces(server):
    sid = closed_with_pieces(server)
    with pytest.raises(HTTPException) as refused:
        await report(server, sid, None)
    assert refused.value.status_code == 409
    assert server.repo.sessions[sid].get("archived_at") is None
    assert receipts_for(server, sid) == []


@asyncio_mark
async def test_pieces_the_worker_did_not_join_are_never_counted_archived(server):
    sid = closed_with_pieces(server, 3)
    partial = joined(server, sid)[:2]
    with pytest.raises(HTTPException) as refused:
        await report(server, sid, partial)
    assert refused.value.status_code == 409
    assert server.repo.alerts[-1]["alert_type"] == "archive_segments_mismatch"
    assert all(s["state"] == "committed" for s in server.repo.segments[sid])
    assert receipts_for(server, sid) == []


@asyncio_mark
async def test_the_archive_receipts_exactly_the_pieces_joined(server):
    sid = closed_with_pieces(server, 3)
    reply = await report(server, sid, joined(server, sid))
    assert reply["receipts_issued"] == 4                 # 3 pieces + the session
    assert {s["state"] for s in server.repo.segments[sid]} == {"archived"}
    again = await report(server, sid, joined(server, sid))
    assert again.get("already") is True                  # not receipted twice


@asyncio_mark
async def test_a_refused_recording_is_never_archived_over(server):
    sid = closed_with_pieces(server)
    server.repo.sessions[sid]["confirmation"] = "refused"
    with pytest.raises(HTTPException) as refused:
        await report(server, sid, joined(server, sid))
    assert refused.value.status_code == 409
    assert server.repo.sessions[sid].get("status") != "archived"


@asyncio_mark
async def test_only_archived_pieces_leave_the_bucket(server):
    sid = closed_with_pieces(server, 2)
    server.repo.segments[sid][1]["state"] = "committed"
    server.repo.segments[sid][0]["state"] = "archived"
    removed = await api_v2._delete_bucket_objects(sid)
    assert removed == 1
    assert server.bucket.removed == [server.repo.segments[sid][0]["object_key"]]


# ============================================================
# 4. Refusals: signed, bounded
# ============================================================

def signed_refusal(key, sid, device_id, *, at=None):
    refusal = {"session_id": sid, "device_id": str(device_id),
               "refused_at": (at or datetime.now(timezone.utc)).strftime(
                   "%Y-%m-%dT%H:%M:%SZ")}
    signature = key.sign(integrity.canonical_json(refusal))
    return refusal, base64.urlsafe_b64encode(signature).decode().rstrip("=")


async def refuse(sid, device, *, refusal=None, signature=None):
    return await api_v2.refuse_session(api_v2.RefuseRequest(
        session_id=sid, refusal=refusal, signature=signature), device=device)


@asyncio_mark
async def test_a_signed_refusal_erases_a_recent_recording(server, device_key, device):
    sid = started(server, device_key, opened_ago=timedelta(days=2))
    refusal, sig = signed_refusal(device_key, sid, device["device_id"])
    reply = await refuse(sid, device, refusal=refusal, signature=sig)
    assert reply["status"] == "refused"
    assert server.repo.sessions[sid]["confirmation"] == "refused"
    assert server.repo.refusals[sid]["signed"] is True


@asyncio_mark
async def test_an_unsigned_refusal_of_an_old_recording_is_refused(server, device_key, device):
    """Before: the bearer token alone erased any recording, of any age."""
    sid = started(server, device_key, opened_ago=timedelta(hours=3))
    with pytest.raises(HTTPException) as refused:
        await refuse(sid, device)
    assert refused.value.status_code == 403
    assert server.repo.sessions[sid]["confirmation"] != "refused"


@asyncio_mark
async def test_an_unsigned_refusal_of_a_new_recording_still_works_but_is_flagged(
        server, device_key, device):
    """Recorder 3.4 cannot sign: its patients can still refuse, and it is visible."""
    sid = started(server, device_key, opened_ago=timedelta(minutes=10))
    await refuse(sid, device)
    assert server.repo.sessions[sid]["confirmation"] == "refused"
    assert any(a["alert_type"] == "unsigned_refusal" for a in server.repo.alerts)


@asyncio_mark
async def test_unsigned_refusals_can_be_switched_off(server, device_key, device, monkeypatch):
    monkeypatch.setenv("AIMS_REQUIRE_SIGNED_REFUSAL", "on")
    sid = started(server, device_key, opened_ago=timedelta(minutes=10))
    with pytest.raises(HTTPException) as refused:
        await refuse(sid, device)
    assert refused.value.status_code == 401


@asyncio_mark
async def test_a_signature_by_another_key_is_refused(server, device_key, device, tmp_path):
    other = agent.DeviceKey.load_or_create(tmp_path / "other.key", allow_plaintext=True)
    sid = started(server, device_key, opened_ago=timedelta(minutes=10))
    refusal, sig = signed_refusal(other, sid, device["device_id"])
    with pytest.raises(HTTPException) as refused:
        await refuse(sid, device, refusal=refusal, signature=sig)
    assert refused.value.status_code == 401
    assert server.repo.sessions[sid]["confirmation"] != "refused"


@asyncio_mark
async def test_a_refusal_signed_for_another_session_is_refused(server, device_key, device):
    sid = started(server, device_key, opened_ago=timedelta(minutes=10))
    other = started(server, device_key)
    refusal, sig = signed_refusal(device_key, other, device["device_id"])
    with pytest.raises(HTTPException) as refused:
        await refuse(sid, device, refusal=refusal, signature=sig)
    assert refused.value.status_code == 400


@asyncio_mark
async def test_a_signed_refusal_after_a_week_needs_an_administrator(server, device_key, device):
    sid = started(server, device_key, opened_ago=timedelta(days=8))
    refusal, sig = signed_refusal(device_key, sid, device["device_id"])
    with pytest.raises(HTTPException) as refused:
        await refuse(sid, device, refusal=refusal, signature=sig)
    assert refused.value.status_code == 403
    await api_v2.admin_erase_session(sid, actor="tests")
    assert server.repo.sessions[sid]["confirmation"] == "refused"


@asyncio_mark
async def test_a_burst_of_refusals_is_alerted(server, device_key, device):
    for _ in range(4):
        sid = started(server, device_key, opened_ago=timedelta(minutes=5))
        refusal, sig = signed_refusal(device_key, sid, device["device_id"])
        await refuse(sid, device, refusal=refusal, signature=sig)
    assert any(a["alert_type"] == "refusal_burst" for a in server.repo.alerts)


# ============================================================
# 5. Signed requests from the PC
# ============================================================

def signed_request(key, *, body=b"{}", path="/api/v2/heartbeat", query="",
                   ts=None, nonce=None, tamper=False):
    ts = str(int(time.time()) if ts is None else ts)
    nonce = nonce or base64.urlsafe_b64encode(os.urandom(16)).decode().rstrip("=")
    full = path + (f"?{query}" if query else "")
    message = integrity.device_request_message(method="POST", path=full, timestamp=ts,
                                               nonce=nonce, body=body)
    signature = base64.urlsafe_b64encode(key.sign(message)).decode().rstrip("=")

    async def read_body():
        return body + (b" " if tamper else b"")
    return SimpleNamespace(
        method="POST", url=SimpleNamespace(path=path, query=query), body=read_body,
        headers={"x-device-timestamp": ts, "x-device-nonce": nonce,
                 "x-device-signature": signature})


def unsigned_request():
    async def read_body():
        return b"{}"
    return SimpleNamespace(method="POST", url=SimpleNamespace(path="/x", query=""),
                           body=read_body, headers={})


@asyncio_mark
async def test_a_signed_request_is_accepted_and_the_pc_must_keep_signing(
        server, device_key, device):
    await api_v2._check_device_signature(signed_request(device_key), device)
    assert str(device["device_id"]) in server.repo.signing
    with pytest.raises(HTTPException) as refused:
        await api_v2._check_device_signature(unsigned_request(),
                                             {**device, "signs_requests": True})
    assert refused.value.status_code == 401


@asyncio_mark
async def test_an_unsigned_request_is_allowed_until_the_pc_signs(server, device):
    await api_v2._check_device_signature(unsigned_request(), device)


@asyncio_mark
async def test_signatures_can_be_required_for_every_pc(server, device, monkeypatch):
    monkeypatch.setenv("AIMS_REQUIRE_DEVICE_SIGNATURES", "on")
    with pytest.raises(HTTPException):
        await api_v2._check_device_signature(unsigned_request(), device)


@pytest.mark.parametrize("change", ["replayed", "stale", "future", "tampered",
                                    "other_key", "bad_nonce"])
@asyncio_mark
async def test_a_bad_signature_is_refused(server, device_key, device, change, tmp_path):
    if change == "replayed":
        first = signed_request(device_key, nonce="n" * 20)
        await api_v2._check_device_signature(first, device)
        request = signed_request(device_key, nonce="n" * 20)
    elif change == "stale":
        request = signed_request(device_key, ts=int(time.time()) - 400)
    elif change == "future":
        request = signed_request(device_key, ts=int(time.time()) + 400)
    elif change == "tampered":
        request = signed_request(device_key, tamper=True)
    elif change == "other_key":
        other = agent.DeviceKey.load_or_create(tmp_path / "o.key", allow_plaintext=True)
        request = signed_request(other)
    else:
        request = signed_request(device_key, nonce="short")
    with pytest.raises(HTTPException) as refused:
        await api_v2._check_device_signature(request, device)
    assert refused.value.status_code == 401


def test_the_signed_bytes_are_the_documented_ones():
    """The recorder builds the same bytes; this pins the format both sides use."""
    message = integrity.device_request_message(
        method="post", path="/api/v2/segment/commit?x=1", timestamp="1792500000",
        nonce="abcdefghijklmnop", body=b'{"a":1}')
    assert message.split(b"\n")[:5] == [
        b"AIMS-DEVICE-SIG-V1", b"POST", b"/api/v2/segment/commit?x=1", b"1792500000",
        b"abcdefghijklmnop"]
    assert message.split(b"\n")[5] == integrity.sha256_bytes(b'{"a":1}').hex().encode()


# ============================================================
# 6. Close: never undoes a quarantine; no naive-time crash
# ============================================================

@asyncio_mark
async def test_a_close_after_a_quarantine_changes_nothing(server, device_key, device):
    sid = started(server, device_key)
    server.repo.sessions[sid].update(status="quarantined", quarantine_reason="hash")
    reply = await api_v2.close_session(api_v2.CloseRequest(session_id=sid), device=device)
    assert reply["status"] == "quarantined"
    assert server.repo.sessions[sid]["status"] == "quarantined"


@asyncio_mark
async def test_a_close_time_without_an_offset_is_read_as_utc(server, device_key, device):
    """Before: TypeError (naive minus aware) - a 500 on every retry."""
    sid = started(server, device_key)
    naive = (datetime.now(timezone.utc) - timedelta(minutes=20)).replace(
        tzinfo=None).isoformat()
    reply = await api_v2.close_session(api_v2.CloseRequest(
        session_id=sid, closed_at=naive, segment_count=2), device=device)
    assert reply["status"] == "incomplete"
    assert api_v2._parse_time("2026-09-13T10:00:00").tzinfo is not None


@asyncio_mark
async def test_a_signed_close_proves_completeness(server, device_key, device, monkeypatch):
    sid = started(server, device_key)
    await commit(sid, segment_entry(server, sid, device_key), device, monkeypatch=monkeypatch)
    head = server.repo.chains[sid][-1]
    close = agent.build_entry(
        entry_no=head.entry_no + 1, entry_type="close",
        payload=agent.close_payload(closed_at=NOW(), segment_count=1, duration_seconds=1.0,
                                    paused_seconds=0.0, reason="doctor_stopped"),
        prev_hash=head.entry_hash, signer=device_key)
    reply = await api_v2.close_session(api_v2.CloseRequest(
        session_id=sid, closed_at=NOW().isoformat(), segment_count=0,
        chain_entry=close.to_wire()), device=device)
    # The PC's unsigned count (0 here) is not what decides: the signed one is.
    assert reply["status"] == "closed"
    again = await api_v2.close_session(api_v2.CloseRequest(
        session_id=sid, chain_entry=close.to_wire()), device=device)
    assert again.get("already") is True


@asyncio_mark
async def test_a_close_entry_that_skips_ahead_waits_for_the_pieces(server, device_key, device):
    sid = started(server, device_key)
    head = server.repo.chains[sid][-1]
    close = agent.build_entry(
        entry_no=head.entry_no + 3, entry_type="close",
        payload=agent.close_payload(closed_at=NOW(), segment_count=2, duration_seconds=2.0,
                                    paused_seconds=0.0),
        prev_hash=b"\x01" * 32, signer=device_key)
    reply = await api_v2.close_session(api_v2.CloseRequest(
        session_id=sid, closed_at=NOW().isoformat(), segment_count=2,
        chain_entry=close.to_wire()), device=device)
    assert reply["status"] == "incomplete"
    assert server.repo.sessions[sid].get("status") != "quarantined"


# ============================================================
# 7. The genesis is this PC's, and opens one recording
# ============================================================

def open_request(sid, genesis, opened_at):
    return api_v2.OpenSessionRequest.model_construct(
        session_id=sid, doctor_id="DR0042", patient_ref="P0012345", hospital_id="HOSP003",
        opened_at=opened_at, genesis=genesis.to_wire(), grant_jti=None,
        continues_session_id=None, consent_method="", audio=None)


@asyncio_mark
async def test_another_pcs_genesis_cannot_open_a_recording(server, device_key, device):
    opened = NOW()
    genesis = agent.build_entry(
        entry_no=0, entry_type="open",
        payload=agent.open_payload(device_id="some-other-pc", doctor_id="DR0042",
                                   hospital_id="HOSP003", patient_ref="P0012345",
                                   opened_at=opened, sample_rate=16000, channels=1,
                                   sample_width=2),
        prev_hash=None, signer=device_key)
    with pytest.raises(HTTPException) as refused:
        await api_v2.open_session(open_request("01JB8XQ4M7YZ2K9V3N5P6R8T00", genesis,
                                               opened.isoformat()), device=device)
    assert refused.value.status_code == 400
    assert server.repo.alerts[-1]["alert_type"] == "genesis_rejected"


@asyncio_mark
async def test_a_genesis_already_used_cannot_open_a_second_recording(server, device_key,
                                                                    device):
    opened = NOW()
    genesis = agent.build_entry(
        entry_no=0, entry_type="open",
        payload=agent.open_payload(device_id=str(device["device_id"]), doctor_id="DR0042",
                                   hospital_id="HOSP003", patient_ref="P0012345",
                                   opened_at=opened, sample_rate=16000, channels=1,
                                   sample_width=2),
        prev_hash=None, signer=device_key)
    server.repo.chains["01JB8XQ4M7YZ2K9V3N5P6R8TZZ"] = [genesis]
    with pytest.raises(HTTPException) as refused:
        await api_v2.open_session(open_request("01JB8XQ4M7YZ2K9V3N5P6R8T01", genesis,
                                               opened.isoformat()), device=device)
    assert refused.value.status_code == 400


@asyncio_mark
async def test_a_malformed_session_id_is_a_400_not_a_500(server, device):
    with pytest.raises(HTTPException) as refused:
        await api_v2.close_session(api_v2.CloseRequest(session_id="not-a-ulid"),
                                   device=device)
    assert refused.value.status_code == 400


# ============================================================
# Against PostgreSQL: the queries, the locks, the new tables
# ============================================================

def visit_row(n):
    return {"session_id": f"01JB8XQ4M7YZ2K9V3N5P6R{n:04d}"}


@pytest_asyncio.fixture
async def live(dbs):
    from test_db_integration import enrolled_device
    device = await enrolled_device(dbs.repo)
    return SimpleNamespace(dbs=dbs, repo=dbs.repo, device=device)


async def make_session(live, sid):
    opened = datetime.now(timezone.utc)
    genesis = integrity.ChainEntry(
        entry_no=0, entry_type="open", payload={"type": "open", "n": sid},
        payload_sha256=integrity.sha256_bytes(integrity.canonical_json(
            {"type": "open", "n": sid})),
        prev_hash=None, entry_hash=b"", signature=None)
    genesis = integrity.ChainEntry(
        entry_no=0, entry_type="open", payload=genesis.payload,
        payload_sha256=genesis.payload_sha256, prev_hash=None,
        entry_hash=integrity.entry_hash(prev_hash=None, entry_no=0, entry_type="open",
                                        payload_sha256=genesis.payload_sha256),
        signature=None)
    await live.repo.open_session(
        session_id=sid, hospital_id=live.device["hospital_id"], doctor_id="DR0042",
        patient_id="P0012345", device_id=live.device["device_id"],
        session_date=opened.date(), opened_at=opened,
        audio={"sample_rate": 16000, "channels": 1, "sample_width": 2},
        consent_method="", genesis=genesis)
    return genesis


def next_entry(prev, entry_no, entry_type="pause", **payload):
    body = {"type": entry_type, **payload}
    digest = integrity.sha256_bytes(integrity.canonical_json(body))
    return integrity.ChainEntry(
        entry_no=entry_no, entry_type=entry_type, payload=body, payload_sha256=digest,
        prev_hash=prev.entry_hash,
        entry_hash=integrity.entry_hash(prev_hash=prev.entry_hash, entry_no=entry_no,
                                        entry_type=entry_type, payload_sha256=digest),
        signature=None)


@postgres
async def test_a_rival_entry_is_a_conflict_not_silently_dropped(live):
    sid = "01JB8XQ4M7YZ2K9V3N5P6R0001"
    genesis = await make_session(live, sid)
    first = next_entry(genesis, 1, reason="a")
    rival = next_entry(genesis, 1, reason="b")
    assert await live.repo.append_chain_entry(sid, first) == "stored"
    assert await live.repo.append_chain_entry(sid, first) == "duplicate"
    assert await live.repo.append_chain_entry(sid, rival) == "conflict"
    assert (await live.repo.chain_tip(sid))[1] == first.entry_hash


@postgres
async def test_one_writer_per_recording(live):
    """The second transaction waits for the first; it does not read the same head."""
    sid = "01JB8XQ4M7YZ2K9V3N5P6R0002"
    await make_session(live, sid)
    order = []

    async def writer(name, hold):
        async with live.repo.session_tx(sid):
            order.append(f"{name} in")
            await asyncio.sleep(hold)
            order.append(f"{name} out")

    await asyncio.gather(writer("a", 0.3), writer("b", 0.0))
    assert order in (["a in", "a out", "b in", "b out"], ["b in", "b out", "a in", "a out"])


@postgres
async def test_writes_inside_the_lock_roll_back_together(live):
    sid = "01JB8XQ4M7YZ2K9V3N5P6R0003"
    await make_session(live, sid)
    with pytest.raises(RuntimeError):
        async with live.repo.session_tx(sid):
            await live.repo.quarantine_session(sid, "inside")
            raise RuntimeError("the request failed")
    assert (await live.repo.get_session(sid))["status"] != "quarantined"


@postgres
async def test_a_close_never_turns_a_quarantine_back(live):
    sid = "01JB8XQ4M7YZ2K9V3N5P6R0004"
    await make_session(live, sid)
    await live.repo.quarantine_session(sid, "bad chain")
    await live.repo.close_session(sid, closed_at=datetime.now(timezone.utc),
                                  duration_seconds=1, paused_seconds=0, segment_count=0,
                                  chain_head=b"\0" * 32, manifest={})
    assert (await live.repo.get_session(sid))["status"] == "quarantined"


@postgres
async def test_a_refused_recording_cannot_be_marked_archived(live):
    sid = "01JB8XQ4M7YZ2K9V3N5P6R0005"
    await make_session(live, sid)
    await live.repo.erase_session(sid, state="refused")
    assert await live.repo.mark_archived(sid, relpath="x.wav", sha256=b"\0" * 32,
                                         byte_length=1) is False


@postgres
async def test_erasing_an_archived_recording_leaves_a_task_and_no_patient(live):
    sid = "01JB8XQ4M7YZ2K9V3N5P6R0006"
    await make_session(live, sid)
    assert await live.repo.mark_archived(sid, relpath="H/D/2026/P0012345_x/P0012345_x.wav",
                                         sha256=b"\1" * 32, byte_length=10)
    await live.repo.erase_session(sid, state="refused")
    task = await live.repo.erase_task(sid)
    assert task["files"] == ["H/D/2026/P0012345_x/P0012345_x.wav"]
    session = await live.repo.get_session(sid)
    assert session["archive_relpath"] is None and session["file_stem"] is None
    assert await live.repo.complete_erase(sid)
    assert (await live.repo.erase_task(sid))["files"] == []


@postgres
async def test_a_nonce_is_used_once(live):
    assert await live.repo.use_device_nonce(live.device["device_id"], "n" * 20)
    assert not await live.repo.use_device_nonce(live.device["device_id"], "n" * 20)


@postgres
async def test_a_genesis_opens_one_recording(live):
    sid = "01JB8XQ4M7YZ2K9V3N5P6R0007"
    genesis = await make_session(live, sid)
    assert await live.repo.genesis_in_use(genesis.entry_hash, "01JB8XQ4M7YZ2K9V3N5P6R0099")
    assert not await live.repo.genesis_in_use(genesis.entry_hash, sid)


@postgres
async def test_the_servers_login_may_use_the_new_tables(live):
    async with live.repo._pool.acquire() as conn:
        for table in ("device_nonces", "erase_tasks"):
            assert await conn.fetchval(
                "SELECT has_table_privilege('aims_api', $1, 'INSERT')", table)


# ============================================================
# The contract with the recorder, pinned
# ============================================================

def test_the_refusal_bytes_match_the_recorders_vector():
    assert integrity.canonical_json({"session_id": "S1", "device_id": "dev-1",
                                     "refused_at": "2026-09-13T04:30:00.000Z"}) == \
        b'{"device_id":"dev-1","refused_at":"2026-09-13T04:30:00.000Z","session_id":"S1"}'


@asyncio_mark
async def test_a_refusal_with_milliseconds_is_accepted(server, device_key, device):
    sid = started(server, device_key, opened_ago=timedelta(minutes=5))
    refusal = {"session_id": sid, "device_id": str(device["device_id"]),
               "refused_at": datetime.now(timezone.utc).strftime(
                   "%Y-%m-%dT%H:%M:%S.") + "123Z"}
    sig = base64.urlsafe_b64encode(
        device_key.sign(integrity.canonical_json(refusal))).decode().rstrip("=")
    assert (await refuse(sid, device, refusal=refusal, signature=sig))["status"] == "refused"


@asyncio_mark
async def test_the_signature_covers_the_raw_path_and_query(server, device_key, device):
    """The path as sent, undecoded, with ?query - from the ASGI scope."""
    ts, nonce = str(int(time.time())), "r" * 20
    raw = b"/api/v2/session/01JB/receipts?since=a%20b"
    message = integrity.device_request_message(method="GET", path=raw, timestamp=ts,
                                               nonce=nonce, body=b"")
    sig = base64.urlsafe_b64encode(device_key.sign(message)).decode().rstrip("=")

    async def no_body():
        return b""
    request = SimpleNamespace(
        method="GET", body=no_body,
        scope={"raw_path": b"/api/v2/session/01JB/receipts", "query_string": b"since=a%20b"},
        url=SimpleNamespace(path="/api/v2/session/01JB/receipts", query="since=a b"),
        headers={"x-device-timestamp": ts, "x-device-nonce": nonce,
                 "x-device-signature": sig})
    await api_v2._check_device_signature(request, device)


def test_a_software_update_close_is_not_abnormal():
    assert "software_update" in api_v2.NORMAL_CLOSE_REASONS
