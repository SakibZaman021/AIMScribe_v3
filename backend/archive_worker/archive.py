"""
Archive file handling: safe paths, WAV joining, and verification.

Everything here is deliberately paranoid about the filesystem, because the inputs
(hospital, doctor, patient identifiers) originate from clients. v1's AIMS LAB
server joined a client-supplied patient_id straight into a path, which allowed
writing anywhere on the volume.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import wave
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
ULID_PATTERN = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")
DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# The consultation folder: the audio file's name without its extension. Longer
# than an identifier because it carries every field, but the same character set.
FOLDER_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,160}$")

# Canonical PCM WAV header written by the agent. Fixed size, no extra chunks.
WAV_HEADER_BYTES = 44


class ArchiveError(RuntimeError):
    """Something about this session cannot be archived safely. Never force it."""


def sha256_file(path: Path, chunk: int = 1 << 20) -> bytes:
    h = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(chunk), b""):
            h.update(block)
    return h.digest()


def sha256_bytes(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()


# ============================================================
# Paths
# ============================================================

def _checked(value: str, pattern: re.Pattern, field: str) -> str:
    if not isinstance(value, str) or not pattern.match(value):
        raise ArchiveError(f"unsafe {field}: {value!r}")
    return value


def session_directory(root: Path, hospital_id: str, doctor_id: str,
                      session_date: str, folder_name: str) -> Path:
    """
    Build <root>/<HOSPITAL>/<DOCTOR>/<YYYY-MM-DD>/<CONSULTATION> and prove it
    stays under root.

    The last level is the full consultation name, the same string as the audio
    file without its extension:

        Hos001/DR001/2026-07-28/145_DR001_Hos001_10_30_10_45_2026_07_28/

    Named for the consultation rather than the patient because a patient can be
    seen twice in a day - a follow-up, a second opinion - and a patient-only
    folder would merge two consultations into one directory. The times make it
    unique.

    Two independent defences: a strict allowlist pattern per component, and a
    resolved-path containment check. Either alone would probably do; together they
    survive one of them being wrong. Every component reaches a filesystem path and
    every one of them originates from a client.
    """
    _checked(hospital_id, ID_PATTERN, "hospital_id")
    _checked(doctor_id, ID_PATTERN, "doctor_id")
    _checked(session_date, DATE_PATTERN, "session_date")
    _checked(folder_name, FOLDER_PATTERN, "folder_name")

    root = root.resolve()
    target = (root / hospital_id / doctor_id / session_date / folder_name).resolve()

    if not _is_within(target, root):
        raise ArchiveError(f"resolved path escapes the archive root: {target}")
    return target


def _is_within(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def archive_filename(
    *, patient_ref: str, doctor_id: str, hospital_id: str,
    opened_at: datetime, closed_at: datetime
) -> str:
    """
    `{patient}_{doctor}_{hospital}_{HH}_{MM}_{HH}_{MM}_{YYYY}_{MM}_{DD}.wav`

        145_DR001_Hos001_10_30_10_45_2026_07_28.wav

    Times are the hospital's local clock, and they are what keeps the name unique:
    without them a second consultation for the same patient on the same day would
    collide. The session ULID stays the database key but is deliberately absent
    here, because a name nobody can read is a name nobody can audit.

    Every field is underscore-separated by request. That makes the name easy to
    read and awkward to parse - the start time is fields 4 and 5, the end time 6
    and 7 - so anything that needs the times should read sessions.start_time and
    sessions.end_time rather than taking them apart again.
    """
    _checked(patient_ref, ID_PATTERN, "patient_ref")
    _checked(doctor_id, ID_PATTERN, "doctor_id")
    _checked(hospital_id, ID_PATTERN, "hospital_id")
    return (f"{patient_ref}_{doctor_id}_{hospital_id}"
            f"_{opened_at.strftime('%H_%M')}"
            f"_{closed_at.strftime('%H_%M')}"
            f"_{opened_at.strftime('%Y_%m_%d')}.wav")


def expected_join_bytes(segment_bytes: List[int]) -> int:
    """
    Size the joined WAV will have: one 44-byte header plus every segment's audio.

    Used to tell "my own file from a run that died before reporting" apart from
    "a different session that happens to produce the same name". The old names
    carried the session ULID and could not collide; these are human-readable, so
    the check has to be explicit.
    """
    return WAV_HEADER_BYTES + sum(max(0, int(b) - WAV_HEADER_BYTES) for b in segment_bytes)


def free_destination(directory: Path, filename: str, expected_bytes: int) -> Tuple[Path, bool]:
    """
    Resolve where this session's audio goes.

    Returns (path, is_ours). `is_ours` means the file is already there at exactly
    the size this session would produce - an interrupted run, safe to re-report.
    Anything else at that name is a genuine collision, so we step to `_02`, `_03`
    rather than overwrite another patient's consultation.
    """
    candidate = directory / filename
    if not candidate.exists():
        return candidate, False
    if candidate.stat().st_size == expected_bytes:
        return candidate, True

    stem, suffix = filename.rsplit(".", 1)
    for n in range(2, 100):
        candidate = directory / f"{stem}_{n:02d}.{suffix}"
        if not candidate.exists():
            logger.warning(
                "Archive name %s is taken by a different session; using %s instead",
                filename, candidate.name)
            return candidate, False
        if candidate.stat().st_size == expected_bytes:
            return candidate, True
    raise ArchiveError(f"could not find a free archive name for {filename}")


def relative_path(hospital_id: str, doctor_id: str, session_date: str,
                  folder_name: str, filename: str) -> str:
    """
    Stored in the database, always relative to the archive root.

    An absolute path would break every row the day the volume is remounted or the
    archive moves to bigger disks.
    """
    return f"{hospital_id}/{doctor_id}/{session_date}/{folder_name}/{filename}"


def local_times(
    opened_at_iso: Optional[str], closed_at_iso: Optional[str], tz_name: str
) -> Tuple[datetime, datetime]:
    """Convert the backend's UTC timestamps to the hospital's wall clock."""
    opened = _parse(opened_at_iso)
    closed = _parse(closed_at_iso) or opened
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(tz_name)
    except Exception:
        # Not cosmetic: at UTC+6 this shifts evening consultations into the
        # previous day's folder. Install the tzdata package.
        logger.error(
            "Timezone %r could not be resolved - falling back to UTC. File names "
            "and folder dates will be wrong for any hospital not on UTC. "
            "Install tzdata (pip install tzdata).", tz_name)
        tz = timezone.utc
    return opened.astimezone(tz), closed.astimezone(tz)


def _parse(value: Optional[str]) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(timezone.utc)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


# ============================================================
# WAV joining
# ============================================================

@dataclass
class JoinResult:
    path: Path
    sha256: bytes
    bytes: int
    duration_seconds: float
    frames: int


def join_wav(segment_paths: List[Path], destination: Path) -> JoinResult:
    """
    Concatenate segments into one WAV, then verify the result from disk.

    Written to a temporary file and renamed, so a crash mid-write never leaves a
    half-session that looks complete. The hash is computed by re-reading the file
    rather than from the buffer we just wrote - that is what proves the bytes
    actually reached the disk intact.
    """
    if not segment_paths:
        raise ArchiveError("no segments to join")

    tmp = destination.with_suffix(destination.suffix + ".partial")
    params: Optional[Tuple[int, int, int]] = None
    frames = 0

    try:
        with wave.open(str(tmp), "wb") as out:
            for index, path in enumerate(segment_paths):
                with wave.open(str(path), "rb") as src:
                    current = (src.getnchannels(), src.getsampwidth(), src.getframerate())
                    if params is None:
                        params = current
                        out.setnchannels(current[0])
                        out.setsampwidth(current[1])
                        out.setframerate(current[2])
                    elif current != params:
                        raise ArchiveError(
                            f"segment {index + 1} has audio parameters {current}, "
                            f"expected {params}; refusing to join mismatched audio")

                    while True:
                        chunk = src.readframes(65536)
                        if not chunk:
                            break
                        out.writeframes(chunk)
                        frames += len(chunk) // (params[0] * params[1])

        # Force to disk before we claim it exists.
        with open(tmp, "rb+") as handle:
            handle.flush()
            os.fsync(handle.fileno())

        os.replace(tmp, destination)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise

    assert params is not None
    size = destination.stat().st_size
    return JoinResult(
        path=destination,
        sha256=sha256_file(destination),
        bytes=size,
        duration_seconds=frames / params[2] if params[2] else 0.0,
        frames=frames,
    )


# ============================================================
# Manifest and daily index
# ============================================================

def write_manifest(audio_path: Path, session: Dict[str, Any], result: JoinResult) -> Path:
    """
    A self-describing record beside the audio.

    If the database is ever lost, the archive is still verifiable: every segment
    hash, the chain, the device that recorded it, and the consent record.
    """
    manifest = {
        "session_id": session["session_id"],
        "hospital_id": session["hospital_id"],
        "doctor_id": session["doctor_id"],
        "patient_ref": session["patient_ref"],
        "session_date": session["session_date"],
        "opened_at": session["opened_at"],
        "closed_at": session["closed_at"],
        "timezone": session.get("timezone"),
        "audio": {"codec": "pcm_s16le", "container": "wav", **(session.get("audio") or {})},
        "archive": {
            "filename": audio_path.name,
            "bytes": result.bytes,
            "sha256": result.sha256.hex(),
            "duration_seconds": round(result.duration_seconds, 3),
            "archived_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        },
        "segments": [
            {"seq_no": s["seq_no"], "sha256": s["sha256"],
             "bytes": s["bytes"], "duration_seconds": s["duration_seconds"]}
            for s in session.get("segments", [])
        ],
        # The agent's manifest carries the full signed chain.
        "agent_manifest": session.get("manifest"),
    }

    path = audio_path.with_suffix(".manifest.json")
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)
    return path


def update_day_index(directory: Path) -> Path:
    """
    Rebuild `_index.json` so a day's folder can be read without the database.

    Regenerated from what is actually on disk, not from what we think we wrote.
    """
    entries = []
    for manifest_path in sorted(directory.glob("*.manifest.json")):
        try:
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        entries.append({
            "session_id": data.get("session_id"),
            "patient_ref": data.get("patient_ref"),
            "file": data.get("archive", {}).get("filename"),
            "opened_at": data.get("opened_at"),
            "duration_seconds": data.get("archive", {}).get("duration_seconds"),
            "sha256": data.get("archive", {}).get("sha256"),
        })

    index = {
        "directory": directory.name,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "session_count": len(entries),
        "sessions": entries,
    }
    path = directory / "_index.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(index, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)
    return path


# ============================================================
# Capacity
# ============================================================

def free_bytes(path: Path) -> int:
    return shutil.disk_usage(path).free


def ensure_space(root: Path, needed: int, *, headroom: int) -> None:
    """
    Refuse to start a session we cannot finish.

    Filling the archive volume is worse than delaying: a partially written
    archive with no space to complete it blocks every later session too.
    """
    available = free_bytes(root)
    if available < needed + headroom:
        raise ArchiveError(
            f"insufficient disk space: {available // 1024 // 1024} MB free, "
            f"need {(needed + headroom) // 1024 // 1024} MB")


__all__ = [
    "ArchiveError", "JoinResult", "archive_filename",
    "expected_join_bytes", "free_destination", "ensure_space", "free_bytes",
    "join_wav", "local_times", "relative_path", "session_directory",
    "sha256_bytes", "sha256_file", "update_day_index", "write_manifest",
]
