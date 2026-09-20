"""
The load test's own arithmetic (`AT-29`).

A load test that quietly counts wrongly is worse than none: it would sign
off a server that drops pieces. These tests are about the two numbers the
result rests on - what a clinic day looks like, and whether the replies
were fast enough.

    python -m pytest tools/test_load_test.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import load_test                                     # noqa: E402
from load_test import Timings                        # noqa: E402


# ============================================================
# The plan
# ============================================================

def a_day(**changes):
    settings = dict(rooms=14, hours=8, minutes_each=15, gap_minutes=5, speed=60)
    settings.update(changes)
    return load_test.plan_day(**settings)


def test_a_clinic_day_fills_every_room():
    plan = a_day()
    rooms = {c.room for c in plan}
    assert rooms == set(range(1, 15))
    # 8 hours of 15-minute consultations with 5-minute gaps: around 24 a room.
    per_room = len([c for c in plan if c.room == 1])
    assert 18 <= per_room <= 30


def test_rooms_do_not_all_start_at_once():
    """A clinic drifts; a server only tested on an even load is not tested."""
    firsts = sorted(c.starts_at for c in a_day() if c.number == 1)
    assert firsts[-1] > firsts[0]


def test_consultations_in_one_room_never_overlap():
    plan = [c for c in a_day() if c.room == 3]
    for earlier, later in zip(plan, plan[1:]):
        ends_at = earlier.starts_at + earlier.minutes * 60 / 60    # speed 60
        assert later.starts_at >= ends_at


def test_a_piece_every_thirty_seconds():
    plan = a_day(minutes_each=15)
    assert all(c.segments == int(c.minutes * 2) for c in plan)
    # Nothing is ever zero-length: a two-minute consultation still sends one.
    assert min(c.segments for c in plan) >= 1


def test_the_day_is_compressed_by_the_speed_factor():
    slow = a_day(speed=1)
    fast = a_day(speed=60)
    assert max(c.starts_at for c in slow) > 8 * 3600 * 0.5
    assert max(c.starts_at for c in fast) < 8 * 3600 / 60


def test_an_empty_plan_is_not_an_error():
    assert a_day(rooms=0) == []
    assert a_day(hours=0) == []


# ============================================================
# The verdict
# ============================================================

def timings(**calls) -> Timings:
    found = Timings()
    for call, values in calls.items():
        for value in values:
            found.record(call, value)
    return found


def test_a_lost_piece_fails_the_run_however_fast_the_replies():
    found = timings(commit=[0.1] * 100)
    found.pieces_sent, found.pieces_accepted = 100, 99
    result = load_test.report(found)
    assert result["pieces_lost"] == 1
    assert result["passed"] is False


def test_a_slow_grant_fails_the_run_however_complete_the_pieces():
    """§9.1: a recorder asks permission and expects an answer in 0.3 s."""
    found = timings(grant=[0.1] * 90 + [0.9] * 10)     # one in ten too slow
    found.pieces_sent = found.pieces_accepted = 20
    result = load_test.report(found)
    assert result["calls"]["grant"]["within_target"] is False
    assert result["passed"] is False


def test_a_good_run_passes():
    found = timings(grant=[0.12] * 30, commit=[0.4] * 300, open=[0.3] * 30,
                    close=[0.2] * 30)
    found.pieces_sent = found.pieces_accepted = 300
    found.consultations = 30
    result = load_test.report(found)
    assert result["passed"] is True
    assert result["calls"]["commit"]["p50"] == 0.4


def test_one_slow_reply_in_a_hundred_does_not_fail_the_day():
    """p95, not the maximum: one slow reply is a network, not a fault."""
    found = timings(commit=[0.2] * 99 + [5.0])
    found.pieces_sent = found.pieces_accepted = 100
    result = load_test.report(found)
    assert result["calls"]["commit"]["max"] == 5.0
    assert result["passed"] is True


def test_a_failure_is_reported_even_when_nothing_was_lost():
    found = timings(commit=[0.2])
    found.pieces_sent = found.pieces_accepted = 1
    found.fail("grant: connection reset")
    result = load_test.report(found)
    assert result["passed"] is False
    assert "connection reset" in result["failures"][0]


@pytest.mark.parametrize("share,expected", [(0.5, 5.0), (0.95, 10.0), (0.0, 1.0)])
def test_percentiles(share, expected):
    values = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0]
    assert load_test.percentile(values, share) == expected


def test_percentile_of_nothing_is_not_an_error():
    assert load_test.percentile([], 0.95) == 0.0


def test_the_written_report_says_which_call_was_too_slow():
    found = timings(grant=[0.9] * 10)
    found.pieces_sent = found.pieces_accepted = 0
    text = load_test.as_text(load_test.report(found))
    assert "OVER" in text and "AT-29: FAILED" in text


# ============================================================
# The messages it sends
# ============================================================

def test_the_audio_it_sends_is_a_real_wav_of_the_right_size():
    import io
    import wave

    data = load_test.wav_bytes(30)
    with wave.open(io.BytesIO(data)) as handle:
        assert handle.getframerate() == 44100
        assert handle.getnchannels() == 1
        assert handle.getnframes() == 30 * 44100
    # 30 seconds at 44.1 kHz, 16-bit mono, plus the header.
    assert len(data) == 30 * 44100 * 2 + 44
