"""
What the PC saw while offline reaches the server afterwards (and alerts at once),
and the recorder stays quick: no folder walk per status, no wait when offline.
"""
from __future__ import annotations

import json
import time
from types import SimpleNamespace

import pytest

from core.health_log import HealthLog
from core.uploader import AuthOutcome, UploadManager
from tests.fakes import ScriptedUploader, make_controller, trigger


def log(tmp_path, **kw):
    return HealthLog(tmp_path / "health_log.jsonl", **kw)


def test_alerts_are_always_kept_and_heartbeats_once_a_minute(tmp_path):
    h = log(tmp_path, routine_every=60)
    h.alert({"alert_type": "silent_session", "session_id": "S1", "detail": "muted?",
             "noise": "dropped"})
    assert h.offline_heartbeat({"state": "recording", "mic_level": 0.4})
    assert not h.offline_heartbeat({"state": "recording"})      # too soon
    entries = h.pending()
    assert [e["kind"] for e in entries] == ["alert", "heartbeat"]
    assert entries[0]["data"] == {"alert_type": "silent_session", "session_id": "S1",
                                  "detail": "muted?"}
    assert entries[1]["data"]["online"] is False
    assert entries[0]["seq"] < entries[1]["seq"]


def test_only_what_the_server_stored_is_deleted(tmp_path):
    h = log(tmp_path, routine_every=0)
    for n in range(5):
        h.alert({"alert_type": f"a{n}"})
    entries = h.pending()
    assert h.delivered(entries[2]["seq"]) == 3
    assert [e["data"]["alert_type"] for e in h.pending()] == ["a3", "a4"]
    h.delivered(entries[-1]["seq"])
    assert h.pending() == [] and not h.path.exists()


def test_order_survives_a_restart_and_a_torn_line_is_skipped(tmp_path):
    h = log(tmp_path)
    h.alert({"alert_type": "first"})
    with open(h.path, "a", encoding="utf-8") as f:
        f.write('{"seq": 99999999999999999, "kind": "al')       # power cut mid-write
    again = log(tmp_path)
    again.alert({"alert_type": "second"})
    names = [e["data"]["alert_type"] for e in again.pending()]
    assert names == ["first", "second"]


def test_over_the_cap_heartbeats_go_before_alerts(tmp_path):
    h = log(tmp_path, max_bytes=4000, routine_every=0)
    h.alert({"alert_type": "keep_me"})
    for _ in range(60):
        h.offline_heartbeat({"state": "idle", "pad": "x" * 40})
    assert h.size <= 4000
    kinds = [e["kind"] for e in h.pending(1000)]
    assert "alert" in kinds and kinds.count("heartbeat") < 60


# ---------------------------------------------------------------- delivery

def uploader_with(tmp_path, replies):
    cfg = SimpleNamespace(
        security=SimpleNamespace(grant_issuer="aimslab", grant_audience="aimscribe-recorder"),
        backend=SimpleNamespace(url=lambda e: f"https://server{e}", request_timeout=5,
                                retry_backoff=(0.0,)),
        spool=SimpleNamespace(purge_grace_hours=0), app_version="3", protocol_version=2)
    up = UploadManager(cfg, device_key=None, spool=None, receipt_public_key=None)
    up.health = log(tmp_path, routine_every=0)
    up._http = object()
    up.sent = []
    script = list(replies)

    async def post(endpoint, payload, attempts=None):
        up.sent.append((endpoint, payload))
        return script.pop(0) if script else None

    up._post = post
    return up


async def test_delivered_entries_are_removed_and_a_failure_keeps_them(tmp_path):
    up = uploader_with(tmp_path, [None])
    up.health.alert({"alert_type": "capture_failed"})
    assert await up.deliver_health() == 0                       # offline: kept
    assert len(up.health.pending()) == 1

    last = up.health.pending()[-1]["seq"]
    up._post = None
    up2 = uploader_with(tmp_path, [{"status": "stored", "stored": 1, "last_seq": last}])
    assert await up2.deliver_health() == 1
    assert up2.sent[0][0] == "/device/health" and up2.health.pending() == []


async def test_a_failed_heartbeat_is_logged_for_later(monkeypatch, spool, device_key, tmp_path):
    class Offline(ScriptedUploader):
        health = log(tmp_path, routine_every=0)

        async def heartbeat(self, status):
            return False

    up = Offline([AuthOutcome("pending", "AGENT_NOT_READY", "")])
    ctl = make_controller(monkeypatch, spool, device_key, up)
    ctl.cfg.app_version, ctl.cfg.protocol_version = "3.4.0", 2
    await ctl.open_session(trigger("P1"))
    payload = ctl.heartbeat_payload()
    assert {"mic_level", "silent", "authorisation"} <= payload.keys()
    assert await up.heartbeat(payload) is False
    up.health.offline_heartbeat(payload)
    line = up.health.pending()[-1]
    assert line["kind"] == "heartbeat" and line["data"]["state"] == "recording"
    await ctl.close()


# ---------------------------------------------------------------- speed

def test_status_does_not_walk_the_spool_every_time(spool, monkeypatch):
    walks = {"n": 0}
    real = type(spool.root).iterdir

    def counting(self):
        walks["n"] += 1
        return real(self)

    monkeypatch.setattr(type(spool.root), "iterdir", counting)
    for _ in range(40):                            # ten seconds of overlay polling
        spool.pressure()
        spool.total_bytes()
    assert walks["n"] == 1
    spool.has_capacity(1)                          # a start always measures afresh
    assert walks["n"] == 2


async def test_start_answers_at_once_when_the_server_is_known_unreachable(
        monkeypatch, spool, device_key):
    class Unreachable(ScriptedUploader):
        consecutive_failures = 3

    up = Unreachable([AuthOutcome("pending", "AGENT_NOT_READY", "")], delay=1.0)
    ctl = make_controller(monkeypatch, spool, device_key, up, authorise_wait_seconds=1.5)
    began = time.monotonic()
    result = await ctl.open_session(trigger("P1"))
    assert time.monotonic() - began < 0.3
    assert result["authorisation"] == "pending" and ctl.state == "recording"
    await ctl.close()
