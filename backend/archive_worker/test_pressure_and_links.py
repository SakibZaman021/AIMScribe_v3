"""
Release on pressure (failure review A1) and continued recordings (S4).

A1: when the archive disk is short of space, archived WAVs whose cloud copy is
proven leave before their retention period, oldest first, until it is not -
and a recording without a proven copy never leaves, however full the disk.

S4: a consultation a restart split into two recordings - each file's JSON and
manifest names the other.
"""
from __future__ import annotations

import json
import logging
import wave
from contextlib import closing
from pathlib import Path

import pytest

import archive
import cloudcopy
import worker as worker_module
from test_worker_copy import (RELPATH, SESSION, STEM, a_recording, catalogued,  # noqa: F401
                              later, setup)

LOW, HIGH = 1 * 1024 ** 3, 500 * 1024 ** 3


@pytest.fixture
def disk(monkeypatch):
    """Free space that depends on which WAVs are still on the disk."""
    state = {"low_while": []}

    def free_bytes(path):
        return LOW if any(p.exists() for p in state["low_while"]) else HIGH

    monkeypatch.setattr(worker_module.archive, "free_bytes", free_bytes)
    return state


def copied(worker, audio):
    catalogued(worker, audio)
    worker.catalogue.mark_copied(SESSION, "copies/HOSP003/x.v1.wav.enc")


def second_recording(worker, root: Path, *, older: bool):
    stem = "P0077777_DR0042_HOSP003_090000_091000_20260913"
    folder = root / "HOSP003" / "DR0042" / "2026-09-13" / stem
    folder.mkdir(parents=True)
    path = folder / f"{stem}.wav"
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(44100)
        handle.writeframes(b"\x01\x00" * 4410)
    relpath = path.relative_to(root).as_posix()
    worker.catalogue.record(
        session={"session_id": "01JB8XQ4M7YZ2K9V3N5P6R8OLD", "patient_ref": "P0077777",
                 "doctor_id": "DR0042", "hospital_id": "HOSP003"},
        archive_relpath=relpath, filename=path.name,
        sha256_hex=cloudcopy.sha256_file(path), byte_length=path.stat().st_size,
        segments=[], session_date="2026-09-13")
    worker.catalogue.mark_copied("01JB8XQ4M7YZ2K9V3N5P6R8OLD", "copies/HOSP003/old.v1.wav.enc")
    if older:
        with closing(worker.catalogue._connect()) as conn:
            conn.execute("UPDATE archived_sessions SET archived_at = '2020-01-01T00:00:00+00:00' "
                         "WHERE session_id = '01JB8XQ4M7YZ2K9V3N5P6R8OLD'")
            conn.commit()
    path.with_suffix(".manifest.json").write_text(json.dumps({"session_id": "old"}),
                                                  encoding="utf-8")
    return path


# ---------------------------------------------------------------- A1

def test_a_full_disk_releases_a_copied_wav_before_its_retention(setup, disk):
    worker, backend, audio = setup
    copied(worker, audio)
    disk["low_while"] = [audio]
    assert worker.release_pass(now=later(1)) == 0           # retention says keep it
    assert worker.release_on_pressure(now=later(1)) == 1    # the disk says it must go
    assert not audio.exists() and not audio.with_suffix(".json").exists()
    manifest = json.loads(audio.with_suffix(".manifest.json").read_text(encoding="utf-8"))
    assert manifest["local_audio"]["status"] == "released"
    assert worker.released_on_pressure == 1


def test_a_wav_without_a_proven_copy_never_leaves_however_full(setup, disk):
    worker, backend, audio = setup
    catalogued(worker, audio)                                # archived, never copied
    disk["low_while"] = [audio]
    assert worker.release_on_pressure(now=later(1)) == 0
    assert audio.exists()


def test_a_wav_that_changed_since_its_copy_is_kept_under_pressure(setup, disk):
    worker, backend, audio = setup
    copied(worker, audio)
    with open(audio, "r+b") as handle:
        handle.seek(-4, 2)
        handle.write(b"\xff\xff\xff\xff")
    disk["low_while"] = [audio]
    assert worker.release_on_pressure(now=later(1)) == 0
    assert audio.exists() and worker.failed == 1


def test_the_oldest_leaves_first_and_only_until_there_is_room(setup, disk, tmp_path):
    worker, backend, audio = setup
    copied(worker, audio)
    old = second_recording(worker, tmp_path, older=True)
    disk["low_while"] = [old]            # releasing the oldest is enough
    assert worker.release_on_pressure(now=later(1)) == 1
    assert not old.exists() and audio.exists()


def test_with_room_on_the_disk_nothing_leaves_early(setup, disk):
    worker, backend, audio = setup
    copied(worker, audio)
    disk["low_while"] = []
    worker._next_retention = 10 ** 12                        # retention pass not due
    assert worker.maybe_release() == 0
    assert audio.exists()


def test_pressure_does_not_wait_for_the_retention_interval(setup, disk):
    worker, backend, audio = setup
    copied(worker, audio)
    disk["low_while"] = [audio]
    worker._next_retention = 10 ** 12
    assert worker.maybe_release() == 1
    assert not audio.exists()


def test_without_cloud_copies_pressure_is_said_once_and_nothing_goes(setup, disk, caplog):
    worker, backend, audio = setup
    copied(worker, audio)
    worker.settings.copy_key = None
    disk["low_while"] = [audio]
    with caplog.at_level(logging.CRITICAL):
        assert worker.release_on_pressure() == 0
        assert worker.release_on_pressure() == 0
    assert audio.exists()
    assert sum("nothing can be released" in r.message for r in caplog.records) == 1


def test_the_floor_is_never_more_than_a_share_of_the_volume(tmp_path):
    total = archive.shutil.disk_usage(tmp_path).total
    assert archive.pressure_floor(tmp_path, 10 ** 18, 0) <= int(total * 0.15)
    assert archive.pressure_floor(tmp_path, None, 1024) == 1024


# ---------------------------------------------------------------- S4

FIRST = {"session_id": "01JB8XQ4M7YZ2K9V3N5P6R8AAA", "file_stem": "P1_DR1_H1_100000_101000_20261016"}
SECOND = {"session_id": "01JB8XQ4M7YZ2K9V3N5P6R8BBB", "file_stem": "P1_DR1_H1_101200_102500_20261016"}


def test_the_json_names_the_recording_it_continues(setup):
    worker, backend, audio = setup
    worker.write_clinical_json(SESSION, audio, links=archive.continuation_links(
        {"continues": FIRST, "continued_by": None}))
    doc = json.loads(audio.with_suffix(".json").read_text(encoding="utf-8"))
    assert doc["continues"] == FIRST and "continued_by" not in doc


def test_what_the_server_says_about_the_link_wins(setup):
    worker, backend, audio = setup
    backend.document = {**backend.document, "continued_by": SECOND}
    worker.write_clinical_json(SESSION, audio, links={"continued_by": FIRST})
    doc = json.loads(audio.with_suffix(".json").read_text(encoding="utf-8"))
    assert doc["continued_by"] == SECOND


def test_the_manifest_names_both_ends(setup, tmp_path):
    worker, backend, audio = setup
    session = {"session_id": SESSION, "hospital_id": "HOSP003", "doctor_id": "DR0042",
               "patient_ref": "P0012345", "session_date": "2026-09-13",
               "opened_at": None, "closed_at": None, "continues": FIRST,
               "continued_by": {"session_id": SECOND["session_id"]}}
    result = archive.JoinResult(path=audio, sha256=bytes(32), bytes=audio.stat().st_size,
                                duration_seconds=0.4, frames=17640)
    path = archive.write_manifest(audio, session, result)
    manifest = json.loads(path.read_text(encoding="utf-8"))
    assert manifest["continues"] == FIRST
    assert manifest["continued_by"] == {"session_id": SECOND["session_id"], "file_stem": None}


def test_a_later_link_reaches_the_json_when_it_is_written_again(setup):
    """The first part learns it was continued only after the second is archived."""
    worker, backend, audio = setup
    worker.copy_pass()
    backend.pending_json.append({"session_id": SESSION, "file_stem": STEM,
                                 "archive_relpath": RELPATH, "continued_by": SECOND})
    assert worker.copy_pass() == 1
    doc = json.loads(audio.with_suffix(".json").read_text(encoding="utf-8"))
    assert doc["continued_by"] == SECOND


def test_malformed_links_are_ignored():
    assert archive.continuation_links({"continues": "nope", "continued_by": {}}) == {}
