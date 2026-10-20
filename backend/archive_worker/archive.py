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

# Patient audio and clinical records are readable by this worker's user and no
# one else on the machine. The worker also sets umask 077 at start, so files
# made by libraries (wave, sqlite) are private too; these are the explicit
# belt to that braces, for files written before the umask applied.
PRIVATE_FILE = 0o600
PRIVATE_DIR = 0o700


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
# Permissions and removal
# ============================================================

def make_private(path: Path, mode: int = PRIVATE_FILE) -> None:
    """Owner-only, where the filesystem has POSIX modes. Best effort elsewhere."""
    try:
        os.chmod(path, mode)
    except OSError as exc:
        logger.debug("Could not restrict %s: %s", path.name, exc)


def make_dirs(directory: Path) -> Path:
    """mkdir -p, every level it creates owner-only."""
    missing = []
    current = directory
    while not current.exists() and current != current.parent:
        missing.append(current)
        current = current.parent
    directory.mkdir(parents=True, exist_ok=True, mode=PRIVATE_DIR)
    for created in missing:
        make_private(created, PRIVATE_DIR)
    return directory


def tighten_tree(root: Path) -> int:
    """
    Make everything already under the archive root owner-only: files archived
    before files were written private. Once per volume - a marker records it.
    Returns how many entries were changed.
    """
    marker = root / ".permissions_v1"
    if marker.exists():
        return 0
    changed = 0
    for current, dirs, files in os.walk(root):
        for name in dirs:
            make_private(Path(current) / name, PRIVATE_DIR)
            changed += 1
        for name in files:
            make_private(Path(current) / name, PRIVATE_FILE)
            changed += 1
    marker.write_text("archive files made owner-only\n", encoding="utf-8")
    make_private(marker)
    return changed


def remove_securely(path: Path) -> bool:
    """
    Overwrite a small file with zeros, force that to disk, then delete it, so
    the record is not left readable in the freed blocks. On copy-on-write or
    flash storage the overwrite may land elsewhere; the volume's own encryption
    covers that case. Returns whether a file was removed.
    """
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        return False
    try:
        with open(path, "r+b") as handle:
            remaining = size
            while remaining > 0:
                step = min(remaining, 1 << 20)
                handle.write(b"\0" * step)
                remaining -= step
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as exc:
        logger.warning("Could not overwrite %s before removing it: %s", path.name, exc)
    path.unlink(missing_ok=True)
    return True


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


def name_for(session: Dict[str, Any], opened_local: datetime,
             closed_local: datetime) -> str:
    """
    The recording's file name.

    The server names every recording when it closes (`SRS-SES-05`) and stores
    that name against the visit in the clinical database, so the audio, the
    JSON beside it and the clinical record all carry the same name. The worker
    uses it as given and derives nothing.

    Only a session that closed before the server did this - or one from a
    protocol-2 recorder - has no name, and then the older form is built here.
    """
    stem = session.get("file_stem")
    if isinstance(stem, str) and FOLDER_PATTERN.match(stem):
        return f"{stem}.wav"
    if stem:
        raise ArchiveError(f"unsafe file_stem: {stem!r}")
    return archive_filename(
        patient_ref=session["patient_ref"], doctor_id=session["doctor_id"],
        hospital_id=session["hospital_id"],
        opened_at=opened_local, closed_at=closed_local)


def expected_join_bytes(segment_bytes: List[int]) -> int:
    """
    Size the joined WAV will have: one 44-byte header plus every segment's audio.

    Used to tell "my own file from a run that died before reporting" apart from
    "a different session that happens to produce the same name". The old names
    carried the session ULID and could not collide; these are human-readable, so
    the check has to be explicit.
    """
    return WAV_HEADER_BYTES + sum(max(0, int(b) - WAV_HEADER_BYTES) for b in segment_bytes)


def free_destination(directory: Path, filename: str, expected_bytes: int, *,
                     session_id: Optional[str] = None,
                     segment_hashes: Optional[List[Tuple[int, str]]] = None
                     ) -> Tuple[Path, bool]:
    """
    Resolve where this session's audio goes.

    Returns (path, is_ours). `is_ours` means the file already there is this
    session's - an interrupted run, safe to re-report. With `session_id` that
    is decided by the manifest beside it (same session, and with
    `segment_hashes` the same pieces in the same order), not by the size:
    with fixed-length pieces two consultations can produce files of the same
    size, and a size match used to re-report another recording's file as this
    one's. Without `session_id` the size alone decides, as before. Anything
    else at that name is a genuine collision, so we step to `_02`, `_03`
    rather than overwrite another patient's consultation.
    """
    def ours(path: Path) -> bool:
        if path.stat().st_size != expected_bytes:
            return False
        if session_id is None:
            return True
        return _manifest_matches(path, session_id, segment_hashes)

    candidate = directory / filename
    if not candidate.exists():
        return candidate, False
    if ours(candidate):
        return candidate, True

    stem, suffix = filename.rsplit(".", 1)
    for n in range(2, 100):
        candidate = directory / f"{stem}_{n:02d}.{suffix}"
        if not candidate.exists():
            logger.warning(
                "Archive name %s is taken by a different session; using %s instead",
                filename, candidate.name)
            return candidate, False
        if ours(candidate):
            return candidate, True
    raise ArchiveError(f"could not find a free archive name for {filename}")


def _manifest_matches(audio_path: Path, session_id: str,
                      segment_hashes: Optional[List[Tuple[int, str]]]) -> bool:
    """The manifest beside this file names this session (and these pieces)."""
    try:
        manifest = json.loads(audio_path.with_suffix(".manifest.json")
                              .read_text(encoding="utf-8"))
    except Exception:
        return False
    if manifest.get("session_id") != session_id:
        return False
    if segment_hashes is None:
        return True
    held = [(int(s.get("seq_no")), str(s.get("sha256", "")).lower())
            for s in manifest.get("segments") or []]
    return held == [(int(n), str(h).lower()) for n, h in segment_hashes]


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
        make_private(tmp)

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
# Cutting one recording into consultations
# ============================================================
#
# A recording that held more than one patient - a follow-up with no
# prescription, then the next patient - is archived as one file per patient.
# The server decides where each consultation begins (in seconds of the joined
# audio); here those seconds become exact frame positions and the joined file
# is cut there. The parts are contiguous and together are every frame of the
# recording exactly once, so joining them again gives back the joined WAV
# byte for byte - which is how a recut proves it is cutting the audio the
# chain describes.

@dataclass
class WavFormat:
    channels: int
    sample_width: int
    rate: int
    frames: int

    @property
    def frame_bytes(self) -> int:
        return self.channels * self.sample_width


def wav_format(path: Path) -> WavFormat:
    with wave.open(str(path), "rb") as src:
        return WavFormat(src.getnchannels(), src.getsampwidth(), src.getframerate(),
                         src.getnframes())


def part_frames(starts: List[float], rate: int, total_frames: int) -> List[Tuple[int, int]]:
    """
    Frame ranges [start, end) for parts beginning at `starts` seconds.

    Only the starts are used: part n ends exactly where part n+1 begins, the
    first part begins at frame 0 and the last ends at the last frame, whatever
    the seconds say. That is what makes it impossible for a cut to drop or
    repeat a single sample. A start that is not after the one before it, or
    that is not inside the recording, means the server's parts do not fit this
    audio - refused, never guessed at.
    """
    if not starts:
        raise ArchiveError("no parts to cut")
    bounds = [0]
    for index, seconds in enumerate(starts[1:], start=2):
        try:
            frame = int(round(float(seconds) * rate))
        except (TypeError, ValueError):
            raise ArchiveError(f"part {index} has no usable start: {seconds!r}")
        if frame <= bounds[-1] or frame >= total_frames:
            raise ArchiveError(
                f"part {index} starts at {seconds}s, which does not fit a "
                f"{total_frames / rate:.3f}s recording after the part before it")
        bounds.append(frame)
    bounds.append(total_frames)
    return [(bounds[i], bounds[i + 1]) for i in range(len(starts))]


def part_bytes(frames: int, fmt: WavFormat) -> int:
    """Size of a part's WAV: one header plus its frames."""
    return WAV_HEADER_BYTES + frames * fmt.frame_bytes


def cut_wav(source: Path, start_frame: int, end_frame: int,
            destination: Path) -> JoinResult:
    """
    Frames [start_frame, end_frame) of `source` as their own WAV, written to a
    temporary file, forced to disk, renamed, and hashed from the disk - the same
    care as join_wav.
    """
    tmp = destination.with_suffix(destination.suffix + ".partial")
    try:
        with wave.open(str(source), "rb") as src:
            params = (src.getnchannels(), src.getsampwidth(), src.getframerate())
            if not 0 <= start_frame < end_frame <= src.getnframes():
                raise ArchiveError(f"cut {start_frame}-{end_frame} is outside the "
                                   f"recording ({src.getnframes()} frames)")
            src.setpos(start_frame)
            with wave.open(str(tmp), "wb") as out:
                out.setnchannels(params[0])
                out.setsampwidth(params[1])
                out.setframerate(params[2])
                remaining = end_frame - start_frame
                while remaining > 0:
                    chunk = src.readframes(min(65536, remaining))
                    if not chunk:
                        raise ArchiveError("the recording ended before the cut did")
                    out.writeframes(chunk)
                    remaining -= len(chunk) // (params[0] * params[1])
        with open(tmp, "rb+") as handle:
            handle.flush()
            os.fsync(handle.fileno())
        make_private(tmp)
        os.replace(tmp, destination)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise

    frames = end_frame - start_frame
    return JoinResult(path=destination, sha256=sha256_file(destination),
                      bytes=destination.stat().st_size,
                      duration_seconds=frames / params[2] if params[2] else 0.0,
                      frames=frames)


# ============================================================
# Manifest and daily index
# ============================================================

def write_manifest(audio_path: Path, session: Dict[str, Any], result: JoinResult,
                   derived_from: Optional[Dict[str, Any]] = None) -> Path:
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
    if derived_from:
        # One consultation cut from a longer recording: which recording, which
        # frames, and the fingerprint of the whole joined audio, so the cut can
        # be checked against the chain without the database.
        manifest["derived_from"] = derived_from
    # A consultation that a restart split into two recordings: each names the
    # other, so the two files are found together without the database.
    for key, value in continuation_links(session).items():
        manifest[key] = value

    path = audio_path.with_suffix(".manifest.json")
    _write_private_json(path, path.with_suffix(".tmp"), manifest)
    return path


def _write_private_json(path: Path, tmp: Path, data: Any, **dump: Any) -> None:
    """Written whole to a private temporary file, then renamed over the old one."""
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, PRIVATE_FILE)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(data, indent=2, ensure_ascii=False, **dump))
    make_private(tmp)
    os.replace(tmp, path)


def record_local_release(manifest_path: Path, *, released_at: str,
                         copy_object_key: str, sha256: str) -> Path:
    """
    Note in the manifest that the WAV beside it has left this disk, and where
    the copy that replaces it is. Written before the WAV is deleted, so the
    folder never holds a missing file it cannot explain.

    The clinical JSON leaves with the audio: it names the patient, and a
    folder whose audio is gone has no reason to keep it. It is not lost - the
    clinical database holds the record, the worker writes the file again from
    there whenever it is needed (a late prescription, a recut, a restore), and
    the encrypted copy of it sits beside the audio's in the copy bucket.
    """
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        manifest = {}
    manifest["local_audio"] = {
        "status": "released",
        "released_at": released_at,
        "cloud_copy": copy_object_key,
        "sha256": sha256,
        "restore": "python restore.py restore <downloaded copy> <folder>",
        "clinical_json": "released with the audio; written again from the clinical "
                         "database when needed, and kept encrypted in the copy bucket",
    }
    _write_private_json(manifest_path, manifest_path.with_suffix(".tmp"), manifest)
    return manifest_path


def erase_recording_files(audio_path: Path) -> List[Path]:
    """
    Everything this recording left under its name: the WAV, the clinical
    JSON, the manifest, a sealed copy or a half-written file. Each is
    overwritten and deleted; a folder left empty is removed. Returns what
    went. The day's _index.json is rebuilt by the caller.
    """
    removed: List[Path] = []
    stem = audio_path.name[:-len(audio_path.suffix)] if audio_path.suffix else audio_path.name
    directory = audio_path.parent
    if not directory.exists():
        return removed
    for path in sorted(directory.iterdir()):
        if path.is_file() and (path.name == audio_path.name
                               or path.name.startswith(stem + ".")):
            if remove_securely(path):
                removed.append(path)
    # The folder's own index names the patient too: rebuilt from what is
    # left, and gone with the folder when nothing is.
    left = [p for p in directory.iterdir() if p.name != "_index.json"]
    if left:
        update_day_index(directory)
    else:
        remove_securely(directory / "_index.json")
        try:
            directory.rmdir()
        except OSError:
            pass
    return removed


def release_clinical_json(audio_path: Path) -> bool:
    """The JSON beside a released WAV, removed the same way. True if one was there."""
    return remove_securely(clinical_json_path(audio_path))


def local_audio_released(manifest_path: Path) -> bool:
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception:
        return False
    return (manifest.get("local_audio") or {}).get("status") == "released"


def clinical_json_path(audio_path: Path) -> Path:
    """`<name>.json`, beside `<name>.wav` (§8.8)."""
    return audio_path.with_suffix(".json")


def write_clinical_json(audio_path: Path, document: Dict[str, Any]) -> Path:
    """
    The clinical record as a file beside the audio (`SRS-CRI-05`).

    The database is the index; this file is the portable copy that travels with
    the recording, including into the cloud copy. It is written again if the
    prescription arrives later (`SRS-ARC-13`), so it is replaced atomically -
    a reader either sees the old file or the new one, never half of either.
    """
    path = clinical_json_path(audio_path)
    _write_private_json(path, path.with_suffix(".json.tmp"), document, sort_keys=False)
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
            "part_no": (data.get("derived_from") or {}).get("part_no"),
            "patient_ref": data.get("patient_ref"),
            "file": data.get("archive", {}).get("filename"),
            "opened_at": data.get("opened_at"),
            "duration_seconds": data.get("archive", {}).get("duration_seconds"),
            "sha256": data.get("archive", {}).get("sha256"),
            "local_audio": (data.get("local_audio") or {}).get("status", "present"),
            "cloud_copy": (data.get("local_audio") or {}).get("cloud_copy"),
        })

    index = {
        "directory": directory.name,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "session_count": len(entries),
        "sessions": entries,
    }
    path = directory / "_index.json"
    _write_private_json(path, path.with_suffix(".tmp"), index)
    return path


# ============================================================
# Capacity
# ============================================================

def free_bytes(path: Path) -> int:
    return shutil.disk_usage(path).free


# Never ask a volume to keep more than this share of itself free - the same cap
# the monitor applies to its floors, so a floor written for the 64 TB UIU array
# does not release everything on a 320 GB droplet.
PRESSURE_SHARE_LIMIT = 0.15


def pressure_floor(path: Path, configured: Optional[int], default: int) -> int:
    """The free space below which archived WAVs leave early, in bytes."""
    floor = configured if configured is not None and configured > 0 else default
    try:
        total = shutil.disk_usage(path).total
    except OSError:
        return floor
    return min(floor, int(total * PRESSURE_SHARE_LIMIT))


def continuation_links(session: Dict[str, Any]) -> Dict[str, Any]:
    """`continues` / `continued_by` ({session_id, file_stem}) when the server sent them."""
    links = {}
    for key in ("continues", "continued_by"):
        value = session.get(key)
        if isinstance(value, dict) and value.get("session_id"):
            links[key] = {"session_id": value["session_id"],
                          "file_stem": value.get("file_stem")}
    return links


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
    "ArchiveError", "JoinResult", "WavFormat", "archive_filename", "clinical_json_path",
    "cut_wav", "part_bytes", "part_frames", "wav_format",
    "expected_join_bytes", "free_destination", "ensure_space", "free_bytes",
    "pressure_floor", "continuation_links",
    "join_wav", "local_times", "name_for", "relative_path", "session_directory",
    "sha256_bytes", "sha256_file", "update_day_index", "write_clinical_json",
    "write_manifest", "make_private", "make_dirs", "tighten_tree", "remove_securely",
    "release_clinical_json", "PRIVATE_FILE", "PRIVATE_DIR",
]
