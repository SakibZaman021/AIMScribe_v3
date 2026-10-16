"""
The server half of labelling: markers in, parts out, files named per patient.

The markers are built and signed with the recorder's own crypto, so the two
halves are tested against each other. The rules themselves are tested in
test_labelling.py; here it is what the server does with their verdict.
"""
from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND / "src"))
sys.path.insert(0, str(BACKEND / "tests"))
sys.path.insert(0, str(BACKEND.parent / "recorder"))

import api_v2                                   # noqa: E402
from core import crypto as agent                # noqa: E402

from test_v3_server import (                    # noqa: E402,F401
    DEVICE, FIELDS, NOW, api2, mint, open_session, prescription, send, server, visit_of)

pytestmark = pytest.mark.asyncio

SALMA = {**FIELDS, "patient_id": "P0055555", "start_time": "2026-09-13T10:20:00+06:00"}
KARIM = {**FIELDS, "patient_id": "P0099999", "start_time": "2026-09-13T10:14:42+06:00"}


@pytest.fixture
def device_key(tmp_path):
    return agent.DeviceKey.load_or_create(tmp_path / "device.key", allow_plaintext=True)


@pytest.fixture
def device(device_key):
    return {**DEVICE, "tpm_pubkey": device_key.public_bytes_raw()}


async def recording(server, device_key, *, minutes=11):
    """A recording opened for FIELDS' patient under its grant, as the server holds it."""
    _, grant = await mint(FIELDS)
    sid = open_session(server, opened_ago=timedelta(minutes=minutes + 1))
    server.repo.auths[grant["jti"]]["session_id"] = sid
    server.repo.sessions[sid].update(
        grant_jti=grant["jti"], confirmation="pending", sample_rate=44100, channels=1,
        sample_width=2, manifest={}, archive_relpath=None, archive_sha256=None,
        archive_bytes=None)
    server.repo.chains[sid] = [agent.build_entry(
        entry_no=0, entry_type="open",
        payload={"type": "open", "patient_ref": FIELDS["patient_id"]},
        prev_hash=None, signer=device_key)]
    server.repo.segments[sid] = [
        {"seq_no": n, "object_key": f"audio/{sid}/seg_{n:05d}.wav", "sha256": b"\x00" * 32,
         "state": "committed", "bytes": 5_292_000, "duration_seconds": 60.0,
         "clip_name": None, "object_deleted_at": None} for n in range(1, minutes + 1)]
    return sid


def marker(server, sid, device_key, event, fields, seconds):
    head = server.repo.chains[sid][-1]
    opened = server.repo.sessions[sid]["opened_at"]
    return agent.build_entry(
        entry_no=head.entry_no + 1, entry_type="marker",
        payload={"type": "marker", "event": event, "patient": dict(fields),
                 "audio_seconds": seconds,
                 "at": agent.iso_utc(opened + timedelta(seconds=seconds))},
        prev_hash=head.entry_hash, signer=device_key)


async def deliver(sid, entry, device, grant_jti=None):
    return await api_v2.session_marker(api_v2.MarkerRequest(
        session_id=sid, chain_entry=entry.to_wire(), grant_jti=grant_jti), device=device)


async def click(server, sid, device_key, device, fields, seconds, *, grant=True):
    jti = None
    if grant:
        _, g = await mint(fields)
        jti = g["jti"]
    return await deliver(sid, marker(server, sid, device_key, "patient_opened", fields,
                                     seconds), device, jti)


async def prescribed(server, sid, device_key, device, fields, seconds):
    return await deliver(sid, marker(server, sid, device_key, "prescription_built",
                                     fields, seconds), device)


def close(server, sid):
    s = server.repo.sessions[sid]
    s["closed_at"] = s["opened_at"] + timedelta(minutes=11)


# ---------------------------------------------------------------- the marker

async def test_a_click_is_kept_with_its_second_and_its_grant(server, device_key, device):
    sid = await recording(server, device_key)
    reply = await click(server, sid, device_key, device, SALMA, 360.0)
    assert (reply["status"], reply["used"], reply["grant_linked"]) == ("recorded", True, True)
    [row] = server.repo.clicks[sid]
    assert (row["event"], row["audio_seconds"], row["patient_id"]) == (
        "patient_opened", 360.0, "P0055555")
    assert server.repo.chains[sid][-1].entry_type == "marker"
    assert server.repo.auths[row["grant_jti"]]["session_id"] == sid


async def test_a_redelivered_marker_is_a_duplicate_and_an_early_one_waits(
        server, device_key, device):
    sid = await recording(server, device_key)
    entry = marker(server, sid, device_key, "patient_opened", SALMA, 30.0)
    await deliver(sid, entry, device)
    again = await deliver(sid, entry, device)
    assert again["duplicate"] is True and len(server.repo.clicks[sid]) == 1

    early = agent.build_entry(entry_no=9, entry_type="marker",
                              payload={"type": "marker"}, prev_hash=b"\x01" * 32,
                              signer=device_key)
    assert (await deliver(sid, early, device))["status"] == "deferred"


async def test_a_bad_grant_or_payload_is_alerted_never_refused(server, device_key, device):
    sid = await recording(server, device_key)
    _, other = await mint(KARIM)                      # a grant for someone else
    reply = await deliver(sid, marker(server, sid, device_key, "patient_opened", SALMA, 40),
                          device, other["jti"])
    assert (reply["status"], reply["grant_linked"]) == ("recorded", False)
    assert server.repo.alerts[-1]["alert_type"] == "marker_grant_invalid"

    head = server.repo.chains[sid][-1]
    junk = agent.build_entry(entry_no=head.entry_no + 1, entry_type="marker",
                             payload={"type": "marker", "event": "sneezed"},
                             prev_hash=head.entry_hash, signer=device_key)
    reply = await deliver(sid, junk, device)
    assert (reply["status"], reply["used"]) == ("recorded", False)
    assert server.repo.chains[sid][-1].entry_no == junk.entry_no      # chain intact
    assert server.repo.alerts[-1]["alert_type"] == "marker_unreadable"


# ---------------------------------------------------------------- one patient

async def test_a_recording_without_markers_is_named_as_it_always_was(server, device_key):
    sid = await recording(server, device_key)
    close(server, sid)
    assert await api_v2.relabel_session(sid) is None
    assert sid not in server.repo.parts


async def test_wrong_pick_corrected_makes_the_whole_recording_karims(
        server, device_key, device):
    await send("patient_information", api2(**KARIM))
    sid = await recording(server, device_key)
    await click(server, sid, device_key, device, KARIM, 10.0)
    await prescribed(server, sid, device_key, device, KARIM, 420.0)
    close(server, sid)
    [part] = await api_v2.relabel_session(sid)
    session = server.repo.sessions[sid]
    assert (part["patient_id"], part["rule"]) == ("P0099999", "R3+R1")
    assert session["patient_id"] == "P0099999"
    assert session["file_stem"].startswith("P0099999_")
    assert session["confirmation"] == "confirmed"           # Karim's API 2 counts
    assert server.repo.auths[session["grant_jti"]]["patient_id"] == "P0099999"


async def test_deliberate_click_and_back_stays_the_first_patients(
        server, device_key, device):
    sid = await recording(server, device_key)
    await click(server, sid, device_key, device, SALMA, 40.0)
    await click(server, sid, device_key, device, FIELDS, 70.0, grant=False)
    await prescribed(server, sid, device_key, device, FIELDS, 500.0)
    close(server, sid)
    [part] = await api_v2.relabel_session(sid)
    assert (part["patient_id"], part["rule"]) == ("P0012345", "R2+R1")
    assert server.repo.sessions[sid]["file_stem"].startswith("P0012345_")


# ---------------------------------------------------------------- two patients

async def follow_up_then_salma(server, device_key, device):
    """Sakib (FIELDS) 6 min with no prescription; Salma at 6:00, her rx at 9:00."""
    await send("patient_information", api2(**SALMA))
    sid = await recording(server, device_key)
    await click(server, sid, device_key, device, SALMA, 360.0)
    await prescribed(server, sid, device_key, device, SALMA, 540.0)
    close(server, sid)
    return sid, await api_v2.relabel_session(sid)


async def test_a_follow_up_and_the_next_patient_become_two_files(
        server, device_key, device):
    sid, parts = await follow_up_then_salma(server, device_key, device)
    assert [(p["patient_id"], p["start_seconds"], p["end_seconds"], p["rule"])
            for p in parts] == [("P0012345", 0.0, 360.0, "R4"),
                                ("P0055555", 360.0, None, "R1")]
    assert parts[0]["file_stem"].startswith("P0012345_DR0042_HOSP003_")
    assert parts[1]["file_stem"].startswith("P0055555_DR0042_HOSP003_")
    assert parts[0]["file_stem"].split("_")[4] == parts[1]["file_stem"].split("_")[3]
    assert server.repo.sessions[sid]["patient_id"] == "P0012345"      # untouched
    assert {k["file_stem"] for k in server.clinical.linked} >= {p["file_stem"] for p in parts}


async def test_the_archive_gets_the_cut_and_a_json_per_part(server, device_key, device):
    sid, parts = await follow_up_then_salma(server, device_key, device)
    pending = (await api_v2.archive_pending())["sessions"]
    [entry] = [s for s in pending if s["session_id"] == sid]
    assert [(p["part_no"], p["start_seconds"], p["end_seconds"], p["file_stem"])
            for p in entry["parts"]] == [(1, 0.0, 360.0, parts[0]["file_stem"]),
                                         (2, 360.0, None, parts[1]["file_stem"])]

    doc = await api_v2.archive_clinical_document(sid, part=2)
    assert doc["file_stem"] == parts[1]["file_stem"]
    assert doc["recording"]["derived_from"]["start_seconds"] == 360.0
    assert doc["recording"]["derived_from"]["rule"] == "R1"
    whole = await api_v2.archive_clinical_document(sid)
    assert "derived_from" not in whole["recording"]

    await api_v2.archive_complete(api_v2.ArchiveCompleteRequest(
        session_id=sid, archive_relpath="H/D/2026-09-13/a/a.wav", sha256="ab" * 32,
        bytes=58_212_044,
        parts=[api_v2.ArchivedPart(part_no=1, archive_relpath="H/D/2026-09-13/a/a.wav",
                                   sha256="11" * 32, bytes=31_752_044,
                                   start_seconds=0.0, end_seconds=360.0),
               api_v2.ArchivedPart(part_no=2, archive_relpath="H/D/2026-09-13/b/b.wav",
                                   sha256="22" * 32, bytes=26_460_044,
                                   start_seconds=360.0)]))
    stored = server.repo.parts[sid]
    assert [p["archive_relpath"] for p in stored] == ["H/D/2026-09-13/a/a.wav",
                                                      "H/D/2026-09-13/b/b.wav"]
    copy_pending = (await api_v2.archive_copy_pending())["sessions"]
    [c] = [s for s in copy_pending if s["session_id"] == sid]
    assert [p["sha256"] for p in c["parts"]] == ["11" * 32, "22" * 32]


async def test_each_part_is_copied_and_the_pieces_go_only_after_the_last(
        server, device_key, device):
    sid, parts = await follow_up_then_salma(server, device_key, device)
    server.repo.sessions[sid].update(archived_at=NOW(), archive_relpath="x.wav",
                                     archive_sha256=b"\xab" * 32, archive_bytes=10)
    keys = []
    for n in (1, 2):
        auth = await api_v2.archive_copy_authorize(
            api_v2.CopyAuthorizeRequest(session_id=sid, kind="audio", part_no=n))
        assert parts[n - 1]["file_stem"] in auth["object_key"]
        server.copy.accept(auth["object_key"], 100, "0" * 32)
        keys.append(auth["object_key"])

    def done(n, final):
        return api_v2.CopyCompleteRequest(session_id=sid, part_no=n, final=final, copies=[
            api_v2.CopyRecord(kind="audio", object_key=keys[n - 1], bytes=100,
                              sha256="ab" * 32, md5="0" * 32)])

    first = await api_v2.archive_copy_complete(done(1, False))
    assert first["status"] == "part_copied" and not server.repo.sessions[sid].get("copied_at")
    last = await api_v2.archive_copy_complete(done(2, True))
    assert last["status"] == "copied" and server.repo.sessions[sid]["copied_at"]


# ---------------------------------------------------------------- late evidence

async def test_a_late_api2_for_a_clicked_patient_finds_the_recording(
        server, device_key, device):
    sid, _ = await follow_up_then_salma(server, device_key, device)
    found = await server.repo.session_for_visit(visit_of(SALMA))
    assert found["session_id"] == sid


async def test_a_late_3b_that_only_changes_the_rule_rewrites_the_json(
        server, device_key, device):
    sid = await recording(server, device_key)
    await click(server, sid, device_key, device, SALMA, 360.0)       # no 3a reached the PC
    close(server, sid)
    parts = await api_v2.relabel_session(sid)
    assert [p["rule"] for p in parts] == ["R4", "R4"]
    server.repo.sessions[sid]["archived_at"] = NOW()

    await send("prescription", prescription(**SALMA))
    assert [p["rule"] for p in server.repo.parts[sid]] == ["R4", "R1"]
    assert server.repo.sessions[sid].get("json_stale") is True
    assert not server.repo.sessions[sid].get("parts_stale")


async def test_a_late_3b_that_moves_the_cut_marks_the_files_to_be_cut_again(
        server, device_key, device):
    sid = await recording(server, device_key)
    await click(server, sid, device_key, device, SALMA, 200.0)
    await click(server, sid, device_key, device, FIELDS, 500.0, grant=False)  # back: a glance?
    close(server, sid)
    [only] = await api_v2.relabel_session(sid)
    assert only["rule"] == "R2+R4"
    server.repo.sessions[sid].update(archived_at=NOW(), archive_sha256=b"\xab" * 32)
    server.repo.parts[sid][0]["archive_relpath"] = "H/D/day/sakib/sakib.wav"

    await send("prescription", prescription(**SALMA))       # Salma was seen after all
    assert [p["patient_id"] for p in server.repo.parts[sid]] == [
        "P0012345", "P0055555", "P0012345"]
    assert server.repo.sessions[sid]["parts_stale"] is True
    assert any(a["alert_type"] == "relabelled_after_archive" for a in server.repo.alerts)
    assert any(a["alert_type"] == "labelling_review" for a in server.repo.alerts)

    [recut] = (await api_v2.archive_recut_pending())["sessions"]
    assert recut["session_id"] == sid and len(recut["parts"]) == 3
    assert recut["archive_sha256"] == "ab" * 32


async def test_a_short_last_stretch_asks_a_person(server, device_key, device):
    sid = await recording(server, device_key)
    await click(server, sid, device_key, device, SALMA, 640.0)       # 20 s before close
    close(server, sid)
    parts = await api_v2.relabel_session(sid)
    assert [p["needs_review"] for p in parts] == [False, True]
    review = [a for a in server.repo.alerts if a["alert_type"] == "labelling_review"]
    assert len(review) == 1
    await api_v2.relabel_session(sid)                     # nothing changed: no second alert
    assert len([a for a in server.repo.alerts if a["alert_type"] == "labelling_review"]) == 1


async def test_a_marker_arriving_after_close_relabels(server, device_key, device):
    sid = await recording(server, device_key)
    close(server, sid)
    await click(server, sid, device_key, device, KARIM, 10.0)       # the PC was offline
    assert server.repo.sessions[sid]["patient_id"] == "P0099999"


async def test_v3_close_reasons_are_not_alarms():
    assert {"counselling_limit", "no_activity_limit", "pc_shutdown",
            "superseded_by_new_patient"} <= api_v2.NORMAL_CLOSE_REASONS
