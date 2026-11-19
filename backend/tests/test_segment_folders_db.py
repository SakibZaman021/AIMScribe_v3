"""
The segment store's layout and the pieces log on Postgres (20 Nov 2026):
the address switch is all or nothing and never touches a recording CMED has
confirmed; the pieces log says what arrived, what is missing and where it is.
"""
from __future__ import annotations

import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND / "src"))
sys.path.insert(0, str(BACKEND / "tests"))

import api_v2                                   # noqa: E402

try:
    from test_db_integration import (_server, _unserve, a_session, dbs,  # noqa: E402,F401
                                     enrolled_device, pg)
    HAVE_POSTGRES = True
except pytest.skip.Exception:
    HAVE_POSTGRES = False

pytestmark = [pytest.mark.asyncio,
              pytest.mark.skipif(not HAVE_POSTGRES, reason="pgserver is not installed")]


async def _waiting(dbs, device, *, pieces=(1, 2), count=None, confirmation="unconfirmed"):
    """A finished recording waiting for CMED, its pieces in the segment store."""
    sid = await a_session(dbs.repo, device, opened_ago=timedelta(minutes=10))
    async with dbs.rec.acquire() as conn:
        await conn.execute("""
            UPDATE sessions SET closed_at = now(), status = 'closed', confirmation = $2,
                   segment_count = $3, object_prefix = $4, session_date = $5
             WHERE session_id = $1
        """, sid, confirmation, count if count is not None else len(pieces),
             f"P1_DR0042_HOSP003_101432_20261119_{sid[-5:]}", date(2026, 11, 19))
        for n in pieces:
            digest = bytes([n]) * 32
            await conn.execute("""
                INSERT INTO chain_entries (session_id, entry_no, entry_type, payload,
                                           payload_sha256, prev_hash, entry_hash,
                                           device_signature, occurred_at)
                VALUES ($1, $2, 'segment', '{}', $3, $3, $3, $3, now())
            """, sid, n, digest)
            await conn.execute("""
                INSERT INTO segments (session_id, seq_no, entry_no, object_key, sha256,
                                      bytes, duration_seconds, state,
                                      captured_start_at, captured_end_at, committed_at)
                VALUES ($1, $2, $2, $3, $4, 100, 30.0, 'committed',
                        now() - interval '10 minutes', now() - interval '9 minutes',
                        now() - interval '9 minutes' + make_interval(secs => $5))
            """, sid, n, f"audio/{sid}/seg_{n:05d}.wav", digest, 3 if n != 2 else 400)
    return sid


def _moves(sid, pieces=(1, 2)):
    return [(n, f"audio/{sid}/seg_{n:05d}.wav", f"audio/NEW/{sid}/piece_{n:04d}.wav")
            for n in pieces]


async def _keys(dbs, sid):
    async with dbs.rec.acquire() as conn:
        rows = await conn.fetch("SELECT object_key FROM segments WHERE session_id = $1 "
                                "ORDER BY seq_no", sid)
        prefix = await conn.fetchval("SELECT object_prefix FROM sessions "
                                     "WHERE session_id = $1", sid)
    return [r["object_key"] for r in rows], prefix


async def test_a_waiting_recording_is_switched_to_its_new_addresses(dbs):
    device = await enrolled_device(dbs.repo)
    sid = await _waiting(dbs, device)
    assert sid in [s["session_id"] for s in await dbs.repo.pieces_waiting_to_move(500)]
    assert await dbs.repo.move_pieces(sid, _moves(sid), f"NEW/{sid}")
    keys, prefix = await _keys(dbs, sid)
    assert keys == [m[2] for m in _moves(sid)] and prefix == f"NEW/{sid}"


async def test_a_confirmed_recording_is_never_switched(dbs):
    device = await enrolled_device(dbs.repo)
    sid = await _waiting(dbs, device, confirmation="confirmed")
    assert sid not in [s["session_id"] for s in await dbs.repo.pieces_waiting_to_move(500)]
    before = await _keys(dbs, sid)
    assert not await dbs.repo.move_pieces(sid, _moves(sid), f"NEW/{sid}")
    assert await _keys(dbs, sid) == before


async def test_one_piece_out_of_step_undoes_the_whole_switch(dbs):
    device = await enrolled_device(dbs.repo)
    sid = await _waiting(dbs, device)
    before = await _keys(dbs, sid)
    moves = _moves(sid)
    moves[1] = (2, "audio/somewhere/else.wav", moves[1][2])      # not where it is
    assert not await dbs.repo.move_pieces(sid, moves, f"NEW/{sid}")
    assert await _keys(dbs, sid) == before                       # piece 1 not moved either


def _person(monkeypatch, role):
    import portal

    async def person_for(request):
        return SimpleNamespace(email="someone@uiu.example", role=role)
    monkeypatch.setattr(portal, "person_for", person_for)
    return SimpleNamespace(headers={}, query_params={})


def _rows(response):
    import csv
    import io
    return list(csv.DictReader(io.StringIO(response.body.decode("utf-8-sig"))))


async def test_the_pieces_log_says_what_arrived_and_what_is_missing(dbs, monkeypatch):
    import dashboard
    device = await enrolled_device(dbs.repo)
    sid = await _waiting(dbs, device, pieces=(1, 2, 4), count=4)
    request = _person(monkeypatch, "researcher")
    _server(dbs)
    try:
        pieces = await dashboard.pieces_csv(request, table="pieces", session_id=sid,
                                            scope=None)
        recordings = await dashboard.pieces_csv(request, table="recordings", session_id=sid,
                                                scope=None)
    finally:
        _unserve()
    assert pieces.headers["content-disposition"] == f'attachment; filename="pieces_{sid}.csv"'
    rows = _rows(pieces)
    assert [r["piece"] for r in rows] == ["1", "2", "4"]
    assert {r["where_now"] for r in rows} == {"in the segment store; waiting for CMED"}
    assert rows[0]["fingerprint_sha256"] == "01" * 32
    assert rows[1]["arrived_seconds_after_recorded"] == "400"
    (summary,) = _rows(recordings)
    assert (summary["pieces_in_recording"], summary["pieces_arrived"],
            summary["missing_piece_numbers"]) == ("4", "3", "3")
    assert summary["pieces_arrived_over_2_min_late"] == "1"
    assert summary["status"] == "finished, but pieces are missing"
    assert summary["folder_in_segment_store"] == (
        f"audio/P1_DR0042_HOSP003_101432_20261119_{sid[-5:]}/")


async def test_the_dates_narrow_the_pieces_log(dbs, monkeypatch):
    import dashboard
    device = await enrolled_device(dbs.repo)
    sid = await _waiting(dbs, device)
    request = _person(monkeypatch, "admin")
    _server(dbs)
    try:
        inside = await dashboard.pieces_csv(request, table="recordings", from_date="2026-11-19",
                                            to_date="2026-11-19", hospital_id="HOSP003",
                                            scope=None)
        outside = await dashboard.pieces_csv(request, table="recordings",
                                             from_date="2026-11-20", to_date="2026-11-21",
                                             scope=None)
        with pytest.raises(HTTPException) as backwards:
            await dashboard.pieces_csv(request, table="recordings", from_date="2026-11-21",
                                       to_date="2026-11-20", scope=None)
    finally:
        _unserve()
    assert sid in [r["recording"] for r in _rows(inside)]
    assert sid not in [r["recording"] for r in _rows(outside)]
    assert backwards.value.status_code == 400


async def test_a_data_collector_cannot_download_the_pieces_log(dbs, monkeypatch):
    import dashboard
    request = _person(monkeypatch, "collector")
    _server(dbs)
    try:
        with pytest.raises(HTTPException) as refused:
            await dashboard.pieces_csv(request, table="pieces", scope=["HOSP003"])
    finally:
        _unserve()
    assert refused.value.status_code == 403
