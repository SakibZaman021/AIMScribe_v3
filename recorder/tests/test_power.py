"""Windows may not sleep while a consultation is open, and may again after."""
from __future__ import annotations

from core import power
from core import session_controller as sc
from tests.fakes import ScriptedUploader, make_controller, trigger
from core.uploader import AuthOutcome


async def test_sleep_is_blocked_exactly_while_recording(monkeypatch, spool, device_key):
    calls = []
    monkeypatch.setattr(sc, "keep_awake", calls.append)
    ctl = make_controller(monkeypatch, spool, device_key,
                          ScriptedUploader([AuthOutcome("granted", "", "")]))
    await ctl.open_session(trigger("P1"))
    assert calls[-1] is True
    await ctl.stop_session(reason="doctor_stop")
    assert calls[-1] is False
    await ctl.close()


def test_keep_awake_asks_windows_once_per_change(monkeypatch):
    seen = []
    monkeypatch.setattr(power, "_awake", False)
    monkeypatch.setattr(power.sys, "platform", "win32")

    class Kernel:
        def SetThreadExecutionState(self, flags):
            seen.append(flags)
            return 1

    import ctypes
    monkeypatch.setattr(ctypes, "windll", type("W", (), {"kernel32": Kernel()})(), raising=False)
    power.keep_awake(True)
    power.keep_awake(True)
    power.keep_awake(False)
    assert seen == [power.ES_CONTINUOUS | power.ES_SYSTEM_REQUIRED, power.ES_CONTINUOUS]
