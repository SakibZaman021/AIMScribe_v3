"""
Freeze the agent/backend wire format into golden test vectors.

`recorder/core/crypto.py` and the backend's `src/integrity.py` are two
implementations of one specification. Every constant and hashing rule has to
match byte for byte, or valid chains are rejected and the whole scheme becomes
noise. Until now that was enforced by a comment in each file asking the reader
to remember - across two repositories that are never checked out together.

This script writes `wire_vectors.json`: fixed inputs and the exact outputs the
specification requires. A copy lives in each repository, and each side has a
test that replays every vector against its own implementation. Change a rule on
one side and that side's own test suite fails immediately, without needing the
other repository present.

    python scripts/gen_wire_vectors.py path/to/wire_vectors.json

Regenerating is a deliberate act: it redefines the protocol. The new file must
be copied to **both** repositories and `EXPECTED_SHA256` updated in both
`test_wire_compatibility.py` files to the value printed at the end. A mismatch
there means the two copies have drifted, which is precisely what this catches.

Signatures are reproducible because Ed25519 is deterministic: the same key and
message always yield the same signature, so a fixed seed pins them exactly.
"""
from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from core import crypto

# Fixed seeds. These are test keys, meant to be public: they exist so a
# signature can be reproduced, and they authorise nothing anywhere.
DEVICE_SEED = bytes(range(32))
RECEIPT_SEED = bytes(range(32, 64))

AT = datetime(2026, 8, 4, 9, 30, 15, 123456, tzinfo=timezone.utc)
SESSION = "01KZ39EJN01QJPT3Z9ZFDRKCTY"


def canonical_cases():
    """
    Payloads chosen to pin one serialisation decision each.

    If any of these regress, hashes diverge silently: every chain from an
    updated agent is rejected by the backend, or - worse - accepted while
    meaning something different.
    """
    return [
        {"why": "key order must not matter", "payload": {"b": 1, "a": 2, "c": 3}},
        {"why": "no insignificant whitespace", "payload": {"x": [1, 2, {"y": "z"}]}},
        {"why": r"ensure_ascii=False: Bengali stays UTF-8, not \uXXXX escapes",
         "payload": {"note": "রোগীর জ্বর", "doctor": "ডাক্তার"}},
        {"why": "floats keep their repr, ints stay ints",
         "payload": {"d": 12.5, "n": 3, "z": 0.0}},
        {"why": "null and bool survive", "payload": {"a": None, "b": True, "c": False}},
        {"why": "empty containers", "payload": {"a": {}, "b": [], "c": ""}},
        {"why": "sorting is recursive", "payload": {"o": {"z": 1, "a": {"q": 2, "b": 3}}}},
        {"why": "a real open payload, as the agent builds it",
         "payload": crypto.open_payload(
             device_id="9fcfc5b0-c7f7-4a34-9e05-2d82576677af",
             doctor_id="DR001", hospital_id="HOSP001", patient_ref="1034GS6",
             opened_at=AT, sample_rate=44100, channels=1, sample_width=2)},
        {"why": "a real segment payload",
         "payload": crypto.segment_payload(
             seq_no=1, audio_sha256=bytes.fromhex("11" * 32), byte_length=2646044,
             duration_seconds=30.0, captured_start_at=AT, captured_end_at=AT,
             rms_mean=0.0213, is_final=False)},
        {"why": "a real pause payload",
         "payload": crypto.pause_payload(
             reason="patient_declined", reason_detail="", authorised_by="",
             supervisor_required=False, at=AT)},
        {"why": "a real close payload",
         "payload": crypto.close_payload(
             closed_at=AT, segment_count=2, duration_seconds=112.5,
             paused_seconds=42.5, reason="doctor_stopped")},
    ]


def build_chain():
    """A full session: open, segment, pause, resume, segment, close."""
    key = crypto.DeviceKey(Ed25519PrivateKey.from_private_bytes(DEVICE_SEED))

    payloads = [
        ("open", crypto.open_payload(
            device_id="9fcfc5b0-c7f7-4a34-9e05-2d82576677af",
            doctor_id="DR001", hospital_id="HOSP001", patient_ref="1034GS6",
            opened_at=AT, sample_rate=44100, channels=1, sample_width=2)),
        ("segment", crypto.segment_payload(
            seq_no=1, audio_sha256=bytes.fromhex("11" * 32), byte_length=2646044,
            duration_seconds=30.0, captured_start_at=AT, captured_end_at=AT,
            rms_mean=0.0213, is_final=False)),
        ("pause", crypto.pause_payload(
            reason="patient_declined", reason_detail="", authorised_by="",
            supervisor_required=False, at=AT)),
        ("resume", crypto.resume_payload(at=AT, paused_seconds=42.5)),
        ("segment", crypto.segment_payload(
            seq_no=2, audio_sha256=bytes.fromhex("22" * 32), byte_length=3527392,
            duration_seconds=40.0, captured_start_at=AT, captured_end_at=AT,
            rms_mean=0.0198, is_final=True)),
        ("close", crypto.close_payload(
            closed_at=AT, segment_count=2, duration_seconds=112.5,
            paused_seconds=42.5, reason="doctor_stopped")),
    ]

    entries, prev = [], None
    for entry_no, (entry_type, payload) in enumerate(payloads):
        entry = crypto.build_entry(entry_no=entry_no, entry_type=entry_type,
                                   payload=payload, prev_hash=prev, signer=key)
        entries.append(entry.to_wire())
        prev = entry.entry_hash

    return {
        "device_seed_hex": DEVICE_SEED.hex(),
        "device_pubkey_hex": key.public_bytes_raw().hex(),
        "device_fingerprint": key.fingerprint(),
        "entries": entries,
        "head_hex": prev.hex(),
    }


def build_receipt():
    key = Ed25519PrivateKey.from_private_bytes(RECEIPT_SEED)
    payload = {
        "session_id": SESSION,
        "scope": "segment",
        "seq_no": 1,
        "sha256": "11" * 32,
        "archived_at": crypto.iso_utc(AT),
    }
    signing_input = crypto.receipt_signing_input(payload)
    return {
        "signer_seed_hex": RECEIPT_SEED.hex(),
        "public_pem": key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        ).decode("ascii"),
        "payload": payload,
        "signing_input_hex": signing_input.hex(),
        "signature_hex": key.sign(signing_input).hex(),
    }


def main() -> int:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("wire_vectors.json")

    vectors = {
        "_comment": "Golden vectors for the AIMScribe chain and receipt wire "
                    "format. Generated by recorder/scripts/gen_wire_vectors.py. "
                    "An identical copy lives in both repositories and both test "
                    "suites pin its sha256.",
        "format_version": 2,
        "domains": {
            "chain": crypto.CHAIN_DOMAIN.decode("ascii"),
            "receipt": crypto.RECEIPT_DOMAIN.decode("ascii"),
        },
        "digest": [
            {"why": "length prefixing: ('ab','c') must differ from ('a','bc')",
             "parts_hex": ["6162", "63"]},
            {"why": "the other half of that pair", "parts_hex": ["61", "6263"]},
            {"why": "empty parts are still length-prefixed", "parts_hex": ["", ""]},
            {"why": "no parts at all", "parts_hex": []},
            {"why": "a domain plus a body",
             "parts_hex": [crypto.CHAIN_DOMAIN.hex(), "deadbeef"]},
        ],
        "iso_utc": [
            {"input": "2026-08-04T09:30:15.123456+00:00",
             "why": "microseconds truncate to milliseconds, Z suffix"},
            {"input": "2026-08-04T15:30:15.123456+06:00",
             "why": "a non-UTC offset converts to UTC"},
            {"input": "2026-08-04T09:30:15",
             "why": "a naive datetime is assumed to be UTC"},
            {"input": "2026-01-01T00:00:00+00:00",
             "why": "zero milliseconds are still printed"},
        ],
        "canonical_json": canonical_cases(),
        "entry_hash": [
            {"why": "genesis: prev_hash is absent", "prev_hash_hex": None,
             "entry_no": 0, "entry_type": "open", "payload_sha256_hex": "aa" * 32},
            {"why": "a later entry", "prev_hash_hex": "bb" * 32,
             "entry_no": 7, "entry_type": "segment", "payload_sha256_hex": "cc" * 32},
            {"why": "entry_no is hashed as ASCII decimal, so 10 is not 1 then 0",
             "prev_hash_hex": "bb" * 32, "entry_no": 10, "entry_type": "segment",
             "payload_sha256_hex": "cc" * 32},
        ],
        "chain": build_chain(),
        "receipt": build_receipt(),
    }

    for case in vectors["digest"]:
        case["expect_hex"] = crypto.digest(
            *[bytes.fromhex(p) for p in case["parts_hex"]]).hex()

    for case in vectors["iso_utc"]:
        case["expect"] = crypto.iso_utc(datetime.fromisoformat(case["input"]))

    for case in vectors["canonical_json"]:
        encoded = crypto.canonical_json(case["payload"])
        case["expect_utf8_hex"] = encoded.hex()
        case["expect_sha256_hex"] = crypto.sha256_bytes(encoded).hex()

    for case in vectors["entry_hash"]:
        case["expect_hex"] = crypto.entry_hash(
            prev_hash=bytes.fromhex(case["prev_hash_hex"]) if case["prev_hash_hex"] else None,
            entry_no=case["entry_no"],
            entry_type=case["entry_type"],
            payload_sha256=bytes.fromhex(case["payload_sha256_hex"]),
        ).hex()

    # Deterministic on disk, so the two copies are byte-identical and the
    # sha256 is stable across machines and platforms.
    text = json.dumps(vectors, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    out.write_text(text, encoding="utf-8", newline="\n")

    fingerprint = hashlib.sha256(text.encode("utf-8")).hexdigest()
    print(f"wrote  {out}")
    print(f"sha256 {fingerprint}")
    print("\nCopy this file to both repositories and set EXPECTED_SHA256 to the "
          "value above\nin both test_wire_compatibility.py files.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
