"""
A microphone that goes mid-consultation is replaced, and the recording carries on.

Driven against a fake PortAudio, because the failure that matters - a USB
microphone pulled out, after which Windows' read simply never returns - cannot
be produced on demand with real hardware.
"""
from __future__ import annotations

import sys
import threading
import time
import types

import pytest

from core import recorder as rec


class World:
    """The microphones plugged into the fake PC, and what each one does."""

    def __init__(self):
        self.devices = {0: "PC microphone", 1: "USB microphone"}
        self.default = 1
        self.behaviour = {}            # name -> "ok" | "hang" | "fail"
        self.release = threading.Event()

    def pull(self, name):
        """Unplug: reads on it hang, and Windows makes the PC microphone default."""
        self.behaviour[name] = "hang"
        self.devices = {i: n for i, n in self.devices.items() if n != name}
        self.default = next(iter(self.devices), None)


class Stream:
    def __init__(self, world, name):
        self.world, self.name = world, name

    def read(self, frames, exception_on_overflow=False):
        mode = self.world.behaviour.get(self.name, "ok")
        if mode == "hang":
            self.world.release.wait()
            raise OSError("device gone")
        if mode == "fail":
            time.sleep(0.001)
            raise OSError("Unanticipated host error")
        time.sleep(0.005)
        return self.name.encode()[:1] * (frames * 2)

    def is_active(self):
        return True

    def stop_stream(self):
        pass

    def close(self):
        pass


@pytest.fixture
def world(monkeypatch):
    w = World()

    class PyAudio:
        def open(self, *, input_device_index=None, **_):
            index = w.default if input_device_index is None else input_device_index
            if index not in w.devices:
                raise OSError(f"Invalid device {input_device_index}")
            return Stream(w, w.devices[index])

        def get_default_input_device_info(self):
            return {"name": w.devices.get(w.default, "none")}

        def get_device_info_by_index(self, index):
            return {"name": w.devices[index], "maxInputChannels": 1, "hostApi": 0}

        def get_device_count(self):
            return max(w.devices, default=-1) + 1

        def get_default_host_api_info(self):
            return {"index": 0}

        def terminate(self):
            pass

    monkeypatch.setitem(sys.modules, "pyaudio", types.SimpleNamespace(
        PyAudio=PyAudio, paInt16=8, paInt32=2))
    monkeypatch.setattr(rec.AudioRecorder, "WATCH_INTERVAL", 0.02)
    monkeypatch.setattr(rec.AudioRecorder, "REOPEN_INTERVAL", 0.05)
    yield w
    w.release.set()                    # let any stuck reader go


def capture(**kw):
    chunks, events = [], {"changed": [], "errors": []}
    r = rec.AudioRecorder(
        sample_rate=44100, channels=1, sample_width=2, frames_per_buffer=64,
        on_chunk=chunks.append, stall_seconds=0.3,
        on_device_change=lambda name, gap: events["changed"].append((name, gap)),
        on_error=events["errors"].append, **kw)
    return r, chunks, events


def wait_for(condition, seconds=3.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.01)
    return False


def test_an_unplugged_usb_microphone_moves_to_the_pc_microphone(world):
    r, chunks, events = capture()
    r.start()
    assert r.device_name == "USB microphone"
    assert wait_for(lambda: len(chunks) > 5)

    world.pull("USB microphone")                  # its next read never returns
    assert wait_for(lambda: events["changed"])
    name, gap = events["changed"][0]
    assert name == "PC microphone" and gap < 2.0
    assert wait_for(lambda: chunks and chunks[-1][:1] == b"P")     # PC audio arriving

    began = time.monotonic()
    stats = r.stop()
    assert time.monotonic() - began < 1.0        # the stuck reader does not hold Stop
    assert stats.device_changes == 1 and not r.is_running
    assert events["errors"] == []


def test_a_microphone_that_keeps_failing_is_replaced(world):
    r, chunks, events = capture()
    r.start()
    world.behaviour["USB microphone"] = "fail"
    world.default = 0
    assert wait_for(lambda: events["changed"])
    assert events["changed"][0][0] == "PC microphone"
    assert r.stats.read_errors >= 1          # whichever notices first: errors or silence
    r.stop()


def test_with_no_microphone_at_all_it_says_so_once_and_waits_for_one(world):
    r, chunks, events = capture()
    r.start()
    world.pull("USB microphone")
    world.devices, world.default = {}, None       # the PC has none either
    assert wait_for(lambda: events["errors"])
    time.sleep(0.3)
    assert len(events["errors"]) == 1 and not events["changed"]

    world.devices, world.default = {2: "Headset"}, 2     # somebody plugs one in
    assert wait_for(lambda: events["changed"])
    assert events["changed"][0][0] == "Headset"
    r.stop()


def test_a_configured_microphone_that_is_missing_falls_back_at_start(world):
    r, chunks, events = capture(input_device_index=7)
    r.start()
    assert r.device_name == "USB microphone"      # the Windows default
    assert wait_for(lambda: len(chunks) > 2)
    r.stop()


def test_a_clean_stop_is_not_mistaken_for_a_lost_microphone(world):
    r, chunks, events = capture()
    r.start()
    assert wait_for(lambda: len(chunks) > 2)
    r.stop()
    time.sleep(0.5)
    assert events == {"changed": [], "errors": []}
    assert r.stats.read_errors == 0 and r.stats.device_changes == 0


def test_pause_and_resume_still_work(world):
    r, chunks, events = capture()
    r.start()
    assert wait_for(lambda: len(chunks) > 2)
    r.stop()
    n = len(chunks)
    r.start()
    assert wait_for(lambda: len(chunks) > n + 2)
    r.stop()
    assert events["changed"] == []


def test_the_jabra_is_used_whatever_windows_has_as_default(world):
    world.devices = {0: "PC microphone", 3: "Microphone (Jabra SPEAK2 55)"}
    world.default = 0                              # Windows chose the PC's own
    r, chunks, events = capture(preferred_name="jabra")
    r.start()
    assert r.device_name == "Microphone (Jabra SPEAK2 55)"

    world.pull("Microphone (Jabra SPEAK2 55)")    # unplugged mid-consultation
    assert wait_for(lambda: events["changed"])
    assert events["changed"][0][0] == "PC microphone"
    r.stop()

    world.devices[3] = "Microphone (Jabra SPEAK2 55)"     # plugged back in
    world.behaviour.clear()
    r.start()                                      # the next patient is on the Jabra
    assert r.device_name == "Microphone (Jabra SPEAK2 55)"
    r.stop()


def test_without_a_jabra_the_windows_default_records(world):
    r, chunks, events = capture(preferred_name="jabra")
    r.start()
    assert r.device_name == "USB microphone"
    r.stop()
