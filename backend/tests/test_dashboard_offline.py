"""
An open recording on a PC that has gone quiet (18 Nov 2026).

Aalo Karail, 17 Nov 21:59: CMED's PC opened a recording and went offline two
seconds later. The dashboard called it "active" all night. Its audio is on the
PC and arrives - and the recording closes - when the PC is back; the dashboard
now says so instead.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND / "src"))

import dashboard                                # noqa: E402


class Row(dict):
    def keys(self):
        return list(super().keys())


NOW = datetime.now(timezone.utc)


def test_an_open_recording_on_a_quiet_pc_is_waiting_on_that_pc():
    seen = NOW - timedelta(hours=9)
    assert dashboard._offline_since(Row(closed_at=None, pc_last_seen=seen)) == seen.isoformat()


def test_a_pc_heard_from_minutes_ago_is_still_recording():
    assert dashboard._offline_since(Row(closed_at=None,
                                        pc_last_seen=NOW - timedelta(minutes=2))) is None


def test_a_closed_recording_is_never_waiting():
    assert dashboard._offline_since(Row(closed_at=NOW, pc_last_seen=NOW - timedelta(days=1))) \
        is None


def test_an_older_query_without_the_column_changes_nothing():
    assert dashboard._offline_since(Row(closed_at=None)) is None
