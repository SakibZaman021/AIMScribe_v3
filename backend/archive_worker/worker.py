"""
AIMScribe Archive Worker - runs on the AIMS LAB server.

Pulls verified sessions out of object storage and into the sorted archive tree,
then asks the backend to issue purge receipts so doctor PCs can delete their local
copies.

    hospital -> doctor -> date -> patient audio file

**Outbound only.** This process listens on no port and accepts no connections. It
holds one credential (its worker key) and receives short-lived presigned URLs for
exactly the objects it needs, so it never holds bucket credentials either. That is
the whole point of the pull design: the AIMS LAB server has no inbound attack
surface at all.

Sequence per session:

  1. GET /api/v2/archive/pending          sessions closed, verified, not archived
  2. download each segment                 verify sha256 against the manifest
  3. join into one WAV                     atomic write, then fsync
  4. re-read from disk and hash            proves the bytes actually landed
  5. write manifest.json, the clinical JSON, and _index.json
  6. POST /api/v2/archive/complete         backend issues the purge receipts

Then, for each archived recording, the cloud copy (SRS-ARC-08-13):

  7. encrypt the raw WAV and the JSON  with a key that never leaves this machine
  8. decrypt again and hash            must equal the archive's own SHA-256
  9. upload them                       the audio to cold storage, the JSON hot
 10. POST /api/v2/archive/copy/complete   the server checks what the store holds
                                          against what was sent, records the
                                          copy, and only then deletes the pieces

And, once a recording's copy is proven and it has aged past the retention
period, the WAV on this disk is released (11), and the clinical JSON beside it
with it: the JSON names the patient, and the clinical database and the copy
bucket both still hold it. The manifest and catalogue row stay; the manifest
says where the copy is. Without this the archive disk fills in days.

Everything this worker writes is readable by its own user only (umask 077, and
an explicit 0600 / 0700 on what it creates).

A session is only reported complete after step 4 succeeds. Any failure leaves the
session pending, the agent keeps its local audio, and the next pass retries.

One recording, several consultations. When the server finds that a recording
held more than one patient (a follow-up with no prescription, then the next
patient), the pending session carries "parts": where each consultation begins,
in seconds of the joined audio. The worker joins as always, then cuts the
joined audio at exact frames into one WAV per patient, each in its own folder
with its own manifest and JSON. The parts are contiguous - joined again they
are the joined WAV byte for byte - so the completion report still carries the
whole recording's fingerprint, the one the chain vouches for. If the server
later changes its mind (a prescription arrived late), the recut pass joins the
existing parts, proves they are still that audio, and cuts again.
"""
from __future__ import annotations

import logging
import os
import signal
import sys
import threading
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

from catalogue import Catalogue

sys.path.insert(0, str(Path(__file__).resolve().parent))

import archive
import cloudcopy
import serve
from archive import ArchiveError
from cloudcopy import CopyError

logging.basicConfig(
    level=os.getenv("AIMS_LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s - ARCHIVE - %(levelname)s - %(message)s",
)
logger = logging.getLogger("archive_worker")


class Settings:
    """Configuration from the environment. No secrets on the command line."""

    def __init__(self) -> None:
        self.backend_url = os.getenv("AIMS_BACKEND_URL", "").rstrip("/")
        self.worker_key = os.getenv("AIMS_WORKER_KEY", "")
        self.archive_root = Path(os.getenv("AIMS_ARCHIVE_ROOT", "D:/AIMSLAB_AUDIO_STORAGE"))
        self.poll_seconds = int(os.getenv("AIMS_POLL_SECONDS", "30"))
        self.batch_size = int(os.getenv("AIMS_BATCH_SIZE", "5"))
        self.request_timeout = int(os.getenv("AIMS_REQUEST_TIMEOUT", "60"))
        self.download_timeout = int(os.getenv("AIMS_DOWNLOAD_TIMEOUT", "600"))
        self.disk_headroom = int(os.getenv("AIMS_DISK_HEADROOM_BYTES", str(20 * 1024 ** 3)))
        self.verify_tls = os.getenv("AIMS_VERIFY_TLS", "true").lower() != "false"
        # The key for the cloud copy. It stays on this machine: the server and
        # the storage provider never see it (SRS-DAT-08).
        self.copy_key = cloudcopy.load_key(os.getenv("AIMS_COPY_KEY"))
        self.copy_batch = int(os.getenv("AIMS_COPY_BATCH", "2"))
        # Days an archived WAV stays on this disk after its cloud copy is
        # proven. Negative keeps every WAV here for ever; the disk then fills.
        self.local_retention_days = float(
            os.getenv("AIMS_ARCHIVE_LOCAL_RETENTION_DAYS", "7"))
        self.retention_interval = int(os.getenv("AIMS_RETENTION_INTERVAL_SECONDS", "600"))
        # Release on pressure (failure review A1): below this much free space,
        # archived WAVs whose cloud copy is proven leave this disk before their
        # retention period, oldest first, until it is free again. Twice the
        # headroom an archive needs by default, capped at 15% of the volume.
        pressure = os.getenv("AIMS_ARCHIVE_PRESSURE_FREE_BYTES", "").strip()
        self.pressure_free_bytes = int(pressure) if pressure else None
        self.pressure_interval = int(os.getenv("AIMS_PRESSURE_INTERVAL_SECONDS", "60"))
        # Where the sealed copy is written before upload. It is as large as the
        # recording, so it goes on the archive disk: a tmpfs counts against the
        # container's memory, and an hour-long consultation would not fit.
        self.copy_work_dir = Path(os.getenv("AIMS_COPY_WORK_DIR",
                                            str(self.archive_root / ".copywork")))
        # Downloads from the portal (serve.py): the API asks this worker to
        # decrypt one copy and stream it back. 0 turns it off.
        self.decrypt_port = int(os.getenv("AIMS_DECRYPT_PORT", "0"))
        self.decrypt_concurrency = int(os.getenv("AIMS_DECRYPT_CONCURRENCY", "2"))

    def problems(self) -> List[str]:
        issues = []
        if not self.backend_url:
            issues.append("AIMS_BACKEND_URL is not set")
        if not self.worker_key:
            issues.append("AIMS_WORKER_KEY is not set")
        if not self.backend_url.startswith("https://") and not self._internal():
            issues.append("AIMS_BACKEND_URL is not https - audio metadata would "
                          "cross the network in cleartext")
        if not self.verify_tls and not self._internal():
            issues.append("AIMS_VERIFY_TLS is false - the server's certificate is not "
                          "checked, so anyone in the path could pose as it")
        return issues

    def _internal(self) -> bool:
        """
        A plain-HTTP address that never leaves the machine: localhost, or a
        name with no dots, which on the UIU server is another container on the
        private Docker network (deploy/uiu). Nothing crosses a wire, so there
        is nothing to encrypt - and warning about it every start would teach
        people to ignore the warnings that matter.
        """
        host = self.backend_url.split("//", 1)[-1].split("/", 1)[0].split(":", 1)[0]
        return host in ("localhost", "127.0.0.1", "::1") or "." not in host


def safe_stem(part: Dict[str, Any]) -> str:
    """A part's file name, refused unless it is one the archive can hold."""
    stem = part.get("file_stem")
    if not isinstance(stem, str) or not archive.FOLDER_PATTERN.match(stem):
        raise ArchiveError(f"part {part.get('part_no')} has an unsafe file_stem: {stem!r}")
    return stem


class ArchiveWorker:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.session = requests.Session()
        self.session.headers.update({
            "X-Worker-Key": settings.worker_key,
            "User-Agent": "AIMScribe-ArchiveWorker/2.0",
        })
        # Lives beside the audio, so a restored volume brings its index with it.
        self.catalogue = Catalogue(settings.archive_root / "catalogue.sqlite3")
        self.running = True
        self.archived = 0
        self.copied = 0
        self.released = 0
        self.failed = 0
        self._next_retention = 0.0
        self._next_pressure = 0.0
        self._pressure_warned = False
        self.released_on_pressure = 0
        self.decrypt = None

    # ---- lifecycle ----

    def stop(self, *_: Any) -> None:
        logger.info("Shutdown requested; finishing the current session first")
        self.running = False
        if self.decrypt is not None:
            threading.Thread(target=self.decrypt.shutdown, daemon=True).start()

    def run(self) -> int:
        root = self.settings.archive_root
        root.mkdir(parents=True, exist_ok=True)
        try:
            changed = archive.tighten_tree(root)
            if changed:
                logger.info("Made %s existing archive file(s) and folder(s) owner-only",
                            changed)
        except OSError as exc:
            logger.warning("Could not tighten the archive's permissions: %s", exc)

        logger.info("=" * 64)
        logger.info("AIMScribe Archive Worker")
        logger.info("Backend : %s", self.settings.backend_url)
        logger.info("Archive : %s (%.1f GB free)",
                    root, archive.free_bytes(root) / 1024 ** 3)
        logger.info("Poll    : every %ss", self.settings.poll_seconds)
        logger.info("Copies  : %s", "on, encrypted at UIU"
                    if self.settings.copy_key else
                    "OFF - AIMS_COPY_KEY is not set, so no cloud copy is kept")
        logger.info("Release : %s", "never - no cloud copy, so every WAV stays here"
                    if self.settings.copy_key is None
                    or self.settings.local_retention_days < 0 else
                    f"WAV removed from this disk {self.settings.local_retention_days:g} "
                    f"day(s) after its cloud copy is proven")
        logger.info("Pressure: below %.1f GB free, copied WAVs leave early",
                    self.pressure_floor() / 1024 ** 3)
        try:
            self.decrypt = serve.start(self.settings)
        except OSError as exc:
            logger.error("Downloads: could not listen on port %s: %s",
                         self.settings.decrypt_port, exc)
        logger.info("Download: %s", f"on, port {self.settings.decrypt_port} (internal), "
                    f"{self.settings.decrypt_concurrency} at a time"
                    if self.decrypt is not None else "off")
        logger.info("=" * 64)

        while self.running:
            try:
                # Sleep unless something actually succeeded. "Processed" used to
                # include failures, so a session that could never be archived was
                # retried as fast as the network allowed - a hot loop against the
                # backend that looked, in the log, like the worker was busy.
                if self.drain_once() == 0:
                    self._sleep(self.settings.poll_seconds)
            except KeyboardInterrupt:
                break
            except Exception as exc:
                logger.error("Poll failed: %s", exc, exc_info=True)
                self._sleep(self.settings.poll_seconds)

        logger.info("Stopped. Archived %s session(s), copied %s, released %s, "
                    "%s failure(s)", self.archived, self.copied, self.released,
                    self.failed)
        return 0

    def _sleep(self, seconds: int) -> None:
        """Sleep in short slices so shutdown is responsive."""
        deadline = time.time() + seconds
        while self.running and time.time() < deadline:
            time.sleep(min(1.0, max(0.0, deadline - time.time())))

    # ---- main pass ----

    def drain_once(self) -> int:
        self.sweep()
        self.erase_pass()
        self.maybe_release()
        sessions = self.fetch_pending()
        if not sessions:
            return self.recut_pass() + self.copy_pass()

        logger.info("%s session(s) pending", len(sessions))
        processed = 0
        for session in sessions:
            if not self.running:
                break
            try:
                self.archive_session(session)
                self.archived += 1
                processed += 1
            except ArchiveError as exc:
                # Expected, recoverable: leave it pending and try again next pass.
                self.failed += 1
                logger.error("Session %s not archived: %s",
                             session.get("session_id"), exc)
            except Exception as exc:
                self.failed += 1
                logger.error("Session %s failed unexpectedly: %s",
                             session.get("session_id"), exc, exc_info=True)

        # Recuts and the cloud copy run after the archiving, on what is already
        # on disk.
        return processed + self.recut_pass() + self.copy_pass()

    def erase_pass(self) -> int:
        """
        Recordings the patient refused after they were archived: their files
        here go too - the WAV, the clinical JSON, the manifest and anything
        else beside them under the same name - removed the way released files
        are (overwritten, then deleted), and the catalogue forgets them. The
        server deletes the cloud copies; this tells it when the disk is clean.
        """
        try:
            response = self.session.get(
                f"{self.settings.backend_url}/api/v2/archive/erase/pending",
                timeout=self.settings.request_timeout, verify=self.settings.verify_tls)
            if response.status_code == 404:
                return 0                       # an older server: nothing to erase
            response.raise_for_status()
            tasks = response.json().get("tasks", [])
        except Exception as exc:
            logger.warning("Could not fetch recordings to erase: %s", exc)
            return 0
        done = 0
        for task in tasks:
            removed = []
            try:
                for relpath in task.get("files") or []:
                    removed += archive.erase_recording_files(self._inside(relpath))
                try:
                    self.catalogue.forget(task["session_id"])
                except Exception as exc:
                    logger.warning("Catalogue still lists %s: %s", task["session_id"], exc)
                response = self.session.post(
                    f"{self.settings.backend_url}/api/v2/archive/erase/complete",
                    json={"session_id": task["session_id"],
                          "removed": [p.name for p in removed]},
                    timeout=self.settings.request_timeout, verify=self.settings.verify_tls)
                response.raise_for_status()
                done += 1
                logger.info("Erased the archive files of refused recording %s (%s file(s))",
                            task["session_id"], len(removed))
            except Exception as exc:
                logger.error("Could not erase the files of %s yet: %s",
                             task.get("session_id"), exc)
        return done

    def sweep(self) -> None:
        """
        Ask the server to settle confirmation deadlines (SRS 3.2 §5.6):
        two minutes without CMED's API 2 makes a recording unconfirmed; one CMED
        never confirms is erased on its erase_after (7 days), never while CMED
        is silent for its clinic. Best effort - archiving goes on if this fails.
        """
        try:
            response = self.session.post(
                f"{self.settings.backend_url}/api/v2/maintenance/sweep",
                timeout=self.settings.request_timeout,
                verify=self.settings.verify_tls,
            )
            if response.status_code >= 300:
                logger.warning("Sweep answered %s", response.status_code)
        except Exception as exc:
            logger.warning("Sweep failed: %s", exc)

    def fetch_pending(self) -> List[Dict[str, Any]]:
        response = self.session.get(
            f"{self.settings.backend_url}/api/v2/archive/pending",
            params={"limit": self.settings.batch_size},
            timeout=self.settings.request_timeout,
            verify=self.settings.verify_tls,
        )
        if response.status_code == 401:
            raise RuntimeError("worker key rejected by the backend")
        response.raise_for_status()
        return response.json().get("sessions", [])

    # ---- one session ----

    def archive_session(self, session: Dict[str, Any]) -> None:
        session_id = session["session_id"]
        segments = session.get("segments") or []
        if not segments:
            raise ArchiveError("session has no committed segments")

        opened_local, closed_local = archive.local_times(
            session.get("opened_at"), session.get("closed_at"),
            session.get("timezone") or "UTC")

        session_date = session.get("session_date") or opened_local.date().isoformat()

        parts = session.get("parts") or []
        if len(parts) > 1:
            self.archive_parts(session, segments, parts, session_date)
            return

        # The filename comes first: the folder is named after it, so that one
        # directory is one consultation. A patient seen twice in a day gets two
        # folders, distinguished by the times in the name.
        filename = archive.name_for(session, opened_local, closed_local)
        folder_name = filename[:-len(".wav")]

        directory = archive.session_directory(
            self.settings.archive_root, session["hospital_id"],
            session["doctor_id"], session_date, folder_name)

        # The name no longer carries the session ULID, so an existing file is only
        # ours if it is exactly the size this session would produce. Anything else
        # is a different consultation and must not be overwritten or re-reported.
        expected = archive.expected_join_bytes([int(s["bytes"]) for s in segments])
        destination, already_ours = archive.free_destination(
            directory, filename, expected, session_id=session_id,
            segment_hashes=[(int(s["seq_no"]), s["sha256"]) for s in segments])
        relpath = archive.relative_path(
            session["hospital_id"], session["doctor_id"], session_date,
            folder_name, destination.name)

        # Already done? Re-report rather than re-downloading; /archive/complete is
        # idempotent, and this is the normal path when a previous run was
        # interrupted between writing the file and reporting it.
        if already_ours:
            existing = archive.sha256_file(destination)
            logger.info("Session %s already present on disk; re-reporting", session_id)
            self.write_clinical_json(session_id, destination, recording={
                "audio_sha256": existing.hex(),
                "audio_bytes": destination.stat().st_size,
                "archive_relpath": relpath,
            }, links=archive.continuation_links(session))
            self.report_complete(session_id, relpath, existing, destination.stat().st_size,
                                 segments=segments)
            return

        total_bytes = sum(int(s["bytes"]) for s in segments)
        archive.ensure_space(self.settings.archive_root, total_bytes * 2,
                             headroom=self.settings.disk_headroom)
        archive.make_dirs(directory)

        with tempfile.TemporaryDirectory(prefix=f"aims_{session_id}_") as scratch:
            paths = self.download_segments(segments, Path(scratch))

            logger.info("Joining %s segment(s) for %s", len(paths), session_id)
            result = archive.join_wav(paths, destination)

            # Re-read from the archive volume. Everything up to here proves the
            # bytes were correct in memory; only this proves they are correct on
            # the disk that will hold them for seven years.
            on_disk = archive.sha256_file(destination)
            if on_disk != result.sha256:
                destination.unlink(missing_ok=True)
                raise ArchiveError("archive file failed verification after write")

        archive.write_manifest(destination, session, result)
        # The clinical record travels with the audio, in the same folder and
        # under the same name (SRS-CRI-05, §8.8), carrying the fingerprint of
        # the file it sits beside.
        self.write_clinical_json(session_id, destination, recording={
            "audio_sha256": result.sha256.hex(),
            "audio_bytes": result.bytes,
            "duration_seconds": round(result.duration_seconds, 3),
            "archive_relpath": relpath,
        }, links=archive.continuation_links(session))
        archive.update_day_index(directory)

        # The hospital's own index, so "what audio do we hold?" is answerable on
        # this machine with no network and no cloud account. Never allowed to fail
        # the archive: losing the index is recoverable, losing the audio is not.
        try:
            self.catalogue.record(
                session=session, archive_relpath=relpath, filename=destination.name,
                sha256_hex=result.sha256.hex(), byte_length=result.bytes,
                segments=segments, session_date=session_date,
                pauses=session.get("pauses") or [])
        except Exception as exc:
            logger.error("Could not write the local catalogue for %s: %s",
                         session_id, exc, exc_info=True)

        logger.info("Archived %s -> %s (%.1f MB, %.1f s)",
                    session_id, relpath, result.bytes / 1024 ** 2,
                    result.duration_seconds)

        if self.report_complete(session_id, relpath, result.sha256, result.bytes,
                                segments=segments):
            try:
                self.catalogue.mark_reported(session_id)
            except Exception as exc:
                logger.warning("Catalogue not marked reported for %s: %s", session_id, exc)

    # ---- one recording, several consultations ----

    def archive_parts(self, session: Dict[str, Any], segments: List[Dict[str, Any]],
                      parts: List[Dict[str, Any]], session_date: str) -> None:
        """
        Join, then cut into one file per consultation, then report once.

        Everything is proven before anything is reported: the joined audio is
        hashed from disk, every part is hashed from the archive volume, and the
        parts' frames add up to the joined audio's. A failure anywhere leaves
        the session pending, exactly as for a single file.
        """
        session_id = session["session_id"]
        parts = sorted(parts, key=lambda q: int(q["part_no"]))
        for part in parts:
            safe_stem(part)                       # refuses an unsafe name now

        total_bytes = sum(int(seg["bytes"]) for seg in segments)
        # The joined audio in scratch, and the parts beside it on the volume.
        archive.ensure_space(self.settings.archive_root, total_bytes * 2,
                             headroom=self.settings.disk_headroom)

        with tempfile.TemporaryDirectory(prefix=f"aims_{session_id}_") as scratch:
            paths = self.download_segments(segments, Path(scratch))
            logger.info("Joining %s segment(s) for %s, to cut into %s consultations",
                        len(paths), session_id, len(parts))
            joined = archive.join_wav(paths, Path(scratch) / "joined.wav")
            if archive.sha256_file(joined.path) != joined.sha256:
                raise ArchiveError("joined audio failed verification after write")
            written = self.write_parts(session, parts, joined, session_date)

        self.finish_parts(session, written, joined, session_date, segments)

    def write_parts(self, session: Dict[str, Any], parts: List[Dict[str, Any]],
                    joined: "archive.JoinResult", session_date: str,
                    own: Optional[set] = None) -> List[Dict[str, Any]]:
        """
        Cut `joined` into the parts and write each to its own folder.

        `own` is the set of paths this session already holds (a recut): a file
        there is overwritten, because it is ours, rather than stepped around.
        """
        own = own or set()
        session_id = session["session_id"]
        fmt = archive.wav_format(joined.path)
        bounds = archive.part_frames([q.get("start_seconds") for q in parts],
                                     fmt.rate, fmt.frames)
        written: List[Dict[str, Any]] = []
        for part, (start, end) in zip(parts, bounds):
            stem = part["file_stem"]
            doctor = part.get("doctor_id") or session["doctor_id"]
            directory = archive.session_directory(
                self.settings.archive_root, session["hospital_id"], doctor,
                session_date, stem)
            filename = f"{stem}.wav"
            if (directory / filename).resolve() in own:
                destination = directory / filename
            else:
                destination, _ = archive.free_destination(
                    directory, filename, archive.part_bytes(end - start, fmt),
                    session_id=session_id)
            archive.make_dirs(directory)
            result = archive.cut_wav(joined.path, start, end, destination)
            if archive.sha256_file(destination) != result.sha256:
                destination.unlink(missing_ok=True)
                raise ArchiveError(f"part {part['part_no']} failed verification after write")
            written.append({
                "part": part, "path": destination, "result": result,
                "start_frame": start, "end_frame": end,
                "relpath": archive.relative_path(session["hospital_id"], doctor,
                                                 session_date, stem, destination.name),
                "derived_from": {
                    "session_id": session_id,
                    "part_no": int(part["part_no"]),
                    "parts_count": len(parts),
                    "start_seconds": round(start / fmt.rate, 3),
                    "end_seconds": round(end / fmt.rate, 3),
                    "start_frame": start, "end_frame": end,
                    "rule": part.get("rule"),
                    "needs_review": bool(part.get("needs_review")),
                    "recording_sha256": joined.sha256.hex(),
                    "recording_bytes": joined.bytes,
                },
            })
        if sum(w["end_frame"] - w["start_frame"] for w in written) != fmt.frames:
            raise ArchiveError("the parts do not add up to the recording")
        return written

    def finish_parts(self, session: Dict[str, Any], written: List[Dict[str, Any]],
                     joined: "archive.JoinResult", session_date: str,
                     segments: List[Dict[str, Any]]) -> bool:
        """Manifest, JSON, indexes and catalogue for each part; then one report."""
        session_id = session["session_id"]
        for w in written:
            part = w["part"]
            as_part = {**session, "patient_ref": part.get("patient_id")
                       or session.get("patient_ref"),
                       "doctor_id": part.get("doctor_id") or session.get("doctor_id")}
            archive.write_manifest(w["path"], as_part, w["result"],
                                   derived_from=w["derived_from"])
            self.write_clinical_json(session_id, w["path"], recording={
                "audio_sha256": w["result"].sha256.hex(),
                "audio_bytes": w["result"].bytes,
                "duration_seconds": round(w["result"].duration_seconds, 3),
                "archive_relpath": w["relpath"],
            }, part_no=int(part["part_no"]), derived_from=w["derived_from"],
               links=archive.continuation_links(session))
            archive.update_day_index(w["path"].parent)

        try:
            first = written[0]
            self.catalogue.record(
                session=session, archive_relpath=first["relpath"],
                filename=first["path"].name, sha256_hex=joined.sha256.hex(),
                byte_length=joined.bytes, segments=segments, session_date=session_date,
                pauses=session.get("pauses") or [], parts_count=len(written))
            for w in written:
                part = w["part"]
                self.catalogue.record_part(session_id, {
                    "part_no": int(part["part_no"]),
                    "patient_id": part.get("patient_id") or session.get("patient_ref"),
                    "doctor_id": part.get("doctor_id") or session.get("doctor_id"),
                    "archive_relpath": w["relpath"], "filename": w["path"].name,
                    "sha256": w["result"].sha256.hex(), "bytes": w["result"].bytes,
                    "start_seconds": w["derived_from"]["start_seconds"],
                    "end_seconds": w["derived_from"]["end_seconds"],
                    "start_frame": w["start_frame"], "end_frame": w["end_frame"],
                    "rule": part.get("rule"), "needs_review": part.get("needs_review"),
                })
            self.catalogue.drop_parts_after(session_id, len(written))
        except Exception as exc:
            logger.error("Could not write the local catalogue for %s: %s",
                         session_id, exc, exc_info=True)

        logger.info("Archived %s as %s consultation(s): %s", session_id, len(written),
                    ", ".join(w["relpath"] for w in written))
        reported = self.report_complete(
            session_id, written[0]["relpath"], joined.sha256, joined.bytes,
            parts=[{"part_no": int(w["part"]["part_no"]),
                    "archive_relpath": w["relpath"],
                    "sha256": w["result"].sha256.hex(), "bytes": w["result"].bytes,
                    "start_seconds": w["derived_from"]["start_seconds"],
                    "end_seconds": w["derived_from"]["end_seconds"]} for w in written],
            segments=segments)
        if reported:
            try:
                self.catalogue.mark_reported(session_id)
            except Exception as exc:
                logger.warning("Catalogue not marked reported for %s: %s", session_id, exc)
        return reported

    # ---- recut: the server changed where the consultations begin ----

    def recut_pass(self) -> int:
        """Cut again every archived recording whose parts the server changed."""
        try:
            sessions = self.fetch("archive/recut/pending", self.settings.batch_size)
        except Exception as exc:
            # An older server has no recut endpoint; nothing to do then.
            logger.debug("No recut list: %s", exc)
            return 0
        done = 0
        for session in sessions:
            if not self.running:
                break
            try:
                if self.recut_session(session):
                    done += 1
            except (ArchiveError, CopyError) as exc:
                self.failed += 1
                logger.error("Recut of %s refused: %s", session.get("session_id"), exc)
                self.report_copy_failure(session.get("session_id"), "recut", str(exc))
            except Exception as exc:
                self.failed += 1
                logger.error("Recut of %s failed unexpectedly: %s",
                             session.get("session_id"), exc, exc_info=True)
                self.report_copy_failure(session.get("session_id"), "recut", str(exc))
        return done

    def _existing_files(self, session: Dict[str, Any]) -> List[Path]:
        """
        The files this recording is archived as now, in order. Taken from the
        server when it lists them, else from this machine's catalogue, else the
        single file the session was archived as.
        """
        listed = (session.get("existing_parts") or session.get("archived_parts")
                  or [q for q in (session.get("parts") or []) if q.get("archive_relpath")])
        relpaths = [q["archive_relpath"] for q in
                    sorted(listed, key=lambda q: int(q.get("part_no") or 0))]
        if not relpaths:
            relpaths = [r["archive_relpath"] for r in
                        self.catalogue.parts_for(session["session_id"])]
        if not relpaths and session.get("archive_relpath"):
            relpaths = [session["archive_relpath"]]
        return [self._inside(rel) for rel in relpaths]

    def _inside(self, relpath: str) -> Path:
        path = (self.settings.archive_root / relpath).resolve()
        if self.settings.archive_root.resolve() not in path.parents:
            raise ArchiveError(f"archived file outside the archive: {relpath}")
        return path

    def _expected_sha(self, session: Dict[str, Any]) -> str:
        expected = session.get("archive_sha256")
        if not expected:
            row = self.catalogue.session_row(session["session_id"])
            expected = row["sha256"] if row else None
        if not expected:
            raise ArchiveError("no fingerprint for the recording to check the cut against")
        return str(expected).lower()

    def recut_session(self, session: Dict[str, Any]) -> bool:
        """
        Join the existing parts, prove that is still the recording the chain
        vouches for, cut at the new starts, report, then tidy the old files.

        Safe to interrupt anywhere. If the run stopped after writing some new
        parts over old ones, the old set no longer joins to the recording - so
        the new set is tried as well; whichever proves itself is the source.
        """
        session_id = session["session_id"]
        parts = sorted(session.get("parts") or [], key=lambda q: int(q["part_no"]))
        if not parts:
            raise ArchiveError("recut requested with no parts")
        for part in parts:
            safe_stem(part)
        expected = self._expected_sha(session)
        session_date = session.get("session_date") or str(session.get("opened_at", ""))[:10]

        new_paths = []
        for part in parts:
            doctor = part.get("doctor_id") or session["doctor_id"]
            directory = archive.session_directory(
                self.settings.archive_root, session["hospital_id"], doctor,
                session_date, part["file_stem"])
            new_paths.append((directory / f"{part['file_stem']}.wav").resolve())
        old_paths = self._existing_files(session)

        with tempfile.TemporaryDirectory(prefix=f"aims_recut_{session_id}_") as scratch:
            joined = None
            for candidate in (old_paths, new_paths):
                if not candidate or not all(path.is_file() for path in candidate):
                    continue
                attempt = archive.join_wav(candidate, Path(scratch) / "joined.wav")
                if attempt.sha256.hex() == expected:
                    joined = attempt
                    break
            if joined is None:
                missing = [path.name for path in old_paths if not path.is_file()]
                if missing:
                    raise ArchiveError(
                        f"{', '.join(missing)} not on this disk (released to the "
                        f"cloud copy?); restore it before the recording can be recut")
                raise ArchiveError("the archived parts no longer join to the recording "
                                   "the chain describes; not cutting them")

            own = set(old_paths) | set(new_paths)
            written = self.write_parts(session, parts, joined, session_date, own=own)
            segments = session.get("segments") or []
            reported = self.finish_parts(session, written, joined, session_date, segments)

        if not reported:
            return False
        kept = {w["path"].resolve() for w in written}
        for old in old_paths:
            if old.resolve() not in kept:
                self._remove_archived(old)
        try:
            self.catalogue.mark_uncopied(session_id)
        except Exception as exc:
            logger.warning("Catalogue copy state not reset for %s: %s", session_id, exc)
        logger.info("Recut %s into %s consultation(s)", session_id, len(written))
        return True

    def _remove_archived(self, audio: Path) -> None:
        """An old part no longer named by the server: its files, then its folder."""
        archive.release_clinical_json(audio)
        for path in (audio, audio.with_suffix(".manifest.json"),
                     audio.parent / "_index.json"):
            path.unlink(missing_ok=True)
        try:
            audio.parent.rmdir()
        except OSError:
            archive.update_day_index(audio.parent)    # something else lives there

    def write_clinical_json(self, session_id: str, audio_path: Path,
                            recording: Optional[Dict[str, Any]] = None,
                            part_no: Optional[int] = None,
                            derived_from: Optional[Dict[str, Any]] = None,
                            links: Optional[Dict[str, Any]] = None) -> Path:
        """
        Fetch what CMED holds about this visit and write it beside the audio.

        Only this server can read the clinical database, so the document is
        assembled there. A failure leaves the session pending: an archive
        without its clinical record is incomplete, and retrying costs one
        request, since the audio is already on disk.

        `recording` fills in what the server cannot know yet. The JSON is
        written as the recording is archived, which is before the server has
        been told the merged file's fingerprint - so the worker, which just
        computed it, puts it in (§8.8). Without this the JSON travelled into
        the cloud copy with an empty `audio_sha256`, and the one field that
        ties the file to the chain was missing from the copy that would be
        used to rebuild it.
        """
        extra = {"params": {"part": part_no}} if part_no is not None else {}
        response = self.session.get(
            f"{self.settings.backend_url}/api/v2/archive/clinical/{session_id}",
            timeout=self.settings.request_timeout, verify=self.settings.verify_tls,
            **extra)
        if response.status_code >= 300:
            raise ArchiveError(f"clinical record unavailable "
                               f"({response.status_code}); will retry")
        document = response.json()
        if recording or derived_from:
            merged = {**(document.get("recording") or {}), **(recording or {})}
            if derived_from:
                # The server says which part this is; the frames and the whole
                # recording's fingerprint are only known here.
                merged["derived_from"] = {**derived_from,
                                          **(merged.get("derived_from") or {})}
            document["recording"] = merged
        # A consultation a restart split in two: the other recording, by name.
        # What the server already put in its document wins.
        for key, value in (links or {}).items():
            if not document.get(key):
                document[key] = value
        return archive.write_clinical_json(audio_path, document)

    def download_segments(self, segments: List[Dict[str, Any]], scratch: Path) -> List[Path]:
        """
        Fetch every segment and verify each against its recorded hash.

        A mismatch aborts the whole session: a partially correct archive is worse
        than none, because it looks complete.
        """
        paths: List[Path] = []
        for segment in sorted(segments, key=lambda s: s["seq_no"]):
            seq_no = segment["seq_no"]
            target = scratch / f"seg_{seq_no:05d}.wav"

            response = self.session.get(
                segment["download_url"], stream=True,
                timeout=self.settings.download_timeout,
                # Presigned URLs already carry their own authentication; sending
                # the worker key to object storage would leak it.
                headers={"X-Worker-Key": None},
                verify=self.settings.verify_tls,
            )
            response.raise_for_status()

            with open(target, "wb") as handle:
                for chunk in response.iter_content(chunk_size=1 << 20):
                    if chunk:
                        handle.write(chunk)

            actual = archive.sha256_file(target).hex()
            if actual != segment["sha256"]:
                raise ArchiveError(
                    f"segment {seq_no} hash mismatch: object storage returned "
                    f"{actual[:16]}..., expected {segment['sha256'][:16]}...")

            paths.append(target)

        return paths

    def report_complete(self, session_id: str, relpath: str,
                        sha256: bytes, byte_length: int,
                        parts: Optional[List[Dict[str, Any]]] = None,
                        segments: Optional[List[Dict[str, Any]]] = None) -> bool:
        """
        Tell the backend the archive copy exists and hashes correctly.

        This is what causes purge receipts to be issued, and therefore the only
        thing that ever authorises a doctor PC to delete its local audio.

        Returns True when the backend accepted it. False means the audio is on
        disk but unacknowledged - the catalogue keeps it in `unreported()` and the
        agent rightly keeps its local copy.
        """
        body: Dict[str, Any] = {
            "session_id": session_id,
            "archive_relpath": relpath,
            "sha256": sha256.hex(),
            "bytes": byte_length,
        }
        if parts:
            body["parts"] = parts
        if segments is not None:
            # Exactly the pieces joined into this file: the server receipts,
            # marks archived and later deletes these and no others.
            body["segments"] = [{"seq_no": int(s["seq_no"]), "sha256": s["sha256"]}
                                for s in segments]
        response = self.session.post(
            f"{self.settings.backend_url}/api/v2/archive/complete",
            json=body,
            timeout=self.settings.request_timeout,
            verify=self.settings.verify_tls,
        )
        if response.status_code == 409:
            try:
                why = response.json().get("detail")
            except Exception:
                why = ""
            logger.warning("Session %s not accepted as archived (%s); no receipts issued",
                           session_id, why or "conflict")
            return False
        response.raise_for_status()
        body = response.json()
        logger.info("Session %s reported complete; %s purge receipt(s) issued, "
                    "%s clip(s) deleted from the bucket", session_id,
                    body.get("receipts_issued", 0), body.get("objects_deleted", 0))
        return True


    # ============================================================
    # The cloud copy (SRS-ARC-08..13)
    # ============================================================

    def copy_pass(self) -> int:
        """
        Make the lossless cloud copy of everything archived but not yet copied,
        and rewrite any JSON that a late prescription has changed.

        Until a recording's copy exists, its pieces stay in the segment bucket:
        the copy is what makes deleting them safe (SRS-ARC-08).
        """
        if self.settings.copy_key is None:
            return 0

        done = 0
        for session in self.fetch("archive/copy/pending", self.settings.copy_batch):
            if not self.running:
                break
            session_id = session["session_id"]
            try:
                self.copy_session(session)
                self.copied += 1
                done += 1
            except (CopyError, ArchiveError) as exc:
                self.failed += 1
                logger.error("Cloud copy of %s failed: %s", session_id, exc)
                self.report_copy_failure(session_id, "copy", str(exc))
            except Exception as exc:
                self.failed += 1
                logger.error("Cloud copy of %s failed unexpectedly: %s",
                             session_id, exc, exc_info=True)
                self.report_copy_failure(session_id, "copy", str(exc))

        for session in self.fetch("archive/json/pending", self.settings.copy_batch):
            if not self.running:
                break
            try:
                self.rewrite_json(session)
                done += 1
            except Exception as exc:
                self.failed += 1
                logger.error("Rewriting the JSON for %s failed: %s",
                             session["session_id"], exc)
                self.report_copy_failure(session["session_id"], "json", str(exc))
        return done

    def parts_of(self, session: Dict[str, Any]) -> List[Dict[str, Any]]:
        """
        The files a multi-consultation recording is archived as, in order, each
        with its fingerprint: from the server's listing when it gives one, else
        from this machine's catalogue. Empty for a single-file recording.
        """
        listed = [q for q in (session.get("parts") or []) if q.get("archive_relpath")]
        if len(listed) > 1:
            return [{"part_no": int(q["part_no"]), "archive_relpath": q["archive_relpath"],
                     "sha256": q.get("archive_sha256") or q.get("sha256"),
                     "copied": bool(q.get("copied_at"))}
                    for q in sorted(listed, key=lambda q: int(q["part_no"]))]
        rows = self.catalogue.parts_for(session["session_id"])
        if len(rows) > 1:
            return [{"part_no": r["part_no"], "archive_relpath": r["archive_relpath"],
                     "sha256": r["sha256"], "copied": bool(r["copied_at"])} for r in rows]
        return []

    def copy_parts(self, session: Dict[str, Any], parts: List[Dict[str, Any]]) -> None:
        """
        The copy of each consultation's file, part by part. The pieces in the
        segment bucket may go only once every part is copied, so "final" goes
        with the last one and with no other.
        """
        session_id = session["session_id"]
        todo = [q for q in parts if not q["copied"]]
        for index, part in enumerate(todo):
            audio = self.archive_file({"archive_relpath": part["archive_relpath"]})
            if part.get("sha256") and archive.sha256_file(audio).hex() != part["sha256"]:
                raise CopyError(f"{audio.name} on disk no longer matches what was "
                                f"archived; not copying it")
            if not archive.clinical_json_path(audio).exists():
                self.write_clinical_json(session_id, audio, part_no=part["part_no"],
                                         links=archive.continuation_links(session))
            audio_copy, json_copy = self.seal_and_upload(
                session_id, audio, part.get("sha256"), part_no=part["part_no"])
            result = self.post("archive/copy/complete", {
                "session_id": session_id, "part_no": part["part_no"],
                "final": index == len(todo) - 1,
                "copies": [audio_copy, json_copy],
            })
            try:
                self.catalogue.mark_part_copied(session_id, part["part_no"],
                                                audio_copy["object_key"])
            except Exception as exc:
                logger.warning("Catalogue not marked copied for %s part %s: %s",
                               session_id, part["part_no"], exc)
            logger.info("Cloud copy of %s part %s stored; %s piece(s) deleted",
                        session_id, part["part_no"], result.get("objects_deleted", 0))
        try:
            rows = self.catalogue.parts_for(session_id)
            first = next((r for r in rows if r["copy_object_key"]), None)
            if first is not None and all(r["copied_at"] for r in rows):
                self.catalogue.mark_copied(session_id, first["copy_object_key"])
        except Exception as exc:
            logger.warning("Catalogue not marked copied for %s: %s", session_id, exc)

    def seal_and_upload(self, session_id: str, audio: Path, expected: Optional[str],
                        part_no: Optional[int] = None):
        """Encrypt the WAV and its JSON, prove the seal, upload both."""
        json_path = archive.clinical_json_path(audio)
        self.settings.copy_work_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=f"aimscopy_{session_id}_",
                                         dir=self.settings.copy_work_dir) as scratch:
            work = Path(scratch)
            stem = audio.stem
            expected = expected or archive.sha256_file(audio).hex()
            bound = cloudcopy.binding(session_id, "audio", part_no=part_no, stem=stem)
            sealed = cloudcopy.encrypt_file(audio, work / f"{stem}.wav.enc",
                                            self.settings.copy_key, bound)
            if sealed.plain_sha256 != expected:
                raise CopyError(f"{audio.name} changed while it was encrypted; "
                                f"the pieces stay where they are")
            if cloudcopy.sealed_sha256(sealed.path, self.settings.copy_key,
                                       bound) != expected:
                raise CopyError("the sealed copy does not decrypt to the archived audio; "
                                "the pieces stay where they are")
            audio_copy = self.upload_copy(session_id, "audio", sealed, part_no=part_no)
            json_copy = self.upload_copy(
                session_id, "json", self.seal_json(session_id, json_path, work, part_no),
                part_no=part_no)
        return audio_copy, json_copy

    def seal_json(self, session_id: str, json_path: Path, work: Path,
                  part_no: Optional[int] = None) -> "cloudcopy.Encrypted":
        """The clinical JSON, sealed and bound to its recording like the audio."""
        stem = json_path.stem
        return cloudcopy.encrypt_file(
            json_path, work / f"{stem}.json.enc", self.settings.copy_key,
            cloudcopy.binding(session_id, "json", part_no=part_no, stem=stem))

    def copy_session(self, session: Dict[str, Any]) -> None:
        """The steps of SRS-ARC-09, in order, each proven before the next."""
        session_id = session["session_id"]
        parts = self.parts_of(session)
        if parts:
            self.copy_parts(session, parts)
            return
        audio = self.archive_file(session)

        # 1. The archived WAV, still exactly what was reported.
        if session.get("archive_sha256"):
            on_disk = archive.sha256_file(audio).hex()
            if on_disk != session["archive_sha256"]:
                raise CopyError(f"{audio.name} on disk no longer matches what was "
                                f"archived; not copying it")

        json_path = archive.clinical_json_path(audio)
        if not json_path.exists():
            self.write_clinical_json(
                session_id, audio, links=archive.continuation_links(session))

        self.settings.copy_work_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=f"aimscopy_{session_id}_",
                                         dir=self.settings.copy_work_dir) as scratch:
            work = Path(scratch)
            stem = audio.stem

            # 2 and 3. Encrypt the WAV itself, then decrypt it again and hash.
            # Both hashes must be the archive's: the bytes that went in, and
            # the bytes the sealed file gives back with the key UIU holds.
            expected = session.get("archive_sha256") or archive.sha256_file(audio).hex()
            # Bound to this recording: a copy put under another one's key is
            # refused on restore instead of restoring the wrong patient.
            bound = cloudcopy.binding(session_id, "audio", stem=stem)
            sealed = cloudcopy.encrypt_file(audio, work / f"{stem}.wav.enc",
                                            self.settings.copy_key, bound)
            if sealed.plain_sha256 != expected:
                raise CopyError(f"{audio.name} changed while it was encrypted; "
                                f"the pieces stay where they are")
            if cloudcopy.sealed_sha256(sealed.path, self.settings.copy_key,
                                       bound) != expected:
                raise CopyError("the sealed copy does not decrypt to the archived audio; "
                                "the pieces stay where they are")

            # 4 and 5. Upload; the server then asks the store for its fingerprint.
            audio_copy = self.upload_copy(session_id, "audio", sealed)
            json_copy = self.upload_copy(
                session_id, "json", self.seal_json(session_id, json_path, work))

        # 6 and 7. Recorded on the server, which then - and only then - deletes
        # the pieces from the segment bucket.
        result = self.post("archive/copy/complete", {
            "session_id": session_id, "final": True,
            "copies": [audio_copy, json_copy],
        })
        try:
            self.catalogue.mark_copied(session_id, audio_copy["object_key"])
        except Exception as exc:
            logger.warning("Catalogue not marked copied for %s: %s", session_id, exc)
        logger.info("Cloud copy of %s stored (%.1f MB WAV); %s piece(s) deleted",
                    session_id, audio_copy["bytes"] / 1024 ** 2,
                    result.get("objects_deleted", 0))

    def rewrite_json(self, session: Dict[str, Any]) -> None:
        """
        A prescription arrived after archiving: write the JSON again and upload
        it as a new object. The earlier ones are kept (SRS-ARC-13).
        """
        session_id = session["session_id"]
        parts = self.parts_of(session)
        if parts:
            for part in parts:
                audio = self.archive_file({"archive_relpath": part["archive_relpath"]},
                                          need_audio=False)
                json_path = self.write_clinical_json(
                    session_id, audio, part_no=part["part_no"],
                    links=archive.continuation_links(session))
                copies = []
                if self.settings.copy_key is not None:
                    with tempfile.TemporaryDirectory(
                            prefix=f"aimsjson_{session_id}_") as scratch:
                        copies.append(self.upload_copy(
                            session_id, "json",
                            self.seal_json(session_id, json_path, Path(scratch),
                                           part["part_no"]),
                            part_no=part["part_no"]))
                self.post("archive/copy/complete",
                          {"session_id": session_id, "part_no": part["part_no"],
                           "final": False, "copies": copies})
                self.drop_json_if_released(audio, copies)
            logger.info("Wrote the JSON of %s part(s) of %s again", len(parts), session_id)
            return
        # The WAV may already have been released from this disk; the JSON
        # beside where it was is still written, and still copied.
        audio = self.archive_file(session, need_audio=False)
        json_path = self.write_clinical_json(
            session_id, audio, links=archive.continuation_links(session))

        copies = []
        if self.settings.copy_key is not None:
            with tempfile.TemporaryDirectory(prefix=f"aimsjson_{session_id}_") as scratch:
                copies.append(self.upload_copy(
                    session_id, "json",
                    self.seal_json(session_id, json_path, Path(scratch))))
        self.post("archive/copy/complete",
                  {"session_id": session_id, "final": False, "copies": copies})
        self.drop_json_if_released(audio, copies)
        logger.info("Wrote the JSON for %s again after a late prescription", session_id)

    def drop_json_if_released(self, audio: Path, copies: List[Dict[str, Any]]) -> None:
        """
        A JSON rewritten for a recording whose WAV has left this disk goes too,
        once its new copy is recorded: the folder keeps no patient record its
        audio no longer backs. Without a copy it stays - it is then the only one.
        """
        if copies and not audio.exists() and archive.local_audio_released(
                audio.with_suffix(".manifest.json")):
            archive.release_clinical_json(audio)

    def archive_file(self, session: Dict[str, Any], need_audio: bool = True) -> Path:
        relpath = session.get("archive_relpath") or ""
        audio = (self.settings.archive_root / relpath).resolve()
        if self.settings.archive_root.resolve() not in audio.parents:
            raise CopyError(f"archived file outside the archive: {relpath}")
        if need_audio and not audio.is_file():
            raise CopyError(f"archived file missing: {relpath}")
        if not need_audio and not audio.parent.is_dir():
            raise CopyError(f"archive folder missing: {relpath}")
        return audio

    # ============================================================
    # Releasing the local WAV once the cloud copy is proven
    # ============================================================

    def maybe_release(self) -> int:
        """
        At most every few minutes: hashing a day's audio is not free. Sooner
        when the disk is short of space, which cannot wait ten minutes.
        """
        now = time.time()
        pressed = self.under_pressure()
        due = now >= self._next_retention
        if not due and not (pressed and now >= self._next_pressure):
            return 0
        released = 0
        try:
            if due:
                self._next_retention = now + self.settings.retention_interval
                released += self.release_pass()
            if pressed:
                self._next_pressure = now + self.settings.pressure_interval
                released += self.release_on_pressure()
        except Exception as exc:
            logger.error("Release pass failed: %s", exc, exc_info=True)
        return released

    # ---- release on pressure (failure review A1) ----

    def pressure_floor(self) -> int:
        return archive.pressure_floor(self.settings.archive_root,
                                      self.settings.pressure_free_bytes,
                                      2 * self.settings.disk_headroom)

    def under_pressure(self) -> bool:
        try:
            return archive.free_bytes(self.settings.archive_root) < self.pressure_floor()
        except OSError:
            return False

    def release_on_pressure(self, now: Optional[datetime] = None) -> int:
        """
        The archive disk is short of space: release the oldest archived WAVs
        whose cloud copy is proven, before their retention period, until it is
        not. The same checks as an ordinary release - a proven copy, and the
        file on disk still the one copied - and nothing without a copy, ever:
        a recording that exists in one place stays in that place.
        """
        floor = self.pressure_floor()
        if self.settings.copy_key is None:
            if not self._pressure_warned:
                self._pressure_warned = True
                logger.critical("Archive disk below %.1f GB free, and nothing can be "
                                "released: there are no cloud copies (AIMS_COPY_KEY is "
                                "not set)", floor / 1024 ** 3)
            return 0
        now = now or datetime.now(timezone.utc)
        released = 0
        for row in self.catalogue.releasable(now.isoformat()):
            if not self.running or not self.under_pressure():
                break
            try:
                if self.release_one(row, now):
                    released += 1
                    logger.warning("Released %s early: the archive disk is below %.1f GB "
                                   "free, and its cloud copy is proven",
                                   row["session_id"], floor / 1024 ** 3)
            except Exception as exc:
                self.failed += 1
                logger.error("Could not release the local WAV of %s: %s",
                             row["session_id"], exc)
        if released:
            self.released += released
            self.released_on_pressure += released
            logger.info("Released %s archived WAV(s) early under disk pressure; %.1f GB "
                        "free now", released,
                        archive.free_bytes(self.settings.archive_root) / 1024 ** 3)
        if self.under_pressure():
            if not self._pressure_warned:
                self._pressure_warned = True
                logger.critical("Archive disk still below %.1f GB free: every recording "
                                "left here is waiting for its cloud copy",
                                floor / 1024 ** 3)
        else:
            self._pressure_warned = False
        return released

    def release_pass(self, now: Optional[datetime] = None) -> int:
        """
        Remove archived WAVs from this disk that are safe elsewhere.

        Safe means three things, all checked here and none assumed: the server
        proved the cloud copy against the store's own fingerprint (only then
        does the catalogue record it as copied), the recording is older than
        the retention period, and the file on disk is still the one that was
        copied. With no copy key there are no copies, so nothing is released,
        ever. The JSON, the manifest and the catalogue row stay, and the
        manifest records where the copy is.
        """
        if self.settings.copy_key is None or self.settings.local_retention_days < 0:
            return 0
        now = now or datetime.now(timezone.utc)
        cutoff = now - timedelta(days=self.settings.local_retention_days)

        released = 0
        for row in self.catalogue.releasable(cutoff.isoformat()):
            if not self.running:
                break
            try:
                if self.release_one(row, now):
                    released += 1
            except Exception as exc:
                self.failed += 1
                logger.error("Could not release the local WAV of %s: %s",
                             row["session_id"], exc)
        if released:
            self.released += released
            logger.info("Released %s archived WAV(s) from this disk; their cloud "
                        "copies are proven", released)
        return released

    def release_one(self, row: Dict[str, Any], now: datetime) -> bool:
        if int(row.get("parts_count") or 1) > 1:
            return self.release_parts(row, now)
        audio = (self.settings.archive_root / row["archive_relpath"]).resolve()
        if self.settings.archive_root.resolve() not in audio.parents:
            raise CopyError(f"outside the archive: {row['archive_relpath']}")
        manifest = audio.with_suffix(".manifest.json")

        if not audio.exists():
            # Released before, and the run stopped before the catalogue knew.
            if archive.local_audio_released(manifest):
                archive.release_clinical_json(audio)
                self.catalogue.mark_released(row["session_id"], now.isoformat())
                return False
            raise CopyError(f"{audio.name} is missing and was never released - "
                            f"restore it from {row['copy_object_key']}")

        # The copy was made of a file with this hash. One that differs now has
        # been changed or damaged since; keep it for a person to look at.
        if archive.sha256_file(audio).hex() != row["sha256"]:
            raise CopyError(f"{audio.name} no longer matches the copied audio; kept")

        # Record first, delete second: a crash in between leaves a manifest
        # that says released beside a WAV still present, which the next pass
        # simply finishes.
        archive.record_local_release(
            manifest, released_at=now.isoformat(timespec="seconds"),
            copy_object_key=row["copy_object_key"], sha256=row["sha256"])
        audio.unlink()
        archive.release_clinical_json(audio)
        self.catalogue.mark_released(row["session_id"], now.isoformat())
        archive.update_day_index(audio.parent)
        logger.info("Released %s from this disk; the copy is %s",
                    audio.name, row["copy_object_key"])
        return True

    def release_parts(self, row: Dict[str, Any], now: datetime) -> bool:
        """
        Every consultation's file of one recording, or none of them: each must
        have a proven copy and still match it. One that does not keeps the
        whole recording here for a person to look at.
        """
        session_id = row["session_id"]
        parts = self.catalogue.parts_for(session_id)
        if len(parts) < 2 or not all(q["copied_at"] and q["copy_object_key"] for q in parts):
            return False
        pending = []
        for part in parts:
            audio = self._inside(part["archive_relpath"])
            manifest = audio.with_suffix(".manifest.json")
            if not audio.exists():
                if archive.local_audio_released(manifest):
                    archive.release_clinical_json(audio)
                    continue
                raise CopyError(f"{audio.name} is missing and was never released - "
                                f"restore it from {part['copy_object_key']}")
            if archive.sha256_file(audio).hex() != part["sha256"]:
                raise CopyError(f"{audio.name} no longer matches the copied audio; kept")
            pending.append((part, audio, manifest))
        for part, audio, manifest in pending:
            archive.record_local_release(
                manifest, released_at=now.isoformat(timespec="seconds"),
                copy_object_key=part["copy_object_key"], sha256=part["sha256"])
            audio.unlink()
            archive.release_clinical_json(audio)
            self.catalogue.mark_part_released(session_id, part["part_no"], now.isoformat())
            archive.update_day_index(audio.parent)
        self.catalogue.mark_released(session_id, now.isoformat())
        logger.info("Released %s consultation file(s) of %s from this disk",
                    len(pending), session_id)
        return bool(pending)

    def upload_copy(self, session_id: str, kind: str,
                    encrypted: "cloudcopy.Encrypted",
                    part_no: Optional[int] = None) -> Dict[str, Any]:
        """
        Put one encrypted object in the copy bucket.

        The copy is not read back here. The audio copy lives in cold storage,
        where reading it back means a restore of hours; and reading every copy
        back would cost more in traffic each month than the copies cost to
        keep. What proves the store holds it is the store's own fingerprint of
        the object, which the server asks for after this returns - so the size
        and the fingerprint go with the report.
        """
        request: Dict[str, Any] = {"session_id": session_id, "kind": kind}
        if part_no is not None:
            request["part_no"] = part_no
        place = self.post("archive/copy/authorize", request)
        with open(encrypted.path, "rb") as handle:
            response = self.session.put(
                place["upload_url"], data=handle,
                headers={"X-Worker-Key": None,
                         "Content-Type": "application/octet-stream"},
                timeout=self.settings.download_timeout, verify=self.settings.verify_tls)
        response.raise_for_status()

        return {"kind": kind, "object_key": place["object_key"],
                "version": place["version"], "bytes": encrypted.bytes,
                "sha256": encrypted.sha256, "plain_sha256": encrypted.plain_sha256,
                "md5": encrypted.md5}

    def report_copy_failure(self, session_id: str, step: str, message: str) -> None:
        """Make a repeated failure visible instead of silent (SRS-ARC-10)."""
        try:
            self.post("archive/copy/failed",
                      {"session_id": session_id, "step": step, "message": message})
        except Exception as exc:
            logger.warning("Could not report the copy failure for %s: %s",
                           session_id, exc)

    # ---- small helpers over the backend ----

    def fetch(self, path: str, limit: int) -> List[Dict[str, Any]]:
        response = self.session.get(
            f"{self.settings.backend_url}/api/v2/{path}", params={"limit": limit},
            timeout=self.settings.request_timeout, verify=self.settings.verify_tls)
        response.raise_for_status()
        return response.json().get("sessions", [])

    def post(self, path: str, body: Dict[str, Any]) -> Dict[str, Any]:
        response = self.session.post(
            f"{self.settings.backend_url}/api/v2/{path}", json=body,
            timeout=self.settings.request_timeout, verify=self.settings.verify_tls)
        response.raise_for_status()
        return response.json()


def main() -> int:
    # Before anything is written: sqlite, wave and tempfile create files with
    # the process umask, and patient audio is not for other users' eyes.
    os.umask(0o077)
    settings = Settings()
    problems = settings.problems()
    if problems:
        for problem in problems:
            logger.critical("CONFIGURATION: %s", problem)
        if not settings.backend_url or not settings.worker_key:
            return 1

    worker = ArchiveWorker(settings)
    signal.signal(signal.SIGINT, worker.stop)
    signal.signal(signal.SIGTERM, worker.stop)
    return worker.run()


if __name__ == "__main__":
    sys.exit(main())
