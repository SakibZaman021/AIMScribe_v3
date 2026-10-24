"""
A portal download, decrypted by this worker (serve.py).

The point of each test: what reaches the API is the archived file, byte for
byte, or it is refused before a byte leaves, or it is cut short so the
browser shows a failed download. Never something else that looks complete.

    cd archive_worker && python -m pytest -q test_serve.py
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

import cloudcopy
import serve

KEY = bytes(range(32))
WORKER_KEY = "the-worker-key"
SID = "01JABCDEFGHJKMNPQRSTVWXYZ0"
STEM = "P001_DR0042_HOSP003_101432_102847_20260913"


@pytest.fixture(autouse=True)
def small_chunks(monkeypatch):
    """Several chunks out of a small file, so the chunk logic is exercised."""
    monkeypatch.setattr(cloudcopy, "CHUNK_BYTES", 1000)


def sealed(tmp_path: Path, plain: bytes, *, session_id=SID, stem=STEM, part_no=None):
    source = tmp_path / f"{stem}.wav"
    source.write_bytes(plain)
    out = cloudcopy.encrypt_file(source, tmp_path / f"{stem}.wav.enc", KEY,
                                 cloudcopy.binding(session_id, "audio", part_no=part_no,
                                                   stem=stem))
    return out.path.read_bytes()


def expect(stem=STEM, **more):
    return {"session_id": SID, "kind": "audio",
            "stem_sha256": cloudcopy.stem_fingerprint(stem), **more}


class Store:
    """A stand-in for the cloud store: serves whatever object it is given."""

    def __init__(self) -> None:
        self.objects = {}
        store = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                body = store.objects.get(self.path)
                if body is None:
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.http.serve_forever, daemon=True).start()

    def put(self, name: str, body: bytes) -> str:
        self.objects["/" + name] = body
        return f"http://127.0.0.1:{self.http.server_address[1]}/{name}"


@pytest.fixture()
def store():
    s = Store()
    yield s
    s.http.shutdown()


@pytest.fixture()
def worker():
    server = serve.DecryptServer(("127.0.0.1", 0), key=KEY, worker_key=WORKER_KEY,
                                 concurrency=2, allow_http=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server
    server.shutdown()


def ask(worker, body, key=WORKER_KEY):
    """POST /decrypt; returns (status, headers, body) even when it is cut short."""
    request = urllib.request.Request(
        f"http://127.0.0.1:{worker.server_address[1]}/decrypt",
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
    except Exception:                      # http.client.IncompleteRead and friends
        pass
    return response.status, dict(response.headers), data


def test_the_size_of_a_sealed_body_gives_the_plaintexts():
    per = cloudcopy._CHUNK_OVERHEAD
    for plain in (0, 1, 999, 1000, 1001, 2000, 2500):
        chunks = max(1, -(-plain // 1000))
        assert cloudcopy.plain_size(plain + chunks * per, 1000) == plain


def test_a_download_is_the_archived_file_byte_for_byte(tmp_path, store, worker):
    plain = os.urandom(3456)
    url = store.put("a.wav.enc", sealed(tmp_path, plain))
    status, headers, body = ask(worker, {"url": url, "expect": expect(),
                                         "plain_sha256": hashlib.sha256(plain).hexdigest()})
    assert status == 200
    assert int(headers["Content-Length"]) == len(plain)
    assert body == plain


def test_a_patient_file_of_a_recording_is_checked_by_its_part(tmp_path, store, worker):
    plain = os.urandom(1500)
    url = store.put("p2.wav.enc", sealed(tmp_path, plain, part_no=2))
    good = {"url": url, "expect": expect(part_no=2),
            "plain_sha256": hashlib.sha256(plain).hexdigest()}
    assert ask(worker, good)[2] == plain
    status, _, body = ask(worker, {**good, "expect": expect(part_no=1)})
    assert status == 409 and b"part_no" in body


def test_another_recordings_copy_is_refused_before_a_byte_leaves(tmp_path, store, worker):
    plain = os.urandom(2500)
    url = store.put("other.wav.enc", sealed(tmp_path, plain, session_id="01JOTHERSESSION0000000000"))
    status, _, body = ask(worker, {"url": url, "expect": expect(),
                                   "plain_sha256": hashlib.sha256(plain).hexdigest()})
    assert status == 409
    assert plain[:100] not in body


def test_a_changed_copy_never_completes(tmp_path, store, worker):
    plain = os.urandom(3500)
    object_ = bytearray(sealed(tmp_path, plain))
    object_[-40] ^= 0x01                                    # inside the last chunk
    url = store.put("changed.wav.enc", bytes(object_))
    status, headers, body = ask(worker, {"url": url, "expect": expect(),
                                         "plain_sha256": hashlib.sha256(plain).hexdigest()})
    assert status != 200 or len(body) < int(headers["Content-Length"])


def test_a_copy_cut_short_never_completes(tmp_path, store, worker):
    plain = os.urandom(3500)
    whole = sealed(tmp_path, plain)
    url = store.put("short.wav.enc", whole[:len(whole) - 1100])   # the last chunk gone
    status, headers, body = ask(worker, {"url": url, "expect": expect(),
                                         "plain_sha256": hashlib.sha256(plain).hexdigest()})
    assert status != 200 or len(body) < int(headers["Content-Length"])


def test_a_file_that_is_not_the_archived_one_is_cut_short(tmp_path, store, worker):
    """It decrypts and is bound right, but its fingerprint is not the archive's."""
    plain = os.urandom(3500)
    url = store.put("wrong.wav.enc", sealed(tmp_path, plain))
    status, headers, body = ask(worker, {"url": url, "expect": expect(),
                                         "plain_sha256": "0" * 64})
    assert status == 200
    assert len(body) < int(headers["Content-Length"])
    assert len(body) <= len(plain) - 500                    # the last chunk held back


def test_only_the_api_with_the_worker_key_is_answered(tmp_path, store, worker):
    plain = os.urandom(100)
    url = store.put("k.wav.enc", sealed(tmp_path, plain))
    body = {"url": url, "expect": expect(), "plain_sha256": hashlib.sha256(plain).hexdigest()}
    assert ask(worker, body, key="wrong")[0] == 401
    assert ask(worker, body, key="")[0] == 401
    assert ask(worker, {**body, "expect": {}})[0] == 400


def test_the_store_is_reached_over_https_only():
    server = serve.DecryptServer(("127.0.0.1", 0), key=KEY, worker_key=WORKER_KEY)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        status, _, _ = ask(server, {"url": "http://example.invalid/x", "expect": expect(),
                                    "plain_sha256": "0" * 64})
        assert status == 400
    finally:
        server.shutdown()


def test_no_key_or_no_port_means_no_listener():
    class S:
        copy_key, decrypt_port = None, 7070
    assert serve.start(S) is None
    S.copy_key, S.decrypt_port = KEY, 0
    assert serve.start(S) is None
