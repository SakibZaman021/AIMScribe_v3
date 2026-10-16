"""A PC whose clock is far off is reported once, not every 30 seconds."""
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

from test_v3_server import DEVICE, server       # noqa: E402,F401

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def fresh(server):
    async def touch_device(device_id, **kw):
        return None
    server.repo.touch_device = touch_device
    api_v2._CLOCK_ALERTED.clear()
    yield
    api_v2._CLOCK_ALERTED.clear()


def skews(server):
    return [a for a in server.repo.alerts if a["alert_type"] == "clock_skew"]


async def beat(**kw):
    return await api_v2.heartbeat(api_v2.HeartbeatRequest(**kw), device=DEVICE)


async def test_a_far_off_clock_is_reported_once(server):
    for _ in range(5):
        assert (await beat(clock_offset_seconds=-310.4))["status"] == "ok"
    alerts = skews(server)
    assert len(alerts) == 1
    assert alerts[0]["detail"]["pc_clock"] == "ahead" and alerts[0]["severity"] == "warning"


async def test_a_small_offset_is_not_an_alert(server):
    await beat(clock_offset_seconds=7.0)
    await beat(clock_offset_seconds=-45.0)
    assert skews(server) == []


async def test_an_old_recorder_is_judged_by_its_sent_at(server):
    behind = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    await beat(sent_at=behind)
    assert skews(server)[0]["detail"]["pc_clock"] == "behind"


async def test_it_is_reported_again_after_six_hours(server):
    await beat(clock_offset_seconds=200)
    api_v2._CLOCK_ALERTED[str(DEVICE["device_id"])] -= timedelta(hours=7)
    await beat(clock_offset_seconds=200)
    assert len(skews(server)) == 2
