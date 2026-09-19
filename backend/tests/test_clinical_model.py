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


def test_unknown_sex_stays_unknown():
    body = dict(API2, demographics={"sex": "F"})
    assert cm.patient_information(body).demographics.sex == "unknown"


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
