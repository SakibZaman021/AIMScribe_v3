"""
Encrypted, crash-safe segment spool.

This is the doctor PC's local copy of a consultation and the reason a backend
outage is a delay rather than data loss. Properties it guarantees:

* **Sealed before sent.** A segment is written, hashed and added to the chain
  before any upload is attempted, so its content is fixed locally and the network
  becomes a delivery problem only.
* **Encrypted at rest.** AES-256-GCM per segment, key wrapped by Windows DPAPI.
  A doctor with file access cannot listen to or edit buffered audio.
* **Crash-safe.** Every state change is appended to a journal and fsynced, so a
  power cut loses at most the segment still being captured.
* **Never silently dropped.** Segments leave only when a signed purge receipt
  proves the archive copy exists. A full spool escalates; it does not evict.

Session IDs are minted locally as ULIDs so recording can start with the backend
unreachable. The server adopts the agent's ID, which also makes re-opening a
session after a restart naturally idempotent.
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import struct
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Any, Dict, Iterable, List, Optional

from core import crypto
from core.crypto import ChainEntry, DeviceKey

logger = logging.getLogger(__name__)

JOURNAL_NAME = "journal.jsonl"
SEGMENT_SUFFIX = ".aimspl"
SPOOL_KEY_NAME = "spool.key"

# Segment lifecycle on this machine.
PENDING = "pending"        # sealed locally, not yet accepted by the server
COMMITTED = "committed"    # server verified the hash and stored the row
RECEIPTED = "receipted"    # archive copy verified; safe to delete after the grace window
PURGED = "purged"          # local file deleted
QUARANTINED = "quarantined"  # hash dispute; never deleted automatically

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


# ============================================================
# Identifiers
# ============================================================

def new_ulid(when: Optional[datetime] = None) -> str:
    """
    26-character Crockford base32 ULID: 48-bit millisecond timestamp, 80 random bits.

    Chronologically sortable, filename-safe, and carries no patient information -
    which is why it, not the patient ID, is what appears in object keys.
    """
    moment = when or datetime.now(timezone.utc)
    timestamp = int(moment.timestamp() * 1000) & ((1 << 48) - 1)
    value = (timestamp << 80) | int.from_bytes(secrets.token_bytes(10), "big")
    out = []
    for shift in range(125, -1, -5):
        out.append(_CROCKFORD[(value >> shift) & 0x1F])
    return "".join(out)


# ============================================================
# WAV framing
# ============================================================

def wav_bytes(pcm: bytes, *, sample_rate: int, channels: int, sample_width: int) -> bytes:
    """
    Wrap PCM in a canonical 44-byte RIFF/WAVE header.

    Written by hand rather than through the `wave` module so the byte layout is
    fixed and deterministic: the server recomputes sha256 over exactly these
    bytes, and any header variation would break the comparison.
    """
    byte_rate = sample_rate * channels * sample_width
    block_align = channels * sample_width
    return b"".join((
        b"RIFF",
        struct.pack("<I", 36 + len(pcm)),
        b"WAVEfmt ",
        struct.pack("<IHHIIHH", 16, 1, channels, sample_rate, byte_rate,
                    block_align, sample_width * 8),
        b"data",
        struct.pack("<I", len(pcm)),
        pcm,
    ))


# ============================================================
# Records
# ============================================================

@dataclass
class SpoolSegment:
    seq_no: int
    filename: str
    sha256: bytes
    byte_length: int
    duration_seconds: float
    captured_start_at: datetime
    captured_end_at: datetime
    rms_mean: float
    is_final: bool
    entry_no: int
    state: str = PENDING
    object_key: Optional[str] = None
    receipt_at: Optional[float] = None
    attempts: int = 0
    last_error: str = ""

    @property
    def deletable_after(self) -> Optional[float]:
        return self.receipt_at


# ============================================================
# One session's spool directory
# ============================================================

class SessionSpool:
    """
    A single consultation on disk: its journal, its chain, and its segment files.

    Not safe for concurrent writers; the uploader and the capture pipeline both go
    through the owning SessionController, and an internal lock guards the journal.
    """

    def __init__(
        self,
        directory: Path,
        *,
        session_id: str,
        device_key: DeviceKey,
        spool_key: bytes,
        audio: Dict[str, int],
    ):
        self.directory = directory
        self.session_id = session_id
        self.audio = audio
        self._device_key = device_key
        self._spool_key = spool_key
        self._lock = RLock()

        self.chain: List[ChainEntry] = []
        self.segments: Dict[int, SpoolSegment] = {}
        self.closed_at: Optional[datetime] = None
        self.opened_at: Optional[datetime] = None
        self.meta: Dict[str, Any] = {}
        self.server_acknowledged = False
        # Set only once the backend has accepted /session/close. Until then the
        # directory must survive, even with every segment archived, or a close that
        # failed during an outage would never be retried.
        self.close_reported = False
        self.duration_seconds = 0.0
        self.paused_seconds = 0.0
        self.close_reason = ""
        # Chain entries the backend has acknowledged. Segments track their own
        # state; this covers pause and resume, which are links in the same chain
        # and were previously sent once and forgotten.
        self.reported_entries: set = set()

        self._journal_path = directory / JOURNAL_NAME

    # ---- construction ----

    @classmethod
    def create(
        cls,
        root: Path,
        *,
        device_key: DeviceKey,
        spool_key: bytes,
        device_id: str,
        doctor_id: str,
        hospital_id: str,
        patient_ref: str,
        consent_method: str,
        audio: Dict[str, int],
        session_id: Optional[str] = None,
        opened_at: Optional[datetime] = None,
    ) -> "SessionSpool":
        session_id = session_id or new_ulid()
        directory = root / session_id
        directory.mkdir(parents=True, exist_ok=True)

        spool = cls(directory, session_id=session_id, device_key=device_key,
                    spool_key=spool_key, audio=audio)
        spool.opened_at = opened_at or datetime.now(timezone.utc)
        spool.meta = {
            "device_id": device_id,
            "doctor_id": doctor_id,
            "hospital_id": hospital_id,
            "patient_ref": patient_ref,
            "consent_method": consent_method,
        }

        spool._append_journal({
            "rec": "session",
            "session_id": session_id,
            "opened_at": crypto.iso_utc(spool.opened_at),
            "audio": audio,
            **spool.meta,
        })

        # Entry 0 binds the session's identity into the chain. Nothing downstream
        # can be re-attributed to a different doctor, hospital or patient without
        # breaking every subsequent hash.
        spool.append_chain_entry("open", crypto.open_payload(
            device_id=device_id,
            doctor_id=doctor_id,
            hospital_id=hospital_id,
            patient_ref=patient_ref,
            opened_at=spool.opened_at,
            sample_rate=audio["sample_rate"],
            channels=audio["channels"],
            sample_width=audio["sample_width"],
        ))
        return spool

    @classmethod
    def load(
        cls, directory: Path, *, device_key: DeviceKey, spool_key: bytes
    ) -> Optional["SessionSpool"]:
        """Rebuild a session from its journal after a crash or restart."""
        journal = directory / JOURNAL_NAME
        if not journal.is_file():
            return None

        spool: Optional["SessionSpool"] = None
        prev_hash: Optional[bytes] = None

        with open(journal, "r", encoding="utf-8") as handle:
            for line_no, line in enumerate(handle, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    # A torn final line is the expected result of a power cut.
                    logger.warning("Ignoring truncated journal line %s in %s", line_no, directory)
                    continue

                kind = record.get("rec")

                if kind == "session":
                    spool = cls(directory, session_id=record["session_id"],
                                device_key=device_key, spool_key=spool_key,
                                audio=record.get("audio", {}))
                    spool.opened_at = _parse_iso(record.get("opened_at"))
                    spool.meta = {
                        key: record.get(key, "")
                        for key in ("device_id", "doctor_id", "hospital_id",
                                    "patient_ref", "consent_method")
                    }
                elif spool is None:
                    continue
                elif kind == "chain":
                    entry = _entry_from_journal(record["entry"])
                    spool.chain.append(entry)
                    prev_hash = entry.entry_hash
                elif kind == "segment":
                    segment = SpoolSegment(
                        seq_no=record["seq_no"],
                        filename=record["file"],
                        sha256=bytes.fromhex(record["sha256"]),
                        byte_length=record["bytes"],
                        duration_seconds=record["duration_seconds"],
                        captured_start_at=_parse_iso(record["captured_start_at"]),
                        captured_end_at=_parse_iso(record["captured_end_at"]),
                        rms_mean=record.get("rms_mean", 0.0),
                        is_final=record.get("is_final", False),
                        entry_no=record["entry_no"],
                    )
                    spool.segments[segment.seq_no] = segment
                elif kind == "state":
                    segment = spool.segments.get(record["seq_no"])
                    if segment:
                        segment.state = record["state"]
                        segment.object_key = record.get("object_key", segment.object_key)
                elif kind == "receipt":
                    segment = spool.segments.get(record["seq_no"])
                    if segment:
                        segment.state = RECEIPTED
                        segment.receipt_at = record.get("at", time.time())
                elif kind == "acknowledged":
                    spool.server_acknowledged = True
                elif kind == "closed":
                    spool.closed_at = _parse_iso(record.get("at"))
                    spool.duration_seconds = record.get("duration_seconds", 0.0)
                    spool.paused_seconds = record.get("paused_seconds", 0.0)
                    # Survives a crash, so a session closed from the tray and
                    # reported after a restart still reports why.
                    spool.close_reason = record.get("reason", "")
                elif kind == "close_reported":
                    spool.close_reported = True
                elif kind == "entry_reported":
                    spool.reported_entries.add(record["entry_no"])

        return spool

    # ---- chain ----

    @property
    def head_hash(self) -> Optional[bytes]:
        return self.chain[-1].entry_hash if self.chain else None

    @property
    def next_entry_no(self) -> int:
        return len(self.chain)

    def append_chain_entry(self, entry_type: str, payload: Dict[str, Any]) -> ChainEntry:
        with self._lock:
            entry = crypto.build_entry(
                entry_no=self.next_entry_no,
                entry_type=entry_type,
                payload=payload,
                prev_hash=self.head_hash,
                signer=self._device_key,
            )
            self.chain.append(entry)
            self._append_journal({"rec": "chain", "entry": entry.to_wire()})
            return entry

    def verify_chain(self) -> crypto.ChainVerdict:
        return crypto.verify_chain(self.chain, device_public_key=self._device_key.public_key())

    # ---- segments ----

    def seal_segment(
        self,
        pcm: bytes,
        *,
        captured_start_at: datetime,
        captured_end_at: datetime,
        rms_mean: float,
        is_final: bool,
    ) -> SpoolSegment:
        """
        Write one WAV segment to the spool and extend the chain.

        Called from the writer thread, never from the audio capture thread.
        """
        with self._lock:
            seq_no = max(self.segments, default=0) + 1
            audio = wav_bytes(
                pcm,
                sample_rate=self.audio["sample_rate"],
                channels=self.audio["channels"],
                sample_width=self.audio["sample_width"],
            )
            # Hash the plaintext WAV: this is exactly what the server receives and
            # re-hashes, and what the purge receipt will attest to.
            audio_hash = crypto.sha256_bytes(audio)
            duration = len(pcm) / max(1, self.audio["sample_rate"]
                                      * self.audio["channels"] * self.audio["sample_width"])

            entry = self.append_chain_entry("segment", crypto.segment_payload(
                seq_no=seq_no,
                audio_sha256=audio_hash,
                byte_length=len(audio),
                duration_seconds=duration,
                captured_start_at=captured_start_at,
                captured_end_at=captured_end_at,
                rms_mean=rms_mean,
                is_final=is_final,
            ))

            filename = f"seg-{seq_no:05d}{SEGMENT_SUFFIX}"
            blob = crypto.encrypt_spool_blob(
                self._spool_key, audio,
                associated=f"{self.session_id}:{seq_no}".encode("ascii"),
            )
            _atomic_write(self.directory / filename, blob)

            segment = SpoolSegment(
                seq_no=seq_no,
                filename=filename,
                sha256=audio_hash,
                byte_length=len(audio),
                duration_seconds=duration,
                captured_start_at=captured_start_at,
                captured_end_at=captured_end_at,
                rms_mean=rms_mean,
                is_final=is_final,
                entry_no=entry.entry_no,
            )
            self.segments[seq_no] = segment

            self._append_journal({
                "rec": "segment",
                "seq_no": seq_no,
                "file": filename,
                "sha256": audio_hash.hex(),
                "bytes": len(audio),
                "duration_seconds": round(duration, 3),
                "captured_start_at": crypto.iso_utc(captured_start_at),
                "captured_end_at": crypto.iso_utc(captured_end_at),
                "rms_mean": round(rms_mean, 2),
                "is_final": is_final,
                "entry_no": entry.entry_no,
            })

            logger.info("Sealed segment %s of session %s (%.1f s, %s bytes)",
                        seq_no, self.session_id, duration, len(audio))
            return segment

    def read_segment(self, seq_no: int) -> bytes:
        """Decrypt one segment and re-verify its hash before handing it to the uploader."""
        segment = self.segments[seq_no]
        blob = (self.directory / segment.filename).read_bytes()
        audio = crypto.decrypt_spool_blob(
            self._spool_key, blob,
            associated=f"{self.session_id}:{seq_no}".encode("ascii"),
        )
        if crypto.sha256_bytes(audio) != segment.sha256:
            raise ValueError(
                f"segment {seq_no} of {self.session_id} does not match its recorded hash")
        return audio

    def quarantined_segments(self) -> List[SpoolSegment]:
        """Segments the backend or a local hash check rejected."""
        return [s for s in sorted(self.segments.values(), key=lambda x: x.seq_no)
                if s.state == QUARANTINED]

    def pending_segments(self) -> List[SpoolSegment]:
        return [s for s in sorted(self.segments.values(), key=lambda x: x.seq_no)
                if s.state == PENDING]

    def set_state(self, seq_no: int, state: str, *, object_key: Optional[str] = None) -> None:
        with self._lock:
            segment = self.segments.get(seq_no)
            if not segment:
                return
            segment.state = state
            if object_key:
                segment.object_key = object_key
            self._append_journal({
                "rec": "state", "seq_no": seq_no, "state": state,
                "object_key": segment.object_key, "at": time.time(),
            })

    def record_receipt(self, seq_no: int, payload: Dict[str, Any], signature: bytes) -> None:
        """Store a verified purge receipt. Deletion happens later, after the grace window."""
        with self._lock:
            segment = self.segments.get(seq_no)
            if not segment:
                return
            segment.state = RECEIPTED
            segment.receipt_at = time.time()
            self._append_journal({
                "rec": "receipt", "seq_no": seq_no, "payload": payload,
                "signature": signature.hex(), "at": segment.receipt_at,
            })

    def purge_segment(self, seq_no: int) -> bool:
        """
        Delete a local segment file. Only ever called for a RECEIPTED segment.

        Returns True if a file was removed.
        """
        with self._lock:
            segment = self.segments.get(seq_no)
            if not segment or segment.state != RECEIPTED:
                return False
            path = self.directory / segment.filename
            try:
                if path.exists():
                    path.unlink()
            except OSError as exc:
                logger.warning("Could not delete %s: %s", path, exc)
                return False
            segment.state = PURGED
            self._append_journal({"rec": "state", "seq_no": seq_no,
                                  "state": PURGED, "at": time.time()})
            logger.info("Purged local segment %s of session %s", seq_no, self.session_id)
            return True

    # ---- lifecycle ----

    def mark_acknowledged(self) -> None:
        with self._lock:
            if not self.server_acknowledged:
                self.server_acknowledged = True
                self._append_journal({"rec": "acknowledged", "at": time.time()})

    def close(self, *, duration_seconds: float, paused_seconds: float,
              reason: str = "", at: Optional[datetime] = None) -> ChainEntry:
        with self._lock:
            # `at` lets the caller supply the exact instant. When one patient
            # supersedes another, CMED's record needs this consultation to end
            # at precisely the moment the next one begins - not a few hundred
            # milliseconds later, once the microphone has been torn down and the
            # final segment sealed.
            self.closed_at = at or datetime.now(timezone.utc)
            self.duration_seconds = duration_seconds
            self.paused_seconds = paused_seconds
            self.close_reason = reason
            entry = self.append_chain_entry("close", crypto.close_payload(
                closed_at=self.closed_at,
                segment_count=len(self.segments),
                duration_seconds=duration_seconds,
                paused_seconds=paused_seconds,
                reason=reason,
            ))
            self._append_journal({
                "rec": "closed",
                "at": crypto.iso_utc(self.closed_at),
                "reason": reason,
                "duration_seconds": round(duration_seconds, 3),
                "paused_seconds": round(paused_seconds, 3),
            })
            return entry

    def mark_entry_reported(self, entry_no: int) -> None:
        with self._lock:
            if entry_no in self.reported_entries:
                return
            self.reported_entries.add(entry_no)
            self._append_journal({"rec": "entry_reported", "entry_no": entry_no})

    def pending_notifications(self) -> List[ChainEntry]:
        """
        Pause and resume entries the backend has not acknowledged.

        These are chain links, not status updates. If one is missing, every entry
        after it fails verification - the backend sees a prev_hash that does not
        follow its stored head and quarantines the session, which is what
        happened to a real consultation.
        """
        with self._lock:
            return [e for e in self.chain
                    if e.entry_type in ("pause", "resume")
                    and e.entry_no not in self.reported_entries]

    def mark_close_reported(self) -> None:
        with self._lock:
            if not self.close_reported:
                self.close_reported = True
                self._append_journal({"rec": "close_reported", "at": time.time()})

    @property
    def is_complete(self) -> bool:
        """
        Safe to remove this directory.

        Requires all four: the backend knows about the session, it was closed, the
        close was accepted, and every segment has been purged after a verified
        receipt. Dropping any one of these can delete the journal for a session the
        server has not finished accounting for.
        """
        return (
            self.server_acknowledged
            and self.closed_at is not None
            and self.close_reported
            and all(segment.state == PURGED for segment in self.segments.values())
        )

    def bytes_on_disk(self) -> int:
        total = 0
        for item in self.directory.glob(f"*{SEGMENT_SUFFIX}"):
            try:
                total += item.stat().st_size
            except OSError:
                pass
        return total

    def manifest(self) -> Dict[str, Any]:
        """Self-describing record shipped with the session, so the archive verifies alone."""
        return {
            "session_id": self.session_id,
            "protocol_version": 2,
            "opened_at": crypto.iso_utc(self.opened_at) if self.opened_at else None,
            "closed_at": crypto.iso_utc(self.closed_at) if self.closed_at else None,
            "audio": {"codec": "pcm_s16le", "container": "wav", **self.audio},
            **self.meta,
            "chain_head": self.head_hash.hex() if self.head_hash else None,
            "device_pubkey": self._device_key.public_bytes_raw().hex(),
            "segments": [
                {
                    "seq_no": s.seq_no,
                    "sha256": s.sha256.hex(),
                    "bytes": s.byte_length,
                    "duration_seconds": s.duration_seconds,
                    "captured_start_at": crypto.iso_utc(s.captured_start_at),
                    "captured_end_at": crypto.iso_utc(s.captured_end_at),
                    "is_final": s.is_final,
                }
                for s in sorted(self.segments.values(), key=lambda x: x.seq_no)
            ],
            "chain": [entry.to_wire() for entry in self.chain],
        }

    # ---- journal ----

    def _append_journal(self, record: Dict[str, Any]) -> None:
        """
        Append and fsync.

        The fsync is the whole point: without it a power cut can leave the journal
        describing a segment file that was never written, or vice versa.
        """
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
        with open(self._journal_path, "a", encoding="utf-8") as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())


# ============================================================
# Spool root
# ============================================================

class Spool:
    """Owns the spool directory, the machine spool key, and capacity accounting."""

    def __init__(self, root: Path, *, key_path: Path, allow_plaintext: bool,
                 max_bytes: int, warn_bytes: int, critical_bytes: int):
        self.root = root
        self.max_bytes = max_bytes
        self.warn_bytes = warn_bytes
        self.critical_bytes = critical_bytes
        self.root.mkdir(parents=True, exist_ok=True)
        self._key = self._load_or_create_key(key_path, allow_plaintext=allow_plaintext)

    @staticmethod
    def _load_or_create_key(path: Path, *, allow_plaintext: bool) -> bytes:
        if path.is_file():
            return crypto.unwrap_secret(path.read_bytes(), allow_plaintext=allow_plaintext)
        key = crypto.new_spool_key()
        path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(path, crypto.wrap_secret(key, allow_plaintext=allow_plaintext))
        logger.info("Created a new spool encryption key at %s", path)
        return key

    @classmethod
    def from_config(cls, cfg) -> "Spool":
        return cls(
            cfg.spool.directory,
            key_path=cfg.security.device_key_path.parent / SPOOL_KEY_NAME,
            allow_plaintext=cfg.security.allow_plaintext_keystore,
            max_bytes=cfg.spool.max_bytes,
            warn_bytes=cfg.spool.warn_bytes,
            critical_bytes=cfg.spool.critical_bytes,
        )

    def open_session(self, *, device_key: DeviceKey, **kwargs) -> SessionSpool:
        return SessionSpool.create(self.root, device_key=device_key,
                                   spool_key=self._key, **kwargs)

    def recover(self, device_key: DeviceKey) -> List[SessionSpool]:
        """
        Load every session directory left behind by a previous run.

        Sessions with unsent segments resume uploading; sessions that were open
        when the process died are closed short, with the interruption recorded.
        """
        recovered: List[SessionSpool] = []
        for directory in sorted(self.root.iterdir()):
            if not directory.is_dir():
                continue
            try:
                session = SessionSpool.load(directory, device_key=device_key, spool_key=self._key)
            except Exception as exc:
                logger.error("Could not load spooled session %s: %s", directory.name, exc)
                continue
            if session is None:
                continue
            if session.is_complete:
                _remove_tree(directory)
                continue
            recovered.append(session)

        if recovered:
            logger.warning("Recovered %s spooled session(s) from a previous run", len(recovered))
        return recovered

    def total_bytes(self) -> int:
        total = 0
        for directory in self.root.iterdir():
            if not directory.is_dir():
                continue
            for item in directory.glob(f"*{SEGMENT_SUFFIX}"):
                try:
                    total += item.stat().st_size
                except OSError:
                    pass
        return total

    def pressure(self) -> str:
        """'ok', 'warn', or 'critical'. A full spool escalates instead of evicting."""
        used = self.total_bytes()
        if used >= self.critical_bytes:
            return "critical"
        if used >= self.warn_bytes:
            return "warn"
        return "ok"

    def has_capacity(self, needed_bytes: int) -> bool:
        return self.total_bytes() + needed_bytes <= self.max_bytes

    def discard(self, session: SessionSpool) -> None:
        """Remove a fully archived and purged session directory."""
        if session.is_complete:
            _remove_tree(session.directory)


# ============================================================
# Helpers
# ============================================================

def _atomic_write(path: Path, data: bytes) -> None:
    """Write to a temporary file, fsync it, then rename into place."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def _remove_tree(directory: Path) -> None:
    try:
        for item in directory.iterdir():
            try:
                item.unlink()
            except OSError:
                pass
        directory.rmdir()
    except OSError as exc:
        logger.debug("Could not remove %s: %s", directory, exc)


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _entry_from_journal(data: Dict[str, Any]) -> ChainEntry:
    return ChainEntry(
        entry_no=data["entry_no"],
        entry_type=data["entry_type"],
        payload=data["payload"],
        payload_sha256=bytes.fromhex(data["payload_sha256"]),
        prev_hash=bytes.fromhex(data["prev_hash"]) if data.get("prev_hash") else None,
        entry_hash=bytes.fromhex(data["entry_hash"]),
        signature=bytes.fromhex(data["signature"]) if data.get("signature") else None,
    )


__all__ = [
    "COMMITTED", "PENDING", "PURGED", "QUARANTINED", "RECEIPTED",
    "SessionSpool", "Spool", "SpoolSegment", "new_ulid", "wav_bytes",
]
