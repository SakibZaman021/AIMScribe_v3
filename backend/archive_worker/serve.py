"""
One cloud copy, decrypted for a download from the portal.

The copy key lives in this worker and nowhere else (SRS-DAT-08); the bucket
credentials live in the API. A download needs both, so neither moves: the API
checks who is asking and logs it, then sends this worker a five-minute link to
the one sealed object. The worker reads it from the store, decrypts it chunk
by chunk and streams the WAV back. Nothing decrypted is written to any disk.

    POST /decrypt          X-Worker-Key: <AIMS_WORKER_KEY>
        {"url": "<the object, presigned>", "expect": {<its binding>},
         "plain_sha256": "<the archived file's>"}
    -> 200, Content-Length: the WAV's size, then the WAV
    -> 409 the copy is not the one asked for, or does not decrypt
    -> 429 already decrypting as many as allowed at once

Every chunk is authenticated as it is decrypted, and the binding is checked
before the first: a copy renamed onto another recording's key is refused
before a byte leaves. The last chunk is held back until the whole file's
SHA-256 is the one recorded when the copy was made, so a download that
completes is the archived file, byte for byte; one that does not is cut
short, and the browser shows it failed.

Listens on the internal network only; the port is not published.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional

import requests

import cloudcopy
from cloudcopy import CopyError

logger = logging.getLogger("archive_worker.serve")

MAX_REQUEST_BYTES = 16 * 1024
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


class _Exact:
    """A stream whose read(n) returns n bytes unless the stream has ended."""

    def __init__(self, raw) -> None:
        self.raw = raw

    def read(self, n: int) -> bytes:
        parts, left = [], n
        while left > 0:
            block = self.raw.read(left)
            if not block:
                break
            parts.append(block)
            left -= len(block)
        return b"".join(parts)


class _Handler(BaseHTTPRequestHandler):
    server_version = "aims-decrypt"
    protocol_version = "HTTP/1.1"
    server: "DecryptServer"

    def log_message(self, fmt: str, *args: Any) -> None:    # no paths in the log
        logger.debug("decrypt: " + fmt, *args)

    def _answer(self, status: int, message: str) -> None:
        body = json.dumps({"detail": message}).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path == "/health":
            self._answer(200, "ok")
        else:
            self._answer(404, "not found")

    def do_POST(self) -> None:
        if self.path != "/decrypt":
            self._answer(404, "not found")
            return
        given = (self.headers.get("X-Worker-Key") or "").encode("utf-8")
        if not self.server.worker_key or not hmac.compare_digest(given, self.server.worker_key):
            self._answer(401, "the worker key is required")
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if not 0 < length <= MAX_REQUEST_BYTES:
            self._answer(400, "a small JSON body is required")
            return
        try:
            ask = json.loads(self.rfile.read(length).decode("utf-8"))
            url, expect, want = ask["url"], ask["expect"], ask["plain_sha256"]
        except (ValueError, KeyError, TypeError, UnicodeDecodeError):
            self._answer(400, "url, expect and plain_sha256 are required")
            return
        if not isinstance(url, str) or not url.startswith(self.server.schemes) \
                or not isinstance(expect, dict) or not expect.get("session_id") \
                or not isinstance(want, str) or not _HEX64.match(want):
            self._answer(400, "url, expect and plain_sha256 are not valid")
            return
        if not self.server.slots.acquire(blocking=False):
            self._answer(429, "already decrypting as many downloads as allowed; try again")
            return
        try:
            self._stream(url, expect, want)
        finally:
            self.server.slots.release()

    def _stream(self, url: str, expect: Dict[str, Any], want: str) -> None:
        name = f"the copy of {expect.get('session_id')}"
        try:
            upstream = self.server.http.get(url, stream=True, timeout=(10, 120),
                                            verify=self.server.verify_tls)
        except requests.RequestException as exc:
            self._answer(502, f"the store could not be reached: {type(exc).__name__}")
            return
        try:
            if upstream.status_code != 200:
                self._answer(502, f"the store answered {upstream.status_code}")
                return
            try:
                total = int(upstream.headers.get("Content-Length", ""))
            except ValueError:
                self._answer(502, "the store did not say how large the copy is")
                return
            try:
                size, chunks = cloudcopy.open_sealed(_Exact(upstream.raw), name,
                                                     self.server.key, expect, total)
                held = next(chunks)          # the first chunk decrypts before any 200
            except CopyError as exc:
                logger.error("Download refused: %s", exc)
                self._answer(409, str(exc))
                return

            self.send_response(200)
            self.send_header("Content-Type", "audio/wav")
            self.send_header("Content-Length", str(size))
            self.end_headers()
            digest = hashlib.sha256()
            try:
                for chunk in chunks:
                    digest.update(held)
                    self.wfile.write(held)
                    held = chunk
                digest.update(held)
                if digest.hexdigest() != want:
                    # Authenticated, yet not the archived file: never complete it.
                    logger.error("Download of %s cut short: it decrypts, but not to "
                                 "the archived file", expect.get("session_id"))
                    self.close_connection = True
                    return
                self.wfile.write(held)
                logger.info("Decrypted %s for a download (%.1f MB)",
                            expect.get("session_id"), (size or 0) / 1024 ** 2)
            except CopyError as exc:
                logger.error("Download of %s cut short: %s", expect.get("session_id"), exc)
                self.close_connection = True
            except OSError as exc:           # the API, or the browser, went away
                logger.warning("Download of %s stopped: %s", expect.get("session_id"), exc)
                self.close_connection = True
        finally:
            upstream.close()


class DecryptServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, *, key: bytes, worker_key: str, concurrency: int = 2,
                 verify_tls: bool = True, allow_http: bool = False,
                 http: Optional[requests.Session] = None) -> None:
        super().__init__(address, _Handler)
        self.key = key
        self.worker_key = worker_key.encode("utf-8")
        self.slots = threading.BoundedSemaphore(max(1, concurrency))
        self.verify_tls = verify_tls
        # The store is reached over https; plain http only in tests.
        self.schemes = ("https://", "http://") if allow_http else ("https://",)
        self.http = http or requests.Session()


def start(settings) -> Optional[DecryptServer]:
    """Serve downloads in the background, when there is a key and a port."""
    if settings.copy_key is None or settings.decrypt_port <= 0:
        return None
    server = DecryptServer(("0.0.0.0", settings.decrypt_port), key=settings.copy_key,
                           worker_key=settings.worker_key,
                           concurrency=settings.decrypt_concurrency,
                           verify_tls=settings.verify_tls)
    threading.Thread(target=server.serve_forever, name="decrypt", daemon=True).start()
    return server


__all__ = ["DecryptServer", "start"]
