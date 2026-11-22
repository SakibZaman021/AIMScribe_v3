"""
Erasing the recordings made while testing (23 Nov 2026): the list comes first
and only test clinics are taken whole; each erase is the server's own.
"""
from __future__ import annotations

import sys
import uuid
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND / "src"))
sys.path.insert(0, str(BACKEND / "tests"))

import erase_test_data                          # noqa: E402


def test_a_test_clinic_says_so():
    assert erase_test_data.is_test_clinic("TEST", "AIMS LAB test clinic")
    assert erase_test_data.is_test_clinic("HOSP_SUSASTHO_TEST", "Susastho Clinic (test 1)")
    assert erase_test_data.is_test_clinic("SIM_CLINIC", "Simulation clinic (test only)")
    assert not erase_test_data.is_test_clinic("AALO_KARAIL", "Aalo Clinic - Karail")
    assert not erase_test_data.is_test_clinic("AMADER_SUSASTHO", "Amader Susastho")


@pytest.mark.asyncio
async def test_each_recording_goes_through_the_admin_erase_and_one_failure_stops_nothing():
    asked = []

    class Client:
        async def post(self, url, params=None, headers=None):
            asked.append((url, params, headers))
            if url.endswith("/BAD/erase"):
                return SimpleNamespace(status_code=500, text="boom", json=lambda: {})
            return SimpleNamespace(status_code=200, text="",
                                   json=lambda: {"objects_deleted": 3})

    report = await erase_test_data.erase(["A1", "BAD", "C3"], by="tester",
                                         base_url="http://localhost:6000",
                                         admin_key="k", client=Client())
    assert [e["session_id"] for e in report["erased"]] == ["A1", "C3"]
    assert report["failed"][0]["session_id"] == "BAD"
    url, params, headers = asked[0]
    assert url == "http://localhost:6000/api/v2/admin/session/A1/erase"
    assert params == {"actor": "erase_test_data.py (tester)"}
    assert headers == {"X-Admin-Key": "k"}


# ============================================================
# On Postgres
# ============================================================

try:
    from test_db_integration import (_server, _unserve, a_session, dbs,  # noqa: E402,F401
                                     enrolled_device, pg)
    HAVE_POSTGRES = True
except pytest.skip.Exception:
    HAVE_POSTGRES = False

needs_postgres = pytest.mark.skipif(not HAVE_POSTGRES, reason="pgserver is not installed")


async def _recording(dbs, device, clinic, *, closed=True, ago=timedelta(hours=3),
                     patient="P1"):
    sid = await a_session(dbs.repo, device, opened_ago=ago)
    async with dbs.rec.acquire() as conn:
        await conn.execute("""
            UPDATE sessions SET hospital_id = $2, patient_id = $3,
                   status = CASE WHEN $4 THEN 'closed' ELSE status END,
                   closed_at = CASE WHEN $4 THEN opened_at + interval '5 minutes' END
             WHERE session_id = $1""", sid, clinic, patient, closed)
    return sid


@pytest.mark.asyncio
@needs_postgres
async def test_the_list_takes_a_test_clinic_whole_and_nothing_else(dbs):
    device = await enrolled_device(dbs.repo)
    clinic = f"T{uuid.uuid4().hex[:6].upper()}_TEST"
    await dbs.repo.upsert_hospital(clinic, "Test clinic", "Asia/Dhaka")
    first = await _recording(dbs, device, clinic)
    second = await _recording(dbs, device, clinic, closed=False, ago=timedelta(days=3))
    now_recording = await _recording(dbs, device, clinic, closed=False,
                                     ago=timedelta(minutes=10))
    await _recording(dbs, device, clinic, patient="REDACTED")         # erased before
    real = await _recording(dbs, device, "HOSP003")

    async with dbs.rec.acquire() as conn:
        listed = await erase_test_data.select_recordings(conn, [clinic])
    assert not listed["refused"]
    assert [r["session_id"] for r in listed["recordings"]] == [second, first]  # oldest first
    assert [r["session_id"] for r in listed["skipped"]] == [now_recording]
    text = erase_test_data.describe(listed)
    assert "Total to erase: 2 recordings" in text and real not in text

    async with dbs.rec.acquire() as conn:
        refused = await erase_test_data.select_recordings(conn, ["HOSP003", clinic])
        unknown = await erase_test_data.select_recordings(conn, ["NO_SUCH_CLINIC"])
        named = await erase_test_data.select_recordings(conn, [], [real.lower()])
    assert refused["refused"] and refused["real_clinics"] == ["HOSP003"]
    assert refused["recordings"] == []
    assert "Not a test clinic: HOSP003" in erase_test_data.describe(refused)
    assert unknown["refused"] and unknown["unknown_clinics"] == ["NO_SUCH_CLINIC"]
    # A real clinic's recording is taken only when it is named.
    assert [r["session_id"] for r in named["recordings"]] == [real]


@pytest.mark.asyncio
@needs_postgres
async def test_an_erase_removes_the_patient_and_says_who_did_it(dbs):
    import api_v2
    device = await enrolled_device(dbs.repo)
    clinic = f"T{uuid.uuid4().hex[:6].upper()}_TEST"
    await dbs.repo.upsert_hospital(clinic, "Test clinic", "Asia/Dhaka")
    sid = await _recording(dbs, device, clinic, patient="PTEST1")

    class ThroughTheServer:
        async def post(self, url, params=None, headers=None):
            session_id = url.rsplit("/", 2)[-2]
            answer = await api_v2.admin_erase_session(session_id, actor=params["actor"])
            return SimpleNamespace(status_code=200, text="", json=lambda: answer)

    _server(dbs)
    try:
        async with dbs.rec.acquire() as conn:
            listed = await erase_test_data.select_recordings(conn, [clinic])
        report = await erase_test_data.erase(
            [r["session_id"] for r in listed["recordings"]], by="tester",
            base_url="http://localhost:6000", admin_key="k", client=ThroughTheServer())
    finally:
        _unserve()
    assert [e["session_id"] for e in report["erased"]] == [sid] and not report["failed"]
    async with dbs.rec.acquire() as conn:
        row = await conn.fetchrow("SELECT patient_id, confirmation FROM sessions "
                                  "WHERE session_id = $1", sid)
        who = await conn.fetchval("SELECT actor_id FROM audit_log WHERE session_id = $1 "
                                  "AND event_type = 'session.erased'", sid)
        again = await erase_test_data.select_recordings(conn, [clinic])
    assert row["patient_id"] == "REDACTED" and row["confirmation"] == "refused"
    assert who == "erase_test_data.py (tester)"
    assert again["recordings"] == []                     # nothing left to erase
