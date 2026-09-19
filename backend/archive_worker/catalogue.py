"""
The AIMS LAB server's own record of what it holds.

Neon is the system of record for sessions, chains and receipts. It is also in
someone else's data centre, reachable only over the internet. If that link is
down - or the cloud account is lost entirely - the hospital still has the audio
on its own disks and must still be able to answer:

    which consultations do we hold, for which patient, on which date,
    where is the file, and what should its hash be?

This is that answer. A single SQLite file beside the archive root, written by the
worker as each session lands, queryable with any SQLite tool and no network.

It is deliberately a mirror, not a second source of truth. It is rebuilt from
Neon if lost, and it never gates archiving: a catalogue failure is logged, and
the audio is still written. Losing the index is recoverable; losing the audio is
not.
"""
from __future__ import annotations

import logging
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS archived_sessions (
    session_id        TEXT PRIMARY KEY,
    patient_id        TEXT NOT NULL,
    doctor_id         TEXT NOT NULL,
    hospital_id       TEXT NOT NULL,
    session_date      TEXT NOT NULL,

    -- Relative to the archive root, never absolute: the volume gets remounted,
    -- moved to bigger disks, restored to a different path.
    archive_relpath   TEXT NOT NULL UNIQUE,
    filename          TEXT NOT NULL,
    sha256            TEXT NOT NULL,
    bytes             INTEGER NOT NULL,

    opened_at         TEXT,
    closed_at         TEXT,
    duration_seconds  REAL,
    paused_seconds    REAL,
    segment_count     INTEGER NOT NULL,

    archived_at       TEXT NOT NULL,
    reported_at       TEXT,
    verified_at       TEXT
);

CREATE INDEX IF NOT EXISTS idx_cat_patient  ON archived_sessions(patient_id, session_date DESC);
CREATE INDEX IF NOT EXISTS idx_cat_doctor   ON archived_sessions(hospital_id, doctor_id, session_date DESC);
CREATE INDEX IF NOT EXISTS idx_cat_date     ON archived_sessions(session_date DESC);
CREATE INDEX IF NOT EXISTS idx_cat_unreported ON archived_sessions(reported_at) WHERE reported_at IS NULL;

CREATE TABLE IF NOT EXISTS archived_clips (
    session_id   TEXT NOT NULL,
    seq_no       INTEGER NOT NULL,
    clip_name    TEXT,
    sha256       TEXT NOT NULL,
    bytes        INTEGER NOT NULL,
    PRIMARY KEY (session_id, seq_no)
);

-- Why the audio stops and starts again.
--
-- A gap in a consultation recording is the thing an auditor asks about, so the
-- reason travels with the audio rather than living only in the cloud. Copied
-- from the signed chain entries: this is what the device attested at the time,
-- not a summary written afterwards.
CREATE TABLE IF NOT EXISTS archived_pauses (
    session_id     TEXT NOT NULL,
    entry_no       INTEGER NOT NULL,
    entry_type     TEXT NOT NULL,          -- 'pause' or 'resume'
    occurred_at    TEXT,
    reason         TEXT,
    authorised_by  TEXT,
    seconds        REAL,
    PRIMARY KEY (session_id, entry_no)
);

CREATE INDEX IF NOT EXISTS idx_cat_pause_session ON archived_pauses(session_id, entry_no);
"""


class Catalogue:
    """
    SQLite index of the archive. One connection, opened per call.

    WAL so a reader (someone running a report) never blocks the worker writing
    the next session.
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as conn:
            conn.executescript(SCHEMA)
            conn.commit()
        logger.info("Archive catalogue at %s", self.path)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        return conn

    def record(
        self,
        *,
        session: Dict[str, Any],
        archive_relpath: str,
        filename: str,
        sha256_hex: str,
        byte_length: int,
        segments: List[Dict[str, Any]],
        session_date: str,
        pauses: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        """Write one archived session. Idempotent: a re-run overwrites its own row."""
        now = datetime.now(timezone.utc).isoformat()
        with closing(self._connect()) as conn:
            with conn:
                conn.execute("""
                    INSERT INTO archived_sessions
                        (session_id, patient_id, doctor_id, hospital_id, session_date,
                         archive_relpath, filename, sha256, bytes, opened_at, closed_at,
                         duration_seconds, paused_seconds, segment_count, archived_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(session_id) DO UPDATE SET
                        archive_relpath = excluded.archive_relpath,
                        filename        = excluded.filename,
                        sha256          = excluded.sha256,
                        bytes           = excluded.bytes,
                        segment_count   = excluded.segment_count,
                        archived_at     = excluded.archived_at
                """, (
                    session["session_id"], str(session.get("patient_ref") or ""),
                    str(session.get("doctor_id") or ""), str(session.get("hospital_id") or ""),
                    session_date, archive_relpath, filename, sha256_hex, byte_length,
                    session.get("opened_at"), session.get("closed_at"),
                    session.get("duration_seconds"), session.get("paused_seconds"),
                    len(segments), now,
                ))
                for s in segments:
                    conn.execute("""
                        INSERT INTO archived_clips (session_id, seq_no, clip_name, sha256, bytes)
                        VALUES (?,?,?,?,?)
                        ON CONFLICT(session_id, seq_no) DO UPDATE SET
                            clip_name = excluded.clip_name
                    """, (session["session_id"], s["seq_no"], s.get("clip_name"),
                          s["sha256"], s["bytes"]))

                for p in (pauses or []):
                    conn.execute("""
                        INSERT INTO archived_pauses
                            (session_id, entry_no, entry_type, occurred_at,
                             reason, authorised_by, seconds)
                        VALUES (?,?,?,?,?,?,?)
                        ON CONFLICT(session_id, entry_no) DO UPDATE SET
                            reason        = excluded.reason,
                            authorised_by = excluded.authorised_by,
                            seconds       = excluded.seconds
                    """, (session["session_id"], p.get("entry_no"), p.get("entry_type"),
                          p.get("occurred_at"), p.get("reason"),
                          p.get("authorised_by"), p.get("seconds")))

    def mark_reported(self, session_id: str) -> None:
        """The backend acknowledged this archive; receipts were issued."""
        now = datetime.now(timezone.utc).isoformat()
        with closing(self._connect()) as conn:
            with conn:
                conn.execute(
                    "UPDATE archived_sessions SET reported_at = ? WHERE session_id = ?",
                    (now, session_id))

    def unreported(self) -> List[Dict[str, Any]]:
        """
        Archived on disk but never acknowledged by the backend.

        These are the sessions whose agents are still holding local audio, because
        no receipt was ever issued. Worth watching: it is the one state where
        doctor PCs quietly fill up.
        """
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT * FROM archived_sessions WHERE reported_at IS NULL "
                "ORDER BY archived_at").fetchall()
        return [dict(r) for r in rows]

    def find(
        self, *, patient_id: Optional[str] = None, doctor_id: Optional[str] = None,
        hospital_id: Optional[str] = None, session_date: Optional[str] = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """The question the hospital actually asks: where is this patient's audio?"""
        where, params = [], []
        for column, value in (("patient_id", patient_id), ("doctor_id", doctor_id),
                              ("hospital_id", hospital_id), ("session_date", session_date)):
            if value:
                where.append(f"{column} = ?")
                params.append(value)
        clause = ("WHERE " + " AND ".join(where)) if where else ""
        params.append(limit)
        with closing(self._connect()) as conn:
            rows = conn.execute(
                f"SELECT * FROM archived_sessions {clause} "
                f"ORDER BY session_date DESC, opened_at DESC LIMIT ?", params).fetchall()
        return [dict(r) for r in rows]

    def pauses_for(self, session_id: str) -> List[Dict[str, Any]]:
        """Every gap in one consultation, with the reason the device signed."""
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT * FROM archived_pauses WHERE session_id = ? ORDER BY entry_no",
                (session_id,)).fetchall()
        return [dict(r) for r in rows]

    def totals(self) -> Dict[str, Any]:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT count(*) AS sessions, COALESCE(sum(bytes),0) AS bytes, "
                "       COALESCE(sum(segment_count),0) AS clips, "
                "       count(*) FILTER (WHERE reported_at IS NULL) AS unreported "
                "FROM archived_sessions").fetchone()
        return dict(row)


__all__ = ["Catalogue"]
