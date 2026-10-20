"""
A close that arrives a moment before its last clip is normal, not an alarm.

Seen live: nearly every short recording raised segment_count_mismatch, because
the recorder sends the close while the last clip is still uploading. The server
answers "incomplete", the recorder sends the close again, and all is well - so
a person is told only if the clips are still missing ten minutes on.
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
from test_reassign import device, device_key, started  # noqa: E402,F401
from test_v3_server import server               # noqa: E402,F401

pytestmark = pytest.mark.asyncio


async def close(sid, device, closed_ago):
    at = (datetime.now(timezone.utc) - closed_ago).isoformat()
    return await api_v2.close_session(api_v2.CloseRequest(
        session_id=sid, closed_at=at, segment_count=1), device=device)


def late(server):
    return [a for a in server.repo.alerts if a["alert_type"] == "segment_count_mismatch"]


async def test_a_clip_still_uploading_is_not_an_alarm(server, device_key, device):
    sid = started(server, device_key)
    reply = await close(sid, device, timedelta(seconds=2))
    assert reply["status"] == "incomplete"
    assert late(server) == []


async def test_clips_missing_ten_minutes_on_are_reported_once(server, device_key, device):
    sid = started(server, device_key)
    for _ in range(3):
        assert (await close(sid, device, timedelta(minutes=12)))["status"] == "incomplete"
    alerts = late(server)
    assert len(alerts) == 1 and alerts[0]["detail"]["minutes_since_close"] >= 10
