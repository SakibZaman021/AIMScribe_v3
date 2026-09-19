"""
Integrity and key handling for the AIMScribe agent.

Three jobs:

1. **Hash chain.** Every session event - open, each segment, each pause and
   resume, close - becomes a chain entry signed by this machine's Ed25519 key.
   Deleting, reordering or substituting audio breaks the chain, and because the
   private key never leaves the machine, a copy of the disk cannot forge a
   replacement chain.

2. **Grant verification.** Recording only starts when CMED's server has signed a
   short-lived grant naming the doctor, hospital and patient. The browser is
   never trusted for identity.

3. **Purge receipts.** A local audio file is deleted only after the AIMS LAB
   server signs a statement that the archive copy exists and hashes correctly.

Every hash is length-prefixed and domain-separated, so no field can be shifted
into another to produce a colliding pre-image.

The chain and receipt rules below are one half of a specification whose other
half is the backend's `src/integrity.py`, in a different repository. They must
agree byte for byte: if they do not, valid chains are rejected and the scheme
becomes noise.

That agreement is pinned rather than remembered. `tests/wire_vectors.json` holds
fixed inputs and the exact outputs required, `tests/test_wire_compatibility.py`
replays them against this module, and an identical copy of the vectors lives in
the backend repository doing the same there. Change a rule here and this
repository's own tests fail immediately.

Regenerating the vectors redefines the protocol and is a two-repository change:

    python scripts/gen_wire_vectors.py tests/wire_vectors.json

then copy the file to the backend and update `EXPECTED_SHA256` in both test
files.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import secrets
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from cryptography.exceptions import InvalidSignature, InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives import serialization

logger = logging.getLogger(__name__)

CHAIN_DOMAIN = b"aimscribe.chain.v2"
RECEIPT_DOMAIN = b"aimscribe.receipt.v2"
SPOOL_DOMAIN = b"aimscribe.spool.v2"

_DEVICE_KEY_ENTROPY = b"aimscribe.device.key.v2"
_MAGIC_DPAPI = b"AIMSDK01"
_MAGIC_PLAIN = b"AIMSDK00"


# ============================================================
# Primitives
# ============================================================

def digest(*parts: bytes) -> bytes:
    """
    SHA-256 over length-prefixed parts.

    Prefixing each part with its length means ("ab", "c") and ("a", "bc") produce
    different digests, which a plain concatenation would not.
    """
    h = hashlib.sha256()
    for part in parts:
        h.update(len(part).to_bytes(4, "big"))
        h.update(part)
    return h.digest()


def sha256_bytes(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()


def sha256_file(path: Path, chunk_size: int = 1 << 20) -> bytes:
    h = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(chunk_size), b""):
            h.update(block)
    return h.digest()


def canonical_json(payload: Any) -> bytes:
    """
    Deterministic JSON so both sides compute identical hashes.

    Sorted keys, no insignificant whitespace, UTF-8. `default=_json_default`
    renders datetimes as UTC ISO-8601 with a Z suffix.
    """
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
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"cannot serialise {type(value).__name__} for hashing")


def iso_utc(moment: datetime) -> str:
    """UTC ISO-8601 with millisecond precision and a Z suffix."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def constant_time_equal(left: str, right: str) -> bool:
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def pseudonymise(value: str, salt: bytes, length: int = 10) -> str:
    """
    Stable pseudonym for log output.

    Log files must not contain patient or doctor identifiers, but support still
    needs to correlate lines about the same session. An HMAC with a machine-local
    salt gives a stable token that cannot be reversed off the machine.
    """
    if not value:
        return "-"
    return hmac.new(salt, value.encode("utf-8"), hashlib.sha256).hexdigest()[:length]


# ============================================================
# Chain payloads
# ============================================================

def open_payload(
    *,
    device_id: str,
    doctor_id: str,
    hospital_id: str,
    patient_ref: str,
    opened_at: datetime,
    sample_rate: int,
    channels: int,
    sample_width: int,
) -> Dict[str, Any]:
    return {
        "type": "open",
        "device_id": device_id,
        "doctor_id": doctor_id,
        "hospital_id": hospital_id,
        "patient_ref": patient_ref,
        "opened_at": iso_utc(opened_at),
        "audio": {
            "codec": "pcm_s16le",
            "container": "wav",
            "sample_rate": sample_rate,
            "channels": channels,
            "sample_width": sample_width,
        },
    }


def segment_payload(
    *,
    seq_no: int,
    audio_sha256: bytes,
    byte_length: int,
    duration_seconds: float,
    captured_start_at: datetime,
    captured_end_at: datetime,
    rms_mean: float,
    is_final: bool,
) -> Dict[str, Any]:
    return {
        "type": "segment",
        "seq_no": seq_no,
        "audio_sha256": audio_sha256.hex(),
        "bytes": byte_length,
        "duration_seconds": round(float(duration_seconds), 3),
        "captured_start_at": iso_utc(captured_start_at),
        "captured_end_at": iso_utc(captured_end_at),
        "rms_mean": round(float(rms_mean), 2),
        "is_final": bool(is_final),
    }


def pause_payload(
    *,
    reason: str,
    reason_detail: str,
    authorised_by: str,
    supervisor_required: bool,
    at: datetime,
) -> Dict[str, Any]:
    """A pause is a first-class chain entry, which is what makes the gap provable."""
    return {
        "type": "pause",
        "reason": reason,
        "reason_detail": reason_detail,
        "authorised_by": authorised_by,
        "supervisor_required": bool(supervisor_required),
        "at": iso_utc(at),
    }


def resume_payload(*, at: datetime, paused_seconds: float) -> Dict[str, Any]:
    return {
        "type": "resume",
        "at": iso_utc(at),
        "paused_seconds": round(float(paused_seconds), 3),
    }


def close_payload(
    *, closed_at: datetime, segment_count: int, duration_seconds: float,
    paused_seconds: float, reason: str = "",
) -> Dict[str, Any]:
    return {
        "type": "close",
        # Why the recording ended, signed with the rest. A consultation stopped
        # from the tray icon rather than from CMED is worth knowing about, and a
        # reason that lives only in a local log is not evidence of anything.
        "reason": reason,
        "closed_at": iso_utc(closed_at),
        "segment_count": segment_count,
        "duration_seconds": round(float(duration_seconds), 3),
        "paused_seconds": round(float(paused_seconds), 3),
    }


# ============================================================
# Chain construction and verification
# ============================================================

@dataclass(frozen=True)
class ChainEntry:
    entry_no: int
    entry_type: str
    payload: Dict[str, Any]
    payload_sha256: bytes
    prev_hash: Optional[bytes]
    entry_hash: bytes
    signature: Optional[bytes] = None

    def to_wire(self) -> Dict[str, Any]:
        return {
            "entry_no": self.entry_no,
            "entry_type": self.entry_type,
            "payload": self.payload,
            "payload_sha256": self.payload_sha256.hex(),
            "prev_hash": self.prev_hash.hex() if self.prev_hash else None,
            "entry_hash": self.entry_hash.hex(),
            "signature": self.signature.hex() if self.signature else None,
        }


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


def build_entry(
    *,
    entry_no: int,
    entry_type: str,
    payload: Dict[str, Any],
    prev_hash: Optional[bytes],
    signer: Optional["DeviceKey"] = None,
) -> ChainEntry:
    payload_hash = sha256_bytes(canonical_json(payload))
    computed = entry_hash(
        prev_hash=prev_hash,
        entry_no=entry_no,
        entry_type=entry_type,
        payload_sha256=payload_hash,
    )
    return ChainEntry(
        entry_no=entry_no,
        entry_type=entry_type,
        payload=payload,
        payload_sha256=payload_hash,
        prev_hash=prev_hash,
        entry_hash=computed,
        signature=signer.sign(computed) if signer else None,
    )


@dataclass(frozen=True)
class ChainVerdict:
    ok: bool
    reason: str = ""
    failed_entry_no: Optional[int] = None

    def __bool__(self) -> bool:
        return self.ok


def verify_chain(
    entries: Sequence[ChainEntry], *, device_public_key: Optional[Ed25519PublicKey] = None
) -> ChainVerdict:
    """
    Recompute the whole chain.

    Detects a deleted entry (a numbering gap), a reordered entry (prev_hash
    mismatch), and substituted audio (payload hash mismatch). With the device
    public key supplied it also proves each entry was signed by that machine.
    """
    if not entries:
        return ChainVerdict(False, "chain is empty")

    previous: Optional[bytes] = None
    for index, item in enumerate(entries):
        if item.entry_no != index:
            return ChainVerdict(
                False, f"expected entry_no {index}, found {item.entry_no}", item.entry_no
            )

        if item.payload_sha256 != sha256_bytes(canonical_json(item.payload)):
            return ChainVerdict(False, "payload does not match its hash", item.entry_no)

        if item.prev_hash != previous:
            return ChainVerdict(False, "prev_hash does not match the previous entry", item.entry_no)

        expected = entry_hash(
            prev_hash=item.prev_hash,
            entry_no=item.entry_no,
            entry_type=item.entry_type,
            payload_sha256=item.payload_sha256,
        )
        if not hmac.compare_digest(expected, item.entry_hash):
            return ChainVerdict(False, "entry_hash does not match its contents", item.entry_no)

        if device_public_key is not None:
            if not item.signature:
                return ChainVerdict(False, "entry is unsigned", item.entry_no)
            try:
                device_public_key.verify(item.signature, item.entry_hash)
            except InvalidSignature:
                return ChainVerdict(False, "device signature is invalid", item.entry_no)

        previous = item.entry_hash

    return ChainVerdict(True)


# ============================================================
# Windows DPAPI key wrapping
# ============================================================

def _dpapi_available() -> bool:
    return sys.platform == "win32"


def dpapi_protect(plaintext: bytes, entropy: bytes = _DEVICE_KEY_ENTROPY) -> bytes:
    """Encrypt to the local machine using CryptProtectData. Windows only."""
    import ctypes
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    def blob(data: bytes) -> DATA_BLOB:
        buffer = ctypes.create_string_buffer(data, len(data))
        return DATA_BLOB(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))

    in_blob, entropy_blob, out_blob = blob(plaintext), blob(entropy), DATA_BLOB()
    # CRYPTPROTECT_LOCAL_MACHINE | CRYPTPROTECT_UI_FORBIDDEN
    if not crypt32.CryptProtectData(
        ctypes.byref(in_blob), None, ctypes.byref(entropy_blob), None, None, 0x4 | 0x1,
        ctypes.byref(out_blob),
    ):
        raise OSError(ctypes.get_last_error(), "CryptProtectData failed")
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        kernel32.LocalFree(out_blob.pbData)


def dpapi_unprotect(ciphertext: bytes, entropy: bytes = _DEVICE_KEY_ENTROPY) -> bytes:
    import ctypes
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    def blob(data: bytes) -> DATA_BLOB:
        buffer = ctypes.create_string_buffer(data, len(data))
        return DATA_BLOB(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))

    in_blob, entropy_blob, out_blob = blob(ciphertext), blob(entropy), DATA_BLOB()
    if not crypt32.CryptUnprotectData(
        ctypes.byref(in_blob), None, ctypes.byref(entropy_blob), None, None, 0x1,
        ctypes.byref(out_blob),
    ):
        raise OSError(ctypes.get_last_error(), "CryptUnprotectData failed")
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        kernel32.LocalFree(out_blob.pbData)


def wrap_secret(secret: bytes, *, allow_plaintext: bool) -> bytes:
    """Wrap a secret for on-disk storage, tagged so unwrap knows which form it is."""
    if _dpapi_available():
        return _MAGIC_DPAPI + dpapi_protect(secret)
    if not allow_plaintext:
        raise RuntimeError(
            "DPAPI is unavailable and AIMS_ALLOW_PLAINTEXT_KEYSTORE is false; "
            "refusing to store an unprotected secret"
        )
    logger.warning("Storing secret WITHOUT DPAPI protection - development only")
    return _MAGIC_PLAIN + secret


def unwrap_secret(blob: bytes, *, allow_plaintext: bool) -> bytes:
    if blob.startswith(_MAGIC_DPAPI):
        return dpapi_unprotect(blob[len(_MAGIC_DPAPI):])
    if blob.startswith(_MAGIC_PLAIN):
        if not allow_plaintext:
            raise RuntimeError(
                "Secret on disk is unprotected but plaintext keystore is disabled")
        return blob[len(_MAGIC_PLAIN):]
    raise ValueError("unrecognised key file format")


# ============================================================
# Device signing key
# ============================================================

class DeviceKey:
    """
    This machine's Ed25519 identity, used to sign every chain entry.

    On a clinical PC the wrapped key is bound to the machine by DPAPI, so lifting
    the file to another computer yields nothing. A TPM-backed CNG key is the
    stronger option and is a drop-in replacement for this class once the
    installer provisions one; the on-disk form is versioned for that migration.
    """

    def __init__(self, private_key: Ed25519PrivateKey):
        self._private = private_key

    # ---- lifecycle ----

    @classmethod
    def load_or_create(cls, path: Path, *, allow_plaintext: bool = False) -> "DeviceKey":
        if path.is_file():
            try:
                raw = unwrap_secret(path.read_bytes(), allow_plaintext=allow_plaintext)
                return cls(Ed25519PrivateKey.from_private_bytes(raw))
            except Exception:
                logger.critical("Device key at %s is unreadable; refusing to overwrite it", path)
                raise

        logger.info("Generating a new device key at %s", path)
        private = Ed25519PrivateKey.generate()
        raw = private.private_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PrivateFormat.Raw,
            encryption_algorithm=serialization.NoEncryption(),
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_bytes(wrap_secret(raw, allow_plaintext=allow_plaintext))
        os.replace(tmp, path)
        return cls(private)

    # ---- use ----

    def sign(self, message: bytes) -> bytes:
        return self._private.sign(message)

    def public_key(self) -> Ed25519PublicKey:
        return self._private.public_key()

    def public_bytes_raw(self) -> bytes:
        return self.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )

    def public_pem(self) -> bytes:
        return self.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )

    def fingerprint(self) -> str:
        return sha256_bytes(self.public_bytes_raw()).hex()[:16]


# ============================================================
# Public key loading
# ============================================================

def load_public_key(path: Path) -> Ed25519PublicKey:
    """Load a pinned Ed25519 public key from PEM, or raw 32 bytes."""
    data = path.read_bytes()
    if data.lstrip().startswith(b"-----BEGIN"):
        key = serialization.load_pem_public_key(data)
    elif len(data.strip()) == 32:
        key = Ed25519PublicKey.from_public_bytes(data.strip())
    else:
        raise ValueError(f"{path} is not a PEM or raw Ed25519 public key")

    if not isinstance(key, Ed25519PublicKey):
        raise ValueError(f"{path} is not an Ed25519 key")
    return key


# ============================================================
# CMED recording grants
# ============================================================

@dataclass(frozen=True)
class Grant:
    """A verified, single-use authorisation to record one consultation."""
    jti: str
    doctor_id: str
    doctor_name: str
    hospital_id: str
    patient_ref: str
    consent_obtained: bool
    consent_method: str
    expires_at: int
    raw: str


class GrantError(Exception):
    """Raised when a grant is missing, malformed, expired, or not properly signed."""


def verify_grant(
    token: str,
    public_key: Ed25519PublicKey,
    *,
    issuer: str,
    audience: str,
    leeway_seconds: int = 5,
) -> Grant:
    """
    Verify a CMED grant and return its claims.

    Only EdDSA is accepted. Passing an explicit algorithm list is what prevents
    the classic "alg: none" and HMAC-confusion attacks against JWT verifiers.
    """
    import jwt  # imported lazily to keep tray startup fast

    if not token or not isinstance(token, str):
        raise GrantError("no grant supplied")

    try:
        claims = jwt.decode(
            token,
            key=public_key,
            algorithms=["EdDSA"],
            audience=audience,
            issuer=issuer,
            leeway=leeway_seconds,
            options={
                "require": ["exp", "iat", "jti", "aud", "iss", "sub"],
                "verify_exp": True,
                "verify_aud": True,
                "verify_iss": True,
                "verify_signature": True,
            },
        )
    except Exception as exc:  # PyJWT raises a family of subclasses
        raise GrantError(f"grant rejected: {type(exc).__name__}") from exc

    if not claims.get("consent_obtained"):
        raise GrantError("grant does not record patient consent")

    # The patient is the one thing only CMED knows, so it is the one thing the
    # grant must carry. Doctor and hospital are deliberately allowed to be empty:
    # they belong to the machine's enrolment, and CMED no longer knows them. A
    # grant that does name them is not trusted either - the controller compares
    # them against the enrolment and raises an integrity alert on a mismatch.
    if not claims.get("patient_ref"):
        raise GrantError("grant is missing patient_ref")

    return Grant(
        jti=str(claims["jti"]),
        doctor_id=str(claims.get("sub") or ""),
        doctor_name=str(claims.get("doctor_name", "")),
        hospital_id=str(claims.get("hospital_id") or ""),
        patient_ref=str(claims["patient_ref"]),
        consent_obtained=True,
        consent_method=str(claims.get("consent_method", "")),
        expires_at=int(claims["exp"]),
        raw=token,
    )


# ============================================================
# Purge receipts
# ============================================================

class ReceiptError(Exception):
    """Raised when a purge receipt cannot be trusted. Never delete audio in this case."""


def receipt_signing_input(payload: Dict[str, Any]) -> bytes:
    return digest(RECEIPT_DOMAIN, canonical_json(payload))


def verify_purge_receipt(
    payload: Dict[str, Any],
    signature: bytes,
    public_key: Ed25519PublicKey,
    *,
    expect_session_id: str,
    expect_sha256: bytes,
    expect_scope: str,
    expect_seq_no: Optional[int] = None,
) -> None:
    """
    Verify that the server really did archive this exact content.

    The signature alone is not enough: a valid receipt for a *different* segment
    would otherwise authorise deleting this one, so every field is checked
    against what we are about to delete. Raises ReceiptError on any mismatch.
    """
    try:
        public_key.verify(signature, receipt_signing_input(payload))
    except InvalidSignature as exc:
        raise ReceiptError("receipt signature is invalid") from exc

    if payload.get("session_id") != expect_session_id:
        raise ReceiptError("receipt is for a different session")
    if payload.get("scope") != expect_scope:
        raise ReceiptError("receipt scope does not match")
    if expect_scope == "segment" and payload.get("seq_no") != expect_seq_no:
        raise ReceiptError("receipt is for a different segment")

    claimed = payload.get("sha256")
    if not isinstance(claimed, str):
        raise ReceiptError("receipt carries no sha256")
    try:
        claimed_bytes = bytes.fromhex(claimed)
    except ValueError as exc:
        raise ReceiptError("receipt sha256 is malformed") from exc
    if not hmac.compare_digest(claimed_bytes, expect_sha256):
        raise ReceiptError("receipt sha256 does not match the local file")

    if not payload.get("archived_at"):
        raise ReceiptError("receipt does not state when the archive copy was written")


# ============================================================
# Spool encryption
# ============================================================

SPOOL_MAGIC = b"AIMSPL02"
_NONCE_BYTES = 12


def new_spool_key() -> bytes:
    return AESGCM.generate_key(bit_length=256)


def encrypt_spool_blob(key: bytes, plaintext: bytes, associated: bytes = b"") -> bytes:
    """
    AES-256-GCM with a random nonce, prefixed by a format magic.

    Associated data binds the ciphertext to its session and sequence number, so a
    segment file cannot be swapped for another session's segment.
    """
    nonce = secrets.token_bytes(_NONCE_BYTES)
    sealed = AESGCM(key).encrypt(nonce, plaintext, digest(SPOOL_DOMAIN, associated))
    return SPOOL_MAGIC + nonce + sealed


def decrypt_spool_blob(key: bytes, blob: bytes, associated: bytes = b"") -> bytes:
    if not blob.startswith(SPOOL_MAGIC):
        raise ValueError("not an AIMScribe spool blob")
    body = blob[len(SPOOL_MAGIC):]
    nonce, sealed = body[:_NONCE_BYTES], body[_NONCE_BYTES:]
    try:
        return AESGCM(key).decrypt(nonce, sealed, digest(SPOOL_DOMAIN, associated))
    except InvalidTag as exc:
        raise ValueError("spool blob failed authentication - it has been altered") from exc


__all__ = [
    "ChainEntry", "ChainVerdict", "DeviceKey", "Grant", "GrantError", "ReceiptError",
    "build_entry", "canonical_json", "close_payload", "constant_time_equal",
    "decrypt_spool_blob", "digest", "encrypt_spool_blob", "entry_hash", "iso_utc",
    "load_public_key", "new_spool_key", "open_payload", "pause_payload", "pseudonymise",
    "resume_payload", "segment_payload", "sha256_bytes", "sha256_file", "unwrap_secret",
    "verify_chain", "verify_grant", "verify_purge_receipt", "wrap_secret",
]
