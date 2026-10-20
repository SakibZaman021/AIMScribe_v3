"""
The cloud copy (SRS-ARC-08..13).

The copy exists so that a lost archive can be rebuilt exactly, and these tests
are about that word: the sealed copy must decrypt to the archived WAV byte for
byte, and a copy that was changed or cut short must refuse to come back rather
than come back short. Copies made earlier as FLAC must still restore.

    cd archive_worker && python -m pytest -q test_cloudcopy.py
"""
from __future__ import annotations

import json
import os
import wave
from pathlib import Path

import pytest

import cloudcopy
import restore
from cloudcopy import CopyError

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
# The raw WAV, proven the same
# ============================================================

def test_the_copy_is_the_wav_itself_byte_for_byte(tmp_path):
    """AT-74, SRS-ARC-09 step 3: a restore produces the archived file exactly."""
    wav = speech_like_wav(tmp_path / "visit.wav")

    sealed = cloudcopy.encrypt_file(wav, tmp_path / "visit.wav.enc", KEY)
    assert sealed.plain_sha256 == cloudcopy.sha256_file(wav)
    assert sealed.sha256 == cloudcopy.sha256_file(sealed.path)
    assert cloudcopy.sealed_sha256(sealed.path, KEY) == cloudcopy.sha256_file(wav)

    back = cloudcopy.decrypt_file(sealed.path, tmp_path / "restored.wav", KEY)
    assert back.read_bytes() == wav.read_bytes()


def test_checking_a_sealed_copy_writes_nothing(tmp_path):
    wav = speech_like_wav(tmp_path / "visit.wav")
    sealed = cloudcopy.encrypt_file(wav, tmp_path / "visit.wav.enc", KEY)
    before = sorted(p.name for p in tmp_path.iterdir())
    cloudcopy.sealed_sha256(sealed.path, KEY)
    assert sorted(p.name for p in tmp_path.iterdir()) == before


def test_a_damaged_sealed_copy_fails_the_check_before_upload(tmp_path):
    """AT-66: caught on this machine, so the pieces are kept."""
    wav = speech_like_wav(tmp_path / "visit.wav")
    sealed = cloudcopy.encrypt_file(wav, tmp_path / "visit.wav.enc", KEY)
    data = bytearray(sealed.path.read_bytes())
    data[len(data) // 2] ^= 0xFF
    sealed.path.write_bytes(bytes(data))
    with pytest.raises(CopyError):
        cloudcopy.sealed_sha256(sealed.path, KEY)


def test_no_flac_writer_remains():
    """Copies are raw WAV; nothing here can make a FLAC any more."""
    assert not hasattr(cloudcopy, "to_flac")


def test_restore_gives_back_the_wav(tmp_path):
    wav = speech_like_wav(tmp_path / "visit.wav")
    sealed = cloudcopy.encrypt_file(wav, tmp_path / "visit.v1.wav.enc", KEY)
    out = restore.restore_one(sealed.path, tmp_path / "out", KEY)
    assert out.name == "visit.v1.wav"
    assert out.read_bytes() == wav.read_bytes()


def test_an_older_flac_copy_still_restores_to_the_same_audio(tmp_path):
    """Copies made before the switch to raw WAV must not become unreadable."""
    soundfile = pytest.importorskip("soundfile")
    wav = speech_like_wav(tmp_path / "visit.wav")
    flac = tmp_path / "old.v1.flac"
    with soundfile.SoundFile(str(wav)) as source:
        with soundfile.SoundFile(str(flac), mode="w", samplerate=source.samplerate,
                                 channels=source.channels, format="FLAC",
                                 subtype=source.subtype) as target:
            target.write(source.read(dtype="int32", always_2d=True))
    sealed = cloudcopy.encrypt_file(flac, tmp_path / "old.v1.flac.enc", KEY)
    flac.unlink()

    out = restore.restore_one(sealed.path, tmp_path / "out", KEY)
    assert out.suffix == ".wav"
    assert cloudcopy.audio_fingerprint(out) == cloudcopy.audio_fingerprint(wav)


# ============================================================
# Encrypted before it leaves UIU
# ============================================================

def test_the_patient_is_not_readable_in_the_uploaded_object(tmp_path):
    """SRS-DAT-08: the storage provider holds nothing it can read."""
    document = {"patient": {"full_name": "Ayesha Rahman", "patient_id": "P0012345"}}
    plain = tmp_path / "visit.json"
    plain.write_text(json.dumps(document), encoding="utf-8")

    sealed = cloudcopy.encrypt_file(plain, tmp_path / "visit.json.enc", KEY)
    blob = sealed.path.read_bytes()
    assert b"Ayesha" not in blob and b"P0012345" not in blob
    assert blob.startswith(b"AIMSCPY2")

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
