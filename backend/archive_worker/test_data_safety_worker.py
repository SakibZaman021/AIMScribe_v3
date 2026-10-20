"""
The worker's side of the data-safety review.

  - it tells the server exactly which pieces it joined
  - a file already on disk is "ours" only if its manifest says so, not by size
  - a recording the patient refused after archiving leaves no file behind

    cd archive_worker && python -m pytest -q test_data_safety_worker.py
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

import pytest

import archive
from worker import ArchiveWorker, Settings

SESSION = "01JB8XQ4M7YZ2K9V3N5P6R8T0W"
STEM = "P0012345_DR0042_HOSP003_101432_102847_20260913"
RELPATH = f"HOSP003/DR0042/2026-09-13/{STEM}/{STEM}.wav"


class Response:
    def __init__(self, body: Dict[str, Any], status: int = 200):
        self._body, self.status_code = body, status

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class Backend:
    def __init__(self):
        self.posts: List[Dict[str, Any]] = []
        self.tasks: List[Dict[str, Any]] = []

    def get(self, url, **kw):
        if url.endswith("/archive/erase/pending"):
            return Response({"tasks": self.tasks})
        raise AssertionError(f"unexpected GET {url}")

    def post(self, url, json=None, **kw):
        self.posts.append({"url": url, "body": json})
        if url.endswith("/archive/complete"):
            return Response({"status": "archived", "receipts_issued": 1})
        if url.endswith("/archive/erase/complete"):
            self.tasks = []
            return Response({"status": "erased"})
        raise AssertionError(f"unexpected POST {url}")


@pytest.fixture
def worker(tmp_path, monkeypatch):
    monkeypatch.setenv("AIMS_BACKEND_URL", "https://backend.example")
    monkeypatch.setenv("AIMS_WORKER_KEY", "worker-key")
    monkeypatch.setenv("AIMS_ARCHIVE_ROOT", str(tmp_path))
    w = ArchiveWorker(Settings())
    w.session = Backend()
    return w


def write_recording(root: Path, *, session_id=SESSION, segments=((1, "aa" * 32),)) -> Path:
    folder = root / "HOSP003" / "DR0042" / "2026-09-13" / STEM
    folder.mkdir(parents=True, exist_ok=True)
    audio = folder / f"{STEM}.wav"
    audio.write_bytes(b"RIFF" + b"\0" * 640)
    audio.with_suffix(".json").write_text('{"patient": "P0012345"}', encoding="utf-8")
    audio.with_suffix(".manifest.json").write_text(json.dumps({
        "session_id": session_id, "patient_ref": "P0012345",
        "segments": [{"seq_no": n, "sha256": h} for n, h in segments],
        "archive": {"filename": audio.name}}), encoding="utf-8")
    archive.update_day_index(folder)
    return audio


# ---------------------------------------------------------------- the pieces joined

def test_the_report_names_the_pieces_joined(worker):
    worker.report_complete(SESSION, RELPATH, b"\x01" * 32, 644,
                           segments=[{"seq_no": 1, "sha256": "aa" * 32},
                                     {"seq_no": 2, "sha256": "bb" * 32}])
    body = worker.session.posts[-1]["body"]
    assert body["segments"] == [{"seq_no": 1, "sha256": "aa" * 32},
                                {"seq_no": 2, "sha256": "bb" * 32}]


# ---------------------------------------------------------------- whose file is it

def test_a_file_of_the_same_size_from_another_recording_is_not_ours(tmp_path):
    """Before: equal size alone made it 'ours', and it was re-reported as this one."""
    audio = write_recording(tmp_path, session_id="01JB8XQ4M7YZ2K9V3N5P6R8TZZ")
    path, ours = archive.free_destination(audio.parent, audio.name, audio.stat().st_size,
                                          session_id=SESSION,
                                          segment_hashes=[(1, "aa" * 32)])
    assert not ours and path.name != audio.name


def test_our_interrupted_file_is_reused(tmp_path):
    audio = write_recording(tmp_path)
    path, ours = archive.free_destination(audio.parent, audio.name, audio.stat().st_size,
                                          session_id=SESSION,
                                          segment_hashes=[(1, "aa" * 32)])
    assert ours and path == audio


def test_our_file_with_other_pieces_is_not_reused(tmp_path):
    audio = write_recording(tmp_path)
    _, ours = archive.free_destination(audio.parent, audio.name, audio.stat().st_size,
                                       session_id=SESSION,
                                       segment_hashes=[(1, "aa" * 32), (2, "bb" * 32)])
    assert not ours


def test_a_file_with_no_manifest_is_never_assumed_ours(tmp_path):
    audio = write_recording(tmp_path)
    audio.with_suffix(".manifest.json").unlink()
    _, ours = archive.free_destination(audio.parent, audio.name, audio.stat().st_size,
                                       session_id=SESSION)
    assert not ours


# ---------------------------------------------------------------- erased after archiving

def test_an_erased_recording_leaves_no_file_and_no_index(tmp_path):
    audio = write_recording(tmp_path)
    removed = archive.erase_recording_files(audio)
    assert {p.name for p in removed} == {audio.name, audio.with_suffix(".json").name,
                                         audio.with_suffix(".manifest.json").name}
    assert not audio.parent.exists()


def test_the_erase_pass_removes_the_files_and_tells_the_server(worker, tmp_path):
    audio = write_recording(tmp_path)
    worker.session.tasks = [{"session_id": SESSION, "files": [RELPATH]}]
    assert worker.erase_pass() == 1
    assert not audio.exists() and not audio.with_suffix(".json").exists()
    done = [p for p in worker.session.posts if p["url"].endswith("/archive/erase/complete")]
    assert done and done[0]["body"]["session_id"] == SESSION


def test_an_erase_task_outside_the_archive_is_refused(worker, tmp_path):
    outside = tmp_path.parent / "elsewhere.wav"
    outside.write_bytes(b"x")
    worker.session.tasks = [{"session_id": SESSION, "files": ["../elsewhere.wav"]}]
    assert worker.erase_pass() == 0
    assert outside.exists()
