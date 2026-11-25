"""
26 Nov 2026: the Recordings table shows the doctor's Stop and Pause choices
(recorder 3.9.8+ logs them in the recording's manifest; before, only the
per-hospital workbook showed them).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from dashboard import _doctor_log                 # noqa: E402


class Row(dict):
    def keys(self):
        return super().keys()


EVENTS = [
    {"at": "2026-11-25T22:34:27.384Z", "event": "doctor_mark",
     "detail": "Consultation finished", "audio_seconds": 37.8},
    {"at": "2026-11-25T22:34:33.484Z", "event": "sensitive_start",
     "detail": "Sensitive personal matter", "audio_seconds": 43.9},
    {"at": "2026-11-25T22:34:36.440Z", "event": "sensitive_end",
     "detail": "Sensitive personal matter; from 43.9s to 46.8s of the audio (3 s); ended by doctor",
     "audio_seconds": 46.8},
]


def test_each_choice_is_listed_with_its_button_reason_and_second():
    log = _doctor_log(Row(doctor_events=EVENTS))
    assert [(e["button"], e["reason"], e["audio_seconds"]) for e in log] == [
        ("Stop", "Consultation finished", 37.8),
        ("Pause", "Sensitive personal matter", 43.9),
        ("Pause ended", EVENTS[2]["detail"], 46.8),
    ]
    assert log[0]["at"] == "2026-11-25T22:34:27.384Z"


def test_json_text_from_the_database_is_read_too():
    assert len(_doctor_log(Row(doctor_events=json.dumps(EVENTS)))) == 3


def test_nothing_when_the_recording_has_no_choices_or_no_log_yet():
    assert _doctor_log(Row(doctor_events=None)) == []
    assert _doctor_log(Row()) == []
    other = [{"at": "x", "event": "microphone", "detail": "Jabra"}]
    assert _doctor_log(Row(doctor_events=other)) == []
