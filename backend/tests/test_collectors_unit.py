"""Data collectors (036): the rules that need no database."""
import sys
from datetime import datetime, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import collectors  # noqa: E402


def test_the_jabra_is_known_by_either_of_its_windows_names():
    assert collectors.is_clinic_mic("Echo Cancelling Speakerphone (Jabra Speak2 55 MS)")
    assert collectors.is_clinic_mic("Echo Cancelling Speakerphone (J")
    assert not collectors.is_clinic_mic("Microphone Array (Realtek(R) Audio)")
    assert not collectors.is_clinic_mic(None)


def test_the_microphone_column():
    jabra = "Echo Cancelling Speakerphone (J"
    pc = "Microphone Array (Realtek)"
    assert collectors.microphone_label(None)["label"] == "not reported"
    assert collectors.microphone_label([jabra]) == {
        "label": "Jabra", "kind": "jabra", "names": [jabra], "switches": 0}
    assert collectors.microphone_label([pc])["kind"] == "pc"
    both = collectors.microphone_label([jabra, pc], 2)
    assert (both["kind"], both["label"]) == ("mixed", "Jabra, then PC microphone (2 switches)")
    assert collectors.microphone_label([pc, jabra], 1)["label"] == \
        "PC microphone, then Jabra (1 switch)"


def test_a_collector_never_widens_their_scope():
    mine = ["HOSP001", "HOSP002"]
    assert collectors.narrow(mine, None) == mine
    assert collectors.narrow(mine, "HOSP002") == ["HOSP002"]
    assert collectors.narrow(mine, "HOSP009") == []           # another place's: nothing
    assert collectors.narrow([], None) == []                  # a place with no hospitals yet
    assert collectors.narrow(None, None) is None              # admins and researchers: all
    assert collectors.narrow(None, "HOSP009") == ["HOSP009"]
    assert collectors.narrow(object(), "HOSP009") == ["HOSP009"]  # called directly: no limit


def test_the_shift_is_the_one_whose_start_is_nearest():
    place = {"morning_start": time(8), "morning_end": time(14),
             "evening_start": time(14), "evening_end": time(20)}

    def at(h, m=0):
        return datetime(2026, 11, 16, h, m)

    assert collectors.shift_for(place, at(7, 50)) == "morning"
    assert collectors.shift_for(place, at(8, 5)) == "morning"
    assert collectors.shift_for(place, at(13, 45)) == "evening"   # the evening collector, early
    assert collectors.shift_for(place, at(16)) == "evening"
    assert collectors.shift_for(place, at(21, 30)) == "other"


def test_the_mail_status_says_what_to_set(monkeypatch):
    monkeypatch.delenv("AIMS_RESEND_API_KEY", raising=False)
    status = collectors.mail_status()
    assert status["ready"] is False and "set-alerts.sh" in status["problem"]
    monkeypatch.setenv("AIMS_RESEND_API_KEY", "re_x")
    monkeypatch.delenv("AIMS_COLLECTOR_MAIL_FROM", raising=False)
    monkeypatch.delenv("AIMS_ALERT_FROM", raising=False)
    status = collectors.mail_status()            # Resend's test sender: the owner only
    assert status["ready"] is False and "verify a domain" in status["problem"]
    monkeypatch.setenv("AIMS_COLLECTOR_MAIL_FROM", "AIMScribe <alerts@aims.example>")
    assert collectors.mail_status() == {"ready": True, "sender": "AIMScribe <alerts@aims.example>",
                                        "problem": None}
