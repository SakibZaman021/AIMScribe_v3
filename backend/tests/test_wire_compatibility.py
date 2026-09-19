"""
The wire format, pinned.

`src/integrity.py` here and `core/crypto.py` in the agent repository are two
implementations of one specification. If they disagree by a single byte, valid
chains are rejected and the whole scheme becomes noise - and the two live in
different repositories, so nothing about a normal review makes the drift
visible.

`wire_vectors.json` is the specification made executable: fixed inputs and the
exact outputs required. An identical copy sits in the agent repository, whose
test suite replays the same vectors against its own code. Either side can
therefore detect its own drift alone, without the other checked out.

Where the agent *produces* these vectors, this side proves it *accepts* them:
the reference chain must verify, and the reference receipt signature must be
reproduced exactly by the signer the agents trust.

The vectors are immutable. Regenerating them redefines the protocol and is a
two-repository change - run the agent's `scripts/gen_wire_vectors.py`, copy the
result to both, update EXPECTED_SHA256 in both test files. If a change here
makes a test below fail, the correct response is almost always to fix the
change, not the vector.
"""
from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import integrity  # noqa: E402

VECTORS_PATH = Path(__file__).parent / "wire_vectors.json"

# The sha256 of wire_vectors.json. The agent repository pins the same value.
# Two copies that disagree is exactly the failure this file exists to catch, so
# a mismatch here is never fixed by editing this constant alone.
EXPECTED_SHA256 = "9dadb81e22f7e1c002fd0a6048b840e58593153cfd1321adebf73e6468ebaeb5"

RAW = VECTORS_PATH.read_bytes()
V = json.loads(RAW.decode("utf-8"))


def _ids(cases):
    return [c["why"] for c in cases]


def test_vectors_file_is_the_agreed_one():
    """
    Both repositories must hold byte-identical vectors.

    Compared on raw bytes, not the parsed object, because line endings and key
    order are part of what makes the two copies comparable at all.
    """
    actual = hashlib.sha256(RAW).hexdigest()
    assert actual == EXPECTED_SHA256, (
        "wire_vectors.json has changed. If that was deliberate, copy the new "
        "file to the agent repository and update EXPECTED_SHA256 in both "
        f"test_wire_compatibility.py files to {actual}."
    )


def test_domains_match_the_specification():
    assert integrity.CHAIN_DOMAIN.decode("ascii") == V["domains"]["chain"]
    assert integrity.RECEIPT_DOMAIN.decode("ascii") == V["domains"]["receipt"]


@pytest.mark.parametrize("case", V["digest"], ids=_ids(V["digest"]))
def test_digest(case):
    parts = [bytes.fromhex(p) for p in case["parts_hex"]]
    assert integrity.digest(*parts).hex() == case["expect_hex"]


def test_length_prefixing_actually_separates_fields():
    """
    The property behind the vectors above: a shifted field boundary must not
    collide. Stated directly so the reason survives even if the vectors are
    ever regenerated carelessly.
    """
    assert integrity.digest(b"ab", b"c") != integrity.digest(b"a", b"bc")


@pytest.mark.parametrize("case", V["iso_utc"], ids=_ids(V["iso_utc"]))
def test_iso_utc(case):
    assert integrity.iso_utc(datetime.fromisoformat(case["input"])) == case["expect"]


@pytest.mark.parametrize("case", V["canonical_json"], ids=_ids(V["canonical_json"]))
def test_canonical_json(case):
    encoded = integrity.canonical_json(case["payload"])
    assert encoded.hex() == case["expect_utf8_hex"]
    assert integrity.sha256_bytes(encoded).hex() == case["expect_sha256_hex"]


@pytest.mark.parametrize("case", V["entry_hash"], ids=_ids(V["entry_hash"]))
def test_entry_hash(case):
    prev = bytes.fromhex(case["prev_hash_hex"]) if case["prev_hash_hex"] else None
    assert integrity.entry_hash(
        prev_hash=prev,
        entry_no=case["entry_no"],
        entry_type=case["entry_type"],
        payload_sha256=bytes.fromhex(case["payload_sha256_hex"]),
    ).hex() == case["expect_hex"]


def _reference_entries():
    return [integrity.parse_entry(e) for e in V["chain"]["entries"]]


def test_reference_chain_parses():
    """Every entry the agent emits must survive this side's wire validation."""
    entries = _reference_entries()
    assert [e.entry_no for e in entries] == list(range(len(V["chain"]["entries"])))
    assert [e.entry_type for e in entries] == \
        [e["entry_type"] for e in V["chain"]["entries"]]


def test_backend_verifies_the_reference_chain():
    """
    The chain a real agent produces must verify here, signatures included.

    This is the single most important assertion in the file: it is the exact
    operation performed at /session/close, run against a chain built by the
    other implementation.
    """
    entries = _reference_entries()
    device_pubkey = bytes.fromhex(V["chain"]["device_pubkey_hex"])
    verdict = integrity.verify_chain(entries, device_pubkey=device_pubkey)
    assert verdict.ok, verdict.reason
    assert entries[-1].entry_hash.hex() == V["chain"]["head_hex"]


def test_backend_verifies_each_entry_in_isolation():
    """The per-entry path used at /segment/commit, walked one entry at a time."""
    prev = None
    for entry in _reference_entries():
        verdict = integrity.verify_entry(
            entry, expected_prev=prev,
            device_pubkey=bytes.fromhex(V["chain"]["device_pubkey_hex"]))
        assert verdict.ok, f"entry {entry.entry_no}: {verdict.reason}"
        prev = entry.entry_hash


def test_tampering_with_the_reference_chain_is_caught():
    """
    A negative control. Without this, every assertion above would still pass if
    verification silently accepted everything.
    """
    entries = _reference_entries()
    device_pubkey = bytes.fromhex(V["chain"]["device_pubkey_hex"])

    edited = list(entries)
    victim = edited[1]
    edited[1] = integrity.ChainEntry(
        entry_no=victim.entry_no,
        entry_type=victim.entry_type,
        payload={**victim.payload, "audio_sha256": "ff" * 32},
        payload_sha256=victim.payload_sha256,
        prev_hash=victim.prev_hash,
        entry_hash=victim.entry_hash,
        signature=victim.signature,
    )
    assert not integrity.verify_chain(edited, device_pubkey=device_pubkey).ok

    dropped = entries[:2] + entries[3:]
    assert not integrity.verify_chain(dropped, device_pubkey=device_pubkey).ok


def test_receipt_signer_reproduces_the_reference_signature():
    """
    The receipt this backend issues must be byte-identical to the one agents
    are pinned against. If it is not, every agent rejects every receipt and
    silently never deletes its local audio.
    """
    receipt = V["receipt"]
    signer = integrity.ReceiptSigner(
        Ed25519PrivateKey.from_private_bytes(bytes.fromhex(receipt["signer_seed_hex"])))

    signed = signer.sign_segment(
        session_id=receipt["payload"]["session_id"],
        seq_no=receipt["payload"]["seq_no"],
        sha256_hex=receipt["payload"]["sha256"],
        archived_at=datetime.fromisoformat(
            receipt["payload"]["archived_at"].replace("Z", "+00:00")),
    )

    assert signed["payload"] == receipt["payload"]
    assert signed["signature"] == receipt["signature_hex"]


def test_receipt_signing_input_matches():
    receipt = V["receipt"]
    computed = integrity.digest(
        integrity.RECEIPT_DOMAIN, integrity.canonical_json(receipt["payload"]))
    assert computed.hex() == receipt["signing_input_hex"]


def test_canonical_json_rejects_types_the_agent_cannot_represent():
    """
    Both sides serialise datetime, bytes and Path, and refuse everything else.

    The refusal is the point. A type accepted here but not on the agent hashes
    on one side and raises on the other - the silent divergence the vectors
    cannot catch, since no unsupported type can appear in a JSON vector file.
    """
    with pytest.raises(TypeError):
        integrity.canonical_json({"bad": {1, 2, 3}})
    with pytest.raises(TypeError):
        integrity.canonical_json({"bad": object()})


def test_canonical_json_accepts_the_same_types_as_the_agent():
    assert integrity.canonical_json({"p": Path("C:/a/b")}) == \
        integrity.canonical_json({"p": str(Path("C:/a/b"))})
    assert integrity.canonical_json({"b": b"\x01\x02"}) == b'{"b":"0102"}'
