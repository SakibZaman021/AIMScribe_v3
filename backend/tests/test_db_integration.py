"""
The v3 server against real PostgreSQL 16.

Runs only where the `pgserver` package is installed (a portable PostgreSQL);
elsewhere it is skipped. It builds both databases from the migration scripts,
exactly as a deployment would, then exercises the repository and the clinical
store on them - so the SQL itself is tested, not a stand-in.

    python -m pytest tests/test_db_integration.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio

pgserver = pytest.importorskip("pgserver")
asyncpg = pytest.importorskip("asyncpg")

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND / "src"))
sys.path.insert(0, str(BACKEND.parent / "recorder"))

import api_v2                                  # noqa: E402
import clinical                                # noqa: E402
from clinical_store import ClinicalStore       # noqa: E402
from confirmation import Visit                 # noqa: E402
from db_v2 import V2Repository                 # noqa: E402

pytestmark = pytest.mark.asyncio

# Every v2 and v3 migration, in order, as a deployment applies them.
RECORDINGS_SCRIPTS = sorted((BACKEND / "scripts").glob("0[0-9][0-9]_*.sql"))

# What the live database looks like before them. It was built by v1 scripts
# that do not replay cleanly from an empty database (init_database.sql and
# migration 001 disagree about legacy tables), so the v1 tables the v2+
# migrations build on are stood up here as they are after migration 001.
V1_BASELINE = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id             VARCHAR(255) PRIMARY KEY,
    patient_id             VARCHAR(100) NOT NULL,
    doctor_id              VARCHAR(100) NOT NULL,
    hospital_id            VARCHAR(100) NOT NULL,
    status                 VARCHAR(20) DEFAULT 'active',
    total_clips            INTEGER DEFAULT 0,
    total_duration_seconds DECIMAL(10,2) DEFAULT 0,
    health_screening       JSONB,
    recording_date         DATE,
    start_time             TIME,
    end_time               TIME,
    created_at             TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    updated_at             TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    completed_at           TIMESTAMPTZ,
    ner_webhook_url        TEXT,
    status_webhook_url     TEXT
);
CREATE TABLE IF NOT EXISTS clips (
    id          SERIAL PRIMARY KEY,
    session_id  VARCHAR(255) REFERENCES sessions(session_id) ON DELETE CASCADE,
    clip_number INTEGER NOT NULL,
    object_key  VARCHAR(255) NOT NULL
);
CREATE TABLE IF NOT EXISTS patients (
    patient_id VARCHAR(100) PRIMARY KEY
);
"""
CLINICAL_SCRIPTS = sorted((BACKEND / "scripts" / "clinical").glob("001_*.sql"))


# The portable PostgreSQL used here ships without contrib extensions. 002 asks
# for pgcrypto but only uses gen_random_uuid(), built in since PostgreSQL 13;
# 011's trigram index needs pg_trgm, which Neon and any full install provide.
# Here, and only here, those statements are left out.
_PORTABLE_SKIPS = (
    "CREATE EXTENSION IF NOT EXISTS pgcrypto;",
    "CREATE EXTENSION IF NOT EXISTS pg_trgm;",
    "CREATE INDEX IF NOT EXISTS idx_sessions_file_stem_trgm\n"
    "    ON sessions USING gin (file_stem gin_trgm_ops);",
)


def _portable(sql: str) -> str:
    for statement in _PORTABLE_SKIPS:
        sql = sql.replace(statement, "-- (skipped on the portable test server)")
    return sql


async def _apply(uri, scripts):
    conn = await asyncpg.connect(uri)
    try:
        for script in scripts:
            try:
                await conn.execute(_portable(script.read_text(encoding="utf-8")))
            except Exception as exc:
                raise AssertionError(f"{script.name}: {exc}") from exc
    finally:
        await conn.close()


@pytest.fixture(scope="module")
def pg():
    server = pgserver.get_server(tempfile.mkdtemp(prefix="aimspg"), cleanup_mode="stop")
    base = server.get_uri()

    async def build():
        admin = await asyncpg.connect(base)
        for name in ("aims_recordings", "aims_clinical"):
            await admin.execute(f"DROP DATABASE IF EXISTS {name}")
            await admin.execute(f"CREATE DATABASE {name}")
        await admin.close()
        root = base.rsplit("/", 1)[0]
        recordings, clinical_uri = f"{root}/aims_recordings", f"{root}/aims_clinical"
        legacy = await asyncpg.connect(recordings)
        await legacy.execute(V1_BASELINE)
        await legacy.close()
        await _apply(recordings, RECORDINGS_SCRIPTS)
        await _apply(clinical_uri, CLINICAL_SCRIPTS)
        return recordings, clinical_uri

    recordings, clinical_uri = asyncio.run(build())
    yield SimpleNamespace(recordings=recordings, clinical=clinical_uri, server=server)


@pytest_asyncio.fixture
async def dbs(pg):
    rec = await asyncpg.create_pool(pg.recordings, min_size=1, max_size=4)
    cli = await asyncpg.create_pool(pg.clinical, min_size=1, max_size=4)
    yield SimpleNamespace(repo=V2Repository(rec), clinical=ClinicalStore(cli),
                          rec=rec, cli=cli)
    await rec.close()
    await cli.close()


_n = iter(range(10_000))


def visit(**changes) -> Visit:
    n = next(_n)
    base = dict(patient_id=f"P{n:05d}", doctor_id="DR0042", cmed_hospital_id="CMED-DHOLPUR",
                start_time=f"2026-09-13T10:{n % 60:02d}:32+06:00", visit_date="2026-09-13")
    base.update(changes)
    return Visit(**base)


async def enrolled_device(repo):
    await repo.upsert_hospital("HOSP003", "Dholpur", "Asia/Dhaka")
    await repo.set_cmed_hospital_id("HOSP003", "CMED-DHOLPUR")
    token = await repo.create_enrollment_token(hospital_id="HOSP003", doctor_id="",
                                               created_by="tests")
    enrolled = await repo.enroll_device(token=token, tpm_pubkey=os.urandom(32),
                                        machine_name="PC", os_version="", app_version="3",
                                        protocol_version=2)
    return await repo.device_by_token(enrolled["device_token"])


async def a_session(repo, device, *, opened_ago=timedelta(seconds=5)):
    from core.spool import new_ulid
    sid = new_ulid()
    opened = datetime.now(timezone.utc) - opened_ago
    async with repo._pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO sessions (session_id, patient_id, doctor_id, hospital_id, device_id,
                                  protocol_version, status, session_date, opened_at)
            VALUES ($1, 'P1', 'DR0042', 'HOSP003', $2, 2, 'active', $3, $4)
        """, sid, device["device_id"], opened.date(), opened)
    return sid


# ============================================================
# The migrations
# ============================================================

async def test_migrations_can_run_again(pg):
    """A deployment re-running the v3 scripts changes nothing and fails nothing."""
    v3 = [p for p in RECORDINGS_SCRIPTS if p.name.startswith(("010_", "011_", "012_"))]
    await _apply(pg.recordings, v3)
    await _apply(pg.clinical, CLINICAL_SCRIPTS)


async def test_no_patient_body_in_the_recordings_database(dbs):
    """SRS-DBA-20: the recordings side has the five fields, never a body or a name."""
    async with dbs.rec.acquire() as conn:
        columns = {r["column_name"] for r in await conn.fetch(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'confirmation_notices'")}
        tables = {r["table_name"] for r in await conn.fetch(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'")}
    assert "body" not in columns and "clinical_records" not in tables


# ============================================================
# Recordings: grants, notices, confirmation, refusal
# ============================================================

async def test_a_notice_confirms_one_grant_once(dbs):
    repo = dbs.repo
    device = await enrolled_device(repo)
    v = visit()
    now = datetime.now(timezone.utc)
    await repo.create_authorisation(jti="j1", device_id=device["device_id"],
                                    hospital_id="HOSP003", visit=v, expires_at=now)
    notice = await repo.record_notice(v, hospital_id="HOSP003", clinical_record_id=7)
    found = await repo.find_unclaimed_notice(v, hospital_id="HOSP003",
                                             since=now - timedelta(minutes=5))
    assert found == notice
    assert await repo.claim_notice(notice, "j1") is True
    assert await repo.claim_notice(notice, "j1") is False
    assert await repo.find_unclaimed_notice(v, hospital_id="HOSP003",
                                            since=now - timedelta(minutes=5)) is None
    assert (await repo.get_authorisation("j1"))["notice_id"] == notice


async def test_the_whole_server_flow_on_postgres(dbs):
    """API 2 arrives, the recorder asks, the grant comes back confirmed."""
    repo = dbs.repo
    device = await enrolled_device(repo)
    key = await repo.create_cmed_key(label="cmed", created_by="tests", expires_at=None)
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from grants import GrantIssuer
    api_v2.ctx.repo, api_v2.ctx.clinical = repo, dbs.clinical
    api_v2.ctx.grants = GrantIssuer(Ed25519PrivateKey.generate())
    try:
        v = visit()
        body = {"patient_id": v.patient_id, "doctor_id": v.doctor_id,
                "hospital_id": v.cmed_hospital_id, "start_time": v.start_time,
                "date": v.visit_date,
                "demographics": {"name": "Test Patient", "sex": "male", "age_years": 51},
                "paramedic": {"blood_pressure": "140/90"}, "previous_visit": None}

        class Request:
            headers = {"x-cmed-key": key}

            async def body(self):
                return json.dumps(body).encode()

        reply = await clinical.receive(Request(), "patient_information")
        assert reply.status_code == 202
        again = await clinical.receive(Request(), "patient_information")
        assert json.loads(again.body)["code"] == "ALREADY_RECEIVED"

        minted = await api_v2.mint_grant({"patient_id": v.patient_id, "doctor_id": v.doctor_id,
                                          "hospital_id": v.cmed_hospital_id,
                                          "start_time": v.start_time, "date": v.visit_date},
                                         device=device)
        assert minted["confirmation"] == "confirmed" and minted["hospital_id"] == "HOSP003"

        async with dbs.cli.acquire() as conn:
            patient = await conn.fetchrow("SELECT * FROM patients WHERE patient_id = $1",
                                          v.patient_id)
            obs = await conn.fetchrow(
                "SELECT o.systolic FROM paramedic_observations o JOIN encounters e "
                "USING (encounter_id) WHERE e.patient_id = $1", v.patient_id)
            male = await conn.fetchval(
                "SELECT count(*) FROM male_details WHERE patient_id = $1", v.patient_id)
        assert (patient["full_name"], patient["sex"], obs["systolic"], male) == (
            "Test Patient", "male", 140, 1)
    finally:
        api_v2.ctx.repo = api_v2.ctx.clinical = api_v2.ctx.grants = None


async def test_confirmation_deadlines_and_sweep(dbs):
    repo = dbs.repo
    device = await enrolled_device(repo)
    api_v2.ctx.repo, api_v2.ctx.clinical = repo, dbs.clinical
    api_v2.ctx.minio = SimpleNamespace(bucket="b", client=SimpleNamespace(
        remove_object=lambda b, k: None))
    try:
        slow = await a_session(repo, device, opened_ago=timedelta(minutes=3))
        old = await a_session(repo, device, opened_ago=timedelta(hours=25))
        for sid in (slow, old):
            await repo.set_session_confirmation(sid, "pending", grant_jti=None)
        result = await api_v2.maintenance_sweep()
        assert (result["erased"], result["marked_unconfirmed"]) == (1, 1)
        assert (await repo.session_confirmation(slow))["confirmation"] == "unconfirmed"
        erased = await repo.get_session(old)
        assert (erased["confirmation"], erased["patient_id"]) == ("expired", "REDACTED")
    finally:
        api_v2.ctx.repo = api_v2.ctx.clinical = api_v2.ctx.minio = None


async def test_refusals_are_remembered_by_digest(dbs):
    repo = dbs.repo
    device = await enrolled_device(repo)
    v = visit()
    assert await repo.record_refusal(session_id="01TESTREFUSAL0000000000000",
                                     device_id=device["device_id"], hospital_id="HOSP003",
                                     doctor_id="DR0042", visit_sha256=v.digest())
    assert not await repo.record_refusal(session_id="01TESTREFUSAL0000000000000",
                                         device_id=device["device_id"], hospital_id="HOSP003",
                                         doctor_id="DR0042", visit_sha256=v.digest())
    assert await repo.refused_visit(v.digest())


async def test_file_names_are_unique(dbs):
    """SRS-DBA-25: two recordings closing in one second do not share a name."""
    repo = dbs.repo
    device = await enrolled_device(repo)
    first, second = await a_session(repo, device), await a_session(repo, device)
    stem = "P1_DR0042_HOSP003_101432_102847_20260913"
    t = datetime(2026, 9, 13, 10, 14, 32).time()
    assert await repo.set_file_names(first, file_stem=stem, local_start=t, local_end=t) == stem
    other = await repo.set_file_names(second, file_stem=stem, local_start=t, local_end=t)
    assert other == f"{stem}_{second[-5:]}"
    # Searching by part of the name (SRS-DBA-24; the trigram index that makes it
    # fast is skipped on this portable server).
    async with dbs.rec.acquire() as conn:
        found = await conn.fetchval(
            "SELECT count(*) FROM sessions WHERE file_stem ILIKE $1", "%HOSP003_1014%")
    assert found >= 2


async def test_cmed_keys(dbs):
    repo = dbs.repo
    key = await repo.create_cmed_key(label="rotate-me", created_by="tests", expires_at=None)
    assert await repo.cmed_key_valid(clinical.key_digest(key)) == "rotate-me"
    assert await repo.revoke_cmed_keys("rotate-me") == 1
    assert await repo.cmed_key_valid(clinical.key_digest(key)) is None


# ============================================================
# aims_clinical
# ============================================================

def _api2(v, **extra):
    return {"patient_id": v.patient_id, "doctor_id": v.doctor_id,
            "hospital_id": v.cmed_hospital_id, "start_time": v.start_time,
            "date": v.visit_date, "previous_visit": None, **extra}


async def _send(store, kind, v, body):
    import integrity
    rid, dup, version = await store.receive(
        kind=kind, visit=v, hospital_id="HOSP003", body=body,
        body_sha256=integrity.sha256_bytes(integrity.canonical_json(body)))
    if not dup:
        assert await store.load(rid), "load failed"
    return rid, dup, version


async def test_intake_is_kept_once_and_prescriptions_are_versioned(dbs):
    """SRS-CHB-07, -08."""
    v = visit()
    rx = {"issued_at": "2026-09-13T10:26:11+06:00", "items": [{"drug": "A"}]}
    assert (await _send(dbs.clinical, "prescription", v, rx))[1:] == (False, 1)
    assert (await _send(dbs.clinical, "prescription", v, rx))[1:] == (True, 1)
    changed = dict(rx, advice="rest")
    assert (await _send(dbs.clinical, "prescription", v, changed))[1:] == (False, 2)


async def test_a_prescription_can_arrive_first(dbs):
    """SRS-CHB-09 / AT-70."""
    v = visit()
    await _send(dbs.clinical, "prescription", v, {"items": [{"drug": "Amlodipine"}],
                                                   "diagnoses": ["Hypertension"]})
    await _send(dbs.clinical, "patient_information", v,
                _api2(v, demographics={"name": "Late", "sex": "female"}))
    async with dbs.cli.acquire() as conn:
        rows = await conn.fetch(
            "SELECT e.encounter_id, i.drug, p.sex FROM encounters e "
            "JOIN patients p USING (patient_id) "
            "JOIN prescriptions r USING (encounter_id) "
            "JOIN prescription_items i USING (prescription_id) "
            "WHERE e.patient_id = $1", v.patient_id)
    assert [(r["drug"], r["sex"]) for r in rows] == [("Amlodipine", "female")]


async def test_the_database_refuses_the_wrong_sex(dbs):
    """AT-54 / SRS-DBA-06: enforced by the database, whatever the application does."""
    v = visit()
    await _send(dbs.clinical, "patient_information", v,
                _api2(v, demographics={"sex": "male"}))
    async with dbs.cli.acquire() as conn:
        encounter = await conn.fetchval(
            "SELECT encounter_id FROM encounters WHERE patient_id = $1", v.patient_id)
        with pytest.raises(asyncpg.ForeignKeyViolationError):
            await conn.execute(
                "INSERT INTO female_details (encounter_id, patient_id) VALUES ($1, $2)",
                encounter, v.patient_id)


async def test_current_and_previous_prescription(dbs):
    """AT-52, AT-53 / SRS-DBA-08, -09: every visit kept; previous is strictly before."""
    store = dbs.clinical
    patient = visit().patient_id
    days = ["2026-06-02", "2026-08-01", "2026-09-13"]
    for n, day in enumerate(days):
        v = Visit(patient_id=patient, doctor_id="DR0042", cmed_hospital_id="CMED-DHOLPUR",
                  start_time=f"{day}T10:00:00+06:00", visit_date=day)
        await _send(store, "patient_information", v, _api2(v, demographics={"sex": "female"}))
        await _send(store, "prescription", v, {"items": [{"drug": f"Drug{n}"}]})
    # A second visit the same day, still in progress: no prescription yet.
    today = Visit(patient_id=patient, doctor_id="DR0042", cmed_hospital_id="CMED-DHOLPUR",
                  start_time="2026-09-13T15:00:00+06:00", visit_date="2026-09-13")
    await _send(store, "patient_information", today, _api2(today, demographics={"sex": "female"}))

    current = await store.prescription_for(patient, which="current", actor="tests")
    previous = await store.prescription_for(patient, which="previous", actor="tests")
    assert current is None                       # the visit in progress has none yet
    assert previous["raw_json"] and "Drug2" in previous["raw_json"]
    async with dbs.cli.acquire() as conn:
        kept = await conn.fetchval(
            "SELECT count(*) FROM prescriptions r JOIN encounters e USING (encounter_id) "
            "WHERE e.patient_id = $1", patient)
        reads = await conn.fetchval(
            "SELECT count(*) FROM clinical_access_log WHERE patient_id = $1", patient)
    assert (kept, reads) == (3, 2)


async def test_previous_visit_from_api2_is_kept(dbs):
    v = visit()
    await _send(dbs.clinical, "patient_information", v, _api2(
        v, demographics={"sex": "male"},
        previous_visit={"date": "2026-06-02", "diagnoses": ["Gastritis"],
                        "prescription": {"items": [{"drug": "Omeprazole"}]}}))
    previous = await dbs.clinical.prescription_for(v.patient_id, which="previous",
                                                   actor="tests")
    assert previous["source"] == "previous_visit"
    assert previous["visit_date"] == date(2026, 6, 2)


async def test_a_failed_load_is_kept_and_retried(dbs):
    """SRS-CRI-04: the body is safe; the load is recorded and tried again."""
    import integrity
    v = visit()
    await _send(dbs.clinical, "patient_information", v, _api2(v, demographics={"sex": "female"}))
    # The same patient now said to be male: the female table's key refuses it.
    body = _api2(v, demographics={"sex": "male"}, paramedic={"notes": "changed"})
    rid, _, _ = await dbs.clinical.receive(
        kind="patient_information", visit=v, hospital_id="HOSP003", body=body,
        body_sha256=integrity.sha256_bytes(integrity.canonical_json(body)))
    assert await dbs.clinical.load(rid) is False
    async with dbs.cli.acquire() as conn:
        row = await conn.fetchrow("SELECT loaded_at, load_error FROM intake_records "
                                  "WHERE id = $1", rid)
    assert row["loaded_at"] is None and row["load_error"]


async def test_refusal_erases_the_visit_and_the_patient(dbs):
    """SRS-CNS-04."""
    v = visit()
    await _send(dbs.clinical, "patient_information", v, _api2(v, demographics={"name": "X"}))
    await _send(dbs.clinical, "prescription", v, {"items": [{"drug": "A"}]})
    assert await dbs.clinical.erase_visit(v) == 2
    async with dbs.cli.acquire() as conn:
        left = await conn.fetchval(
            "SELECT (SELECT count(*) FROM patients WHERE patient_id = $1)"
            " + (SELECT count(*) FROM encounters WHERE patient_id = $1)"
            " + (SELECT count(*) FROM intake_records WHERE patient_id = $1)", v.patient_id)
    assert left == 0


async def test_linking_by_the_shared_file_name(dbs):
    v = visit()
    await _send(dbs.clinical, "patient_information", v, _api2(v, demographics={}))
    assert await dbs.clinical.link_session(v, session_id="01S", file_stem="P_D_H_1_2_3",
                                           hospital_id="HOSP003")
    async with dbs.cli.acquire() as conn:
        assert await conn.fetchval("SELECT file_stem FROM encounters WHERE patient_id = $1",
                                   v.patient_id) == "P_D_H_1_2_3"


async def test_the_access_log_cannot_be_edited(dbs):
    """SRS-DBA-18."""
    await dbs.clinical.log_access(actor="tests", action="read", patient_id="P1")
    async with dbs.cli.acquire() as conn:
        with pytest.raises(asyncpg.RaiseError):
            await conn.execute("UPDATE clinical_access_log SET actor = 'someone else'")
        with pytest.raises(asyncpg.RaiseError):
            await conn.execute("DELETE FROM clinical_access_log")


# ============================================================
# The v3 endpoints end to end on PostgreSQL
# ============================================================

async def _linked_session(dbs, device, v):
    """A recording opened under a grant, with CMED's API 2 stored and matched."""
    repo = dbs.repo
    sid = await a_session(repo, device)
    now = datetime.now(timezone.utc)
    jti = f"jti-{sid}"
    await repo.create_authorisation(jti=jti, device_id=device["device_id"],
                                    hospital_id="HOSP003", visit=v, expires_at=now)
    await repo.link_authorisation(jti, sid)
    rid, _, _ = await _send(dbs.clinical, "patient_information", v,
                            _api2(v, demographics={"name": "Linked", "sex": "female"}))
    notice = await repo.record_notice(v, hospital_id="HOSP003", clinical_record_id=rid)
    await repo.claim_notice(notice, jti)
    await repo.set_session_confirmation(sid, "confirmed", grant_jti=jti)
    return sid


def _server(dbs):
    removed = []
    api_v2.ctx.repo, api_v2.ctx.clinical = dbs.repo, dbs.clinical
    api_v2.ctx.minio = SimpleNamespace(bucket="b", client=SimpleNamespace(
        remove_object=lambda b, k: removed.append(k)))
    # The copy store, as the server sees it: it hands out one address to put an
    # object, and answers what it holds. Whatever is asked for is held here.
    api_v2.ctx.copy = SimpleNamespace(
        bucket="copies",
        get_presigned_upload_url=lambda key, expires: f"https://copies/put/{key}",
        client=SimpleNamespace(stat_object=lambda bucket, key: SimpleNamespace(
            size=900_000, etag="ab" * 16)))
    api_v2.ctx.copy_json = None
    return removed


def _unserve():
    api_v2.ctx.repo = api_v2.ctx.clinical = api_v2.ctx.minio = api_v2.ctx.copy = None
    api_v2.ctx.copy_json = None


async def test_refusal_endpoint_on_postgres(dbs):
    """AT-03, server side, with the real audit chain."""
    device = await enrolled_device(dbs.repo)
    v = visit(patient_id="PREFUSE1")
    sid = await _linked_session(dbs, device, v)
    _server(dbs)
    try:
        result = await api_v2.refuse_session(api_v2.RefuseRequest(session_id=sid),
                                             device=device)
        assert result["status"] == "refused"
        session = await dbs.repo.get_session(sid)
        assert (session["confirmation"], session["patient_id"]) == ("refused", "REDACTED")
        assert (await dbs.repo.refusal(sid))["completed_at"] is not None
        assert await dbs.repo.refused_visit(v.digest())
        async with dbs.rec.acquire() as conn:
            notices = await conn.fetchval(
                "SELECT count(*) FROM confirmation_notices WHERE patient_id = $1", v.patient_id)
            audit = await conn.fetchval(
                "SELECT detail::text FROM audit_log WHERE session_id = $1 "
                "AND event_type = 'session.refused'", sid)
        async with dbs.cli.acquire() as conn:
            clinical_left = await conn.fetchval(
                "SELECT count(*) FROM intake_records WHERE patient_id = $1", v.patient_id)
        assert (notices, clinical_left) == (0, 0)
        assert audit and "PREFUSE1" not in audit          # SRS-CNS-06
    finally:
        _unserve()


async def test_closing_names_the_recording_and_links_the_visit(dbs):
    """SRS-SES-05, SRS-DBA-21, SRS-DBA-25."""
    device = await enrolled_device(dbs.repo)
    v = visit(patient_id="PNAME1")
    sid = await _linked_session(dbs, device, v)
    opened = datetime(2026, 9, 13, 4, 14, 32, tzinfo=timezone.utc)      # 10:14:32 Dhaka
    async with dbs.rec.acquire() as conn:
        await conn.execute("UPDATE sessions SET patient_id = $2, opened_at = $3, "
                           "closed_at = $4 WHERE session_id = $1",
                           sid, v.patient_id, opened, opened + timedelta(minutes=14, seconds=15))
    _server(dbs)
    try:
        stem = await api_v2._record_file_name(sid)
    finally:
        _unserve()
    assert stem == "PNAME1_DR0042_HOSP003_101432_102847_20260913"
    async with dbs.rec.acquire() as conn:
        row = await conn.fetchrow("SELECT file_stem, local_start, local_end FROM sessions "
                                  "WHERE session_id = $1", sid)
    async with dbs.cli.acquire() as conn:
        encounter = await conn.fetchrow(
            "SELECT file_stem, session_id FROM encounters WHERE patient_id = $1 "
            "AND source = 'live'", v.patient_id)
    assert (row["file_stem"], str(row["local_start"]), str(row["local_end"])) == (
        stem, "10:14:32", "10:28:47")
    assert (encounter["file_stem"], encounter["session_id"]) == (stem, sid)


async def test_the_archive_list_runs_and_skips_unconfirmed(dbs):
    """The archive worker's query, with the confirmed-only filter (SRS §5.6)."""
    pending = await dbs.repo.pending_archive(10)
    assert all(p.get("session_id") for p in pending)


# ============================================================
# The cloud copy (SRS-ARC-08..13)
# ============================================================

async def _archived(dbs, device, v, *, stem):
    """A confirmed recording, closed and archived."""
    sid = await _linked_session(dbs, device, v)
    async with dbs.rec.acquire() as conn:
        await conn.execute("UPDATE sessions SET patient_id = $2, closed_at = now(), "
                           "file_stem = $3 WHERE session_id = $1",
                           sid, v.patient_id, stem)
    await dbs.repo.mark_archived(sid, relpath=f"HOSP003/DR0042/2026-09-13/{stem}/{stem}.wav",
                                 sha256=b"\x11" * 32, byte_length=1_500_044)
    return sid


async def test_an_archived_recording_waits_for_its_copy(dbs):
    """SRS-ARC-08: archived is not finished; the pieces wait for the copy."""
    device = await enrolled_device(dbs.repo)
    v = visit(patient_id="PCOPY1")
    stem = "PCOPY1_DR0042_HOSP003_101432_102847_20260913"
    sid = await _archived(dbs, device, v, stem=stem)

    waiting = await dbs.repo.pending_copy(10)
    assert sid in [w["session_id"] for w in waiting]
    assert next(w for w in waiting if w["session_id"] == sid)["file_stem"] == stem

    key = f"copies/HOSP003/DR0042/2026-09-13/{stem}.v1.flac.enc"
    assert await dbs.repo.next_copy_version(sid, "audio") == 1
    await dbs.repo.record_cloud_copy(
        session_id=sid, kind="audio", object_key=key, version=1,
        byte_length=900_000, sha256=b"\x22" * 32, plain_sha256=b"\x33" * 32)
    await dbs.repo.record_cloud_copy(
        session_id=sid, kind="json", object_key=key.replace("flac", "json"), version=1,
        byte_length=2048, sha256=b"\x44" * 32, plain_sha256=b"\x55" * 32)
    await dbs.repo.mark_copied(sid)
    await dbs.repo.mark_segments_deleted(sid)

    assert sid not in [w["session_id"] for w in await dbs.repo.pending_copy(10)]
    copies = await dbs.repo.copies_for(sid)
    assert [c["kind"] for c in copies] == ["audio", "json"]
    assert bytes(copies[0]["sha256"]) == b"\x22" * 32
    assert all(c["verified_at"] is not None for c in copies)
    session = await dbs.repo.get_session(sid)
    assert session["copied_at"] is not None


async def test_the_same_object_recorded_twice_is_one_row(dbs):
    """AT-65: the retry after an interrupted run completes."""
    device = await enrolled_device(dbs.repo)
    v = visit(patient_id="PCOPY2")
    stem = "PCOPY2_DR0042_HOSP003_101432_102847_20260913"
    sid = await _archived(dbs, device, v, stem=stem)
    key = f"copies/{stem}.v1.flac.enc"

    first = await dbs.repo.record_cloud_copy(
        session_id=sid, kind="audio", object_key=key, version=1, byte_length=900_000,
        sha256=b"\x22" * 32, plain_sha256=None)
    again = await dbs.repo.record_cloud_copy(
        session_id=sid, kind="audio", object_key=key, version=1, byte_length=900_000,
        sha256=b"\x22" * 32, plain_sha256=b"\x33" * 32)
    assert first == again
    assert len(await dbs.repo.copies_for(sid)) == 1


async def test_a_late_prescription_marks_the_json_and_a_new_version_follows(dbs):
    """SRS-ARC-13: the JSON is written again; earlier objects are kept."""
    device = await enrolled_device(dbs.repo)
    v = visit(patient_id="PCOPY3")
    stem = "PCOPY3_DR0042_HOSP003_101432_102847_20260913"
    sid = await _archived(dbs, device, v, stem=stem)
    await dbs.repo.record_cloud_copy(
        session_id=sid, kind="json", object_key=f"copies/{stem}.v1.json.enc", version=1,
        byte_length=2048, sha256=b"\x44" * 32, plain_sha256=None)

    found = await dbs.repo.session_for_visit(v)
    assert found["session_id"] == sid and found["archived_at"] is not None
    assert await dbs.repo.mark_json_stale(sid)
    assert sid in [p["session_id"] for p in await dbs.repo.pending_json(10)]

    assert await dbs.repo.next_copy_version(sid, "json") == 2
    await dbs.repo.record_cloud_copy(
        session_id=sid, kind="json", object_key=f"copies/{stem}.v2.json.enc", version=2,
        byte_length=2500, sha256=b"\x66" * 32, plain_sha256=None)
    await dbs.repo.clear_json_stale(sid)

    assert await dbs.repo.pending_json(10) == []
    assert [c["version"] for c in await dbs.repo.copies_for(sid)] == [1, 2]


async def test_a_recording_never_archived_is_never_waiting_for_a_copy(dbs):
    """SRS-ARC-16: unconfirmed and refused recordings have nothing to copy."""
    device = await enrolled_device(dbs.repo)
    open_only = await a_session(dbs.repo, device)
    assert open_only not in [w["session_id"] for w in await dbs.repo.pending_copy(50)]


async def test_the_json_document_is_what_cmed_holds_about_the_visit(dbs):
    """§8.8, SRS-CRI-05: the file that travels with the audio."""
    v = visit(patient_id="PJSON1")
    earlier = visit(patient_id="PJSON1", start_time="2026-06-02T09:00:00+06:00",
                    visit_date="2026-06-02")
    await _send(dbs.clinical, "patient_information", earlier,
                _api2(earlier, demographics={"name": "Ayesha Rahman", "sex": "female"}))
    await _send(dbs.clinical, "prescription", earlier,
                {"issued_at": "2026-06-02T09:20:00+06:00", "diagnoses": ["Gastritis"],
                 "items": [{"drug": "Omeprazole", "dose": "20 mg"}]})
    await _send(dbs.clinical, "patient_information", v, _api2(
        v, demographics={"name": "Ayesha Rahman", "sex": "female", "age_years": 34},
        paramedic={"blood_pressure": "120/80", "pulse_bpm": 78, "blood_sugar": 6.1}))
    await _send(dbs.clinical, "prescription", v,
                {"issued_at": "2026-09-13T10:26:11+06:00", "diagnoses": ["Hypertension"],
                 "investigations": ["ECG"], "follow_up": "2026-10-13",
                 "items": [{"drug": "Amlodipine", "dose": "5 mg", "frequency": "1+0+0"},
                           {"drug": "Aspirin"}]})

    document = await dbs.clinical.json_document(v, actor="archive-worker")

    assert document["patient"]["full_name"] == "Ayesha Rahman"
    assert document["patient"]["sex"] == "female"
    assert document["patient"]["age_years"] == 34
    assert document["visit"]["start_time"] == v.start_time
    assert document["paramedic"]["blood_pressure"] == "120/80"
    assert document["paramedic"]["pulse_bpm"] == 78
    assert document["paramedic"]["other"] == {"blood_sugar": 6.1}      # OD-16
    assert [i["drug"] for i in document["prescription"]["items"]] == ["Amlodipine",
                                                                      "Aspirin"]
    assert document["prescription"]["diagnoses"] == ["Hypertension"]
    assert document["prescription"]["follow_up_date"] == "2026-10-13"
    assert document["previous_visit"]["date"] == "2026-06-02"
    assert [i["drug"] for i in document["previous_visit"]["prescription"]["items"]] \
        == ["Omeprazole"]
    # Read, and recorded as read (SRS-DBA-19).
    async with dbs.cli.acquire() as conn:
        assert await conn.fetchval(
            "SELECT count(*) FROM clinical_access_log WHERE patient_id = $1 "
            "AND action = 'read_visit_json'", v.patient_id) == 1


async def test_the_document_carries_the_latest_prescription_only(dbs):
    v = visit(patient_id="PJSON2")
    await _send(dbs.clinical, "patient_information", v,
                _api2(v, demographics={"name": "Kamal", "sex": "male"}))
    rx = {"issued_at": "2026-09-13T10:26:11+06:00", "items": [{"drug": "First"}]}
    await _send(dbs.clinical, "prescription", v, rx)
    await _send(dbs.clinical, "prescription", v, dict(rx, items=[{"drug": "Second"}]))

    document = await dbs.clinical.json_document(v, actor="archive-worker")
    assert document["prescription"]["version"] == 2
    assert [i["drug"] for i in document["prescription"]["items"]] == ["Second"]
    assert document["previous_visit"] is None


async def test_a_visit_cmed_never_described_gives_an_empty_document(dbs):
    assert await dbs.clinical.json_document(visit(patient_id="PJSON3"),
                                            actor="archive-worker") == {}


async def test_the_server_assembles_the_file_beside_the_audio(dbs):
    """The whole §8.8 document, both halves, through the endpoint."""
    device = await enrolled_device(dbs.repo)
    v = visit(patient_id="PJSON4")
    stem = "PJSON4_DR0042_HOSP003_101432_102847_20260913"
    sid = await _archived(dbs, device, v, stem=stem)
    await _send(dbs.clinical, "prescription", v,
                {"issued_at": "2026-09-13T10:26:11+06:00",
                 "items": [{"drug": "Amlodipine"}]})
    _server(dbs)
    try:
        document = await api_v2.archive_clinical_document(sid)
    finally:
        _unserve()

    assert document["file_stem"] == stem
    assert document["patient"]["patient_id"] == "PJSON4"
    assert document["recording"]["session_id"] == sid
    assert document["recording"]["confirmation"] == "confirmed"
    assert document["recording"]["audio_sha256"] == ("11" * 32)
    assert document["prescription"]["items"][0]["drug"] == "Amlodipine"


async def test_the_sweep_notices_a_copy_that_never_happened(dbs):
    """SRS-ARC-10, SRS-ARC-14: a step failing every pass becomes visible."""
    device = await enrolled_device(dbs.repo)
    v = visit(patient_id="PSTUCK1")
    stem = "PSTUCK1_DR0042_HOSP003_101432_102847_20260913"
    sid = await _archived(dbs, device, v, stem=stem)
    async with dbs.rec.acquire() as conn:
        await conn.execute("UPDATE sessions SET archived_at = now() - interval '2 days' "
                           "WHERE session_id = $1", sid)

    assert sid in [r["session_id"] for r in await dbs.repo.copies_overdue(hours=24)]
    _server(dbs)
    try:
        result = await api_v2.maintenance_sweep()
    finally:
        _unserve()
    assert result["copies_overdue"] >= 1
    async with dbs.rec.acquire() as conn:
        assert await conn.fetchval(
            "SELECT count(*) FROM integrity_alerts WHERE session_id = $1 "
            "AND alert_type = 'cloud_copy_overdue'", sid) == 1

    # Once copied, it stops being reported.
    await dbs.repo.record_cloud_copy(
        session_id=sid, kind="audio", object_key=f"copies/{stem}.v9.flac.enc", version=9,
        byte_length=1, sha256=bytes.fromhex("77") * 32, plain_sha256=None)
    await dbs.repo.mark_copied(sid)
    assert sid not in [r["session_id"] for r in await dbs.repo.copies_overdue(hours=24)]


async def test_the_copy_endpoints_run_on_postgres(dbs):
    """/archive/copy/authorize and /complete against the real tables."""
    device = await enrolled_device(dbs.repo)
    v = visit(patient_id="PCOPY9")
    stem = "PCOPY9_DR0042_HOSP003_101432_102847_20260913"
    sid = await _archived(dbs, device, v, stem=stem)
    digest = bytes.fromhex("99") * 32
    async with dbs.rec.acquire() as conn:
        await conn.execute("""
            INSERT INTO chain_entries (session_id, entry_no, entry_type, payload,
                                       payload_sha256, prev_hash, entry_hash,
                                       device_signature, occurred_at)
            VALUES ($1, 1, 'segment', '{}', $2, $2, $2, $2, now())
        """, sid, digest)
        await conn.execute("""
            INSERT INTO segments (session_id, seq_no, entry_no, object_key, sha256,
                                  bytes, duration_seconds, state)
            VALUES ($1, 1, 1, $2, $3, 100, 1.0, 'committed')
        """, sid, f"audio/{sid}/seg_00001.wav", digest)

    removed = _server(dbs)
    try:
        place = await api_v2.archive_copy_authorize(
            api_v2.CopyAuthorizeRequest(session_id=sid, kind="audio"))
        assert place["object_key"].endswith(f"{stem}.v1.flac.enc")
        result = await api_v2.archive_copy_complete(api_v2.CopyCompleteRequest(
            session_id=sid, copies=[{
                "kind": "audio", "object_key": place["object_key"], "version": 1,
                "bytes": 900_000, "sha256": "cd" * 32, "plain_sha256": "ef" * 32,
                "md5": "ab" * 16}]))
    finally:
        _unserve()

    assert result["status"] == "copied"
    assert removed == [f"audio/{sid}/seg_00001.wav"]
    session = await dbs.repo.get_session(sid)
    assert session["copied_at"] is not None and session["segments_deleted_at"] is not None
    async with dbs.rec.acquire() as conn:
        assert await conn.fetchval(
            "SELECT count(*) FROM audit_log WHERE session_id = $1 "
            "AND event_type = 'session.copied'", sid) == 1
