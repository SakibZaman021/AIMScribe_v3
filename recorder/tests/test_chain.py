"""
The integrity chain.

These tests are the evidence behind the claim that a recording cannot be quietly
altered. Each one corresponds to a specific tampering attempt.
"""
from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone

import pytest

from core import crypto
from core.crypto import ChainEntry, DeviceKey, build_entry, verify_chain


def _session_chain(device_key: DeviceKey, segments: int = 3):
    """open -> N segments -> close, exactly as the agent builds it."""
    start = datetime(2026, 7, 26, 9, 30, tzinfo=timezone.utc)
    entries = [build_entry(
        entry_no=0,
        entry_type="open",
        payload=crypto.open_payload(
            device_id="dev-1", doctor_id="DR001", hospital_id="HOSP001",
            patient_ref="P12345", opened_at=start,
            sample_rate=44100, channels=1, sample_width=2),
        prev_hash=None,
        signer=device_key,
    )]

    for index in range(1, segments + 1):
        entries.append(build_entry(
            entry_no=len(entries),
            entry_type="segment",
            payload=crypto.segment_payload(
                seq_no=index,
                audio_sha256=crypto.sha256_bytes(f"audio-{index}".encode()),
                byte_length=16_000_000,
                duration_seconds=180.0,
                captured_start_at=start + timedelta(minutes=3 * (index - 1)),
                captured_end_at=start + timedelta(minutes=3 * index),
                rms_mean=1200.0,
                is_final=(index == segments),
            ),
            prev_hash=entries[-1].entry_hash,
            signer=device_key,
        ))

    entries.append(build_entry(
        entry_no=len(entries),
        entry_type="close",
        payload=crypto.close_payload(
            closed_at=start + timedelta(minutes=3 * segments),
            segment_count=segments, duration_seconds=180.0 * segments,
            paused_seconds=0.0),
        prev_hash=entries[-1].entry_hash,
        signer=device_key,
    ))
    return entries


def test_intact_chain_verifies(device_key):
    chain = _session_chain(device_key)
    assert verify_chain(chain, device_public_key=device_key.public_key())


def test_deleting_a_segment_is_detected(device_key):
    """The classic attack: remove the part of the consultation you dislike."""
    chain = _session_chain(device_key, segments=3)
    tampered = chain[:2] + chain[3:]

    verdict = verify_chain(tampered, device_public_key=device_key.public_key())
    assert not verdict.ok
    assert verdict.failed_entry_no == 3


def test_reordering_segments_is_detected(device_key):
    chain = _session_chain(device_key, segments=3)
    tampered = list(chain)
    tampered[1], tampered[2] = tampered[2], tampered[1]

    verdict = verify_chain(tampered, device_public_key=device_key.public_key())
    assert not verdict.ok


def test_substituting_audio_is_detected(device_key):
    """Swapping the audio changes its hash, which is committed inside the chain."""
    chain = _session_chain(device_key)
    tampered = list(chain)

    payload = copy.deepcopy(tampered[1].payload)
    payload["audio_sha256"] = crypto.sha256_bytes(b"different audio").hex()
    tampered[1] = ChainEntry(
        entry_no=tampered[1].entry_no,
        entry_type=tampered[1].entry_type,
        payload=payload,
        payload_sha256=tampered[1].payload_sha256,   # stale hash
        prev_hash=tampered[1].prev_hash,
        entry_hash=tampered[1].entry_hash,
        signature=tampered[1].signature,
    )

    verdict = verify_chain(tampered, device_public_key=device_key.public_key())
    assert not verdict.ok
    assert "payload" in verdict.reason


def test_recomputing_hashes_after_edit_still_fails_without_the_key(device_key, tmp_path):
    """
    A thorough attacker rebuilds the chain after editing.

    They can recompute every hash, but not the signatures - the device key is
    non-exportable, so a disk image yields nothing.
    """
    chain = _session_chain(device_key, segments=3)
    attacker_key = DeviceKey.load_or_create(tmp_path / "attacker.dpapi", allow_plaintext=True)

    rebuilt = []
    prev = None
    for entry in chain[:2] + chain[3:]:      # segment 2 removed
        new_entry = build_entry(
            entry_no=len(rebuilt),
            entry_type=entry.entry_type,
            payload=entry.payload,
            prev_hash=prev,
            signer=attacker_key,
        )
        rebuilt.append(new_entry)
        prev = new_entry.entry_hash

    # Self-consistent, so it passes structural checks alone...
    assert verify_chain(rebuilt)
    # ...but not against the enrolled device's registered public key.
    verdict = verify_chain(rebuilt, device_public_key=device_key.public_key())
    assert not verdict.ok
    assert "signature" in verdict.reason


def test_unsigned_entry_is_rejected(device_key):
    chain = _session_chain(device_key, segments=1)
    chain[1] = ChainEntry(
        entry_no=chain[1].entry_no,
        entry_type=chain[1].entry_type,
        payload=chain[1].payload,
        payload_sha256=chain[1].payload_sha256,
        prev_hash=chain[1].prev_hash,
        entry_hash=chain[1].entry_hash,
        signature=None,
    )
    verdict = verify_chain(chain, device_public_key=device_key.public_key())
    assert not verdict.ok
    assert "unsigned" in verdict.reason


def test_pause_is_part_of_the_chain(device_key):
    """
    A supervised pause is a chain entry, which is what makes the gap explained
    rather than merely missing.
    """
    now = datetime(2026, 7, 26, 10, 0, tzinfo=timezone.utc)
    chain = _session_chain(device_key, segments=1)[:2]

    pause = build_entry(
        entry_no=2, entry_type="pause",
        payload=crypto.pause_payload(
            reason="patient_declined",
            reason_detail="family matter",
            authorised_by="DR001",
            supervisor_required=False,
            at=now),
        prev_hash=chain[-1].entry_hash, signer=device_key)
    resume = build_entry(
        entry_no=3, entry_type="resume",
        payload=crypto.resume_payload(at=now + timedelta(minutes=5), paused_seconds=300.0),
        prev_hash=pause.entry_hash, signer=device_key)

    chain += [pause, resume]
    assert verify_chain(chain, device_public_key=device_key.public_key())

    recorded = [e for e in chain if e.entry_type == "pause"][0]
    assert recorded.payload["reason"] == "patient_declined"
    assert recorded.payload["authorised_by"] == "DR001"


def test_empty_chain_is_rejected():
    assert not verify_chain([])


@pytest.mark.parametrize("left,right", [
    ((b"ab", b"c"), (b"a", b"bc")),
    ((b"", b"x"), (b"x", b"")),
])
def test_digest_is_unambiguous(left, right):
    """
    Length prefixing means field boundaries cannot be shifted.

    A plain concatenation would hash these pairs identically, which is how
    forgeries against naive schemes are built.
    """
    assert crypto.digest(*left) != crypto.digest(*right)
