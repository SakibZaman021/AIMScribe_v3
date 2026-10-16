"""
Close the consultation properly when Windows shuts down, restarts or signs out.

Without this the agent is simply killed: the clip being assembled is lost and the
recording is left open until the next boot. Windows tells every top-level window
first (WM_QUERYENDSESSION); this module owns a hidden one, holds the shutdown for
the second or so the close takes - with a reason Windows can show, "AIMScribe is
saving the consultation" - and then lets it continue. It never refuses a
shutdown: a doctor who presses Shut down gets one.

A forced power-off cannot be caught by any program. That case is covered by the
clip in progress being kept on disk every few seconds (core.spool, PARTIAL_NAME).

The window must be top-level and hidden, not message-only: message-only windows
(HWND_MESSAGE) are not sent the session-ending broadcasts.
"""
from __future__ import annotations

import logging
import sys
import threading
from typing import Callable, Optional

logger = logging.getLogger(__name__)

WM_QUERYENDSESSION = 0x0011
WM_ENDSESSION = 0x0016
WM_CLOSE = 0x0010
WM_DESTROY = 0x0002

REASON = "AIMScribe is saving the consultation"
# Applications are given levels 0x100-0x3FF; higher is asked earlier. Asked early,
# the close runs while the rest of the desktop is still shutting down around it.
SHUTDOWN_LEVEL = 0x3FF
# How long the close may take before Windows is let go regardless.
CLOSE_TIMEOUT_SECONDS = 4.0


class ShutdownHandler:
    """
    What to do with each session-ending message, apart from any window, so it
    can be tested without shutting a PC down.

    `close(timeout)` closes the recording and returns once it has (True) or the
    timeout passed (False). `block(text)` / `unblock()` set and clear the reason
    Windows shows while it waits.
    """

    def __init__(self, close: Callable[[float], bool],
                 block: Callable[[str], None] = lambda text: None,
                 unblock: Callable[[], None] = lambda: None,
                 timeout: float = CLOSE_TIMEOUT_SECONDS):
        self._close = close
        self._block = block
        self._unblock = unblock
        self.timeout = timeout
        self.closed = False
        self._lock = threading.Lock()

    def handle(self, message: int, wparam: int = 0, lparam: int = 0) -> Optional[int]:
        """The window procedure's answer, or None to leave it to Windows."""
        if message == WM_QUERYENDSESSION:
            self._finish()
            return 1                      # TRUE: never stand in the way
        if message == WM_ENDSESSION:
            if wparam:                    # the session really is ending
                self._finish()
            self._unblock()
            return 0
        return None

    def _finish(self) -> None:
        with self._lock:
            if self.closed:
                return
            self._block(REASON)
            try:
                ok = self._close(self.timeout)
            except Exception as exc:
                logger.error("Closing the recording for shutdown failed: %s", exc)
                ok = False
            self.closed = True
        if ok:
            logger.warning("Windows is shutting down; the recording was closed properly")
        else:
            logger.error("Windows is shutting down; the recording could not be closed in "
                         "%.0fs - the clip on disk is recovered at next start", self.timeout)


class ShutdownGuard:
    """The hidden window, on its own thread. A no-op off Windows."""

    def __init__(self, handler: ShutdownHandler):
        self.handler = handler
        self._thread: Optional[threading.Thread] = None
        self._hwnd = None
        self._proc = None                 # kept referenced: Windows calls it

    def start(self) -> None:
        if sys.platform != "win32":
            return
        self._thread = threading.Thread(target=self._run, name="ShutdownGuard", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        try:
            self._loop()
        except Exception as exc:
            logger.error("Shutdown guard unavailable: %s", exc)

    def _loop(self) -> None:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        LRESULT = ctypes.c_ssize_t
        WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT,
                                     wintypes.WPARAM, wintypes.LPARAM)

        user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT,
                                          wintypes.WPARAM, wintypes.LPARAM]
        user32.DefWindowProcW.restype = LRESULT
        user32.ShutdownBlockReasonCreate.argtypes = [wintypes.HWND, wintypes.LPCWSTR]
        user32.ShutdownBlockReasonDestroy.argtypes = [wintypes.HWND]
        user32.CreateWindowExW.restype = wintypes.HWND
        user32.CreateWindowExW.argtypes = [
            wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
            wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]

        handler = self.handler
        handler._block = lambda text: user32.ShutdownBlockReasonCreate(self._hwnd, text)
        handler._unblock = lambda: user32.ShutdownBlockReasonDestroy(self._hwnd)

        def proc(hwnd, msg, wparam, lparam):
            try:
                answer = handler.handle(msg, wparam, lparam)
            except Exception as exc:          # never let Windows see an exception
                logger.error("Shutdown guard: %s", exc)
                answer = 1 if msg == WM_QUERYENDSESSION else None
            if answer is not None:
                return answer
            return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

        self._proc = WNDPROC(proc)

        class WNDCLASSW(ctypes.Structure):
            _fields_ = [("style", wintypes.UINT), ("lpfnWndProc", WNDPROC),
                        ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                        ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
                        ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
                        ("lpszMenuName", wintypes.LPCWSTR),
                        ("lpszClassName", wintypes.LPCWSTR)]

        # Every handle declared: left to ctypes' default int, a 64-bit handle is
        # cut to 32 bits and Windows reads through the stump.
        kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
        kernel32.GetModuleHandleW.restype = wintypes.HMODULE
        kernel32.SetProcessShutdownParameters.argtypes = [wintypes.DWORD, wintypes.DWORD]
        user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
        user32.RegisterClassW.restype = wintypes.ATOM
        user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND,
                                       wintypes.UINT, wintypes.UINT]
        user32.GetMessageW.restype = wintypes.BOOL
        user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
        user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
        user32.DispatchMessageW.restype = LRESULT

        instance = kernel32.GetModuleHandleW(None)
        wc = WNDCLASSW()
        wc.lpfnWndProc = self._proc
        wc.hInstance = instance
        wc.lpszClassName = "AIMScribeShutdownGuard"
        if not user32.RegisterClassW(ctypes.byref(wc)):
            raise OSError(ctypes.get_last_error(), "RegisterClassW failed")

        # Top-level, never shown: WS_OVERLAPPED with no WS_VISIBLE.
        self._hwnd = user32.CreateWindowExW(0, wc.lpszClassName, "AIMScribe", 0,
                                            0, 0, 0, 0, None, None, instance, None)
        if not self._hwnd:
            raise OSError(ctypes.get_last_error(), "CreateWindowExW failed")
        if not kernel32.SetProcessShutdownParameters(SHUTDOWN_LEVEL, 0):
            logger.debug("SetProcessShutdownParameters failed: %s", ctypes.get_last_error())
        logger.info("Shutdown guard ready: a Windows shutdown closes the recording first")

        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))


def guard_runtime(runtime) -> ShutdownGuard:
    """Start a guard that closes `runtime`'s recording on shutdown."""
    import asyncio

    def close(timeout: float) -> bool:
        loop, controller = runtime.loop, runtime.controller
        if loop is None or controller is None:
            return True
        future = asyncio.run_coroutine_threadsafe(
            controller.stop_session(reason="pc_shutdown"), loop)
        try:
            future.result(timeout)
            return True
        except Exception:
            return False

    guard = ShutdownGuard(ShutdownHandler(close))
    guard.start()
    return guard


__all__ = ["ShutdownHandler", "ShutdownGuard", "guard_runtime", "REASON"]
