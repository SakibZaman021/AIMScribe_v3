"""
One recording, several consultations: cut into one file per patient.

A follow-up with no prescription, then the next patient, is one recording on the
doctor's PC. The server says where each consultation begins; the worker cuts the
joined audio there. These tests hold the worker to the promises that make that
safe: the cut lands on the exact frame, the parts are every frame exactly once
(joined again they are the recording, byte for byte), an interrupted run
finishes, a recut proves it is cutting the audio the chain describes and refuses
otherwise, and the copy and the local release treat the parts as one recording.

    cd archive_worker && python -m pytest -q test_parts.py
"""
from __future__ import annotations

import base64
import hashlib
import json
import wave
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List

import pytest

import archive
import restore
from archive import ArchiveError
from worker import ArchiveWorker, Settings

KEY = bytes(range(32))
RATE = 8000
SESSION = "01JB8XQ4M7YZ2K9V3N5P6R8T0W"
STEMS = ["P0000001_DR0042_HOSP003_101400_102000_20260913",
         "P0000002_DR0042_HOSP003_102000_102700_20260913",
         "P0000003_DR0042_HOSP003_102700_103000_20260913"]


# ============================================================
# A server and a segment bucket, in memory
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


class Backend:
    """The server's side of /archive/*, keeping everything it was told."""

    def __init__(self):
        self.blobs: Dict[str, bytes] = {}
        self.completed: List[Dict[str, Any]] = []
        self.clinical_asks: List[Any] = []
        self.recut: List[Dict[str, Any]] = []
        self.copy_pending: List[Dict[str, Any]] = []
        self.authorized: List[Dict[str, Any]] = []
        self.copies: List[Dict[str, Any]] = []
        self.objects: Dict[str, bytes] = {}
        self.failures: List[Dict[str, Any]] = []
        self.fail_complete = 0

    def get(self, url, params=None, **kw):
        if url.startswith("https://bucket.example/"):
            return Response(body=self.blobs[url])
        if "/archive/clinical/" in url:
            part = (params or {}).get("part")
            self.clinical_asks.append(part)
            return Response({"file_stem": STEMS[(part or 1) - 1], "patient": {"part": part},
                             "recording": {"derived_from": {"rule": "server-says"}}})
        if "/archive/recut/pending" in url:
            return Response({"sessions": self.recut})
        if "/archive/copy/pending" in url:
            return Response({"sessions": self.copy_pending})
        if "/archive/json/pending" in url:
            return Response({"sessions": []})
        raise AssertionError(f"unexpected GET {url}")

    def put(self, url, data=None, **kw):
        self.objects[url.split("/put/", 1)[1]] = data.read() if hasattr(data, "read") else data
        return Response({})

    def post(self, url, json=None, **kw):
        body = json or {}
        if url.endswith("/archive/complete"):
            if self.fail_complete:
                self.fail_complete -= 1
                return Response({"detail": "database away"}, status=503)
            self.completed.append(body)
            return Response({"status": "archived", "receipts_issued": 3})
        if url.endswith("/archive/copy/authorize"):
            self.authorized.append(body)
            n = len(self.authorized)
            key = f"copies/{body['session_id']}/p{body.get('part_no')}.{n}." + \
                  ("wav.enc" if body["kind"] == "audio" else "json.enc")
            return Response({"object_key": key, "version": n,
                             "upload_url": f"https://copies.example/put/{key}"})
        if url.endswith("/archive/copy/complete"):
            self.copies.append(body)
            return Response({"status": "copied",
                             "objects_deleted": 3 if body.get("final") else 0})
        if url.endswith("/archive/copy/failed"):
            self.failures.append(body)
            return Response({"status": "recorded"})
        raise AssertionError(f"unexpected POST {url}")


# ============================================================
# Fixtures
# ============================================================

def pcm(start: int, frames: int) -> bytes:
    """A ramp: every frame a different value, so a cut one frame out shows."""
    return b"".join(((start + n) % 30000).to_bytes(2, "little", signed=True)
                    for n in range(frames))


def segment_wav(path: Path, start: int, frames: int) -> Path:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(pcm(start, frames))
    return path


def frames_of(path: Path) -> bytes:
    with wave.open(str(path), "rb") as w:
        return w.readframes(w.getnframes())


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("AIMS_BACKEND_URL", "https://backend.example")
    monkeypatch.setenv("AIMS_WORKER_KEY", "worker-key")
    monkeypatch.setenv("AIMS_ARCHIVE_ROOT", str(tmp_path / "archive"))
    monkeypatch.setenv("AIMS_COPY_KEY", base64.b64encode(KEY).decode())
    monkeypatch.setenv("AIMS_DISK_HEADROOM_BYTES", "0")

    backend = Backend()
    segments = []
    for n in range(3):                               # 3 clips of 1 s = 24000 frames
        path = segment_wav(tmp_path / f"seg{n}.wav", n * RATE, RATE)
        url = f"https://bucket.example/seg{n}"
        backend.blobs[url] = path.read_bytes()
        segments.append({"seq_no": n + 1, "download_url": url, "bytes": path.stat().st_size,
                         "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                         "duration_seconds": 1.0})
    worker = ArchiveWorker(Settings())
    worker.session = backend
    return worker, backend, segments


def a_session(segments, parts):
    return {
        "session_id": SESSION, "hospital_id": "HOSP003", "doctor_id": "DR0042",
        "patient_ref": "P0000001", "session_date": "2026-09-13",
        "opened_at": "2026-09-13T04:14:00+00:00", "closed_at": "2026-09-13T04:30:00+00:00",
        "timezone": "Asia/Dhaka", "file_stem": STEMS[0],
        "audio": {"sample_rate": RATE, "channels": 1, "sample_width": 2},
        "segments": segments, "parts": parts,
    }


THREE = [
    {"part_no": 1, "start_seconds": 0.0, "end_seconds": 1.25, "file_stem": STEMS[0],
     "patient_id": "P0000001", "doctor_id": "DR0042", "rule": "R4", "needs_review": False},
    {"part_no": 2, "start_seconds": 1.25, "end_seconds": 2.4, "file_stem": STEMS[1],
     "patient_id": "P0000002", "doctor_id": "DR0042", "rule": "R1", "needs_review": False},
    {"part_no": 3, "start_seconds": 2.4, "end_seconds": None, "file_stem": STEMS[2],
     "patient_id": "P0000003", "doctor_id": "DR0042", "rule": "R5", "needs_review": True},
]


def part_path(worker, stem: str) -> Path:
    return worker.settings.archive_root / "HOSP003" / "DR0042" / "2026-09-13" / stem / f"{stem}.wav"


# ============================================================
# The cut
# ============================================================

def test_cut_points_are_exact_frames_from_the_starts_only():
    assert archive.part_frames([0.0, 1.25, 2.4], RATE, 24000) == \
        [(0, 10000), (10000, 19200), (19200, 24000)]
    # The first part starts at frame 0 and the last ends at the last frame,
    # whatever the seconds say: nothing can fall outside a part.
    assert archive.part_frames([0.7], RATE, 24000) == [(0, 24000)]
    for bad in ([0.0, 2.0, 1.0], [0.0, 3.0], [0.0, 0.0], [0.0, "x"]):
        with pytest.raises(ArchiveError):
            archive.part_frames(bad, RATE, 24000)


def test_three_consultations_become_three_files_that_are_the_recording(setup):
    worker, backend, segments = setup
    worker.archive_session(a_session(segments, THREE))

    paths = [part_path(worker, stem) for stem in STEMS]
    whole = pcm(0, 24000)
    assert [frames_of(p) for p in paths] == [whole[:20000], whole[20000:38400], whole[38400:]]

    report = backend.completed[-1]
    joined = restore.join_parts(paths, worker.settings.archive_root / "whole.wav")
    assert joined.sha256.hex() == report["sha256"]          # the chain's fingerprint
    assert report["bytes"] == joined.bytes
    assert report["archive_relpath"].endswith(f"{STEMS[0]}.wav")
    assert [(q["part_no"], q["start_seconds"], q["end_seconds"]) for q in report["parts"]] == \
        [(1, 0.0, 1.25), (2, 1.25, 2.4), (3, 2.4, 3.0)]
    for q, path in zip(report["parts"], paths):
        assert q["sha256"] == archive.sha256_file(path).hex()

    # Each consultation's own JSON, asked for by part, saying where it came from.
    assert backend.clinical_asks == [1, 2, 3]
    doc = json.loads(paths[1].with_suffix(".json").read_text(encoding="utf-8"))
    derived = doc["recording"]["derived_from"]
    assert derived["rule"] == "server-says"                  # the server's word wins
    assert (derived["start_frame"], derived["end_frame"]) == (10000, 19200)
    assert derived["recording_sha256"] == report["sha256"]
    manifest = json.loads(paths[2].with_suffix(".manifest.json").read_text(encoding="utf-8"))
    assert manifest["patient_ref"] == "P0000003" and manifest["derived_from"]["needs_review"]

    assert [r["patient_id"] for r in worker.catalogue.parts_for(SESSION)] == \
        ["P0000001", "P0000002", "P0000003"]
    assert worker.catalogue.session_row(SESSION)["parts_count"] == 3


def test_a_single_consultation_is_archived_exactly_as_before(setup):
    worker, backend, segments = setup
    worker.archive_session(a_session(segments, [THREE[0]]))
    report = backend.completed[-1]
    assert "parts" not in report
    assert part_path(worker, STEMS[0]).exists()
    assert frames_of(part_path(worker, STEMS[0])) == pcm(0, 24000)
    assert backend.clinical_asks == [None]
    assert worker.catalogue.parts_for(SESSION) == []


def test_an_interrupted_run_finishes_on_the_next_pass(setup):
    worker, backend, segments = setup
    backend.fail_complete = 1
    with pytest.raises(RuntimeError):
        worker.archive_session(a_session(segments, THREE))
    assert backend.completed == []                           # still pending

    worker.archive_session(a_session(segments, THREE))
    assert len(backend.completed) == 1
    for stem in STEMS:                                       # the same files, no _02
        folder = part_path(worker, stem).parent
        assert sorted(p.name for p in folder.glob("*.wav")) == [f"{stem}.wav"]


def test_an_unsafe_part_name_is_refused_before_anything_is_written(setup):
    worker, backend, segments = setup
    bad = [THREE[0], {**THREE[1], "file_stem": "../../escape"}]
    with pytest.raises(ArchiveError):
        worker.archive_session(a_session(segments, bad))
    assert backend.completed == []
    assert not (worker.settings.archive_root / "HOSP003").exists()


# ============================================================
# Recut: the server moved where a consultation begins
# ============================================================

def recut_of(worker, backend, parts):
    report = backend.completed[-1]
    return {**a_session([], parts), "archive_sha256": report["sha256"],
            "archive_relpath": report["archive_relpath"],
            "existing_parts": [{"part_no": q["part_no"], "archive_relpath": q["archive_relpath"]}
                               for q in report["parts"]]}


TWO = [
    {**THREE[0], "end_seconds": 2.0},
    {**THREE[1], "part_no": 2, "start_seconds": 2.0, "end_seconds": None,
     "rule": "R1", "needs_review": False},
]


def test_a_recut_moves_the_boundary_and_tidies_the_old_files(setup):
    worker, backend, segments = setup
    worker.archive_session(a_session(segments, THREE))
    first = backend.completed[-1]["sha256"]

    backend.recut = [recut_of(worker, backend, TWO)]
    assert worker.recut_pass() == 1

    report = backend.completed[-1]
    assert report["sha256"] == first                         # still the same recording
    assert [q["start_seconds"] for q in report["parts"]] == [0.0, 2.0]
    whole = pcm(0, 24000)
    assert frames_of(part_path(worker, STEMS[0])) == whole[:32000]
    assert frames_of(part_path(worker, STEMS[1])) == whole[32000:]
    assert not part_path(worker, STEMS[2]).parent.exists()   # the third is gone
    assert len(worker.catalogue.parts_for(SESSION)) == 2


def test_a_recut_never_cuts_audio_that_is_not_the_recording(setup):
    worker, backend, segments = setup
    worker.archive_session(a_session(segments, THREE))
    damaged = part_path(worker, STEMS[1])
    data = bytearray(damaged.read_bytes())
    data[100] ^= 0xFF
    damaged.write_bytes(bytes(data))
    before = {stem: part_path(worker, stem).read_bytes() for stem in STEMS}

    backend.recut = [recut_of(worker, backend, TWO)]
    assert worker.recut_pass() == 0
    assert backend.failures[-1]["step"] == "recut"
    assert len(backend.completed) == 1                       # nothing reported
    assert {stem: part_path(worker, stem).read_bytes() for stem in STEMS} == before


def test_a_recut_interrupted_after_writing_finishes_from_the_new_files(setup):
    worker, backend, segments = setup
    worker.archive_session(a_session(segments, THREE))
    recut = recut_of(worker, backend, TWO)

    backend.fail_complete = 1                                # parts written, report lost
    backend.recut = [recut]
    assert worker.recut_pass() == 0
    # The first part was overwritten with its new length, so the old set no
    # longer joins to the recording; the new set does, and is used.
    assert worker.recut_pass() == 1
    assert backend.completed[-1]["sha256"] == backend.completed[0]["sha256"]
    assert [q["start_seconds"] for q in backend.completed[-1]["parts"]] == [0.0, 2.0]


def test_a_recut_of_released_audio_asks_for_a_restore(setup):
    worker, backend, segments = setup
    worker.archive_session(a_session(segments, THREE))
    part_path(worker, STEMS[2]).unlink()
    backend.recut = [recut_of(worker, backend, TWO)]
    assert worker.recut_pass() == 0
    assert "restore" in backend.failures[-1]["message"]


# ============================================================
# Copy and release, part by part
# ============================================================

def later(days: float) -> datetime:
    return datetime.now(timezone.utc) + timedelta(days=days)


def test_each_part_is_copied_and_only_the_last_releases_the_pieces(setup):
    worker, backend, segments = setup
    worker.archive_session(a_session(segments, THREE))
    report = backend.completed[-1]
    backend.copy_pending = [{"session_id": SESSION, "archive_relpath": report["archive_relpath"],
                             "archive_sha256": report["sha256"]}]

    assert worker.copy_pass() == 1
    assert [(a["kind"], a["part_no"]) for a in backend.authorized] == \
        [("audio", 1), ("json", 1), ("audio", 2), ("json", 2), ("audio", 3), ("json", 3)]
    assert [(c["part_no"], c["final"]) for c in backend.copies] == \
        [(1, False), (2, False), (3, True)]
    assert all(r["copied_at"] for r in worker.catalogue.parts_for(SESSION))
    assert worker.catalogue.session_row(SESSION)["copied_at"]


def test_a_failed_part_copy_never_releases_the_pieces(setup, monkeypatch):
    worker, backend, segments = setup
    worker.archive_session(a_session(segments, THREE))
    report = backend.completed[-1]
    backend.copy_pending = [{"session_id": SESSION, "archive_relpath": report["archive_relpath"],
                             "archive_sha256": report["sha256"]}]
    import cloudcopy
    real = cloudcopy.sealed_sha256
    calls = {"n": 0}

    def flaky(path, key):
        calls["n"] += 1
        return "0" * 64 if calls["n"] == 2 else real(path, key)

    monkeypatch.setattr(cloudcopy, "sealed_sha256", flaky)
    assert worker.copy_pass() == 0
    assert [c["final"] for c in backend.copies] == [False]   # part 1 only, not final
    assert not worker.catalogue.session_row(SESSION)["copied_at"]

    monkeypatch.setattr(cloudcopy, "sealed_sha256", real)
    assert worker.copy_pass() == 1                           # parts 2 and 3, then final
    assert [(c["part_no"], c["final"]) for c in backend.copies] == \
        [(1, False), (2, False), (3, True)]


def test_the_parts_leave_this_disk_together_once_all_are_copied(setup):
    worker, backend, segments = setup
    worker.archive_session(a_session(segments, THREE))
    report = backend.completed[-1]
    backend.copy_pending = [{"session_id": SESSION, "archive_relpath": report["archive_relpath"],
                             "archive_sha256": report["sha256"]}]
    worker.copy_pass()

    assert worker.release_pass(now=later(8)) == 1
    for stem in STEMS:
        path = part_path(worker, stem)
        assert not path.exists()
        manifest = json.loads(path.with_suffix(".manifest.json").read_text(encoding="utf-8"))
        assert manifest["local_audio"]["status"] == "released"
        assert path.with_suffix(".json").exists()
    assert worker.release_pass(now=later(9)) == 0


def test_one_changed_part_keeps_the_whole_recording(setup):
    worker, backend, segments = setup
    worker.archive_session(a_session(segments, THREE))
    report = backend.completed[-1]
    backend.copy_pending = [{"session_id": SESSION, "archive_relpath": report["archive_relpath"],
                             "archive_sha256": report["sha256"]}]
    worker.copy_pass()
    damaged = part_path(worker, STEMS[2])
    damaged.write_bytes(damaged.read_bytes()[:-2] + b"\x00\x01")

    assert worker.release_pass(now=later(8)) == 0
    assert all(part_path(worker, stem).exists() for stem in STEMS)


def test_restore_joins_parts_back_into_the_recording(setup, tmp_path):
    worker, backend, segments = setup
    worker.archive_session(a_session(segments, THREE))
    report = backend.completed[-1]
    code = restore.main(["join", str(tmp_path / "whole.wav"),
                         *[str(part_path(worker, s)) for s in STEMS],
                         "--sha256", report["sha256"]])
    assert code == 0
    assert restore.main(["join", str(tmp_path / "w2.wav"),
                         str(part_path(worker, STEMS[1])), str(part_path(worker, STEMS[0])),
                         "--sha256", report["sha256"]]) == 1          # wrong order
