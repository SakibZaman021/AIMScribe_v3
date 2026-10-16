"""
What this PC saw while it could not reach the AIMS LAB server.

Recording never waits for the network, so an outage loses no audio - but it
used to lose everything else. Heartbeats were sent once and dropped, and the
alerts a doctor saw on the overlay ("the microphone may be muted") never left
the laptop at all. The dashboard showed a gap, and nobody could say afterwards
whether a room was recording well or recording silence.

This keeps a small log until the server has it:

    alert()               every alert, always - sent at once when online
    offline_heartbeat()   one line a minute while the server is unreachable
    pending()             the oldest entries, in order, for delivery
    delivered(last_seq)   the server stored up to here: delete those

Deleted only after the server says it stored them. Bounded at 5 MB, about
twenty-five days of outage; past that the oldest routine lines go first and
alerts are kept. Plain JSON lines: no database, nothing to install, and a torn
last line after a power cut is simply skipped.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.spool import ends_open

logger = logging.getLogger(__name__)

FILE_NAME = "health_log.jsonl"
MAX_BYTES = 5 * 1024 * 1024
ROUTINE_EVERY_SECONDS = 60.0


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class HealthLog:
    def __init__(self, path: Path, *, max_bytes: int = MAX_BYTES,
                 routine_every: float = ROUTINE_EVERY_SECONDS):
        self.path = Path(path)
        self.max_bytes = max_bytes
        self.routine_every = routine_every
        # Called from the event loop and from the segmenter thread.
        self._lock = threading.Lock()
        self._last_routine = 0.0
        self._seq = self._highest_seq()

    # ---- writing ----

    def alert(self, data: Dict[str, Any]) -> None:
        keep = {k: data.get(k) for k in ("alert_type", "session_id", "seq_no", "detail")
                if data.get(k) is not None}
        self._append("alert", keep)

    def offline_heartbeat(self, payload: Dict[str, Any]) -> bool:
        """One line a minute while unreachable. True when a line was written."""
        now = time.monotonic()
        with self._lock:
            if now - self._last_routine < self.routine_every:
                return False
            self._last_routine = now
        self._append("heartbeat", dict(payload, online=False))
        return True

    def _append(self, kind: str, data: Dict[str, Any]) -> None:
        with self._lock:
            # Strictly increasing, even across a restart or a clock step back:
            # the server keeps one row per (device, seq).
            self._seq = max(self._seq + 1, time.time_ns() // 1000)
            line = json.dumps({"seq": self._seq, "kind": kind, "at": _now_iso(),
                               "data": data}, ensure_ascii=False, default=str)
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                line = ends_open(self.path) + line       # a line torn by a power cut
                with open(self.path, "a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                if self.path.stat().st_size > self.max_bytes:
                    self._shrink()
            except OSError as exc:
                logger.warning("Could not write the health log: %s", exc)

    # ---- delivery ----

    def pending(self, limit: int = 200) -> List[Dict[str, Any]]:
        with self._lock:
            return self._read()[:limit]

    def delivered(self, last_seq: int) -> int:
        """The server stored everything up to `last_seq`. Returns lines removed."""
        with self._lock:
            entries = self._read()
            keep = [e for e in entries if e["seq"] > last_seq]
            self._rewrite(keep)
            return len(entries) - len(keep)

    @property
    def size(self) -> int:
        try:
            return self.path.stat().st_size
        except OSError:
            return 0

    # ---- internals (lock held) ----

    def _read(self) -> List[Dict[str, Any]]:
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return []
        except OSError as exc:
            logger.warning("Could not read the health log: %s", exc)
            return []
        entries = []
        for line in lines:
            try:
                entry = json.loads(line)
            except ValueError:
                continue                       # a line torn by a power cut
            if isinstance(entry, dict) and isinstance(entry.get("seq"), int):
                entries.append(entry)
        return entries

    def _rewrite(self, entries: List[Dict[str, Any]]) -> None:
        if not entries:
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass
            return
        partial = self.path.with_suffix(".partial")
        partial.write_text("".join(json.dumps(e, ensure_ascii=False, default=str) + "\n"
                                   for e in entries), encoding="utf-8")
        os.replace(partial, self.path)

    def _shrink(self) -> None:
        """Over the cap: drop the oldest heartbeats first; alerts go last."""
        entries = self._read()
        heartbeats = [e for e in entries if e.get("kind") == "heartbeat"]
        drop = set()
        size = self.path.stat().st_size
        for entry in heartbeats + [e for e in entries if e.get("kind") != "heartbeat"]:
            if size <= self.max_bytes * 0.8:
                break
            drop.add(entry["seq"])
            size -= len(json.dumps(entry, ensure_ascii=False, default=str)) + 1
        logger.warning("Health log over %s bytes; dropped %s oldest line(s)",
                       self.max_bytes, len(drop))
        self._rewrite([e for e in entries if e["seq"] not in drop])

    def _highest_seq(self) -> int:
        entries = self._read()
        return max((e["seq"] for e in entries), default=0)


__all__ = ["HealthLog", "FILE_NAME"]
