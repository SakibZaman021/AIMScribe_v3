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
  5. write manifest.json and _index.json
  6. POST /api/v2/archive/complete         backend issues the purge receipts

A session is only reported complete after step 4 succeeds. Any failure leaves the
session pending, the agent keeps its local audio, and the next pass retries.
"""
from __future__ import annotations

import logging
import os
import signal
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

from catalogue import Catalogue

sys.path.insert(0, str(Path(__file__).resolve().parent))

import archive
from archive import ArchiveError

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

    def problems(self) -> List[str]:
        issues = []
        if not self.backend_url:
            issues.append("AIMS_BACKEND_URL is not set")
        if not self.worker_key:
            issues.append("AIMS_WORKER_KEY is not set")
        if not self.backend_url.startswith("https://") and "localhost" not in self.backend_url:
            issues.append("AIMS_BACKEND_URL is not https - audio metadata would "
                          "cross the network in cleartext")
        return issues


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
        self.failed = 0

    # ---- lifecycle ----

    def stop(self, *_: Any) -> None:
        logger.info("Shutdown requested; finishing the current session first")
        self.running = False

    def run(self) -> int:
        root = self.settings.archive_root
        root.mkdir(parents=True, exist_ok=True)

        logger.info("=" * 64)
        logger.info("AIMScribe Archive Worker")
        logger.info("Backend : %s", self.settings.backend_url)
        logger.info("Archive : %s (%.1f GB free)",
                    root, archive.free_bytes(root) / 1024 ** 3)
        logger.info("Poll    : every %ss", self.settings.poll_seconds)
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

        logger.info("Stopped. Archived %s session(s), %s failure(s)",
                    self.archived, self.failed)
        return 0

    def _sleep(self, seconds: int) -> None:
        """Sleep in short slices so shutdown is responsive."""
        deadline = time.time() + seconds
        while self.running and time.time() < deadline:
            time.sleep(min(1.0, max(0.0, deadline - time.time())))

    # ---- main pass ----

    def drain_once(self) -> int:
        sessions = self.fetch_pending()
        if not sessions:
            return 0

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
        return processed

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

        # The filename comes first: the folder is named after it, so that one
        # directory is one consultation. A patient seen twice in a day gets two
        # folders, distinguished by the times in the name.
        filename = archive.archive_filename(
            patient_ref=session["patient_ref"], doctor_id=session["doctor_id"],
            hospital_id=session["hospital_id"],
            opened_at=opened_local, closed_at=closed_local)
        folder_name = filename[:-len(".wav")]

        directory = archive.session_directory(
            self.settings.archive_root, session["hospital_id"],
            session["doctor_id"], session_date, folder_name)

        # The name no longer carries the session ULID, so an existing file is only
        # ours if it is exactly the size this session would produce. Anything else
        # is a different consultation and must not be overwritten or re-reported.
        expected = archive.expected_join_bytes([int(s["bytes"]) for s in segments])
        destination, already_ours = archive.free_destination(directory, filename, expected)
        relpath = archive.relative_path(
            session["hospital_id"], session["doctor_id"], session_date,
            folder_name, destination.name)

        # Already done? Re-report rather than re-downloading; /archive/complete is
        # idempotent, and this is the normal path when a previous run was
        # interrupted between writing the file and reporting it.
        if already_ours:
            existing = archive.sha256_file(destination)
            logger.info("Session %s already present on disk; re-reporting", session_id)
            self.report_complete(session_id, relpath, existing, destination.stat().st_size)
            return

        total_bytes = sum(int(s["bytes"]) for s in segments)
        archive.ensure_space(self.settings.archive_root, total_bytes * 2,
                             headroom=self.settings.disk_headroom)
        directory.mkdir(parents=True, exist_ok=True)

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

        if self.report_complete(session_id, relpath, result.sha256, result.bytes):
            try:
                self.catalogue.mark_reported(session_id)
            except Exception as exc:
                logger.warning("Catalogue not marked reported for %s: %s", session_id, exc)

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
                        sha256: bytes, byte_length: int) -> bool:
        """
        Tell the backend the archive copy exists and hashes correctly.

        This is what causes purge receipts to be issued, and therefore the only
        thing that ever authorises a doctor PC to delete its local audio.

        Returns True when the backend accepted it. False means the audio is on
        disk but unacknowledged - the catalogue keeps it in `unreported()` and the
        agent rightly keeps its local copy.
        """
        response = self.session.post(
            f"{self.settings.backend_url}/api/v2/archive/complete",
            json={
                "session_id": session_id,
                "archive_relpath": relpath,
                "sha256": sha256.hex(),
                "bytes": byte_length,
            },
            timeout=self.settings.request_timeout,
            verify=self.settings.verify_tls,
        )
        if response.status_code == 409:
            logger.warning("Session %s is quarantined; no receipts issued", session_id)
            return False
        response.raise_for_status()
        body = response.json()
        logger.info("Session %s reported complete; %s purge receipt(s) issued, "
                    "%s clip(s) deleted from the bucket", session_id,
                    body.get("receipts_issued", 0), body.get("objects_deleted", 0))
        return True


def main() -> int:
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
