"""
25 Nov 2026: on Recordings, a patient ID (by its start) and a visit ID find
the exact recordings, and the doctor narrows the counts as well as the list.
"""
from __future__ import annotations

import json
import sys
import uuid
from datetime import timedelta
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND / "src"))
sys.path.insert(0, str(BACKEND / "tests"))

try:
    from test_db_integration import (_server, _unserve, a_session, dbs,  # noqa: E402,F401
                                     enrolled_device, pg)
    HAVE_POSTGRES = True
except pytest.skip.Exception:
    HAVE_POSTGRES = False

import dashboard                                                         # noqa: E402

pytestmark = [pytest.mark.asyncio,
              pytest.mark.skipif(not HAVE_POSTGRES, reason="pgserver is not installed")]


def _list(**kw):
    base = dict(from_date=None, to_date=None, hospital_id=None, doctor_id=None,
                patient_id=None, visit_id=None, state=None, search=None, limit=500,
                _=None, scope=None)
    return dashboard.recordings(**{**base, **kw})


async def test_patient_by_its_start_visit_exactly_and_the_doctor_in_the_counts(dbs):
    repo = dbs.repo
    device = await enrolled_device(repo)
    tag = uuid.uuid4().hex[:6].upper()
    a = await a_session(repo, device, opened_ago=timedelta(minutes=5))
    b = await a_session(repo, device, opened_ago=timedelta(minutes=4))
    async with dbs.rec.acquire() as conn:
        for sid, patient, doctor, visit in ((a, f"9ca656d0-{tag}-aaa", f"DRA{tag}", f"visit-{tag}a"),
                                            (b, f"77aa0000-{tag}-bbb", f"DRB{tag}", f"visit-{tag}b")):
            await conn.execute("""
                UPDATE sessions SET patient_id = $2, doctor_id = $3,
                       manifest = $4::jsonb, confirmation = 'pending'
                 WHERE session_id = $1
            """, sid, patient, doctor, json.dumps({"trigger": {"visit_id": visit}}))
    _server(dbs)
    try:
        by_patient = await _list(patient_id=f"9ca656d0-{tag}")
        by_visit = await _list(visit_id=f"visit-{tag}b")
        whole = await dashboard.summary(from_date=None, to_date=None, hospital_id=None,
                                        doctor_id=None, _=None, scope=None)
        one = await dashboard.summary(from_date=None, to_date=None, hospital_id=None,
                                      doctor_id=f"DRA{tag}", _=None, scope=None)
    finally:
        _unserve()
    assert [r["session_id"] for r in by_patient["recordings"]] == [a]
    assert [r["session_id"] for r in by_visit["recordings"]] == [b]
    assert one["doctor_id"] == f"DRA{tag}"
    assert one["confirmation"]["totals"].get("pending") == 1
    assert whole["confirmation"]["totals"].get("pending", 0) >= 2
