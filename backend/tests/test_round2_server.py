"""
Round 2: what a recorder says about itself (U3, X1, V2, U1), a recording that
continues another after a crash or restart (S4), and one unconfirmed alert per
clinic outage instead of one per recording (D4).
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

from test_v3_server import DEVICE, open_session, server  # noqa: E402,F401

try:
    from test_db_integration import a_session, dbs, enrolled_device, pg  # noqa: E402,F401
    HAVE_POSTGRES = True
except pytest.skip.Exception:
    HAVE_POSTGRES = False

postgres = pytest.mark.skipif(not HAVE_POSTGRES, reason="pgserver is not installed")
pytestmark = pytest.mark.asyncio


def alerts(server, kind):
    return [a for a in server.repo.alerts if a["alert_type"] == kind]


async def beat(**kw):
    return await api_v2.heartbeat(api_v2.HeartbeatRequest(**kw), device=DEVICE)


# ---------------------------------------------------------------- heartbeat

async def test_the_heartbeat_keeps_what_the_pc_says_about_itself(server):
    held = [{"session_id": "01HELD", "code": "UNMAPPED_CLINIC", "since": "2026-10-16T09:00Z"}]
    await beat(app_version="3.4.0", held=held, disk_encrypted=True, clock_offset_seconds=-3.2)
    state = server.repo.devices[str(DEVICE["device_id"])]
    assert state["held_sessions"] == held and state["held_count"] == 1
    assert (state["disk_encrypted"], state["clock_offset_seconds"]) == (True, -3.2)
    assert alerts(server, "recorder_outdated") == alerts(server, "disk_not_encrypted") == []


async def test_an_old_build_and_an_open_disk_are_each_said_once(server):
    for _ in range(4):
        await beat(app_version="3.3.0", disk_encrypted=False)
    outdated, open_disk = alerts(server, "recorder_outdated"), alerts(server, "disk_not_encrypted")
    assert len(outdated) == len(open_disk) == 1
    assert outdated[0]["detail"]["minimum"] == "3.4.0"


async def test_the_minimum_version_is_a_setting(server, monkeypatch):
    monkeypatch.setenv("AIMS_MIN_RECORDER_VERSION", "3.5.0")
    await beat(app_version="3.4.0")
    assert len(alerts(server, "recorder_outdated")) == 1


async def test_versions_compare_as_numbers():
    vt = api_v2.version_tuple
    assert vt("3.10.0") > vt("3.4.0") > vt("3.3.9") > vt("garbage")
    assert not api_v2.recorder_outdated("3.4.0") and api_v2.recorder_outdated("3.3.0")
    assert not api_v2.recorder_outdated(None)          # an old server row: unknown, not flagged


async def test_an_old_recorder_without_the_new_fields_changes_nothing(server):
    await beat(app_version="3.4.0", held=[], disk_encrypted=True)
    await beat(app_version="3.4.0")                     # 3.3-style heartbeat fields only
    state = server.repo.devices[str(DEVICE["device_id"])]
    assert state["held_sessions"] == [] and state["disk_encrypted"] is True


# ---------------------------------------------------------------- continuation (S4)

def confirmed_first(server):
    first = open_session(server, opened_ago=timedelta(minutes=12))
    server.repo.sessions[first].update(confirmation="confirmed", file_stem="P1_first")
    second = open_session(server)
    server.repo.sessions[second].update(confirmation="pending", file_stem="P1_second")
    return first, second


async def test_a_resumed_recording_is_confirmed_and_linked_like_the_first(server):
    first, second = confirmed_first(server)
    state = await api_v2._link_continuation(second, first, "P0012345", DEVICE, "pending")
    assert state == "confirmed" and server.repo.sessions[second]["confirmation"] == "confirmed"
    links = await api_v2._continuation_payload(second)
    assert links == {"continues": {"session_id": first, "file_stem": "P1_first"}}
    assert (await api_v2._continuation_payload(first))["continued_by"]["session_id"] == second


async def test_it_must_be_this_pcs_recording_of_this_patient(server):
    first, second = confirmed_first(server)
    state = await api_v2._link_continuation(second, first, "P0099999", DEVICE, "pending")
    assert state == "pending" and alerts(server, "continuation_rejected")
    assert await api_v2._continuation_payload(second) == {}


async def test_a_late_api2_confirms_the_continuation_too(server):
    first, second = confirmed_first(server)
    server.repo.sessions[first]["confirmation"] = "pending"
    await api_v2._link_continuation(second, first, "P0012345", DEVICE, "pending")
    assert server.repo.sessions[second]["confirmation"] == "pending"
    server.repo.sessions[first]["confirmation"] = "confirmed"
    assert await api_v2.confirm_continuations(first) == [second]
    assert server.repo.sessions[second]["confirmation"] == "confirmed"


async def test_an_archived_first_recording_gets_its_json_written_again(server):
    first, second = confirmed_first(server)
    server.repo.sessions[first]["archived_at"] = datetime.now(timezone.utc)
    await api_v2._link_continuation(second, first, "P0012345", DEVICE, "pending")
    assert server.repo.sessions[first].get("json_stale") is True


async def test_a_continuation_is_never_marked_unconfirmed_when_the_first_is_confirmed(server):
    first, second = confirmed_first(server)
    server.repo.sessions[second]["continues_session_id"] = first
    await api_v2._mark_unconfirmed(second, server.repo.sessions[second])
    assert server.repo.sessions[second]["confirmation"] == "confirmed"
    assert alerts(server, "unconfirmed_recording") == []


# ---------------------------------------------------------------- D4

async def test_a_cmed_outage_is_one_alert_per_clinic(server):
    sids = [open_session(server, opened_ago=timedelta(minutes=5)) for _ in range(6)]
    for sid in sids:
        server.repo.sessions[sid]["confirmation"] = "pending"
        await api_v2._mark_unconfirmed(sid, server.repo.sessions[sid])
    raised = alerts(server, "unconfirmed_recording")
    assert len(raised) == 1
    assert raised[0]["detail"]["count"] == 6 and raised[0]["detail"]["session_ids"] == sids
    assert all(server.repo.sessions[s]["confirmation"] == "unconfirmed" for s in sids)


# ---------------------------------------------------------------- real PostgreSQL

@postgres
async def test_the_device_columns_and_dashboard_on_postgres(dbs):
    import dashboard
    device = await enrolled_device(dbs.repo)
    held = [{"session_id": "01HELDX", "code": "KEY_MISMATCH", "since": "2026-10-16T09:00Z"}]
    await dbs.repo.touch_device(device["device_id"], app_version="3.3.0", held_sessions=held,
                                held_count=1, disk_encrypted=False, clock_offset_seconds=-130.5)
    async with dbs.rec.acquire() as conn:
        recorders = await dashboard._recorders(conn, None)
    room = next(r for r in recorders["rooms"] if r["device_id"] == str(device["device_id"]))
    assert (room["held_count"], room["held"][0]["code"]) == (1, "KEY_MISMATCH")
    assert (room["disk_encrypted"], room["clock_offset_seconds"], room["outdated"]) == \
        (False, -130.5, True)
    assert recorders["held"][0]["session_id"] == "01HELDX"
    assert recorders["outdated"] >= 1 and recorders["unencrypted"] >= 1

    await dbs.repo.touch_device(device["device_id"])          # an older heartbeat: kept
    async with dbs.rec.acquire() as conn:
        again = await dashboard._recorders(conn, None)
    room = next(r for r in again["rooms"] if r["device_id"] == str(device["device_id"]))
    assert room["held_count"] == 1 and room["disk_encrypted"] is False


@postgres
async def test_grouped_alerts_count_and_a_new_window_starts_a_new_one(dbs):
    device = await enrolled_device(dbs.repo)
    for n in range(3):
        sid = await a_session(dbs.repo, device)
        await dbs.repo.group_alert(alert_type="unconfirmed_recording", group="unconfirmed:H9",
                                   session_id=sid, window=timedelta(hours=6),
                                   device_id=device["device_id"])
    async with dbs.rec.acquire() as conn:
        rows = await conn.fetch("SELECT id, detail FROM integrity_alerts "
                                "WHERE detail->>'group' = 'unconfirmed:H9'")
        assert len(rows) == 1
        import json
        detail = json.loads(rows[0]["detail"]) if isinstance(rows[0]["detail"], str) \
            else rows[0]["detail"]
        assert detail["count"] == 3 and len(detail["session_ids"]) == 3
        await conn.execute("UPDATE integrity_alerts SET raised_at = now() - interval '7 hours' "
                           "WHERE id = $1", rows[0]["id"])
    assert await dbs.repo.group_alert(alert_type="unconfirmed_recording",
                                      group="unconfirmed:H9", session_id="X",
                                      window=timedelta(hours=6)) is True
    assert await dbs.repo.alert_raised_since("unconfirmed_recording", device["device_id"],
                                             datetime.now(timezone.utc) - timedelta(days=1))


@postgres
async def test_continuation_links_and_the_review_panel_on_postgres(dbs):
    import dashboard
    device = await enrolled_device(dbs.repo)
    first, second = await a_session(dbs.repo, device), await a_session(dbs.repo, device)
    await dbs.repo.set_continuation(second, first)
    await dbs.repo.set_continuation(second, "something else")     # first link wins
    links = await dbs.repo.continuation_links(second)
    assert links["continues"]["session_id"] == first and links["continued_by"] is None
    assert (await dbs.repo.continuation_links(first))["continued_by"]["session_id"] == second
    assert await dbs.repo.continuations_of(first) == [second]
    assert (await dbs.repo.get_session(second))["continues_session_id"] == first

    await dbs.repo.raise_alert(alert_type="labelling_review", session_id=first,
                               device_id=device["device_id"], detail={"parts": 2})
    async with dbs.rec.acquire() as conn:
        review = await dashboard._review(conn, None)
    assert review["count"] >= 1 and review["items"][0]["session_id"] == first
