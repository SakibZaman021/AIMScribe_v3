"""
One patient's history on the dashboard (18 Nov 2026).

A patient comes back after 20 days: a researcher types the patient ID and
sees every earlier visit - demographics, vitals, prescription, medicines and
recordings - without opening the database. Researchers and admins only; each
look-up is logged by name.
"""
from __future__ import annotations

import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND / "src"))
sys.path.insert(0, str(BACKEND / "tests"))

import api_v2                                   # noqa: E402

try:
    from test_db_integration import _server, _unserve, dbs, pg  # noqa: E402,F401
    HAVE_POSTGRES = True
except pytest.skip.Exception:
    HAVE_POSTGRES = False

pytestmark = [pytest.mark.asyncio,
              pytest.mark.skipif(not HAVE_POSTGRES, reason="pgserver is not installed")]


def as_person(monkeypatch, role, email="someone@uiu.example"):
    import portal

    async def person_for(request):
        return SimpleNamespace(email=email, role=role) if role else None
    monkeypatch.setattr(portal, "person_for", person_for)
    return SimpleNamespace(headers={}, query_params={})


async def _two_visits(patient):
    import clinical_v4
    for visit_id, day, start, temp, drug in (
            (f"VIS-{patient}-1", "2026-10-28", "2026-10-28T10:00:00+06:00", "99", "Napa"),
            (f"VIS-{patient}-2", "2026-11-17", "2026-11-17T11:50:39+06:00", "98", "Fexo")):
        five = {"visit_id": visit_id, "patient_id": patient, "doctor_id": "DR4290371",
                "hospital_id": "Susastho-Clinic-Test-1", "start_time": start, "date": day}
        info = {**five, "demographics": {"sex": "female", "age": "6Y 2M"},
                "observations": [{"category": "vitals", "items": [
                    {"name": "Temperature", "value": temp, "unit": "F"}]}]}
        rx = {**five, "diagnosis": [{"display": "Fever"}], "issued_at": start,
              "drug_orders": [{"form": "Syrup", "medication_name": drug, "dose": "1 spoon"}]}
        for kind, body in (("patient_information", info), ("prescription", rx)):
            await clinical_v4.file_message(kind, body, five["hospital_id"], visit_id, patient,
                                           clinical_v4.read(kind, body), "t")


async def test_a_researcher_sees_every_earlier_visit_newest_first(dbs, monkeypatch):
    import dashboard
    patient = f"P{uuid.uuid4().hex[:7]}"
    _server(dbs)
    try:
        await _two_visits(patient)
        request = as_person(monkeypatch, "researcher", "rahat@uiu.example")
        found = await dashboard.patient_history(patient, request)
        async with dbs.cli.acquire() as conn:
            logged = await conn.fetchrow(
                "SELECT actor, action FROM clinical_access_log WHERE patient_id = $1 "
                "ORDER BY id DESC LIMIT 1", patient)
    finally:
        _unserve()
    assert found["found"] and [v["visit_id"] for v in found["visits"]] == [
        f"VIS-{patient}-2", f"VIS-{patient}-1"]
    newest = found["visits"][0]
    assert newest["demographics"]["temperature"] == "98 F"
    assert newest["prescription"]["diagnoses"] == "Fever"
    assert [m["medicine"] for m in newest["medicines"]] == ["Fexo"]
    assert found["patient"]["visits"] == 2
    assert (logged["actor"], logged["action"]) == ("rahat@uiu.example", "dashboard.patient")


async def test_a_visit_id_finds_its_patient(dbs, monkeypatch):
    import dashboard
    patient = f"P{uuid.uuid4().hex[:7]}"
    _server(dbs)
    try:
        await _two_visits(patient)
        found = await dashboard.patient_history(f"VIS-{patient}-1",
                                                as_person(monkeypatch, "admin"))
    finally:
        _unserve()
    assert found["found"] and found["patient"]["patient_id"] == patient


async def test_an_unknown_id_says_so(dbs, monkeypatch):
    import dashboard
    _server(dbs)
    try:
        found = await dashboard.patient_history("PNOBODY", as_person(monkeypatch, "admin"))
    finally:
        _unserve()
    assert found == {"query": "PNOBODY", "found": False, "patient": None, "visits": []}


async def test_a_data_collector_never_sees_a_clinical_record(dbs, monkeypatch):
    import dashboard
    _server(dbs)
    try:
        with pytest.raises(HTTPException) as refused:
            await dashboard.patient_history("P1", as_person(monkeypatch, "collector"))
    finally:
        _unserve()
    assert refused.value.status_code == 403


async def test_patient_and_visit_id_give_that_one_consultation(dbs, monkeypatch):
    """18 Nov: patient ID alone is the history; with a visit ID, that consultation."""
    import dashboard
    patient = f"P{uuid.uuid4().hex[:7]}"
    _server(dbs)
    try:
        await _two_visits(patient)
        who = as_person(monkeypatch, "researcher")
        one = await dashboard.patient_history(patient, who, visit_id=f"VIS-{patient}-1")
        other = await dashboard.patient_history(patient, who, visit_id="VIS-NOT-THEIRS")
        with pytest.raises(HTTPException) as bad:
            await dashboard.patient_history(patient, who, visit_id="../x")
    finally:
        _unserve()
    assert [v["visit_id"] for v in one["visits"]] == [f"VIS-{patient}-1"]
    assert one["visit_found"] is True and one["visits"][0]["demographics"]["temperature"] == "99 F"
    assert other["found"] is True and other["visit_found"] is False and other["visits"] == []
    assert bad.value.status_code == 400


# ============================================================
# 19 Nov 2026: the demographics, prescriptions and medicines as CSV
# ============================================================

def _read_csv(response):
    import csv
    import io
    return list(csv.DictReader(io.StringIO(response.body.decode("utf-8-sig"))))


async def test_a_researcher_downloads_demographics_and_it_is_logged(dbs, monkeypatch):
    import dashboard
    patient = f"P{uuid.uuid4().hex[:7]}"
    _server(dbs)
    try:
        await _two_visits(patient)
        response = await dashboard.clinical_csv(
            as_person(monkeypatch, "researcher", "rahat@uiu.example"),
            table="demographics", patient_id=patient)
        async with dbs.cli.acquire() as conn:
            logged = await conn.fetch(
                "SELECT actor, action, detail FROM clinical_access_log WHERE patient_id = $1 "
                "AND action = 'dashboard.csv'", patient)
    finally:
        _unserve()
    assert response.media_type.startswith("text/csv")
    assert "attachment" in response.headers["content-disposition"]
    header = response.body.decode("utf-8-sig").splitlines()[0].split(",")
    assert header[:5] == ["clinic", "cmed_clinic_code", "patient_id", "doctor_id", "visit_id"]
    assert "visit_key" not in header                 # internal, never in a download
    rows = _read_csv(response)
    assert sorted(r["visit_id"] for r in rows) == [f"VIS-{patient}-1", f"VIS-{patient}-2"]
    assert {r["temperature"] for r in rows} == {"99 F", "98 F"}
    assert {r["sex"] for r in rows} == {"female"}
    assert [(r["actor"], r["action"]) for r in logged] == [("rahat@uiu.example", "dashboard.csv")]


async def test_the_dates_and_the_clinic_narrow_the_download(dbs, monkeypatch):
    import dashboard
    patient = f"P{uuid.uuid4().hex[:7]}"
    _server(dbs)
    try:
        await _two_visits(patient)
        who = as_person(monkeypatch, "admin")
        later = await dashboard.clinical_csv(who, table="prescriptions", patient_id=patient,
                                             from_date="2026-11-01", to_date="2026-11-30")
        by_code = await dashboard.clinical_csv(who, table="medicines", patient_id=patient,
                                               hospital_id="Susastho-Clinic-Test-1")
        elsewhere = await dashboard.clinical_csv(who, table="demographics", patient_id=patient,
                                                 hospital_id="HOSP_NOWHERE")
    finally:
        _unserve()
    rows = _read_csv(later)
    assert [r["visit_id"] for r in rows] == [f"VIS-{patient}-2"]
    assert rows[0]["diagnoses"] == "Fever" and "Fexo" in rows[0]["medicines"]
    assert sorted(r["medicine"] for r in _read_csv(by_code)) == ["Fexo", "Napa"]
    # Nothing found: the column names alone, so the file still opens.
    assert _read_csv(elsewhere) == []
    assert elsewhere.body.decode("utf-8-sig").startswith("clinic,cmed_clinic_code,patient_id")


async def test_a_data_collector_cannot_download_clinical_data(dbs, monkeypatch):
    import dashboard
    _server(dbs)
    try:
        with pytest.raises(HTTPException) as refused:
            await dashboard.clinical_csv(as_person(monkeypatch, "collector"),
                                         table="demographics")
        with pytest.raises(HTTPException) as unknown:
            await dashboard.clinical_csv(as_person(monkeypatch, "admin"), table="audit_log")
        with pytest.raises(HTTPException) as bad_date:
            await dashboard.clinical_csv(as_person(monkeypatch, "admin"), from_date="19/11/2026")
    finally:
        _unserve()
    assert refused.value.status_code == 403
    assert unknown.value.status_code == 400
    assert bad_date.value.status_code == 400


async def test_the_dashboard_button_downloads_through_the_real_address(dbs, monkeypatch):
    """Through the route, signed in as on the dashboard: the from/to names and the role."""
    from test_db_integration import _portal_app, _sign_in
    patient = f"P{uuid.uuid4().hex[:7]}"
    who = {"value": "owner@uiu.example"}
    async with _portal_app(dbs, monkeypatch, google_email=who) as client:
        await _two_visits(patient)
        await _sign_in(client)
        got = await client.get("/api/v2/dashboard/clinical.csv",
                               params={"table": "demographics", "from": "2026-11-01",
                                       "to": "2026-11-30", "patient_id": patient},
                               headers={"X-AIMS-Portal": "1"})
        wrong = await client.get("/api/v2/dashboard/clinical.csv", params={"table": "nothing"},
                                 headers={"X-AIMS-Portal": "1"})
    _unserve()
    assert got.status_code == 200, got.text
    assert got.headers["content-disposition"] == (
        'attachment; filename="demographics_2026-11-01_2026-11-30.csv"')
    rows = _read_csv(SimpleNamespace(body=got.content))
    assert [r["visit_id"] for r in rows] == [f"VIS-{patient}-2"]
    assert wrong.status_code == 400
