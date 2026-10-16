"""
The consultation CMED asked for, remembered until it is recording.

A start can fail where nobody is watching: the microphone is held by another
program for a few seconds, or the recorder itself dies mid-consultation and
the Windows task brings it back a minute later. In both cases CMED's page has
already sent its trigger and the doctor has moved on to talking to the
patient. Asking the doctor to click the patient again is exactly what this
system must never need.

So the trigger is written here when it arrives, and stays until the
consultation ends normally:

    want()        CMED asked for this consultation (and whether it is recording)
    armed()       its prescription was built
    touch()       still going - written by the heartbeat while recording
    forget()      it ended normally: stopped, refused, superseded or reset
    recent()      what to resume after a restart, if it was live moments ago

One small JSON file, replaced atomically. Nothing about the patient is in it
beyond the five fields the spool journal already holds.
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

FILE_NAME = "live_consultation.json"


class StartKeeper:
    def __init__(self, state_dir: Optional[Path]):
        self._path = Path(state_dir) / FILE_NAME if state_dir else None

    @property
    def enabled(self) -> bool:
        return self._path is not None

    def want(self, trigger: Dict[str, str], *, session_id: Optional[str] = None) -> None:
        current = self.load() or {}
        same = current.get("trigger") == dict(trigger)
        self._write({
            "trigger": dict(trigger),
            "session_id": session_id or (current.get("session_id") if same else None),
            "armed": bool(current.get("armed")) if same else False,
            "asked_at": current.get("asked_at") if same else time.time(),
        })

    def armed(self) -> None:
        current = self.load()
        if current is not None:
            current["armed"] = True
            self._write(current)

    def touch(self) -> None:
        current = self.load()
        if current is not None:
            self._write(current)

    def forget(self) -> None:
        if self._path is None:
            return
        try:
            self._path.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            logger.warning("Could not clear %s: %s", self._path, exc)

    def load(self) -> Optional[Dict[str, Any]]:
        if self._path is None:
            return None
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
            # A torn file from a power cut is worth nothing; resuming from a
            # half-written trigger would be worse than not resuming.
            logger.warning("Ignoring unreadable %s: %s", self._path, exc)
            return None
        return data if isinstance(data.get("trigger"), dict) else None

    def recent(self, within_seconds: float) -> Optional[Dict[str, Any]]:
        """The wanted consultation, if it was still live `within_seconds` ago."""
        data = self.load()
        if data is None:
            return None
        try:
            age = time.time() - self._path.stat().st_mtime
        except OSError:
            return None
        if age > within_seconds:
            # Yesterday's last patient, or a PC that was off for an hour: the
            # doctor is not still in that consultation.
            logger.info("Not resuming a consultation last seen %.0f s ago", age)
            self.forget()
            return None
        data["age_seconds"] = age
        return data

    def _write(self, data: Dict[str, Any]) -> None:
        if self._path is None:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            partial = self._path.with_suffix(".partial")
            partial.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
            os.replace(partial, self._path)
        except OSError as exc:
            logger.warning("Could not remember the live consultation: %s", exc)


__all__ = ["StartKeeper", "FILE_NAME"]
