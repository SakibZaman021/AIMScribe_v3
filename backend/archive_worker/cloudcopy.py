"""
The lossless cloud copy (SRS 3.2 §7.10, `SRS-ARC-08`-`SRS-ARC-13`).

Pieces in the cloud are not deleted when a recording is archived. Each finished
consultation is first kept in the cloud as one merged, losslessly compressed
copy, so that a lost archive volume can be rebuilt exactly:

    WAV (archived) -> FLAC -> decoded and compared -> encrypted -> copy bucket

Two rules shape this module:

**Lossless, and proven so.** The FLAC is decoded again and its samples compared
with the archived WAV before anything is uploaded (`SRS-ARC-09`, step 3). A
compressor that silently resampled, or a half-written file, is caught here
rather than in five years when someone needs the audio.

**Encrypted before it leaves.** The key lives at UIU and never reaches the
server or the bucket (`SRS-DAT-08`), so the copy is unreadable to the storage
provider. The file is encrypted in chunks, each with its own nonce and with its
position sealed into it, so a truncated or re-ordered file fails to decrypt
instead of decrypting to something shorter that looks complete.
"""
from __future__ import annotations

import base64
import hashlib
import os
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional, Tuple

MAGIC = b"AIMSCPY1"
CHUNK_BYTES = 4 * 1024 * 1024          # plaintext per chunk
NONCE_BYTES = 12
_HEADER = struct.Struct("<8sI")        # magic, chunk size
_FRAME = struct.Struct("<I")           # ciphertext length


class CopyError(RuntimeError):
    """This copy cannot be made or trusted. Keep the pieces and try again."""


# ============================================================
# FLAC
# ============================================================

def _soundfile():
    try:
        import soundfile                                    # noqa: WPS433
    except Exception as exc:                                # pragma: no cover
        raise CopyError(
            "the soundfile package is needed for the lossless cloud copy "
            "(pip install soundfile)") from exc
    return soundfile


def to_flac(wav_path: Path, flac_path: Path) -> Path:
    """
    Compress the archived WAV to FLAC, written beside it and renamed only once
    complete, so a crash never leaves a short file that looks finished.
    """
    sf = _soundfile()
    partial = flac_path.with_suffix(flac_path.suffix + ".partial")
    try:
        with sf.SoundFile(str(wav_path)) as source:
            if source.subtype not in ("PCM_16", "PCM_24", "PCM_S8", "PCM_32"):
                raise CopyError(f"{wav_path.name} is {source.subtype}, not integer PCM; "
                                "FLAC would not be lossless")
            with sf.SoundFile(str(partial), mode="w", samplerate=source.samplerate,
                              channels=source.channels, format="FLAC",
                              subtype=source.subtype) as target:
                for block in source.blocks(blocksize=1 << 18, dtype="int32",
                                           always_2d=True):
                    target.write(block)
        os.replace(partial, flac_path)
    except CopyError:
        partial.unlink(missing_ok=True)
        raise
    except Exception as exc:
        partial.unlink(missing_ok=True)
        raise CopyError(f"could not compress {wav_path.name}: {exc}") from exc
    return flac_path


def audio_fingerprint(path: Path) -> str:
    """
    A fingerprint of the audio itself - the samples, the rate and the channel
    count - not of the file. Two files with the same fingerprint hold the same
    recording however they are encoded.
    """
    sf = _soundfile()
    digest = hashlib.sha256()
    try:
        with sf.SoundFile(str(path)) as audio:
            digest.update(f"{audio.samplerate}/{audio.channels}/".encode("ascii"))
            for block in audio.blocks(blocksize=1 << 18, dtype="int32", always_2d=True):
                digest.update(block.tobytes())
    except Exception as exc:
        raise CopyError(f"could not read the audio in {path.name}: {exc}") from exc
    return digest.hexdigest()


def flac_matches_wav(flac_path: Path, wav_path: Path) -> bool:
    """
    `SRS-ARC-09`, step 3: decode the FLAC and confirm it holds exactly the
    samples of the archived WAV. Nothing is uploaded until this is true.
    """
    return audio_fingerprint(flac_path) == audio_fingerprint(wav_path)


# ============================================================
# Encryption
# ============================================================

def load_key(value: Optional[str]) -> Optional[bytes]:
    """
    The copy key as 32 bytes, from base64 or 64 hex characters.

    Returns None when unset - the caller then makes no copies at all, rather
    than uploading patient audio in the clear.
    """
    text = (value or "").strip()
    if not text:
        return None
    try:
        key = bytes.fromhex(text) if len(text) == 64 and not text.endswith("=") \
            else base64.b64decode(text, validate=True)
    except Exception as exc:
        raise CopyError("AIMS_COPY_KEY is neither base64 nor 64 hex characters") from exc
    if len(key) != 32:
        raise CopyError(f"AIMS_COPY_KEY must be 32 bytes, not {len(key)}")
    return key


def _cipher(key: bytes):
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except Exception as exc:                                # pragma: no cover
        raise CopyError("the cryptography package is needed to encrypt the copy") from exc
    return AESGCM(key)


def _aad(index: int, last: bool) -> bytes:
    return MAGIC + struct.pack("<I?", index, last)


@dataclass
class Encrypted:
    path: Path
    bytes: int
    sha256: str            # the object as it is uploaded
    plain_sha256: str      # the FLAC or JSON before encryption


def encrypt_file(source: Path, target: Path, key: bytes) -> Encrypted:
    """Encrypt one file for the copy bucket, chunk by chunk."""
    aes = _cipher(key)
    plain = hashlib.sha256()
    cipher_digest = hashlib.sha256()
    partial = target.with_suffix(target.suffix + ".partial")

    def write(handle, data: bytes) -> None:
        handle.write(data)
        cipher_digest.update(data)

    try:
        with open(source, "rb") as reader, open(partial, "wb") as writer:
            write(writer, _HEADER.pack(MAGIC, CHUNK_BYTES))
            index = 0
            block = reader.read(CHUNK_BYTES)
            while True:
                following = reader.read(CHUNK_BYTES)
                last = not following
                plain.update(block)
                nonce = os.urandom(NONCE_BYTES)
                sealed = aes.encrypt(nonce, block, _aad(index, last))
                write(writer, nonce)
                write(writer, _FRAME.pack(len(sealed)))
                write(writer, sealed)
                if last:
                    break
                block, index = following, index + 1
            writer.flush()
            os.fsync(writer.fileno())
        os.replace(partial, target)
    except Exception as exc:
        partial.unlink(missing_ok=True)
        raise CopyError(f"could not encrypt {source.name}: {exc}") from exc

    return Encrypted(path=target, bytes=target.stat().st_size,
                     sha256=cipher_digest.hexdigest(), plain_sha256=plain.hexdigest())


def decrypt_file(source: Path, target: Path, key: bytes) -> Path:
    """
    Turn one copy-bucket object back into the file it was made from - the
    restore path (`AT-74`), and the only way the copy is of any use.
    """
    aes = _cipher(key)
    partial = target.with_suffix(target.suffix + ".partial")
    try:
        with open(source, "rb") as reader, open(partial, "wb") as writer:
            magic, chunk_size = _HEADER.unpack(reader.read(_HEADER.size))
            if magic != MAGIC:
                raise CopyError(f"{source.name} is not an AIMScribe copy")
            index, done = 0, False
            while not done:
                nonce = reader.read(NONCE_BYTES)
                header = reader.read(_FRAME.size)
                if len(nonce) != NONCE_BYTES or len(header) != _FRAME.size:
                    raise CopyError(f"{source.name} ends in the middle of a chunk")
                (length,) = _FRAME.unpack(header)
                sealed = reader.read(length)
                if len(sealed) != length:
                    raise CopyError(f"{source.name} is truncated")
                # Whether this was the last chunk is sealed into it, so a file
                # cut short fails here instead of decrypting to less audio.
                for last in (False, True):
                    try:
                        writer.write(aes.decrypt(nonce, sealed, _aad(index, last)))
                        done = last
                        break
                    except Exception:
                        if last:
                            raise CopyError(
                                f"{source.name} does not decrypt: wrong key, or the "
                                f"file has been changed (chunk {index})")
                index += 1
        os.replace(partial, target)
    except CopyError:
        partial.unlink(missing_ok=True)
        raise
    except Exception as exc:
        partial.unlink(missing_ok=True)
        raise CopyError(f"could not decrypt {source.name}: {exc}") from exc
    return target


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(chunk), b""):
            digest.update(block)
    return digest.hexdigest()


__all__ = ["CopyError", "Encrypted", "audio_fingerprint", "decrypt_file",
           "encrypt_file", "flac_matches_wav", "load_key", "sha256_file", "to_flac"]
