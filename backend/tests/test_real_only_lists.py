"""
24 Nov 2026, after the beta split (deploy/uiu/BETA_SERVER.md): the real
dashboard lists only what is real.

- A revoked PC is still in the reply, marked revoked; the page hides it unless
  "Revoked" is chosen. 26 Nov 2026: on the real server a revoked PC that has no
  recording here is not in the reply at all (the test PCs live on beta). Removed addresses and revoked CMED connections: the same, behind a
  "Show removed" switch.
- A clinic switched off (hospitals.active = false) is not offered: the test
  clinics, once moved.
"""
from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND / "src"))
sys.path.insert(0, str(BACKEND / "tests"))

try:
    from test_db_integration import (_server, _unserve, dbs, enrolled_device,  # noqa: E402,F401
                                     pg)
    HAVE_POSTGRES = True
except pytest.skip.Exception:
    HAVE_POSTGRES = False

import dashboard                                                            # noqa: E402

pytestmark = [pytest.mark.asyncio,
              pytest.mark.skipif(not HAVE_POSTGRES, reason="pgserver is not installed")]


async def test_revoked_pcs_and_switched_off_clinics_are_not_listed(dbs):
    repo = dbs.repo
    kept, revoked = await enrolled_device(repo), await enrolled_device(repo)
    test_clinic = f"TESTC_{uuid.uuid4().hex[:6].upper()}"
    await repo.upsert_hospital(test_clinic, "A test clinic", "Asia/Dhaka")
    async with dbs.rec.acquire() as conn:
        await conn.execute("UPDATE devices SET revoked_at = now(), revoked_reason = 'beta' "
                           "WHERE device_id = $1", revoked["device_id"])
        await conn.execute("UPDATE hospitals SET active = false WHERE hospital_id = $1",
                           test_clinic)
    _server(dbs)
    try:
        async with dashboard._pool().acquire() as conn:
            listed = await dashboard._recorders(conn, None)
        clinics = await dashboard.clinics(_=None, scope=None)
    finally:
        _unserve()
    state = {str(r["device_id"]): r["revoked"] for r in listed["rooms"]}
    assert state[str(kept["device_id"])] is False
    # 26 Nov 2026: on the real server a revoked PC with no recording here is
    # not listed at all; beta lists it, marked revoked.
    assert str(revoked["device_id"]) not in state
    offered = {c["hospital_id"] for c in clinics["clinics"]}
    assert "HOSP003" in offered and test_clinic not in offered


async def test_beta_still_lists_its_revoked_pcs(dbs, monkeypatch):
    repo = dbs.repo
    revoked = await enrolled_device(repo)
    async with dbs.rec.acquire() as conn:
        await conn.execute("UPDATE devices SET revoked_at = now() WHERE device_id = $1",
                           revoked["device_id"])
    monkeypatch.setenv("AIMS_ENVIRONMENT", "beta")
    _server(dbs)
    try:
        async with dashboard._pool().acquire() as conn:
            listed = await dashboard._recorders(conn, None)
    finally:
        _unserve()
    state = {str(r["device_id"]): r["revoked"] for r in listed["rooms"]}
    assert state[str(revoked["device_id"])] is True


async def test_a_revoked_pc_that_recorded_here_stays_listed(dbs):
    from datetime import timedelta
    from test_db_integration import a_session
    repo = dbs.repo
    device = await enrolled_device(repo)
    await a_session(repo, device, opened_ago=timedelta(minutes=3))
    async with dbs.rec.acquire() as conn:
        await conn.execute("UPDATE devices SET revoked_at = now() WHERE device_id = $1",
                           device["device_id"])
    _server(dbs)
    try:
        async with dashboard._pool().acquire() as conn:
            listed = await dashboard._recorders(conn, None)
    finally:
        _unserve()
    assert str(device["device_id"]) in {str(r["device_id"]) for r in listed["rooms"]}


async def test_the_real_server_can_stop_approving_pcs_by_itself(dbs):
    """052: with auto_approve_pcs off, CMED's confirmations never approve a PC."""
    from datetime import timedelta
    from test_db_integration import a_session
    repo = dbs.repo
    device = await enrolled_device(repo)
    async with dbs.rec.acquire() as conn:
        await conn.execute("UPDATE devices SET approval = 'pending' WHERE device_id = $1",
                           device["device_id"])
        await conn.execute("UPDATE server_settings SET value = 'off' "
                           "WHERE name = 'auto_approve_pcs'")
        try:
            for ago in (3, 2):
                sid = await a_session(repo, device, opened_ago=timedelta(minutes=ago))
                await conn.execute("UPDATE sessions SET confirmation = 'confirmed' "
                                   "WHERE session_id = $1", sid)
            assert await conn.fetchval("SELECT approval FROM devices WHERE device_id = $1",
                                       device["device_id"]) == "pending"
        finally:
            await conn.execute("UPDATE server_settings SET value = 'on' "
                               "WHERE name = 'auto_approve_pcs'")


async def test_recordings_all_on_the_jabra_have_their_own_filter(dbs):
    """24 Nov 2026: "On the Jabra" - every part on the Jabra - beside "Not on the Jabra"."""
    from datetime import timedelta
    from test_db_integration import a_session
    repo = dbs.repo
    device = await enrolled_device(repo)
    jabra = await a_session(repo, device, opened_ago=timedelta(minutes=3))
    laptop = await a_session(repo, device, opened_ago=timedelta(minutes=2))
    async with dbs.rec.acquire() as conn:
        await conn.execute("UPDATE sessions SET microphones = ARRAY['Jabra Speak 410'] "
                           "WHERE session_id = $1", jabra)
        await conn.execute("UPDATE sessions SET microphones = ARRAY['Microphone Array (Intel)'] "
                           "WHERE session_id = $1", laptop)
    _server(dbs)
    try:
        on = await dashboard.recordings(from_date=None, to_date=None, hospital_id=None,
                                        doctor_id=None, patient_id=None, state="on_jabra",
                                        search=None, limit=500, _=None, scope=None)
        off = await dashboard.recordings(from_date=None, to_date=None, hospital_id=None,
                                         doctor_id=None, patient_id=None, state="not_jabra",
                                         search=None, limit=500, _=None, scope=None)
    finally:
        _unserve()
    on_ids = {r["session_id"] for r in on["recordings"]}
    off_ids = {r["session_id"] for r in off["recordings"]}
    assert jabra in on_ids and laptop not in on_ids
    assert laptop in off_ids and jabra not in off_ids
