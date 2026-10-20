"""
A full disk mid-consultation no longer ends the recording (failure review C2).

Before: a failed clip write re-raised on the segmenter thread, the thread died,
capture kept filling a queue nobody read, and the overlay still said
"recording". Now the clip is kept in memory, written as soon as the disk takes
it, the chain is never left naming a clip that is not there, and a new
recording is refused while the disk has under 1 GB.
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

import core.session_controller as sc
from core import disk, spool as spool_module
from core.simple_splitter import Segmenter
from core.session_controller import SessionError
from core.start_keeper import StartKeeper
from core.uploader import AuthOutcome
from tests.fakes import FakeSegmenter, ScriptedUploader, make_controller, trigger

GRANTED = AuthOutcome("granted", "GRANTED", "", None, "confirmed")


def segmenter(writes, **kw):
    """A real segmenter with tiny clips, writing through `writes`."""
    return Segmenter(sample_rate=4000, channels=1, sample_width=2, min_seconds=0.5,
                     max_seconds=0.5, silence_rms=300, silence_hold_seconds=0.2,
                     on_segment=writes, retry_seconds=0.05, **kw)


def wait(condition, seconds=3.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.01)
    return condition()


class Disk:
    """A write target that is full until told otherwise."""

    def __init__(self):
        self.full = True
        self.written = []
        self.attempts = 0

    def __call__(self, sealed):
        self.attempts += 1
        if self.full:
            raise OSError(28, "No space left on device")
        self.written.append(sealed.pcm)


def test_a_full_disk_keeps_the_audio_and_writes_it_once_there_is_room():
    target, errors, recovered = Disk(), [], []
    seg = segmenter(target, on_write_error=lambda e, s: errors.append(s),
                    on_write_recovered=recovered.append)
    seg.start(datetime.now(timezone.utc))
    audio = bytes(range(256)) * 64                # 16 KB = 2 s of audio
    for i in range(0, len(audio), 800):
        seg.submit(audio[i:i + 800])
    assert wait(lambda: target.attempts >= 2)
    assert seg._thread.is_alive()                 # the thread did not die
    assert seg.pending_seconds > 0 and len(errors) == 1   # said once, not per retry

    target.full = False
    assert wait(lambda: seg.pending_seconds == 0)
    seg.stop(seal_remaining=True)
    assert b"".join(target.written) == audio      # every byte, once, in order
    assert len(recovered) == 1


def test_memory_is_bounded_and_the_loss_is_counted():
    target = Disk()
    seg = segmenter(target, pending_limit_seconds=1.0)
    seg.start(datetime.now(timezone.utc))
    for _ in range(40):                           # 4 s of audio against a 1 s limit
        seg.submit(b"\x10\x00" * 400)
    assert wait(lambda: seg.lost_seconds > 0)
    assert seg.pending_seconds <= 1.0 + 0.5       # the limit, plus at most one clip
    seg._stop.set()
    target.full = False
    assert wait(lambda: seg.pending_seconds == 0)


def test_stop_does_not_wait_while_the_disk_is_full():
    target = Disk()
    seg = segmenter(target)
    seg.start(datetime.now(timezone.utc))
    seg.submit(b"\x10\x00" * 2000)
    began = time.monotonic()
    seg.stop(seal_remaining=True)
    assert time.monotonic() - began < 3.0         # Stop answers; the thread keeps trying
    assert seg.pending_seconds > 0
    target.full = False
    assert seg.wait_written(3.0)
    assert sum(len(p) for p in target.written) == 4000


def test_while_clips_wait_the_power_cut_copy_holds_all_of_them(spool, device_key):
    """No hole: what is not in a sealed clip is all in the partial file, in order."""
    session = spool.open_session(device_key=device_key, device_id="D", doctor_id="DR",
                                 hospital_id="H", patient_ref="P", consent_method="reception",
                                 audio={"sample_rate": 4000, "channels": 1, "sample_width": 2})
    state = {"full": True}

    def write(sealed):
        if state["full"]:
            raise OSError(28, "No space left on device")
        session.seal_segment(sealed.pcm, captured_start_at=sealed.captured_start_at,
                             captured_end_at=sealed.captured_end_at,
                             rms_mean=sealed.rms_mean, is_final=sealed.is_final)

    seg = segmenter(write, on_partial=session.write_partial, partial_every_seconds=0.2)
    seg.start(datetime.now(timezone.utc))
    audio = bytes(range(250)) * 80                # 20 KB = 2.5 s
    for i in range(0, len(audio), 400):
        seg.submit(audio[i:i + 400])
    assert wait(lambda: seg.pending_seconds >= 1.0)
    seg.flush()
    assert wait(lambda: (session.read_partial() or (0, b""))[1] == audio)

    state["full"] = False                         # room again: all of it sealed
    assert wait(lambda: seg.pending_seconds == 0)
    seg.stop(seal_remaining=True)
    assert session.verify_chain().ok
    assert sum(s.byte_length - 44 for s in session.segments.values()) == len(audio)


# ---------------------------------------------------------------- the spool

def test_a_failed_clip_write_leaves_the_chain_as_it_was(spool, device_key, monkeypatch):
    session = spool.open_session(device_key=device_key, device_id="D", doctor_id="DR",
                                 hospital_id="H", patient_ref="P", consent_method="reception",
                                 audio={"sample_rate": 4000, "channels": 1, "sample_width": 2})
    real = spool_module._atomic_write
    head, entries = session.head_hash, len(session.chain)

    def full(path, data):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(spool_module, "_atomic_write", full)
    now = datetime.now(timezone.utc)
    with pytest.raises(OSError):
        session.seal_segment(b"\x01\x00" * 100, captured_start_at=now, captured_end_at=now,
                             rms_mean=1.0, is_final=False)
    assert (session.head_hash, len(session.chain), session.segments) == (head, entries, {})

    monkeypatch.setattr(spool_module, "_atomic_write", real)
    seg = session.seal_segment(b"\x01\x00" * 100, captured_start_at=now, captured_end_at=now,
                               rms_mean=1.0, is_final=False)
    assert seg.seq_no == 1 and session.verify_chain().ok


def test_a_journal_that_cannot_be_written_takes_the_clip_file_back(spool, device_key,
                                                                   monkeypatch):
    session = spool.open_session(device_key=device_key, device_id="D", doctor_id="DR",
                                 hospital_id="H", patient_ref="P", consent_method="reception",
                                 audio={"sample_rate": 4000, "channels": 1, "sample_width": 2})
    entries = len(session.chain)

    def full(records):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(session, "_append_journal_records", full)
    now = datetime.now(timezone.utc)
    with pytest.raises(OSError):
        session.seal_segment(b"\x01\x00" * 100, captured_start_at=now, captured_end_at=now,
                             rms_mean=1.0, is_final=False)
    assert len(session.chain) == entries
    assert not list(session.directory.glob("seg-*"))


# ---------------------------------------------------------------- the controller

@pytest.fixture
def ctl(monkeypatch, spool, device_key, tmp_path):
    controller = make_controller(monkeypatch, spool, device_key, ScriptedUploader([GRANTED]))
    controller._keeper = StartKeeper(tmp_path / "state")
    return controller


async def test_no_new_recording_with_under_a_gigabyte_free(ctl, monkeypatch):
    monkeypatch.setattr(ctl._drive, "free_bytes", lambda: 300 * 2**20)
    with pytest.raises(SessionError) as refused:
        await ctl.open_session(trigger("P1"))
    assert refused.value.code == "AGENT_NOT_READY"
    with pytest.raises(SessionError):
        await ctl.open_session(trigger("P1"))
    alerts = [d for n, d in ctl.events if n == "integrity_alert" and d["alert_type"] == "disk_full"]
    assert len(alerts) == 1                       # once, not per attempt
    await ctl.close()


async def test_the_heartbeat_checks_the_disk(ctl, monkeypatch):
    monkeypatch.setattr(ctl._drive, "free_bytes", lambda: 100 * 2**20)
    ctl.cfg.app_version, ctl.cfg.protocol_version = "3.4.0", 2
    assert ctl._check_disk() == 100 * 2**20
    assert ctl.heartbeat_payload()["disk_free_bytes"] == 100 * 2**20
    assert [d["alert_type"] for n, d in ctl.events if n == "integrity_alert"] == ["disk_full"]
    await ctl.close()


async def test_a_stop_while_clips_wait_for_the_disk_closes_once_they_are_written(
        monkeypatch, spool, device_key, tmp_path):
    state = {"waiting": True}

    class Waiting(FakeSegmenter):
        @property
        def pending_seconds(self):
            return 4.0 if state["waiting"] else 0.0

        def wait_written(self, timeout):
            time.sleep(min(timeout, 0.05))
            return not state["waiting"]

    up = ScriptedUploader([GRANTED])
    ctl = make_controller(monkeypatch, spool, device_key, up)
    monkeypatch.setattr(sc, "Segmenter", Waiting)
    ctl._keeper = StartKeeper(tmp_path / "state")
    started = await ctl.open_session(trigger("P1"))
    result = await ctl.stop_session(reason="doctor_stopped")
    assert result["waiting_for_disk"] is True and ctl.state == "idle"
    live = next(s for s in spool.recover(device_key) if s.session_id == started["session_id"])
    assert live.closed_at is None                 # the close waits for the clips

    state["waiting"] = False
    assert await asyncio.wait_for(_until(lambda: up.closed), 5.0)
    await ctl.close()


async def _until(condition):
    while not condition():
        await asyncio.sleep(0.02)
    return True


# ---------------------------------------------------------------- BitLocker

@pytest.mark.parametrize("answer,expected", [(1, True), (2, False), (0, False),
                                             (3, None), (None, None)])
def test_bitlocker_is_read_from_the_shell(monkeypatch, answer, expected):
    monkeypatch.setattr(disk.sys, "platform", "win32")
    seen = []
    monkeypatch.setattr(disk, "_volume_property",
                        lambda path, name: seen.append((path, name)) or answer)
    assert disk._probe_bitlocker("C:") is expected
    assert seen == [("C:\\", "System.Volume.BitLockerProtection")]


def test_bitlocker_is_read_without_starting_any_program(monkeypatch):
    """No PowerShell, no child process: what antivirus behaviour rules flag."""
    import subprocess
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("a program was run"))
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: pytest.fail("a program was run"))
    disk._probe_bitlocker("C:")                     # the real property store on Windows


@pytest.mark.skipif(disk.sys.platform != "win32", reason="the Shell is Windows only")
def test_bitlocker_is_answered_on_this_pc():
    assert disk._volume_property("C:\\", "System.Volume.BitLockerProtection") in (0, 1, 2, 3)


def test_the_probe_runs_in_the_background(monkeypatch, tmp_path):
    def slow(drive):
        time.sleep(0.5)
        return True

    monkeypatch.setattr(disk, "_probe_bitlocker", slow)
    facts = disk.DriveFacts(tmp_path)
    began = time.monotonic()
    thread = facts.probe_in_background()
    assert time.monotonic() - began < 0.2 and facts.encrypted is None
    thread.join(2)
    assert facts.encrypted is True
