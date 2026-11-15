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


def test_only_a_passing_failure_is_tried_again():
    assert collectors._transient("Resend could not be reached: ConnectTimeout")
    assert collectors._transient("telegram: Telegram answered 429: Too Many Requests")
    assert collectors._transient("Resend answered 503: unavailable")
    assert not collectors._transient("Resend answered 403: domain not verified")
    assert not collectors._transient("telegram: Telegram answered 403: bot was blocked by the user")
    assert not collectors._transient(None)


def test_the_bot_token_never_shows_in_an_error(monkeypatch):
    monkeypatch.setenv("AIMS_TELEGRAM_BOT_TOKEN", "123456:SECRETSECRETSECRETSECRETSECRET12")
    assert "SECRET" not in collectors._hide(
        "error at https://api.telegram.org/bot123456:SECRETSECRETSECRETSECRETSECRET12/x")
    monkeypatch.delenv("AIMS_TELEGRAM_BOT_TOKEN")
    status = collectors.telegram_status_now()
    assert status["ready"] is False and "set-telegram.sh" in status["problem"]


def test_an_urgent_telegram_message_is_marked_and_fits():
    text = collectors._telegram_text("Jabra unplugged: HOSP003, PC-1", "x" * 5000, True)
    assert text.startswith("\U0001F534 Jabra unplugged") and len(text) <= 4000
    assert not collectors._telegram_text("Shift summary", "body", False).startswith("\U0001F534")


def test_the_report_time_and_what_problems_only_keeps(monkeypatch):
    import watchers
    monkeypatch.delenv("AIMS_REPORT_AT", raising=False)
    assert watchers.report_time() == time(20, 30)
    monkeypatch.setenv("AIMS_REPORT_AT", "21:15")
    assert watchers.report_time() == time(21, 15)
    monkeypatch.setenv("AIMS_REPORT_AT", "late")
    assert watchers.report_time() == time(20, 30)
    assert "escalation" in watchers.PROBLEMS_ONLY and "daily_report" in watchers.PROBLEMS_ONLY
    assert "jabra_back" not in watchers.PROBLEMS_ONLY
    assert collectors.PROBLEMS["silent_session"].startswith("the microphone gives no sound")


def test_only_the_two_ordinary_ends_are_left_untold():
    for ordinary in ("superseded_by_new_patient", "doctor_stopped", "consultation_finished",
                     "counselling_limit", ""):
        assert collectors.stop_kind(ordinary) is None, ordinary
    assert collectors.stop_kind("doctor_stopped:patient_left") == "patient_left"
    assert collectors.stop_kind("pc_shutdown") == "pc_shutdown"
    assert collectors.stop_label("doctor_stopped:urgent_interruption") ==         "an emergency or urgent interruption"
    assert collectors.stop_label("doctor_stopped:something_new") == "something new"
    assert "technical_problem" in collectors.TECHNICAL_ENDS


def test_the_researcher_reads_what_the_collector_was_asked_and_who_to_call():
    import watchers
    collector = ("At 10:05 the recording moved off the Jabra.\n\nPlease go to the room and "
                 "plug the Jabra back in." + collectors._footer())
    text = watchers.researcher_body(collector, "Data collector on shift at Karail:\n- Rahim")
    assert text.startswith("At 10:05 the recording moved off the Jabra.")
    assert "The collector was asked to: go to the room and plug the Jabra back in." in text
    assert "Please" not in text and "mark this as seen" not in text
    assert text.endswith("- Rahim")
    line = watchers._person_line({"email": "r@x", "display_name": "Rahim", "phone": "01711",
                                  "username": "rahim_u"}, since=False)
    assert line == "- Rahim: phone 01711, Telegram @rahim_u"
    assert "no phone given, Telegram not connected" in watchers._person_line(
        {"email": "r@x", "chat_id": None}, since=False)


def test_a_phone_number_is_checked():
    import pytest
    import watchers
    assert watchers.clean_phone(" +880  1711-000000 ") == "+880 1711-000000"
    assert watchers.clean_phone("") is None
    for bad in ("call me", "123", "+880 1711 000000 ext 5"):
        with pytest.raises(ValueError):
            watchers.clean_phone(bad)
