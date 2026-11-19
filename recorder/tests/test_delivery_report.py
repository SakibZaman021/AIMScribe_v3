"""
3.9.6: every heartbeat says, for each recording this PC still holds something
of, how many pieces it sealed, how many the server accepted, and the numbers
still waiting - so the server's pieces log can name the pieces on the PC.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from core import spool as spool_mod
from core.spool import COMMITTED, PURGED, RECEIPTED
from tests.conftest import pcm
from tests.fakes import ScriptedUploader, make_controller
from tests.test_device_state import GRANTED, a_session, uploader

START = datetime(2026, 11, 20, 4, 0, tzinfo=timezone.utc)


def _pieces(session, n):
    for i in range(n):
        at = START + timedelta(seconds=30 * i)
        session.seal_segment(pcm(0.1, value=1000), captured_start_at=at,
                             captured_end_at=at + timedelta(seconds=30), rms_mean=1000.0,
                             is_final=False)


def test_a_recording_with_pieces_still_to_send_is_listed_by_number(spool, device_key):
    up = uploader(spool, device_key)
    rec = a_session(spool, device_key)
    _pieces(rec, 20)
    for seq in range(1, 16):
        rec.set_state(seq, COMMITTED)
    up._sessions = [rec]
    report = up.delivery_report()
    assert report["complete"] is True
    (row,) = report["recordings"]
    assert (row["session_id"], row["recorded"], row["last_recorded"], row["sent"],
            row["waiting"]) == (rec.session_id, 20, 20, 15, 5)
    assert row["waiting_numbers"] == [16, 17, 18, 19, 20]
    assert row["rejected_numbers"] == [] and row["finished"] is False
    assert row["oldest_waiting_at"].endswith("Z")


def test_sent_receipted_and_deleted_pieces_all_count_as_sent(spool, device_key):
    up = uploader(spool, device_key)
    rec = a_session(spool, device_key)
    _pieces(rec, 4)
    rec.set_state(1, PURGED)
    rec.set_state(2, RECEIPTED)
    rec.set_state(3, COMMITTED)
    rec.set_state(4, spool_mod.QUARANTINED)
    up._sessions = [rec]
    (row,) = up.delivery_report()["recordings"]
    assert (row["sent"], row["waiting"], row["rejected_numbers"]) == (3, 0, [4])


def test_a_recording_fully_sent_and_closed_is_left_out(spool, device_key):
    up = uploader(spool, device_key)
    done, sending = a_session(spool, device_key), a_session(spool, device_key)
    _pieces(done, 2)
    _pieces(sending, 2)
    for seq in (1, 2):
        done.set_state(seq, COMMITTED)
    done.close(duration_seconds=60, paused_seconds=0)
    done.mark_close_reported()
    sending.close(duration_seconds=60, paused_seconds=0)     # closed, not yet sent
    up._sessions = [done, sending]
    rows = up.delivery_report()["recordings"]
    assert [r["session_id"] for r in rows] == [sending.session_id]
    assert rows[0]["finished"] is True and rows[0]["waiting_numbers"] == [1, 2]


def test_only_the_longest_waiting_are_listed_and_the_list_says_so(spool, device_key):
    up = uploader(spool, device_key)
    up.REPORT_MOST = 2
    recs = [a_session(spool, device_key) for _ in range(3)]
    for r in recs:
        _pieces(r, 1)
    up._sessions = recs
    report = up.delivery_report()
    assert len(report["recordings"]) == 2 and report["complete"] is False


async def test_the_heartbeat_carries_the_report(monkeypatch, spool, device_key):
    class Reporting(ScriptedUploader):
        def delivery_report(self):
            return {"recordings": [{"session_id": "S1", "waiting_numbers": [3]}],
                    "complete": True}

    ctl = make_controller(monkeypatch, spool, device_key, Reporting([GRANTED]))
    ctl.cfg.app_version, ctl.cfg.protocol_version = "3.9.6", 2
    payload = ctl.heartbeat_payload()
    assert payload["recordings"] == [{"session_id": "S1", "waiting_numbers": [3]}]
    assert payload["recordings_complete"] is True


async def test_a_failing_report_never_costs_the_heartbeat(monkeypatch, spool, device_key):
    class Broken(ScriptedUploader):
        def delivery_report(self):
            raise RuntimeError("spool unreadable")

    ctl = make_controller(monkeypatch, spool, device_key, Broken([GRANTED]))
    ctl.cfg.app_version, ctl.cfg.protocol_version = "3.9.6", 2
    payload = ctl.heartbeat_payload()
    assert "recordings" not in payload and payload["device_id"]
