"""
The worker's copy pass, end to end against a stand-in server and bucket.

The order in `SRS-ARC-09` is the whole point of these tests: compress, prove
the compression lossless, encrypt, upload, read back, record - and only then
may the pieces go. Each test here breaks one step and checks that the pieces
survive it.

    cd archive_worker && python -m pytest -q test_worker_copy.py
"""
from __future__ import annotations

import base64
import json
import math
import wave
from pathlib import Path
from typing import Any, Dict, Optional

import pytest

pytest.importorskip("soundfile")

import cloudcopy
import worker as worker_module
from worker import ArchiveWorker, Settings

KEY = bytes(range(32))
SESSION = "01JB8XQ4M7YZ2K9V3N5P6R8T0W"
STEM = "P0012345_DR0042_HOSP003_101432_102847_20260913"
RELPATH = f"HOSP003/DR0042/2026-09-13/{STEM}/{STEM}.wav"


# ============================================================
# A server and two buckets, in memory
# ============================================================

class Response:
    def __init__(self, payload: Any = None, *, status: int = 200, body: bytes = b""):
        self.status_code = status
        self._payload = payload if payload is not None else {}
        self.content = body

    def json(self) -> Any:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 300:
            raise RuntimeError(f"HTTP {self.status_code}")

    def iter_content(self, chunk_size: int = 1 << 20):
        for start in range(0, len(self.content), chunk_size):
            yield self.content[start:start + chunk_size]


class FakeBackend:
    """Answers the worker like the real server, and keeps what it was told."""

    def __init__(self):
        self.pending_copy = []
        self.pending_json = []
        self.document: Dict[str, Any] = {"file_stem": STEM, "patient": {"sex": "female"}}
        self.objects: Dict[str, bytes] = {}
        self.recorded = []
        self.failures = []
        self.pieces_deleted = 0
        self.versions: Dict[str, int] = {}
        self.truncate_uploads = False

    # ---- the worker's requests ----

    def get(self, url, **kw):
        if "/archive/copy/pending" in url:
            return Response({"sessions": self.pending_copy})
        if "/archive/json/pending" in url:
            return Response({"sessions": self.pending_json})
        if "/archive/clinical/" in url:
            return Response(self.document)
        if url.startswith("https://copies.example/get/"):
            key = url.split("/get/", 1)[1]
            if key not in self.objects:
                return Response(status=404)
            return Response(body=self.objects[key])
        raise AssertionError(f"unexpected GET {url}")

    def put(self, url, data=None, **kw):
        key = url.split("/put/", 1)[1]
        blob = data.read() if hasattr(data, "read") else (data or b"")
        self.objects[key] = blob[:-32] if self.truncate_uploads else blob
        return Response({})

    def post(self, url, json=None, **kw):
        body = json or {}
        if url.endswith("/archive/copy/authorize"):
            kind = body["kind"]
            version = self.versions.get(kind, 0) + 1
            self.versions[kind] = version
            key = f"copies/HOSP003/DR0042/2026-09-13/{STEM}.v{version}." + \
                  ("flac.enc" if kind == "audio" else "json.enc")
            return Response({"object_key": key, "version": version,
                             "upload_url": f"https://copies.example/put/{key}",
                             "download_url": f"https://copies.example/get/{key}"})
        if url.endswith("/archive/copy/complete"):
            self.recorded.append(body)
            deleted = 0
            if body.get("final", True):
                deleted, self.pieces_deleted = 3, 3
                self.pending_copy = [s for s in self.pending_copy
                                     if s["session_id"] != body["session_id"]]
            else:
                self.pending_json = [s for s in self.pending_json
                                     if s["session_id"] != body["session_id"]]
            return Response({"status": "copied", "objects_deleted": deleted})
        if url.endswith("/archive/copy/failed"):
            self.failures.append(body)
            return Response({"status": "recorded"})
        raise AssertionError(f"unexpected POST {url}")

    # ---- what the tests read ----

    def stored(self, kind: str) -> Optional[bytes]:
        keys = [k for k in self.objects if k.endswith(
            "flac.enc" if kind == "audio" else "json.enc")]
        return self.objects[sorted(keys)[-1]] if keys else None


# ============================================================
# Fixtures
# ============================================================

def a_recording(root: Path, *, seconds: float = 0.4) -> Path:
    """An archived consultation: the WAV and the JSON beside it."""
    folder = root / "HOSP003" / "DR0042" / "2026-09-13" / STEM
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{STEM}.wav"
    frames = int(seconds * 44100)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(44100)
        handle.writeframes(b"".join(
            int(7000 * math.sin(n / 24.0)).to_bytes(2, "little", signed=True)
            for n in range(frames)))
    path.with_suffix(".json").write_text(
        json.dumps({"file_stem": STEM, "prescription": None}), encoding="utf-8")
    return path


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("AIMS_BACKEND_URL", "https://backend.example")
    monkeypatch.setenv("AIMS_WORKER_KEY", "worker-key")
    monkeypatch.setenv("AIMS_ARCHIVE_ROOT", str(tmp_path))
    monkeypatch.setenv("AIMS_COPY_KEY", base64.b64encode(KEY).decode())

    audio = a_recording(tmp_path)
    backend = FakeBackend()
    backend.pending_copy.append({
        "session_id": SESSION, "file_stem": STEM, "archive_relpath": RELPATH,
        "archive_sha256": cloudcopy.sha256_file(audio),
        "archive_bytes": audio.stat().st_size})

    worker = ArchiveWorker(Settings())
    worker.session = backend
    return worker, backend, audio


# ============================================================
# The copy, in order
# ============================================================

def test_the_copy_is_made_and_only_then_are_the_pieces_released(setup, tmp_path):
    """AT-64: the FLAC and the JSON reach the copy bucket; the pieces follow."""
    worker, backend, audio = setup
    assert worker.copy_pass() == 1

    recorded = backend.recorded[0]
    assert recorded["session_id"] == SESSION and recorded["final"] is True
    assert {c["kind"] for c in recorded["copies"]} == {"audio", "json"}
    assert backend.pieces_deleted == 3
    assert backend.failures == []

    # What is in the bucket is the recording, and nothing else would do: decrypt
    # it, decode it, and compare the samples with the archived WAV (AT-74).
    sealed = tmp_path / "from_bucket.flac.enc"
    sealed.write_bytes(backend.stored("audio"))
    flac = cloudcopy.decrypt_file(sealed, tmp_path / "from_bucket.flac", KEY)
    assert cloudcopy.flac_matches_wav(flac, audio)

    audio_copy = next(c for c in recorded["copies"] if c["kind"] == "audio")
    assert audio_copy["sha256"] == cloudcopy.sha256_file(sealed)
    assert audio_copy["plain_sha256"] == cloudcopy.sha256_file(flac)


def test_the_uploaded_json_is_the_one_beside_the_audio(setup, tmp_path):
    worker, backend, audio = setup
    worker.copy_pass()

    sealed = tmp_path / "from_bucket.json.enc"
    sealed.write_bytes(backend.stored("json"))
    back = cloudcopy.decrypt_file(sealed, tmp_path / "back.json", KEY)
    assert json.loads(back.read_text(encoding="utf-8")) == \
        json.loads(audio.with_suffix(".json").read_text(encoding="utf-8"))


def test_a_missing_json_is_fetched_before_the_copy(setup):
    """An older recording, archived before the JSON was written beside it."""
    worker, backend, audio = setup
    audio.with_suffix(".json").unlink()

    worker.copy_pass()
    assert json.loads(audio.with_suffix(".json").read_text(encoding="utf-8")) \
        == backend.document
    assert backend.pieces_deleted == 3


# ============================================================
# Nothing is released on a doubt
# ============================================================

def test_a_flac_that_does_not_decode_to_the_audio_stops_everything(setup, monkeypatch):
    """AT-66: mismatch detected, pieces kept, alert raised."""
    worker, backend, _ = setup
    monkeypatch.setattr(cloudcopy, "flac_matches_wav", lambda flac, wav: False)

    assert worker.copy_pass() == 0
    assert backend.objects == {}                    # nothing was uploaded
    assert backend.recorded == []                   # nothing was recorded
    assert backend.pieces_deleted == 0              # nothing was deleted
    assert backend.failures[0]["step"] == "copy"


def test_a_short_upload_is_caught_on_the_way_back(setup):
    """The read-back is what proves the bucket really holds the copy."""
    worker, backend, _ = setup
    backend.truncate_uploads = True

    assert worker.copy_pass() == 0
    assert backend.recorded == []
    assert backend.pieces_deleted == 0
    assert "read back" in backend.failures[0]["message"]


def test_an_archive_file_that_changed_is_not_copied(setup):
    """The WAV must still be what was archived and receipted."""
    worker, backend, audio = setup
    with open(audio, "r+b") as handle:
        handle.seek(1000)
        handle.write(b"\x7f\x7f")

    assert worker.copy_pass() == 0
    assert backend.objects == {} and backend.pieces_deleted == 0


def test_a_missing_archive_file_is_reported_not_skipped(setup):
    worker, backend, audio = setup
    audio.unlink()

    assert worker.copy_pass() == 0
    assert backend.failures[0]["session_id"] == SESSION
    assert backend.pieces_deleted == 0


def test_without_a_key_nothing_is_uploaded_at_all(setup, monkeypatch):
    """No key means no copies - never patient audio in the clear."""
    worker, backend, _ = setup
    worker.settings.copy_key = None

    assert worker.copy_pass() == 0
    assert backend.objects == {} and backend.recorded == []


# ============================================================
# A prescription that arrives after archiving (SRS-ARC-13)
# ============================================================

def test_the_json_is_written_again_and_uploaded_as_a_new_object(setup, tmp_path):
    worker, backend, audio = setup
    worker.copy_pass()                              # the first copy
    first_json = backend.stored("json")

    backend.document = {"file_stem": STEM,
                        "prescription": {"version": 1, "items": [{"drug": "Amlodipine"}]}}
    backend.pending_json.append({"session_id": SESSION, "file_stem": STEM,
                                 "archive_relpath": RELPATH})

    assert worker.copy_pass() == 1
    # The file beside the audio now has the prescription in it.
    beside = json.loads(audio.with_suffix(".json").read_text(encoding="utf-8"))
    assert beside["prescription"]["items"][0]["drug"] == "Amlodipine"

    # A new object, the earlier one untouched, and no second deletion.
    rewrite = backend.recorded[-1]
    assert rewrite["final"] is False
    assert rewrite["copies"][0]["version"] == 2
    assert backend.stored("json") != first_json
    assert len([k for k in backend.objects if k.endswith("json.enc")]) == 2
