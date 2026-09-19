"""
AIMScribe v2 - server side of the integrity chain.

This module is the mirror of the agent's `core/crypto.py`. Every constant and
every hashing rule here must match it byte for byte, or valid chains will be
rejected and the whole scheme becomes noise. If you change one side, change both.

That requirement is no longer left to memory. `tests/wire_vectors.json` holds
fixed inputs and the exact outputs the format requires; `tests/test_wire_
compatibility.py` replays every one of them against the code below, and an
identical copy of the vectors is checked into the agent repository where its
test suite does the same. Change a rule here and this repository's own tests
fail, with no need for the agent to be checked out.

Regenerating the vectors redefines the protocol and is a two-repository change:
run `recorder/scripts/gen_wire_vectors.py`, copy the result to both, and update
`EXPECTED_SHA256` in both test files.

Responsibilities:

* **Verify chains.** Recompute what the agent claims, so a deleted, reordered or
  substituted segment is detected server-side rather than taken on trust.
* **Verify device signatures.** Each entry is signed by the machine's Ed25519 key,
  registered at enrollment. A rebuilt chain from an attacker fails here even when
  it is internally consistent.
* **Sign purge receipts.** The statement that lets an agent delete its only local
  copy. Nothing else in the system is allowed to authorise that.
* **Append to the audit log.** Hash-chained, so the log itself cannot be quietly
  rewritten.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

logger = logging.getLogger(__name__)

# MUST match recorder/core/crypto.py exactly.
CHAIN_DOMAIN = b"aimscribe.chain.v2"
RECEIPT_DOMAIN = b"aimscribe.receipt.v2"
AUDIT_DOMAIN = b"aimscribe.audit.v2"

VALID_ENTRY_TYPES = frozenset({"open", "segment", "pause", "resume", "close"})


# ============================================================
# Primitives - mirrored from the agent
# ============================================================

def digest(*parts: bytes) -> bytes:
    """
    SHA-256 over length-prefixed parts.

    Prefixing is what makes ("ab","c") and ("a","bc") hash differently; a plain
    concatenation would let an attacker shift a field boundary to forge a match.
    """
    h = hashlib.sha256()
    for part in parts:
        h.update(len(part).to_bytes(4, "big"))
        h.update(part)
    return h.digest()


def sha256_bytes(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()


def canonical_json(payload: Any) -> bytes:
    """Deterministic JSON. Identical settings to the agent, or hashes diverge."""
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=_json_default,
    ).encode("utf-8")


def _json_default(value: Any) -> str:
    if isinstance(value, datetime):
        return iso_utc(value)
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).hex()
    # Unreachable from the wire, where a payload has already been through JSON
    # and holds only primitives. Present so this function is behaviourally
    # identical to the agent's, which does serialise Path: two canonicalisers
    # that accept different type sets are two canonicalisers, and the next
    # payload field to carry one would hash on one side and raise on the other.
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"cannot serialise {type(value).__name__} for hashing")


def iso_utc(moment: datetime) -> str:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def entry_hash(
    *, prev_hash: Optional[bytes], entry_no: int, entry_type: str, payload_sha256: bytes
) -> bytes:
    return digest(
        CHAIN_DOMAIN,
        entry_type.encode("ascii"),
        prev_hash or b"",
        str(entry_no).encode("ascii"),
        payload_sha256,
    )


# ============================================================
# Wire parsing
# ============================================================

@dataclass(frozen=True)
class ChainEntry:
    entry_no: int
    entry_type: str
    payload: Dict[str, Any]
    payload_sha256: bytes
    prev_hash: Optional[bytes]
    entry_hash: bytes
    signature: Optional[bytes]

    @property
    def occurred_at(self) -> Optional[datetime]:
        raw = self.payload.get("at") or self.payload.get("opened_at") \
            or self.payload.get("closed_at") or self.payload.get("captured_end_at")
        if not raw:
            return None
        try:
            return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except ValueError:
            return None


class ChainError(ValueError):
    """A chain entry that cannot be trusted. Never store one silently."""


def parse_entry(raw: Dict[str, Any]) -> ChainEntry:
    """
    Turn an agent-supplied entry into a validated ChainEntry.

    Every field is checked here rather than trusted, because this is the boundary
    between our data and a client's claims.
    """
    if not isinstance(raw, dict):
        raise ChainError("chain entry must be an object")

    try:
        entry_no = int(raw["entry_no"])
        entry_type = str(raw["entry_type"])
        payload = raw["payload"]
        payload_sha256 = bytes.fromhex(raw["payload_sha256"])
        entry_hash_bytes = bytes.fromhex(raw["entry_hash"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ChainError(f"malformed chain entry: {exc}") from exc

    if entry_no < 0:
        raise ChainError("entry_no must not be negative")
    if entry_type not in VALID_ENTRY_TYPES:
        raise ChainError(f"unknown entry_type {entry_type!r}")
    if not isinstance(payload, dict):
        raise ChainError("payload must be an object")
    if len(payload_sha256) != 32 or len(entry_hash_bytes) != 32:
        raise ChainError("hashes must be 32 bytes")

    prev_raw = raw.get("prev_hash")
    prev_hash = bytes.fromhex(prev_raw) if prev_raw else None
    if prev_hash is not None and len(prev_hash) != 32:
        raise ChainError("prev_hash must be 32 bytes")

    sig_raw = raw.get("signature")
    signature = bytes.fromhex(sig_raw) if sig_raw else None
    if signature is not None and len(signature) != 64:
        raise ChainError("signature must be 64 bytes")

    return ChainEntry(
        entry_no=entry_no,
        entry_type=entry_type,
        payload=payload,
        payload_sha256=payload_sha256,
        prev_hash=prev_hash,
        entry_hash=entry_hash_bytes,
        signature=signature,
    )


# ============================================================
# Verification
# ============================================================

@dataclass(frozen=True)
class Verdict:
    ok: bool
    reason: str = ""
    failed_entry_no: Optional[int] = None

    def __bool__(self) -> bool:
        return self.ok


def verify_entry(
    entry: ChainEntry,
    *,
    expected_prev: Optional[bytes],
    device_pubkey: Optional[bytes] = None,
) -> Verdict:
    """
    Check one entry in isolation. Used at /segment/commit, where entries arrive
    one at a time and must be validated before anything is stored.
    """
    if entry.payload_sha256 != sha256_bytes(canonical_json(entry.payload)):
        return Verdict(False, "payload does not match its hash", entry.entry_no)

    if entry.prev_hash != expected_prev:
        return Verdict(False, "prev_hash does not follow the stored chain", entry.entry_no)

    expected = entry_hash(
        prev_hash=entry.prev_hash,
        entry_no=entry.entry_no,
        entry_type=entry.entry_type,
        payload_sha256=entry.payload_sha256,
    )
    if not hmac.compare_digest(expected, entry.entry_hash):
        return Verdict(False, "entry_hash does not match its contents", entry.entry_no)

    if device_pubkey is not None:
        if not entry.signature:
            return Verdict(False, "entry is unsigned", entry.entry_no)
        try:
            Ed25519PublicKey.from_public_bytes(device_pubkey).verify(
                entry.signature, entry.entry_hash)
        except InvalidSignature:
            return Verdict(False, "device signature is invalid", entry.entry_no)
        except Exception as exc:
            return Verdict(False, f"signature check failed: {exc}", entry.entry_no)

    return Verdict(True)


def verify_chain(
    entries: Sequence[ChainEntry], *, device_pubkey: Optional[bytes] = None
) -> Verdict:
    """
    Recompute a whole session's chain, as done at /session/close.

    Detects a deleted entry (numbering gap), a reordered entry (prev_hash break),
    substituted audio (payload hash), and - with the device key - a chain rebuilt
    by anyone other than the enrolled machine.

    Entries must be supplied in order. This deliberately does not sort them: the
    agent's verify_chain does not either, and the two implementations must behave
    identically or the cross-check between them proves nothing. Callers loading
    from the database use ORDER BY entry_no.
    """
    if not entries:
        return Verdict(False, "chain is empty")

    if entries[0].entry_no != 0 or entries[0].entry_type != "open":
        return Verdict(False, "chain does not begin with an open entry", 0)

    previous: Optional[bytes] = None
    for index, entry in enumerate(entries):
        if entry.entry_no != index:
            return Verdict(
                False, f"expected entry_no {index}, found {entry.entry_no}", entry.entry_no)

        verdict = verify_entry(entry, expected_prev=previous, device_pubkey=device_pubkey)
        if not verdict.ok:
            return verdict

        previous = entry.entry_hash

    return Verdict(True)


def chain_summary(entries: Sequence[ChainEntry]) -> Dict[str, Any]:
    """Counts an operator cares about: how many segments, how many pauses."""
    counts: Dict[str, int] = {}
    for entry in entries:
        counts[entry.entry_type] = counts.get(entry.entry_type, 0) + 1
    pauses = [
        {
            "reason": e.payload.get("reason"),
            "reason_detail": e.payload.get("reason_detail"),
            "authorised_by": e.payload.get("authorised_by"),
            "at": e.payload.get("at"),
        }
        for e in entries if e.entry_type == "pause"
    ]
    return {"entry_counts": counts, "pauses": pauses, "total_entries": len(entries)}


# ============================================================
# Purge receipts
# ============================================================

class ReceiptSigner:
    """
    Signs the statement that permits an agent to delete local audio.

    The private key lives only on the backend. Without it nothing can authorise a
    deletion, which is the property that makes automatic purging safe.
    """

    def __init__(self, private_key: Ed25519PrivateKey):
        self._key = private_key

    @classmethod
    def from_env(cls, var: str = "AIMS_RECEIPT_PRIVATE_KEY") -> Optional["ReceiptSigner"]:
        """
        Load from a PEM in an environment variable.

        Returns None when unset: the backend still runs and still accepts audio, it
        simply cannot authorise deletions, and agents keep their local copies. That
        is the safe failure direction.
        """
        pem = os.getenv(var, "").strip()
        if not pem:
            logger.critical(
                "%s is not set - purge receipts cannot be issued and agents will "
                "never delete their local audio", var)
            return None
        try:
            key = serialization.load_pem_private_key(
                pem.replace("\\n", "\n").encode("utf-8"), password=None)
        except Exception as exc:
            logger.critical("Could not load the receipt signing key: %s", exc)
            return None
        if not isinstance(key, Ed25519PrivateKey):
            logger.critical("Receipt signing key is not Ed25519")
            return None
        return cls(key)

    def public_pem(self) -> str:
        return self._key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        ).decode("ascii")

    def sign_segment(
        self, *, session_id: str, seq_no: int, sha256_hex: str, archived_at: datetime
    ) -> Dict[str, Any]:
        payload = {
            "session_id": session_id,
            "scope": "segment",
            "seq_no": seq_no,
            "sha256": sha256_hex,
            "archived_at": iso_utc(archived_at),
        }
        return self._sign(payload)

    def sign_session(
        self, *, session_id: str, sha256_hex: str, archived_at: datetime
    ) -> Dict[str, Any]:
        payload = {
            "session_id": session_id,
            "scope": "session",
            "seq_no": None,
            "sha256": sha256_hex,
            "archived_at": iso_utc(archived_at),
        }
        return self._sign(payload)

    def _sign(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        signature = self._key.sign(digest(RECEIPT_DOMAIN, canonical_json(payload)))
        return {"payload": payload, "signature": signature.hex()}


# ============================================================
# Audit log
# ============================================================

def audit_entry_hash(
    *,
    prev_hash: Optional[bytes],
    occurred_at: datetime,
    event_type: str,
    actor_type: str,
    actor_id: Optional[str],
    session_id: Optional[str],
    detail: Dict[str, Any],
) -> bytes:
    """
    Hash-chain the audit log so entries cannot be removed or edited unnoticed.

    The table also has an append-only trigger; the chain is the second line of
    defence, for anyone who can bypass the trigger with enough database privilege.
    """
    return digest(
        AUDIT_DOMAIN,
        prev_hash or b"",
        iso_utc(occurred_at).encode("utf-8"),
        event_type.encode("utf-8"),
        actor_type.encode("utf-8"),
        (actor_id or "").encode("utf-8"),
        (session_id or "").encode("utf-8"),
        canonical_json(detail),
    )


# ============================================================
# Identifier safety
# ============================================================

import re

ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
ULID_PATTERN = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")


def safe_identifier(value: str, *, field: str) -> str:
    """
    Validate anything that becomes part of an object key or a file path.

    v1's AIMS LAB server joined a client-supplied patient_id straight into a
    filesystem path, which allowed writing anywhere on the volume.
    """
    if not isinstance(value, str) or not ID_PATTERN.match(value):
        raise ValueError(f"{field} must be 1-64 characters of A-Z a-z 0-9 _ -")
    return value


def safe_session_id(value: str) -> str:
    """Protocol 2 session IDs are ULIDs minted by the agent."""
    if not isinstance(value, str) or not ULID_PATTERN.match(value):
        raise ValueError("session_id must be a 26-character ULID")
    return value


__all__ = [
    "CHAIN_DOMAIN", "RECEIPT_DOMAIN", "AUDIT_DOMAIN",
    "ChainEntry", "ChainError", "ReceiptSigner", "Verdict",
    "audit_entry_hash", "canonical_json", "chain_summary", "digest", "entry_hash",
    "iso_utc", "parse_entry", "safe_identifier", "safe_session_id",
    "sha256_bytes", "verify_chain", "verify_entry",
]
