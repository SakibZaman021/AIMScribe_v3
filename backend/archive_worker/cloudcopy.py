"""
The cloud copy (SRS 3.2 §7.10, `SRS-ARC-08`-`SRS-ARC-13`).

Pieces in the cloud are not deleted when a recording is archived. Each finished
consultation is first kept in the cloud as one merged copy of the raw WAV,
byte for byte the file in the archive, so that a lost archive volume can be
rebuilt exactly:

    WAV (archived) -> encrypted -> decrypted again and hashed -> copy bucket

The audio is not compressed. The dataset is raw WAV by decision, and a copy
that is the archived file itself needs no codec to restore and cannot differ
from it in any way a sample comparison might miss. Copies made earlier as FLAC
can still be read (`restore.py`); none are made any more.

Two rules shape this module:

**The same bytes, and proven so.** The plaintext is hashed as it is encrypted,
and the sealed file is decrypted again and hashed before anything is uploaded
(`SRS-ARC-09`, step 3). Both must equal the archive's own SHA-256, so a
half-written file or a bad key is caught here rather than in five years when
someone needs the audio.

**Encrypted before it leaves.** The key lives at UIU and never reaches the
server or the bucket (`SRS-DAT-08`), so the copy is unreadable to the storage
provider. The file is encrypted in chunks, each with its own nonce and with its
position sealed into it, so a truncated or re-ordered file fails to decrypt
instead of decrypting to something shorter that looks complete.

**Bound to its recording.** Since format 2 (`AIMSCPY2`) every object also
carries a small binding - the session, the part, the kind (audio or JSON) and
a fingerprint of the file name - in its header, and the binding is sealed into
every chunk. The name itself is not in it: it begins with the patient's
identifier, and the header is readable by whoever holds the object. An
object copied or renamed onto another recording's key therefore either fails
to decrypt (if the header was edited to match) or names the recording it really
belongs to, and the restore refuses it. Format-1 objects, made before this,
still decrypt; they simply carry no binding to check.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterator, Optional, Tuple

MAGIC = b"AIMSCPY1"                    # format 1: no binding (read only)
MAGIC_BOUND = b"AIMSCPY2"              # format 2: bound to its recording
CHUNK_BYTES = 4 * 1024 * 1024          # plaintext per chunk
NONCE_BYTES = 12
_HEADER = struct.Struct("<8sI")        # magic, chunk size
_BINDING_LEN = struct.Struct("<H")     # format 2: binding length, then the binding
MAX_BINDING_BYTES = 1024
_FRAME = struct.Struct("<I")           # ciphertext length


class CopyError(RuntimeError):
    """This copy cannot be made or trusted. Keep the pieces and try again."""


# ============================================================
# FLAC - reading older copies only
# ============================================================

def _soundfile():
    try:
        import soundfile                                    # noqa: WPS433
    except Exception as exc:                                # pragma: no cover
        raise CopyError(
            "the soundfile package is needed to restore an older FLAC copy "
            "(pip install soundfile)") from exc
    return soundfile


def audio_fingerprint(path: Path) -> str:
    """
    A fingerprint of the audio itself - the samples, the rate and the channel
    count - not of the file. Two files with the same fingerprint hold the same
    recording however they are encoded. Used only to check an older FLAC copy
    against the WAV restored from it.
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
    """An older FLAC copy and the WAV decoded from it hold the same samples."""
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


def _aad(index: int, last: bool, binding: Optional[bytes] = None) -> bytes:
    if binding is None:                                     # format 1
        return MAGIC + struct.pack("<I?", index, last)
    return (MAGIC_BOUND + hashlib.sha256(binding).digest()
            + struct.pack("<I?", index, last))


def binding(session_id: str, kind: str, *, part_no: Optional[int] = None,
            stem: Optional[str] = None) -> Dict[str, Any]:
    """
    What a copy is a copy of: the session ULID, the part, audio or JSON, and
    the SHA-256 of the file name (`stem_sha256`). Nothing that identifies the
    patient: the binding sits in the clear in the object's header.
    """
    found: Dict[str, Any] = {"session_id": str(session_id), "kind": str(kind)}
    if part_no is not None:
        found["part_no"] = int(part_no)
    if stem:
        found["stem_sha256"] = stem_fingerprint(stem)
    return found


def stem_fingerprint(stem: str) -> str:
    """The file name's fingerprint, as a binding records it."""
    return hashlib.sha256(str(stem).encode("utf-8")).hexdigest()


def _encode_binding(value: Dict[str, Any]) -> bytes:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if len(raw) > MAX_BINDING_BYTES:
        raise CopyError("the copy's binding is too long")
    return raw


@dataclass
class Encrypted:
    path: Path
    bytes: int
    sha256: str            # the object as it is uploaded
    plain_sha256: str      # the WAV or JSON before encryption
    md5: str               # what the store returns as the object's ETag


def encrypt_file(source: Path, target: Path, key: bytes,
                 bound_to: Optional[Dict[str, Any]] = None) -> Encrypted:
    """
    Encrypt one file for the copy bucket, chunk by chunk, bound to `bound_to`
    (see `binding`). Every copy the worker makes names its recording.
    """
    aes = _cipher(key)
    context = _encode_binding(bound_to or {})
    plain = hashlib.sha256()
    cipher_digest = hashlib.sha256()
    # The store's own fingerprint of a single-part upload is the MD5 of the
    # object. Weak as a hash, but here it is only used to ask the store "is
    # what you hold what we sent?" - and it is the only fingerprint the store
    # will give back without handing over the whole object, which would cost
    # more in traffic than keeping the copy costs in storage.
    etag = hashlib.md5()
    partial = target.with_suffix(target.suffix + ".partial")

    def write(handle, data: bytes) -> None:
        handle.write(data)
        cipher_digest.update(data)
        etag.update(data)

    try:
        fd = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with open(source, "rb") as reader, os.fdopen(fd, "wb") as writer:
            write(writer, _HEADER.pack(MAGIC_BOUND, CHUNK_BYTES))
            write(writer, _BINDING_LEN.pack(len(context)))
            write(writer, context)
            index = 0
            block = reader.read(CHUNK_BYTES)
            while True:
                following = reader.read(CHUNK_BYTES)
                last = not following
                plain.update(block)
                nonce = os.urandom(NONCE_BYTES)
                sealed = aes.encrypt(nonce, block, _aad(index, last, context))
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
                     sha256=cipher_digest.hexdigest(), plain_sha256=plain.hexdigest(),
                     md5=etag.hexdigest())


def _read_header(reader, name: str) -> Optional[bytes]:
    """The binding as sealed (format 2), or None for a format-1 copy."""
    head = reader.read(_HEADER.size)
    if len(head) != _HEADER.size:
        raise CopyError(f"{name} is not an AIMScribe copy")
    magic, _ = _HEADER.unpack(head)
    if magic == MAGIC:
        return None
    if magic != MAGIC_BOUND:
        raise CopyError(f"{name} is not an AIMScribe copy")
    size_raw = reader.read(_BINDING_LEN.size)
    if len(size_raw) != _BINDING_LEN.size:
        raise CopyError(f"{name} ends in its header")
    (size,) = _BINDING_LEN.unpack(size_raw)
    if size > MAX_BINDING_BYTES:
        raise CopyError(f"{name} has a malformed header")
    context = reader.read(size)
    if len(context) != size:
        raise CopyError(f"{name} ends in its header")
    return context


def read_binding(source: Path) -> Optional[Dict[str, Any]]:
    """
    What a copy says it is a copy of: a dict for format 2, None for format 1.
    Not proof by itself - the binding is only authenticated once the chunks
    decrypt with it, so read it after `decrypt_file` or `sealed_sha256`.
    """
    with open(source, "rb") as reader:
        context = _read_header(reader, source.name)
    if context is None:
        return None
    try:
        value = json.loads(context.decode("utf-8")) if context else {}
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CopyError(f"{source.name} has a malformed binding") from exc
    if not isinstance(value, dict):
        raise CopyError(f"{source.name} has a malformed binding")
    return value


def check_binding(found: Optional[Dict[str, Any]], expect: Optional[Dict[str, Any]],
                  name: str, *, allow_unbound: bool = True) -> None:
    """
    Every field of `expect` that the copy's binding records must have the same
    value. A field the binding does not record was not bound when the copy was
    made (a format-1 copy records none) and passes only while `allow_unbound`.
    Only the holder of the copy key can make a binding, so a field present and
    different is the one case that means "another recording's copy".
    """
    if not expect:
        return
    if found is None:
        if allow_unbound:
            return
        raise CopyError(f"{name} was made before copies were bound to their "
                        f"recording, and cannot be checked")
    for field, value in expect.items():
        if value is None:
            continue
        if field not in found:
            if allow_unbound:
                continue
            raise CopyError(f"{name} does not record its {field}, and cannot be checked")
        if str(found.get(field)) != str(value):
            raise CopyError(
                f"{name} is not the copy asked for: its {field} is "
                f"{found.get(field)!r}, expected {value!r}")


def _plain_chunks(source: Path, key: bytes,
                  expect: Optional[Dict[str, Any]] = None) -> Iterator[bytes]:
    """The plaintext of one sealed file, chunk by chunk, each one authenticated."""
    aes = _cipher(key)
    with open(source, "rb") as reader:
        context = _read_header(reader, source.name)
        if expect:
            # Checked before any plaintext leaves; the decryption below then
            # proves the binding read here is the one that was sealed.
            found = None
            if context is not None:
                try:
                    found = json.loads(context.decode("utf-8")) if context else {}
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise CopyError(f"{source.name} has a malformed binding") from exc
            check_binding(found, expect, source.name)
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
                    plain = aes.decrypt(nonce, sealed, _aad(index, last, context))
                    done = last
                    break
                except Exception:
                    if last:
                        raise CopyError(
                            f"{source.name} does not decrypt: wrong key, or the "
                            f"file has been changed (chunk {index})")
            yield plain
            index += 1


def decrypt_file(source: Path, target: Path, key: bytes,
                 expect: Optional[Dict[str, Any]] = None) -> Path:
    """
    Turn one copy-bucket object back into the file it was made from - the
    restore path (`AT-74`), and the only way the copy is of any use. With
    `expect`, a copy bound to anything else is refused. The restored file is
    readable by its owner only: it is patient audio or a clinical record.
    """
    partial = target.with_suffix(target.suffix + ".partial")
    try:
        fd = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as writer:
            for plain in _plain_chunks(source, key, expect):
                writer.write(plain)
        os.replace(partial, target)
    except CopyError:
        partial.unlink(missing_ok=True)
        raise
    except Exception as exc:
        partial.unlink(missing_ok=True)
        raise CopyError(f"could not decrypt {source.name}: {exc}") from exc
    return target


def sealed_sha256(source: Path, key: bytes,
                  expect: Optional[Dict[str, Any]] = None) -> str:
    """
    Decrypt a sealed file in memory and hash what comes out, writing nothing.

    The proof, before upload, that the object about to leave decrypts to the
    archived file with the key UIU holds (`SRS-ARC-09`, step 3).
    """
    digest = hashlib.sha256()
    try:
        for plain in _plain_chunks(source, key, expect):
            digest.update(plain)
    except CopyError:
        raise
    except Exception as exc:
        raise CopyError(f"could not read {source.name} back: {exc}") from exc
    return digest.hexdigest()


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(chunk), b""):
            digest.update(block)
    return digest.hexdigest()


__all__ = ["CopyError", "Encrypted", "audio_fingerprint", "binding", "check_binding",
           "stem_fingerprint",
           "decrypt_file", "encrypt_file", "flac_matches_wav", "load_key",
           "read_binding", "sealed_sha256", "sha256_file"]
