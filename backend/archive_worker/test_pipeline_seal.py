"""
The analysis pipeline's audio (053): the archive worker seals a recording's
copy to the claiming lab machine's key (serve.py POST /seal, pipeline_seal).

What must hold: only that machine opens it; what it opens is the archived WAV
byte for byte; a sealed file for the wrong job, a changed or a cut file never
opens; and a copy that decrypts to anything but the archived file is cut
short before its last chunk.

    cd archive_worker && python -m pytest -q test_pipeline_seal.py
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import urllib.error
import urllib.request
from pathlib import Path

import pytest

import pipeline_seal
from pipeline_seal import SealError, new_box_key, open_sealed, seal, sealed_size
from test_serve import WORKER_KEY, expect, sealed, store, worker, small_chunks  # noqa: F401

JOB = "job_ABCDEFGHJK"
HERE = Path(__file__).resolve().parent


def ask_seal(worker, body, key=WORKER_KEY):
    request = urllib.request.Request(
        f"http://127.0.0.1:{worker.server_address[1]}/seal",
        data=json.dumps(body).encode(), method="POST",
        headers={"X-Worker-Key": key, "Content-Type": "application/json"})
    try:
        response = urllib.request.urlopen(request, timeout=10)
    except urllib.error.HTTPError as err:
        return err.code, dict(err.headers), err.read()
    data = b""
    try:
        while True:
            block = response.read(4096)
            if not block:
                break
            data += block
    except Exception:
        pass
    return response.status, dict(response.headers), data


@pytest.mark.parametrize("size", [0, 1, 1023, 1024, 4096, 70_001])
def test_seal_and_open_give_back_the_same_bytes(size):
    private, public = new_box_key()
    plain = os.urandom(size)
    blob = b"".join(seal([plain[i:i + 777] for i in range(0, size, 777)], size, public,
                         JOB, chunk=1024))
    assert len(blob) == sealed_size(size, JOB, chunk=1024)
    n, chunks = open_sealed(io.BytesIO(blob).read, private, JOB)
    assert n == size and b"".join(chunks) == plain


def test_nothing_else_opens_it():
    private, public = new_box_key()
    plain = os.urandom(5000)
    blob = b"".join(seal([plain], len(plain), public, JOB, chunk=1024))
    cases = {
        "another machine's key": (blob, new_box_key()[0], JOB),
        "another job": (blob, private, "job_OTHER12345"),
        "one byte changed": (blob[:200] + bytes([blob[200] ^ 1]) + blob[201:], private, JOB),
        "cut short": (blob[:-1], private, JOB),
        "bytes added": (blob + b"x", private, JOB),
    }
    for why, (data, key, job) in cases.items():
        with pytest.raises(SealError):
            n, chunks = open_sealed(io.BytesIO(data).read, key, job)
            b"".join(chunks)


def test_a_stream_of_the_wrong_length_is_refused():
    _, public = new_box_key()
    with pytest.raises(SealError):
        b"".join(seal([b"abc"], 4, public, JOB))
    with pytest.raises(SealError):
        b"".join(seal([b"abcde"], 4, public, JOB))


def test_the_worker_seals_the_archived_file_for_the_claiming_machine(tmp_path, store, worker):
    private, public = new_box_key()
    plain = os.urandom(3456)
    url = store.put("a.wav.enc", sealed(tmp_path, plain))
    status, headers, body = ask_seal(worker, {
        "url": url, "expect": expect(), "plain_sha256": hashlib.sha256(plain).hexdigest(),
        "job_id": JOB, "recipient": public.hex()})
    assert status == 200
    assert int(headers["Content-Length"]) == len(body) == sealed_size(len(plain), JOB)
    n, chunks = open_sealed(io.BytesIO(body).read, private, JOB)
    assert b"".join(chunks) == plain


def test_a_copy_that_is_not_the_archived_file_is_cut_before_its_end(tmp_path, store, worker):
    private, public = new_box_key()
    plain = os.urandom(3456)
    url = store.put("b.wav.enc", sealed(tmp_path, plain))
    status, headers, body = ask_seal(worker, {
        "url": url, "expect": expect(), "plain_sha256": "0" * 64,
        "job_id": JOB, "recipient": public.hex()})
    assert len(body) < int(headers.get("Content-Length", 0) or 10**9)
    with pytest.raises(SealError):
        n, chunks = open_sealed(io.BytesIO(body).read, private, JOB)
        b"".join(chunks)


def test_the_seal_needs_the_worker_key_and_a_proper_recipient(tmp_path, store, worker):
    _, public = new_box_key()
    plain = os.urandom(100)
    url = store.put("c.wav.enc", sealed(tmp_path, plain))
    body = {"url": url, "expect": expect(), "plain_sha256": hashlib.sha256(plain).hexdigest(),
            "job_id": JOB, "recipient": public.hex()}
    assert ask_seal(worker, body, key="wrong")[0] == 401
    assert ask_seal(worker, {**body, "recipient": "zz"})[0] == 400
    assert ask_seal(worker, {**body, "job_id": "../../etc"})[0] == 400


def test_the_agent_carries_the_same_sealing_code():
    agent_copy = HERE.parent.parent / "pipeline_agent" / "pipeline_seal.py"
    assert agent_copy.read_bytes() == (HERE / "pipeline_seal.py").read_bytes()
