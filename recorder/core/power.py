"""
Keep the PC awake while a consultation is being recorded.

A doctor can talk to a patient for fifteen minutes without touching the
keyboard, which is exactly when Windows decides the laptop is idle and puts it
to sleep - and a sleeping PC records nothing. While a recording is open the
agent tells Windows the system is in use; the screen may still switch off.
Nothing is changed in the PC's power settings, and the request ends with the
recording (or with the agent itself).
"""
from __future__ import annotations

import logging
import sys

logger = logging.getLogger(__name__)

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001

_awake = False


def keep_awake(on: bool) -> None:
    """Ask Windows not to sleep (on) or allow it again (off). Idempotent."""
    global _awake
    if on == _awake or sys.platform != "win32":
        _awake = on
        return
    try:
        import ctypes
        flags = ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if on else 0)
        if not ctypes.windll.kernel32.SetThreadExecutionState(flags):
            logger.warning("Windows refused the keep-awake request")
            return
    except Exception as exc:
        logger.warning("Could not change the sleep setting: %s", exc)
        return
    _awake = on
    logger.info("Sleep %s while recording", "blocked" if on else "allowed again")


__all__ = ["keep_awake"]
