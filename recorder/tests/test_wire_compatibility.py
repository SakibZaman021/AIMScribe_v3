"""
The wire format, pinned.

`core/crypto.py` here and `src/integrity.py` in the backend are two
implementations of one specification. If they disagree by a single byte, valid
chains are rejected and the whole scheme becomes noise - and the two live in
different repositories, so nothing about a normal review makes the drift
visible.

`wire_vectors.json` is the specification made executable: fixed inputs and the
exact outputs required. An identical copy sits in the backend, whose test suite
replays the same vectors against its own code. Either side can therefore detect
its own drift alone, without the other repository present.

The vectors are treated as immutable. Regenerating them redefines the protocol
and is a two-repository change:

    python scripts/gen_wire_vectors.py tests/wire_vectors.json

then copy the file to the backend and update EXPECTED_SHA256 in both test files.
If a change here makes a test below fail, the correct response is almost always
to fix the change, not the vector.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from core import crypto

VECTORS_PATH = Path(__file__).parent / "wire_vectors.json"

# The sha256 of wire_vectors.json. The backend pins the same value. Two copies
# that disagree is exactly the failure this file exists to catch, so a mismatch
# here is never fixed by editing this constant alone.
EXPECTED_SHA256 = "9dadb81e22f7e1c002fd0a6048b840e58593153cfd1321adebf73e6468ebaeb5"

RAW = VECTORS_PATH.read_bytes()
V = json.loads(RAW.decode("utf-8"))


def _ids(cases):
    return [c["why"] for c in cases]


def test_vectors_file_is_the_agreed_one():
    """
    Both repositories must hold byte-identical vectors.

    Compared on the raw bytes, not the parsed object, because line endings and
    key order are part of what makes the two copies comparable at all.
    """
    actual = hashlib.sha256(RAW).hexdigest()
    assert actual == EXPECTED_SHA256, (
        "wire_vectors.json has changed. If that was deliberate, copy the new "
        "file to the backend repository and update EXPECTED_SHA256 in both "
        f"test_wire_compatibility.py files to {actual}."
    )


def test_domains_match_the_specification():
    assert crypto.CHAIN_DOMAIN.decode("ascii") == V["domains"]["chain"]
    assert crypto.RECEIPT_DOMAIN.decode("ascii") == V["domains"]["receipt"]


@pytest.mark.parametrize("case", V["digest"], ids=_ids(V["digest"]))
def test_digest(case):
    parts = [bytes.fromhex(p) for p in case["parts_hex"]]
    assert crypto.digest(*parts).hex() == case["expect_hex"]


def test_length_prefixing_actually_separates_fields():
    """
    The property behind the vectors above: a shifted field boundary must not
    collide. Stated directly so the reason survives even if the vectors are
    ever regenerated carelessly.
    """
    assert crypto.digest(b"ab", b"c") != crypto.digest(b"a", b"bc")


@pytest.mark.parametrize("case", V["iso_utc"], ids=_ids(V["iso_utc"]))
def test_iso_utc(case):
    assert crypto.iso_utc(datetime.fromisoformat(case["input"])) == case["expect"]


@pytest.mark.parametrize("case", V["canonical_json"], ids=_ids(V["canonical_json"]))
def test_canonical_json(case):
    encoded = crypto.canonical_json(case["payload"])
    assert encoded.hex() == case["expect_utf8_hex"]
    assert crypto.sha256_bytes(encoded).hex() == case["expect_sha256_hex"]


@pytest.mark.parametrize("case", V["entry_hash"], ids=_ids(V["entry_hash"]))
def test_entry_hash(case):
    prev = bytes.fromhex(case["prev_hash_hex"]) if case["prev_hash_hex"] else None
    assert crypto.entry_hash(
        prev_hash=prev,
        entry_no=case["entry_no"],
        entry_type=case["entry_type"],
        payload_sha256=bytes.fromhex(case["payload_sha256_hex"]),
    ).hex() == case["expect_hex"]


def test_agent_reproduces_the_reference_chain():
    """
    Rebuild the pinned session from the same key and payloads and require every
    entry to come out byte for byte identical, signatures included.

    Ed25519 is deterministic, so a signature is a fixed function of key and
    message - which makes this a complete check of hashing, ordering, chaining
    and signing in one.
    """
    chain = V["chain"]
    key = crypto.DeviceKey(
        Ed25519PrivateKey.from_private_bytes(bytes.fromhex(chain["device_seed_hex"])))

    assert key.public_bytes_raw().hex() == chain["device_pubkey_hex"]
    assert key.fingerprint() == chain["device_fingerprint"]

    prev = None
    for expected in chain["entries"]:
        rebuilt = crypto.build_entry(
            entry_no=expected["entry_no"],
            entry_type=expected["entry_type"],
            payload=expected["payload"],
            prev_hash=prev,
            signer=key,
        )
        assert rebuilt.to_wire() == expected, (
            f"entry {expected['entry_no']} ({expected['entry_type']}) diverged"
        )
        prev = rebuilt.entry_hash

    assert prev.hex() == chain["head_hex"]


def test_agent_verifies_the_reference_chain():
    entries = [
        crypto.ChainEntry(
            entry_no=e["entry_no"],
            entry_type=e["entry_type"],
            payload=e["payload"],
            payload_sha256=bytes.fromhex(e["payload_sha256"]),
            prev_hash=bytes.fromhex(e["prev_hash"]) if e["prev_hash"] else None,
            entry_hash=bytes.fromhex(e["entry_hash"]),
            signature=bytes.fromhex(e["signature"]) if e["signature"] else None,
        )
        for e in V["chain"]["entries"]
    ]
    # The agent takes a key object here where the backend takes raw bytes. That
    # is an interface difference between two codebases, not a protocol one -
    # the bytes verified are identical either way.
    device_public_key = Ed25519PublicKey.from_public_bytes(
        bytes.fromhex(V["chain"]["device_pubkey_hex"]))
    assert crypto.verify_chain(entries, device_public_key=device_public_key)


def test_receipt_signing_input_matches():
    receipt = V["receipt"]
    assert crypto.receipt_signing_input(receipt["payload"]).hex() == receipt["signing_input_hex"]


def test_agent_accepts_the_reference_receipt():
    """
    The receipt the backend would issue must verify here, or no agent ever
    deletes anything.
    """
    receipt = V["receipt"]
    public_key = Ed25519PrivateKey.from_private_bytes(
        bytes.fromhex(receipt["signer_seed_hex"])).public_key()

    crypto.verify_purge_receipt(
        receipt["payload"],
        bytes.fromhex(receipt["signature_hex"]),
        public_key,
        expect_session_id=receipt["payload"]["session_id"],
        expect_sha256=bytes.fromhex(receipt["payload"]["sha256"]),
        expect_scope=receipt["payload"]["scope"],
        expect_seq_no=receipt["payload"]["seq_no"],
    )


def test_canonical_json_rejects_types_the_backend_cannot_represent():
    """
    Both sides serialise datetime, bytes and Path, and refuse everything else.

    The refusal is the point. A type accepted here but not on the backend
    hashes on one side and raises on the other, which is the silent divergence
    the vectors above cannot catch - no unsupported type can be expressed in a
    JSON vector file in the first place.
    """
    with pytest.raises(TypeError):
        crypto.canonical_json({"bad": {1, 2, 3}})
    with pytest.raises(TypeError):
        crypto.canonical_json({"bad": object()})
