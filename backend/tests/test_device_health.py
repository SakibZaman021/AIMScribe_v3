"""
What a recorder saw while offline, delivered when it is back (and alerts at once).

Also the database half of the reassign entry: the chain table's own constraint
refused 'reassign' until migration 013, and only a real PostgreSQL shows that.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND / "src"))
sys.path.insert(0, str(BACKEND / "tests"))
sys.path.insert(0, str(BACKEND.parent / "recorder"))

import api_v2                                   # noqa: E402
from core import crypto as agent                # noqa: E402

from test_v3_server import DEVICE, server       # noqa: E402,F401

try:
    from test_db_integration import a_session, dbs, enrolled_device, pg  # noqa: E402,F401
    HAVE_POSTGRES = True
except pytest.skip.Exception:
    HAVE_POSTGRES = False

asyncio = pytest.mark.asyncio


def postgres(test):
    return pytest.mark.skipif(not HAVE_POSTGRES, reason="pgserver is not installed")(
        asyncio(test))


def batch(*items):
    return api_v2.HealthLogRequest(entries=[api_v2.HealthEntry(**i) for i in items])


AN_HOUR_AGO = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
OFFLINE = {"seq": 1, "kind": "heartbeat", "at": AN_HOUR_AGO,
           "data": {"online": False, "state": "recording", "mic_level": 0.31}}
SILENT = {"seq": 2, "kind": "alert", "at": AN_HOUR_AGO,
          "data": {"alert_type": "silent_session", "session_id": "S1",
                   "detail": "two silent segments"}}


@pytest.fixture
def fake_store(server):
    rows = {}

    async def store_health(device_id, entries):
        new = [e["seq"] for e in entries if (device_id, e["seq"]) not in rows]
        rows.update({(device_id, e["seq"]): e for e in entries})
        return new

    server.repo.store_health = store_health
    return rows


@asyncio
async def test_a_backlog_is_stored_and_its_alerts_raised_once(server, fake_store):
    reply = await api_v2.device_health(batch(OFFLINE, SILENT), device=DEVICE)
    assert reply == {"status": "stored", "stored": 2, "last_seq": 2}
    raised = [a for a in server.repo.alerts if a["alert_type"] == "silent_session"]
    assert len(raised) == 1 and raised[0]["detail"]["reported_late"] is True

    again = await api_v2.device_health(batch(OFFLINE, SILENT), device=DEVICE)
    assert (again["stored"], again["last_seq"]) == (0, 2)        # safe to resend
    assert len([a for a in server.repo.alerts if a["alert_type"] == "silent_session"]) == 1


@asyncio
async def test_a_live_alert_is_not_marked_late(server, fake_store):
    now = {**SILENT, "seq": 9, "at": datetime.now(timezone.utc).isoformat(),
           "data": {**SILENT["data"], "alert_type": "microphone_unavailable"}}
    await api_v2.device_health(batch(now), device=DEVICE)
    alert = server.repo.alerts[-1]
    assert (alert["severity"], alert["detail"]["reported_late"]) == ("critical", False)


@asyncio
async def test_a_bad_entry_is_refused_whole():
    with pytest.raises(Exception):
        batch({**OFFLINE, "kind": "gossip"})


# ---------------------------------------------------------------- database

@postgres
async def test_the_health_log_table_keeps_each_entry_once(dbs):
    device = await enrolled_device(dbs.repo)
    entries = [{"seq": 1, "kind": "heartbeat", "at": datetime.now(timezone.utc),
                "data": {"online": False}},
               {"seq": 2, "kind": "alert", "at": datetime.now(timezone.utc),
                "data": {"alert_type": "silent_session"}}]
    assert await dbs.repo.store_health(device["device_id"], entries) == [1, 2]
    assert await dbs.repo.store_health(device["device_id"], entries) == []


@postgres
async def test_the_chain_table_accepts_a_reassign_entry(dbs, tmp_path):
    """Refused by the table's own CHECK before migration 013 - found live, not in tests."""
    device = await enrolled_device(dbs.repo)
    sid = await a_session(dbs.repo, device)
    key = agent.DeviceKey.load_or_create(tmp_path / "k", allow_plaintext=True)
    entry = agent.build_entry(entry_no=1, entry_type="reassign",
                              payload={"type": "reassign", "to": {"patient_id": "P2"}},
                              prev_hash=await dbs.repo.chain_head(sid), signer=key)
    from integrity import parse_entry
    await dbs.repo.append_chain_entry(sid, parse_entry(entry.to_wire()))
    assert (await dbs.repo.chain_head(sid)) == entry.entry_hash
    await dbs.repo.reassign_session(sid, patient_id="P2", doctor_id="DR0007")
    moved = await dbs.repo.get_session(sid)
    assert (moved["patient_id"], moved["doctor_id"]) == ("P2", "DR0007")


@postgres
async def test_the_dashboard_turns_the_log_into_offline_periods(dbs):
    import dashboard
    device = await enrolled_device(dbs.repo)
    start = datetime.now(timezone.utc).replace(second=0, microsecond=0) - timedelta(hours=5)
    lines, seq = [], 0
    for minute in range(99):                      # 10:02-11:40, three consultations
        seq += 1
        lines.append({"seq": seq, "kind": "heartbeat", "at": start + timedelta(minutes=minute),
                      "data": {"state": "recording", "session_id": f"S{minute // 33}",
                               "silent": False}})
    later = start + timedelta(hours=3)
    for minute in range(5):                       # a short one, with a muted microphone
        seq += 1
        lines.append({"seq": seq, "kind": "heartbeat", "at": later + timedelta(minutes=minute),
                      "data": {"state": "recording", "session_id": "S9", "silent": minute == 2}})
    await dbs.repo.store_health(device["device_id"], lines)

    async with dbs.rec.acquire() as conn:
        periods = (await dashboard._offline_periods(conn, [device["device_id"]]))[
            device["device_id"]]
    assert [(p["minutes"], p["consultations"], p["microphone"]) for p in periods] == [
        (5, 1, "silent at times"), (99, 3, "fine")]
