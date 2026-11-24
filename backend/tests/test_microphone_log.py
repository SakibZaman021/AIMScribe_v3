"""
25 Nov 2026 (recorder 3.9.7): a recording's microphones come from its own log.

The heartbeat reported one microphone every 30 s, so a recording shorter than
that - 22 of GP-Female's 35 on 23 Nov - had none, and a third microphone used
between two heartbeats was never known. Each "microphone" line of the log the
recording closes with is added, once each, in the order used.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND / "src"))
sys.path.insert(0, str(BACKEND / "tests"))

try:
    from test_db_integration import a_session, dbs, enrolled_device, pg  # noqa: E402,F401
    HAVE_POSTGRES = True
except pytest.skip.Exception:
    HAVE_POSTGRES = False

pytestmark = [pytest.mark.asyncio,
              pytest.mark.skipif(not HAVE_POSTGRES, reason="pgserver is not installed")]


async def test_every_microphone_in_the_log_is_kept_once_in_order(dbs):
    repo = dbs.repo
    device = await enrolled_device(repo)
    sid = await a_session(repo, device, opened_ago=timedelta(seconds=20))
    # One microphone already seen by a heartbeat.
    await repo.note_session_microphone(device["device_id"], sid,
                                       "Echo Cancelling Speakerphone (Jabra Speak2 55 MS)")
    events = [
        {"at": "t1", "event": "microphone",
         "detail": "Echo Cancelling Speakerphone (Jabra Speak2 55 MS)"},
        {"at": "t2", "event": "microphone", "detail": "Headset Microphone (USB Audio)"},
        {"at": "t3", "event": "microphone_switch", "detail": "the Jabra was unplugged"},
        {"at": "t4", "event": "microphone", "detail": "Microphone Array (Intel® Smart Sound)"},
        {"at": "t5", "event": "microphone", "detail": "Headset Microphone (USB Audio)"},
    ]
    await repo.close_session(sid, closed_at=datetime.now(timezone.utc), duration_seconds=12.0,
                             paused_seconds=0.0, segment_count=1, chain_head=b"\x00" * 32,
                             manifest={"events": events})
    async with dbs.rec.acquire() as conn:
        mics = await conn.fetchval("SELECT microphones FROM sessions WHERE session_id = $1", sid)
    assert mics == ["Echo Cancelling Speakerphone (Jabra Speak2 55 MS)",
                    "Headset Microphone (USB Audio)",
                    "Microphone Array (Intel® Smart Sound)"]


async def test_a_short_recording_with_no_heartbeat_still_has_its_microphone(dbs):
    repo = dbs.repo
    device = await enrolled_device(repo)
    sid = await a_session(repo, device, opened_ago=timedelta(seconds=5))
    await repo.close_session(sid, closed_at=datetime.now(timezone.utc), duration_seconds=4.0,
                             paused_seconds=0.0, segment_count=1, chain_head=b"\x00" * 32,
                             manifest={"events": [{"at": "t", "event": "microphone",
                                                   "detail": "Microphone Array (Intel)"}]})
    async with dbs.rec.acquire() as conn:
        mics = await conn.fetchval("SELECT microphones FROM sessions WHERE session_id = $1", sid)
    assert mics == ["Microphone Array (Intel)"]
