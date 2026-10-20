"""
Windows shutting down closes the recording first, and never waits for ever.

The handler is tested on its own; on Windows the real hidden window is also
created and sent the shutdown message, which exercises the window procedure
exactly as Windows would - without shutting this PC down.
"""
from __future__ import annotations

import asyncio
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from core import shutdown_guard as sg


def handler(close_result=True, raises=False):
    seen = {"closes": [], "blocks": [], "unblocks": 0}

    def close(timeout):
        seen["closes"].append(timeout)
        if raises:
            raise RuntimeError("boom")
        return close_result

    h = sg.ShutdownHandler(close, block=seen["blocks"].append,
                           unblock=lambda: seen.__setitem__("unblocks", seen["unblocks"] + 1))
    return h, seen


def test_shutdown_closes_once_and_never_refuses():
    h, seen = handler()
    assert h.handle(sg.WM_QUERYENDSESSION) == 1
    assert h.handle(sg.WM_QUERYENDSESSION) == 1          # asked again: no second close
    assert h.handle(sg.WM_ENDSESSION, wparam=1) == 0
    assert seen["closes"] == [sg.CLOSE_TIMEOUT_SECONDS]
    assert seen["blocks"] == [sg.REASON] and seen["unblocks"] == 1


def test_a_cancelled_shutdown_releases_the_block():
    h, seen = handler()
    h.handle(sg.WM_QUERYENDSESSION)
    assert h.handle(sg.WM_ENDSESSION, wparam=0) == 0     # someone pressed Cancel
    assert seen["unblocks"] == 1


def test_a_failed_close_still_lets_windows_go():
    for kwargs in ({"close_result": False}, {"raises": True}):
        h, seen = handler(**kwargs)
        assert h.handle(sg.WM_QUERYENDSESSION) == 1
        assert h.closed


def test_other_messages_are_left_to_windows():
    h, seen = handler()
    assert h.handle(0x0001) is None and seen["closes"] == []


def test_end_session_without_a_query_still_closes():
    h, seen = handler()
    h.handle(sg.WM_ENDSESSION, wparam=1)
    assert len(seen["closes"]) == 1


def runtime_on_a_loop(stop_session):
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    rt = SimpleNamespace(loop=loop, controller=SimpleNamespace(stop_session=stop_session))
    return rt, loop, thread


def test_the_runtime_close_stops_the_session_with_the_shutdown_reason(monkeypatch):
    reasons = []

    async def stop_session(reason):
        reasons.append(reason)
        return {"status": "stopped"}

    async def hold():
        reasons.append("hold")

    rt, loop, thread = runtime_on_a_loop(stop_session)
    rt.controller.hold_for_shutdown = hold
    monkeypatch.setattr(sg.ShutdownGuard, "start", lambda self: None)
    try:
        guard = sg.guard_runtime(rt)
        assert guard.handler.handle(sg.WM_QUERYENDSESSION) == 1
        assert reasons == ["hold"]                      # asked: only paused
        assert guard.handler.handle(sg.WM_ENDSESSION, wparam=1) == 0
        assert reasons == ["hold", "pc_shutdown"]       # it really ends: closed
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(2)
        loop.close()


def test_a_stuck_close_is_given_up_after_the_timeout(monkeypatch):
    async def stop_session(reason):
        await asyncio.sleep(30)

    rt, loop, thread = runtime_on_a_loop(stop_session)
    monkeypatch.setattr(sg.ShutdownGuard, "start", lambda self: None)
    try:
        guard = sg.guard_runtime(rt)
        guard.handler.timeout = 0.3
        began = time.monotonic()
        assert guard.handler.handle(sg.WM_ENDSESSION, wparam=1) == 0
        assert time.monotonic() - began < 2.0
    finally:
        for task in asyncio.all_tasks(loop):
            loop.call_soon_threadsafe(task.cancel)
        loop.call_soon_threadsafe(loop.stop)
        thread.join(2)
        loop.close()


def test_nothing_running_yet_is_fine():
    rt = SimpleNamespace(loop=None, controller=None)
    h = sg.ShutdownHandler(lambda t: True)
    assert h.handle(sg.WM_QUERYENDSESSION) == 1
    del rt


@pytest.mark.skipif(sys.platform != "win32", reason="the window exists only on Windows")
def test_the_real_hidden_window_answers_the_shutdown_message():
    import ctypes
    from ctypes import wintypes

    closed = threading.Event()
    guard = sg.ShutdownGuard(sg.ShutdownHandler(lambda t: closed.set() or True))
    guard.start()
    deadline = time.monotonic() + 3
    while guard._hwnd is None and time.monotonic() < deadline:
        time.sleep(0.02)
    assert guard._hwnd, "the hidden window was not created"

    user32 = ctypes.WinDLL("user32")
    user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT,
                                    wintypes.WPARAM, wintypes.LPARAM]
    user32.SendMessageW.restype = ctypes.c_ssize_t
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    assert not user32.IsWindowVisible(guard._hwnd)                  # never shown
    assert user32.GetParent(guard._hwnd) == 0                       # top-level
    assert user32.SendMessageW(guard._hwnd, sg.WM_QUERYENDSESSION, 0, 0) == 1
    assert closed.is_set()
    user32.SendMessageW(guard._hwnd, sg.WM_ENDSESSION, 0, 0)        # as if cancelled
    user32.PostMessageW(guard._hwnd, sg.WM_CLOSE, 0, 0)


# ---- the shutdown that is cancelled ----

def two_phase(recording=True):
    seen = {"closes": 0, "holds": 0, "resumes": 0, "blocks": []}
    h = sg.ShutdownHandler(
        lambda t: seen.__setitem__("closes", seen["closes"] + 1) or True,
        block=seen["blocks"].append,
        prepare=lambda t: seen.__setitem__("holds", seen["holds"] + 1) or True,
        cancelled=lambda t: seen.__setitem__("resumes", seen["resumes"] + 1) or True,
        recording=lambda: recording)
    return h, seen


def test_a_cancelled_shutdown_resumes_the_recording():
    h, seen = two_phase()
    h.handle(sg.WM_QUERYENDSESSION)
    h.handle(sg.WM_ENDSESSION, wparam=0)                # Cancel on the close-apps screen
    assert (seen["holds"], seen["resumes"], seen["closes"]) == (1, 1, 0)


def test_after_a_cancelled_shutdown_the_next_real_one_is_handled():
    h, seen = two_phase()
    h.handle(sg.WM_QUERYENDSESSION)
    h.handle(sg.WM_ENDSESSION, wparam=0)
    h.handle(sg.WM_QUERYENDSESSION)
    h.handle(sg.WM_ENDSESSION, wparam=1)
    assert (seen["holds"], seen["closes"]) == (2, 1)
    assert h.closed


def test_shutdown_is_held_up_only_while_recording():
    h, seen = two_phase(recording=False)
    h.handle(sg.WM_QUERYENDSESSION)
    h.handle(sg.WM_ENDSESSION, wparam=1)
    assert seen["blocks"] == []
    h, seen = two_phase(recording=True)
    h.handle(sg.WM_QUERYENDSESSION)
    assert seen["blocks"] == [sg.REASON]
