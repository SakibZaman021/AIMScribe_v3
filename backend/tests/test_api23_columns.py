"""
The recordings list says when CMED's API 2 and API 3 arrived (18 Nov 2026).

With 17,000 recordings nobody pages through tables to see whether API 2 came
for the one just made: the newest recording is the first row of the
dashboard's list, and its API 2 and API 3 columns answer it.
"""
from __future__ import annotations

import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

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

START = "2026-11-17T11:50:39+06:00"


async def _recording(dbs, device, patient, *, visit_id=None, grant=True):
    from confirmation import Visit
    sid = await a_session(dbs.repo, device)
    if grant:
        jti = uuid.uuid4().hex
        v = Visit(patient_id=patient, doctor_id="DR0042", cmed_hospital_id="CMED-DHOLPUR",
                  start_time=START, visit_date="2026-11-17")
        await dbs.repo.create_authorisation(jti=jti, device_id=device["device_id"],
                                            hospital_id="HOSP003", visit=v,
                                            expires_at=datetime.now(timezone.utc),
                                            visit_id=visit_id)
        async with dbs.rec.acquire() as conn:
            await conn.execute("UPDATE sessions SET patient_id = $2, grant_jti = $3 "
                               "WHERE session_id = $1", sid, patient, jti)
    else:
        async with dbs.rec.acquire() as conn:
            await conn.execute("UPDATE sessions SET patient_id = $2 WHERE session_id = $1",
                               sid, patient)
    return sid


async def test_the_list_says_when_api2_and_api3_arrived(dbs):
    import clinical_v4
    import dashboard
    device = await enrolled_device(dbs.repo)
    patient = f"P{uuid.uuid4().hex[:7]}"
    sid = await _recording(dbs, device, patient, visit_id="VIS-T-777")
    api_v2.ctx.clinical = dbs.clinical
    body = {"visit_id": "VIS-T-777", "patient_id": patient, "doctor_id": "DR0042",
            "hospital_id": "CMED-DHOLPUR", "start_time": START, "date": "2026-11-17"}
    await clinical_v4.file_message("patient_information", body, "CMED-DHOLPUR", "VIS-T-777",
                                   patient, clinical_v4.read("patient_information", body), "t")
    _server(dbs)
    try:
        listed = await dashboard.recordings(patient_id=patient)
    finally:
        _unserve()
    row = next(r for r in listed["recordings"] if r["session_id"] == sid)
    assert row["cmed_matched"] is True
    assert row["api2_at"] is not None and row["api3_at"] is None


async def test_a_visit_without_a_visit_id_is_found_by_patient_and_start(dbs):
    """The older interface: no visit_id, so the API 2 is found by patient and start time."""
    import dashboard
    import integrity
    from confirmation import Visit
    device = await enrolled_device(dbs.repo)
    patient = f"P{uuid.uuid4().hex[:7]}"
    sid = await _recording(dbs, device, patient)
    v = Visit(patient_id=patient, doctor_id="DR0042", cmed_hospital_id="CMED-DHOLPUR",
              start_time=START, visit_date="2026-11-17")
    body = {"patient_id": patient, "doctor_id": "DR0042", "hospital_id": "CMED-DHOLPUR",
            "start_time": START, "date": "2026-11-17", "items": [{"drug": "Napa"}]}
    await dbs.clinical.receive(kind="prescription", visit=v, hospital_id="HOSP003", body=body,
                               body_sha256=integrity.sha256_bytes(json.dumps(body).encode()))
    _server(dbs)
    try:
        listed = await dashboard.recordings(patient_id=patient)
    finally:
        _unserve()
    row = next(r for r in listed["recordings"] if r["session_id"] == sid)
    assert row["api2_at"] is None and row["api3_at"] is not None


async def test_a_recording_with_nothing_to_match_shows_no_columns(dbs):
    import dashboard
    device = await enrolled_device(dbs.repo)
    patient = f"P{uuid.uuid4().hex[:7]}"
    sid = await _recording(dbs, device, patient, grant=False)
    _server(dbs)
    try:
        listed = await dashboard.recordings(patient_id=patient)
    finally:
        _unserve()
    row = next(r for r in listed["recordings"] if r["session_id"] == sid)
    assert row["cmed_matched"] is False and row["api2_at"] is None
