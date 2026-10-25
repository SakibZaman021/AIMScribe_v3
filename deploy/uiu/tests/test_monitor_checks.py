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


def test_the_uiu_floor_does_not_alarm_for_ever_on_the_droplet():
    """500 GB free cannot exist on a 320 GB disk; the default is capped."""
    floor = checks.effective_floor_gb(None, 500, 320 * GB)
    assert floor == 48                                     # 15% of 320 GB
    healthy = checks.disk_space("archive", free_bytes=250 * GB, total_bytes=320 * GB,
                                floor_gb=floor)
    assert healthy.status == OK


def test_the_uiu_array_keeps_its_absolute_floor():
    assert checks.effective_floor_gb(None, 500, 96000 * GB) == 500
    assert checks.effective_floor_gb("", 500, 96000 * GB) == 500


def test_an_explicit_floor_is_used_unless_it_is_impossible():
    assert checks.effective_floor_gb("40", 500, 320 * GB) == 40
    assert checks.effective_floor_gb("500", 500, 320 * GB) == 48   # copied from UIU
    assert checks.effective_floor_gb("not a number", 100, 320 * GB) == 48


def test_a_droplet_filling_up_still_alarms():
    floor = checks.effective_floor_gb(None, 500, 320 * GB)
    assert checks.disk_space("archive", free_bytes=30 * GB, total_bytes=320 * GB,
                             floor_gb=floor).status == WARNING
    assert checks.disk_space("archive", free_bytes=20 * GB, total_bytes=320 * GB,
                             floor_gb=floor).status == CRITICAL


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


# ---------------------------------------------------------------- schema

def test_a_missing_migration_is_critical():
    f = checks.schema({"status": "degraded", "schema": "missing 015_v3_consultation_parts.sql"})
    assert f.status == CRITICAL and "015_v3_consultation_parts.sql" in f.message


def test_an_up_to_date_schema_is_ok_and_silence_is_a_warning():
    assert checks.schema({"status": "healthy", "schema": "ok"}).status == OK
    assert checks.schema(None).status == WARNING
    assert checks.schema({"status": "healthy"}).status == WARNING   # an older server


# ---------------------------------------------------------------- recovery to a moment

def test_wal_that_stops_leaving_the_server_is_critical():
    fresh = NOON - timedelta(minutes=1)
    ok = checks.wal_shipping({"pending": "0", "stuck": "0", "error": ""}, fresh, fresh, NOON)
    assert ok.status == OK
    stuck = checks.wal_shipping({"pending": "9", "stuck": "4", "error": "could not send x"},
                                fresh, NOON - timedelta(hours=2), NOON)
    assert stuck.status == CRITICAL and "4 WAL" in stuck.message and "could not send" in stuck.message
    retrying = checks.wal_shipping({"pending": "1", "stuck": "0", "error": "could not send x"},
                                   fresh, fresh, NOON)
    assert retrying.status == WARNING


def test_a_shipper_that_stopped_reporting_is_critical_and_one_never_started_is_raised():
    old = NOON - timedelta(minutes=40)
    assert checks.wal_shipping({"pending": "0", "stuck": "0"}, old, old, NOON).status == CRITICAL
    assert checks.wal_shipping(None, None, None, NOON).status == WARNING


def test_postgres_that_cannot_archive_is_critical_until_it_can_again():
    failing = {"last_failed_time": NOON - timedelta(minutes=5), "last_failed_wal": "0001",
               "last_archived_time": NOON - timedelta(hours=2), "failed_count": 7}
    assert checks.wal_archiver(failing, NOON).status == CRITICAL
    recovered = dict(failing, last_archived_time=NOON - timedelta(minutes=1))
    assert checks.wal_archiver(recovered, NOON).status == OK
    long_ago = dict(failing, last_failed_time=NOON - timedelta(days=2),
                    last_archived_time=NOON - timedelta(days=3))
    assert checks.wal_archiver(long_ago, NOON).status == OK          # an old blip
    assert checks.wal_archiver(None, NOON).status == WARNING


def test_no_base_backup_means_no_recovery_to_a_moment():
    assert checks.base_backup(None, NOON).status == CRITICAL
    assert checks.base_backup(NOON - timedelta(hours=40), NOON).status == CRITICAL
    assert checks.base_backup(NOON - timedelta(hours=3), NOON).status == OK


def test_a_failed_drill_is_critical_a_missed_one_a_warning_and_none_yet_is_quiet():
    at = NOON - timedelta(days=3)
    assert checks.restore_drill({"status": "ok", "message": "recovered"}, at, NOON).status == OK
    failed = checks.restore_drill({"status": "failed", "message": " pitr.sessions:3/9"}, at, NOON)
    assert failed.status == CRITICAL and "sessions" in failed.message
    assert checks.restore_drill({"status": "ok"}, NOON - timedelta(days=40), NOON).status == WARNING
    assert checks.restore_drill(None, None, NOON).status == OK       # first runs tonight


# ---------------------------------------------------------------- email

def test_alerts_go_by_email_once_per_pass_and_a_weekly_report(tmp_path, monkeypatch):
    import asyncio
    import monitor
    sent = []

    class Box(monitor.Mailer):
        def send(self, subject, text):
            sent.append((subject, text))
            return True

    monkeypatch.setenv("AIMS_ALERT_EMAIL", "lab@example.org")
    monkeypatch.setenv("AIMS_RESEND_API_KEY", "re_test")
    monkeypatch.setattr(monitor, "STATE", tmp_path)
    monkeypatch.setattr(monitor, "disk_findings", lambda: [
        Finding("disk:archive", CRITICAL, "archive has 3 GB free"),
        Finding("disk:db", WARNING, "db has 40 GB free")])

    async def no_db(dsn, now):
        return []
    monkeypatch.setattr(monitor, "database_findings", no_db)
    monkeypatch.setattr(monitor, "certificate_finding", lambda h, n: Finding("tls", OK, "fine"))
    monkeypatch.setattr(monitor, "backup_findings", lambda n: [])
    monkeypatch.setattr(monitor, "schema_finding", lambda u: Finding("api:schema", OK, "ok"))

    asyncio.run(monitor.one_pass("dsn", "aims.example", "", Box()))
    problems = [s for s in sent if not s[0].startswith("weekly")]
    assert len(problems) == 1                       # both findings, one email
    subject, text = problems[0]
    assert subject.startswith("CRITICAL") and "archive has 3 GB" in subject
    assert "db has 40 GB" in text and "aims.example" in text
    assert any(s[0].startswith("weekly") for s in sent)

    sent.clear()
    asyncio.run(monitor.one_pass("dsn", "aims.example", "", Box()))
    assert sent == []                               # held back, and the weekly is done


def test_without_a_key_no_email_is_attempted(monkeypatch):
    import monitor
    monkeypatch.delenv("AIMS_RESEND_API_KEY", raising=False)
    monkeypatch.setenv("AIMS_ALERT_EMAIL", "lab@example.org")
    assert monitor.Mailer().ready is False
    assert monitor.Mailer().send("x", "y") is False
