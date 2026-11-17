"""
One row per patient, and the readable tables first (18 Nov 2026).

The user could not find a patient without scrolling: the visit tables sat in
schema by_visit, which the database viewer does not open, and nothing listed
each patient once. clinical/008 adds by_visit.patient_list; the role scripts
make by_visit the first schema for the reading logins (never the server's).
"""
from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND / "src"))
sys.path.insert(0, str(BACKEND / "tests"))

import api_v2                                   # noqa: E402

try:
    from test_db_integration import _role_setup, dbs, pg  # noqa: E402,F401
    HAVE_POSTGRES = True
except pytest.skip.Exception:
    HAVE_POSTGRES = False

pytestmark = [pytest.mark.asyncio,
              pytest.mark.skipif(not HAVE_POSTGRES, reason="pgserver is not installed")]


async def _visit(patient, visit_id, date, start, *, sex="female", age="6Y 2M", dx=None):
    import clinical_v4
    five = {"visit_id": visit_id, "patient_id": patient, "doctor_id": "DR4290371",
            "hospital_id": "Susastho-Clinic-Test-1", "start_time": start, "date": date}
    info = {**five, "demographics": {"sex": sex, "date_of_birth": "2020-09-17", "age": age,
                                     "blood_group": "B+"}}
    await clinical_v4.file_message("patient_information", info, five["hospital_id"], visit_id,
                                   patient, clinical_v4.read("patient_information", info), "t")
    if dx:
        rx = {**five, "diagnosis": [{"display": dx}], "issued_at": start}
        await clinical_v4.file_message("prescription", rx, five["hospital_id"], visit_id,
                                       patient, clinical_v4.read("prescription", rx), "t")


async def test_each_patient_is_one_row_with_their_latest_visit(dbs):
    api_v2.ctx.clinical = dbs.clinical
    try:
        patient = f"P{uuid.uuid4().hex[:7]}"
        await _visit(patient, "VIS-A-1", "2026-11-10", "2026-11-10T10:00:00+06:00",
                     age="6Y 1M", dx="Cough")
        await _visit(patient, "VIS-A-2", "2026-11-17", "2026-11-17T11:50:39+06:00",
                     age="6Y 2M", dx="Fever, unspecified")
        async with dbs.cli.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM by_visit.patient_list WHERE patient_id = $1",
                                    patient)
            cols = [r["column_name"] for r in await conn.fetch(
                "SELECT column_name FROM information_schema.columns WHERE table_schema = "
                "'by_visit' AND table_name = 'patient_list' ORDER BY ordinal_position")]
        assert len(rows) == 1
        row = rows[0]
        assert cols[:3] == ["clinic", "cmed_clinic_code", "patient_id"]
        assert (row["sex"], row["age"], row["blood_group"], row["visits"]) == (
            "female", "6Y 2M", "B+", 2)
        assert (str(row["first_visit"]), str(row["last_visit"]), row["last_visit_id"]) == (
            "2026-11-10", "2026-11-17", "VIS-A-2")
        assert row["last_diagnoses"] == "Fever, unspecified"
    finally:
        api_v2.ctx.clinical = None


async def test_the_reading_logins_start_in_the_readable_tables(pg, dbs):
    import asyncpg
    admin = await asyncpg.connect(pg.clinical)
    try:
        await admin.execute("""
            DO $$ BEGIN
                IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'aims_clinical_reader')
                THEN CREATE ROLE aims_clinical_reader LOGIN; END IF;
                IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'aims_clinical_writer')
                THEN CREATE ROLE aims_clinical_writer LOGIN; END IF;
            END $$;""")
    finally:
        await admin.close()
    await _role_setup(pg)

    def path_of(rows, role):
        for r in rows:
            if r["rolname"] == role:
                return [s for s in (r["setconfig"] or []) if s.startswith("search_path")]
        return []

    async with dbs.cli.acquire() as conn:
        rows = await conn.fetch("""
            SELECT r.rolname, s.setconfig FROM pg_roles r
              JOIN pg_db_role_setting s ON s.setrole = r.oid
              JOIN pg_database d ON d.oid = s.setdatabase AND d.datname = current_database()
        """)
    assert path_of(rows, "aims_clinical_reader") == ["search_path=by_visit, public"]
    assert path_of(rows, "aims_clinical_writer") == []          # the server keeps public
