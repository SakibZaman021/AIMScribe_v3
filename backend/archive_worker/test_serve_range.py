"""
Listening on the portal: part of a recording, decrypted by this worker (serve.py /range).

The point of each test: whatever stretch is asked for, what comes back is
exactly those bytes of the archived file; only the chunks holding it are
fetched from the store; and a copy that is another recording's, or has been
changed, is refused rather than played.

    cd archive_worker && python -m pytest -q test_serve_range.py
"""
from __future__ import annotations

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
CHUNK = 1000


@pytest.fixture(autouse=True)
def small_chunks(monkeypatch):
    monkeypatch.setattr(cloudcopy, "CHUNK_BYTES", CHUNK)


def sealed(tmp_path: Path, plain: bytes, *, session_id=SID, part_no=None) -> bytes:
    source = tmp_path / f"{STEM}.wav"
    source.write_bytes(plain)
    out = cloudcopy.encrypt_file(source, tmp_path / f"{STEM}.wav.enc", KEY,
                                 cloudcopy.binding(session_id, "audio", part_no=part_no,
                                                   stem=STEM))
    return out.path.read_bytes()


def expect(**more):
    return {"session_id": SID, "kind": "audio",
            "stem_sha256": cloudcopy.stem_fingerprint(STEM), **more}


class RangedStore:
    """A stand-in for R2: honours `Range: bytes=a-b` and counts the bytes it sends."""

    def __init__(self) -> None:
        self.objects, self.sent, self.gets = {}, 0, 0
        store = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                body = store.objects.get(self.path.split("?", 1)[0])
                store.gets += 1
                if body is None:
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                ask = self.headers.get("Range", "")
                if ask.startswith("bytes="):
                    a, b = ask[6:].split("-")
                    a, b = int(a), min(int(b), len(body) - 1)
                    part = body[a:b + 1]
                    self.send_response(206)
                    self.send_header("Content-Range", f"bytes {a}-{b}/{len(body)}")
                else:
                    part = body
                    self.send_response(200)
                self.send_header("Content-Length", str(len(part)))
                self.end_headers()
                self.wfile.write(part)
                store.sent += len(part)

        self.http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.http.serve_forever, daemon=True).start()

    def put(self, name: str, body: bytes) -> str:
        self.objects["/" + name] = body
        return f"http://127.0.0.1:{self.http.server_address[1]}/{name}?sig=1"


@pytest.fixture()
def store():
    s = RangedStore()
    yield s
    s.http.shutdown()


@pytest.fixture()
def worker():
    server = serve.DecryptServer(("127.0.0.1", 0), key=KEY, worker_key=WORKER_KEY,
                                 concurrency=2, allow_http=True, listen_concurrency=2)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server
    server.shutdown()


def ask(worker, body, key=WORKER_KEY):
    request = urllib.request.Request(
        f"http://127.0.0.1:{worker.server_address[1]}/range",
        data=json.dumps(body).encode(), method="POST",
        headers={"X-Worker-Key": key, "Content-Type": "application/json"})
    try:
        response = urllib.request.urlopen(request, timeout=10)
    except urllib.error.HTTPError as err:
        return err.code, dict(err.headers), err.read()
    return response.status, dict(response.headers), response.read()


def test_any_stretch_is_exactly_those_bytes_of_the_file(tmp_path, store, worker):
    plain = os.urandom(4567)                       # five chunks, the last one short
    url = store.put("a.wav.enc", sealed(tmp_path, plain))
    for start, end in [(0, 0), (0, 99), (999, 1000), (1500, 2600), (4000, 4566),
                       (4566, None), (0, None), (2999, None)]:
        status, headers, body = ask(worker, {"url": url, "expect": expect(),
                                             "start": start, "end": end})
        assert status == 200, body
        assert int(headers["X-Plain-Size"]) == len(plain)
        assert int(headers["X-Start"]) == start
        assert body == plain[start:int(headers["X-End"]) + 1]
        if end is not None:
            assert int(headers["X-End"]) == min(end, start // CHUNK * CHUNK + 2 * CHUNK - 1)


def test_an_open_end_is_answered_with_at_most_two_chunks(tmp_path, store, worker):
    plain = os.urandom(9000)
    url = store.put("b.wav.enc", sealed(tmp_path, plain))
    status, headers, body = ask(worker, {"url": url, "expect": expect(), "start": 3500})
    assert status == 200
    assert int(headers["X-End"]) == 4999 and body == plain[3500:5000]


def test_a_jump_fetches_only_the_chunks_it_needs(tmp_path, store, worker):
    plain = os.urandom(20000)                      # twenty chunks
    url = store.put("c.wav.enc", sealed(tmp_path, plain))
    ask(worker, {"url": url, "expect": expect(), "start": 0, "end": 10})
    store.sent, store.gets = 0, 0
    status, _, body = ask(worker, {"url": url, "expect": expect(), "start": 15100, "end": 15200})
    assert status == 200 and body == plain[15100:15201]
    # The header is remembered: one ranged GET, one chunk and its overhead.
    assert store.gets == 1
    assert store.sent <= CHUNK + cloudcopy._CHUNK_OVERHEAD


def test_past_the_end_is_416_with_the_size(tmp_path, store, worker):
    plain = os.urandom(1200)
    url = store.put("d.wav.enc", sealed(tmp_path, plain))
    status, headers, _ = ask(worker, {"url": url, "expect": expect(), "start": 1200})
    assert status == 416 and int(headers["X-Plain-Size"]) == 1200


def test_another_recordings_copy_is_refused(tmp_path, store, worker):
    url = store.put("e.wav.enc", sealed(tmp_path, os.urandom(2500),
                                        session_id="01JOTHERSESSION0000000000"))
    status, _, body = ask(worker, {"url": url, "expect": expect(), "start": 0})
    assert status == 409 and b"session_id" in body


def test_another_patient_file_is_refused(tmp_path, store, worker):
    url = store.put("f.wav.enc", sealed(tmp_path, os.urandom(2500), part_no=2))
    assert ask(worker, {"url": url, "expect": expect(part_no=2), "start": 0})[0] == 200
    assert ask(worker, {"url": url, "expect": expect(part_no=1), "start": 0})[0] == 409


def test_a_changed_chunk_is_refused(tmp_path, store, worker):
    plain = os.urandom(3500)
    body = bytearray(sealed(tmp_path, plain))
    body[-500] ^= 1                                # inside the last chunk
    url = store.put("g.wav.enc", bytes(body))
    assert ask(worker, {"url": url, "expect": expect(), "start": 0, "end": 10})[0] == 200
    status, _, answer = ask(worker, {"url": url, "expect": expect(), "start": 3200})
    assert status == 409 and b"does not decrypt" in answer


def test_a_chunk_moved_to_another_place_does_not_decrypt(tmp_path, store, worker):
    plain = os.urandom(3000)
    body = sealed(tmp_path, plain)
    where = cloudcopy.layout(body[:cloudcopy.HEADER_MAX_BYTES], "x", len(body))
    a0, b0 = where.span(0, 0)
    a1, b1 = where.span(1, 1)
    swapped = body[:a0] + body[a1:b1] + body[a0:b0] + body[b1:]
    url = store.put("h.wav.enc", swapped)
    status, _, answer = ask(worker, {"url": url, "expect": expect(), "start": 0, "end": 10})
    assert status == 409


def test_only_the_worker_key_is_answered(tmp_path, store, worker):
    url = store.put("i.wav.enc", sealed(tmp_path, os.urandom(100)))
    assert ask(worker, {"url": url, "expect": expect(), "start": 0}, key="wrong")[0] == 401


def test_bad_requests_are_refused(tmp_path, store, worker):
    url = store.put("j.wav.enc", sealed(tmp_path, os.urandom(100)))
    for bad in [{"url": url, "expect": expect()},
                {"url": url, "expect": expect(), "start": -1},
                {"url": url, "expect": expect(), "start": 50, "end": 10},
                {"url": "ftp://x", "expect": expect(), "start": 0},
                {"url": url, "expect": {}, "start": 0}]:
        assert ask(worker, bad)[0] == 400, bad
