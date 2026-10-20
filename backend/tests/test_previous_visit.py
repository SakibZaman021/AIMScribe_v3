"""
The previous prescription, and history CMED sends again (SRS-CRI-02).

The owner's rule: what is already held and identical is reported as held and
stored again nowhere; a changed one is kept as that visit's newest version; a
visit not held is stored under its real doctor and clinic. CMED can ask what is
held before it sends anything.

The first part needs nothing. The second runs against real PostgreSQL where
the `pgserver` package is installed, like tests/test_db_integration.py, and is
skipped elsewhere. The last part uses the in-memory server of test_v3_server.

    python -m pytest tests/test_previous_visit.py
"""
from __future__ import annotations

import json
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest
import pytest_asyncio

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND / "src"))
sys.path.insert(0, str(BACKEND / "tests"))

import api_v2                                   # noqa: E402
import clinical                                 # noqa: E402
import clinical_model as cm                     # noqa: E402
from confirmation import Visit                  # noqa: E402

from test_v3_server import (                    # noqa: E402,F401
    DEVICE, FIELDS, NOW, api2, mint, open_session, prescription, send, server)

try:
    from test_db_integration import dbs, pg     # noqa: E402,F401
    HAVE_POSTGRES = True
except pytest.skip.Exception:
    HAVE_POSTGRES = False

asyncio = pytest.mark.asyncio


def postgres(test):
    return pytest.mark.skipif(not HAVE_POSTGRES, reason="pgserver is not installed")(
        asyncio(test))


# ============================================================
# The fingerprint (no database)
# ============================================================

# The worked example in CMED_DATA_CONTRACT.md. If this changes, CMED's copy of
# the rule no longer agrees with ours and every comparison says "changed".
EXAMPLE = {
    "issued_at": "2026-10-01T10:20:00+06:00",
    "items": [{"drug": "Paracetamol", "dose": "500 mg", "frequency": "1+1+1",
               "duration": "5 days"}],
    "diagnoses": ["Fever"],
    "investigations": ["CBC"],
    "advice": "Rest and fluids",
    "follow_up": "2026-10-08",
}
EXAMPLE_CANONICAL = (
    '{"advice":"Rest and fluids","diagnoses":["Fever"],"follow_up":"2026-10-08",'
    '"investigations":["CBC"],"items":[{"dose":"500 mg","drug":"Paracetamol",'
    '"duration":"5 days","frequency":"1+1+1","instructions":null}],"notes":null}')
EXAMPLE_FINGERPRINT = "f1e7109404fd5d4010c358f0b3afdec79699955299a089eaf2c830fb60ec4806"


def test_the_worked_example_is_pinned():
    import hashlib
    assert hashlib.sha256(EXAMPLE_CANONICAL.encode("utf-8")).hexdigest() == \
        EXAMPLE_FINGERPRINT
    assert cm.fingerprint(cm.prescription(EXAMPLE)) == EXAMPLE_FINGERPRINT


def test_only_what_was_prescribed_counts():
    same = cm.fingerprint(cm.prescription(EXAMPLE))
    reissued = dict(reversed(list(EXAMPLE.items())), issued_at="2026-10-02T09:00:00+06:00",
                    cmed_internal_id="abc")
    assert cm.fingerprint(cm.prescription(reissued)) == same
    padded = dict(EXAMPLE, advice="  Rest and fluids  ")
    assert cm.fingerprint(cm.prescription(padded)) == same
    # Every normalising rule in CMED_DATA_CONTRACT.md section 3, at once.
    loose = {"diagnoses": [{"name": "Fever"}], "investigations": "CBC",
             "items": [{"drug": "  Paracetamol ", "dose": "500 mg", "frequency": "1+1+1",
                        "duration": "5 days", "instructions": ""}, {"dose": "no drug"}],
             "advice": "Rest and fluids", "follow_up": "2026-10-08", "notes": ""}
    assert cm.fingerprint(cm.prescription(loose)) == same
    for change in ({"advice": "Rest"}, {"follow_up": "2026-10-09"}, {"diagnoses": ["Flu"]},
                   {"items": [{"drug": "Napa", "dose": "500 mg"}]}, {"notes": "x"}):
        assert cm.fingerprint(cm.prescription(dict(EXAMPLE, **change))) != same
    two = dict(EXAMPLE, items=[{"drug": "A"}, {"drug": "B"}])
    swapped = dict(EXAMPLE, items=[{"drug": "B"}, {"drug": "A"}])
    assert cm.fingerprint(cm.prescription(two)) != cm.fingerprint(cm.prescription(swapped))


def test_previous_visit_shapes():
    named = cm.previous_visit({"previous_visit": {
        "date": "2026-10-01", "doctor_id": "DR0001", "hospital_id": "CMED-1",
        "start_time": "2026-10-01T10:00:00+06:00", "prescription": EXAMPLE}})
    assert named.identified and named.doctor_id == "DR0001"
    dated = cm.previous_visit({"previous_visit": {"date": "2026-10-01",
                                                  "prescription": EXAMPLE}})
    assert not dated.identified


def _problems(previous, **body):
    return {p["field"] for p in clinical.problems_with(
        "patient_information", api2(previous_visit=previous, **body))}


def test_previous_visit_is_named_whole_or_by_date_only():
    whole = {"date": "2026-09-01", "doctor_id": "DR0001", "hospital_id": "CMED-1",
             "start_time": "2026-09-01T10:00:00+06:00", "prescription": EXAMPLE}
    assert _problems(whole) == set()
    assert _problems({"date": "2026-09-01", "prescription": EXAMPLE}) == set()
    half = {k: v for k, v in whole.items() if k != "start_time"}
    assert _problems(half) == {"previous_visit.start_time"}
    assert _problems(dict(whole, patient_id="SOMEONE_ELSE")) == {"previous_visit.patient_id"}
    assert _problems(dict(whole, date="2026-09-30")) == {"previous_visit.date"}
    assert _problems({"prescription": EXAMPLE}) == {"previous_visit.date"}
    assert _problems({"date": "2026-09-01"}) == {"previous_visit.prescription"}
    itself = dict(whole, date=FIELDS["date"], start_time=FIELDS["start_time"])
    assert _problems(itself) == {"previous_visit"}


# ============================================================
# Late clinical records reach their recording (in-memory server)
# ============================================================

async def _closed(server, *, archive=False):
    """A recording granted and closed with no API 2 yet - the outage case."""
    _, reply = await mint()
    sid = open_session(server, segments=2)
    await api_v2._link_grant(sid, reply["jti"], "P0012345", DEVICE)
    server.repo.sessions[sid].update(file_stem="P0012345_STEM", closed_at=NOW())
    if archive:
        await api_v2.archive_complete(api_v2.ArchiveCompleteRequest(
            session_id=sid, archive_relpath="HOSP003/x.wav", sha256="ab" * 32,
            bytes=1_000, segments=[
                api_v2.JoinedSegment(seq_no=s["seq_no"], sha256=bytes(s["sha256"]).hex())
                for s in server.repo.segments.get(sid, [])]))
    return sid


@asyncio
async def test_a_late_api2_is_linked_to_the_closed_recording(server):
    sid = await _closed(server)
    status, reply = await send("patient_information", api2())
    assert status == 202 and reply["previous_visit"] == {"status": "NOT_SENT"}
    assert {"session_id": sid, "file_stem": "P0012345_STEM",
            "hospital_id": "HOSP003"} in server.clinical.linked


@asyncio
async def test_a_late_api2_after_archiving_rewrites_the_json(server):
    sid = await _closed(server, archive=True)
    assert not server.repo.sessions[sid].get("json_stale")
    assert (await send("patient_information", api2()))[0] == 202
    assert server.repo.sessions[sid]["json_stale"] is True


@asyncio
async def test_a_late_prescription_after_archiving_rewrites_the_json(server):
    sid = await _closed(server, archive=True)
    assert (await send("prescription", prescription()))[0] == 202
    assert server.repo.sessions[sid]["json_stale"] is True
    assert any(link["session_id"] == sid for link in server.clinical.linked)


@asyncio
async def test_a_repeat_links_nothing_new(server):
    await _closed(server)
    await send("patient_information", api2())
    before = len(server.clinical.linked)
    assert (await send("patient_information", api2()))[1]["code"] == "ALREADY_RECEIVED"
    assert len(server.clinical.linked) == before


# ============================================================
# Against PostgreSQL
# ============================================================

_n = iter(range(100_000))


class _Request:
    def __init__(self, key, body=None, query=None):
        self.headers = {"x-cmed-key": key}
        self.query_params = query or {}
        self._body = body

    async def body(self):
        return json.dumps(self._body).encode()


def _visit(patient, day, hhmmss, doctor="DR0042", clinic="CMED-DHOLPUR"):
    return {"patient_id": patient, "doctor_id": doctor, "hospital_id": clinic,
            "start_time": f"{day}T{hhmmss}+06:00", "date": day}


def _rx(visit, drug="Paracetamol", issued=None):
    return {**visit, "issued_at": issued or visit["start_time"], "diagnoses": ["Fever"],
            "items": [{"drug": drug, "dose": "500 mg"}], "investigations": [],
            "advice": "rest"}


def _previous(visit, drug="Paracetamol"):
    rx = _rx(visit, drug)
    return {"date": visit["date"], "doctor_id": visit["doctor_id"],
            "hospital_id": visit["hospital_id"], "start_time": visit["start_time"],
            "prescription": {k: rx[k] for k in ("diagnoses", "items", "investigations",
                                                "advice")}}


def _api2_body(visit, previous=None):
    return {**visit, "demographics": {"name": "Probe", "sex": "male", "age_years": 40},
            "previous_visit": previous}


@pytest_asyncio.fixture
async def live(dbs):
    """The real clinical store and repository behind the real endpoints."""
    key = await dbs.repo.create_cmed_key(label="cmed", created_by="tests", expires_at=None,
                                         cmed_hospital_ids=["CMED-DHOLPUR", "CMED-OTHER",
                                                            "CMED-1", "HOSP003"])
    await dbs.repo.upsert_hospital("HOSP003", "Dholpur", "Asia/Dhaka")
    await dbs.repo.set_cmed_hospital_id("HOSP003", "CMED-DHOLPUR")
    api_v2.forget_directory()
    api_v2.ctx.repo, api_v2.ctx.clinical = dbs.repo, dbs.clinical

    async def post(kind, body):
        reply = await clinical.receive(_Request(key, body), kind)
        return reply.status_code, json.loads(reply.body)

    async def visits(patient, **query):
        reply = await clinical.patient_visits(patient, _Request(key, query=query))
        return reply.status_code, json.loads(reply.body)

    async def rows(patient):
        async with dbs.cli.acquire() as conn:
            return [dict(r) for r in await conn.fetch("""
                SELECT e.visit_date, e.source, e.doctor_id, e.cmed_hospital_id, p.version,
                       (SELECT drug FROM prescription_items i
                         WHERE i.prescription_id = p.prescription_id AND line_no = 1) AS drug
                  FROM encounters e LEFT JOIN prescriptions p USING (encounter_id)
                 WHERE e.patient_id = $1
                 ORDER BY e.visit_date, e.start_time, p.version
            """, patient)]

    yield type("Live", (), dict(post=staticmethod(post), visits=staticmethod(visits),
                                rows=staticmethod(rows), key=key, dbs=dbs))
    api_v2.ctx.repo = api_v2.ctx.clinical = None


def _patient():
    return f"PV{next(_n):05d}"


@postgres
async def test_the_probe_that_found_the_duplicate_now_stores_one_visit(live):
    """The exact sequence that once filed the 1 Oct visit twice, under the wrong doctor."""
    p = _patient()
    v1 = _visit(p, "2026-10-01", "10:00:00", doctor="DR0001")
    v2 = _visit(p, "2026-10-08", "11:00:00")
    v3 = _visit(p, "2026-10-15", "12:00:00")
    assert (await live.post("patient_information", _api2_body(v1)))[0] == 202
    assert (await live.post("prescription", _rx(v1)))[0] == 202

    status, reply = await live.post("patient_information", _api2_body(v2, _previous(v1)))
    assert status == 202
    assert reply["previous_visit"]["status"] == "ALREADY_STORED"
    assert reply["previous_visit"]["version"] == 1

    status, reply = await live.post("patient_information",
                                    _api2_body(v3, _previous(v1, "Napa Extra")))
    assert reply["previous_visit"] == {"status": "UPDATED", "version": 2,
                                       "record_id": reply["record_id"]}

    held = await live.rows(p)
    first = [r for r in held if r["visit_date"] == date(2026, 10, 1)]
    assert {(r["source"], r["doctor_id"]) for r in first} == {("live", "DR0001")}
    assert [(r["version"], r["drug"]) for r in first] == [(1, "Paracetamol"),
                                                         (2, "Napa Extra")]
    current = await live.dbs.clinical.prescription_for(p, which="previous", actor="tests")
    assert current["version"] == 2


@postgres
async def test_a_visit_not_held_is_stored_under_its_real_doctor(live):
    p = _patient()
    old = _visit(p, "2026-09-01", "09:30:00", doctor="DR0007", clinic="CMED-OTHER")
    now = _visit(p, "2026-10-01", "10:00:00")
    status, reply = await live.post("patient_information", _api2_body(now, _previous(old)))
    assert reply["previous_visit"]["status"] == "STORED"
    earlier = [r for r in await live.rows(p) if r["visit_date"] == date(2026, 9, 1)]
    assert earlier == [{"visit_date": date(2026, 9, 1), "source": "previous_visit",
                        "doctor_id": "DR0007", "cmed_hospital_id": "CMED-OTHER",
                        "version": 1, "drug": "Paracetamol"}]

    # The identical API 2 again: nothing new, and CMED is told it is held.
    status, reply = await live.post("patient_information", _api2_body(now, _previous(old)))
    assert (status, reply["code"]) == (200, "ALREADY_RECEIVED")
    assert reply["previous_visit"]["status"] == "ALREADY_STORED"

    # Its own API 3 arriving late, after an outage: the same visit, not a second one.
    status, reply = await live.post("prescription", _rx(old))
    assert (status, reply["code"]) == (200, "ALREADY_RECEIVED")
    earlier = [r for r in await live.rows(p) if r["visit_date"] == date(2026, 9, 1)]
    assert len(earlier) == 1


@postgres
async def test_a_late_api2_for_a_backfilled_visit_takes_it_over(live):
    """A visit first seen as history, then its own API 2 arrives: one live row."""
    p = _patient()
    old = _visit(p, "2026-09-01", "09:30:00")
    await live.post("patient_information",
                    _api2_body(_visit(p, "2026-10-01", "10:00:00"), _previous(old)))
    assert (await live.post("patient_information", _api2_body(old)))[0] == 202
    earlier = [r for r in await live.rows(p) if r["visit_date"] == date(2026, 9, 1)]
    assert [(r["source"], r["version"]) for r in earlier] == [("live", 1)]


@postgres
async def test_a_reissued_prescription_is_not_a_new_version(live):
    p = _patient()
    v = _visit(p, "2026-10-01", "10:00:00")
    assert (await live.post("prescription", _rx(v)))[1]["version"] == 1
    status, reply = await live.post("prescription",
                                    _rx(v, issued="2026-10-01T18:00:00+06:00"))
    assert (status, reply["code"], reply["version"]) == (200, "ALREADY_RECEIVED", 1)
    status, reply = await live.post("prescription", _rx(v, "Napa"))
    assert (status, reply["version"]) == (202, 2)
    # An exact replay of version 1 - a queue flushing after an outage - must
    # never roll the visit back: it is the old record, not a new version.
    status, reply = await live.post("prescription", _rx(v))
    assert (status, reply["code"], reply["version"]) == (200, "ALREADY_RECEIVED", 1)
    # The doctor really going back to the first prescription is a new version.
    status, reply = await live.post("prescription",
                                    _rx(v, issued="2026-10-01T19:00:00+06:00"))
    assert (status, reply["version"]) == (202, 3)


@postgres
async def test_the_date_only_shape_never_borrows_todays_doctor(live):
    p = _patient()
    now = _visit(p, "2026-10-01", "10:00:00")
    dated = {"date": "2026-09-01", "prescription": {"items": [{"drug": "Omeprazole"}]}}
    status, reply = await live.post("patient_information", _api2_body(now, dated))
    assert reply["previous_visit"]["status"] == "UNIDENTIFIED"
    assert reply["previous_visit"]["stored"] is True
    earlier = [r for r in await live.rows(p) if r["visit_date"] == date(2026, 9, 1)]
    assert [(r["doctor_id"], r["cmed_hospital_id"]) for r in earlier] == \
        [("UNKNOWN", "UNKNOWN")]

    # The same history from the next visit: held already, nothing added.
    nxt = _visit(p, "2026-10-08", "10:00:00")
    status, reply = await live.post("patient_information", _api2_body(nxt, dated))
    assert reply["previous_visit"] == {"status": "UNIDENTIFIED", "stored": False,
                                       "version": None, "record_id": None}
    assert len([r for r in await live.rows(p) if r["visit_date"] == date(2026, 9, 1)]) == 1


@postgres
async def test_the_visits_list_gives_identifiers_and_fingerprints_only(live):
    p = _patient()
    v1 = _visit(p, "2026-09-01", "09:30:00", doctor="DR0001")
    v2 = _visit(p, "2026-10-01", "10:00:00")
    await live.post("patient_information", _api2_body(v1))
    await live.post("prescription", {**_rx(v1), **{k: EXAMPLE[k] for k in EXAMPLE
                                                   if k != "issued_at"}})
    await live.post("patient_information", _api2_body(v2))

    status, reply = await live.visits(p)
    assert (status, reply["code"], reply["next_cursor"]) == (200, "OK", None)
    assert [v["date"] for v in reply["visits"]] == ["2026-10-01", "2026-09-01"]
    first = reply["visits"][1]
    assert first["prescription_fingerprint"] == EXAMPLE_FINGERPRINT
    assert (first["doctor_id"], first["hospital_id"], first["start_time"],
            first["source"], first["prescription_version"]) == (
        "DR0001", "CMED-DHOLPUR", "2026-09-01T09:30:00+06:00", "live", 1)
    assert reply["visits"][0]["prescription_fingerprint"] is None
    assert set(first) == {"patient_id", "doctor_id", "hospital_id", "start_time", "date",
                          "source", "prescription_version", "prescription_fingerprint",
                          "received_at"}

    status, reply = await live.visits(p, limit="1")
    assert reply["next_cursor"] and len(reply["visits"]) == 1
    status, reply = await live.visits(p, limit="1", cursor=reply["next_cursor"])
    assert [v["date"] for v in reply["visits"]] == ["2026-09-01"]

    # Three visits on one day, read one at a time: none skipped, none twice.
    same_day = [_visit(p, "2026-08-01", f"{h:02d}:00:00") for h in (9, 11, 15)]
    for v in same_day:
        await live.post("patient_information", _api2_body(v))
    seen, cursor = [], None
    while True:
        status, reply = await live.visits(p, limit="1", **({"cursor": cursor} if cursor else {}))
        seen += [v["start_time"] for v in reply["visits"]]
        cursor = reply["next_cursor"]
        if not cursor:
            break
    assert len(seen) == len(set(seen)) == 5
    assert (await live.visits(p, cursor="not-a-cursor"))[0] == 400

    # Every read that returned a list is logged: three above, six pages here
    # (five visits one at a time, then the empty page); the refused one is not.
    async with live.dbs.cli.acquire() as conn:
        reads = await conn.fetchval(
            "SELECT count(*) FROM clinical_access_log WHERE patient_id = $1 "
            "AND action = 'list_visits'", p)
    assert reads == 9


@postgres
async def test_the_visits_list_refuses_like_the_other_endpoints(live):
    reply = await clinical.patient_visits("P1", _Request("wrong"))
    assert (reply.status_code, json.loads(reply.body)["code"]) == (401, "INVALID_KEY")
    for query, field in (({"limit": "0"}, "limit"), ({"limit": "500"}, "limit"),
                         ({"limit": "x"}, "limit"), ({"before": "1/1/2026"}, "before")):
        status, reply = await live.visits("P1", **query)
        assert (status, reply["code"], reply["fields"]) == (400, "MISSING_FIELD", [field])
    status, reply = await live.visits("P 1")
    assert (status, reply["code"]) == (400, "INVALID_IDENTIFIER")
    status, reply = await live.visits(_patient())
    assert (status, reply["visits"]) == (200, [])


@postgres
async def test_a_load_that_fails_reports_pending_not_stored(live):
    """The reply never claims more than the tables hold."""
    p = _patient()
    first = _visit(p, "2026-10-01", "10:00:00")
    await live.post("patient_information", {**_api2_body(first),
                                            "demographics": {"sex": "female"}})
    # Now said to be male: the female table's key refuses the load (SRS-CRI-04).
    second = _visit(p, "2026-10-08", "10:00:00")
    body = {**_api2_body(second, _previous(_visit(p, "2026-09-01", "09:00:00"))),
            "demographics": {"sex": "male"}}
    status, reply = await live.post("patient_information", body)
    assert status == 202
    assert reply["previous_visit"] == {"status": "PENDING"}


@postgres
async def test_the_clean_up_merges_old_duplicates(live):
    """What the old path left behind, put right by scripts/clinical/merge_previous_visits.py."""
    sys.path.insert(0, str(BACKEND / "scripts" / "clinical"))
    import merge_previous_visits as merge

    p = _patient()
    v1 = _visit(p, "2026-10-01", "10:00:00", doctor="DR0001")
    v2 = _visit(p, "2026-10-08", "11:00:00")
    await live.post("patient_information", _api2_body(v1))
    await live.post("prescription", _rx(v1))
    await live.post("patient_information", _api2_body(v2))
    # The old loader's copies, carried by v2's API 2: midnight, under v2's
    # doctor and clinic. One repeats the live 1 Oct visit; one was never held.
    async with live.dbs.cli.acquire() as conn:
        intake = await conn.fetchval(
            "SELECT id FROM intake_records WHERE patient_id = $1 AND visit_date = $2",
            p, date(2026, 10, 8))
        copy = await conn.fetchval("""
            INSERT INTO encounters (patient_id, doctor_id, cmed_hospital_id, start_time,
                                    visit_date, source, patient_information_id)
            VALUES ($1, 'DR0042', 'CMED-DHOLPUR', '2026-10-01T00:00:00+06:00',
                    '2026-10-01', 'previous_visit', $2) RETURNING encounter_id
        """, p, intake)
        rx = cm.prescription(_rx(v1))
        await live.dbs.clinical._store_prescription(conn, copy, rx, version=1,
                                                    intake_id=intake)
        orphan = await conn.fetchval("""
            INSERT INTO encounters (patient_id, doctor_id, cmed_hospital_id, start_time,
                                    visit_date, source, patient_information_id)
            VALUES ($1, 'DR0042', 'CMED-DHOLPUR', '2026-08-01T00:00:00+06:00',
                    '2026-08-01', 'previous_visit', $2) RETURNING encounter_id
        """, p, intake)
        await live.dbs.clinical._store_prescription(conn, orphan, rx, version=1,
                                                    intake_id=intake)

    def mine(report):
        return report["duplicates"], report["unattributed"]

    before = await live.rows(p)
    report = await merge.run(None, apply=False, pool=live.dbs.cli)
    assert mine(report) >= (1, 1)
    assert await live.rows(p) == before                    # a dry run changes nothing

    await merge.run(None, apply=True, pool=live.dbs.cli)
    held = await live.rows(p)
    assert [(r["visit_date"], r["source"], r["doctor_id"]) for r in held] == [
        (date(2026, 8, 1), "previous_visit", "UNKNOWN"),
        (date(2026, 10, 1), "live", "DR0001"),
        (date(2026, 10, 8), "live", "DR0042")]

    again = await merge.run(None, apply=True, pool=live.dbs.cli)   # idempotent
    assert mine(again) == (0, 0)


@postgres
async def test_the_clean_up_removes_test_patients_only_when_asked(live):
    sys.path.insert(0, str(BACKEND / "scripts" / "clinical"))
    import merge_previous_visits as merge

    test_patient, real = f"TEST_PREV_{next(_n)}", _patient()
    for p in (test_patient, real):
        await live.post("patient_information",
                        _api2_body(_visit(p, "2026-10-01", "10:00:00")))
    report = await merge.run(None, apply=True, pool=live.dbs.cli)
    assert report["test_patients"] == 0 and await live.rows(test_patient)
    report = await merge.run(None, apply=False, purge_test_patients=True, pool=live.dbs.cli)
    assert report["test_patients"] >= 1 and await live.rows(test_patient)
    report = await merge.run(None, apply=True, purge_test_patients=True, pool=live.dbs.cli)
    assert await live.rows(test_patient) == [] and await live.rows(real)
    async with live.dbs.cli.acquire() as conn:
        assert await conn.fetchval(
            "SELECT count(*) FROM intake_records WHERE patient_id = $1", test_patient) == 0
