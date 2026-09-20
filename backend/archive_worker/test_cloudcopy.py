"""
The lossless cloud copy (SRS-ARC-08..13).

The copy exists so that a lost archive can be rebuilt exactly, and these tests
are about that word: the FLAC must decode to the same samples, and a copy that
was changed or cut short must refuse to come back rather than come back short.

    cd archive_worker && python -m pytest -q test_cloudcopy.py
"""
from __future__ import annotations

import json
import os
import wave
from pathlib import Path

import pytest

import cloudcopy
from cloudcopy import CopyError

soundfile = pytest.importorskip("soundfile")

KEY = bytes(range(32))
SAMPLE_RATE = 44100


def speech_like_wav(path: Path, seconds: float = 0.5) -> Path:
    """Something that actually compresses, rather than a constant tone."""
    import math
    frames = int(seconds * SAMPLE_RATE)
    data = bytearray()
    for n in range(frames):
        value = int(8000 * math.sin(n / 30.0) + 900 * math.sin(n / 3.3))
        data += int(value).to_bytes(2, "little", signed=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(SAMPLE_RATE)
        handle.writeframes(bytes(data))
    return path


# ============================================================
# Lossless, and proven so
# ============================================================

def test_the_flac_holds_exactly_the_archived_audio(tmp_path):
    """SRS-ARC-09 step 3, SRS-ARC-11."""
    wav = speech_like_wav(tmp_path / "visit.wav")
    flac = cloudcopy.to_flac(wav, tmp_path / "visit.flac")

    assert flac.exists()
    assert cloudcopy.flac_matches_wav(flac, wav)
    assert flac.stat().st_size < wav.stat().st_size      # it did compress


def test_a_corrupted_flac_is_caught_before_it_is_uploaded(tmp_path):
    """AT-66: a mismatch is detected, so the pieces are kept."""
    wav = speech_like_wav(tmp_path / "visit.wav")
    flac = cloudcopy.to_flac(wav, tmp_path / "visit.flac")

    data = bytearray(flac.read_bytes())
    data[len(data) // 2] ^= 0xFF
    flac.write_bytes(bytes(data))

    # Either the samples differ or the file no longer decodes at all. Both mean
    # the same thing here: do not upload, do not delete the pieces.
    try:
        assert not cloudcopy.flac_matches_wav(flac, wav)
    except CopyError:
        pass


def test_a_truncated_flac_does_not_pass_as_the_recording(tmp_path):
    wav = speech_like_wav(tmp_path / "visit.wav", seconds=0.4)
    flac = cloudcopy.to_flac(wav, tmp_path / "visit.flac")
    data = flac.read_bytes()
    flac.write_bytes(data[: len(data) // 2])

    try:
        assert not cloudcopy.flac_matches_wav(flac, wav)
    except CopyError:
        pass


def test_the_fingerprint_is_of_the_audio_not_the_file(tmp_path):
    wav = speech_like_wav(tmp_path / "visit.wav")
    flac = cloudcopy.to_flac(wav, tmp_path / "visit.flac")
    assert cloudcopy.audio_fingerprint(flac) == cloudcopy.audio_fingerprint(wav)
    assert cloudcopy.sha256_file(flac) != cloudcopy.sha256_file(wav)


def test_different_audio_has_a_different_fingerprint(tmp_path):
    one = speech_like_wav(tmp_path / "one.wav", seconds=0.3)
    other = speech_like_wav(tmp_path / "other.wav", seconds=0.31)
    assert cloudcopy.audio_fingerprint(one) != cloudcopy.audio_fingerprint(other)


# ============================================================
# Encrypted before it leaves UIU
# ============================================================

def test_the_copy_comes_back_byte_for_byte(tmp_path):
    """AT-74: a restore has to produce the archived file exactly."""
    wav = speech_like_wav(tmp_path / "visit.wav")
    flac = cloudcopy.to_flac(wav, tmp_path / "visit.flac")

    sealed = cloudcopy.encrypt_file(flac, tmp_path / "visit.flac.enc", KEY)
    assert sealed.plain_sha256 == cloudcopy.sha256_file(flac)
    assert sealed.sha256 == cloudcopy.sha256_file(sealed.path)

    back = cloudcopy.decrypt_file(sealed.path, tmp_path / "restored.flac", KEY)
    assert back.read_bytes() == flac.read_bytes()
    assert cloudcopy.flac_matches_wav(back, wav)


def test_the_patient_is_not_readable_in_the_uploaded_object(tmp_path):
    """SRS-DAT-08: the storage provider holds nothing it can read."""
    document = {"patient": {"full_name": "Ayesha Rahman", "patient_id": "P0012345"}}
    plain = tmp_path / "visit.json"
    plain.write_text(json.dumps(document), encoding="utf-8")

    sealed = cloudcopy.encrypt_file(plain, tmp_path / "visit.json.enc", KEY)
    blob = sealed.path.read_bytes()
    assert b"Ayesha" not in blob and b"P0012345" not in blob
    assert blob.startswith(b"AIMSCPY1")

    back = cloudcopy.decrypt_file(sealed.path, tmp_path / "back.json", KEY)
    assert json.loads(back.read_text(encoding="utf-8")) == document


def test_a_changed_object_refuses_to_decrypt(tmp_path):
    plain = tmp_path / "visit.json"
    plain.write_text("x" * 5000, encoding="utf-8")
    sealed = cloudcopy.encrypt_file(plain, tmp_path / "visit.json.enc", KEY)

    data = bytearray(sealed.path.read_bytes())
    data[-20] ^= 0x01
    sealed.path.write_bytes(bytes(data))

    with pytest.raises(CopyError):
        cloudcopy.decrypt_file(sealed.path, tmp_path / "back.json", KEY)


def test_a_truncated_object_refuses_to_decrypt(tmp_path, monkeypatch):
    """A copy cut short must fail, not come back as a shorter recording."""
    monkeypatch.setattr(cloudcopy, "CHUNK_BYTES", 1024)
    plain = tmp_path / "visit.bin"
    plain.write_bytes(os.urandom(4096))
    sealed = cloudcopy.encrypt_file(plain, tmp_path / "visit.bin.enc", KEY)

    data = sealed.path.read_bytes()
    sealed.path.write_bytes(data[: len(data) // 2])
    with pytest.raises(CopyError):
        cloudcopy.decrypt_file(sealed.path, tmp_path / "back.bin", KEY)


def test_the_wrong_key_gets_nothing(tmp_path):
    plain = tmp_path / "visit.json"
    plain.write_text("clinical", encoding="utf-8")
    sealed = cloudcopy.encrypt_file(plain, tmp_path / "visit.json.enc", KEY)

    with pytest.raises(CopyError):
        cloudcopy.decrypt_file(sealed.path, tmp_path / "back.json", bytes(32))


def test_a_file_larger_than_one_chunk_survives_the_round_trip(tmp_path, monkeypatch):
    monkeypatch.setattr(cloudcopy, "CHUNK_BYTES", 1000)
    plain = tmp_path / "long.bin"
    plain.write_bytes(os.urandom(4321))
    sealed = cloudcopy.encrypt_file(plain, tmp_path / "long.bin.enc", KEY)
    back = cloudcopy.decrypt_file(sealed.path, tmp_path / "back.bin", KEY)
    assert back.read_bytes() == plain.read_bytes()


def test_an_empty_file_still_round_trips(tmp_path):
    plain = tmp_path / "empty.json"
    plain.write_bytes(b"")
    sealed = cloudcopy.encrypt_file(plain, tmp_path / "empty.json.enc", KEY)
    back = cloudcopy.decrypt_file(sealed.path, tmp_path / "back.json", KEY)
    assert back.read_bytes() == b""


# ============================================================
# The key
# ============================================================

def test_the_key_is_read_as_base64_or_hex():
    import base64
    assert cloudcopy.load_key(base64.b64encode(KEY).decode()) == KEY
    assert cloudcopy.load_key(KEY.hex()) == KEY


def test_no_key_means_no_copies_rather_than_plain_audio():
    assert cloudcopy.load_key(None) is None
    assert cloudcopy.load_key("   ") is None


@pytest.mark.parametrize("value", ["not-a-key", "c2hvcnQ="])
def test_a_key_that_is_not_32_bytes_is_refused(value):
    with pytest.raises(CopyError):
        cloudcopy.load_key(value)
