"""
The recorder's own updates (core/updater.py).

The updater runs as SYSTEM on every clinical PC, so each of its checks is a
security boundary, and each is tested here on its own: the signed notice
(good, tampered, the wrong key, the wrong domain, another PC's, replayed),
the file's SHA-256 and its Authenticode publisher, waiting for the
consultation to end, three tries and no more, and the agent's half - passing
on the server's word and reporting back.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from core import crypto, trusted_origins, updater
from core.updater import (MAX_ATTEMPTS, NoticeRefused, UpdateFailed, Updater, agent_busy,
                          check_publisher, check_sha256, verify_notice, wait_until_idle)

NOW = datetime(2026, 11, 9, 8, 0, tzinfo=timezone.utc)
SETUP = b"MZ pretend this is AIMScribeSetup.exe 3.8.4"
SETUP_SHA = __import__("hashlib").sha256(SETUP).hexdigest()
GOOD_SIGNATURE = {"status": "Valid", "subject": "CN=AIMS LAB, O=AIMS LAB, L=Dhaka, C=BD",
                  "timestamped": True}


def payload(**over):
    p = {"scope": "recorder_update", "device_id": "DEV1", "release_id": 7,
         "version": "3.8.4", "url": "https://github.com/x/releases/download/v/AIMScribeSetup.exe",
         "sha256": SETUP_SHA, "rollback": False, "issued_at": "2026-11-09T07:59:00.000Z"}
    p.update(over)
    return p


def sign(key, body, domain=updater.DOMAIN):
    return {"payload": body,
            "signature": key.sign(crypto.digest(domain, crypto.canonical_json(body))).hex()}


@pytest.fixture
def key():
    return Ed25519PrivateKey.generate()


def check(bundle, key, **kw):
    args = dict(device_id="DEV1", installed_version="3.8.3", highest_release_id=0, now=NOW)
    args.update(kw)
    return verify_notice(bundle, key.public_key(), **args)


# ============================================================ the signed notice

def test_a_good_notice_is_taken(key):
    notice = check(sign(key, payload()), key)
    assert (notice.version, notice.release_id, notice.sha256) == ("3.8.4", 7, SETUP_SHA)
    assert not notice.rollback


def test_a_tampered_notice_is_refused(key):
    bundle = sign(key, payload())
    for field, value in (("version", "9.9.9"), ("sha256", "0" * 64),
                         ("url", "https://evil.example/AIMScribeSetup.exe"),
                         ("rollback", True), ("device_id", "DEV2"), ("release_id", 99)):
        forged = {"payload": {**bundle["payload"], field: value}, "signature": bundle["signature"]}
        with pytest.raises(NoticeRefused, match="signature"):
            check(forged, key)
    with pytest.raises(NoticeRefused, match="signature"):
        check({**bundle, "signature": "00" * 64}, key)
    with pytest.raises(NoticeRefused):
        check({**bundle, "signature": "not hex"}, key)


def test_a_notice_signed_by_another_key_is_refused(key):
    with pytest.raises(NoticeRefused, match="signature"):
        check(sign(Ed25519PrivateKey.generate(), payload()), key)


def test_the_same_key_under_another_domain_is_refused(key):
    """A trusted-origins list or a purge receipt signed with this very key is never a notice."""
    with pytest.raises(NoticeRefused, match="signature"):
        check(sign(key, payload(), domain=trusted_origins.DOMAIN), key)
    with pytest.raises(NoticeRefused, match="signature"):
        check(sign(key, payload(), domain=crypto.RECEIPT_DOMAIN), key)
    # And the other way round: a notice is not a list of CMED's addresses.
    as_list = sign(key, {"scope": "trusted_origins", "origins": ["https://evil.example"],
                         "version": 10 ** 15, "issued_at": "2026-11-09T07:59:00Z"})
    trusted_origins.configure(None, key.public_key())
    try:
        assert not trusted_origins.apply(as_list, persist=False)
    finally:
        trusted_origins.configure(None, None)
    # Signed under the update domain but saying it is something else.
    with pytest.raises(NoticeRefused, match="not as a recorder update"):
        check(sign(key, payload(scope="trusted_origins")), key)


def test_a_notice_for_another_pc_is_refused(key):
    with pytest.raises(NoticeRefused, match="another PC"):
        check(sign(key, payload(device_id="DEV2")), key)
    with pytest.raises(NoticeRefused, match="another PC"):
        check(sign(key, payload()), key, device_id="")          # this PC has not joined


def test_an_older_version_replayed_is_refused(key):
    with pytest.raises(NoticeRefused, match="not newer"):
        check(sign(key, payload(version="3.8.2")), key)
    with pytest.raises(NoticeRefused, match="not newer"):
        check(sign(key, payload(version="3.8.3")), key)
    # 3.10 is newer than 3.9: versions compare as numbers.
    assert check(sign(key, payload(version="3.10.0")), key, installed_version="3.9.9")


def test_an_older_release_replayed_is_refused_after_a_newer_one(key):
    old = sign(key, payload(release_id=6, version="3.8.5"))
    with pytest.raises(NoticeRefused, match="replayed"):
        check(old, key, highest_release_id=7)
    assert check(sign(key, payload(release_id=7)), key, highest_release_id=7)


def test_a_stale_notice_is_refused(key):
    with pytest.raises(NoticeRefused, match="replayed"):
        check(sign(key, payload(issued_at="2026-10-30T08:00:00Z")), key)
    with pytest.raises(NoticeRefused, match="replayed"):
        check(sign(key, payload(issued_at="2026-11-12T08:00:00Z")), key)
    with pytest.raises(NoticeRefused):
        check(sign(key, payload(issued_at="yesterday")), key)


def test_a_rollback_is_taken_only_when_the_server_signed_it(key):
    notice = check(sign(key, payload(version="3.8.2", rollback=True, release_id=8)), key,
                   installed_version="3.8.4")
    assert notice.rollback and notice.version == "3.8.2"
    with pytest.raises(NoticeRefused, match="already installed"):
        check(sign(key, payload(version="3.8.4", rollback=True)), key, installed_version="3.8.4")


def test_a_notice_must_say_everything_plainly(key):
    for bad in (dict(url="http://github.com/AIMScribeSetup.exe"), dict(sha256="ABC"),
                dict(version="3.8"), dict(release_id=0), dict(release_id=True),
                dict(rollback="no")):
        with pytest.raises(NoticeRefused):
            check(sign(key, payload(**bad)), key)
    for junk in (None, [], "x", {"payload": "x", "signature": "00"}):
        with pytest.raises(NoticeRefused):
            check(junk, key)
    with pytest.raises(NoticeRefused, match="no server key"):
        verify_notice(sign(key, payload()), None, device_id="DEV1", installed_version="3.8.3",
                      now=NOW)


# ============================================================ the file

def test_the_sha256_must_match(tmp_path):
    setup = tmp_path / "AIMScribeSetup.exe"
    setup.write_bytes(SETUP)
    check_sha256(setup, SETUP_SHA)
    check_sha256(setup, SETUP_SHA.upper())
    setup.write_bytes(SETUP + b"!")
    with pytest.raises(UpdateFailed, match="SHA-256"):
        check_sha256(setup, SETUP_SHA)


def test_the_publisher_must_be_aims_lab_exactly():
    check_publisher(GOOD_SIGNATURE, "AIMS LAB")
    check_publisher({"status": "Valid", "subject": 'CN=Someone, O="AIMS LAB"'}, "aims lab")
    check_publisher({"status": "Valid", "subject":
                     'CN="AIMS LAB, United International University", O=UIU'},
                    "AIMS LAB, United International University")
    refused = [
        {"status": "NotSigned", "subject": ""},
        {"status": "HashMismatch", "subject": GOOD_SIGNATURE["subject"]},   # altered after signing
        {"status": "UnknownError", "subject": GOOD_SIGNATURE["subject"]},   # untrusted root
        {"status": "Valid", "subject": "CN=AIMS LAB Evil, O=Evil Ltd"},
        {"status": "Valid", "subject": "CN=Evil, O=Not AIMS LAB"},
        {"status": "Valid", "subject": "CN=Evil, OU=AIMS LAB"},
        {"status": "Valid", "subject": ""},
        {},
    ]
    for signature in refused:
        with pytest.raises(UpdateFailed):
            check_publisher(signature, "AIMS LAB")
    with pytest.raises(UpdateFailed):
        check_publisher(GOOD_SIGNATURE, "")


def test_subject_names_are_read_with_quotes():
    names = updater.subject_names('CN=AIMS LAB, O="AIMS LAB, UIU", L=Dhaka, C=BD')
    assert names == {"CN": "AIMS LAB", "O": "AIMS LAB, UIU", "L": "Dhaka", "C": "BD"}


# ============================================================ the consultation

def status(state_dir, state, *, ago=0.0, session_id=None, now=1_000_000.0):
    (state_dir / updater.AGENT_STATUS_FILE).write_text(json.dumps(
        {"state": state, "session_id": session_id, "at": now - ago}), encoding="utf-8")


def test_a_recording_agent_is_busy_and_a_silent_one_is_not(tmp_path):
    now = 1_000_000.0
    assert not agent_busy(tmp_path, now=now)                   # no agent has ever run
    status(tmp_path, "recording", session_id="S1")
    assert agent_busy(tmp_path, now=now)
    status(tmp_path, "paused", session_id="S1")
    assert agent_busy(tmp_path, now=now)
    status(tmp_path, "idle")
    assert not agent_busy(tmp_path, now=now)
    # An agent that said "recording" ten minutes ago and nothing since is gone.
    status(tmp_path, "recording", ago=600, session_id="S1")
    assert not agent_busy(tmp_path, now=now)


def test_a_consultation_cmed_asked_for_counts_as_busy(tmp_path):
    status(tmp_path, "idle")
    live = tmp_path / updater.LIVE_FILE
    live.write_text("{}", encoding="utf-8")
    assert agent_busy(tmp_path, now=live.stat().st_mtime + 30)
    assert not agent_busy(tmp_path, now=live.stat().st_mtime + 600)


class Clock:
    def __init__(self, start=1_000_000.0):
        self.now = start
        self.slept = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


def test_it_waits_until_the_consultation_ends(tmp_path):
    clock = Clock()
    status(tmp_path, "recording", session_id="S1", now=clock.now)

    def sleep(seconds):
        clock.sleep(seconds)
        if len(clock.slept) == 3:                              # the doctor stops
            status(tmp_path, "idle", now=clock.now)
        elif len(clock.slept) > 3:
            status(tmp_path, "idle", now=clock.now)

    assert wait_until_idle(tmp_path, clock=clock, sleep=sleep, max_wait=3600, poll=30)
    # Two quiet looks in a row, not one: a consultation starting is not cut off.
    assert len(clock.slept) == 4


def test_it_gives_up_waiting_and_tries_later(tmp_path):
    clock = Clock()

    def sleep(seconds):
        clock.sleep(seconds)
        status(tmp_path, "recording", session_id="S1", now=clock.now)

    status(tmp_path, "recording", session_id="S1", now=clock.now)
    assert not wait_until_idle(tmp_path, clock=clock, sleep=sleep, max_wait=300, poll=30)
    assert sum(clock.slept) <= 300


# ============================================================ one run of the task

class Rig:
    """An updater with the network, Windows and the clock stood in for."""

    def __init__(self, tmp_path, key, *, installed="3.8.3", body=SETUP,
                 signature=GOOD_SIGNATURE, busy=False):
        self.state = tmp_path / "state"
        self.work = tmp_path / "private"
        self.state.mkdir(exist_ok=True)
        (self.state / "device.json").write_text(json.dumps({"device_id": "DEV1"}),
                                                encoding="utf-8")
        self.key, self.body, self.signature = key, body, signature
        self.fetched, self.launched = [], []
        self.clock = Clock(NOW.timestamp())
        self.busy = busy
        self.installed = installed
        self.fail_fetch = None

    def updater(self, installed=None):
        if not self.busy:
            status(self.state, "idle", now=self.clock.now)
        return Updater(state_dir=self.state, work_dir=self.work, public_key=self.key.public_key(),
                       installed_version=installed or self.installed, fetch=self.fetch,
                       signature_of=lambda path: self.signature, launch=self.launch,
                       clock=self.clock, sleep=self.sleep, idle_wait=120)

    def sleep(self, seconds):
        self.clock.sleep(seconds)
        status(self.state, "recording" if self.busy else "idle",
               session_id="S1" if self.busy else None, now=self.clock.now)

    def fetch(self, url, target):
        self.fetched.append(url)
        if self.fail_fetch:
            raise self.fail_fetch
        Path(target).write_bytes(self.body)

    def launch(self, setup, log, exit_file):
        self.launched.append(Path(setup))
        assert Path(setup).read_bytes() == self.body           # the file checked is the one run

    def offer(self, **over):
        (self.state / updater.NOTICE_FILE).write_text(
            json.dumps(sign(self.key, payload(**over))), encoding="utf-8")

    def setup_ended(self, code):
        (self.work / "setup.exit").write_text(f"{code} \r\n", encoding="ascii")

    def ledger(self):
        return json.loads((self.work / "ledger.json").read_text(encoding="utf-8"))

    def report(self):
        return updater.last_report(self.state)


def test_nothing_offered_nothing_done(tmp_path, key):
    rig = Rig(tmp_path, key)
    assert rig.updater().run_once()["state"] == "idle"
    assert rig.fetched == rig.launched == []


def test_a_good_update_is_installed_and_reported(tmp_path, key):
    rig = Rig(tmp_path, key)
    rig.offer()
    result = rig.updater().run_once()
    assert result["state"] == "installing" and result["version"] == "3.8.4"
    assert len(rig.launched) == 1 and rig.ledger()["attempts"] == {f"7:3.8.4:{SETUP_SHA}": 1}
    # Setup replaced the program; the next run is the new version and says so.
    rig.setup_ended(0)
    done = rig.updater(installed="3.8.4").run_once()
    assert done["state"] == "installed" and done["version"] == "3.8.4"
    assert rig.ledger()["pending"] is None and rig.ledger()["attempts"] == {}
    assert not (rig.state / updater.NOTICE_FILE).exists()
    assert not rig.launched[0].exists()                         # the download is cleaned up
    assert rig.updater(installed="3.8.4").run_once()["state"] == "installed"


def test_while_setup_runs_nothing_else_starts(tmp_path, key):
    rig = Rig(tmp_path, key)
    rig.offer()
    rig.updater().run_once()
    rig.clock.now += 120
    assert rig.updater().run_once()["state"] == "installing"
    assert len(rig.launched) == 1


def test_a_tampered_notice_never_reaches_the_network(tmp_path, key):
    rig = Rig(tmp_path, key)
    bundle = sign(key, payload())
    bundle["payload"]["url"] = "https://evil.example/AIMScribeSetup.exe"
    (rig.state / updater.NOTICE_FILE).write_text(json.dumps(bundle), encoding="utf-8")
    result = rig.updater().run_once()
    assert result["state"] == "refused" and "signature" in result["reason"]
    assert rig.fetched == rig.launched == []


def test_a_wrong_download_is_never_run_and_is_given_up_after_three(tmp_path, key):
    rig = Rig(tmp_path, key, body=b"MZ something else")
    rig.offer()
    states = [rig.updater().run_once() for _ in range(MAX_ATTEMPTS + 2)]
    assert [s["state"] for s in states] == ["failed", "failed", "gave_up", "gave_up", "gave_up"]
    assert "SHA-256" in states[0]["reason"]
    assert len(rig.fetched) == MAX_ATTEMPTS and rig.launched == []
    assert not any(rig.work.glob("AIMScribeSetup-*.exe"))       # nothing left to run


@pytest.mark.parametrize("signature", [
    {"status": "NotSigned", "subject": ""},
    {"status": "Valid", "subject": "CN=Someone Else, O=Someone Else"},
])
def test_a_download_not_signed_by_aims_lab_is_never_run(tmp_path, key, signature):
    rig = Rig(tmp_path, key, signature=signature)
    rig.offer()
    result = rig.updater().run_once()
    assert result["state"] == "failed" and result["attempts"] == 1
    assert rig.launched == []


def test_a_failed_setup_is_reported_and_tried_three_times_in_all(tmp_path, key):
    rig = Rig(tmp_path, key)
    rig.offer()
    seen = []
    for _ in range(MAX_ATTEMPTS):
        assert rig.updater().run_once()["state"] == "installing"
        (rig.work / "setup.log").write_text("...\nError: the TPM could not be opened\n",
                                            encoding="utf-8")
        rig.setup_ended(8)
        seen.append(rig.updater().run_once())          # still 3.8.3: it failed
    assert [s["state"] for s in seen] == ["failed", "failed", "gave_up"]
    assert "exit code 8" in seen[0]["reason"] and "TPM" in seen[0]["reason"]
    assert rig.updater().run_once()["state"] == "gave_up"
    assert len(rig.launched) == MAX_ATTEMPTS
    # A new release - a new number - is tried afresh.
    rig.offer(release_id=8, version="3.8.5")
    assert rig.updater().run_once()["state"] == "installing"


def test_a_setup_that_never_finishes_counts_as_failed(tmp_path, key):
    rig = Rig(tmp_path, key)
    rig.offer()
    rig.updater().run_once()
    rig.clock.now += 31 * 60
    result = rig.updater().run_once()
    assert result["state"] == "failed" and "30 minutes" in result["reason"]


def test_it_waits_for_the_consultation_before_downloading(tmp_path, key):
    rig = Rig(tmp_path, key, busy=True)
    status(rig.state, "recording", session_id="S1", now=rig.clock.now)
    rig.offer()
    result = rig.updater().run_once()
    assert result["state"] == "waiting" and "consultation" in result["reason"]
    assert rig.fetched == rig.launched == []
    assert rig.ledger()["attempts"] == {}                   # waiting is not a failure
    rig.busy = False
    assert rig.updater().run_once()["state"] == "installing"


def test_a_consultation_that_starts_during_the_download_is_not_cut_off(tmp_path, key):
    rig = Rig(tmp_path, key)
    rig.offer()

    def fetch(url, target):
        Path(target).write_bytes(SETUP)
        status(rig.state, "recording", session_id="S9", now=rig.clock.now)

    rig.fetch = fetch
    result = rig.updater().run_once()
    assert result["state"] == "waiting" and rig.launched == []


def test_a_network_failure_is_not_counted_against_the_version(tmp_path, key):
    rig = Rig(tmp_path, key)
    rig.offer()
    rig.fail_fetch = OSError("no route to host")
    for _ in range(MAX_ATTEMPTS + 1):
        result = rig.updater().run_once()
        assert result["state"] == "failed" and "download failed" in result["reason"]
    assert rig.ledger()["attempts"] == {}
    rig.fail_fetch = None
    assert rig.updater().run_once()["state"] == "installing"


def test_an_old_notice_put_back_after_a_newer_one_is_refused(tmp_path, key):
    rig = Rig(tmp_path, key)
    rig.offer(release_id=9, version="3.8.6")
    rig.updater().run_once()
    rig.setup_ended(0)
    rig.updater(installed="3.8.6").run_once()
    rig.installed = "3.8.6"
    # Someone on the PC writes back a genuine older notice (a rollback even).
    rig.offer(release_id=7, version="3.8.4", rollback=True)
    result = rig.updater().run_once()
    assert result["state"] == "refused" and "replayed" in result["reason"]
    assert len(rig.launched) == 1


def test_a_signed_rollback_installs_an_older_version(tmp_path, key):
    rig = Rig(tmp_path, key, installed="3.8.5")
    rig.offer(release_id=12, version="3.8.4", rollback=True)
    assert rig.updater().run_once()["state"] == "installing"
    rig.setup_ended(0)
    assert rig.updater(installed="3.8.4").run_once()["state"] == "installed"


# ============================================================ the agent's half

def test_the_agent_passes_on_the_notice_and_withdraws_it(tmp_path, key):
    bundle = sign(key, payload())
    updater.keep_notice(tmp_path, bundle)
    assert json.loads((tmp_path / updater.NOTICE_FILE).read_text(encoding="utf-8")) == bundle
    updater.keep_notice(tmp_path, None)                       # paused, or out of the stage
    assert not (tmp_path / updater.NOTICE_FILE).exists()
    updater.keep_notice(None, bundle)                          # no state folder: nothing


def test_the_agent_says_when_it_records(tmp_path):
    updater.note_agent_state(tmp_path, "recording", "S1")
    assert agent_busy(tmp_path)
    updater.note_agent_state(tmp_path, "idle", None)
    assert not agent_busy(tmp_path)


async def test_the_heartbeat_reply_reaches_the_updater_and_its_report_goes_back(
        monkeypatch, spool, device_key, tmp_path, key):
    from tests.fakes import ScriptedUploader, make_controller
    ctl = make_controller(monkeypatch, spool, device_key, ScriptedUploader([]))
    ctl._state_dir = tmp_path
    ctl.cfg.app_version, ctl.cfg.protocol_version = "3.8.3", 2
    bundle = sign(key, payload())
    await ctl._on_heartbeat_reply({"status": "ok", "update": bundle})
    assert (tmp_path / updater.NOTICE_FILE).exists()
    await ctl._on_heartbeat_reply({"status": "ok"})
    assert not (tmp_path / updater.NOTICE_FILE).exists()
    assert ctl.heartbeat_payload()["update_status"] is None
    (tmp_path / updater.STATUS_FILE).write_text(json.dumps(
        {"state": "gave_up", "version": "3.8.4", "attempts": 3, "reason": "SHA-256"}),
        encoding="utf-8")
    assert ctl.heartbeat_payload()["update_status"]["state"] == "gave_up"
    # Recording or not is written for the updater on every change of state.
    ctl.state = "recording"
    assert agent_busy(tmp_path)
    ctl.state = "idle"
    assert not agent_busy(tmp_path)
    await ctl.close()


def test_the_task_refuses_to_run_without_the_server_key(tmp_path, monkeypatch):
    monkeypatch.setattr(updater, "is_administrator", lambda: True)
    cfg = SimpleNamespace(security=SimpleNamespace(receipt_public_key_path=tmp_path / "none.pem"),
                          paths=SimpleNamespace(state_dir=tmp_path), app_version="3.8.3",
                          update_publisher="AIMS LAB")
    assert updater.run_from_task(cfg) == 4


def test_the_launch_keeps_the_exit_code_and_asks_for_a_report(monkeypatch, tmp_path):
    started = []

    class Popen:
        def __init__(self, command, **kw):
            started.append((command, kw["creationflags"]))

    monkeypatch.setattr(updater.subprocess, "Popen", Popen)
    updater.launch_setup(tmp_path / "AIMScribeSetup-3.8.4.exe", tmp_path / "setup.log",
                         tmp_path / "setup.exit")
    command = started[0][0]
    assert "/VERYSILENT /SUPPRESSMSGBOXES /NORESTART" in command
    assert "/v:on" in command and "!errorlevel!>" in command
    assert 'schtasks /Run /TN "AIMScribe Updater"' in command
    assert started[0][1] & 0x01000000                     # out of the task's job


def test_the_domain_matches_the_server():
    server = (Path(__file__).resolve().parents[2] / "backend" / "src" / "integrity.py")
    if not server.exists():
        pytest.skip("the server's code is not beside this recorder")
    assert 'UPDATE_DOMAIN = b"aimscribe.recorder-update.v1"' in server.read_text(encoding="utf-8")
    assert updater.DOMAIN == b"aimscribe.recorder-update.v1"
