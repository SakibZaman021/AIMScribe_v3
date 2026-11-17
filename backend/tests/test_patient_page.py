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
