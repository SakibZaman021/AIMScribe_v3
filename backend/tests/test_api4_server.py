"""
3.9.8, the server's half of API 4 and of the doctor's logged choices.

* An end by CMED's API 4 ("consultation_ended") is an ordinary end: no alert,
  green on the dashboard, "CMED closed the visit" in reports.
* The heartbeat says when API 2 reached the open recording.
* The shift workbook lists the doctor's logged choices with the second of audio
  they were made at - what the lab trims by.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND / "src"))
sys.path.insert(0, str(BACKEND / "tests"))

import api_v2                                   # noqa: E402
import collectors                               # noqa: E402
import workbook                                 # noqa: E402
from test_v3_server import DEVICE, NOW, server  # noqa: E402,F401


def test_cmeds_end_time_is_read_from_an_api4_close_only():
    assert api_v2.api4_end_time("consultation_ended", "end_time=2026-11-26T10:31:05+06:00") \
        == "2026-11-26T10:31:05+06:00"
    assert api_v2.api4_end_time("superseded_by_new_patient",
                                "end_time=2026-11-26T10:31:05+06:00") is None
    assert api_v2.api4_end_time("consultation_ended", "") is None
    assert api_v2.api4_end_time("consultation_ended", None) is None


@pytest.mark.asyncio
async def test_the_json_label_ends_the_visit_at_cmeds_end_time(server):
    """The lab labels by CMED's end_time when API 4 ended the recording."""
    sid = "01JB8XQ4M7YZ2K9V3N5P6R8T13"
    closed = datetime(2026, 11, 26, 4, 31, 7, tzinfo=timezone.utc)
    base = {"session_id": sid, "device_id": DEVICE["device_id"], "doctor_id": "DR0042",
            "hospital_id": "HOSP003", "opened_at": closed, "closed_at": closed,
            "confirmation": "confirmed"}
    server.repo.sessions[sid] = dict(base, close_reason="consultation_ended",
                                     close_detail="end_time=2026-11-26T10:31:05+06:00")
    document = await api_v2.archive_clinical_document(sid)
    assert document["visit"]["end_time"] == "2026-11-26T10:31:05+06:00"
    assert document["visit"]["end_time_from"] == "CMED API 4"
    # Ended any other way: when the recording stopped, as before.
    server.repo.sessions[sid] = dict(base, close_reason="superseded_by_new_patient",
                                     close_detail="")
    document = await api_v2.archive_clinical_document(sid)
    assert document["visit"]["end_time"] == closed.isoformat()
    assert document["visit"]["end_time_from"] == "recording stopped"


def test_an_api4_end_is_an_ordinary_end():
    assert collectors.stop_kind("consultation_ended") is None
    assert collectors.stop_label("consultation_ended") == "ended normally"
    assert "consultation_ended" in api_v2.NORMAL_CLOSE_REASONS
    words, column = workbook.ended({"refused": False, "closed_at": NOW(),
                                    "close_reason": "consultation_ended"})
    assert (words, column) == ("CMED closed the visit (API 4)", None)


@pytest.mark.asyncio
async def test_the_heartbeat_says_when_api2_arrived(server):
    sid = "01JB8XQ4M7YZ2K9V3N5P6R8T12"
    server.repo.sessions[sid] = {"session_id": sid, "device_id": DEVICE["device_id"],
                                 "closed_at": None}
    beat = api_v2.HeartbeatRequest(session_id=sid)
    assert "demographics_received" not in await api_v2.heartbeat(beat, device=DEVICE)
    await server.repo.note_demographics(sid)
    reply = await api_v2.heartbeat(beat, device=DEVICE)
    assert reply["demographics_received"] == {"session_id": sid}


def test_the_workbook_lists_the_doctors_choices_with_the_audio_time():
    at = datetime(2026, 11, 26, 4, 20, tzinfo=timezone.utc)
    r = {"pauses": [], "refused": False, "closed_at": at, "opened_at": at,
         "close_reason": "consultation_ended", "close_detail": "",
         "manifest_events": [
             {"at": "2026-11-26T04:05:00Z", "event": "doctor_mark",
              "detail": "Patient left the room", "audio_seconds": 125.0},
             {"at": "2026-11-26T04:06:00Z", "event": "sensitive_start",
              "detail": "Patient's request", "audio_seconds": 185.0},
             {"at": "2026-11-26T04:08:00Z", "event": "sensitive_end",
              "detail": "Patient's request; from 185.0s to 305.0s of the audio (120 s); "
                        "ended by doctor", "audio_seconds": 305.0},
             {"at": "2026-11-26T04:19:00Z", "event": "api4_end",
              "detail": "end_time=2026-11-26T10:19:00+06:00", "audio_seconds": 960.0},
         ]}
    rows = workbook._file_events(r)
    events = [row[1] for row in rows]
    assert events[:4] == ["Doctor's reason (recording continued)",
                          "Sensitive part marked (to remove)", "Sensitive part ended",
                          "CMED closed the visit (API 4)"]
    assert "at 2:05 of the audio" in rows[0][2]
    assert rows[-1][1] == "Ended" and "CMED closed the visit" in rows[-1][2]
