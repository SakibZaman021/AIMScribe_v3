"""
Interface 4 (/api/v4/clinical): plain JSON keyed by CMED's visit_id.

Without a database: what a message is read as, which values become null and
are warned about, and the body the older pipeline is given.

Against PostgreSQL (when pgserver is installed): the whole path - filing in
patient / patient_visit / visit_messages, the older tables filled through
clinical.process, corrections as versions, the three ways a message is
stopped, and the status of one visit, with and without its recording.
"""
from __future__ import annotations

import json
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import api_v2                                   # noqa: E402
import clinical_v4 as v4                        # noqa: E402
import security                                 # noqa: E402
from test_cmed_auth_fhir import Req             # noqa: E402

try:
    from test_db_integration import dbs, pg, enrolled_device   # noqa: E402,F401
    HAVE_POSTGRES = True
except pytest.skip.Exception:
    HAVE_POSTGRES = False


def postgres(test):
    return pytest.mark.skipif(not HAVE_POSTGRES, reason="pgserver is not installed")(
        pytest.mark.asyncio(test))


def six(**changes):
    base = {"visit_id": "VIS-" + uuid.uuid4().hex[:10], "patient_id": "P" + uuid.uuid4().hex[:9],
            "doctor_id": "DR0042", "hospital_id": "CMED-DHOLPUR",
            "start_time": "2025-11-15T11:40:12+06:00", "date": "2025-11-15"}
    base.update(changes)
    return base


def api2(**changes):
    return {**six(), "demographics": {"sex": "female", "date_of_birth": "1989-11-14",
                                      "age": "36Y 1D", "blood_group": None},
            "observations": [{"category": "vitals", "recorded_by": "paramedic",
                              "recorded_at": "2025-11-15T11:31:05+06:00",
                              "items": [{"name": "Blood Pressure", "value": "128/90",
                                         "unit": "mm of Hg"},
                                        {"name": "Temperature", "value": "98.5", "unit": "F"},
                                        {"name": "Weight", "value": "49.6", "unit": "kg"}]}],
            **changes}


def api3(**changes):
    return {**six(), "issued_at": "2025-11-15T11:52:40+06:00",
            "chief_complaints": [{"complaint": "Fever", "duration": "2 days",
                                  "severity": "high", "note": None}],
            "observations": [
                {"category": "allergy", "note": None,
                 "items": [{"name": "Food", "value": "Yes, prawn"}]},
                {"category": "drug_history", "note": "paracetamol", "items": []},
                {"category": "obs_gyne", "note": None, "items": [{"name": "Para", "value": "1+1"}]}],
            "diagnosis": [{"code": "A97.0", "display": "Dengue fever", "type": "provisional",
                           "note": None}],
            "lab_orders": [{"test_code": "CBC", "test_name": "Complete Blood Count"}],
            "radiology_orders": [{"procedure_name": "X-ray PNS O/M view"}],
            "drug_orders": [{"form": "Tablet", "medication_name": "Napa One",
                             "strength": "1000 mg", "dose": "1+1+1", "timing": "after meal",
                             "duration": "3 days"}],
            "advice": [{"type": "general", "instruction": "প্রতিদিন গোসল করবেন"}],
            "follow_up": "2025-11-18", **changes}


# ============================================================
# Reading (no database)
# ============================================================

def test_the_link_keys_are_the_only_required_fields():
    assert v4._link_keys(six(visit_id="VIS1", patient_id="P1", hospital_id="H1")) == \
        ("VIS1", "P1", "H1")
    with pytest.raises(v4._Stop) as stop:
        v4._link_keys(six(visit_id=None, hospital_id=""))
    assert stop.value.code == "MISSING_KEY" and stop.value.fields == ["visit_id", "hospital_id"]
    # An underscore would split the audio file's name in the wrong place.
    with pytest.raises(v4._Stop) as stop:
        v4._link_keys(six(visit_id="VIS_1"))
    assert stop.value.code == "INVALID_IDENTIFIER"


def test_empty_and_odd_values_become_null_and_are_named():
    parsed = v4.read("prescription", {"visit_id": "V", "doctor_id": "", "start_time": "soon",
                                      "chief_complaints": None, "advice": "rest",
                                      "follow_up": "after 7 days"})
    assert parsed["practitioner_id"] is None and parsed["start_time"] is None
    assert parsed["chief_complaints"] == [] and parsed["drug_orders"] == []
    assert parsed["advice"] == ["rest"]                      # a single value: a list of one
    assert parsed["follow_up"] is None and parsed["follow_up_text"] == "after 7 days"
    named = {w["field"] for w in parsed["warnings"]}
    assert {"doctor_id", "start_time", "date", "advice", "follow_up"} <= named


def test_api2_keeps_age_as_written_and_reads_the_years():
    parsed = v4.read("patient_information", api2())
    assert (parsed["age"], parsed["age_years"], parsed["sex"]) == ("36Y 1D", 36, "female")
    assert str(parsed["date_of_birth"]) == "1989-11-14" and parsed["blood_group"] is None
    assert parsed["warnings"] == []


def test_the_older_body_carries_every_section():
    old = v4.to_older_body("prescription", api3())
    assert old["items"] == [{"drug": "Tablet Napa One (1000 mg)", "dose": "1+1+1",
                             "frequency": None, "duration": "3 days",
                             "instructions": "after meal"}]
    assert old["diagnoses"] == ["Dengue fever"]
    assert old["investigations"] == ["Complete Blood Count", "X-ray PNS O/M view"]
    assert old["advice"] == "প্রতিদিন গোসল করবেন" and old["follow_up"] == "2025-11-18"
    sections = {(f["section"], f["item"]) for f in old["findings"]}
    assert ("chief_complaint", "Fever") in sections
    assert ("allergy", "Food") in sections and ("history", "drug") in sections
    assert ("gynae_obstetric", "Para") in sections
    vitals = v4.to_older_body("patient_information", api2())["paramedic"]
    assert vitals["blood_pressure"] == "128/90" and vitals["temperature_c"] == "98.5 F"
    assert vitals["weight_kg"] == "49.6 kg"


# ============================================================
# Against PostgreSQL
# ============================================================

@pytest_asyncio.fixture
async def cmed(dbs):
    api_v2.ctx.repo, api_v2.ctx.clinical = dbs.repo, dbs.clinical
    api_v2.forget_directory()
    security.AUTH_FAILURES._locked.clear()
    device = await enrolled_device(dbs.repo)            # HOSP003 = CMED-DHOLPUR
    key = await dbs.repo.create_cmed_key(label=f"v4-{uuid.uuid4().hex[:6]}",
                                         created_by="tests", expires_at=None,
                                         cmed_hospital_ids=["CMED-DHOLPUR"])
    yield SimpleNamespace(dbs=dbs, key=key, device=device)
    api_v2.ctx.repo = api_v2.ctx.clinical = None


async def send(c, kind, body, key=None):
    fn = v4.patient_information if kind == "patient_information" else v4.prescription
    reply = await fn(Req(body, headers={"x-cmed-key": key or c.key}))
    return reply.status_code, json.loads(reply.body)


async def status(c, visit_id):
    reply = await v4.visit_status(visit_id, Req(headers={"x-cmed-key": c.key}))
    return reply.status_code, json.loads(reply.body)


@postgres
async def test_one_patient_row_one_visit_row_and_the_older_tables_too(cmed):
    first, second = api2(), api3()
    second.update({k: first[k] for k in ("visit_id", "patient_id")})
    code, reply = await send(cmed, "patient_information", first)
    assert (code, reply["code"], reply["recording"]) == (202, "ACCEPTED", "waiting"), reply
    code, reply = await send(cmed, "prescription", second)
    assert (code, reply["version"], reply["warnings"]) == (202, 1, []), reply

    # A second visit by the same patient: still one patient row.
    again = api2(patient_id=first["patient_id"], start_time="2025-11-20T10:00:00+06:00",
                 date="2025-11-20")
    assert (await send(cmed, "patient_information", again))[0] == 202

    async with cmed.dbs.cli.acquire() as conn:
        patients = await conn.fetch("SELECT * FROM patient WHERE hospital_patient_id = $1",
                                    first["patient_id"])
        visit = await conn.fetchrow("SELECT * FROM patient_visit WHERE visit_id = $1",
                                    first["visit_id"])
        older = await conn.fetchval("SELECT count(*) FROM intake_records WHERE patient_id = $1",
                                    first["patient_id"])
    assert len(patients) == 1 and patients[0]["sex"] == "female"
    assert visit["age"] == "36Y 1D" and visit["practitioner_id"] == "DR0042"
    assert json.loads(visit["drug_orders"])[0]["medication_name"] == "Napa One"
    assert json.loads(visit["advice"])[0]["instruction"] == "প্রতিদিন গোসল করবেন"
    assert str(visit["follow_up"]) == "2025-11-18"
    assert older == 3                    # API 2 twice and API 3 went through clinical.process


@postgres
async def test_a_repeat_is_already_received_and_a_correction_is_the_next_version(cmed):
    rx = api3()
    assert (await send(cmed, "prescription", rx))[1]["version"] == 1
    code, reply = await send(cmed, "prescription", dict(rx))
    assert (code, reply["code"], reply["version"]) == (200, "ALREADY_RECEIVED", 1)
    # Only the moment it was sent changed: the same prescription.
    code, reply = await send(cmed, "prescription",
                             dict(rx, issued_at="2025-11-15T11:59:00+06:00"))
    assert reply["code"] == "ALREADY_RECEIVED"
    corrected = dict(rx, issued_at="2025-11-15T12:05:00+06:00", follow_up="2025-11-20")
    code, reply = await send(cmed, "prescription", corrected)
    assert (code, reply["version"]) == (202, 2)
    # An older version replayed afterwards never rolls the visit back.
    older = dict(rx, issued_at="2025-11-15T11:00:00+06:00", follow_up="2025-11-30")
    code, reply = await send(cmed, "prescription", older)
    assert reply["version"] == 3 and any(w["field"] == "issued_at" for w in reply["warnings"])
    async with cmed.dbs.cli.acquire() as conn:
        follow = await conn.fetchval("SELECT follow_up FROM patient_visit WHERE visit_id = $1",
                                     rx["visit_id"])
        kept = await conn.fetchval("SELECT count(*) FROM visit_messages WHERE visit_id = $1",
                                   rx["visit_id"])
    assert str(follow) == "2025-11-20" and kept == 3


@postgres
async def test_nothing_is_refused_for_empty_fields(cmed):
    bare = {"visit_id": "VIS-" + uuid.uuid4().hex[:8], "patient_id": "PBARE",
            "hospital_id": "CMED-DHOLPUR", "drug_orders": [], "advice": None}
    code, reply = await send(cmed, "prescription", bare)
    assert code == 202, reply
    named = {w["field"] for w in reply["warnings"]}
    assert {"doctor_id", "start_time", "date"} <= named
    assert reply["recording"] == "waiting"


@postgres
async def test_a_message_without_its_keys_is_kept_for_review(cmed):
    code, reply = await send(cmed, "patient_information", api2(visit_id=None))
    assert (code, reply["code"], reply["fields"]) == (422, "MISSING_KEY", ["visit_id"])
    async with cmed.dbs.cli.acquire() as conn:
        kept = await conn.fetchval("SELECT count(*) FROM visit_quarantine "
                                   "WHERE problems::text LIKE '%visit_id%'")
    assert kept >= 1


@postgres
async def test_a_visit_id_belongs_to_one_patient(cmed):
    first = api2()
    assert (await send(cmed, "patient_information", first))[0] == 202
    code, reply = await send(cmed, "patient_information", dict(first, patient_id="POTHER"))
    assert (code, reply["code"]) == (409, "VISIT_CONFLICT")


@postgres
async def test_another_hospitals_message_is_refused(cmed):
    code, reply = await send(cmed, "patient_information", api2(hospital_id="CMED-ELSEWHERE"))
    assert (code, reply["code"]) == (403, "HOSPITAL_NOT_ALLOWED")


@postgres
async def test_the_status_of_one_visit(cmed):
    assert (await status(cmed, "VIS-NEVER-SENT"))[0] == 404
    first = api2()
    rx = api3(visit_id=first["visit_id"], patient_id=first["patient_id"])
    await send(cmed, "patient_information", first)
    code, state = await status(cmed, first["visit_id"])
    assert code == 200 and state["api2"]["warnings"] == 0
    assert (state["api1_recording"], state["api3"], state["complete"]) == (None, None, False)

    # The recording of this visit reaches us: the page's click names the six fields.
    sid = uuid.uuid4().hex[:26].upper()
    opened = datetime.now(timezone.utc) - timedelta(minutes=12)
    async with cmed.dbs.rec.acquire() as conn:
        await conn.execute("""
            INSERT INTO sessions (session_id, patient_id, doctor_id, hospital_id, device_id,
                                  protocol_version, status, session_date, opened_at,
                                  segment_count)
            VALUES ($1, $2, 'DR0042', 'HOSP003', $3, 2, 'active', $4, $5, 3)
        """, sid, first["patient_id"], cmed.device["device_id"], opened.date(), opened)
        await conn.execute("""
            INSERT INTO session_clicks (session_id, entry_no, event, audio_seconds,
                                        occurred_at, patient_id, doctor_id, cmed_hospital_id,
                                        start_time, visit_date)
            VALUES ($1, 1, 'patient_opened', 0, $2, $3, 'DR0042', 'CMED-DHOLPUR', $4, $5)
        """, sid, opened, first["patient_id"], first["start_time"],
            datetime.fromisoformat(first["date"]).date())
    code, reply = await send(cmed, "prescription", rx)
    assert reply["recording"] == "linked", reply
    code, state = await status(cmed, first["visit_id"])
    assert state["api1_recording"]["session_id"] == sid
    assert state["api1_recording"]["audio_received"] is True
    assert state["api3"]["version"] == 1 and state["complete"] is True
    async with cmed.dbs.cli.acquire() as conn:
        assert await conn.fetchval("SELECT aims_id FROM patient_visit WHERE visit_id = $1",
                                   first["visit_id"]) == sid
