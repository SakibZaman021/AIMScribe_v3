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

  7. compress the WAV to FLAC          decode it again and compare the samples
  8. encrypt the FLAC and the JSON     with a key that never leaves this machine
  9. upload them                       the audio to cold storage, the JSON hot
 10. POST /api/v2/archive/copy/complete   the server checks what the store holds
                                          against what was sent, records the
                                          copy, and only then deletes the pieces

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
import cloudcopy
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

    def problems(self) -> List[str]:
        issues = []
        if not self.backend_url:
            issues.append("AIMS_BACKEND_URL is not set")
        if not self.worker_key:
            issues.append("AIMS_WORKER_KEY is not set")
        if not self.backend_url.startswith("https://") and not self._internal():
            issues.append("AIMS_BACKEND_URL is not https - audio metadata would "
                          "cross the network in cleartext")
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
        logger.info("Copies  : %s", "on, encrypted at UIU"
                    if self.settings.copy_key else
                    "OFF - AIMS_COPY_KEY is not set, so no cloud copy is kept")
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

        logger.info("Stopped. Archived %s session(s), copied %s, %s failure(s)",
                    self.archived, self.copied, self.failed)
        return 0

    def _sleep(self, seconds: int) -> None:
        """Sleep in short slices so shutdown is responsive."""
        deadline = time.time() + seconds
        while self.running and time.time() < deadline:
            time.sleep(min(1.0, max(0.0, deadline - time.time())))

    # ---- main pass ----

    def drain_once(self) -> int:
        self.sweep()
        sessions = self.fetch_pending()
        if not sessions:
            return self.copy_pass()

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

        # The cloud copy runs after the archiving, on what is already on disk.
        return processed + self.copy_pass()

    def sweep(self) -> None:
        """
        Ask the server to settle confirmation deadlines (SRS 3.2 §5.6):
        two minutes without CMED's API 2 makes a recording unconfirmed, and 24
        hours erases it. Best effort - archiving goes on if this fails.
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
            self.write_clinical_json(session_id, destination, recording={
                "audio_sha256": existing.hex(),
                "audio_bytes": destination.stat().st_size,
                "archive_relpath": relpath,
            })
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
        # The clinical record travels with the audio, in the same folder and
        # under the same name (SRS-CRI-05, §8.8), carrying the fingerprint of
        # the file it sits beside.
        self.write_clinical_json(session_id, destination, recording={
            "audio_sha256": result.sha256.hex(),
            "audio_bytes": result.bytes,
            "duration_seconds": round(result.duration_seconds, 3),
            "archive_relpath": relpath,
        })
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

    def write_clinical_json(self, session_id: str, audio_path: Path,
                            recording: Optional[Dict[str, Any]] = None) -> Path:
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
        response = self.session.get(
            f"{self.settings.backend_url}/api/v2/archive/clinical/{session_id}",
            timeout=self.settings.request_timeout, verify=self.settings.verify_tls)
        if response.status_code >= 300:
            raise ArchiveError(f"clinical record unavailable "
                               f"({response.status_code}); will retry")
        document = response.json()
        if recording:
            document["recording"] = {**(document.get("recording") or {}), **recording}
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

    def copy_session(self, session: Dict[str, Any]) -> None:
        """The steps of SRS-ARC-09, in order, each proven before the next."""
        session_id = session["session_id"]
        audio = self.archive_file(session)

        # 1. The archived WAV, still exactly what was reported.
        if session.get("archive_sha256"):
            on_disk = archive.sha256_file(audio).hex()
            if on_disk != session["archive_sha256"]:
                raise CopyError(f"{audio.name} on disk no longer matches what was "
                                f"archived; not copying it")

        json_path = archive.clinical_json_path(audio)
        if not json_path.exists():
            self.write_clinical_json(session_id, audio)

        with tempfile.TemporaryDirectory(prefix=f"aimscopy_{session_id}_") as scratch:
            work = Path(scratch)
            stem = audio.stem

            # 2 and 3. Compress, then decode again and compare the samples.
            flac = cloudcopy.to_flac(audio, work / f"{stem}.flac")
            if not cloudcopy.flac_matches_wav(flac, audio):
                raise CopyError("the FLAC does not decode to the archived audio; "
                                "the pieces stay where they are")

            # 4 and 5. Encrypt here, upload, read back, compare.
            audio_copy = self.upload_copy(
                session_id, "audio",
                cloudcopy.encrypt_file(flac, work / f"{stem}.flac.enc",
                                       self.settings.copy_key))
            json_copy = self.upload_copy(
                session_id, "json",
                cloudcopy.encrypt_file(json_path, work / f"{stem}.json.enc",
                                       self.settings.copy_key))

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
        logger.info("Cloud copy of %s stored (%.1f MB FLAC); %s piece(s) deleted",
                    session_id, audio_copy["bytes"] / 1024 ** 2,
                    result.get("objects_deleted", 0))

    def rewrite_json(self, session: Dict[str, Any]) -> None:
        """
        A prescription arrived after archiving: write the JSON again and upload
        it as a new object. The earlier ones are kept (SRS-ARC-13).
        """
        session_id = session["session_id"]
        audio = self.archive_file(session)
        json_path = self.write_clinical_json(session_id, audio)

        copies = []
        if self.settings.copy_key is not None:
            with tempfile.TemporaryDirectory(prefix=f"aimsjson_{session_id}_") as scratch:
                copies.append(self.upload_copy(
                    session_id, "json",
                    cloudcopy.encrypt_file(json_path,
                                           Path(scratch) / f"{audio.stem}.json.enc",
                                           self.settings.copy_key)))
        self.post("archive/copy/complete",
                  {"session_id": session_id, "final": False, "copies": copies})
        logger.info("Wrote the JSON for %s again after a late prescription", session_id)

    def archive_file(self, session: Dict[str, Any]) -> Path:
        relpath = session.get("archive_relpath") or ""
        audio = (self.settings.archive_root / relpath).resolve()
        if not audio.is_file() or self.settings.archive_root.resolve() not in audio.parents:
            raise CopyError(f"archived file missing or outside the archive: {relpath}")
        return audio

    def upload_copy(self, session_id: str, kind: str,
                    encrypted: "cloudcopy.Encrypted") -> Dict[str, Any]:
        """
        Put one encrypted object in the copy bucket.

        The copy is not read back here. The audio copy lives in cold storage,
        where reading it back means a restore of hours; and reading every copy
        back would cost more in traffic each month than the copies cost to
        keep. What proves the store holds it is the store's own fingerprint of
        the object, which the server asks for after this returns - so the size
        and the fingerprint go with the report.
        """
        place = self.post("archive/copy/authorize",
                          {"session_id": session_id, "kind": kind})
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
