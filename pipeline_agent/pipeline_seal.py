"""
Audio sealed to one machine of the analysis pipeline (053, 27 Nov 2026).

The archive worker decrypts a recording's copy and, as it streams, seals it
again to the public key of the lab machine that claimed the job. Only that
machine's private key opens it, so a copied download, a proxy log or a stolen
link is useless to anyone else.

    header   MAGIC | ephemeral X25519 public key (32) | job id (u16 + bytes)
             | chunk size (u32) | plain size (u64) | nonce prefix (8)
    chunks   AES-256-GCM, 12-byte nonce = prefix + counter (u32);
             associated data = SHA-256(header) | counter | final flag

The key is HKDF-SHA256 of the X25519 shared secret, salted with both public
keys. Every chunk is bound to the header (so to the job), to its position and
to whether it is the last: a truncated, re-ordered or re-labelled file fails
to open instead of opening to something shorter.

This file is used by the archive worker and, unchanged, by the pipeline agent
(pipeline_agent/pipeline_seal.py); a test checks the two copies are identical.
"""
from __future__ import annotations

import hashlib
import os
import struct
from typing import Callable, Iterable, Iterator, Tuple

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

MAGIC = b"AIMSPIP1"
INFO = b"aimscribe-pipeline-v1"
CHUNK = 1024 * 1024
TAG = 16
MAX_JOB_ID = 128


class SealError(ValueError):
    """The sealed file is not for this key or this job, or it was changed."""


def _key(shared: bytes, ephemeral_pub: bytes, recipient_pub: bytes) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=ephemeral_pub + recipient_pub,
                info=INFO).derive(shared)


def _raw(public: X25519PublicKey) -> bytes:
    return public.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def _aad(header_hash: bytes, counter: int, final: bool) -> bytes:
    return header_hash + struct.pack(">IB", counter, 1 if final else 0)


def _chunks_count(plain_size: int, chunk: int) -> int:
    return max(1, -(-plain_size // chunk))


def sealed_size(plain_size: int, job_id: str, chunk: int = CHUNK) -> int:
    """The exact number of bytes a sealed file of `plain_size` bytes takes."""
    header = len(MAGIC) + 32 + 2 + len(job_id.encode("utf-8")) + 4 + 8 + 8
    return header + plain_size + _chunks_count(plain_size, chunk) * TAG


def seal(chunks: Iterable[bytes], plain_size: int, recipient_public: bytes, job_id: str,
         chunk: int = CHUNK) -> Iterator[bytes]:
    """
    Seal a stream of `plain_size` bytes to `recipient_public` (raw X25519, 32
    bytes). Yields the header, then one sealed chunk at a time. Raises
    SealError if the stream is not exactly `plain_size` bytes long.
    """
    job = job_id.encode("utf-8")
    if not job or len(job) > MAX_JOB_ID:
        raise SealError("the job id must be 1-128 bytes")
    if len(recipient_public) != 32:
        raise SealError("the recipient key must be 32 bytes (X25519)")
    ephemeral = X25519PrivateKey.generate()
    ephemeral_pub = _raw(ephemeral.public_key())
    key = _key(ephemeral.exchange(X25519PublicKey.from_public_bytes(recipient_public)),
               ephemeral_pub, recipient_public)
    prefix = os.urandom(8)
    header = (MAGIC + ephemeral_pub + struct.pack(">H", len(job)) + job
              + struct.pack(">IQ", chunk, plain_size) + prefix)
    header_hash = hashlib.sha256(header).digest()
    aead = AESGCM(key)
    yield header

    total = _chunks_count(plain_size, chunk)
    buffer, counter, seen = bytearray(), 0, 0

    def sealed(block: bytes, final: bool) -> bytes:
        nonce = prefix + struct.pack(">I", counter)
        return aead.encrypt(nonce, bytes(block), _aad(header_hash, counter, final))

    for piece in chunks:
        if not piece:
            continue
        seen += len(piece)
        if seen > plain_size:
            raise SealError("the stream is longer than its stated size")
        buffer += piece
        while len(buffer) >= chunk and counter < total - 1:
            yield sealed(buffer[:chunk], False)
            del buffer[:chunk]
            counter += 1
    if seen != plain_size:
        raise SealError(f"the stream ended at {seen} of {plain_size} bytes")
    yield sealed(buffer, True)


def open_sealed(read: Callable[[int], bytes], private_key: X25519PrivateKey,
                expect_job_id: str) -> Tuple[int, Iterator[bytes]]:
    """
    Open a sealed stream with this machine's private key. `read(n)` returns up
    to n bytes. Returns the plain size and an iterator of plain chunks; the
    iterator raises SealError on any change, truncation or wrong job.
    """
    def exactly(n: int) -> bytes:
        out = bytearray()
        while len(out) < n:
            block = read(n - len(out))
            if not block:
                raise SealError("the sealed file ends early")
            out += block
        return bytes(out)

    magic = exactly(len(MAGIC))
    if magic != MAGIC:
        raise SealError("this is not a sealed pipeline file")
    ephemeral_pub = exactly(32)
    (job_len,) = struct.unpack(">H", exactly(2))
    if not 0 < job_len <= MAX_JOB_ID:
        raise SealError("the job id in the header is not valid")
    job = exactly(job_len)
    chunk, plain_size = struct.unpack(">IQ", exactly(12))
    prefix = exactly(8)
    if job.decode("utf-8", "replace") != expect_job_id:
        raise SealError("this file was sealed for another job")
    if not 1024 <= chunk <= 16 * 1024 * 1024:
        raise SealError("the chunk size in the header is not valid")
    header = MAGIC + ephemeral_pub + struct.pack(">H", job_len) + job \
        + struct.pack(">IQ", chunk, plain_size) + prefix
    header_hash = hashlib.sha256(header).digest()
    recipient_pub = _raw(private_key.public_key())
    try:
        key = _key(private_key.exchange(X25519PublicKey.from_public_bytes(ephemeral_pub)),
                   ephemeral_pub, recipient_pub)
    except ValueError as exc:
        raise SealError(f"the header's key is not valid: {exc}") from None
    aead = AESGCM(key)
    total = _chunks_count(plain_size, chunk)

    def chunks() -> Iterator[bytes]:
        left = plain_size
        for counter in range(total):
            final = counter == total - 1
            size = left if final else chunk
            block = exactly(size + TAG)
            try:
                plain = aead.decrypt(prefix + struct.pack(">I", counter), block,
                                     _aad(header_hash, counter, final))
            except Exception:
                raise SealError(f"chunk {counter} does not open: not this machine's "
                                f"key, or the file was changed") from None
            left -= len(plain)
            yield plain
        if read(1):
            raise SealError("the sealed file has bytes after its last chunk")

    return plain_size, chunks()


def new_box_key() -> Tuple[X25519PrivateKey, bytes]:
    """A machine's receiving key pair: the private key and the raw public key."""
    private = X25519PrivateKey.generate()
    return private, _raw(private.public_key())


__all__ = ["MAGIC", "CHUNK", "SealError", "seal", "open_sealed", "sealed_size",
           "new_box_key"]
