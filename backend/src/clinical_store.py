"""
The clinical database, aims_clinical (SRS 3.2 §8.7.2).

Everything CMED sends lands in intake_records first, exactly as sent, and is
then loaded field by field into the tables (SRS-CRI-06). The two steps are
separate on purpose: a body is safe the moment it is received, and a load that
fails - a constraint, a value out of range - is recorded against the body and
tried again later rather than losing it (SRS-CRI-04).

This module is the only code that reads or writes patient names.
"""
from __future__ import annotations

import datetime
import decimal
import json
import logging
import uuid
from typing import Any, Dict, List, Optional, Tuple

import clinical_model as cm

logger = logging.getLogger(__name__)

_VISIT = """patient_id = $1 AND doctor_id = $2 AND cmed_hospital_id = $3
            AND start_time = $4 AND visit_date = $5"""


def _visit_args(visit) -> tuple:
    return (visit.patient_id, visit.doctor_id, visit.cmed_hospital_id,
            visit.start_time, visit.day)


def _plain(value):
    """Database values as JSON can carry them: no Decimal, no date objects."""
    if isinstance(value, decimal.Decimal):
        return float(value)
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, str):
        # JSONB columns come back as text from asyncpg unless a codec is set.
        return value
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    return value


def _row(row, *, drop: Tuple[str, ...] = ()) -> Dict[str, Any]:
    """One database row as a plain dictionary, empty fields left out."""
    if row is None:
        return {}
    skip = set(drop) | {"created_at", "updated_at"}
    out = {}
    for key, value in dict(row).items():
        if key in skip or value is None or value == {} or value == "":
            continue
        if key == "other" and isinstance(value, str):
            try:
                value = json.loads(value)
            except ValueError:
                pass
            if not value:
                continue
        out[key] = _plain(value)
    return out


class ClinicalStore:
    def __init__(self, pool, *, separate: bool = True):
        self._pool = pool
        # False only while aims_clinical has not been set up and the recordings
        # database stands in; the server logs that loudly at startup.
        self.separate = separate

    # ------------------------------------------------------------ intake

    async def receive(self, *, kind: str, visit, hospital_id: Optional[str],
                      body: Dict[str, Any], body_sha256: bytes) -> Tuple[int, bool, int]:
        """
        Keep one request, exactly as sent. Returns (id, already there, version).

        The same body twice is one row (SRS-CHB-07). A changed prescription for
        the same visit is the next version (SRS-CHB-08); a lock on the visit
        stops two arriving together from taking the same number.
        """
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                existing = await conn.fetchrow(
                    "SELECT id, version FROM intake_records WHERE kind = $1 AND body_sha256 = $2",
                    kind, body_sha256)
                if existing:
                    return int(existing["id"]), True, int(existing["version"])
                await conn.execute("SELECT pg_advisory_xact_lock(hashtext($1))",
                                   f"{kind}|{visit.patient_id}|{visit.start_time}")
                version = 1
                if kind == "prescription":
                    version = int(await conn.fetchval(
                        f"SELECT COALESCE(MAX(version), 0) + 1 FROM intake_records "
                        f"WHERE kind = 'prescription' AND {_VISIT}", *_visit_args(visit)))
                new_id = await conn.fetchval("""
                    INSERT INTO intake_records
                        (kind, cmed_hospital_id, hospital_id, patient_id, doctor_id,
                         start_time, visit_date, version, body, body_sha256)
                    VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
                    ON CONFLICT ON CONSTRAINT intake_once DO NOTHING
                    RETURNING id
                """, kind, visit.cmed_hospital_id, hospital_id, visit.patient_id,
                     visit.doctor_id, visit.start_time, visit.day, version,
                     json.dumps(body, ensure_ascii=False), body_sha256)
                if new_id is None:                  # arrived in between
                    row = await conn.fetchrow(
                        "SELECT id, version FROM intake_records "
                        "WHERE kind = $1 AND body_sha256 = $2", kind, body_sha256)
                    return int(row["id"]), True, int(row["version"])
                return int(new_id), False, version

    async def quarantine(self, *, kind: str, raw: str, problems: List[Dict[str, str]]) -> int:
        async with self._pool.acquire() as conn:
            return int(await conn.fetchval(
                "INSERT INTO intake_quarantine (kind, raw, problems) VALUES ($1,$2,$3) "
                "RETURNING id", kind, raw, json.dumps(problems, ensure_ascii=False)))

    # ------------------------------------------------------------ loading

    async def load(self, intake_id: int) -> bool:
        """Load one received body into the tables. False, and recorded, if it fails."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM intake_records WHERE id = $1", intake_id)
            if row is None:
                return False
            body = row["body"]
            if isinstance(body, str):
                body = json.loads(body)
            try:
                async with conn.transaction():
                    if row["kind"] == "patient_information":
                        await self._load_patient_information(conn, row, body)
                    else:
                        await self._load_prescription(conn, row, body)
                    await conn.execute(
                        "UPDATE intake_records SET loaded_at = now(), load_error = NULL "
                        "WHERE id = $1", intake_id)
                return True
            except Exception as exc:
                logger.warning("Could not load clinical record %s: %s", intake_id, exc)
                await conn.execute("UPDATE intake_records SET load_error = $2 WHERE id = $1",
                                   intake_id, str(exc)[:500])
                return False

    async def load_pending(self, limit: int = 50) -> int:
        """Retry bodies that have not loaded yet. Called by the sweep."""
        async with self._pool.acquire() as conn:
            ids = [r["id"] for r in await conn.fetch(
                "SELECT id FROM intake_records WHERE loaded_at IS NULL ORDER BY id LIMIT $1",
                limit)]
        loaded = 0
        for intake_id in ids:
            loaded += await self.load(intake_id)
        return loaded

    @staticmethod
    async def _ensure_patient(conn, patient_id: str, d: Optional[cm.Demographics]) -> None:
        if d is None:
            await conn.execute(
                "INSERT INTO patients (patient_id) VALUES ($1) ON CONFLICT DO NOTHING",
                patient_id)
            return
        # A known sex is kept; 'unknown' never overwrites one. A real change of
        # sex fails on the female/male tables' keys and is left for review.
        await conn.execute("""
            INSERT INTO patients (patient_id, sex, full_name, date_of_birth, phone, address)
            VALUES ($1,$2,$3,$4,$5,$6)
            ON CONFLICT (patient_id) DO UPDATE SET
                sex = CASE WHEN EXCLUDED.sex = 'unknown' THEN patients.sex
                           ELSE EXCLUDED.sex END,
                full_name = COALESCE(EXCLUDED.full_name, patients.full_name),
                date_of_birth = COALESCE(EXCLUDED.date_of_birth, patients.date_of_birth),
                phone = COALESCE(EXCLUDED.phone, patients.phone),
                address = COALESCE(EXCLUDED.address, patients.address)
        """, patient_id, d.sex, d.full_name, d.date_of_birth, d.phone, d.address)

    @staticmethod
    async def _encounter(conn, row, *, source: str, start_time: str, visit_date,
                         patient_information_id: Optional[int] = None) -> Any:
        return await conn.fetchval("""
            INSERT INTO encounters
                (patient_id, doctor_id, hospital_id, cmed_hospital_id, start_time,
                 visit_date, source, patient_information_id)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
            ON CONFLICT ON CONSTRAINT encounter_once DO UPDATE SET
                hospital_id = COALESCE(EXCLUDED.hospital_id, encounters.hospital_id),
                patient_information_id = COALESCE(EXCLUDED.patient_information_id,
                                                  encounters.patient_information_id)
            RETURNING encounter_id
        """, row["patient_id"], row["doctor_id"], row["hospital_id"],
             row["cmed_hospital_id"], start_time, visit_date, source,
             patient_information_id)

    async def _load_patient_information(self, conn, row, body: Dict[str, Any]) -> None:
        info = cm.patient_information(body)
        d = info.demographics
        await self._ensure_patient(conn, row["patient_id"], d)
        encounter = await self._encounter(conn, row, source="live",
                                          start_time=row["start_time"],
                                          visit_date=row["visit_date"],
                                          patient_information_id=row["id"])
        await conn.execute("""
            INSERT INTO encounter_demographics
                (encounter_id, full_name, sex, age_years, date_of_birth, phone, address)
            VALUES ($1,$2,$3,$4,$5,$6,$7)
            ON CONFLICT (encounter_id) DO UPDATE SET
                full_name = EXCLUDED.full_name, sex = EXCLUDED.sex,
                age_years = EXCLUDED.age_years, date_of_birth = EXCLUDED.date_of_birth,
                phone = EXCLUDED.phone, address = EXCLUDED.address
        """, encounter, d.full_name, d.sex, d.age_years, d.date_of_birth, d.phone, d.address)

        p = info.paramedic
        if p is not None:
            await conn.execute("""
                INSERT INTO paramedic_observations
                    (encounter_id, recorded_at, weight_kg, height_cm, blood_pressure,
                     systolic, diastolic, pulse_bpm, temperature_c, spo2_percent, notes, other)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)
                ON CONFLICT (encounter_id) DO UPDATE SET
                    recorded_at = EXCLUDED.recorded_at, weight_kg = EXCLUDED.weight_kg,
                    height_cm = EXCLUDED.height_cm, blood_pressure = EXCLUDED.blood_pressure,
                    systolic = EXCLUDED.systolic, diastolic = EXCLUDED.diastolic,
                    pulse_bpm = EXCLUDED.pulse_bpm, temperature_c = EXCLUDED.temperature_c,
                    spo2_percent = EXCLUDED.spo2_percent, notes = EXCLUDED.notes,
                    other = EXCLUDED.other
            """, encounter, p.recorded_at, p.weight_kg, p.height_cm, p.blood_pressure,
                 p.systolic, p.diastolic, p.pulse_bpm, p.temperature_c, p.spo2_percent,
                 p.notes, json.dumps(p.other, ensure_ascii=False))

        # One row in the table for the patient's sex; the database refuses the
        # other (SRS-DBA-06). The fields themselves are OD-15.
        if d.sex in ("female", "male"):
            table = f"{d.sex}_details"
            details = info.female_details if d.sex == "female" else info.male_details
            await conn.execute(f"""
                INSERT INTO {table} (encounter_id, patient_id, details)
                VALUES ($1, $2, $3)
                ON CONFLICT (encounter_id) DO UPDATE SET details = EXCLUDED.details
            """, encounter, row["patient_id"], json.dumps(details, ensure_ascii=False))

        if info.previous is not None:
            earlier = await self._encounter(
                conn, row, source="previous_visit",
                start_time=cm.previous_start_time(info.previous.visit_date, row["start_time"]),
                visit_date=info.previous.visit_date, patient_information_id=row["id"])
            await self._store_prescription(conn, earlier, info.previous.prescription,
                                           version=1, intake_id=row["id"])

    async def _load_prescription(self, conn, row, body: Dict[str, Any]) -> None:
        # A prescription may arrive before API 2 (SRS-CHB-09): the patient and
        # the visit are created now and filled in when API 2 lands.
        await self._ensure_patient(conn, row["patient_id"], None)
        encounter = await self._encounter(conn, row, source="live",
                                          start_time=row["start_time"],
                                          visit_date=row["visit_date"])
        await self._store_prescription(conn, encounter, cm.prescription(body),
                                       version=row["version"], intake_id=row["id"])

    @staticmethod
    async def _store_prescription(conn, encounter, p: cm.Prescription, *, version: int,
                                  intake_id: int) -> None:
        prescription_id = await conn.fetchval("""
            INSERT INTO prescriptions
                (encounter_id, version, issued_at, advice, follow_up_date, notes,
                 raw_json, intake_id)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
            ON CONFLICT ON CONSTRAINT prescription_version_once DO NOTHING
            RETURNING prescription_id
        """, encounter, version, p.issued_at, p.advice, p.follow_up_date, p.notes,
             json.dumps(p.raw, ensure_ascii=False), intake_id)
        if prescription_id is None:
            return                                  # already loaded
        await conn.executemany("""
            INSERT INTO prescription_items
                (prescription_id, line_no, drug, dose, frequency, duration, instructions)
            VALUES ($1,$2,$3,$4,$5,$6,$7)
        """, [(prescription_id, i.line_no, i.drug, i.dose, i.frequency, i.duration,
               i.instructions) for i in p.items])
        await conn.executemany(
            "INSERT INTO diagnoses (prescription_id, line_no, text) VALUES ($1,$2,$3)",
            [(prescription_id, n, t) for n, t in enumerate(p.diagnoses, start=1)])
        await conn.executemany(
            "INSERT INTO investigations (prescription_id, line_no, name) VALUES ($1,$2,$3)",
            [(prescription_id, n, t) for n, t in enumerate(p.investigations, start=1)])

    # ------------------------------------------------------------ linking

    async def link_session(self, visit, *, session_id: str, file_stem: Optional[str],
                           hospital_id: Optional[str]) -> bool:
        """Tie the visit to its recording by the shared file name (SRS-DBA-21)."""
        async with self._pool.acquire() as conn:
            linked = await conn.fetchval(f"""
                UPDATE encounters
                   SET session_id = $6, file_stem = $7,
                       hospital_id = COALESCE($8, hospital_id)
                 WHERE {_VISIT} AND source = 'live'
                RETURNING encounter_id
            """, *_visit_args(visit), session_id, file_stem, hospital_id)
        return linked is not None

    # ------------------------------------------------------------ refusal

    async def erase_visit(self, visit) -> int:
        """
        The patient refused (SRS-CNS-04): remove everything CMED sent for this
        visit, the visit, the older visits it brought with it, and the patient
        themselves if nothing else is held about them.
        """
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                intake_ids = [r["id"] for r in await conn.fetch(
                    f"SELECT id FROM intake_records WHERE {_VISIT}", *_visit_args(visit))]
                if intake_ids:
                    await conn.execute(
                        "DELETE FROM encounters WHERE source = 'previous_visit' "
                        "AND patient_information_id = ANY($1::bigint[])", intake_ids)
                await conn.execute(f"DELETE FROM encounters WHERE {_VISIT} AND source = 'live'",
                                   *_visit_args(visit))
                removed = await conn.execute(
                    f"DELETE FROM intake_records WHERE {_VISIT}", *_visit_args(visit))
                await conn.execute("""
                    DELETE FROM patients p WHERE p.patient_id = $1
                       AND NOT EXISTS (SELECT 1 FROM encounters e WHERE e.patient_id = p.patient_id)
                """, visit.patient_id)
        return int(removed.split()[-1]) if removed else 0

    # ------------------------------------------------------------ reading

    async def log_access(self, *, actor: str, action: str, patient_id: Optional[str],
                         detail: Optional[Dict[str, Any]] = None) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO clinical_access_log (actor, action, patient_id, detail) "
                "VALUES ($1,$2,$3,$4)", actor, action, patient_id,
                json.dumps(detail or {}, ensure_ascii=False))

    async def json_document(self, visit, *, actor: str) -> Dict[str, Any]:
        """
        Everything CMED holds about one visit, as the JSON that is written
        beside the recording and travels with it into the cloud copy
        (SRS 3.2 §8.8, SRS-CRI-05).

        Built from the loaded tables rather than the stored body, so it shows
        what the database actually holds - if a field was refused on load, it is
        missing here too, and the mismatch is visible instead of hidden.
        """
        await self.log_access(actor=actor, action="read_visit_json",
                              patient_id=visit.patient_id)
        async with self._pool.acquire() as conn:
            encounter = await conn.fetchrow(
                f"SELECT * FROM encounters WHERE {_VISIT} AND source = 'live'",
                *_visit_args(visit))
            if encounter is None:
                return {}
            eid = encounter["encounter_id"]
            patient = await conn.fetchrow(
                "SELECT patient_id, sex FROM patients WHERE patient_id = $1",
                visit.patient_id)
            demographics = await conn.fetchrow(
                "SELECT * FROM encounter_demographics WHERE encounter_id = $1", eid)
            paramedic = await conn.fetchrow(
                "SELECT * FROM paramedic_observations WHERE encounter_id = $1", eid)
            sex = (patient or {}).get("sex") or "unknown"
            details = None
            if sex in ("female", "male"):
                details = await conn.fetchval(
                    f"SELECT details FROM {sex}_details WHERE encounter_id = $1", eid)
            prescription = await self._prescription_json(conn, eid)
            earlier = await conn.fetchrow("""
                SELECT encounter_id, visit_date FROM encounters
                 WHERE patient_id = $1
                   AND (visit_date, start_time) < ($2::date, $3)
                 ORDER BY visit_date DESC, start_time DESC LIMIT 1
            """, visit.patient_id, visit.day, visit.start_time)
            previous = None
            if earlier is not None:
                previous = {
                    "date": _plain(earlier["visit_date"]),
                    "prescription": await self._prescription_json(
                        conn, earlier["encounter_id"]),
                }

        document: Dict[str, Any] = {
            "patient": {"patient_id": visit.patient_id, "sex": sex,
                        **_row(demographics, drop=("encounter_id", "sex"))},
            "visit": {"doctor_id": visit.doctor_id,
                      "hospital_id": encounter["hospital_id"],
                      "cmed_hospital_id": visit.cmed_hospital_id,
                      "start_time": visit.start_time,
                      "visit_date": _plain(visit.day)},
            "paramedic": _row(paramedic, drop=("encounter_id",)),
            "previous_visit": previous,
            "prescription": prescription,
        }
        if isinstance(details, str):
            try:
                details = json.loads(details)
            except ValueError:
                details = None
        if details:
            document[f"{sex}_details"] = _plain(details)
        return document

    @staticmethod
    async def _prescription_json(conn, encounter_id) -> Optional[Dict[str, Any]]:
        """The latest version for one encounter, with its medicines."""
        row = await conn.fetchrow(
            "SELECT * FROM prescriptions WHERE encounter_id = $1 "
            "ORDER BY version DESC LIMIT 1", encounter_id)
        if row is None:
            return None
        pid = row["prescription_id"]
        items = await conn.fetch(
            "SELECT line_no, drug, dose, frequency, duration, instructions "
            "FROM prescription_items WHERE prescription_id = $1 ORDER BY line_no", pid)
        diagnoses = await conn.fetch(
            "SELECT text FROM diagnoses WHERE prescription_id = $1 ORDER BY line_no", pid)
        investigations = await conn.fetch(
            "SELECT name FROM investigations WHERE prescription_id = $1 ORDER BY line_no",
            pid)
        return {
            "version": row["version"],
            "issued_at": _plain(row["issued_at"]),
            "advice": row["advice"],
            "follow_up_date": _plain(row["follow_up_date"]),
            "notes": row["notes"],
            "diagnoses": [d["text"] for d in diagnoses],
            "investigations": [i["name"] for i in investigations],
            "items": [_row(i) for i in items],
        }

    async def prescription_for(self, patient_id: str, *, which: str,
                               actor: str) -> Optional[Dict[str, Any]]:
        """current or previous prescription (SRS-DBA-08). Every read is logged."""
        view = {"current": "current_prescription",
                "previous": "previous_prescription"}[which]
        await self.log_access(actor=actor, action=f"read_{which}_prescription",
                              patient_id=patient_id)
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(f"SELECT * FROM {view} WHERE patient_id = $1",
                                      patient_id)
        return dict(row) if row else None


__all__ = ["ClinicalStore"]
