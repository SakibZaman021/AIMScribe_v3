"""
One visit, one folder (migration 044, clinical/006, visit_layout.py).

The archive kept copies by day; finding one patient meant opening record
after record. Now a visit has a key - <patient>_<doctor>_<visit_id> - that is
its folder in the cloud archive and its row in v_visit_folder.
"""
from __future__ import annotations

import json
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND / "src"))
sys.path.insert(0, str(BACKEND / "tests"))
sys.path.insert(0, str(BACKEND / "archive_worker"))

import api_v2                                   # noqa: E402
import visit_layout                             # noqa: E402

from test_v3_server import (DEVICE, FIELDS, STEM, api2, archived, copy_of, mint,  # noqa: E402,F401
                            open_session, prescription, send, server)

pytestmark = pytest.mark.asyncio

KEY = "P0012345_DR0042_20260913T101432"          # FIELDS has no visit_id: start_time names it


# ---------------------------------------------------------------- the key

def test_the_visit_key():
    vk = api_v2.visit_key
    assert vk("P4347460", "DR4290371", "VIS-T-31496") == "P4347460_DR4290371_VIS-T-31496"
    assert vk("P4347460", "DR4290371", None, "2026-11-17T11:50:39+06:00") == \
        "P4347460_DR4290371_20261117T115039"
    assert vk("P1", "DR1", None, None, fallback="01JB8XQ4M7") == "P1_DR1_01JB8XQ4M7"
    assert vk("P1", None, "V-1") == "P1_unknown_V-1"
    assert vk("P1", "DR1") is None and vk("", "DR1", "V-1") is None
    assert vk("P1", "DR1", "../x") is None                 # never a path


def test_a_copy_moves_folder_but_keeps_its_name():
    """The encryption binds the file name: only the folder may change."""
    session = {"session_id": "01S", "file_stem": STEM, "patient_id": "P0012345",
               "doctor_id": "DR0042", "hospital_id": "HOSP003", "session_date": "2026-09-13"}
    old = api_v2.copy_object_key(session, "audio", 2)
    new = api_v2.copy_object_key(session, "audio", 2, KEY)
    assert old == f"copies/HOSP003/DR0042/2026-09-13/{STEM}.v2.wav.enc"
    assert new == f"visits/P0012345/{KEY}/{STEM}.v2.wav.enc"
    assert old.rsplit("/", 1)[1] == new.rsplit("/", 1)[1]
    import cloudcopy
    import restore
    assert restore.expected_binding(Path(new.rsplit("/", 1)[1]))["stem_sha256"] == \
        cloudcopy.stem_fingerprint(STEM)


# ---------------------------------------------------------------- new copies

async def test_a_new_copy_goes_to_its_visits_folder_and_is_registered(server):
    sid = await archived(server)
    place = await api_v2.archive_copy_authorize(
        api_v2.CopyAuthorizeRequest(session_id=sid, kind="audio"))
    assert place["object_key"] == f"visits/P0012345/{KEY}/{STEM}.v1.wav.enc"
    assert server.repo.sessions[sid]["visit_key"] == KEY

    await api_v2.archive_copy_complete(api_v2.CopyCompleteRequest(
        session_id=sid, copies=[copy_of(server, "audio", place["object_key"])]))
    assert server.repo.copies[-1]["layout"] == "by_visit"
    registered = server.clinical.visit_objects[-1]
    assert (registered["visit_key"], registered["patient_id"], registered["kind"],
            registered["session_id"]) == (KEY, "P0012345", "audio", sid)


async def test_cmeds_visit_id_names_the_folder(server):
    _, reply = await mint({**FIELDS, "visit_id": "VIS-T-31496"})
    sid = open_session(server, segments=1)
    await api_v2._link_grant(sid, reply["jti"], "P0012345", DEVICE)
    session = server.repo.sessions[sid]
    assert await api_v2.visit_key_of(session) == "P0012345_DR0042_VIS-T-31496"


async def test_a_reassigned_recording_never_takes_the_first_patients_visit(server):
    _, reply = await mint({**FIELDS, "visit_id": "VIS-WRONG"})
    sid = open_session(server, segments=1)
    await api_v2._link_grant(sid, reply["jti"], "P0012345", DEVICE)
    server.repo.sessions[sid].update(patient_id="P0099999", visit_id="VIS-WRONG")
    assert await api_v2.visit_key_of(server.repo.sessions[sid]) == f"P0099999_DR0042_{sid}"


async def test_a_refusal_removes_the_visits_file_register(server):
    sid = await archived(server)
    place = await api_v2.archive_copy_authorize(
        api_v2.CopyAuthorizeRequest(session_id=sid, kind="audio"))
    await api_v2.archive_copy_complete(api_v2.CopyCompleteRequest(
        session_id=sid, copies=[copy_of(server, "audio", place["object_key"])]))
    assert server.clinical.visit_objects
    await api_v2._erase_session(sid, state="refused")
    assert server.clinical.visit_objects == []


def test_the_segment_bucket_code_never_deletes_a_visits_copy(monkeypatch):
    monkeypatch.setattr(api_v2.ctx, "copy", SimpleNamespace(bucket="copies"))
    monkeypatch.setattr(api_v2.ctx, "minio", SimpleNamespace(bucket="pieces"))
    with pytest.raises(RuntimeError):
        api_v2._remove_object(f"visits/P1/{KEY}/x.v1.wav.enc")


# ---------------------------------------------------------------- on PostgreSQL

try:
    from test_db_integration import a_session, dbs, enrolled_device, pg  # noqa: E402,F401
    HAVE_POSTGRES = True
except pytest.skip.Exception:
    HAVE_POSTGRES = False

postgres = pytest.mark.skipif(not HAVE_POSTGRES, reason="pgserver is not installed")


class Stat(SimpleNamespace):
    pass


class FakeStore:
    """The copy bucket: copy inside it, and say what it holds."""

    def __init__(self, bucket="aimscribe-archive", *, corrupt=False):
        self.bucket, self.objects, self.corrupt = bucket, {}, corrupt
        self.client = SimpleNamespace(copy_object=self._copy, stat_object=self._stat,
                                      remove_object=self._remove)
        self.removed = []

    def _copy(self, bucket, target, source):
        size, etag = self.objects[source.object_name]
        self.objects[target] = (size - 1 if self.corrupt else size, etag)

    def _remove(self, bucket, key):
        self.objects.pop(key, None)
        self.removed.append(key)

    def _stat(self, bucket, key):
        if key not in self.objects:
            raise RuntimeError(f"NoSuchKey: {key}")
        size, etag = self.objects[key]
        return Stat(size=size, etag=etag)


async def _archived_on_postgres(dbs, *, visit_id="VIS-T-31496"):
    """A confirmed recording with one by-day copy, as the live server holds them."""
    from test_db_integration import visit as a_visit
    repo = dbs.repo
    device = await enrolled_device(repo)
    sid = await a_session(repo, device)
    patient = f"P{uuid.uuid4().hex[:7]}"
    v = a_visit(patient_id=patient)
    jti = uuid.uuid4().hex
    await repo.create_authorisation(jti=jti, device_id=device["device_id"],
                                    hospital_id="HOSP003", visit=v,
                                    expires_at=datetime.now(timezone.utc), visit_id=visit_id)
    stem = f"{patient}_DR0042_HOSP003_101432_102847_20260913"
    async with dbs.rec.acquire() as conn:
        await conn.execute("""
            UPDATE sessions SET patient_id = $2, grant_jti = $3, visit_id = $4,
                   confirmation = 'confirmed', file_stem = $5, archived_at = now()
             WHERE session_id = $1""", sid, patient, jti, visit_id, stem)
    old = f"copies/HOSP003/DR0042/2026-09-13/{stem}.v1.wav.enc"
    await repo.record_cloud_copy(session_id=sid, kind="audio", object_key=old, version=1,
                                 byte_length=1000, sha256=b"\1" * 32, plain_sha256=None)
    return sid, patient, stem, old


@postgres
async def test_the_backfill_moves_registers_and_runs_again_safely(dbs):
    store = FakeStore()
    api_v2.ctx.repo, api_v2.ctx.clinical = dbs.repo, dbs.clinical
    api_v2.ctx.copy, api_v2.ctx.copy_json = store, None
    try:
        sid, patient, stem, old = await _archived_on_postgres(dbs)
        store.objects[old] = (1000, "ab" * 16)
        key = f"{patient}_DR0042_VIS-T-31496"
        new = f"visits/{patient}/{key}/{stem}.v1.wav.enc"

        dry = await visit_layout.backfill(apply=False)
        assert dry["copies"]["would_move"] >= 1 and new not in store.objects
        async with dbs.rec.acquire() as conn:                           # nothing written
            assert await conn.fetchval("SELECT visit_key FROM sessions WHERE session_id = $1",
                                       sid) is None

        done = await visit_layout.backfill(apply=True)
        # (Other tests' copies in this database are not in this store: they fail, unrecorded.)
        assert not any(f["object_key"] == old for f in done["copies"]["failed"])
        assert new in store.objects
        assert old in store.objects                                     # the old one stays
        async with dbs.rec.acquire() as conn:
            assert await conn.fetchval("SELECT visit_key FROM sessions WHERE session_id = $1",
                                       sid) == key
        async with dbs.rec.acquire() as conn:
            rows = await conn.fetch("SELECT object_key, layout, copied_from, sha256 "
                                    "FROM cloud_copies WHERE session_id = $1 ORDER BY id", sid)
        assert [(r["object_key"], r["layout"], r["copied_from"]) for r in rows] == [
            (old, "by_day", None), (new, "by_visit", old)]
        assert rows[0]["sha256"] == rows[1]["sha256"]
        async with dbs.cli.acquire() as conn:
            registered = await conn.fetchrow("SELECT * FROM visit_objects WHERE object_key = $1",
                                             new)
        assert (registered["visit_key"], registered["patient_id"], registered["visit_id"]) == (
            key, patient, "VIS-T-31496")

        async with dbs.rec.acquire() as conn:
            visit = await conn.fetchrow("SELECT * FROM v_visit_recordings WHERE visit_key = $1",
                                        key)
        assert (visit["session_ids"], visit["confirmed"], visit["audio_objects"]) == (
            [sid], True, [new])

        again = await visit_layout.backfill(apply=True)
        assert again["copies"]["moved"] == 0
        async with dbs.rec.acquire() as conn:
            assert await conn.fetchval("SELECT count(*) FROM cloud_copies WHERE session_id = $1",
                                       sid) == 2
    finally:
        api_v2.ctx.repo = api_v2.ctx.clinical = api_v2.ctx.copy = None


@postgres
async def test_a_copy_that_does_not_read_back_the_same_is_not_recorded(dbs):
    store = FakeStore(corrupt=True)
    api_v2.ctx.repo, api_v2.ctx.clinical = dbs.repo, dbs.clinical
    api_v2.ctx.copy, api_v2.ctx.copy_json = store, None
    try:
        sid, _, _, old = await _archived_on_postgres(dbs)
        store.objects[old] = (1000, "ab" * 16)
        report = await visit_layout.move_copies(apply=True)
        assert any(f["object_key"] == old for f in report["failed"])
        async with dbs.rec.acquire() as conn:
            assert await conn.fetchval("SELECT count(*) FROM cloud_copies WHERE session_id = $1",
                                       sid) == 1
    finally:
        api_v2.ctx.repo = api_v2.ctx.clinical = api_v2.ctx.copy = None


@postgres
async def test_one_row_per_visit_with_everything_on_it(dbs):
    """v_visit_folder and patient_folder(): demographics, prescription and files together."""
    import clinical_v4
    api_v2.ctx.clinical = dbs.clinical
    try:
        patient, visit_id = f"P{uuid.uuid4().hex[:7]}", "VIS-T-777"
        five = {"visit_id": visit_id, "patient_id": patient, "doctor_id": "DR4290371",
                "hospital_id": "Susastho-Clinic-Test-1",
                "start_time": "2026-11-17T11:50:39+06:00", "date": "2026-11-17"}
        info = {**five, "demographics": {"sex": "male", "date_of_birth": "1992-01-01",
                                         "age": "34Y"},
                "observations": [{"category": "vitals", "items": [
                    {"name": "Blood Pressure", "value": "128/90", "unit": "mmHg"}]}]}
        rx = {**five, "diagnosis": [{"code": "J06.9", "display": "Upper respiratory infection"}],
              "drug_orders": [{"form": "Tablet", "medication_name": "Napa", "strength": "500 mg",
                               "dose": "1+1+1", "duration": "5 days"}],
              "lab_orders": [{"test_name": "CBC"}], "advice": [{"instruction": "Rest"}],
              "issued_at": "2026-11-17T12:03:35+06:00"}
        for kind, body in (("patient_information", info), ("prescription", rx)):
            await clinical_v4.file_message(kind, body, five["hospital_id"], visit_id, patient,
                                           clinical_v4.read(kind, body), "tests")
        key = f"{patient}_DR4290371_{visit_id}"
        assert key == api_v2.visit_key(patient, "DR4290371", visit_id)   # SQL and Python agree
        await dbs.clinical.record_visit_object(
            visit_key=key, patient_id=patient, doctor_id="DR4290371", visit_id=visit_id,
            hospital_id="HOSP_SUSASTHO_TEST", session_id="01SESSION", part_no=None,
            kind="audio", object_key=f"visits/{patient}/{key}/x.v1.wav.enc", version=1,
            byte_length=10)

        rows = await dbs.clinical.visit_folder(patient)
        assert len(rows) == 1
        row = rows[0]
        assert (row["visit_key"], row["sex"], row["age"], row["source"]) == (
            key, "male", "34Y", "interface 4")
        assert row["vitals"] == "Blood Pressure 128/90 mmHg"
        assert row["diagnoses"] == "Upper respiratory infection"
        assert row["drugs"] == "Tablet Napa 500 mg 1+1+1 5 days"
        assert (row["tests"], row["advice"]) == ("CBC", "Rest")
        assert row["audio_objects"] == [f"visits/{patient}/{key}/x.v1.wav.enc"]
        assert row["session_ids"] == ["01SESSION"]
        assert row["archive_folder"] == f"visits/{patient}/{key}/"
    finally:
        api_v2.ctx.clinical = None


@postgres
async def test_a_visit_without_a_visit_id_is_keyed_by_its_start(dbs):
    """Interface 2: no visit_id; the key is CMED's start_time, as Python makes it."""
    import integrity
    from confirmation import Visit
    patient = f"P{uuid.uuid4().hex[:7]}"
    v = Visit(patient_id=patient, doctor_id="DR0042", cmed_hospital_id="CMED-DHOLPUR",
              start_time="2026-09-13T10:14:32+06:00", visit_date="2026-09-13")
    body = {"patient_id": patient, "doctor_id": "DR0042", "hospital_id": "CMED-DHOLPUR",
            "start_time": v.start_time, "date": v.visit_date,
            "demographics": {"sex": "female", "age_years": 41}, "paramedic": None,
            "previous_visit": None}
    record, _, _ = await dbs.clinical.receive(
        kind="patient_information", visit=v, hospital_id="HOSP003", body=body,
        body_sha256=integrity.sha256_bytes(json.dumps(body).encode()))
    await dbs.clinical.load(record)
    rows = await dbs.clinical.visit_folder(patient)
    assert [(r["visit_key"], r["sex"], r["age_years"], r["source"]) for r in rows] == [
        (api_v2.visit_key(patient, "DR0042", None, v.start_time), "female", 41, "interface 2")]


@postgres
async def test_old_copies_go_only_once_their_twin_is_in_the_store(dbs):
    """045: the user's decision of 17 Nov - remove the by-day copies, safely."""
    store = FakeStore()
    api_v2.ctx.repo, api_v2.ctx.clinical = dbs.repo, dbs.clinical
    api_v2.ctx.copy, api_v2.ctx.copy_json = store, None
    try:
        sid, patient, stem, old = await _archived_on_postgres(dbs)
        store.objects[old] = (1000, "ab" * 16)
        await visit_layout.move_copies(apply=True)
        new = next(k for k in store.objects if k.startswith(f"visits/{patient}/"))

        dry = await visit_layout.retire_old_copies(apply=False)
        assert dry["would_remove"] >= 1 and old in store.objects

        done = await visit_layout.retire_old_copies(apply=True)
        assert old not in store.objects and new in store.objects
        assert not any(k["object_key"] == old for k in done["kept"])
        async with dbs.rec.acquire() as conn:
            row = await conn.fetchrow("SELECT removed_at FROM cloud_copies WHERE object_key = $1",
                                      old)
        assert row["removed_at"] is not None                   # the row stays, marked
        # Downloads see only the copy that is there.
        assert [c["object_key"] for c in await dbs.repo.copies_for(sid)] == [new]
        again = await visit_layout.retire_old_copies(apply=True)
        assert not any(k["object_key"] == old for k in again["kept"])
    finally:
        api_v2.ctx.repo = api_v2.ctx.clinical = api_v2.ctx.copy = None


@postgres
async def test_an_old_copy_whose_twin_is_missing_is_kept(dbs):
    store = FakeStore()
    api_v2.ctx.repo, api_v2.ctx.clinical = dbs.repo, dbs.clinical
    api_v2.ctx.copy, api_v2.ctx.copy_json = store, None
    try:
        sid, patient, stem, old = await _archived_on_postgres(dbs)
        store.objects[old] = (1000, "ab" * 16)
        await visit_layout.move_copies(apply=True)
        new = next(k for k in store.objects if k.startswith(f"visits/{patient}/"))
        del store.objects[new]                                  # the twin vanished
        report = await visit_layout.retire_old_copies(apply=True)
        assert any(k["object_key"] == old for k in report["kept"])
        assert old in store.objects and old not in store.removed
    finally:
        api_v2.ctx.repo = api_v2.ctx.clinical = api_v2.ctx.copy = None
