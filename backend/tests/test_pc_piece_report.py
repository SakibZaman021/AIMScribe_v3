"""
Recorder 3.9.6 reports, with every heartbeat, the pieces it still holds of
each recording (20 Nov 2026). The server keeps that on the recording - only
the PC's own - and the pieces log names the pieces still on the PC.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND / "src"))
sys.path.insert(0, str(BACKEND / "tests"))

import api_v2                                   # noqa: E402

from test_v3_server import DEVICE, server       # noqa: E402,F401

SID = "01M7SPZC8F929V47JCX96JWRS5"


def test_a_report_is_made_safe_before_it_is_kept():
    kept = api_v2._piece_reports([
        {"session_id": SID, "recorded": 20, "sent": 15, "waiting": 5,
         "waiting_numbers": [20, 16, 17, 18, 19, 16, "x", -1, True],
         "rejected_numbers": [3], "finished": True},
        {"session_id": "../etc", "recorded": 1},                 # not a recording ID
        "not a dict",
        {"session_id": SID[:-1] + "A", "recorded": "many", "sent": -2},
    ])
    assert kept[0] == {"session_id": SID, "recorded": 20, "sent": 15, "waiting": 5,
                       "waiting_numbers": [16, 17, 18, 19, 20], "rejected_numbers": [3],
                       "finished": True}
    assert len(kept) == 2 and kept[1]["recorded"] is None and kept[1]["sent"] is None


@pytest.mark.asyncio
async def test_the_heartbeat_keeps_the_report_and_never_fails_for_it(server):
    seen = []

    async def note_piece_reports(device_id, reports, *, complete):
        seen.append((device_id, [r["session_id"] for r in reports], complete))

    server.repo.note_piece_reports = note_piece_reports
    body = api_v2.HeartbeatRequest(recordings=[{"session_id": SID, "waiting_numbers": [4]}],
                                   recordings_complete=False)
    assert (await api_v2.heartbeat(body, device=DEVICE))["status"] == "ok"
    assert seen == [(DEVICE["device_id"], [SID], False)]

    async def broken(*a, **kw):
        raise RuntimeError("database away")
    server.repo.note_piece_reports = broken
    assert (await api_v2.heartbeat(body, device=DEVICE))["status"] == "ok"

    # An older recorder sends no report: nothing is touched.
    seen.clear()
    server.repo.note_piece_reports = note_piece_reports
    await api_v2.heartbeat(api_v2.HeartbeatRequest(), device=DEVICE)
    assert seen == []


# ============================================================
# On Postgres
# ============================================================

try:
    from test_db_integration import _server, _unserve, dbs, enrolled_device, pg  # noqa: E402,F401
    from test_segment_folders_db import _person, _rows, _waiting                 # noqa: E402
    HAVE_POSTGRES = True
except pytest.skip.Exception:
    HAVE_POSTGRES = False

async def _pc(dbs, sid):
    async with dbs.rec.acquire() as conn:
        return dict(await conn.fetchrow(
            "SELECT pc_pieces_recorded, pc_pieces_sent, pc_pieces_waiting, pc_waiting_numbers, "
            "pc_rejected_numbers, pc_finished_recording, pc_reported_at "
            "FROM sessions WHERE session_id = $1", sid))


def _report(sid, waiting, recorded=5, rejected=()):
    return {"session_id": sid, "recorded": recorded, "sent": recorded - len(waiting),
            "waiting": len(waiting), "waiting_numbers": list(waiting),
            "rejected_numbers": list(rejected), "finished": False}


@pytest.mark.asyncio
@pytest.mark.skipif(not HAVE_POSTGRES, reason="pgserver is not installed")
async def test_a_report_is_kept_only_on_the_pcs_own_recording(dbs):
    mine, theirs = await enrolled_device(dbs.repo), await enrolled_device(dbs.repo)
    sid = await _waiting(dbs, mine, pieces=(1, 2))
    await dbs.repo.note_piece_reports(theirs["device_id"], [_report(sid, [3, 4, 5])])
    assert (await _pc(dbs, sid))["pc_reported_at"] is None       # another PC's word: ignored
    await dbs.repo.note_piece_reports(mine["device_id"], [_report(sid, [3, 4, 5])])
    kept = await _pc(dbs, sid)
    assert (kept["pc_pieces_recorded"], kept["pc_pieces_waiting"],
            kept["pc_waiting_numbers"]) == (5, 3, [3, 4, 5])


@pytest.mark.asyncio
@pytest.mark.skipif(not HAVE_POSTGRES, reason="pgserver is not installed")
async def test_a_recording_no_longer_listed_has_nothing_waiting_unless_the_list_was_cut(dbs):
    device = await enrolled_device(dbs.repo)
    sid = await _waiting(dbs, device, pieces=(1, 2))
    await dbs.repo.note_piece_reports(device["device_id"], [_report(sid, [3])])
    await dbs.repo.note_piece_reports(device["device_id"], [], complete=False)
    assert (await _pc(dbs, sid))["pc_pieces_waiting"] == 1       # maybe just not listed
    await dbs.repo.note_piece_reports(device["device_id"], [], complete=True)
    kept = await _pc(dbs, sid)
    assert (kept["pc_pieces_waiting"], kept["pc_waiting_numbers"]) == (0, [])


@pytest.mark.asyncio
@pytest.mark.skipif(not HAVE_POSTGRES, reason="pgserver is not installed")
async def test_the_pieces_log_names_the_pieces_still_on_the_pc(dbs, monkeypatch):
    import dashboard
    device = await enrolled_device(dbs.repo)
    sid = await _waiting(dbs, device, pieces=(1, 2), count=5)
    async with dbs.rec.acquire() as conn:          # the PC has not finished sending
        await conn.execute("UPDATE sessions SET closed_at = NULL, status = 'active' "
                           "WHERE session_id = $1", sid)
    # Piece 3 arrived after the PC's last word: it is no longer "on the PC".
    await dbs.repo.note_piece_reports(device["device_id"],
                                      [_report(sid, [3, 4, 5], rejected=[])])
    async with dbs.rec.acquire() as conn:
        digest = bytes([3]) * 32
        await conn.execute("""
            INSERT INTO chain_entries (session_id, entry_no, entry_type, payload,
                                       payload_sha256, prev_hash, entry_hash,
                                       device_signature, occurred_at)
            VALUES ($1, 3, 'segment', '{}', $2, $2, $2, $2, now())""", sid, digest)
        await conn.execute("""
            INSERT INTO segments (session_id, seq_no, entry_no, object_key, sha256, bytes,
                                  duration_seconds, state, captured_start_at,
                                  captured_end_at, committed_at)
            VALUES ($1, 3, 3, $2, $3, 100, 30.0, 'committed', now(), now(), now())
        """, sid, f"audio/{sid}/seg_00003.wav", digest)
    request = _person(monkeypatch, "researcher")
    _server(dbs)
    try:
        pieces = _rows(await dashboard.pieces_csv(request, table="pieces", session_id=sid,
                                                  scope=None))
        (summary,) = _rows(await dashboard.pieces_csv(request, table="recordings",
                                                      session_id=sid, scope=None))
        listed = await dashboard.recordings(from_date="2026-11-19", to_date="2026-11-19",
                                            scope=None)
    finally:
        _unserve()
    assert [(r["piece"], r["where_now"]) for r in pieces] == [
        ("1", "in the segment store; recording still sending"),
        ("2", "in the segment store; recording still sending"),
        ("3", "in the segment store; recording still sending"),
        ("4", "on the PC: not sent yet"),
        ("5", "on the PC: not sent yet")]
    assert pieces[3]["fingerprint_sha256"] == "" and pieces[3]["arrived_at"] == ""
    assert (summary["pieces_recorded_on_pc"], summary["pieces_waiting_on_pc"],
            summary["piece_numbers_waiting_on_pc"]) == ("5", "2", "4 5")
    assert summary["missing_piece_numbers"] == "4 5"
    assert summary["status"] == "the PC still has 2 piece(s) to send: 4 5"
    row = next(r for r in listed["recordings"] if r["session_id"] == sid)
    assert row["pc_waiting"] == [4, 5]
