"""
Tests for the archive worker's file handling.

This code writes to the volume that holds every consultation recording, and its
inputs come from clients, so the path handling gets the most attention.

    cd archive_worker && python -m pytest -q
"""
from __future__ import annotations

import json
import wave
from datetime import datetime, timezone
from pathlib import Path

import pytest

import archive
from archive import ArchiveError

SAMPLE_RATE = 44100


def make_wav(path: Path, seconds: float, *, rate: int = SAMPLE_RATE,
             channels: int = 1, width: int = 2) -> Path:
    frames = int(seconds * rate)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(width)
        w.setframerate(rate)
        w.writeframes(b"\x01\x00" * frames * channels)
    return path


# ============================================================
# Path safety - the v1 vulnerability class
# ============================================================

def test_builds_the_expected_tree(tmp_path):
    directory = archive.session_directory(tmp_path, "HOSP001", "DR001", "2026-07-26", "P1_DR001_HOSP001_09_30_10_15_2026_07_26")
    assert directory == (tmp_path / "HOSP001" / "DR001" / "2026-07-26" /
                         "P1_DR001_HOSP001_09_30_10_15_2026_07_26").resolve()


@pytest.mark.parametrize("hospital", [
    "..",
    "../../Windows",
    "..\\..\\Windows",
    "C:/Windows",
    "/etc",
    "HOSP001/../../..",
    "",
    "a" * 100,
    "HOSP 001",
    "HOSP;001",
    "HOSP\x00001",
])
def test_traversal_in_hospital_is_rejected(tmp_path, hospital):
    """v1 joined a client-supplied id into a path and could write anywhere."""
    with pytest.raises(ArchiveError):
        archive.session_directory(tmp_path, hospital, "DR001", "2026-07-26", "P1_x")


@pytest.mark.parametrize("doctor", ["..", "../x", "C:/Windows", "dr 1", ""])
def test_traversal_in_doctor_is_rejected(tmp_path, doctor):
    with pytest.raises(ArchiveError):
        archive.session_directory(tmp_path, "HOSP001", doctor, "2026-07-26", "P1_x")


@pytest.mark.parametrize("bad_date", ["2026-7-26", "26-07-2026", "../2026-07-26",
                                      "2026-07-26/..", "", "not-a-date"])
def test_bad_date_is_rejected(tmp_path, bad_date):
    with pytest.raises(ArchiveError):
        archive.session_directory(tmp_path, "HOSP001", "DR001", bad_date, "P1_x")


def test_filename_shape():
    """patient_doctor_hospital_start-end_date, the agreed archive contract."""
    opened = datetime(2026, 5, 1, 9, 30, tzinfo=timezone.utc)
    closed = datetime(2026, 5, 1, 10, 15, tzinfo=timezone.utc)
    name = archive.archive_filename(
        patient_ref="145", doctor_id="DR001", hospital_id="Hos001",
        opened_at=opened, closed_at=closed)
    assert name == "145_DR001_Hos001_09_30_10_15_2026_05_01.wav"


def test_filename_carries_no_session_ulid():
    """The ULID stays the database key; a reader should never need to parse it."""
    name = archive.archive_filename(
        patient_ref="10045", doctor_id="DR001", hospital_id="HOSP001",
        opened_at=datetime(2026, 5, 1, 9, 30, tzinfo=timezone.utc),
        closed_at=datetime(2026, 5, 1, 10, 15, tzinfo=timezone.utc))
    assert not archive.ULID_PATTERN.search(name.replace("_", " "))
    # patient, doctor, hospital, HH, MM, HH, MM, YYYY, MM, DD
    assert name.count("_") == 9


def test_date_comes_from_the_local_open_time():
    """A consultation opening at 23:50 belongs to that day, not the next."""
    opened = datetime(2026, 5, 1, 23, 50, tzinfo=timezone.utc)
    closed = datetime(2026, 5, 2, 0, 20, tzinfo=timezone.utc)
    name = archive.archive_filename(
        patient_ref="10045", doctor_id="DR001", hospital_id="HOSP001",
        opened_at=opened, closed_at=closed)
    assert name == "10045_DR001_HOSP001_23_50_00_20_2026_05_01.wav"


@pytest.mark.parametrize("patient", ["../x", "P 12345", "", "a" * 100])
def test_bad_patient_ref_rejected(patient):
    with pytest.raises(ArchiveError):
        archive.archive_filename(
            patient_ref=patient, doctor_id="DR001", hospital_id="HOSP001",
            opened_at=datetime.now(timezone.utc), closed_at=datetime.now(timezone.utc))


@pytest.mark.parametrize("field,value", [
    ("doctor_id", "../etc"), ("doctor_id", "DR 001"), ("doctor_id", ""),
    ("hospital_id", "../.."), ("hospital_id", "HOSP/001"), ("hospital_id", ""),
])
def test_bad_identifiers_rejected(field, value):
    """Every component reaches a filename, so every component is validated."""
    kwargs = {"patient_ref": "10045", "doctor_id": "DR001", "hospital_id": "HOSP001",
              "opened_at": datetime.now(timezone.utc),
              "closed_at": datetime.now(timezone.utc)}
    kwargs[field] = value
    with pytest.raises(ArchiveError):
        archive.archive_filename(**kwargs)


def test_expected_join_bytes_counts_one_header():
    """Three 44-byte-header clips join into one file with a single header."""
    assert archive.expected_join_bytes([44 + 100, 44 + 200, 44 + 300]) == 44 + 600


def test_free_destination_reuses_our_own_interrupted_file(tmp_path):
    target = tmp_path / "10045_DR001_HOSP001_0930-1015_20260501.wav"
    target.write_bytes(b"x" * 644)
    path, ours = archive.free_destination(tmp_path, target.name, 644)
    assert ours is True
    assert path == target


def test_free_destination_steps_aside_for_a_different_session(tmp_path):
    """A same-named file of a different size is another consultation, not ours."""
    name = "10045_DR001_HOSP001_0930-1015_20260501.wav"
    (tmp_path / name).write_bytes(b"x" * 999)
    path, ours = archive.free_destination(tmp_path, name, 644)
    assert ours is False
    assert path.name == "10045_DR001_HOSP001_0930-1015_20260501_02.wav"
    assert (tmp_path / name).read_bytes() == b"x" * 999


def test_relative_path_has_no_root():
    """Absolute paths in the database break the day the volume is remounted."""
    name = "10045_DR001_HOSP001_09_30_10_15_2026_05_01"
    rel = archive.relative_path("HOSP001", "DR001", "2026-05-01", name, name + ".wav")
    assert rel == f"HOSP001/DR001/2026-05-01/{name}/{name}.wav"
    assert not rel.startswith("/")
    assert ":" not in rel


def test_folder_is_the_whole_consultation_name(tmp_path):
    """
    One directory is one consultation.

    Named for the consultation, not the patient: a patient seen twice in a day
    would otherwise have both recordings land in the same folder.
    """
    name = "145_DR001_Hos001_10_30_10_45_2026_07_28"
    d = archive.session_directory(tmp_path, "Hos001", "DR001", "2026-07-28", name)
    assert d == tmp_path.resolve() / "Hos001" / "DR001" / "2026-07-28" / name


def test_same_patient_twice_in_a_day_gets_two_folders(tmp_path):
    morning = archive.session_directory(
        tmp_path, "Hos001", "DR001", "2026-07-28", "145_DR001_Hos001_10_30_10_45_2026_07_28")
    afternoon = archive.session_directory(
        tmp_path, "Hos001", "DR001", "2026-07-28", "145_DR001_Hos001_15_00_15_20_2026_07_28")
    assert morning != afternoon
    assert morning.parent == afternoon.parent


@pytest.mark.parametrize("folder", ["../escape", "a/b", "", "x" * 200])
def test_folder_cannot_escape_the_archive(tmp_path, folder):
    """The folder name is built from client-supplied fields, so it is validated."""
    with pytest.raises(ArchiveError):
        archive.session_directory(tmp_path, "HOSP001", "DR001", "2026-05-01", folder)


# ============================================================
# Joining
# ============================================================

def test_joins_segments_in_order(tmp_path):
    parts = [make_wav(tmp_path / f"s{i}.wav", 1.0) for i in range(3)]
    out = tmp_path / "joined.wav"

    result = archive.join_wav(parts, out)

    assert out.exists()
    assert pytest.approx(result.duration_seconds, abs=0.01) == 3.0
    assert result.bytes == out.stat().st_size
    # The hash must describe what is on disk, not what we held in memory.
    assert result.sha256 == archive.sha256_file(out)

    with wave.open(str(out), "rb") as w:
        assert w.getframerate() == SAMPLE_RATE
        assert w.getnchannels() == 1
        assert w.getsampwidth() == 2
        assert w.getnframes() == SAMPLE_RATE * 3


def test_mismatched_sample_rate_is_refused(tmp_path):
    """
    Joining audio recorded at different rates would silently change its speed.
    Better to refuse and keep the segments than produce a plausible-sounding lie.
    """
    parts = [
        make_wav(tmp_path / "a.wav", 1.0, rate=44100),
        make_wav(tmp_path / "b.wav", 1.0, rate=16000),
    ]
    with pytest.raises(ArchiveError, match="audio parameters"):
        archive.join_wav(parts, tmp_path / "joined.wav")


def test_failed_join_leaves_no_partial_file(tmp_path):
    """A half-written archive that looks complete is worse than none."""
    parts = [
        make_wav(tmp_path / "a.wav", 1.0),
        make_wav(tmp_path / "b.wav", 1.0, channels=2),
    ]
    out = tmp_path / "joined.wav"
    with pytest.raises(ArchiveError):
        archive.join_wav(parts, out)

    assert not out.exists()
    assert not out.with_suffix(".wav.partial").exists()


def test_empty_segment_list_is_refused(tmp_path):
    with pytest.raises(ArchiveError):
        archive.join_wav([], tmp_path / "joined.wav")


# ============================================================
# Manifest and index
# ============================================================

def _session(session_id="01KYFR0Z2R744499P1SM490YJ4"):
    return {
        "session_id": session_id,
        "hospital_id": "HOSP001",
        "doctor_id": "DR001",
        "patient_ref": "P12345",
        "session_date": "2026-07-26",
        "opened_at": "2026-07-26T09:32:00+00:00",
        "closed_at": "2026-07-26T09:47:00+00:00",
        "timezone": "Asia/Dhaka",
        "audio": {"sample_rate": 44100, "channels": 1, "sample_width": 2},
        "segments": [
            {"seq_no": 1, "sha256": "ab" * 32, "bytes": 100, "duration_seconds": 1.0},
        ],
        "manifest": {"chain": [{"entry_no": 0, "entry_type": "open"}]},
    }


def test_manifest_makes_the_archive_self_describing(tmp_path):
    parts = [make_wav(tmp_path / "s1.wav", 1.0)]
    audio = tmp_path / "P12345_01KYFR0Z2R744499P1SM490YJ4_0932-0947.wav"
    result = archive.join_wav(parts, audio)

    manifest_path = archive.write_manifest(audio, _session(), result)
    data = json.loads(manifest_path.read_text(encoding="utf-8"))

    assert data["session_id"] == "01KYFR0Z2R744499P1SM490YJ4"
    assert data["hospital_id"] == "HOSP001"
    assert data["archive"]["sha256"] == result.sha256.hex()
    assert data["audio"]["codec"] == "pcm_s16le"
    assert data["audio"]["sample_rate"] == 44100
    # The agent's signed chain travels with the audio, so the archive can be
    # verified even if the database is lost.
    assert data["agent_manifest"]["chain"][0]["entry_type"] == "open"


def test_day_index_is_rebuilt_from_disk(tmp_path):
    directory = tmp_path / "HOSP001" / "DR001" / "2026-07-26"
    directory.mkdir(parents=True)

    for index, sid in enumerate(["01KYFR0Z2R744499P1SM490YJ4",
                                 "01KYFR0Z2R744499P1SM490YJ5"]):
        audio = directory / f"P{index}_{sid}_0932-0947.wav"
        result = archive.join_wav([make_wav(tmp_path / f"x{index}.wav", 0.5)], audio)
        session = _session(sid)
        session["patient_ref"] = f"P{index}"
        archive.write_manifest(audio, session, result)

    index_path = archive.update_day_index(directory)
    data = json.loads(index_path.read_text(encoding="utf-8"))

    assert data["session_count"] == 2
    assert {s["patient_ref"] for s in data["sessions"]} == {"P0", "P1"}
    assert all(s["sha256"] for s in data["sessions"])


# ============================================================
# Time handling
# ============================================================

def test_times_convert_to_hospital_local():
    """
    Dhaka is UTC+6, so a 19:30 UTC consultation is 01:30 the next day locally.
    Naming by UTC would file evening clinics under the wrong date.
    """
    opened, closed = archive.local_times(
        "2026-07-26T19:30:00+00:00", "2026-07-26T19:45:00+00:00", "Asia/Dhaka")
    assert opened.strftime("%H%M") == "0130"
    assert closed.strftime("%H%M") == "0145"
    assert opened.date().isoformat() == "2026-07-27"


def test_unknown_timezone_falls_back_to_utc():
    opened, _ = archive.local_times(
        "2026-07-26T09:30:00+00:00", None, "Not/AZone")
    assert opened.strftime("%H%M") == "0930"


# ============================================================
# Capacity
# ============================================================

def test_refuses_to_start_without_room(tmp_path):
    with pytest.raises(ArchiveError, match="insufficient disk space"):
        archive.ensure_space(tmp_path, 1, headroom=10 ** 15)


def test_allows_when_space_is_available(tmp_path):
    archive.ensure_space(tmp_path, 1024, headroom=1024)
