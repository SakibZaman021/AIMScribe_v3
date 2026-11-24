"""
3.9.7 (25 Nov 2026): a microphone that gives pure digital silence.

On 24 Nov GP-Males' own microphone gave exact zeros for 45 recordings. Now,
on any microphone but the clinic's, 5 s of pure zeros moves the recording:
to the Jabra if it is plugged in, else to another microphone with sound.
On the Jabra itself the recording never moves - a quiet room is silence too.
"""
from __future__ import annotations

import pytest

from core import microphone
from core import recorder as rec
from tests import test_microphone_failover as base
from tests.test_microphone_failover import capture, wait_for, world  # noqa: F401


@pytest.fixture(autouse=True)
def silent_mode(monkeypatch):
    """A device whose behaviour is "silent" delivers exact zeros."""
    original = base.Stream.read

    def read(self, frames, exception_on_overflow=False):
        if self.world.behaviour.get(self.name) == "silent":
            return b"\x00" * (frames * 2)
        return original(self, frames, exception_on_overflow)

    monkeypatch.setattr(base.Stream, "read", read)
    monkeypatch.setattr(rec.AudioRecorder, "SILENCE_SECONDS", 0.2)


def test_a_silent_pc_microphone_hands_over_to_one_with_sound(world):
    world.devices = {0: "Microphone Array (Intel)", 2: "Headset microphone"}
    world.default = 0
    world.behaviour["Microphone Array (Intel)"] = "silent"
    r, chunks, events = capture(preferred_name="jabra")
    r.start()
    assert r.device_name == "Microphone Array (Intel)"
    assert wait_for(lambda: events["changed"])
    assert events["changed"][0][0] == "Headset microphone"
    assert r.last_switch_reason == "Microphone Array (Intel) gave pure silence"
    r.stop()


def test_a_silent_pc_microphone_goes_to_the_jabra_first(world):
    world.devices = {0: "Microphone Array (Intel)", 2: "Headset microphone"}
    world.default = 0
    world.behaviour["Microphone Array (Intel)"] = "silent"
    r, chunks, events = capture(preferred_name="jabra")
    r.start()
    world.devices[3] = "Microphone (Jabra SPEAK2 55)"      # plugged in meanwhile
    assert wait_for(lambda: events["changed"])
    assert events["changed"][0][0] == "Microphone (Jabra SPEAK2 55)"
    r.stop()


def test_the_jabra_is_never_left_for_silence(world):
    world.devices = {0: "PC microphone", 3: "Microphone (Jabra SPEAK2 55)"}
    world.default = 0
    world.behaviour["Microphone (Jabra SPEAK2 55)"] = "silent"   # a quiet room
    r, chunks, events = capture(preferred_name="jabra")
    r.start()
    assert r.device_name == "Microphone (Jabra SPEAK2 55)"
    assert not wait_for(lambda: events["changed"], seconds=0.8)
    assert r.device_name == "Microphone (Jabra SPEAK2 55)"
    r.stop()


def test_with_nothing_better_it_stays_and_says_so_once(world):
    world.devices = {0: "Microphone Array (Intel)"}
    world.default = 0
    world.behaviour["Microphone Array (Intel)"] = "silent"
    r, chunks, events = capture(preferred_name="jabra")
    r.start()
    assert wait_for(lambda: events["errors"])
    assert not events["changed"]
    assert len(events["errors"]) == 1 and "gives no sound" in events["errors"][0]
    r.stop()


def test_a_garbled_name_is_read_as_windows_shows_it():
    assert microphone.readable_name("Microphone Array (IntelÂ® Smart ") == \
        "Microphone Array (Intel® Smart "
    assert microphone.readable_name("Echo Cancelling Speakerphone (J") == \
        "Echo Cancelling Speakerphone (J"
