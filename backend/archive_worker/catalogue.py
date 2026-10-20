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
    verified_at       TEXT,

    -- The lossless cloud copy (SRS-ARC-09). Until this is filled in, this
    -- volume and the pieces in the segment bucket are the only copies.
    copied_at         TEXT,
    copy_object_key   TEXT,

    -- When the WAV left this disk, its copy proven. The row, the JSON and the
    -- manifest stay; the audio is then only in the copy bucket.
    local_released_at TEXT
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

-- One recording that held more than one consultation - a follow-up with no
-- prescription, then the next patient - is archived as one file per patient.
-- The session row above then describes the whole recording (its sha256 is the
-- joined audio's, the one the chain vouches for) and points at part 1; each
-- patient's file is a row here. A single-consultation recording has no rows.
CREATE TABLE IF NOT EXISTS archived_parts (
    session_id        TEXT NOT NULL,
    part_no           INTEGER NOT NULL,
    patient_id        TEXT NOT NULL,
    doctor_id         TEXT NOT NULL,
    archive_relpath   TEXT NOT NULL UNIQUE,
    filename          TEXT NOT NULL,
    sha256            TEXT NOT NULL,
    bytes             INTEGER NOT NULL,
    start_seconds     REAL,
    end_seconds       REAL,
    start_frame       INTEGER,
    end_frame         INTEGER,
    rule              TEXT,
    needs_review      INTEGER NOT NULL DEFAULT 0,
    archived_at       TEXT NOT NULL,
    copied_at         TEXT,
    copy_object_key   TEXT,
    local_released_at TEXT,
    PRIMARY KEY (session_id, part_no)
);

CREATE INDEX IF NOT EXISTS idx_cat_part_patient ON archived_parts(patient_id);
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
            # A catalogue written before the cloud copy existed has no columns
            # for it. Adding them here keeps an in-place upgrade to one step.
            existing = {row["name"] for row in
                        conn.execute("PRAGMA table_info(archived_sessions)")}
            for column in ("copied_at", "copy_object_key", "local_released_at"):
                if column not in existing:
                    conn.execute(f"ALTER TABLE archived_sessions ADD COLUMN {column} TEXT")
            if "parts_count" not in existing:
                conn.execute("ALTER TABLE archived_sessions "
                             "ADD COLUMN parts_count INTEGER NOT NULL DEFAULT 1")
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
        parts_count: int = 1,
    ) -> None:
        """Write one archived session. Idempotent: a re-run overwrites its own row."""
        now = datetime.now(timezone.utc).isoformat()
        with closing(self._connect()) as conn:
            with conn:
                conn.execute("""
                    INSERT INTO archived_sessions
                        (session_id, patient_id, doctor_id, hospital_id, session_date,
                         archive_relpath, filename, sha256, bytes, opened_at, closed_at,
                         duration_seconds, paused_seconds, segment_count, archived_at,
                         parts_count)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(session_id) DO UPDATE SET
                        archive_relpath = excluded.archive_relpath,
                        filename        = excluded.filename,
                        sha256          = excluded.sha256,
                        bytes           = excluded.bytes,
                        segment_count   = CASE WHEN excluded.segment_count > 0
                                               THEN excluded.segment_count
                                               ELSE archived_sessions.segment_count END,
                        archived_at     = excluded.archived_at,
                        parts_count     = excluded.parts_count
                """, (
                    session["session_id"], str(session.get("patient_ref") or ""),
                    str(session.get("doctor_id") or ""), str(session.get("hospital_id") or ""),
                    session_date, archive_relpath, filename, sha256_hex, byte_length,
                    session.get("opened_at"), session.get("closed_at"),
                    session.get("duration_seconds"), session.get("paused_seconds"),
                    len(segments), now, max(1, int(parts_count)),
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

    # ---- parts: one row per patient's file ----

    def record_part(self, session_id: str, part: Dict[str, Any]) -> None:
        """One consultation's file, cut from a longer recording. Idempotent."""
        now = datetime.now(timezone.utc).isoformat()
        with closing(self._connect()) as conn:
            with conn:
                # A recut can hand a part's name to another part number; the
                # row that held that file before is replaced, not duplicated.
                conn.execute("DELETE FROM archived_parts WHERE archive_relpath = ? "
                             "AND NOT (session_id = ? AND part_no = ?)",
                             (part["archive_relpath"], session_id, part["part_no"]))
                conn.execute("""
                    INSERT INTO archived_parts
                        (session_id, part_no, patient_id, doctor_id, archive_relpath,
                         filename, sha256, bytes, start_seconds, end_seconds,
                         start_frame, end_frame, rule, needs_review, archived_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(session_id, part_no) DO UPDATE SET
                        patient_id = excluded.patient_id, doctor_id = excluded.doctor_id,
                        archive_relpath = excluded.archive_relpath,
                        filename = excluded.filename, sha256 = excluded.sha256,
                        bytes = excluded.bytes, start_seconds = excluded.start_seconds,
                        end_seconds = excluded.end_seconds,
                        start_frame = excluded.start_frame, end_frame = excluded.end_frame,
                        rule = excluded.rule, needs_review = excluded.needs_review,
                        archived_at = excluded.archived_at,
                        copied_at = CASE WHEN archived_parts.sha256 = excluded.sha256
                                         THEN archived_parts.copied_at END,
                        copy_object_key = CASE WHEN archived_parts.sha256 = excluded.sha256
                                               THEN archived_parts.copy_object_key END,
                        local_released_at = NULL
                """, (session_id, part["part_no"], str(part.get("patient_id") or ""),
                      str(part.get("doctor_id") or ""), part["archive_relpath"],
                      part["filename"], part["sha256"], part["bytes"],
                      part.get("start_seconds"), part.get("end_seconds"),
                      part.get("start_frame"), part.get("end_frame"), part.get("rule"),
                      1 if part.get("needs_review") else 0, now))

    def forget(self, session_id: str) -> None:
        """A refused recording leaves nothing here that names the patient."""
        with closing(self._connect()) as conn:
            for table in ("archived_parts", "archived_pauses", "archived_clips",
                          "archived_sessions"):
                conn.execute(f"DELETE FROM {table} WHERE session_id = ?", (session_id,))
            conn.commit()

    def session_row(self, session_id: str) -> Optional[Dict[str, Any]]:
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT * FROM archived_sessions WHERE session_id = ?",
                               (session_id,)).fetchone()
        return dict(row) if row else None

    def parts_for(self, session_id: str) -> List[Dict[str, Any]]:
        with closing(self._connect()) as conn:
            rows = conn.execute("SELECT * FROM archived_parts WHERE session_id = ? "
                                "ORDER BY part_no", (session_id,)).fetchall()
        return [dict(r) for r in rows]

    def drop_parts_after(self, session_id: str, last_part_no: int) -> None:
        """A recut left fewer parts than before."""
        with closing(self._connect()) as conn:
            with conn:
                conn.execute("DELETE FROM archived_parts WHERE session_id = ? "
                             "AND part_no > ?", (session_id, last_part_no))

    def mark_part_copied(self, session_id: str, part_no: int, object_key: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with closing(self._connect()) as conn:
            with conn:
                conn.execute("UPDATE archived_parts SET copied_at = ?, copy_object_key = ? "
                             "WHERE session_id = ? AND part_no = ?",
                             (now, object_key, session_id, part_no))

    def mark_part_released(self, session_id: str, part_no: int, released_at: str) -> None:
        with closing(self._connect()) as conn:
            with conn:
                conn.execute("UPDATE archived_parts SET local_released_at = ? "
                             "WHERE session_id = ? AND part_no = ?",
                             (released_at, session_id, part_no))

    def mark_uncopied(self, session_id: str) -> None:
        """A recut made new files: the copies of the old ones no longer stand for them."""
        with closing(self._connect()) as conn:
            with conn:
                conn.execute("UPDATE archived_sessions SET copied_at = NULL, "
                             "copy_object_key = NULL, local_released_at = NULL "
                             "WHERE session_id = ?", (session_id,))

    def mark_reported(self, session_id: str) -> None:
        """The backend acknowledged this archive; receipts were issued."""
        now = datetime.now(timezone.utc).isoformat()
        with closing(self._connect()) as conn:
            with conn:
                conn.execute(
                    "UPDATE archived_sessions SET reported_at = ? WHERE session_id = ?",
                    (now, session_id))

    def mark_copied(self, session_id: str, object_key: str) -> None:
        """
        The lossless cloud copy of this recording exists and was verified
        (SRS-ARC-09). Kept here too, so "is this recording safe anywhere but
        this volume?" can be answered on this machine with no network.
        """
        now = datetime.now(timezone.utc).isoformat()
        with closing(self._connect()) as conn:
            with conn:
                conn.execute("UPDATE archived_sessions SET copied_at = ?, "
                             "copy_object_key = ? WHERE session_id = ?",
                             (now, object_key, session_id))

    def releasable(self, archived_before: str) -> List[Dict[str, Any]]:
        """
        Copied, proven, archived before the cutoff, and still on this disk.
        Oldest first, so the oldest audio leaves first.
        """
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT * FROM archived_sessions WHERE copied_at IS NOT NULL "
                "AND copy_object_key IS NOT NULL AND local_released_at IS NULL "
                "AND archived_at <= ? ORDER BY archived_at",
                (archived_before,)).fetchall()
        return [dict(r) for r in rows]

    def mark_released(self, session_id: str, released_at: str) -> None:
        """The WAV is gone from this disk; the copy bucket holds the audio."""
        with closing(self._connect()) as conn:
            with conn:
                conn.execute("UPDATE archived_sessions SET local_released_at = ? "
                             "WHERE session_id = ?", (released_at, session_id))

    def uncopied(self) -> List[Dict[str, Any]]:
        """Archived here, with no cloud copy yet: still only one copy in the world."""
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT * FROM archived_sessions WHERE copied_at IS NULL "
                "ORDER BY archived_at").fetchall()
        return [dict(r) for r in rows]

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
