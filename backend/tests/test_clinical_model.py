"""CMED's clinical messages turned into rows (SRS 3.2 §8.6, §8.7.2) - no database."""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import clinical_model as cm  # noqa: E402

API2 = {
    "patient_id": "P0012345", "doctor_id": "DR0042", "hospital_id": "CMED-1",
    "start_time": "2026-09-13T10:14:32+06:00", "date": "2026-09-13",
    "demographics": {"name": " Ayesha Rahman ", "sex": "Female", "age_years": 34,
                     "phone": "0171", "address": "Dhaka"},
    "paramedic": {"recorded_at": "2026-09-13T10:06:00+06:00", "weight_kg": 58,
                  "height_cm": "156", "blood_pressure": "120/80", "pulse_bpm": 78,
                  "temperature_c": 37.1, "spo2_percent": 98, "notes": "ok",
                  "blood_sugar": 6.1},
    "previous_visit": {"date": "2026-06-02", "diagnoses": ["Gastritis"],
                       "notes": "follow up",
                       "prescription": {"items": [{"drug": "Omeprazole", "dose": "20 mg"}]}},
}


def test_demographics_are_cleaned_not_guessed():
    info = cm.patient_information(API2)
    d = info.demographics
    assert (d.full_name, d.sex, d.age_years, d.phone) == ("Ayesha Rahman", "female", 34, "0171")


def test_sex_is_read_as_clinics_write_it_and_the_rest_stays_unknown():
    for sent, read in (("F", "female"), ("Female", "female"), ("মহিলা", "female"),
                       ("M", "male"), ("পুরুষ", "male"), ("other", "unknown"), ("", "unknown")):
        body = dict(API2, demographics={"sex": sent})
        assert cm.patient_information(body).demographics.sex == read, sent


def test_measurements_are_read_with_their_units_and_bangla_digits():
    p = cm.paramedic({"paramedic": {
        "weight_kg": "128 lb", "height_cm": "5'2\"", "temperature_c": "৯৮.৬ F",
        "blood_pressure": "120/80 mmHg", "pulse_bpm": "78 bpm", "spo2_percent": "98%"}})
    assert (p.weight_kg, p.height_cm, p.temperature_c) == (58.1, 157.5, 37.0)
    assert (p.systolic, p.diastolic, p.pulse_bpm, p.spo2_percent) == (120, 80, 78, 98)
    assert cm.temperature_c(98.6)[0] == 37.0            # only Fahrenheit can be 98.6
    assert cm.height_cm("1.57")[0] == 157.0             # only metres can be 1.57


def test_what_cannot_be_known_is_kept_as_sent_never_guessed():
    p = cm.paramedic({"paramedic": {"height_cm": "5.2", "weight_kg": "heavy",
                                    "temperature_c": 60}})
    assert (p.height_cm, p.weight_kg, p.temperature_c) == (None, None, None)
    assert p.other == {"height_cm": "5.2", "weight_kg": "heavy", "temperature_c": 60}
    notes = {n["field"] for n in cm.quality_notes("patient_information", {
        "demographics": {}, "previous_visit": None,
        "paramedic": {"height_cm": "5.2", "weight_kg": "heavy", "temperature_c": 60}})}
    assert notes == {"paramedic.height_cm", "paramedic.weight_kg", "paramedic.temperature_c"}


def test_dates_follow_ups_and_ages_in_the_forms_clinics_use():
    assert cm.day("20/09/2026").isoformat() == "2026-09-20"        # day first
    assert cm.day("২০-০৯-২০২৬").isoformat() == "2026-09-20"
    assert cm.day("20 Sep 2026").isoformat() == "2026-09-20"
    assert cm.day("31/02/2026") is None
    visit = cm.day("2026-09-13")
    for sent, due in (("7 days", "2026-09-20"), ("৭ দিন পর", "2026-09-20"),
                      ("2 weeks", "2026-09-27"), ("1 month", "2026-10-13")):
        assert cm.follow_up_of(sent, visit)[0].isoformat() == due, sent
    assert cm.follow_up_of("when needed", visit)[0] is None
    for sent, years in (("34", 34), ("34 years", 34), ("34Y", 34), ("6 months", 0),
                        ("১০ দিন", 0), ("2y 3m", 2)):
        assert cm.age_years(sent)[0] == years, sent


def test_a_prescription_in_other_shapes_is_still_a_prescription():
    p = cm.prescription({"date": "2026-09-13", "items": "Napa 500", "diagnoses": "Fever",
                         "follow_up": "7 days", "issued_at": "2026-09-13 10:26:55"})
    assert [i.drug for i in p.items] == ["Napa 500"] and p.diagnoses == ["Fever"]
    assert p.follow_up_date.isoformat() == "2026-09-20"
    assert p.issued_at.isoformat() == "2026-09-13T10:26:55+06:00"
    named = cm.prescription({"items": [{"medicine": "Napa", "dosage": "1 tab"}, {"dose": "x"}]})
    assert [(i.drug, i.dose) for i in named.items] == [("Napa", "1 tab")]


def test_paramedic_columns_and_the_rest():
    p = cm.patient_information(API2).paramedic
    assert (p.weight_kg, p.height_cm, p.systolic, p.diastolic, p.pulse_bpm) == (
        58.0, 156.0, 120, 80, 78)
    assert p.recorded_at.utcoffset().total_seconds() == 6 * 3600
    # OD-16: not yet a column, never lost.
    assert p.other == {"blood_sugar": 6.1}


def test_values_out_of_range_become_empty_not_errors():
    body = dict(API2, paramedic={"pulse_bpm": 900, "spo2_percent": "high",
                                 "blood_pressure": "high"})
    p = cm.patient_information(body).paramedic
    assert (p.pulse_bpm, p.spo2_percent, p.systolic, p.blood_pressure) == (
        None, None, None, "high")


def test_previous_visit_with_its_prescription():
    previous = cm.patient_information(API2).previous
    assert previous.visit_date == date(2026, 6, 2)
    assert [i.drug for i in previous.prescription.items] == ["Omeprazole"]
    assert previous.prescription.diagnoses == ["Gastritis"]
    assert previous.prescription.notes == "follow up"


def test_first_visit_has_no_previous():
    assert cm.patient_information(dict(API2, previous_visit=None)).previous is None


def test_prescription_one_row_per_medicine():
    """SRS-DBA-10."""
    p = cm.prescription({
        "issued_at": "2026-09-13T10:26:11+06:00", "follow_up": "2026-10-13",
        "diagnoses": ["Hypertension", {"text": "Obesity"}], "investigations": ["ECG"],
        "items": [{"drug": "Amlodipine", "dose": "5 mg", "frequency": "1+0+0",
                   "duration": "30 days"},
                  {"drug": "  "},                      # no drug: not a medicine
                  {"drug": "Aspirin", "instructions": "after food"}],
    })
    assert [(i.line_no, i.drug) for i in p.items] == [(1, "Amlodipine"), (2, "Aspirin")]
    assert p.items[0].frequency == "1+0+0"
    assert p.diagnoses == ["Hypertension", "Obesity"]
    assert p.follow_up_date == date(2026, 10, 13)


def test_an_older_visit_sorts_before_any_real_one():
    assert cm.previous_start_time(date(2026, 6, 2), "2026-09-13T10:14:32+06:00") == \
        "2026-06-02T00:00:00+06:00"


def test_the_doctors_findings_come_with_the_prescription_in_any_common_shape():
    p = cm.prescription({
        "chief_complaints": [{"complaint": "Fever", "severity": "moderate", "duration": "3 days"},
                             "Cough"],
        "history": {"family": "Diabetes (mother)", "personal": "Non-smoker"},
        "drug_history": "Metformin",
        "allergies": {"food": "Shrimp", "medicine": "Penicillin", "dust": False},
        "on_examination": {"anaemia": "absent", "jaundice": "absent", "oedema": "present"},
        "systemic_examination": {"heart": "S1 S2 normal", "lungs": "clear"},
        "gynae_obstetric": {"obstetric": "G2 P2", "menstrual": "regular"},
        "items": [], "diagnoses": [], "investigations": []})
    got = [(f.section, f.item, f.value) for f in p.findings]
    assert ("chief_complaint", "Fever", None) in got and ("chief_complaint", "Cough", None) in got
    assert next(f for f in p.findings if f.item == "Fever").detail == {
        "severity": "moderate", "duration": "3 days"}
    assert ("history", "family", "Diabetes (mother)") in got
    assert ("history", "drug", "Metformin") in got
    assert ("allergy", "medicine", "Penicillin") in got and ("allergy", "dust", "no") in got
    assert ("on_examination", "oedema", "present") in got
    assert ("systemic_examination", "lungs", "clear") in got
    assert ("gynae_obstetric", "obstetric", "G2 P2") in got


def test_findings_change_the_version_but_not_cmeds_fingerprint():
    base = {"items": [{"drug": "Napa"}], "diagnoses": ["Fever"], "investigations": []}
    plain = cm.prescription(base)
    with_cc = cm.prescription({**base, "chief_complaints": ["Fever"]})
    corrected = cm.prescription({**base, "chief_complaints": ["Fever", "Headache"]})
    # CMED's fingerprint is the six keys only: unchanged by findings.
    assert cm.fingerprint(plain) == cm.fingerprint(with_cc) == cm.fingerprint(corrected)
    # Without findings the version check is the fingerprint itself ...
    assert cm.content_fingerprint(plain) == cm.fingerprint(plain)
    # ... and a corrected finding is a new version.
    assert cm.content_fingerprint(with_cc) != cm.content_fingerprint(corrected)


def test_api2_keeps_no_name_phone_or_address():
    body, dropped = cm.minimise({"demographics": {"name": "Rahima", "phone": "017",
                                                  "address": "Dhaka", "sex": "female",
                                                  "date_of_birth": "1992-03-11"}})
    assert body["demographics"] == {"sex": "female", "date_of_birth": "1992-03-11"}
    assert dropped == ["name", "phone", "address"]
    assert cm.minimise({"demographics": {"sex": "male"}}) == ({"demographics": {"sex": "male"}}, [])
