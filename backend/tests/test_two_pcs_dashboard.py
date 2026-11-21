"""
One visit, two PCs (visit-e2984763, 21 Nov 2026): DESKTOP-EH4I47M recorded it
11:15-11:24 and was confirmed; DrTZShama recorded the same visit at 11:57 and
waited to be erased. The server never confirms such a recording by itself;
the Recordings page shows the other PC, its times and whether the two ran at
the same time, and an admin may confirm it by hand.
"""
from __future__ import annotations

import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND / "src"))
sys.path.insert(0, str(BACKEND / "tests"))

import dashboard                                # noqa: E402

T0 = datetime(2026, 11, 21, 5, 15, 42, tzinfo=timezone.utc)


def _row(opened, closed, others):
    class Row(dict):
        def keys(self):
            return super().keys()
    return Row(opened_at=opened, closed_at=closed, other_pcs=others)


def _other(opened, closed, session_id="01OTHER", confirmation="confirmed"):
    return {"pc": "DESKTOP-EH4I47M", "session_id": session_id,
            "opened_at": opened.isoformat() if opened else None,
            "closed_at": closed.isoformat() if closed else None,
            "confirmation": confirmation}


def test_one_after_the_other_is_not_at_the_same_time():
    row = _row(T0 + timedelta(minutes=42), T0 + timedelta(minutes=49),
               [_other(T0, T0 + timedelta(minutes=9))])
    (other,) = dashboard._other_pcs(row)
    assert other["overlapped"] is False and other["pc"] == "DESKTOP-EH4I47M"


def test_two_pcs_recording_together_is_flagged():
    row = _row(T0, T0 + timedelta(minutes=10), [_other(T0 + timedelta(minutes=5), None)])
    assert dashboard._other_pcs(row)[0]["overlapped"] is True        # still open, inside


def test_a_pc_that_only_asked_recorded_nothing_to_compare():
    row = _row(T0, None, [_other(T0, None, session_id=None, confirmation=None)])
    assert dashboard._other_pcs(row)[0]["overlapped"] is None


def test_nothing_to_show_without_another_pc():
    assert dashboard._other_pcs(_row(T0, None, None)) == []
    assert dashboard._other_pcs({"opened_at": T0}) == []


# ============================================================
# On Postgres
# ============================================================

try:
    from test_db_integration import (_server, _unserve, a_session, dbs,  # noqa: E402,F401
                                     enrolled_device, pg)
    from test_db_integration import visit as a_visit                    # noqa: E402
    HAVE_POSTGRES = True
except pytest.skip.Exception:
    HAVE_POSTGRES = False


@pytest.mark.asyncio
@pytest.mark.skipif(not HAVE_POSTGRES, reason="pgserver is not installed")
async def test_the_recordings_page_names_the_other_pc_and_its_times(dbs):
    repo = dbs.repo
    first_pc, second_pc = await enrolled_device(repo), await enrolled_device(repo)
    now = datetime.now(timezone.utc)

    patient = f"P{uuid.uuid4().hex[:7]}"
    one_visit = a_visit(patient_id=patient)

    async def recorded(device, *, ago, minutes, confirmation):
        sid = await a_session(repo, device, opened_ago=ago)
        jti = uuid.uuid4().hex
        await repo.create_authorisation(jti=jti, device_id=device["device_id"],
                                        hospital_id="HOSP003", visit=one_visit, expires_at=now)
        await repo.link_authorisation(jti, sid)
        await repo.set_session_confirmation(sid, confirmation, grant_jti=jti)
        async with dbs.rec.acquire() as conn:
            await conn.execute("""
                UPDATE sessions SET patient_id = $2, session_date = $3, status = 'closed',
                       closed_at = opened_at + make_interval(mins => $4)
                 WHERE session_id = $1""", sid, patient, now.date(), minutes)
        return sid

    first = await recorded(first_pc, ago=timedelta(minutes=50), minutes=9,
                           confirmation="confirmed")
    second = await recorded(second_pc, ago=timedelta(minutes=8), minutes=7,
                            confirmation="unconfirmed")
    _server(dbs)
    try:
        day = now.date().isoformat()
        listed = await dashboard.recordings(from_date=(now.date() - timedelta(days=1)).isoformat(),
                                            to_date=day, scope=None)
    finally:
        _unserve()
    rows = {r["session_id"]: r for r in listed["recordings"]}
    (other,) = rows[second]["other_pcs"]
    assert other["session_id"] == first and other["confirmation"] == "confirmed"
    assert other["overlapped"] is False
    assert rows[second]["opened_at"] and rows[second]["closed_at"]
    assert rows[first]["other_pcs"] == []          # confirmed: nothing to decide
