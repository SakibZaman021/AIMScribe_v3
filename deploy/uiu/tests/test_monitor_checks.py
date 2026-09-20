"""
What the watchman decides (`SRS-SRV-08`).

The point of these tests is the phrase "before a doctor would notice": each
check has to fire while there is still time to act, and stay quiet when
nothing is wrong - because a monitor that cries every five minutes is one
nobody reads at two in the morning.

    cd deploy/uiu && python -m pytest -q
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "monitor"))

import checks                                    # noqa: E402
from checks import CRITICAL, Finding, OK, WARNING  # noqa: E402

GB = 1024 ** 3
NOON = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)


# ============================================================
# Disks
# ============================================================

def test_room_left_is_measured_in_gigabytes_not_percent():
    """5% of a 96 TB array is weeks of recording; 5% of the database is minutes."""
    array = checks.disk_space("archive", free_bytes=4800 * GB, total_bytes=96000 * GB,
                              floor_gb=500)
    database = checks.disk_space("database", free_bytes=90 * GB, total_bytes=1800 * GB,
                                 floor_gb=100)
    assert array.status == OK                  # 5% free, and plenty
    assert database.status == WARNING          # 5% free, and not enough


def test_half_the_floor_is_critical():
    finding = checks.disk_space("archive", free_bytes=200 * GB,
                                total_bytes=96000 * GB, floor_gb=500)
    assert finding.status == CRITICAL
    assert "recordings will start failing" in finding.message


def test_a_healthy_volume_says_how_much_is_left():
    finding = checks.disk_space("archive", free_bytes=20000 * GB,
                                total_bytes=96000 * GB, floor_gb=500)
    assert (finding.status, finding.detail["free_gb"]) == (OK, 20000.0)


# ============================================================
# Work that stopped
# ============================================================

def test_a_busy_evening_is_not_a_fault_but_a_stalled_worker_is():
    assert checks.archive_backlog(6, oldest_minutes=8).status == OK
    assert checks.archive_backlog(6, oldest_minutes=45).status == WARNING
    stalled = checks.archive_backlog(40, oldest_minutes=300)
    assert stalled.status == CRITICAL
    assert "stopped" in stalled.message


def test_nothing_waiting_is_quiet():
    assert checks.archive_backlog(0, oldest_minutes=None).status == OK


def test_a_recording_with_no_cloud_copy_is_a_recording_in_one_place():
    assert checks.copies_behind(3, oldest_hours=4).status == OK        # due tonight
    assert checks.copies_behind(3, oldest_hours=30).status == WARNING
    alone = checks.copies_behind(3, oldest_hours=72)
    assert alone.status == CRITICAL
    assert "one place only" in alone.message


def test_a_critical_alert_is_critical_however_few():
    finding = checks.failed_jobs([{"alert_type": "cloud_copy_unverified",
                                   "severity": "critical"}])
    assert finding.status == CRITICAL
    assert "cloud_copy_unverified" in finding.message


def test_ordinary_alerts_are_counted_not_ignored():
    alerts = [{"alert_type": "unmapped_clinic", "severity": "warning"}] * 3
    finding = checks.failed_jobs(alerts)
    assert finding.status == WARNING
    assert finding.detail["by_type"] == {"unmapped_clinic": 3}


def test_a_quiet_hour_is_quiet():
    assert checks.failed_jobs([]).status == OK


# ============================================================
# Recorders
# ============================================================

def devices(*ages_hours, enrolled=4):
    """`enrolled` PCs, the first few last heard from this long ago."""
    made = []
    for n in range(enrolled):
        seen = NOON - timedelta(hours=ages_hours[n]) if n < len(ages_hours) else NOON
        made.append({"device_id": f"dev-{n}", "machine_name": f"ROOM{n}",
                     "last_seen_at": seen, "revoked_at": None})
    return made


def test_a_pc_silent_all_morning_is_raised():
    finding = checks.silent_recorders(devices(6), NOON)
    assert finding.status == WARNING
    assert finding.detail["silent"] == ["ROOM0"]


def test_half_the_fleet_silent_is_critical():
    finding = checks.silent_recorders(devices(6, 7, 8), NOON)
    assert finding.status == CRITICAL


def test_silence_at_night_is_not_a_fault():
    midnight = NOON.replace(hour=2)
    assert checks.silent_recorders(devices(6, 7, 8), midnight).status == OK


def test_a_revoked_pc_is_not_expected_to_speak():
    fleet = devices(6)
    fleet[0]["revoked_at"] = NOON - timedelta(days=30)
    assert checks.silent_recorders(fleet, NOON).status == OK


def test_a_laptop_not_yet_delivered_is_not_silent():
    fleet = devices()
    fleet[0]["last_seen_at"] = None
    assert checks.silent_recorders(fleet, NOON).status == OK


@pytest.mark.parametrize("window,hour,inside", [
    ("08:00-22:00", 12, True),
    ("08:00-22:00", 7, False),
    ("08:00-22:00", 22, True),
    ("22:00-06:00", 2, True),          # a window over midnight
    ("22:00-06:00", 12, False),
    ("nonsense", 3, True),             # a broken window watches all day
])
def test_clinic_hours(window, hour, inside):
    assert checks.in_clinic_hours(NOON.replace(hour=hour), window) is inside


# ============================================================
# The certificate - the one failure that stops every PC at once
# ============================================================

def test_a_certificate_that_has_not_renewed_itself_is_critical():
    assert checks.certificate(NOON + timedelta(days=40), NOON).status == OK
    assert checks.certificate(NOON + timedelta(days=14), NOON).status == WARNING
    assert checks.certificate(NOON + timedelta(days=3), NOON).status == CRITICAL
    assert checks.certificate(NOON - timedelta(days=1), NOON).status == CRITICAL


def test_a_certificate_that_cannot_be_read_is_not_assumed_good():
    finding = checks.certificate(None, NOON)
    assert finding.status == CRITICAL
    assert "gateway may be down" in finding.message


# ============================================================
# Saying it once
# ============================================================

def test_a_problem_is_announced_then_held_back_then_repeated():
    bad = Finding("disk:archive", WARNING, "low", {})
    assert checks.should_send(bad, None, NOON) is True                    # first time
    assert checks.should_send(bad, NOON - timedelta(hours=1), NOON) is False
    assert checks.should_send(bad, NOON - timedelta(hours=7), NOON) is True


def test_the_all_clear_is_sent_once_and_only_after_a_problem():
    good = Finding("disk:archive", OK, "fine", {})
    assert checks.should_send(good, NOON - timedelta(hours=1), NOON) is True
    assert checks.should_send(good, None, NOON) is False


def test_the_summary_carries_the_worst_of_what_was_found():
    findings = [Finding("a", OK, "fine"), Finding("b", WARNING, "hmm"),
                Finding("c", CRITICAL, "bad")]
    report = checks.summary(findings)
    assert report["status"] == CRITICAL
    assert [f["check"] for f in report["findings"]] == ["a", "b", "c"]
    assert checks.worst([Finding("a", OK, "fine")]) == OK
