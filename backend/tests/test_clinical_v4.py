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
    # API 3 reached us while the recording was open: the heartbeat will say so.
    async with cmed.dbs.rec.acquire() as conn:
        assert await conn.fetchval("SELECT prescription_received_at IS NOT NULL FROM sessions "
                                   "WHERE session_id = $1", sid)


# ============================================================
# The recording side of interface 4
# ============================================================

def test_the_file_name_carries_the_visit_when_there_is_one():
    start = datetime(2025, 11, 15, 11, 40, 12)
    end = datetime(2025, 11, 15, 11, 54, 52)
    assert api_v2.file_stem_for("P1", "DR1", "HOSP003", start, end) == \
        "P1_DR1_HOSP003_114012_115452_20251115"
    assert api_v2.file_stem_for("P1", "DR1", "HOSP003", start, end, visit="VIS1001") == \
        "P1_DR1_HOSP003_VIS1001_114012_115452_20251115"


@postgres
async def test_the_heartbeat_tells_the_recorder_its_prescription_arrived(cmed):
    repo = cmed.dbs.repo
    sid = uuid.uuid4().hex[:26].upper()
    opened = datetime.now(timezone.utc) - timedelta(minutes=5)
    async with cmed.dbs.rec.acquire() as conn:
        await conn.execute("""
            INSERT INTO sessions (session_id, patient_id, doctor_id, hospital_id, device_id,
                                  protocol_version, status, session_date, opened_at)
            VALUES ($1, 'P1', 'DR0042', 'HOSP003', $2, 2, 'active', $3, $4)
        """, sid, cmed.device["device_id"], opened.date(), opened)
    beat = api_v2.HeartbeatRequest(device_id=str(cmed.device["device_id"]), session_id=sid)
    reply = await api_v2.heartbeat(beat, device=cmed.device)
    assert "prescription_received" not in reply             # no API 3 yet

    await repo.note_prescription(sid)
    first = await repo.prescription_notice(cmed.device["device_id"], sid)
    await repo.note_prescription(sid)                       # a correction moves nothing
    assert first is not None and first < 5
    reply = await api_v2.heartbeat(beat, device=cmed.device)
    assert reply["prescription_received"]["session_id"] == sid
    # Another PC, or a closed recording, is told nothing.
    assert await repo.prescription_notice(uuid.uuid4(), sid) is None
    async with cmed.dbs.rec.acquire() as conn:
        await conn.execute("UPDATE sessions SET closed_at = now() WHERE session_id = $1", sid)
    assert await repo.prescription_notice(cmed.device["device_id"], sid) is None


@postgres
async def test_a_grant_remembers_the_visit_id(cmed):
    import confirmation as conf
    visit = conf.parse_visit({"patient_id": "P1", "doctor_id": "DR0042",
                              "hospital_id": "CMED-DHOLPUR",
                              "start_time": "2025-11-15T11:40:12+06:00", "date": "2025-11-15"})
    jti = uuid.uuid4().hex
    await cmed.dbs.repo.create_authorisation(
        jti=jti, device_id=cmed.device["device_id"], hospital_id="HOSP003", visit=visit,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=1), visit_id="VIS1001")
    assert (await cmed.dbs.repo.get_authorisation(jti))["visit_id"] == "VIS1001"


@postgres
async def test_the_heartbeat_keeps_which_microphone_records(cmed):
    sid = uuid.uuid4().hex[:26].upper()
    opened = datetime.now(timezone.utc) - timedelta(minutes=2)
    async with cmed.dbs.rec.acquire() as conn:
        await conn.execute("""
            INSERT INTO sessions (session_id, patient_id, doctor_id, hospital_id, device_id,
                                  protocol_version, status, session_date, opened_at)
            VALUES ($1, 'P1', 'DR0042', 'HOSP003', $2, 2, 'active', $3, $4)
        """, sid, cmed.device["device_id"], opened.date(), opened)
    jabra = "Echo Cancelling Speakerphone (Jabra Speak2 55 MS)"
    laptop = "Microphone Array (2- Realtek(R) Audio)"
    for mic, clinic in ((jabra, True), (jabra, True), (laptop, False)):
        await api_v2.heartbeat(api_v2.HeartbeatRequest(
            device_id=str(cmed.device["device_id"]), session_id=sid, microphone=mic,
            microphone_is_clinic=clinic), device=cmed.device)
    async with cmed.dbs.rec.acquire() as conn:
        device = await conn.fetchrow("SELECT microphone, microphone_is_clinic FROM devices "
                                     "WHERE device_id = $1", cmed.device["device_id"])
        used = await conn.fetchval("SELECT microphones FROM sessions WHERE session_id = $1", sid)
    assert (device["microphone"], device["microphone_is_clinic"]) == (laptop, False)
    assert list(used) == [jabra, laptop]               # each once, in the order used


@postgres
async def test_a_person_moves_a_pc_to_another_clinic(cmed):
    from fastapi import HTTPException
    repo, did = cmed.dbs.repo, str(cmed.device["device_id"])
    await repo.upsert_hospital("HOSP004", "Another clinic", "Asia/Dhaka")
    async with cmed.dbs.rec.acquire() as conn:
        await conn.execute("UPDATE devices SET last_seen_at = now() WHERE device_id = $1",
                           cmed.device["device_id"])

    moved = await api_v2.admin_move_device(did, api_v2.DeviceMove(
        moved_by="Tester", hospital_id="HOSP004"), None)
    assert (moved["from"], moved["to"]) == ("HOSP003", "HOSP004")
    async with cmed.dbs.rec.acquire() as conn:
        row = await conn.fetchrow("SELECT hospital_id, last_seen_at FROM devices "
                                  "WHERE device_id = $1", cmed.device["device_id"])
        audited = await conn.fetchval("SELECT count(*) FROM audit_log WHERE event_type = "
                                      "'device.moved' AND device_id = $1", cmed.device["device_id"])
    assert row["hospital_id"] == "HOSP004" and row["last_seen_at"] is not None
    assert audited == 1

    # Reinstalled: it may register again at the new clinic.
    await api_v2.admin_move_device(did, api_v2.DeviceMove(
        moved_by="Tester", hospital_id="HOSP003", register_again=True), None)
    async with cmed.dbs.rec.acquire() as conn:
        row = await conn.fetchrow("SELECT hospital_id, last_seen_at FROM devices "
                                  "WHERE device_id = $1", cmed.device["device_id"])
    assert row["hospital_id"] == "HOSP003" and row["last_seen_at"] is None

    for bad in (api_v2.DeviceMove(moved_by="Tester", hospital_id="NO_SUCH_CLINIC"),):
        with pytest.raises(HTTPException) as err:
            await api_v2.admin_move_device(did, bad, None)
        assert err.value.status_code == 404
    with pytest.raises(HTTPException) as err:
        await api_v2.admin_move_device(str(uuid.uuid4()), api_v2.DeviceMove(
            moved_by="Tester", hospital_id="HOSP004"), None)
    assert err.value.status_code == 404


# ============================================================
# Every section as rows, and the research views (clinical/005)
# ============================================================

def full_api3(**changes):
    rx = api3(**changes)
    rx["observations"] = rx["observations"] + [
        {"category": "on_examination", "note": "Flushed face",
         "items": [{"name": "Blood Pressure", "value": "130/85", "unit": "mm of Hg"},
                   {"name": "Pulse", "value": 88, "unit": "bpm"},
                   {"name": "Anaemia", "value": "Absent"}]}]
    rx["diagnosis"] = rx["diagnosis"] + [{"code": None, "display": "Viral fever",
                                          "type": "final", "note": None}]
    rx["drug_orders"] = [dict(rx["drug_orders"][0], generic_name="Paracetamol",
                              order_id="DRG-1", status="prescribed", brand_new_field="kept")]
    return rx


async def rows(c, sql, *args):
    async with c.dbs.cli.acquire() as conn:
        return [dict(r) for r in await conn.fetch(sql, *args)]


@postgres
async def test_every_section_becomes_rows_and_the_views_read_them(cmed):
    first = api2()
    second = full_api3(**{k: first[k] for k in ("visit_id", "patient_id")})
    assert (await send(cmed, "patient_information", first))[0] == 202
    assert (await send(cmed, "prescription", second))[0] == 202
    v = first["visit_id"]

    visit = (await rows(cmed, "SELECT * FROM v_visit WHERE visit_id = $1", v))[0]
    assert (visit["patient_id"], visit["doctor_id"]) == (first["patient_id"], "DR0042")
    assert visit["has_patient_information"] and visit["has_prescription"]
    assert (visit["complaints"], visit["diagnoses"], visit["drug_orders"],
            visit["lab_orders"], visit["radiology_orders"]) == (1, 2, 1, 1, 1)

    drugs = await rows(cmed, "SELECT * FROM v_drug_orders WHERE visit_id = $1", v)
    assert [(d["medication_name"], d["generic_name"], d["dose"], d["timing"])
            for d in drugs] == [("Napa One", "Paracetamol", "1+1+1", "after meal")]
    extra = await rows(cmed, "SELECT x.extra FROM visit_drug_order x JOIN patient_visit pv "
                             "ON pv.id = x.visit_pk WHERE pv.visit_id = $1", v)
    assert json.loads(extra[0]["extra"]) == {"brand_new_field": "kept"}   # never lost

    diagnoses = await rows(cmed, "SELECT code, is_icd10, display FROM v_diagnoses "
                                 "WHERE visit_id = $1 ORDER BY line_no", v)
    assert [(d["code"], d["is_icd10"]) for d in diagnoses] == [("A97.0", True), (None, None)]

    # Vitals twice: the paramedic's (API 2) and the doctor's (API 3, on examination).
    vitals = await rows(cmed, "SELECT measured_by, name, value, value_num, systolic, diastolic "
                              "FROM v_vitals WHERE visit_id = $1 ORDER BY measured_by, name", v)
    by = {(x["measured_by"], x["name"]): x for x in vitals}
    assert (by[("paramedic", "Blood Pressure")]["systolic"],
            by[("paramedic", "Blood Pressure")]["diastolic"]) == (128, 90)
    assert by[("doctor", "Blood Pressure")]["value"] == "130/85"
    assert by[("doctor", "Pulse")]["value_num"] == 88                     # a number, as sent
    assert ("doctor", "Anaemia") not in by                                # a finding, not a vital
    assert float(by[("paramedic", "Temperature")]["value_num"]) == pytest.approx(98.5)

    notes = await rows(cmed, "SELECT category, note FROM v_observations WHERE visit_id = $1 "
                             "AND item_no = 0 ORDER BY group_no", v)
    assert {"drug_history": "paracetamol", "on_examination": "Flushed face"} == \
        {n["category"]: n["note"] for n in notes}
    advice = await rows(cmed, "SELECT instruction FROM v_advice WHERE visit_id = $1", v)
    assert advice[0]["instruction"] == "প্রতিদিন গোসল করবেন"


@postgres
async def test_a_correction_shows_its_own_rows_and_keeps_every_version(cmed):
    rx = full_api3()
    v = rx["visit_id"]
    await send(cmed, "prescription", rx)
    corrected = dict(rx, issued_at="2025-11-15T12:05:00+06:00",
                     drug_orders=[{"medication_name": "Seclo 20", "generic_name": "Omeprazole"}])
    assert (await send(cmed, "prescription", corrected))[1]["version"] == 2
    older = dict(rx, issued_at="2025-11-15T11:00:00+06:00",
                 drug_orders=[{"medication_name": "Old drug"}])
    assert (await send(cmed, "prescription", older))[1]["version"] == 3

    shown = await rows(cmed, "SELECT medication_name FROM v_drug_orders WHERE visit_id = $1", v)
    assert [d["medication_name"] for d in shown] == ["Seclo 20"]     # the newest, not the replay
    every = await rows(cmed, "SELECT x.medication_name FROM visit_drug_order x "
                             "JOIN patient_visit pv ON pv.id = x.visit_pk WHERE pv.visit_id = $1 "
                             "ORDER BY x.message_id", v)
    assert [d["medication_name"] for d in every] == ["Napa One", "Seclo 20", "Old drug"]
    versions = await rows(cmed, "SELECT version, shown FROM v_prescription_versions "
                                "WHERE visit_id = $1 ORDER BY version", v)
    assert [(x["version"], x["shown"]) for x in versions] == [(1, False), (2, True), (3, False)]


@postgres
async def test_odd_shapes_are_filed_not_refused(cmed):
    rx = {**six(), "drug_orders": {"medication_name": "Napa"},        # one object, not a list
          "advice": "Rest", "chief_complaints": ["Cough", None, ""],  # bare text, null, empty
          "diagnosis": [{"code": "j45.9", "display": "Asthma"}],
          "observations": [{"category": "allergy", "items": ["Dust", {"name": "Food"}]}],
          "lab_orders": [], "radiology_orders": None}
    code, reply = await send(cmed, "prescription", rx)
    assert code == 202, reply
    v = rx["visit_id"]
    assert [d["medication_name"] for d in
            await rows(cmed, "SELECT * FROM v_drug_orders WHERE visit_id = $1", v)] == ["Napa"]
    assert [a["instruction"] for a in
            await rows(cmed, "SELECT * FROM v_advice WHERE visit_id = $1", v)] == ["Rest"]
    complaints = await rows(cmed, "SELECT complaint FROM v_complaints WHERE visit_id = $1 "
                                  "ORDER BY line_no", v)
    assert [c["complaint"] for c in complaints] == ["Cough", None]     # "" kept as an empty line
    assert (await rows(cmed, "SELECT code, is_icd10 FROM v_diagnoses WHERE visit_id = $1", v)) \
        == [{"code": "J45.9", "is_icd10": True}]
    assert sorted(o["name"] for o in await rows(
        cmed, "SELECT name FROM v_observations WHERE visit_id = $1", v)) == ["Dust", "Food"]


@postgres
async def test_the_sections_are_rebuilt_from_the_messages(cmed, pg):
    """Loading twice adds nothing; and the migration fills messages that came before it."""
    rx = full_api3()
    await send(cmed, "prescription", rx)
    old = full_api3()
    async with cmed.dbs.cli.acquire() as conn:
        message = await conn.fetchval("SELECT id FROM visit_messages WHERE visit_id = $1",
                                      rx["visit_id"])
        assert await conn.fetchval("SELECT load_visit_sections($1)", message) == 0

        # A visit filed before 005: its message is there, its rows are not.
        await conn.execute("INSERT INTO patient (hospital_id, hospital_patient_id) "
                           "VALUES ('CMED-DHOLPUR', $1) ON CONFLICT DO NOTHING",
                           old["patient_id"])
        await conn.execute("INSERT INTO patient_visit (visit_id, hospital_id, hospital_patient_id,"
                           " prescription_version, prescription_at) "
                           "VALUES ($1, 'CMED-DHOLPUR', $2, 1, now())",
                           old["visit_id"], old["patient_id"])
        await conn.execute("INSERT INTO visit_messages (kind, hospital_id, visit_id, version, "
                           "body, body_sha256, content_sha256) "
                           "VALUES ('prescription', 'CMED-DHOLPUR', $1, 1, $2, $3, $3)",
                           old["visit_id"], json.dumps(old), uuid.uuid4().bytes)
    from test_db_integration import _apply, CLINICAL_SCRIPTS
    await _apply(pg.clinical, [s for s in CLINICAL_SCRIPTS if s.name.startswith("005_")])
    drugs = await rows(cmed, "SELECT medication_name FROM v_drug_orders WHERE visit_id = $1",
                       old["visit_id"])
    assert [d["medication_name"] for d in drugs] == ["Napa One"]


@postgres
async def test_gynae_history_is_one_row_per_visit_that_has_it(cmed):
    gyne = {"category": "obs_gyne", "note": "Regular check-ups", "items": [
        {"name": "Age of Menarche", "value": "13 Years"}, {"name": "Cycle", "value": "Regular"},
        {"name": "Amount of Flow", "value": "Normal"}, {"name": "Days of Period", "value": "7 Days"},
        {"name": "LMP", "value": "2025-10-16"}, {"name": "Para", "value": "1+1"},
        {"name": "Age of Last Child", "value": "6 Years"},
        {"name": "Delivery History", "value": "NVD"},
        {"name": "Contraceptive History", "value": "Condom"},
        {"name": "Abortion", "value": "None"}]}

    # A woman: API 2 (her sex) and API 3 with the gynae section.
    woman = api2()
    rx = api3(**{k: woman[k] for k in ("visit_id", "patient_id")})
    rx["observations"] = [gyne, {"category": "anc", "items": [{"name": "TT", "value": "2 doses"}]}]
    await send(cmed, "patient_information", woman)
    await send(cmed, "prescription", rx)

    # A man with no gynae section, and a man whose prescription carries one (an error).
    man = api2(demographics={"sex": "male"})
    await send(cmed, "patient_information", man)
    await send(cmed, "prescription", api3(**{k: man[k] for k in ("visit_id", "patient_id")},
                                          observations=[]))
    wrong = api2(demographics={"sex": "male"})
    await send(cmed, "patient_information", wrong)
    await send(cmed, "prescription", api3(**{k: wrong[k] for k in ("visit_id", "patient_id")},
                                          observations=[gyne]))

    rows_ = {r["visit_id"]: r for r in await rows(
        cmed, "SELECT * FROM v_obs_gyne WHERE visit_id = ANY($1)",
        [woman["visit_id"], man["visit_id"], wrong["visit_id"]])}
    assert man["visit_id"] not in rows_                  # no section: no row of empty cells
    w = rows_[woman["visit_id"]]
    assert (w["sex"], w["sex_mismatch"]) == ("female", False)
    assert (w["age_of_menarche"], w["cycle"], w["amount_of_flow"], w["days_of_period"]) == \
        ("13 Years", "Regular", "Normal", "7 Days")
    assert (w["lmp"], str(w["lmp_date"]), w["para"], w["gravida"]) == \
        ("2025-10-16", "2025-10-16", "1+1", None)        # gravida was not sent: empty
    assert (w["age_of_last_child"], w["delivery_history"], w["contraceptive_history"]) == \
        ("6 Years", "NVD", "Condom")
    assert w["has_anc"] and not w["has_pnc"] and w["note"] == "Regular check-ups"
    assert json.loads(w["other_items"]) == {"obs_gyne: Abortion": "None", "anc: TT": "2 doses"}
    assert rows_[wrong["visit_id"]]["sex_mismatch"] is True    # found, not hidden


@postgres
async def test_a_prescription_changed_back_is_a_new_version_and_is_shown(cmed):
    a = full_api3(issued_at="2025-11-15T12:00:00+06:00")
    v = a["visit_id"]
    b = dict(a, issued_at="2025-11-15T12:05:00+06:00",
             drug_orders=[{"medication_name": "Seclo 20", "generic_name": "Omeprazole"}])
    assert (await send(cmed, "prescription", a))[1]["version"] == 1
    assert (await send(cmed, "prescription", b))[1]["version"] == 2

    # A late resend of the first message, byte for byte: still only a resend.
    code, reply = await send(cmed, "prescription", a)
    assert (code, reply["code"], reply["version"]) == (200, "ALREADY_RECEIVED", 1)

    # The doctor puts A back: a third version, and the one the visit shows.
    back = dict(a, issued_at="2025-11-15T12:10:00+06:00")
    code, reply = await send(cmed, "prescription", back)
    assert (code, reply["version"]) == (202, 3), reply
    shown = await rows(cmed, "SELECT medication_name FROM v_drug_orders WHERE visit_id = $1", v)
    assert [d["medication_name"] for d in shown] == ["Napa One"]

    # The latest again, only its time changed: a resend of version 3.
    again = dict(a, issued_at="2025-11-15T12:11:00+06:00")
    code, reply = await send(cmed, "prescription", again)
    assert (code, reply["code"], reply["version"]) == (200, "ALREADY_RECEIVED", 3)
