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
import hashlib
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
import dashboard                              # noqa: E402
import clinical                                # noqa: E402
from clinical_store import ClinicalStore       # noqa: E402
from confirmation import Visit                 # noqa: E402
from db_v2 import V2Repository                 # noqa: E402

pytestmark = pytest.mark.asyncio

# Every v2 and v3 migration, in order, as a deployment applies them.
RECORDINGS_SCRIPTS = sorted((BACKEND / "scripts").glob("0[0-9][0-9]_*.sql"))

# What the live database looks like before them: the v1 tables, as the UIU
# server's own deployment creates them. Read from that file rather than
# repeated here, so the tests and a new server cannot drift apart.
V1_BASELINE = (BACKEND.parent / "deploy" / "uiu" / "postgres" / "schema"
               / "00_v1_baseline.sql").read_text(encoding="utf-8")

# 002 makes the server's roles (superuser only); the rest are the clinical schema.
CLINICAL_SCRIPTS = [s for s in sorted((BACKEND / "scripts" / "clinical").glob("0[0-9][0-9]_*.sql"))
                    if not s.name.startswith("002_")]
GUARD_SCRIPT = BACKEND / "scripts" / "guard" / "data_guard.sql"


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


# The two migrations decided in review to destroy data (DESTRUCTIVE_APPROVED in
# deploy/uiu/tests/test_deployment.py). They ran before the data guard existed;
# applied again under it, as here, they need its switch.
DECIDED_DESTRUCTIVE = {"022_v3_drop_v1_tables.sql", "024_v3_readable_devices.sql"}


async def _apply(uri, scripts):
    conn = await asyncpg.connect(uri)
    try:
        for script in scripts:
            approve = "on" if script.name in DECIDED_DESTRUCTIVE else ""
            await conn.execute(f"SET aims.allow_destructive = '{approve}'")
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
        # As migrate.sh does last: every test below runs under the data guard.
        await _apply(recordings, [GUARD_SCRIPT])
        await _apply(clinical_uri, [GUARD_SCRIPT])
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
    """
    Re-running the scripts changes nothing and fails nothing - all of them,
    from the baseline, not only the v3 ones.

    `CREATE OR REPLACE VIEW` cannot remove or reorder a column, so a script
    that replaces a view a later migration widened fails the second time it
    is applied. That is what happened when the bench was started for a second
    time on a database it had already built, so the views are dropped first.
    """
    await _apply(pg.recordings, RECORDINGS_SCRIPTS)
    await _apply(pg.clinical, CLINICAL_SCRIPTS)
    # And again: idempotent means idempotent, however many times.
    await _apply(pg.recordings, RECORDINGS_SCRIPTS)
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
    key = await repo.create_cmed_key(label="cmed", created_by="tests", expires_at=None,
                                     cmed_hospital_ids=["CMED-DHOLPUR"])
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
        # The name was sent, and is not kept: sex is all that is needed.
        assert (patient["full_name"], patient["sex"], obs["systolic"], male) == (
            None, "male", 140, 1)
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
        assert (result["erased"], result["marked_unconfirmed"]) == (0, 2)
        assert (await repo.session_confirmation(slow))["confirmation"] == "unconfirmed"
        kept = await repo.get_session(old)
        assert (kept["confirmation"], kept["patient_id"]) != ("expired", "REDACTED")
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

    key = f"copies/HOSP003/DR0042/2026-09-13/{stem}.v1.wav.enc"
    assert await dbs.repo.next_copy_version(sid, "audio") == 1
    await dbs.repo.record_cloud_copy(
        session_id=sid, kind="audio", object_key=key, version=1,
        byte_length=900_000, sha256=b"\x22" * 32, plain_sha256=b"\x33" * 32)
    await dbs.repo.record_cloud_copy(
        session_id=sid, kind="json", object_key=key.replace("wav.enc", "json.enc"), version=1,
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
    key = f"copies/{stem}.v1.wav.enc"

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


async def test_a_pc_whose_key_lives_in_its_tpm_can_enrol(dbs):
    """021: a 65-byte P-256 key (0x04 X Y) enrols; anything else is still refused."""
    import asyncpg
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    repo = dbs.repo
    await repo.upsert_hospital("HOSP003", "Dholpur", "Asia/Dhaka")
    point = ec.generate_private_key(ec.SECP256R1()).public_key().public_bytes(
        Encoding.X962, PublicFormat.UncompressedPoint)
    token = await repo.create_enrollment_token(hospital_id="HOSP003", doctor_id="",
                                               created_by="tests")
    enrolled = await repo.enroll_device(token=token, tpm_pubkey=point, machine_name="PC",
                                        os_version="", app_version="3.6.0",
                                        protocol_version=2)
    device = await repo.device_by_token(enrolled["device_token"])
    assert bytes(device["tpm_pubkey"]) == point

    for bad in (b"" * 33, b"" + point[1:]):
        token = await repo.create_enrollment_token(hospital_id="HOSP003", doctor_id="",
                                                   created_by="tests")
        with pytest.raises(asyncpg.CheckViolationError):
            await repo.enroll_device(token=token, tpm_pubkey=bad, machine_name="PC",
                                     os_version="", app_version="3.6.0",
                                     protocol_version=2)


async def test_what_keeps_a_rogue_pc_out_of_the_dataset_runs_on_postgres(dbs):
    """The three queries behind the dataset gate, on the real schema."""
    repo = dbs.repo
    first = await enrolled_device(repo)
    second = await enrolled_device(repo)
    v = visit(patient_id="PROGUE1")
    sid = await _linked_session(dbs, first, v)

    # One consultation, two PCs asking for it: both are named.
    now = datetime.now(timezone.utc)
    assert await repo.visit_devices(v, hospital_id="HOSP003") == [str(first["device_id"])]
    await repo.create_authorisation(jti="rogue-1", device_id=second["device_id"],
                                    hospital_id="HOSP003", visit=v, expires_at=now)
    assert sorted(await repo.visit_devices(v, hospital_id="HOSP003")) == sorted(
        [str(first["device_id"]), str(second["device_id"])])

    # A clinic's records attach only to that clinic's recordings.
    assert (await repo.session_for_visit(v, "HOSP003"))["session_id"] == sid
    assert await repo.session_for_visit(v, "HOSP999") is None

    # Every recording reaches the server; only a confirmed one is joined into a
    # WAV on its disk. One still waiting stays as its pieces in the cloud.
    async with dbs.rec.acquire() as conn:
        await conn.execute("UPDATE sessions SET closed_at = now() WHERE session_id = $1", sid)
    held = await a_session(repo, first)
    digest = os.urandom(32)
    async with dbs.rec.acquire() as conn:
        await conn.execute("UPDATE sessions SET closed_at = now(), confirmation = "
                           "'unconfirmed' WHERE session_id = $1", held)
        # One uploaded piece: the archive takes only recordings with audio.
        await conn.execute("""
            INSERT INTO chain_entries (session_id, entry_no, entry_type, payload,
                                       payload_sha256, prev_hash, entry_hash,
                                       device_signature, occurred_at)
            VALUES ($1, 1, 'segment', '{}', $2, $2, $2, $2, now())
        """, held, digest)
        await conn.execute("""
            INSERT INTO segments (session_id, seq_no, entry_no, object_key, sha256,
                                  bytes, duration_seconds, state)
            VALUES ($1, 1, 1, $2, $3, 100, 1.0, 'committed')
        """, held, f"audio/{held}/seg_00001.wav", digest)
    assert held not in {p["session_id"] for p in await repo.pending_archive(50)}

    # CMED confirms it, however late: now it is joined and archived.
    await repo.set_session_confirmation(held, "confirmed")
    assert held in {p["session_id"] for p in await repo.pending_archive(50)}
    await repo.mark_archived(held, relpath="x/held.wav", sha256=b"\x11" * 32,
                             byte_length=44)

    # A PC revoked as untrusted: its recordings not yet archived are held.
    # Already archived (held, above) stays as it is, marked; the rest is held.
    assert await repo.quarantine_device_sessions(first["device_id"], "untrusted") >= 1
    assert (await repo.get_session(sid))["status"] == "quarantined"
    assert (await repo.get_session(held))["status"] == "archived"
    assert sid not in {p["session_id"] for p in await repo.pending_archive(50)}

    # Leave the shared day as it was for the dashboard's counts.
    async with dbs.rec.acquire() as conn:
        await conn.execute("UPDATE sessions SET status = 'closed', quarantine_reason = NULL "
                           "WHERE device_id = $1", first["device_id"])


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
        session_id=sid, kind="audio", object_key=f"copies/{stem}.v9.wav.enc", version=9,
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
            VALUES ($1, 1, 1, $2, $3, 100, 1.0, 'archived')
        """, sid, f"audio/{sid}/seg_00001.wav", digest)
    # 'archived': the piece is in the archive file, as /archive/complete marks
    # it. Only such pieces leave the bucket after the copy.

    removed = _server(dbs)
    try:
        place = await api_v2.archive_copy_authorize(
            api_v2.CopyAuthorizeRequest(session_id=sid, kind="audio"))
        assert place["object_key"].endswith(f"{stem}.v1.wav.enc")
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


# ============================================================
# The operational dashboard (§8.9)
# ============================================================

async def _a_days_work(dbs, device, day, tag):
    """
    A day's recordings in every state the dashboard reports.

    The day is deliberately not today: every test shares one database, and
    sessions opened by other tests are filed under the real date.
    """
    made = {}
    for n, (suffix, state) in enumerate([
        ("1", "confirmed"), ("2", "confirmed"), ("3", "pending"),
        ("4", "unconfirmed"), ("5", "legacy"),
    ]):
        patient = f"{tag}{suffix}"
        sid = await a_session(dbs.repo, device)
        async with dbs.rec.acquire() as conn:
            await conn.execute("""
                UPDATE sessions
                   SET patient_id = $2, confirmation = $3, session_date = $4,
                       doctor_id = $5, total_duration_seconds = $6,
                       closed_at = now(), segment_count = 3
                 WHERE session_id = $1
            """, sid, patient, state, day,
                 "DR0042" if n % 2 == 0 else "DR0099", 900 + n * 60)
        made[patient] = sid
    return made


async def test_the_dashboard_counts_a_day(dbs):
    """SRS-DSH-01, -08: volume and confirmation, by clinic and doctor."""
    device = await enrolled_device(dbs.repo)
    await _a_days_work(dbs, device, date(2027, 3, 1), "PDASH")
    _server(dbs)
    try:
        result = await dashboard.summary(from_date="2027-03-01", to_date="2027-03-01")
    finally:
        _unserve()

    volume = result["volume"]
    assert volume["total_recordings"] == 5
    assert volume["total_hours"] > 1.2
    assert {d["doctor_id"] for d in volume["by_doctor"]} == {"DR0042", "DR0099"}
    assert result["confirmation"]["totals"]["confirmed"] == 2
    assert result["confirmation"]["totals"]["unconfirmed"] == 1
    assert result["volume"]["by_clinic"][0]["hospital_id"] == "HOSP003"


async def test_a_refused_consultation_is_not_counted_as_work(dbs):
    """SRS-CNS-06: it was erased; it is not part of the day's volume."""
    device = await enrolled_device(dbs.repo)
    sid = await a_session(dbs.repo, device)
    async with dbs.rec.acquire() as conn:
        await conn.execute("UPDATE sessions SET confirmation = 'refused', "
                           "session_date = $2, total_duration_seconds = 600 "
                           "WHERE session_id = $1", sid, date(2027, 3, 6))
    _server(dbs)
    try:
        result = await dashboard.summary(from_date="2027-03-06", to_date="2027-03-06")
    finally:
        _unserve()
    assert result["volume"]["total_recordings"] == 0
    assert result["confirmation"]["totals"]["refused"] == 1


async def test_the_dashboard_shows_what_went_wrong_and_where(dbs):
    """SRS-DSH-03: quarantines and early stops, against clinic and doctor."""
    device = await enrolled_device(dbs.repo)
    quarantined = await a_session(dbs.repo, device)
    stopped = await a_session(dbs.repo, device)
    # The two normal ends - the next patient, two minutes after the
    # prescription - are not "stopped early".
    normal = [await a_session(dbs.repo, device), await a_session(dbs.repo, device)]
    # Today, because an alert is stamped when it is raised: the dashboard
    # shows the alerts of the days it is asked about.
    day = datetime.now(timezone.utc).date()
    async with dbs.rec.acquire() as conn:
        await conn.execute("UPDATE sessions SET session_date = $2, "
                           "quarantine_reason = 'chain_broken' WHERE session_id = $1",
                           quarantined, day)
        await conn.execute("UPDATE sessions SET session_date = $2, "
                           "close_reason = 'doctor_stopped' WHERE session_id = $1",
                           stopped, day)
        for sid, reason in zip(normal, ("superseded_by_new_patient", "counselling_limit")):
            await conn.execute("UPDATE sessions SET session_date = $2, close_reason = $3 "
                               "WHERE session_id = $1", sid, day, reason)
    await dbs.repo.raise_alert(alert_type="dashboard_test_alert", severity="warning",
                               session_id=stopped, detail={})
    _server(dbs)
    try:
        result = await dashboard.summary(from_date=day.isoformat(),
                                         to_date=day.isoformat())
    finally:
        _unserve()

    integrity = result["integrity"]
    assert integrity["quarantined_total"] == 1
    assert integrity["quarantined"][0]["quarantine_reason"] == "chain_broken"
    assert integrity["stopped_early_total"] == 1
    assert integrity["stopped_early"][0]["close_reason"] == "doctor_stopped"
    assert any(a["alert_type"] == "dashboard_test_alert" for a in integrity["alerts"])


async def test_the_dashboard_shows_every_room(dbs):
    """SRS-DSH-04: heard from, holding audio, or silent."""
    device = await enrolled_device(dbs.repo)
    await dbs.repo.touch_device(device["device_id"], spool_bytes=42 * 1024 ** 2,
                                pending_segments=4)
    _server(dbs)
    try:
        result = await dashboard.summary()
    finally:
        _unserve()

    rooms = result["recorders"]
    assert rooms["enrolled"] >= 1
    mine = next(r for r in rooms["rooms"] if r["device_id"] == str(device["device_id"]))
    assert (mine["pending_segments"], mine["spool_mb"]) == (4, 42.0)
    assert mine["silent"] is False


async def test_a_recorder_not_heard_from_is_called_silent(dbs):
    device = await enrolled_device(dbs.repo)
    async with dbs.rec.acquire() as conn:
        await conn.execute("UPDATE devices SET last_seen_at = now() - interval '2 hours' "
                           "WHERE device_id = $1", device["device_id"])
    _server(dbs)
    try:
        result = await dashboard.summary()
    finally:
        _unserve()
    mine = next(r for r in result["recorders"]["rooms"]
                if r["device_id"] == str(device["device_id"]))
    assert mine["silent"] is True
    assert result["recorders"]["silent"] >= 1


async def test_the_dashboard_matches_recordings_against_cmed(dbs):
    """SRS-DSH-05: a recording with no record, and a record with no recording."""
    device = await enrolled_device(dbs.repo)
    waiting = await a_session(dbs.repo, device)
    async with dbs.rec.acquire() as conn:
        await conn.execute("UPDATE sessions SET confirmation = 'pending', "
                           "session_date = $2 WHERE session_id = $1",
                           waiting, date(2027, 3, 5))
    orphan = visit(patient_id="PORPHAN", visit_date="2027-03-05")
    notice = await dbs.repo.record_notice(orphan, hospital_id="HOSP003",
                                          clinical_record_id=1)
    async with dbs.rec.acquire() as conn:
        await conn.execute("UPDATE confirmation_notices SET received_at = "
                           "now() - interval '30 minutes' WHERE id = $1", notice)
    _server(dbs)
    try:
        result = await dashboard.summary(from_date="2027-03-05", to_date="2027-03-05")
    finally:
        _unserve()

    matching = result["reconciliation"]
    assert matching["recordings_without_a_record"] == 1
    assert matching["records_without_a_recording"] == 1


async def test_the_dashboard_shows_what_is_not_yet_safe_in_two_places(dbs):
    """SRS-DSH-09: copies outstanding, old pieces, and the last restore test."""
    device = await enrolled_device(dbs.repo)
    v = visit(patient_id="PSAFE1")
    await _archived(dbs, device, v, stem="PSAFE1_DR0042_HOSP003_101432_102847_20260913")
    _server(dbs)
    try:
        before = await dashboard.summary()
        await dashboard.record_restore_test({"by": "tester", "result": "passed",
                                             "what": "one day from the copy store"})
        after = await dashboard.summary()
    finally:
        _unserve()

    assert before["copies"]["recordings_without_a_cloud_copy"] >= 1
    assert before["copies"]["last_restore_test"]["at"] is None
    assert after["copies"]["last_restore_test"]["at"] is not None
    assert after["copies"]["last_restore_test"]["detail"]["result"] == "passed"


async def test_every_count_opens_into_its_rows(dbs):
    """SRS-DSH-07, SRS-DSH-02: clinic, doctor, date, patient - and the recording."""
    device = await enrolled_device(dbs.repo)
    made = await _a_days_work(dbs, device, date(2027, 3, 2), "PROWS")
    _server(dbs)
    try:
        unconfirmed = await dashboard.recordings(
            from_date="2027-03-02", to_date="2027-03-02", state="unconfirmed")
        one_doctor = await dashboard.recordings(
            from_date="2027-03-02", to_date="2027-03-02", doctor_id="DR0099")
        one_patient = await dashboard.recordings(
            from_date="2027-03-02", to_date="2027-03-02", patient_id="PROWS1")
    finally:
        _unserve()

    assert [r["patient_id"] for r in unconfirmed["recordings"]] == ["PROWS4"]
    assert {r["doctor_id"] for r in one_doctor["recordings"]} == {"DR0099"}
    assert one_patient["recordings"][0]["session_id"] == made["PROWS1"]


async def test_the_dashboard_finds_a_recording_by_part_of_its_name(dbs):
    """SRS-DBA-24: search part of a file name."""
    device = await enrolled_device(dbs.repo)
    sid = await a_session(dbs.repo, device)
    stem = "PFIND9_DR0042_HOSP003_101432_102847_20270304"
    async with dbs.rec.acquire() as conn:
        await conn.execute("UPDATE sessions SET file_stem = $2, session_date = $3 "
                           "WHERE session_id = $1", sid, stem, date(2027, 3, 4))
    _server(dbs)
    try:
        found = await dashboard.recordings(from_date="2027-03-04", to_date="2027-03-04",
                                           search="102847")
    finally:
        _unserve()
    assert [r["session_id"] for r in found["recordings"]] == [sid]


async def test_the_dashboard_never_reaches_the_clinical_database(dbs):
    """SRS-DSH-06: it holds no credential for it, and asks it nothing."""
    device = await enrolled_device(dbs.repo)
    v = visit(patient_id="PNAME9")
    await _send(dbs.clinical, "patient_information", v,
                _api2(v, demographics={"name": "Should Not Appear", "sex": "female"}))
    sid = await _linked_session(dbs, device, v)
    async with dbs.rec.acquire() as conn:
        await conn.execute("UPDATE sessions SET patient_id = $2, session_date = $3 "
                           "WHERE session_id = $1", sid, v.patient_id, date(2027, 3, 3))
    _server(dbs)
    try:
        api_v2.ctx.clinical = None              # the dashboard must not need it
        result = await dashboard.summary(from_date="2027-03-03", to_date="2027-03-03")
        rows = await dashboard.recordings(from_date="2027-03-03", to_date="2027-03-03")
    finally:
        _unserve()

    assert "Should Not Appear" not in json.dumps(result)
    assert "Should Not Appear" not in json.dumps(rows, default=str)
    assert rows["recordings"][0]["patient_id"] == "PNAME9"   # the number, not a name


async def test_the_dashboard_page_is_one_file_with_no_build_step(dbs):
    _server(dbs)
    try:
        page = await dashboard.page()
    finally:
        _unserve()
    body = page.body.decode("utf-8")
    assert "<title>AIMScribe" in body
    assert "src=" not in body.replace('src="', "")        # nothing fetched from a CDN
    assert "localStorage.setItem" not in body              # the key is never stored


async def test_the_same_piece_committed_twice_at_once_is_a_duplicate(dbs):
    """
    Two retries of one piece, racing.

    A recorder retries a commit whose reply it never saw. If the original is
    still in flight, both find nothing stored and both insert; the loser used
    to get a unique-violation 500, and the recorder retried it for ever.
    Found by the 14-room simulation (SRS-REC-13).
    """
    device = await enrolled_device(dbs.repo)
    sid = await a_session(dbs.repo, device)
    digest = bytes.fromhex("5a") * 32

    def commit(entry_no: int):
        from integrity import ChainEntry
        entry = ChainEntry(entry_no=entry_no, entry_type="segment", payload={"n": 1},
                           payload_sha256=digest, prev_hash=None, entry_hash=digest,
                           signature=digest)
        return dbs.repo.commit_segment(
            session_id=sid, seq_no=1, entry=entry,
            object_key=f"audio/{sid}/seg_00001.wav", byte_length=1000,
            duration_seconds=30.0, sha256=digest, rms_mean=100.0,
            captured_start_at=datetime.now(timezone.utc),
            captured_end_at=datetime.now(timezone.utc), is_final=False)

    first, second = await asyncio.gather(commit(1), commit(2), return_exceptions=True)
    outcomes = {first, second}
    assert not any(isinstance(o, Exception) for o in (first, second)), (first, second)
    assert outcomes == {"stored", "duplicate"}

    # And a different piece claiming the same place is still a conflict.
    other = bytes.fromhex("77") * 32
    from integrity import ChainEntry
    entry = ChainEntry(entry_no=3, entry_type="segment", payload={"n": 1},
                       payload_sha256=other, prev_hash=None, entry_hash=other,
                       signature=other)
    assert await dbs.repo.commit_segment(
        session_id=sid, seq_no=1, entry=entry, object_key="audio/other.wav",
        byte_length=10, duration_seconds=1.0, sha256=other, rms_mean=1.0,
        captured_start_at=datetime.now(timezone.utc),
        captured_end_at=datetime.now(timezone.utc), is_final=False) == "conflict"


async def test_the_viewer_sorts_recordings_by_day_and_clinic(dbs):
    """023: a day's count per clinic, and the files behind every problem."""
    device = await enrolled_device(dbs.repo)
    # In the past: the grid of quiet days runs up to today.
    day = date(2026, 1, 15)
    fine, corrupt, short, odd_end = [await a_session(dbs.repo, device) for _ in range(4)]
    async with dbs.rec.acquire() as conn:
        await conn.execute("""
            UPDATE sessions
               SET session_date = $2, status = 'archived', confirmation = 'confirmed',
                   closed_at = now(), archived_at = now(), copied_at = now(),
                   chain_verified_at = now(), archive_relpath = session_id || '.wav',
                   archive_sha256 = $3, archive_bytes = 1000000,
                   total_duration_seconds = 600, close_reason = 'doctor_stopped'
             WHERE session_id = ANY($1::text[])
        """, [fine, corrupt, short, odd_end], day, b"" * 32)
        await conn.execute("UPDATE sessions SET status = 'quarantined', "
                           "quarantine_reason = 'segment hash mismatch on arrival' "
                           "WHERE session_id = $1", corrupt)
        await conn.execute("UPDATE sessions SET total_duration_seconds = 2 "
                           "WHERE session_id = $1", short)
        await conn.execute("UPDATE sessions SET close_reason = 'tray_stop' "
                           "WHERE session_id = $1", odd_end)

        problems = {r["internal_key"]: r for r in await conn.fetch(
            "SELECT * FROM v_audio_problems WHERE day = $1 AND hospital_id = 'HOSP003'", day)}
        daily = await conn.fetchrow(
            "SELECT * FROM v_audio_daily WHERE day = $1 AND hospital_id = 'HOSP003'", day)
        quiet = await conn.fetchval(
            "SELECT recordings FROM v_audio_daily WHERE day = $1 AND hospital_id = 'HOSP003'",
            day + timedelta(days=1))
        # Every way a consultation normally ends, none of them a problem.
        for reason in api_v2.NORMAL_CLOSE_REASONS:
            await conn.execute("UPDATE sessions SET close_reason = $2 WHERE session_id = $1",
                               fine, reason)
            assert not await conn.fetchval(
                "SELECT count(*) FROM v_audio_problems WHERE internal_key = $1", fine), reason

    assert set(problems) == {corrupt, short, odd_end}
    assert problems[corrupt]["severity"] == "critical"
    assert "segment hash mismatch" in problems[corrupt]["problems"]
    assert problems[short]["problems"] == "under 5 seconds of audio"
    assert problems[odd_end]["problems"] == "ended abnormally: tray_stop"
    assert (daily["recordings"], daily["wav_saved"], daily["in_cloud"]) == (4, 4, 4)
    assert (daily["corrupted"], daily["critical"], daily["need_attention"]) == (1, 1, 3)
    # A clinic that recorded nothing that day is listed, with a zero.
    assert quiet == 0


async def test_the_viewer_names_each_pc_and_its_requests(dbs):
    """024: a nonce reads as a PC at a clinic, and a replay is still refused."""
    device = await enrolled_device(dbs.repo)
    assert await dbs.repo.use_device_nonce(device["device_id"], "nonce-viewer-1")
    assert not await dbs.repo.use_device_nonce(device["device_id"], "nonce-viewer-1")

    async with dbs.rec.acquire() as conn:
        pc = await conn.fetchrow("SELECT * FROM v_devices WHERE device_id = $1",
                                 device["device_id"])
        request = await conn.fetchrow(
            "SELECT * FROM v_device_requests WHERE nonce = 'nonce-viewer-1'")
        # A nonce from no PC the database knows is refused by the database itself.
        with pytest.raises(asyncpg.ForeignKeyViolationError):
            await conn.execute("INSERT INTO device_nonces (device_id, nonce) "
                               "VALUES (gen_random_uuid(), 'nonce-from-nowhere')")
        described = await conn.fetchval("SELECT obj_description('device_nonces'::regclass)")

    assert (pc["hospital_id"], pc["machine_name"]) == ("HOSP003", "PC")
    assert pc["state"] == "enrolled, never heard from"
    assert pc["signed_requests_15min"] == 1
    assert (request["hospital_id"], request["machine_name"]) == ("HOSP003", "PC")
    assert "v_device_requests" in described


# ============================================================
# 025: only what CMED confirms is kept; PCs that register themselves
# ============================================================

def _p256():
    """A P-256 key, as a TPM holds one: the raw point, and r||s signatures."""
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
    key = ec.generate_private_key(ec.SECP256R1())
    public = key.public_key().public_bytes(serialization.Encoding.X962,
                                           serialization.PublicFormat.UncompressedPoint)

    def sign(message: bytes) -> bytes:
        r, s = decode_dss_signature(key.sign(message, ec.ECDSA(hashes.SHA256())))
        return r.to_bytes(32, "big") + s.to_bytes(32, "big")
    return public, sign


def _registration(cmed_code="CMED-DHOLPUR", *, key=None, at=None, machine="CLINIC-PC"):
    import base64
    import time
    import integrity
    public, sign = key or _p256()
    stamp = int(time.time() if at is None else at)
    proof = sign(integrity.registration_proof_message(cmed_code, public, stamp))
    return api_v2.RegisterRequest(
        device_pubkey=public.hex(),
        key_proof=base64.urlsafe_b64encode(proof).rstrip(b"=").decode(),
        cmed_hospital_id=cmed_code, timestamp=stamp, machine_name=machine,
        app_version="3.7.0")


def _caller(address="203.0.113.7"):
    return SimpleNamespace(client=SimpleNamespace(host=address), headers={})


def _refusal(reply):
    """A _coded() refusal as (status, code)."""
    return reply.status_code, json.loads(reply.body)["code"]


async def test_a_pc_registers_itself_and_cmed_confirming_two_visits_approves_it(dbs):
    """025: no token; pending until CMED's server confirms two of its recordings."""
    import integrity
    await enrolled_device(dbs.repo)                       # HOSP003 <- CMED-DHOLPUR
    key = _p256()
    _server(dbs)
    try:
        reply = await api_v2.register_device(_registration(key=key), _caller())
        assert reply["approval"] == "pending"
        assert reply["device_id"] == str(integrity.device_id_for_key(key[0]))
        assert reply["hospital_id"] == "HOSP003"
        pc = await dbs.repo.device_by_token(reply["device_token"])
        assert pc["approval"] == "pending"

        first = await a_session(dbs.repo, pc)
        second = await a_session(dbs.repo, pc)
        async with dbs.rec.acquire() as conn:
            await conn.execute("UPDATE sessions SET confirmation = 'confirmed' "
                               "WHERE session_id = $1", first)
            after_one = await conn.fetchval(
                "SELECT approval FROM devices WHERE device_id = $1", pc["device_id"])
            await conn.execute("UPDATE sessions SET confirmation = 'confirmed' "
                               "WHERE session_id = $1", second)
            after_two = await conn.fetchrow(
                "SELECT approval, approved_by FROM devices WHERE device_id = $1",
                pc["device_id"])
        assert after_one == "pending"
        assert (after_two["approval"], after_two["approved_by"]) == ("approved", "cmed")

        await api_v2._settle_unconfirmed()                 # the sweep writes the audit entry
        async with dbs.rec.acquire() as conn:
            audited = await conn.fetchval(
                "SELECT count(*) FROM audit_log WHERE device_id = $1 "
                "AND event_type = 'device.approved'", pc["device_id"])
        assert audited == 1
    finally:
        _unserve()


async def test_what_an_outsider_cannot_register(dbs, monkeypatch):
    """025: a wrong clinic, a key it does not hold, an old proof, too many at once."""
    import time
    await enrolled_device(dbs.repo)
    _server(dbs)
    try:
        assert _refusal(await api_v2.register_device(
            _registration("NOT-A-CLINIC"), _caller())) == (403, "UNKNOWN_CLINIC")

        forged = _registration()
        forged.device_pubkey = _p256()[0].hex()            # proof made with another key
        assert _refusal(await api_v2.register_device(forged, _caller())) \
            == (401, "KEY_PROOF_INVALID")

        assert _refusal(await api_v2.register_device(
            _registration(at=time.time() - 3600), _caller())) == (401, "CLOCK")

        # Asked again before it was ever heard from: a new token. After: refused,
        # so a registration seen on the wire cannot take a working PC's token.
        key = _p256()
        first = await api_v2.register_device(_registration(key=key), _caller())
        again = await api_v2.register_device(_registration(key=key), _caller())
        assert again["device_id"] == first["device_id"]
        assert again["device_token"] != first["device_token"]
        await dbs.repo.touch_device(first["device_id"])
        assert _refusal(await api_v2.register_device(_registration(key=key), _caller())) \
            == (409, "ALREADY_REGISTERED")

        async with dbs.rec.acquire() as conn:
            pending = await conn.fetchval(
                "SELECT count(*) FROM devices WHERE hospital_id = 'HOSP003' "
                "AND approval = 'pending' AND revoked_at IS NULL")
        monkeypatch.setenv("AIMS_PENDING_DEVICES_PER_CLINIC", str(pending + 1))
        assert "device_token" in await api_v2.register_device(_registration(), _caller())
        assert _refusal(await api_v2.register_device(_registration(), _caller())) \
            == (429, "TOO_MANY_PENDING")

        monkeypatch.setenv("AIMS_SELF_REGISTRATION", "off")
        assert _refusal(await api_v2.register_device(_registration(), _caller())) \
            == (403, "SELF_REGISTRATION_OFF")
    finally:
        _unserve()


async def test_a_pending_pc_uploads_only_its_allowance(dbs, monkeypatch):
    """025: an outsider who registers can never put more than this on the server."""
    await enrolled_device(dbs.repo)
    _server(dbs)
    api_v2.ctx.minio.get_presigned_upload_url = lambda key, expires: f"https://put/{key}"
    monkeypatch.setenv("AIMS_PENDING_UPLOAD_MB", "1")
    try:
        reply = await api_v2.register_device(_registration(), _caller())
        pc = await dbs.repo.device_by_token(reply["device_token"])
        sid = await a_session(dbs.repo, pc)
        small = await api_v2.authorize_segment(
            api_v2.AuthorizeRequest(session_id=sid, seq_no=1, bytes=500_000,
                                    sha256="a" * 64), device=pc)
        assert "upload_url" in small
        large = await api_v2.authorize_segment(
            api_v2.AuthorizeRequest(session_id=sid, seq_no=2, bytes=2 * 1024 * 1024,
                                    sha256="b" * 64), device=pc)
        assert _refusal(large) == (429, "AWAITING_APPROVAL")
        assert large.headers["Retry-After"] == "300"
    finally:
        _unserve()


async def test_only_what_cmed_confirms_is_kept_and_an_outage_erases_nothing(dbs):
    """025: erased on its date unless confirmed - but never while CMED is silent."""
    from datetime import timedelta as days_ago
    device = await enrolled_device(dbs.repo)
    await dbs.repo.upsert_hospital("HOSPERASE", "Erase clinic", "Asia/Dhaka")
    old = days_ago(days=8)
    waiting = await a_session(dbs.repo, device, opened_ago=old)
    kept = await a_session(dbs.repo, device, opened_ago=old)
    held = await a_session(dbs.repo, device, opened_ago=old)
    young = await a_session(dbs.repo, device, opened_ago=days_ago(days=1))
    async with dbs.rec.acquire() as conn:
        await conn.execute("""
            UPDATE sessions SET hospital_id = 'HOSPERASE', confirmation = 'unconfirmed',
                   patient_id = 'PERASE1'
             WHERE session_id = ANY($1::text[])
        """, [waiting, kept, held, young])
        await conn.execute("UPDATE sessions SET legal_hold = true WHERE session_id = $1", held)
    _server(dbs)
    try:
        # CMED has never been heard from for this clinic: an outage, nothing goes.
        await api_v2._settle_unconfirmed()
        async with dbs.rec.acquire() as conn:
            assert await conn.fetchval(
                "SELECT confirmation FROM sessions WHERE session_id = $1", waiting) \
                == "unconfirmed"
            silent = await conn.fetchval(
                "SELECT detail->>'count' FROM integrity_alerts "
                "WHERE alert_type = 'erase_held_cmed_silent' "
                "AND detail->>'hospital_id' = 'HOSPERASE'")
        assert silent == "2"            # both still waiting then

        # CMED is talking again; one of them is confirmed just in time.
        await dbs.repo.cmed_heard_from("HOSPERASE")
        async with dbs.rec.acquire() as conn:
            await conn.execute("UPDATE sessions SET confirmation = 'confirmed' "
                               "WHERE session_id = $1", kept)
        await api_v2._settle_unconfirmed()

        async with dbs.rec.acquire() as conn:
            rows = {r["session_id"]: r for r in await conn.fetch(
                "SELECT session_id, confirmation, patient_id, erase_after FROM sessions "
                "WHERE session_id = ANY($1::text[])", [waiting, kept, held, young])}
            audited = await conn.fetchval(
                "SELECT detail->>'reason' FROM audit_log WHERE session_id = $1 "
                "AND event_type = 'session.expired'", waiting)
        assert (rows[waiting]["confirmation"], rows[waiting]["patient_id"]) \
            == ("expired", "REDACTED")
        assert rows[kept]["confirmation"] == "confirmed"
        assert rows[kept]["erase_after"] is None              # cleared by the database
        assert rows[held]["confirmation"] == "unconfirmed"    # a legal hold is never erased
        assert rows[young]["confirmation"] == "unconfirmed"
        assert rows[young]["erase_after"] is not None         # its date is set, and shown
        assert audited == "not confirmed by CMED"
    finally:
        _unserve()


async def test_a_clinic_whose_recordings_would_mostly_be_erased_stops_until_an_admin_looks(
        dbs, monkeypatch):
    """030: the circuit breaker - a confirmation fault must not erase a clinic's week."""
    from datetime import timedelta as days_ago
    device = await enrolled_device(dbs.repo)
    await dbs.repo.upsert_hospital("HOSPBRK", "Breaker clinic", "Asia/Dhaka")
    await dbs.repo.cmed_heard_from("HOSPBRK")                 # not an outage
    old = days_ago(days=8)
    lost = [await a_session(dbs.repo, device, opened_ago=old) for _ in range(25)]
    fine = [await a_session(dbs.repo, device, opened_ago=old) for _ in range(5)]
    async with dbs.rec.acquire() as conn:
        await conn.execute("UPDATE sessions SET hospital_id = 'HOSPBRK', confirmation = 'unconfirmed' "
                           "WHERE session_id = ANY($1::text[])", lost)
        await conn.execute("UPDATE sessions SET hospital_id = 'HOSPBRK', confirmation = 'confirmed' "
                           "WHERE session_id = ANY($1::text[])", fine)

    async def states():
        async with dbs.rec.acquire() as conn:
            return {r["confirmation"] for r in await conn.fetch(
                "SELECT confirmation FROM sessions WHERE session_id = ANY($1::text[])", lost)}

    _server(dbs)
    try:
        # 25 of 30 would go: far beyond stray clicks. Nothing is erased; one critical alert.
        result = await api_v2._settle_unconfirmed()
        assert result[0] == 0 and result[4] == 1
        assert await states() == {"unconfirmed"}
        async with dbs.rec.acquire() as conn:
            alert = await conn.fetchrow(
                "SELECT severity, detail FROM integrity_alerts WHERE alert_type = 'erase_breaker' "
                "AND detail->>'hospital_id' = 'HOSPBRK'")
        assert alert["severity"] == "critical"
        assert json.loads(alert["detail"])["unconfirmed"] == 25

        # Paused altogether: nothing either, whatever the breaker says.
        monkeypatch.setenv("AIMS_ERASE_PAUSED", "true")
        await dbs.repo.release_erase("HOSPBRK", by="tests", reason="checked: strays", hours=1)
        assert (await api_v2._settle_unconfirmed())[0] == 0
        assert await states() == {"unconfirmed"}
        status = await api_v2.admin_erase_status()
        assert status["paused"] is True
        assert status["clinics"][0]["breaker"] == "released"

        # An admin has looked and released it: now they go.
        monkeypatch.delenv("AIMS_ERASE_PAUSED")
        erased = 0
        for _ in range(3):
            erased += (await api_v2._settle_unconfirmed())[0]
        assert erased == 25 and await states() == {"expired"}
    finally:
        _unserve()


async def test_a_pc_that_registers_and_never_records_is_let_go(dbs):
    """033: registered, recorded nothing for AIMS_PENDING_SILENT_DAYS (30): let go."""
    await enrolled_device(dbs.repo)
    _server(dbs)
    try:
        reply = await api_v2.register_device(_registration(), _caller())
        async with dbs.rec.acquire() as conn:
            await conn.execute("UPDATE devices SET enrolled_at = now() - interval '8 days' "
                               "WHERE device_id = $1::uuid", reply["device_id"])
        await api_v2._settle_unconfirmed()
        assert await dbs.repo.device_by_token(reply["device_token"]) is not None   # a week: kept
        async with dbs.rec.acquire() as conn:
            await conn.execute("UPDATE devices SET enrolled_at = now() - interval '31 days' "
                               "WHERE device_id = $1::uuid", reply["device_id"])
        await api_v2._settle_unconfirmed()
        async with dbs.rec.acquire() as conn:
            pc = await conn.fetchrow("SELECT approval, revoked_at FROM devices "
                                     "WHERE device_id = $1::uuid", reply["device_id"])
        assert pc["approval"] == "rejected" and pc["revoked_at"] is not None
        assert await dbs.repo.device_by_token(reply["device_token"]) is None
    finally:
        _unserve()


async def _pending_pc_with_recordings(dbs, n, *, ago_hours, confirmed=0):
    """A self-registered PC at HOSP003 with n uploaded recordings, ago_hours old."""
    import random
    from core.spool import new_ulid
    await enrolled_device(dbs.repo)
    async with dbs.rec.acquire() as conn:
        # Earlier tests' PCs would fill the clinic's pending places (the cap is 5).
        await conn.execute("UPDATE devices SET approval = 'approved' "
                           "WHERE hospital_id = 'HOSP003' AND approval = 'pending'")
    reply = await api_v2.register_device(
        _registration(), _caller(f"203.0.113.{random.randint(20, 250)}"))
    assert isinstance(reply, dict), reply.body
    opened = datetime.now(timezone.utc) - timedelta(hours=ago_hours)
    async with dbs.rec.acquire() as conn:
        for i in range(n):
            await conn.execute("""
                INSERT INTO sessions (session_id, patient_id, doctor_id, hospital_id, device_id,
                                      protocol_version, status, session_date, opened_at,
                                      segment_count, confirmation)
                VALUES ($1, 'P1', 'DR0042', 'HOSP003', $2::uuid, 2, 'closed', $3, $4, 3, $5)
            """, new_ulid(), reply["device_id"], opened.date(), opened + timedelta(minutes=i),
                "confirmed" if i < confirmed else "unconfirmed")
    return reply


async def _cmed_heard(dbs, ago_hours):
    async with dbs.rec.acquire() as conn:
        await conn.execute("UPDATE hospitals SET cmed_last_message_at = now() - "
                           "make_interval(hours => $1) WHERE hospital_id = 'HOSP003'", ago_hours)


async def _approval(dbs, device_id):
    async with dbs.rec.acquire() as conn:
        return await conn.fetchrow("SELECT approval, revoked_at, blocked_at FROM devices "
                                   "WHERE device_id = $1::uuid", device_id)


async def test_an_outage_never_blocks_a_new_pc(dbs):
    """033: CMED not heard from since the recordings: no evidence, nothing judged."""
    _server(dbs)
    try:
        pc = await _pending_pc_with_recordings(dbs, 5, ago_hours=48)
        await _cmed_heard(dbs, 60)                      # last heard before they were made
        await api_v2._settle_unconfirmed()
        assert (await _approval(dbs, pc["device_id"]))["approval"] == "pending"
    finally:
        _unserve()


async def test_a_pc_cmed_works_for_but_never_confirms_is_blocked_and_can_be_unblocked(dbs):
    """033: CMED working for the clinic, none of the PC's 3 recordings confirmed."""
    _server(dbs)
    try:
        pc = await _pending_pc_with_recordings(dbs, 3, ago_hours=3)
        await _cmed_heard(dbs, 0)                       # CMED talking for this clinic now
        await api_v2._settle_unconfirmed()
        row = await _approval(dbs, pc["device_id"])
        assert row["approval"] == "rejected" and row["blocked_at"] is not None
        assert (await dbs.repo.device_by_token(pc["device_token"]))["revoked_at"] is not None

        await api_v2.admin_unblock_device(pc["device_id"], api_v2.DeviceDecision(
            decided_by="Tester", reason="CMED's outbox was stuck"), None)
        row = await _approval(dbs, pc["device_id"])
        assert row["approval"] == "pending" and row["revoked_at"] is None
        assert (await dbs.repo.device_by_token(pc["device_token"]))["revoked_at"] is None
        await api_v2._settle_unconfirmed()             # its old recordings are not judged again
        assert (await _approval(dbs, pc["device_id"]))["approval"] == "pending"
    finally:
        _unserve()


async def test_one_confirmed_recording_means_it_is_ours(dbs):
    _server(dbs)
    try:
        pc = await _pending_pc_with_recordings(dbs, 4, ago_hours=30, confirmed=1)
        await _cmed_heard(dbs, 0)
        await api_v2._settle_unconfirmed()
        assert (await _approval(dbs, pc["device_id"]))["approval"] == "pending"
    finally:
        _unserve()


async def test_a_clinic_recording_while_cmed_is_silent_raises_one_alert(dbs):
    _server(dbs)
    try:
        await _pending_pc_with_recordings(dbs, 2, ago_hours=0.5)
        await _cmed_heard(dbs, 5)
        async with dbs.rec.acquire() as conn:          # an earlier test's sweep may have alerted
            await conn.execute("UPDATE hospitals SET cmed_silence_alerted_at = NULL "
                               "WHERE hospital_id = 'HOSP003'")
        first = await dbs.repo.cmed_silent_clinics(window_minutes=120, min_recordings=2,
                                                    repeat_hours=6)
        again = await dbs.repo.cmed_silent_clinics(window_minutes=120, min_recordings=2,
                                                    repeat_hours=6)
        assert [c["hospital_id"] for c in first] == ["HOSP003"] and again == []
    finally:
        _unserve()


async def test_trusted_origins_are_versioned_and_signed(dbs):
    import integrity
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    repo = dbs.repo
    _server(dbs)
    key = Ed25519PrivateKey.generate()
    api_v2.ctx.signer = integrity.ReceiptSigner(key)
    try:
        start = await repo.trusted_origins_version()
        await api_v2.admin_add_trusted_origin(api_v2.TrustedOriginRequest(
            origin="https://EMR.cmed.example/", by="Tester"), None)
        v1 = await repo.trusted_origins_version()
        assert v1 > start
        bundle = await api_v2._trusted_origins_bundle()
        payload = bundle["payload"]
        assert "https://emr.cmed.example" in payload["origins"] and payload["version"] == v1
        # A PC that has not joined yet gets the same signed list with no token,
        # and nothing when it already holds this version.
        assert (await api_v2.trusted_origins_for_new_pc(have=0))["trusted_origins"] == bundle
        assert (await api_v2.trusted_origins_for_new_pc(have=v1))["trusted_origins"] is None
        key.public_key().verify(bytes.fromhex(bundle["signature"]), integrity.digest(
            integrity.ORIGINS_DOMAIN, integrity.canonical_json(payload)))
        with pytest.raises(Exception):
            await api_v2.admin_add_trusted_origin(api_v2.TrustedOriginRequest(
                origin="https://*.cmed.example", by="Tester"), None)
        await api_v2.admin_remove_trusted_origin(api_v2.TrustedOriginRequest(
            origin="https://emr.cmed.example", by="Tester"), None)
        assert await repo.trusted_origins_version() >= v1
        assert "https://emr.cmed.example" not in (await api_v2._trusted_origins_bundle()
                                                  )["payload"]["origins"]
    finally:
        api_v2.ctx.signer = None
        _unserve()


# ============================================================
# 026: the portal - Google sign-in, roles, and who may change what
# ============================================================

def _portal_app(dbs, monkeypatch, *, google_email, owner="owner@uiu.example"):
    """The sign-in routes and the dashboard on one app; Google's two calls stood in."""
    import httpx
    import portal
    import dashboard
    from fastapi import FastAPI

    monkeypatch.setenv("AIMS_GOOGLE_CLIENT_ID", "client-123.apps.googleusercontent.com")
    monkeypatch.setenv("AIMS_GOOGLE_CLIENT_SECRET", "test-secret")
    monkeypatch.setenv("AIMS_PUBLIC_HOST", "aims.test")
    monkeypatch.setenv("AIMS_PORTAL_OWNER_EMAIL", owner)
    monkeypatch.setenv("AIMS_ADMIN_KEY", "the-admin-key")

    async def exchange(code, verifier):
        assert len(verifier) >= 43                         # PKCE verifier sent back
        return {"id_token": "signed-by-google"}

    def verify(token, nonce):
        return {"email": google_email["value"], "email_verified": True, "name": "Test Person",
                "nonce": nonce}

    monkeypatch.setattr(portal, "_exchange", exchange)
    monkeypatch.setattr(portal, "_verify_id_token", verify)
    _server(dbs)
    app = FastAPI()
    app.include_router(portal.router)
    app.include_router(dashboard.router)
    app.include_router(api_v2.router)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                             base_url="https://aims.test")


async def _sign_in(client, next_path="/dashboard"):
    from urllib.parse import parse_qs, urlparse
    start = await client.get(f"/auth/login?next={next_path}")
    assert start.status_code == 302
    query = parse_qs(urlparse(start.headers["location"]).query)
    assert query["code_challenge_method"] == ["S256"] and query["code_challenge"][0]
    assert query["redirect_uri"] == ["https://aims.test/auth/callback"]
    return await client.get(f"/auth/callback?state={query['state'][0]}&code=abc")


async def test_only_people_on_the_list_get_in_and_the_owner_always_does(dbs, monkeypatch):
    who = {"value": "stranger@gmail.example"}
    async with _portal_app(dbs, monkeypatch, google_email=who) as client:
        refused = await _sign_in(client)
        assert refused.status_code == 403 and "aims_portal" not in refused.cookies
        assert (await client.get("/auth/me")).status_code == 401

        who["value"] = "Owner@UIU.example"                 # Google's case, any case
        signed = await _sign_in(client, "/db/")
        assert signed.status_code == 302 and signed.headers["location"] == "/db/"
        assert "httponly" in signed.headers["set-cookie"].lower()
        assert "secure" in signed.headers["set-cookie"].lower()
        me = (await client.get("/auth/me")).json()
        assert (me["email"], me["role"]) == ("owner@uiu.example", "admin")
    try:
        async with dbs.rec.acquire() as conn:
            refused_logged = await conn.fetchval(
                "SELECT count(*) FROM audit_log WHERE event_type = 'portal.refused' "
                "AND actor_id = 'stranger@gmail.example'")
        assert refused_logged >= 1
    finally:
        _unserve()


async def test_a_researcher_looks_but_cannot_change_anything(dbs, monkeypatch):
    who = {"value": "owner@uiu.example"}
    async with _portal_app(dbs, monkeypatch, google_email=who) as client:
        await _sign_in(client)
        added = await client.post("/api/v2/admin/people",
                                  json={"email": "Researcher@uni.example", "role": "researcher"},
                                  headers={"X-AIMS-Portal": "1"})
        assert added.status_code == 200 and added.json()["email"] == "researcher@uni.example"

        # The same admin session, from another site: no X-AIMS-Portal, refused.
        forged = await client.post("/api/v2/admin/people",
                                   json={"email": "evil@x.example", "role": "admin"})
        assert forged.status_code == 403

        client.cookies.clear()
        who["value"] = "researcher@uni.example"
        await _sign_in(client)
        assert (await client.get("/api/v2/dashboard/clinics")).status_code == 200
        assert (await client.get("/api/v2/admin/people")).status_code == 403
        tried = await client.post("/api/v2/admin/people/remove",
                                  json={"email": "owner@uiu.example"},
                                  headers={"X-AIMS-Portal": "1"})
        assert tried.status_code == 403

        # The gateway's question, for the viewer and for an admin route.
        ok = await client.get("/auth/check", headers={"X-Forwarded-Uri": "/db/"})
        assert ok.status_code == 200 and ok.headers["x-portal-role"] == "researcher"
        assert (await client.get("/auth/check",
                                 headers={"X-Forwarded-Uri": "/api/v2/admin/people"})
                ).status_code == 403
    _unserve()


async def test_removing_a_person_ends_their_session_at_once(dbs, monkeypatch):
    who = {"value": "owner@uiu.example"}
    async with _portal_app(dbs, monkeypatch, google_email=who) as client:
        await _sign_in(client)
        await client.post("/api/v2/admin/people", json={"email": "leaver@uni.example",
                                                        "role": "researcher"},
                          headers={"X-AIMS-Portal": "1"})
        owner_cookie = client.cookies.get("aims_portal")
        client.cookies.clear()
        who["value"] = "leaver@uni.example"
        await _sign_in(client)
        assert (await client.get("/auth/me")).status_code == 200

        leaver_cookie = client.cookies.get("aims_portal")
        client.cookies.clear()
        client.cookies.set("aims_portal", owner_cookie, domain="aims.test")
        removed = await client.post("/api/v2/admin/people/remove",
                                    json={"email": "leaver@uni.example"},
                                    headers={"X-AIMS-Portal": "1"})
        assert removed.status_code == 200
        assert (await client.post("/api/v2/admin/people/remove",
                                  json={"email": "owner@uiu.example"},
                                  headers={"X-AIMS-Portal": "1"})).status_code == 409

        client.cookies.clear()
        client.cookies.set("aims_portal", leaver_cookie, domain="aims.test")
        assert (await client.get("/auth/me")).status_code == 401
        check = await client.get("/auth/check", headers={"X-Forwarded-Uri": "/dashboard",
                                                         "Accept": "text/html"})
        assert check.status_code == 302 and check.headers["location"].startswith("/auth/login")
    _unserve()


def test_sign_in_never_sends_anyone_to_another_site():
    import portal
    for bad in ("https://evil.example/", "//evil.example", "/\\evil", "", None, "/auth/logout"):
        assert portal.safe_next(bad) == "/dashboard"
    assert portal.safe_next("/db/?pgsql=postgres") == "/db/?pgsql=postgres"


# ============================================================
# 027: downloading a recording - decrypted by the archive worker, logged
# ============================================================

_DL_WAV = b"RIFF" + bytes(range(256)) * 40


def _download_app(dbs, monkeypatch, who, worker):
    """The portal app with downloads; the copy store and the archive worker stood in."""
    import httpx
    import downloads
    client = _portal_app(dbs, monkeypatch, google_email=who)
    client._transport.app.include_router(downloads.router)
    monkeypatch.setenv("AIMS_DECRYPT_URL", "http://archive-worker:7070")
    monkeypatch.setenv("AIMS_WORKER_KEY", "the-worker-key")
    api_v2.ctx.copy.get_presigned_download_url = \
        lambda key, expires: f"https://copies/get/{key}?expires={expires}"
    monkeypatch.setattr(downloads, "_client",
                        lambda: httpx.AsyncClient(transport=httpx.MockTransport(worker)))
    return client


async def _copied_recording(dbs, patient, *, plain=_DL_WAV):
    device = await enrolled_device(dbs.repo)
    v = visit(patient_id=patient)
    stem = f"{patient}_DR0042_HOSP003_101432_102847_20260913"
    sid = await _archived(dbs, device, v, stem=stem)
    await dbs.repo.record_cloud_copy(
        session_id=sid, kind="audio", object_key=f"copies/HOSP003/DR0042/2026-09-13/{stem}.v1.wav.enc",
        version=1, byte_length=len(plain) + 200, sha256=b"\x22" * 32,
        plain_sha256=hashlib.sha256(plain).digest())
    await dbs.repo.mark_copied(sid)
    return sid, stem


async def _researcher(client, who, email="researcher@uni.example"):
    who["value"] = "owner@uiu.example"
    await _sign_in(client)
    await client.post("/api/v2/admin/people", json={"email": email, "role": "researcher"},
                      headers={"X-AIMS-Portal": "1"})
    client.cookies.clear()
    who["value"] = email
    await _sign_in(client)


async def test_a_researcher_downloads_the_archived_wav_and_it_is_logged(dbs, monkeypatch):
    sid, stem = await _copied_recording(dbs, "PDOWN1")
    asked = []

    def worker(request):
        asked.append((request.headers.get("x-worker-key"), json.loads(request.content)))
        return httpx.Response(200, content=_DL_WAV,
                              headers={"Content-Length": str(len(_DL_WAV))})

    import httpx
    who = {"value": ""}
    async with _download_app(dbs, monkeypatch, who, worker) as client:
        await _researcher(client, who)
        got = await client.get(f"/api/v2/dashboard/recordings/{sid}/audio",
                               headers={"Sec-Fetch-Site": "same-origin"})
        assert got.status_code == 200 and got.content == _DL_WAV
        assert got.headers["content-type"] == "audio/wav"
        disposition = got.headers["content-disposition"]
        assert sid in disposition and "PDOWN1" not in disposition   # no patient id
        assert got.headers["cache-control"] == "no-store"

        # Not signed in, or started from another site: nothing.
        assert (await client.get(f"/api/v2/dashboard/recordings/{sid}/audio",
                                 headers={"Sec-Fetch-Site": "cross-site"})).status_code == 403
        client.cookies.clear()
        assert (await client.get(f"/api/v2/dashboard/recordings/{sid}/audio")).status_code == 401
    try:
        key, body = asked[0]
        assert key == "the-worker-key"
        assert body["url"].startswith("https://copies/get/copies/HOSP003/")
        assert body["plain_sha256"] == hashlib.sha256(_DL_WAV).hexdigest()
        assert body["expect"] == {"session_id": sid, "kind": "audio",
                                  "stem_sha256": hashlib.sha256(stem.encode()).hexdigest()}
        async with dbs.rec.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM portal_downloads WHERE session_id = $1", sid)
            logged = await conn.fetchval(
                "SELECT count(*) FROM audit_log WHERE event_type = 'recording.downloaded' "
                "AND actor_id = 'researcher@uni.example' AND session_id = $1", sid)
        assert (row["email"], row["outcome"], row["bytes"]) == (
            "researcher@uni.example", "completed", len(_DL_WAV))
        assert logged == 1
    finally:
        _unserve()


async def test_a_download_the_worker_refuses_or_cuts_short_is_logged_as_failed(dbs, monkeypatch):
    import httpx
    refused_sid, _ = await _copied_recording(dbs, "PDOWN2")
    short_sid, _ = await _copied_recording(dbs, "PDOWN3")

    def worker(request):
        if json.loads(request.content)["expect"]["session_id"] == refused_sid:
            return httpx.Response(409, json={"detail": "not the copy asked for"})
        return httpx.Response(200, content=_DL_WAV[:100],
                              headers={"Content-Length": str(len(_DL_WAV))})

    who = {"value": ""}
    async with _download_app(dbs, monkeypatch, who, worker) as client:
        await _researcher(client, who, "failing@uni.example")
        refused = await client.get(f"/api/v2/dashboard/recordings/{refused_sid}/audio",
                                   headers={"Accept": "text/html"})
        assert refused.status_code == 409 and "did not pass its check" in refused.text
        try:
            await client.get(f"/api/v2/dashboard/recordings/{short_sid}/audio")
        except Exception:
            pass                                   # the transport may notice the short body
    try:
        async with dbs.rec.acquire() as conn:
            rows = {r["session_id"]: r for r in await conn.fetch(
                "SELECT session_id, outcome, reason FROM portal_downloads "
                "WHERE session_id = ANY($1::text[])", [refused_sid, short_sid])}
        assert rows[refused_sid]["outcome"] == "failed"
        assert "not the copy asked for" in rows[refused_sid]["reason"]
        assert rows[short_sid]["outcome"] == "failed"
    finally:
        _unserve()


async def test_downloads_have_a_daily_limit_and_need_a_confirmed_copy(dbs, monkeypatch):
    import httpx
    sid, _ = await _copied_recording(dbs, "PDOWN4")
    bare_device = await enrolled_device(dbs.repo)
    uncopied = await _archived(dbs, bare_device, visit(patient_id="PDOWN5"),
                               stem="PDOWN5_DR0042_HOSP003_101432_102847_20260913")
    monkeypatch.setenv("AIMS_DOWNLOAD_DAILY_LIMIT", "1")

    def worker(request):
        return httpx.Response(200, content=_DL_WAV,
                              headers={"Content-Length": str(len(_DL_WAV))})

    who = {"value": ""}
    async with _download_app(dbs, monkeypatch, who, worker) as client:
        await _researcher(client, who, "limited@uni.example")
        assert (await client.get(f"/api/v2/dashboard/recordings/{uncopied}/audio")
                ).status_code == 409
        assert (await client.get(f"/api/v2/dashboard/recordings/{sid}/audio")).status_code == 200
        limited = await client.get(f"/api/v2/dashboard/recordings/{sid}/audio")
        assert limited.status_code == 429 and "limit" in limited.json()["detail"]
    _unserve()


# ============================================================
# 029: downloading in numbered batches - never a file twice, never one skipped
# ============================================================

def _wav_for(sid: str) -> bytes:
    return b"RIFF" + sid.encode("ascii") * 64


async def _bulk_recording(dbs, patient, doctor, day, minute):
    """A confirmed, copied recording by this doctor on this day, at 10:<minute>."""
    device = await enrolled_device(dbs.repo)
    v = visit(patient_id=patient, doctor_id=doctor, visit_date=day.isoformat())
    stem = f"{patient}_{doctor}_HOSP003_10{minute:02d}00_10{minute:02d}59_{day:%Y%m%d}"
    sid = await _archived(dbs, device, v, stem=stem)
    async with dbs.rec.acquire() as conn:
        await conn.execute("""
            UPDATE sessions SET session_date = $2, doctor_id = $3, local_start = $4,
                                opened_at = $5, total_duration_seconds = 600
             WHERE session_id = $1
        """, sid, day, doctor, datetime(2000, 1, 1, 10, minute).time(),
            datetime(day.year, day.month, day.day, 4, minute, tzinfo=timezone.utc))
    await dbs.repo.record_cloud_copy(
        session_id=sid, kind="audio", version=1,
        object_key=f"copies/HOSP003/{doctor}/{day.isoformat()}/{stem}.v1.wav.enc",
        byte_length=len(_wav_for(sid)) + 200, sha256=b"\x22" * 32,
        plain_sha256=hashlib.sha256(_wav_for(sid)).digest())
    await dbs.repo.mark_copied(sid)
    return sid


def _bulk_app(dbs, monkeypatch, who, worker):
    import bulk
    client = _download_app(dbs, monkeypatch, who, worker)
    client._transport.app.include_router(bulk.router)
    monkeypatch.setenv("AIMS_BULK_HOURS", "")
    return client


def _zip_of(response):
    import io
    import zipfile
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/zip"
    z = zipfile.ZipFile(io.BytesIO(response.content))
    assert z.testzip() is None
    rows = list(__import__("csv").DictReader(io.StringIO(z.read("manifest.csv").decode())))
    return z, rows


async def test_batches_hand_out_a_selection_without_repeats_or_gaps(dbs, monkeypatch):
    import httpx
    d1, d2 = date(2026, 8, 1), date(2026, 8, 2)
    made = [await _bulk_recording(dbs, f"PBULK{n}", doc, day, n) for n, (doc, day) in enumerate(
        [("DRBULKA", d1)] * 3 + [("DRBULKA", d2)] * 2 + [("DRBULKB", d2)] * 2)]
    short = set()

    def worker(request):
        sid = json.loads(request.content)["expect"]["session_id"]
        body = _wav_for(sid)
        if sid in short:                       # fails its check: cut short
            return httpx.Response(200, content=body[:50],
                                  headers={"Content-Length": str(len(body))})
        return httpx.Response(200, content=body, headers={"Content-Length": str(len(body))})

    monkeypatch.setenv("AIMS_BULK_BATCH_SIZE", "3")
    who = {"value": ""}
    sel = {"hospital_id": "HOSP003", "from": "2026-08-01", "to": "2026-08-02"}
    got: list = []
    async with _bulk_app(dbs, monkeypatch, who, worker) as client:
        await _researcher(client, who, "bulk@uni.example")
        H = {"X-AIMS-Portal": "1"}
        first = (await client.get("/api/v2/dashboard/bulk/preview", params=sel)).json()
        assert (first["total"], first["downloaded"], first["remaining"]) == (7, 0, 7)
        assert {(d["doctor_id"], d["files"]) for d in first["doctors"]} == {
            ("DRBULKA", 5), ("DRBULKB", 2)}
        assert first["next"] == {**first["next"], "batch_no": 1, "first_no": 1, "last_no": 3}

        # Without X-AIMS-Portal (another site's form): refused.
        assert (await client.post("/api/v2/dashboard/bulk/batches", json=sel)).status_code == 403

        for expect_no, expect_files in ((1, 3), (2, 3)):
            batch = (await client.post("/api/v2/dashboard/bulk/batches",
                                       json={**sel, "size": 50}, headers=H)).json()
            assert (batch["batch_no"], batch["files"]) == (expect_no, expect_files)
            assert (await client.get(f"/api/v2/dashboard/bulk/batches/{batch['id']}/check")
                    ).json() == {"ok": True}
            z, rows = _zip_of(await client.get(batch["url"]))
            assert "README.txt" in z.namelist()
            assert all(r["status"] == "OK" for r in rows)
            for r in rows:
                assert z.read(r["file"]) == _wav_for(r["session_id"])
                assert "PBULK" not in r["file"]                 # no patient id
                assert r["sha256"] == hashlib.sha256(_wav_for(r["session_id"])).hexdigest()
            got += [r["session_id"] for r in rows]

        middle = (await client.get("/api/v2/dashboard/bulk/preview", params=sel)).json()
        assert (middle["downloaded"], middle["remaining"]) == (6, 1)
        assert middle["next"]["first_no"] == 7 and middle["next"]["batch_no"] == 3

        # The last file fails its check: the batch completes, the file does not count.
        last = (set(made) - set(got)).pop()
        short.add(last)
        batch = (await client.post("/api/v2/dashboard/bulk/batches", json=sel, headers=H)).json()
        z, rows = _zip_of(await client.get(batch["url"]))
        assert [(r["session_id"], r["status"]) for r in rows] == [(last, "FAILED")]
        assert (await client.get("/api/v2/dashboard/bulk/preview", params=sel)).json()["remaining"] == 1

        # ... so it is in the next batch, and then the selection is done.
        short.clear()
        batch = (await client.post("/api/v2/dashboard/bulk/batches", json=sel, headers=H)).json()
        z, rows = _zip_of(await client.get(batch["url"]))
        assert [(r["session_id"], r["status"]) for r in rows] == [(last, "OK")]
        got.append(last)
        done = await client.post("/api/v2/dashboard/bulk/batches", json=sel, headers=H)
        assert done.status_code == 409 and "already have all 7" in done.json()["detail"]

        # One doctor of the same days: already all downloaded.
        one = (await client.get("/api/v2/dashboard/bulk/preview",
                                params={**sel, "doctor": "DRBULKB"})).json()
        assert (one["total"], one["remaining"]) == (2, 0)
        listed = (await client.get("/api/v2/dashboard/bulk/batches")).json()["batches"]
        assert [b["outcome"] for b in listed] == ["completed"] * 4
    _unserve()
    assert sorted(got) == sorted(made)                         # every file, once


async def test_batch_hours_daily_limit_and_a_stopped_batch(dbs, monkeypatch):
    import httpx
    day = date(2026, 8, 5)
    made = [await _bulk_recording(dbs, f"PLIM{n}", "DRLIMIT", day, n) for n in range(4)]

    def worker(request):
        body = _wav_for(json.loads(request.content)["expect"]["session_id"])
        return httpx.Response(200, content=body, headers={"Content-Length": str(len(body))})

    sel = {"hospital_id": "HOSP003", "from": "2026-08-05", "to": "2026-08-05"}
    H = {"X-AIMS-Portal": "1"}
    who = {"value": ""}
    async with _bulk_app(dbs, monkeypatch, who, worker) as client:
        # Outside the hours: a researcher is told when they open; an admin is not held.
        now = datetime.now()
        later = (now + timedelta(hours=2)).strftime("%H:%M")
        much_later = (now + timedelta(hours=3)).strftime("%H:%M")
        monkeypatch.setenv("AIMS_BULK_HOURS", f"{later}-{much_later}")
        await _researcher(client, who, "night@uni.example")
        closed = await client.post("/api/v2/dashboard/bulk/batches", json=sel, headers=H)
        assert closed.status_code == 403 and "open" in closed.json()["detail"]
        assert (await client.get("/api/v2/dashboard/bulk/preview", params=sel)
                ).json()["limits"]["window"]["open"] is False

        # Any time, but 2 files a day.
        monkeypatch.setenv("AIMS_BULK_HOURS", "")
        monkeypatch.setenv("AIMS_BULK_DAILY_FILES", "2")
        batch = (await client.post("/api/v2/dashboard/bulk/batches", json=sel, headers=H)).json()
        assert batch["files"] == 2
        _zip_of(await client.get(batch["url"]))
        limited = await client.post("/api/v2/dashboard/bulk/batches", json=sel, headers=H)
        assert limited.status_code == 429 and "limit of 2" in limited.json()["detail"]

        # A batch whose download stopped part way: not running any more, and
        # its files are left for the next batch.
        monkeypatch.setenv("AIMS_BULK_DAILY_FILES", "100")
        stuck = (await client.post("/api/v2/dashboard/bulk/batches", json=sel, headers=H)).json()
        async with dbs.rec.acquire() as conn:
            await conn.execute("UPDATE portal_batches SET outcome = 'started', started_at = now(), "
                               "progress_at = now() WHERE id = $1", stuck["id"])
        busy = await client.post("/api/v2/dashboard/bulk/batches", json=sel, headers=H)
        assert busy.status_code == 409 and "downloading now" in busy.json()["detail"]
        async with dbs.rec.acquire() as conn:
            await conn.execute("UPDATE portal_batches SET progress_at = now() - interval '1 hour' "
                               "WHERE id = $1", stuck["id"])
        view = (await client.get("/api/v2/dashboard/bulk/preview", params=sel)).json()
        assert (view["downloaded"], view["remaining"], view["running"]) == (2, 2, None)
        again = (await client.post("/api/v2/dashboard/bulk/batches", json=sel, headers=H)).json()
        assert again["first_no"] == 3 and again["files"] == 2
        z, rows = _zip_of(await client.get(again["url"]))
        assert sorted(r["session_id"] for r in rows) == sorted(made[2:])

        # The admin: no hours, no daily limit, and may see everyone's batches.
        monkeypatch.setenv("AIMS_BULK_HOURS", f"{later}-{much_later}")
        monkeypatch.setenv("AIMS_BULK_DAILY_FILES", "1")
        client.cookies.clear()
        who["value"] = "owner@uiu.example"
        await _sign_in(client)
        mine = (await client.post("/api/v2/dashboard/bulk/batches", json=sel, headers=H)).json()
        assert mine["files"] == 4                               # the admin has none yet
        _zip_of(await client.get(mine["url"]))
        everyone = (await client.get("/api/v2/dashboard/bulk/batches",
                                     params={"everyone": "true"})).json()["batches"]
        assert {"night@uni.example", "owner@uiu.example"} <= {b["email"] for b in everyone}
        # Someone else's batch is not theirs to download.
        theirs = next(b for b in everyone if b["email"] == "night@uni.example")
        assert (await client.get(theirs["url"])).status_code == 404
    _unserve()


# ============================================================
# 028: each researcher's own read-only database login
# ============================================================

ROLE_SCRIPTS = BACKEND / "scripts" / "roles"


async def _role_setup(pg):
    """What the migration runner does as the superuser on every update."""
    for uri, name in ((pg.recordings, "researcher_logins_recordings.sql"),
                      (pg.clinical, "researcher_logins_clinical.sql")):
        conn = await asyncpg.connect(uri)
        try:
            await conn.execute((ROLE_SCRIPTS / name).read_text(encoding="utf-8"))
            await conn.execute((ROLE_SCRIPTS / name).read_text(encoding="utf-8"))   # twice: safe
        finally:
            await conn.close()


def _as(uri, user, password):
    root, db = uri.rsplit("/", 1)
    host = root.split("@", 1)[1]
    return asyncpg.connect(f"postgresql://{user}:{password}@{host}/{db}")


async def test_a_researcher_gets_their_own_read_only_login(pg, dbs, monkeypatch):
    import portal
    await _role_setup(pg)
    who = {"value": "owner@uiu.example"}
    async with _portal_app(dbs, monkeypatch, google_email=who) as client:
        await _sign_in(client)
        added = (await client.post("/api/v2/admin/people",
                                   json={"email": "Dr.Analyst@uni.example", "role": "researcher"},
                                   headers={"X-AIMS-Portal": "1"})).json()
        login = added["db_login"]
        assert login["db_user"] == portal.db_user_for("dr.analyst@uni.example")
        assert login["db_user"].startswith("r_dr_analyst_")

        # Reads both databases; changes nothing; sees no sign-in secrets.
        for uri in (pg.recordings, pg.clinical):
            conn = await _as(uri, login["db_user"], login["password"])
            try:
                await conn.fetchval("SELECT count(*) FROM pg_class")
                table = "sessions" if uri == pg.recordings else "encounters"
                await conn.fetchval(f"SELECT count(*) FROM {table}")
                for right in ("INSERT", "UPDATE", "DELETE", "TRUNCATE"):
                    assert not await conn.fetchval(
                        "SELECT has_table_privilege(current_user, $1, $2)", table, right)
                with pytest.raises(asyncpg.InsufficientPrivilegeError):
                    await conn.execute(f"BEGIN READ WRITE; DELETE FROM {table}; COMMIT")
                await conn.execute("ROLLBACK")
                if uri == pg.recordings:
                    with pytest.raises(asyncpg.InsufficientPrivilegeError):
                        await conn.fetchval("SELECT count(*) FROM portal_logins")
                    with pytest.raises(asyncpg.PostgresError):
                        await conn.execute("CREATE ROLE intruder LOGIN")
            finally:
                await conn.close()

        # The gateway hands the viewer this person's login name, and only it.
        client.cookies.clear()
        who["value"] = "dr.analyst@uni.example"
        await _sign_in(client)
        check = await client.get("/auth/check", headers={"X-Forwarded-Uri": "/db/"})
        assert check.headers["x-portal-db-user"] == login["db_user"]

        # A new password: the old one stops working.
        client.cookies.clear()
        who["value"] = "owner@uiu.example"
        await _sign_in(client)
        again = (await client.post("/api/v2/admin/people/db-login",
                                   json={"email": "dr.analyst@uni.example"},
                                   headers={"X-AIMS-Portal": "1"})).json()
        assert again["db_user"] == login["db_user"] and again["password"] != login["password"]
        with pytest.raises(asyncpg.InvalidPasswordError):
            await _as(pg.recordings, login["db_user"], login["password"])
        (await _as(pg.recordings, again["db_user"], again["password"])).terminate()

        # Removed from the portal: the login is gone too.
        removed = await client.post("/api/v2/admin/people/remove",
                                    json={"email": "dr.analyst@uni.example"},
                                    headers={"X-AIMS-Portal": "1"})
        assert removed.status_code == 200
        with pytest.raises(asyncpg.PostgresError):
            await _as(pg.recordings, again["db_user"], again["password"])
    try:
        async with dbs.rec.acquire() as conn:
            left = await conn.fetchval("SELECT count(*) FROM pg_roles WHERE rolname = $1",
                                       login["db_user"])
            logged = await conn.fetchval(
                "SELECT count(*) FROM audit_log WHERE event_type = 'portal.db_login_set'")
        assert left == 0 and logged >= 2
    finally:
        _unserve()


async def test_the_role_functions_make_researcher_logins_and_nothing_else(pg, dbs):
    import portal
    await _role_setup(pg)
    async with dbs.rec.acquire() as conn:
        for bad in ("aims_api", "postgres", "r_x; DROP ROLE aims_api", "R_upper"):
            with pytest.raises(asyncpg.PostgresError):
                await conn.execute("SELECT portal_db_login_set($1, $2)", bad,
                                   portal.scram_verifier("x"))
        with pytest.raises(asyncpg.PostgresError, match="verifier"):
            await conn.execute("SELECT portal_db_login_set('r_plain_abc123', 'a plain password')")
        await conn.execute("CREATE ROLE r_not_ours_000000 LOGIN")
        try:
            with pytest.raises(asyncpg.PostgresError, match="not a researcher login"):
                await conn.execute("SELECT portal_db_login_set($1, $2)", "r_not_ours_000000",
                                   portal.scram_verifier("x"))
            await conn.execute("SELECT portal_db_login_drop('r_not_ours_000000')")
            assert await conn.fetchval("SELECT rolcanlogin FROM pg_roles "
                                       "WHERE rolname = 'r_not_ours_000000'")
        finally:
            await conn.execute("DROP ROLE r_not_ours_000000")


# ============================================================
# Odd values: accepted, read the way clinics write them, never lost
# ============================================================

async def test_a_message_full_of_odd_values_still_confirms_its_recording(dbs):
    """
    The capital F, the Fahrenheit, the pounds, the "7 days": each once
    quarantined API 2 or API 3, which left the recording unconfirmed and
    eventually erased. Now accepted, read correctly, warned about.
    """
    repo = dbs.repo
    device = await enrolled_device(repo)
    key = await repo.create_cmed_key(label="cmed", created_by="tests", expires_at=None,
                                     cmed_hospital_ids=["CMED-DHOLPUR"])
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from grants import GrantIssuer
    api_v2.ctx.repo, api_v2.ctx.clinical = repo, dbs.clinical
    api_v2.ctx.grants = GrantIssuer(Ed25519PrivateKey.generate())
    sent = {}

    class Request:
        headers = {"x-cmed-key": key}

        async def body(self):
            return json.dumps(sent).encode()

    try:
        v = visit()
        five = {"patient_id": v.patient_id, "doctor_id": v.doctor_id,
                "hospital_id": v.cmed_hospital_id, "start_time": v.start_time,
                "date": v.visit_date}
        sent.update(five, demographics={"name": "Rahima", "sex": "F", "age_years": "34 years",
                                        "date_of_birth": "11/03/1992"},
                    paramedic={"weight_kg": "128 lb", "height_cm": "5.2",
                               "temperature_c": "৯৮.৬", "blood_pressure": "120/80 mmHg",
                               "spo2_percent": "98%"})          # previous_visit left out
        reply = await clinical.receive(Request(), "patient_information")
        answer = json.loads(reply.body)
        assert (reply.status_code, answer["code"]) == (202, "ACCEPTED")
        warned = {w["field"] for w in answer["warnings"]}
        assert {"demographics.sex", "paramedic.weight_kg", "paramedic.height_cm",
                "paramedic.temperature_c", "previous_visit"} <= warned

        sent.clear()
        sent.update(five, issued_at="2026-09-13 10:40:00", diagnoses="Fever",
                    items=[{"medicine": "Napa 500", "dosage": "1 tab"}, {"dose": "2 tab"}],
                    investigations=[], follow_up="৭ দিন পর")
        reply = await clinical.receive(Request(), "prescription")
        assert (reply.status_code, json.loads(reply.body)["code"]) == (202, "ACCEPTED")

        minted = await api_v2.mint_grant(five, device=device)
        assert minted["confirmation"] == "confirmed"

        async with dbs.cli.acquire() as conn:
            d = await conn.fetchrow(
                "SELECT d.sex, d.age_years, d.date_of_birth FROM encounter_demographics d "
                "JOIN encounters e USING (encounter_id) WHERE e.patient_id = $1", v.patient_id)
            o = await conn.fetchrow(
                "SELECT o.weight_kg, o.height_cm, o.temperature_c, o.systolic, o.spo2_percent,"
                " o.other FROM paramedic_observations o JOIN encounters e USING (encounter_id)"
                " WHERE e.patient_id = $1", v.patient_id)
            rx = await conn.fetchrow(
                "SELECT p.prescription_id, p.follow_up_date, p.issued_at FROM prescriptions p "
                "JOIN encounters e USING (encounter_id) WHERE e.patient_id = $1", v.patient_id)
            drugs = await conn.fetch("SELECT drug, dose FROM prescription_items "
                                     "WHERE prescription_id = $1 ORDER BY line_no",
                                     rx["prescription_id"])
            diagnoses = await conn.fetch("SELECT * FROM diagnoses WHERE prescription_id = $1",
                                         rx["prescription_id"])
            quarantined = await conn.fetchval("SELECT count(*) FROM intake_quarantine")
        assert (d["sex"], d["age_years"], str(d["date_of_birth"])) == ("female", 34, "1992-03-11")
        assert (float(o["weight_kg"]), float(o["temperature_c"]), o["systolic"],
                o["spo2_percent"]) == (58.1, 37.0, 120, 98)
        other = o["other"] if isinstance(o["other"], dict) else json.loads(o["other"])
        assert o["height_cm"] is None and other["height_cm"] == "5.2"   # not guessed, kept
        assert str(rx["follow_up_date"]) == "2026-09-20"
        assert rx["issued_at"].utcoffset() is not None
        assert [(r["drug"], r["dose"]) for r in drugs] == [("Napa 500", "1 tab")]
        assert len(diagnoses) == 1 and quarantined == 0
    finally:
        api_v2.ctx.repo = api_v2.ctx.clinical = api_v2.ctx.grants = None


async def test_api3_brings_the_doctors_findings_and_api2_keeps_no_name(dbs):
    """API 2: the paramedic's data, no name. API 3: the whole consultation."""
    repo = dbs.repo
    device = await enrolled_device(repo)
    key = await repo.create_cmed_key(label="cmed", created_by="tests", expires_at=None,
                                     cmed_hospital_ids=["CMED-DHOLPUR"])
    api_v2.ctx.repo, api_v2.ctx.clinical = repo, dbs.clinical
    sent = {}

    class Request:
        headers = {"x-cmed-key": key}

        async def body(self):
            return json.dumps(sent).encode()

    try:
        v = visit()
        five = {"patient_id": v.patient_id, "doctor_id": v.doctor_id,
                "hospital_id": v.cmed_hospital_id, "start_time": v.start_time,
                "date": v.visit_date}
        sent.update(five, demographics={"name": "Rahima", "phone": "017", "sex": "female",
                                        "date_of_birth": "1992-03-11"},
                    paramedic={"blood_pressure": "120/80"}, previous_visit=None)
        reply = json.loads((await clinical.receive(Request(), "patient_information")).body)
        assert reply["code"] == "ACCEPTED"
        assert any(w["field"] == "demographics" and "not stored" in w["note"]
                   for w in reply["warnings"])

        rx = {**five, "issued_at": "2026-09-13T10:40:00+06:00",
              "chief_complaints": [{"complaint": "Fever", "duration": "3 days"}],
              "on_examination": {"jaundice": "absent"},
              "items": [{"drug": "Napa 500"}], "diagnoses": ["Fever"], "investigations": []}
        sent.clear(); sent.update(rx)
        assert (await clinical.receive(Request(), "prescription")).status_code == 202
        # The same prescription with a corrected examination: the next version.
        sent.clear(); sent.update({**rx, "on_examination": {"jaundice": "mild"}})
        again = json.loads((await clinical.receive(Request(), "prescription")).body)
        assert (again["code"], again["version"]) == ("ACCEPTED", 2)

        async with dbs.cli.acquire() as conn:
            names = await conn.fetchrow(
                "SELECT p.full_name, p.phone, d.full_name AS shown, d.sex "
                "FROM patients p JOIN encounters e USING (patient_id) "
                "JOIN encounter_demographics d USING (encounter_id) WHERE p.patient_id = $1",
                v.patient_id)
            raw = await conn.fetchval("SELECT body::text FROM intake_records "
                                      "WHERE patient_id = $1 AND kind = 'patient_information'",
                                      v.patient_id)
            rows = await conn.fetch(
                "SELECT p.version, f.section, f.item, f.value, f.detail FROM consultation_findings f "
                "JOIN prescriptions p USING (prescription_id) JOIN encounters e USING (encounter_id) "
                "WHERE e.patient_id = $1 ORDER BY p.version, f.line_no", v.patient_id)
        assert (names["full_name"], names["phone"], names["shown"], names["sex"]) == (
            None, None, None, "female")
        assert "Rahima" not in raw and "017" not in raw          # not even in the stored message
        assert [(r["version"], r["section"], r["item"], r["value"]) for r in rows] == [
            (1, "chief_complaint", "Fever", None), (1, "on_examination", "jaundice", "absent"),
            (2, "chief_complaint", "Fever", None), (2, "on_examination", "jaundice", "mild")]
        detail = rows[0]["detail"]
        assert (json.loads(detail) if isinstance(detail, str) else detail) == {"duration": "3 days"}
    finally:
        api_v2.ctx.repo = api_v2.ctx.clinical = None
