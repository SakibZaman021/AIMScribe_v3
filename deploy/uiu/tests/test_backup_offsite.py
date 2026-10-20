"""
Every encrypted backup also leaves the server (failure review A3).

Runs the real backup.sh with a stand-in rclone that keeps "the bucket" in a
folder, so the upload, the size check, the catch-up after a failed night and
the refusal to send unencrypted backups are all exercised without a network.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent.parent
SCRIPT = HERE / "backup" / "backup.sh"
SH = shutil.which("sh")

FAKE_RCLONE = r'''#!/bin/sh
# Stand-in for rclone: "offsite:<bucket>/<prefix>/<name>" lives in $FAKE_REMOTE/<name>.
echo "$*" >> "$FAKE_REMOTE.calls"
[ -f "$FAKE_REMOTE.down" ] && exit 1
last() { for a in "$@"; do x="$a"; done; echo "$x"; }
case "$1" in
  copyto)
    from=""; to=""; skip=0
    for a in "$@"; do
      if [ "$skip" = 1 ]; then skip=0; continue; fi
      case "$a" in copyto) ;; --retries|--low-level-retries) skip=1 ;; -*) ;;
        *) if [ -z "$from" ]; then from="$a"; else to="$a"; fi ;; esac
    done
    case "$from" in
      offsite:*) cp "$FAKE_REMOTE/${from##*/}" "$to" ;;
      *) if [ -f "$FAKE_REMOTE.short" ]; then head -c 10 "$from" > "$FAKE_REMOTE/${to##*/}"
         else cp "$from" "$FAKE_REMOTE/${to##*/}"; fi ;;
    esac ;;
  size)
    f="$FAKE_REMOTE/$(last "$@" | sed 's#.*/##')"
    [ -f "$f" ] || exit 3
    echo "{\"count\":1,\"bytes\":$(wc -c < "$f" | tr -d ' ')}" ;;
  delete) ;;
  lsf|lsl) ls "$FAKE_REMOTE" ;;
  *) exit 2 ;;
esac
'''

pytestmark = pytest.mark.skipif(SH is None, reason="needs a POSIX sh")


@pytest.fixture
def box(tmp_path):
    bin_dir, remote, backups = tmp_path / "bin", tmp_path / "remote", tmp_path / "backups"
    for d in (bin_dir, remote, backups):
        d.mkdir()
    rclone = bin_dir / "rclone"
    rclone.write_text(FAKE_RCLONE, encoding="utf-8", newline="\n")
    rclone.chmod(0o755)
    env = {**os.environ,
           "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
           "FAKE_REMOTE": str(remote), "AIMS_BACKUP_DIR": str(backups),
           "AIMS_BACKUP_KEY": "k" * 32, "AIMS_OFFSITE_BUCKET": "aimscribe-backups",
           "AIMS_OFFSITE_RETRY_SECONDS": "0", "AIMS_OFFSITE_TRIES": "2"}

    def run(*args, **overrides):
        e = {**env, **overrides}
        e = {k: v for k, v in e.items() if v is not None}
        return subprocess.run([SH, str(SCRIPT), *args], env=e, capture_output=True,
                              text=True, timeout=60)

    def backup(name, size=8192):
        (backups / name).write_bytes(os.urandom(size))

    return run, backup, backups, remote


def test_encrypted_backups_are_copied_checked_and_marked(box):
    run, backup, backups, remote = box
    backup("aims_recordings_20261016_0230.sql.gz.enc")
    backup("aims_clinical_20261016_0230.sql.gz.enc", 5000)
    done = run("--offsite")
    assert done.returncode == 0, done.stdout + done.stderr
    assert sorted(p.name for p in remote.iterdir()) == [
        "aims_clinical_20261016_0230.sql.gz.enc", "aims_clinical_20261016_0230.sql.gz.enc.sha256",
        "aims_recordings_20261016_0230.sql.gz.enc",
        "aims_recordings_20261016_0230.sql.gz.enc.sha256"]
    assert (backups / "aims_clinical_20261016_0230.sql.gz.enc.offsite").exists()
    assert (backups / ".last_offsite").exists()
    assert "size checked" in done.stdout

    before = (remote.parent / "remote.calls").read_text().count("copyto")
    assert run("--offsite").returncode == 0                   # nothing new to send
    assert (remote.parent / "remote.calls").read_text().count("copyto") == before


def test_a_night_without_the_bucket_is_caught_up_next_time(box):
    run, backup, backups, remote = box
    backup("aims_recordings_20261015_0230.sql.gz.enc")
    (remote.parent / "remote.down").write_text("")
    failed = run("--offsite")
    assert failed.returncode == 1 and "not off-site yet" in failed.stdout
    assert not (backups / ".last_offsite").exists()
    assert not (backups / "aims_recordings_20261015_0230.sql.gz.enc.offsite").exists()

    (remote.parent / "remote.down").unlink()
    backup("aims_recordings_20261016_0230.sql.gz.enc")
    assert run("--offsite").returncode == 0
    sent = [p for p in remote.iterdir() if p.name.endswith(".enc")]
    assert len(sent) == 2                                     # both nights now off-site


def test_a_copy_of_the_wrong_size_is_not_trusted(box):
    run, backup, backups, remote = box
    backup("aims_recordings_20261016_0230.sql.gz.enc")
    (remote.parent / "remote.short").write_text("")
    done = run("--offsite")
    assert done.returncode == 1 and "expected 8192 bytes" in done.stdout
    assert not (backups / "aims_recordings_20261016_0230.sql.gz.enc.offsite").exists()


def test_unencrypted_backups_never_leave_the_server(box):
    run, backup, backups, remote = box
    backup("aims_recordings_20261016_0230.sql.gz.enc")
    done = run("--offsite", AIMS_BACKUP_KEY=None)
    assert done.returncode == 1 and "not encrypted" in done.stdout
    assert list(remote.iterdir()) == []


def test_without_a_bucket_it_says_the_backups_are_only_here(box):
    run, backup, backups, remote = box
    backup("aims_recordings_20261016_0230.sql.gz.enc")
    done = run("--offsite", AIMS_OFFSITE_BUCKET=None)
    assert done.returncode == 0 and "exist only on this server" in done.stdout
    assert list(remote.iterdir()) == []


def test_fetch_brings_back_the_newest(box):
    run, backup, backups, remote = box
    for day in ("20261014", "20261016", "20261015"):
        (remote / f"aims_clinical_{day}_0230.sql.gz.enc").write_bytes(b"x" * 100)
    got = run("fetch", "latest", "aims_clinical")
    assert got.returncode == 0, got.stdout + got.stderr
    assert got.stdout.strip().endswith("aims_clinical_20261016_0230.sql.gz.enc")
    assert (backups / "aims_clinical_20261016_0230.sql.gz.enc").exists()


def test_fetch_checks_the_checksum_and_refuses_a_changed_backup(box):
    run, backup, backups, remote = box
    backup("aims_clinical_20261016_0230.sql.gz.enc")
    assert run("--offsite").returncode == 0
    for p in list(backups.iterdir()):
        p.unlink()
    good = run("fetch", "latest", "aims_clinical")
    assert good.returncode == 0 and "checksum ok" in good.stderr

    for p in list(backups.iterdir()):
        p.unlink()
    damaged = remote / "aims_clinical_20261016_0230.sql.gz.enc"
    damaged.write_bytes(b"not what was backed up" + damaged.read_bytes()[22:])
    bad = run("fetch", "latest", "aims_clinical")
    assert bad.returncode == 1 and "REFUSED" in bad.stderr
    assert not (backups / "aims_clinical_20261016_0230.sql.gz.enc").exists()


def test_fetch_of_a_backup_without_a_checksum_warns(box):
    run, backup, backups, remote = box
    (remote / "aims_clinical_20261001_0230.sql.gz.enc").write_bytes(b"x" * 100)
    got = run("fetch", "latest", "aims_clinical")
    assert got.returncode == 0 and "no checksum" in got.stderr


def test_fetch_takes_names_not_paths(box):
    run, backup, backups, remote = box
    assert run("fetch", "../../etc/passwd").returncode == 2


FAKE_PG_DUMP = """#!/bin/sh
[ -n "$FAKE_PG_DUMP_FAIL" ] && { echo partial; exit 1; }
echo "INSERT INTO patients VALUES ('Rahima Begum');"
head -c 30000 /dev/urandom | od -An -tx1          # enough that it does not shrink to nothing
"""


@pytest.fixture
def dumping(box, tmp_path):
    run, backup, backups, remote = box
    fake = tmp_path / "bin" / "pg_dump"
    fake.write_text(FAKE_PG_DUMP, encoding="utf-8", newline="\n")
    fake.chmod(0o755)
    return run, backups


@pytest.mark.skipif(shutil.which("openssl") is None, reason="needs openssl")
def test_a_backup_is_only_ever_written_encrypted_with_its_checksum(dumping):
    run, backups = dumping
    done = run("--now", AIMS_OFFSITE_BUCKET=None)
    assert done.returncode == 0, done.stdout + done.stderr
    names = sorted(p.name for p in backups.iterdir() if p.name.startswith("aims_"))
    assert len(names) == 4
    assert all(n.endswith(".sql.gz.enc") or n.endswith(".sql.gz.enc.sha256") for n in names)
    for n in names:
        assert b"Rahima" not in (backups / n).read_bytes()      # never in the clear
    enc = next(backups / n for n in names if n.endswith(".enc"))
    check = subprocess.run(["sha256sum", "-c", enc.name + ".sha256"], cwd=backups,
                           capture_output=True, text=True)
    assert check.returncode == 0, check.stdout + check.stderr


def test_no_key_means_no_backup_at_all(dumping):
    run, backups = dumping
    done = run("--now", AIMS_BACKUP_KEY=None, AIMS_OFFSITE_BUCKET=None)
    assert done.returncode == 1 and "never written" in done.stdout
    assert not [p for p in backups.iterdir() if p.name.startswith("aims_")]


@pytest.mark.skipif(shutil.which("openssl") is None, reason="needs openssl")
def test_a_dump_that_fails_half_way_leaves_nothing_behind(dumping):
    run, backups = dumping
    done = run("--now", FAKE_PG_DUMP_FAIL="1", AIMS_OFFSITE_BUCKET=None)
    assert done.returncode == 1 and "FAILED to dump" in done.stdout
    assert not [p for p in backups.iterdir() if p.name.startswith("aims_")]


RESTORE = HERE / "backup" / "restore.sh"


def test_restore_refuses_a_backup_that_does_not_match_its_checksum(tmp_path):
    f = tmp_path / "aims_clinical_20261016_0230.sql.gz.enc"
    f.write_bytes(b"x" * 5000)
    (tmp_path / (f.name + ".sha256")).write_text("0" * 64 + "  " + f.name + "\n")
    done = subprocess.run([SH, str(RESTORE), str(f), "aims_restore_test"],
                          capture_output=True, text=True, timeout=60)
    assert done.returncode == 1 and "REFUSED" in done.stdout


def test_restore_refuses_a_target_that_is_not_a_plain_name(tmp_path):
    f = tmp_path / "x.sql.gz.enc"
    f.write_bytes(b"x")
    done = subprocess.run([SH, str(RESTORE), str(f), "x; DROP DATABASE aims_clinical"],
                          capture_output=True, text=True, timeout=60)
    assert done.returncode == 2


# ---------------------------------------------------------------- the monitor

import sys                                                      # noqa: E402
sys.path.insert(0, str(HERE / "monitor"))
import checks                                                   # noqa: E402

NOW = datetime(2026, 10, 17, 9, 0, tzinfo=timezone.utc)


def statuses(findings):
    return {f.check: f.status for f in findings}


def test_monitor_fresh_backups_on_and_off_the_server_are_ok():
    found = checks.backups(NOW - timedelta(hours=7), NOW - timedelta(hours=7), True, NOW)
    assert statuses(found) == {"backup:local": checks.OK, "backup:offsite": checks.OK}


def test_monitor_backups_that_did_not_leave_are_critical():
    found = checks.backups(NOW - timedelta(hours=7), NOW - timedelta(hours=50), True, NOW)
    assert statuses(found)["backup:offsite"] == checks.CRITICAL
    assert statuses(checks.backups(NOW, None, True, NOW))["backup:offsite"] == checks.CRITICAL


def test_monitor_no_bucket_is_a_warning_and_no_backup_is_critical():
    found = statuses(checks.backups(None, None, False, NOW))
    assert found == {"backup:local": checks.CRITICAL, "backup:offsite": checks.WARNING}
