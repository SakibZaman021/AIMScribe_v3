"""
A polite way for the installer to stop the agent before it replaces the files.

The installer used to `taskkill /F` the agent: the clip being assembled was cut
off, the recording was left open, and a process killing another one is what
antivirus behaviour rules watch for. Instead the agent creates a named event,
Global\\AIMScribeAgentStop, and waits on it. The installer (elevated) signals
it; the agent closes any open recording with the reason "software_update" -
which keeps nothing from being lost and lets the updated agent pick the
consultation up again - writes everything to disk, shuts down normally and
exits, within 30 seconds. The installer kills only if it has not.

The event's security descriptor lets SYSTEM, Administrators and the agent's
own account signal it, and nobody else: an ordinary program running as
another user cannot stop the recorder this way.
"""
from __future__ import annotations

import logging
import sys
import threading
from typing import Callable, Optional

logger = logging.getLogger(__name__)

EVENT_NAME = "Global\\AIMScribeAgentStop"
# SYSTEM, built-in Administrators, the owner: EVENT_ALL_ACCESS (0x1F0003).
EVENT_SDDL = "D:(A;;0x1F0003;;;SY)(A;;0x1F0003;;;BA)(A;;0x1F0003;;;OW)"
STOP_REASON = "software_update"
EXIT_WITHIN_SECONDS = 30.0
WAIT_OBJECT_0, WAIT_TIMEOUT, INFINITE = 0, 0x102, 0xFFFFFFFF


class Win32Event:
    """The real named event. Kept separate so the listener can be tested."""

    def __init__(self, name: str = EVENT_NAME, sddl: str = EVENT_SDDL):
        import ctypes
        from ctypes import wintypes
        self._ctypes = ctypes
        self._k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        advapi = ctypes.WinDLL("advapi32", use_last_error=True)

        class SECURITY_ATTRIBUTES(ctypes.Structure):
            _fields_ = [("nLength", wintypes.DWORD), ("lpSecurityDescriptor", ctypes.c_void_p),
                        ("bInheritHandle", wintypes.BOOL)]

        descriptor = ctypes.c_void_p()
        advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
            wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(wintypes.ULONG)]
        if not advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(
                sddl, 1, ctypes.byref(descriptor), None):          # SDDL_REVISION_1
            raise OSError(ctypes.get_last_error(), "could not build the event's ACL")
        attributes = SECURITY_ATTRIBUTES(ctypes.sizeof(SECURITY_ATTRIBUTES), descriptor, False)
        self._k32.CreateEventW.argtypes = [ctypes.POINTER(SECURITY_ATTRIBUTES), wintypes.BOOL,
                                           wintypes.BOOL, wintypes.LPCWSTR]
        self._k32.CreateEventW.restype = wintypes.HANDLE
        # Auto-reset, initially unset.
        self.handle = self._k32.CreateEventW(ctypes.byref(attributes), False, False, name)
        self._k32.LocalFree(descriptor)
        if not self.handle:
            raise OSError(ctypes.get_last_error(), "could not create the stop event")
        self._k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        self._k32.WaitForSingleObject.restype = wintypes.DWORD

    def wait(self, timeout_ms: int) -> bool:
        return self._k32.WaitForSingleObject(self.handle, timeout_ms) == WAIT_OBJECT_0

    def close(self) -> None:
        if self.handle:
            self._k32.CloseHandle(self.handle)
            self.handle = None


class StopListener:
    """Waits on the event on its own thread and calls `on_stop` once."""

    def __init__(self, on_stop: Callable[[], None], event=None):
        self._on_stop = on_stop
        self._event = event
        self._done = threading.Event()
        self.thread: Optional[threading.Thread] = None
        self.fired = False

    def start(self) -> bool:
        if self._event is None:
            if sys.platform != "win32":
                return False
            try:
                self._event = Win32Event()
            except OSError as exc:
                logger.warning("No stop event for the installer (%s); an update will have "
                               "to end the agent the hard way", exc)
                return False
        self.thread = threading.Thread(target=self._run, name="StopListener", daemon=True)
        self.thread.start()
        return True

    def _run(self) -> None:
        while not self._done.is_set():
            if self._event.wait(1000):
                self.fired = True
                logger.warning("The installer asked the agent to stop for a software update")
                try:
                    self._on_stop()
                except Exception:
                    logger.exception("Stopping for the update failed")
                return

    def stop(self) -> None:
        self._done.set()
        close = getattr(self._event, "close", None)
        if close is not None and self.thread is not None:
            self.thread.join(2)
            close()


def stop_for_update(runtime, quit_agent: Callable[[], None],
                    timeout: float = EXIT_WITHIN_SECONDS - 5) -> bool:
    """
    Close any open recording (kept, not erased; the updated agent resumes the
    consultation), let everything reach the disk, then quit the agent.
    True when the recording closed in time.
    """
    import asyncio
    closed = True
    loop, controller = getattr(runtime, "loop", None), getattr(runtime, "controller", None)
    if loop is not None and controller is not None:
        future = asyncio.run_coroutine_threadsafe(
            controller.stop_session(reason=STOP_REASON), loop)
        try:
            future.result(timeout)
        except Exception as exc:
            closed = False
            logger.error("The recording could not be closed for the update in time: %s", exc)
    quit_agent()
    return closed


__all__ = ["StopListener", "Win32Event", "stop_for_update", "EVENT_NAME", "EVENT_SDDL",
           "STOP_REASON"]
