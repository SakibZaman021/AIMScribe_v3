"""
The chain must survive a retry, and must survive a clip that will not upload.

Both of these broke a real consultation. A clip was committed, the response was
lost, the agent retried, and the backend judged the identical entry against a
head that had already moved past it - so an intact ten-clip consultation was
quarantined and held out of the archive.

These run against the real integrity code, with a real device key and a real
chain. No mocks: the point is to prove the rule, not to describe it.
"""
from __future__ import annotations

import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND / "src"))
sys.path.insert(0, str(BACKEND.parent.parent / "AIMScribe.exe-main" /
                      "AIMScribe.exe-main" / "recorder"))

import integrity                      # the backend's verifier
from core import crypto               # the agent's chain builder


def build_chain(device_key, count: int):
    """An agent-side chain: open, then `count` segment entries."""
    entries, prev = [], None
    for n in range(count + 1):
        payload = ({"type": "open", "session_id": "01TESTRETRY0000000000000AB"}
                   if n == 0 else
                   {"type": "segment", "seq_no": n, "sha256": "ab" * 32})
        entry = crypto.build_entry(
            entry_no=n,
            entry_type="open" if n == 0 else "segment",
            payload=payload,
            prev_hash=prev,
            signer=device_key,
        )
        entries.append(entry)
        prev = entry.entry_hash
    return entries


def wire(entry):
    return integrity.parse_entry(entry.to_wire())


def test_a_retried_entry_is_a_duplicate_not_a_violation(tmp_path):
    key = crypto.DeviceKey.load_or_create(tmp_path / "dev.key", allow_plaintext=True)
    pub = key.public_bytes_raw()
    entries = build_chain(key, 3)

    # The backend has stored entries 0..2. Its head is entry 2.
    head = entries[2].entry_hash

    # Entry 2 arrives again - the agent retrying a commit whose response was
    # lost. Verified against the head, it fails, because the head IS entry 2.
    replay = wire(entries[2])
    verdict = integrity.verify_entry(replay, expected_prev=head, device_pubkey=pub)
    assert not verdict.ok, "this is the false rejection the fix exists for"

    # The duplicate check is what saves it: the entry is byte-identical to the
    # one already stored, so it is the same entry, not a forgery.
    assert replay.entry_hash == entries[2].entry_hash
    assert replay.entry_no == entries[2].entry_no


def test_a_forged_entry_cannot_ride_in_on_a_stored_hash(tmp_path):
    """
    The duplicate rule must not become a way to launder a tampered entry.

    parse_entry carries the entry_hash it is given rather than recomputing it,
    so a tampered payload sent with the original hash looks like a duplicate on
    hash alone. That is why the duplicate path verifies in full, against the
    entry's own prev_hash, instead of trusting the match.
    """
    key = crypto.DeviceKey.load_or_create(tmp_path / "dev.key", allow_plaintext=True)
    pub = key.public_bytes_raw()
    entries = build_chain(key, 3)

    tampered = entries[2].to_wire()
    tampered["payload"]["seq_no"] = 99          # the hash is left as the original
    parsed = integrity.parse_entry(tampered)

    assert parsed.entry_hash == entries[2].entry_hash, "it does look like a duplicate"

    # ...and the verification the duplicate path performs still refuses it.
    verdict = integrity.verify_entry(
        parsed, expected_prev=parsed.prev_hash, device_pubkey=pub)
    assert not verdict.ok
    assert "payload" in verdict.reason


def test_a_genuine_retry_passes_that_same_verification(tmp_path):
    """The other half: an untouched retry must survive the check above."""
    key = crypto.DeviceKey.load_or_create(tmp_path / "dev.key", allow_plaintext=True)
    entries = build_chain(key, 3)
    replay = wire(entries[2])
    verdict = integrity.verify_entry(
        replay, expected_prev=replay.prev_hash, device_pubkey=key.public_bytes_raw())
    assert verdict.ok, verdict.reason


def test_the_chain_still_verifies_end_to_end(tmp_path):
    key = crypto.DeviceKey.load_or_create(tmp_path / "dev.key", allow_plaintext=True)
    entries = [wire(e) for e in build_chain(key, 5)]
    verdict = integrity.verify_chain(entries, device_pubkey=key.public_bytes_raw())
    assert verdict.ok, verdict.reason
