"""
The AIMS LAB server's local index.

The property under test is independence: after these run, every question the
hospital needs answered about its own audio is answerable from one SQLite file,
with no network and no cloud account.
"""
from __future__ import annotations

import pytest

from catalogue import Catalogue


def _session(session_id="01KYFR0Z2R744499P1SM490YJ4", patient="10045", doctor="DR001"):
    return {
        "session_id": session_id,
        "patient_ref": patient,
        "doctor_id": doctor,
        "hospital_id": "HOSP001",
        "opened_at": "2026-05-01T09:30:00+00:00",
        "closed_at": "2026-05-01T10:15:00+00:00",
        "duration_seconds": 2700.0,
        "paused_seconds": 120.0,
    }


def _segments(n=3):
    return [{"seq_no": i, "clip_name": f"10045_DR001_HOSP001_20260501_{i:04d}.wav",
             "sha256": f"{i:064x}", "bytes": 1000 + i} for i in range(1, n + 1)]


def _record(cat, session=None, relpath=None, segments=None, **overrides):
    session = session or _session()
    relpath = relpath or f"HOSP001/DR001/2026-05-01/{session['patient_ref']}_x.wav"
    cat.record(
        session=session, archive_relpath=relpath,
        filename=relpath.rsplit("/", 1)[-1],
        sha256_hex=overrides.get("sha256_hex", "ab" * 32),
        byte_length=overrides.get("byte_length", 5000),
        segments=segments or _segments(),
        session_date=overrides.get("session_date", "2026-05-01"))


@pytest.fixture
def cat(tmp_path):
    return Catalogue(tmp_path / "catalogue.sqlite3")


def test_created_on_a_fresh_volume(tmp_path):
    """A brand new AIMS LAB server has no database until the worker makes one."""
    path = tmp_path / "nested" / "catalogue.sqlite3"
    Catalogue(path)
    assert path.exists()


def test_records_and_finds_by_patient(cat):
    _record(cat)
    found = cat.find(patient_id="10045")
    assert len(found) == 1
    assert found[0]["archive_relpath"].startswith("HOSP001/DR001/")
    assert found[0]["segment_count"] == 3
    assert found[0]["sha256"] == "ab" * 32


def test_find_by_doctor_and_date(cat):
    _record(cat)
    assert len(cat.find(hospital_id="HOSP001", doctor_id="DR001")) == 1
    assert len(cat.find(session_date="2026-05-01")) == 1
    assert cat.find(session_date="2026-05-02") == []


def test_relpath_is_relative(cat):
    """An absolute path breaks the day the volume is remounted."""
    _record(cat)
    rel = cat.find()[0]["archive_relpath"]
    assert not rel.startswith("/")
    assert ":" not in rel


def test_clip_names_are_kept(cat):
    _record(cat)
    import sqlite3
    conn = sqlite3.connect(str(cat.path))
    names = [r[0] for r in conn.execute(
        "SELECT clip_name FROM archived_clips ORDER BY seq_no")]
    conn.close()
    assert names == ["10045_DR001_HOSP001_20260501_0001.wav",
                     "10045_DR001_HOSP001_20260501_0002.wav",
                     "10045_DR001_HOSP001_20260501_0003.wav"]


def test_rerun_does_not_duplicate(cat):
    """An interrupted run that repeats must not create a second row."""
    _record(cat)
    _record(cat, byte_length=6000)
    found = cat.find(patient_id="10045")
    assert len(found) == 1
    assert found[0]["bytes"] == 6000


def test_unreported_until_the_backend_acknowledges(cat):
    """
    The one state where doctor PCs quietly fill up: archived here, but no receipt
    ever issued, so every agent keeps its local copy.
    """
    _record(cat)
    assert len(cat.unreported()) == 1

    cat.mark_reported("01KYFR0Z2R744499P1SM490YJ4")
    assert cat.unreported() == []
    assert cat.find()[0]["reported_at"] is not None


def test_totals_across_sessions(cat):
    _record(cat)
    _record(cat, session=_session(session_id="01KYFR0Z2R744499P1SM490YJ5", patient="10046"),
            relpath="HOSP001/DR001/2026-05-01/10046_x.wav")
    cat.mark_reported("01KYFR0Z2R744499P1SM490YJ4")

    totals = cat.totals()
    assert totals["sessions"] == 2
    assert totals["clips"] == 6
    assert totals["bytes"] == 10000
    assert totals["unreported"] == 1


def test_survives_reopen(cat):
    """The index has to outlive the worker process, not just the run."""
    _record(cat)
    reopened = Catalogue(cat.path)
    assert len(reopened.find(patient_id="10045")) == 1


def test_pause_reasons_travel_with_the_audio(cat):
    """
    A gap in a consultation is what an auditor asks about. The reason has to be
    answerable on this machine, without reaching back to the cloud.
    """
    pauses = [
        {"entry_no": 3, "entry_type": "pause", "occurred_at": "2026-05-01T09:45:00+00:00",
         "reason": "patient_examination", "authorised_by": None, "seconds": None},
        {"entry_no": 4, "entry_type": "resume", "occurred_at": "2026-05-01T09:47:30+00:00",
         "reason": None, "authorised_by": None, "seconds": 150.0},
    ]
    _record(cat, segments=_segments(), **{})
    cat.record(session=_session(), archive_relpath="HOSP001/DR001/2026-05-01/10045_x.wav",
               filename="10045_x.wav", sha256_hex="ab" * 32, byte_length=5000,
               segments=_segments(), session_date="2026-05-01", pauses=pauses)

    found = cat.pauses_for("01KYFR0Z2R744499P1SM490YJ4")
    assert [p["entry_type"] for p in found] == ["pause", "resume"]
    assert found[0]["reason"] == "patient_examination"
    assert found[1]["seconds"] == 150.0


def test_recording_with_no_pauses_records_none(cat):
    _record(cat)
    assert cat.pauses_for("01KYFR0Z2R744499P1SM490YJ4") == []
