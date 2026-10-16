"""
Migration 015 against a real PostgreSQL: markers in the chain, the clicks, the
parts, and the queries the labelling and the archive rely on.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND / "src"))
sys.path.insert(0, str(BACKEND / "tests"))
sys.path.insert(0, str(BACKEND.parent / "recorder"))

from core import crypto as agent                # noqa: E402

try:
    from test_db_integration import (           # noqa: E402,F401
        a_session, dbs, enrolled_device, pg, visit)
    HAVE_POSTGRES = True
except pytest.skip.Exception:
    HAVE_POSTGRES = False

pytestmark = [pytest.mark.asyncio,
              pytest.mark.skipif(not HAVE_POSTGRES, reason="pgserver is not installed")]

NOW = datetime(2026, 9, 13, 4, 20, tzinfo=timezone.utc)


def part(n, start, end, v, *, rule="R4", review=False, stem=None):
    return {"part_no": n, "start_seconds": start, "end_seconds": end,
            "patient_id": v.patient_id, "doctor_id": v.doctor_id,
            "cmed_hospital_id": v.cmed_hospital_id, "start_time": v.start_time,
            "visit_date": v.day, "grant_jti": None, "rule": rule, "needs_review": review,
            "file_stem": stem or f"{v.patient_id}_part{n}", "local_start": None,
            "local_end": None}


async def test_the_chain_table_accepts_a_marker_entry(dbs, tmp_path):
    from integrity import parse_entry
    device = await enrolled_device(dbs.repo)
    sid = await a_session(dbs.repo, device)
    key = agent.DeviceKey.load_or_create(tmp_path / "k", allow_plaintext=True)
    entry = agent.build_entry(entry_no=1, entry_type="marker",
                              payload={"type": "marker", "event": "patient_opened"},
                              prev_hash=await dbs.repo.chain_head(sid), signer=key)
    await dbs.repo.append_chain_entry(sid, parse_entry(entry.to_wire()))
    assert (await dbs.repo.chain_head(sid)) == entry.entry_hash


async def test_clicks_are_kept_once_in_audio_order(dbs):
    repo = dbs.repo
    device = await enrolled_device(repo)
    sid = await a_session(repo, device)
    salma, rina = visit(), visit()
    await repo.record_click(sid, entry_no=3, event="patient_opened", audio_seconds=600.0,
                            occurred_at=NOW, visit=rina, grant_jti=None)
    await repo.record_click(sid, entry_no=2, event="patient_opened", audio_seconds=360.0,
                            occurred_at=NOW, visit=salma, grant_jti=None)
    await repo.record_click(sid, entry_no=2, event="patient_opened", audio_seconds=360.0,
                            occurred_at=NOW, visit=salma, grant_jti="late-grant")
    clicks = await repo.clicks_for(sid)
    assert [(c["entry_no"], c["patient_id"]) for c in clicks] == [
        (2, salma.patient_id), (3, rina.patient_id)]
    assert clicks[0]["grant_jti"] == "late-grant"         # a grant that came later


async def test_a_clicked_patient_finds_the_recording_with_or_without_a_grant(dbs):
    repo = dbs.repo
    device = await enrolled_device(repo)
    sid = await a_session(repo, device)
    first, granted, ungranted = visit(), visit(), visit()
    await repo.create_authorisation(jti="j-first", device_id=device["device_id"],
                                    hospital_id="HOSP003", visit=first, expires_at=NOW)
    await repo.create_authorisation(jti="j-salma", device_id=device["device_id"],
                                    hospital_id="HOSP003", visit=granted, expires_at=NOW)
    for jti in ("j-first", "j-salma"):
        await repo.link_authorisation(jti, sid)
    await repo.set_session_confirmation(sid, "pending", grant_jti="j-first")

    assert await repo.session_for_visit(granted) is None    # not clicked: not attached
    await repo.record_click(sid, entry_no=1, event="patient_opened", audio_seconds=360.0,
                            occurred_at=NOW, visit=granted, grant_jti="j-salma")
    await repo.record_click(sid, entry_no=2, event="patient_opened", audio_seconds=500.0,
                            occurred_at=NOW, visit=ungranted, grant_jti=None)
    assert (await repo.session_for_visit(granted))["session_id"] == sid
    assert (await repo.session_for_visit(ungranted))["session_id"] == sid
    assert (await repo.session_for_visit(first))["session_id"] == sid
    assert [g["jti"] for g in await repo.grants_for_session(sid)] == ["j-first", "j-salma"]


async def test_parts_keep_their_files_when_only_the_labels_move(dbs):
    repo = dbs.repo
    device = await enrolled_device(repo)
    sid = await a_session(repo, device)
    sakib, salma = visit(), visit()
    await repo.store_parts(sid, [part(1, 0.0, 360.0, sakib), part(2, 360.0, None, salma)],
                           keep_archive=False)
    await repo.mark_part_archived(sid, 1, relpath="a.wav", sha256=b"\x11" * 32,
                                  byte_length=10)
    await repo.store_parts(sid, [part(1, 0.0, 360.0, sakib),
                                 part(2, 360.0, None, salma, rule="R1")], keep_archive=True)
    parts = await repo.parts_for(sid)
    assert [(p["rule"], p["archive_relpath"]) for p in parts] == [("R4", "a.wav"),
                                                                 ("R1", None)]
    assert (await repo.get_session(sid))["parts_count"] == 2
    assert [p["archive_relpath"] for p in await repo.archived_parts_for(sid)] == ["a.wav"]

    await repo.store_parts(sid, [part(1, 0.0, None, sakib, rule="R2+R4")],
                           keep_archive=False)
    assert (await repo.parts_for(sid))[0]["archive_relpath"] is None
    assert (await repo.get_session(sid))["parts_count"] == 1


async def test_a_recording_is_copied_only_when_every_part_is(dbs):
    repo = dbs.repo
    device = await enrolled_device(repo)
    sid = await a_session(repo, device)
    await repo.store_parts(sid, [part(1, 0.0, 300.0, visit()), part(2, 300.0, None, visit())],
                           keep_archive=False)
    assert await repo.mark_part_copied(sid, 1) is False
    assert await repo.mark_part_copied(sid, 2) is True


async def test_a_moved_cut_is_listed_for_recutting_and_held_from_the_copy(dbs):
    repo = dbs.repo
    device = await enrolled_device(repo)
    sid = await a_session(repo, device)
    async with repo._pool.acquire() as conn:
        await conn.execute("""
            UPDATE sessions SET closed_at = now(), archived_at = now(),
                   archive_relpath = 'x.wav', archive_sha256 = $2, archive_bytes = 10
             WHERE session_id = $1
        """, sid, b"\xab" * 32)
    assert sid in [s["session_id"] for s in await repo.pending_copy(50)]
    await repo.set_parts_stale(sid, True)
    assert sid in [s["session_id"] for s in await repo.pending_recut(50)]
    assert sid not in [s["session_id"] for s in await repo.pending_copy(50)]
    await repo.label_session(sid, patient_id="P-KARIM", doctor_id="DR0007", grant_jti=None)
    row = await repo.get_session(sid)
    assert (row["patient_id"], row["doctor_id"], row["parts_stale"]) == (
        "P-KARIM", "DR0007", True)


async def test_relabel_end_to_end_on_the_real_tables(dbs, tmp_path):
    """Close-time labelling with every query real: two patients, two named parts."""
    import api_v2
    from integrity import parse_entry
    repo = dbs.repo
    device = await enrolled_device(repo)
    sid = await a_session(repo, device)
    sakib, salma = visit(), visit()
    key = agent.DeviceKey.load_or_create(tmp_path / "k", allow_plaintext=True)
    opened = agent.build_entry(entry_no=0, entry_type="open",
                               payload={"type": "open", "patient_ref": sakib.patient_id},
                               prev_hash=None, signer=key)
    await repo.append_chain_entry(sid, parse_entry(opened.to_wire()))
    await repo.create_authorisation(jti="j-sakib", device_id=device["device_id"],
                                    hospital_id="HOSP003", visit=sakib, expires_at=NOW)
    await repo.link_authorisation("j-sakib", sid)
    async with repo._pool.acquire() as conn:
        await conn.execute("""
            UPDATE sessions SET patient_id = $2, closed_at = opened_at + interval '11 min',
                                grant_jti = 'j-sakib'
             WHERE session_id = $1
        """, sid, sakib.patient_id)
    session = await repo.get_session(sid)
    await repo.record_click(sid, entry_no=1, event="patient_opened", audio_seconds=360.0,
                            occurred_at=session["opened_at"], visit=salma, grant_jti=None)
    await repo.record_click(sid, entry_no=2, event="prescription_built", audio_seconds=540.0,
                            occurred_at=session["opened_at"], visit=salma, grant_jti=None)

    saved = api_v2.ctx.repo, api_v2.ctx.clinical
    api_v2.ctx.repo, api_v2.ctx.clinical = repo, dbs.clinical
    try:
        rows = await api_v2.relabel_session(sid)
    finally:
        api_v2.ctx.repo, api_v2.ctx.clinical = saved
    assert [(r["patient_id"], r["rule"]) for r in rows] == [
        (sakib.patient_id, "R4"), (salma.patient_id, "R1")]
    stored = await repo.parts_for(sid)
    assert [p["file_stem"].split("_")[0] for p in stored] == [sakib.patient_id,
                                                             salma.patient_id]
    assert stored[0]["local_end"] is not None
    assert (await repo.get_session(sid))["parts_count"] == 2
