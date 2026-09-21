"""
The Channel B checker, checked (`SRS-CHB-01`-`12`).

CMED's engineers will trust what this tool says, so it has to agree with the
server exactly: a message it calls good must be accepted, and one it calls
bad must be refused for the reason it gave.

    python -m pytest tools/test_channel_b_test.py -q
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import channel_b_test as tool                          # noqa: E402


# ============================================================
# The samples are good messages
# ============================================================

@pytest.mark.parametrize("kind", ["patient_information", "prescription"])
def test_the_sample_it_hands_out_is_one_the_server_would_accept(kind):
    assert tool.validate(kind, tool.sample(kind)).ok


def test_the_sample_says_out_loud_that_it_is_a_test():
    body = tool.sample("patient_information")
    assert body["patient_id"].startswith("TEST")
    assert "Test" in body["demographics"]["name"]


def test_a_first_visit_says_so_rather_than_leaving_it_out():
    """SRS-CHB-11: `previous_visit: null` is required, so a missing one is caught."""
    body = tool.sample("patient_information")
    assert body["previous_visit"] is None
    without = {k: v for k, v in body.items() if k != "previous_visit"}
    verdict = tool.validate("patient_information", without)
    assert not verdict.ok
    assert verdict.problems[0]["field"] == "previous_visit"


# ============================================================
# What it refuses, and why
# ============================================================

def test_a_missing_identifier_is_named():
    body = {k: v for k, v in tool.sample("patient_information").items()
            if k != "doctor_id"}
    verdict = tool.validate("patient_information", body)
    assert not verdict.ok
    assert verdict.problems[0]["field"] == "doctor_id"


def test_a_start_time_with_no_time_zone_is_refused():
    """SRS-CNF-02: it is matched character for character against the recording."""
    body = dict(tool.sample("patient_information"), start_time="2026-09-13T10:14:32")
    assert not tool.validate("patient_information", body).ok


def test_a_sex_we_do_not_know_is_refused_not_guessed():
    body = tool.sample("patient_information")
    body["demographics"] = {**body["demographics"], "sex": "F"}
    verdict = tool.validate("patient_information", body)
    assert not verdict.ok
    assert verdict.problems[0]["field"] == "demographics.sex"


def test_a_medicine_with_no_name_is_refused():
    body = dict(tool.sample("prescription"), items=[{"dose": "5 mg"}])
    verdict = tool.validate("prescription", body)
    assert not verdict.ok
    assert "drug" in verdict.problems[0]["field"]


def test_a_message_over_the_limit_is_refused_before_anything_else():
    body = dict(tool.sample("patient_information"),
                notes="x" * (tool.MAX_BODY_BYTES + 100))
    verdict = tool.validate("patient_information", body)
    assert not verdict.ok
    assert "413" in verdict.problems[0]["problem"]


def test_something_that_is_not_an_object_is_refused_plainly():
    assert not tool.validate("patient_information", ["not", "an", "object"]).ok
    assert not tool.validate("patient_information", "neither is this").ok


# ============================================================
# What it allows
# ============================================================

def test_extra_fields_are_kept_not_refused():
    """SRS-CHB-11: CMED may send more than we ask for."""
    body = dict(tool.sample("patient_information"), cmed_internal_ref="X-1",
                referred_by="Dr Someone")
    verdict = tool.validate("patient_information", body)
    assert verdict.ok
    assert "cmed_internal_ref" in verdict.note and "referred_by" in verdict.note


def test_a_visit_that_had_an_earlier_one_is_allowed():
    body = dict(tool.sample("patient_information"),
                previous_visit={"date": "2026-06-02", "diagnoses": ["Gastritis"],
                                "prescription": {"items": [{"drug": "Omeprazole"}]}})
    assert tool.validate("patient_information", body).ok


def test_the_checker_agrees_with_the_server_it_imports():
    """
    It is not a copy of the rules - it calls the server's own checker, so the
    two cannot drift apart. This test is here to keep it that way.
    """
    import clinical
    assert tool.clinical is clinical
    assert tool.MAX_BODY_BYTES == clinical.MAX_BODY_BYTES


# ============================================================
# The cases it runs against a server
# ============================================================

def test_every_case_says_what_it_proves():
    for case in tool.CASES:
        assert case.why, case.name
        assert case.expect_code
        assert 200 <= case.expect_status < 500


def test_the_cases_cover_every_reply_the_interface_can_give():
    """§6.2.4: the codes CMED has to handle."""
    covered = {case.expect_code for case in tool.CASES}
    assert covered == {"ACCEPTED", "ALREADY_RECEIVED", "SCHEMA_INVALID",
                       "MISSING_FIELD", "INVALID_KEY", "TOO_LARGE"}


def test_the_broken_cases_really_are_broken_and_the_good_ones_good():
    """Each case's message is checked here, before anyone runs it for real."""
    for case in tool.CASES:
        body = tool._mutated(case, tool.sample(case.kind))
        verdict = tool.validate(case.kind, body)
        if case.expect_code in ("ACCEPTED", "ALREADY_RECEIVED", "INVALID_KEY"):
            assert verdict.ok, f"{case.name} should be a valid message"
        else:
            assert not verdict.ok, f"{case.name} should be refused"


def test_the_report_names_what_went_wrong():
    results = [
        {"case": "A good API 2", "why": "w", "expected": "202 ACCEPTED",
         "got": "202 ACCEPTED", "passed": True, "message": "", "fields": []},
        {"case": "The same API 2 again", "why": "a retry must not store it twice",
         "expected": "200 ALREADY_RECEIVED", "got": "202 ACCEPTED", "passed": False,
         "message": "", "fields": []},
    ]
    text = tool.as_text(results)
    assert "not as expected" in text
    assert "a retry must not store it twice" in text
    assert "NOT READY" in text


def test_a_clean_run_reads_as_ready():
    results = [{"case": "c", "why": "w", "expected": "202 ACCEPTED",
                "got": "202 ACCEPTED", "passed": True, "message": "", "fields": []}]
    assert "Channel B: READY" in tool.as_text(results)


# ============================================================
# The command line
# ============================================================

def test_it_prints_a_sample_anyone_can_copy(capsys):
    assert tool.main(["--sample", "prescription"]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert tool.validate("prescription", printed).ok


def test_it_checks_a_file_and_says_no_without_a_server(tmp_path, capsys):
    path = tmp_path / "broken.json"
    path.write_text(json.dumps({"patient_id": "P1"}), encoding="utf-8")
    assert tool.main(["--validate", str(path)]) == 1
    assert "would be refused" in capsys.readouterr().out


def test_it_checks_a_good_file_and_says_yes(tmp_path, capsys):
    path = tmp_path / "good.json"
    path.write_text(json.dumps(tool.sample("patient_information")), encoding="utf-8")
    assert tool.main(["--validate", str(path)]) == 0
    assert "would be accepted" in capsys.readouterr().out


def test_a_file_that_is_not_json_says_so(tmp_path, capsys):
    path = tmp_path / "bad.json"
    path.write_text("{not json", encoding="utf-8")
    assert tool.main(["--validate", str(path)]) == 1
    assert "not valid JSON" in capsys.readouterr().out
