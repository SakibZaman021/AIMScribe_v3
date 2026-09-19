"""
SRS 3.2 on the existing server: grants, confirmation, Channel B, refusals and
receipts on custody.

The endpoint functions run for real against an in-memory repository and a fake
bucket, so every rule here is exercised in the code that ships - only Postgres
and R2 are stood in. Grants are checked with the recorder's own verifier, so a
passing test also proves the two sides agree on the format.
"""
from __future__ import annotations

import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND / "src"))
sys.path.insert(0, str(BACKEND.parent / "recorder"))

import api_v2                                   # noqa: E402
import clinical                                 # noqa: E402
import integrity                                # noqa: E402
from confirmation import Visit                  # noqa: E402
from grants import GrantIssuer                  # noqa: E402
from integrity import ReceiptSigner             # noqa: E402

pytestmark = pytest.mark.asyncio

NOW = lambda: datetime.now(timezone.utc)        # noqa: E731
DEVICE = {"device_id": "dev-1", "hospital_id": "HOSP003", "revoked_at": None}
OTHER_DEVICE = {"device_id": "dev-2", "hospital_id": "HOSP003", "revoked_at": None}
CMED_ID = "CMED-DHK-BANANI-01"
KEY = "cmed_test_key"

FIELDS = {
    "patient_id": "P0012345",
    "doctor_id": "DR0042",
    "hospital_id": CMED_ID,
    "start_time": "2026-09-13T10:14:32+06:00",
    "date": "2026-09-13",
}
ULIDS = iter(f"01JB8XQ4M7YZ2K9V3N5P6R8T{a}{b}"
             for a in "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
             for b in "0123456789ABCDEFGHJKMNPQRSTVWXYZ")


# ============================================================
# Stand-ins for Postgres and R2
# ============================================================

class FakeRepo:
    def __init__(self):
        self.hospitals = {"HOSP001": None, "HOSP003": CMED_ID}   # code -> CMED id
        self.doctors, self.auths, self.notices = set(), {}, []
        self.sessions, self.refusals = {}, {}
        self.receipts, self.segments = [], {}
        self.alerts, self.audits, self.keys = [], [], {}

    # clinics
    async def hospital_for_cmed_id(self, cmed_id):
        return next((h for h, c in self.hospitals.items() if c == cmed_id), None)

    async def hospital_exists(self, hospital_id):
        return hospital_id in self.hospitals

    async def set_cmed_hospital_id(self, hospital_id, cmed_id):
        self.hospitals[hospital_id] = cmed_id

    async def upsert_doctor(self, **kw):
        self.doctors.add((kw["doctor_id"], kw["hospital_id"]))

    # grants
    async def create_authorisation(self, *, jti, device_id, hospital_id, visit, expires_at):
        self.auths[jti] = dict(
            jti=jti, device_id=device_id, hospital_id=hospital_id,
            cmed_hospital_id=visit.cmed_hospital_id, patient_id=visit.patient_id,
            doctor_id=visit.doctor_id, start_time=visit.start_time,
            visit_date=visit.day, notice_id=None, session_id=None, issued_at=NOW())

    async def get_authorisation(self, jti):
        return dict(self.auths[jti]) if jti in self.auths else None

    async def link_authorisation(self, jti, session_id):
        if self.auths[jti]["session_id"] in (None, session_id):
            self.auths[jti]["session_id"] = session_id

    @staticmethod
    def _match(row, visit, hospital_id):
        return (row["patient_id"], row["doctor_id"], row["cmed_hospital_id"],
                row["start_time"], row["visit_date"], row["hospital_id"]) == (
            visit.patient_id, visit.doctor_id, visit.cmed_hospital_id,
            visit.start_time, visit.day, hospital_id)

    async def find_unclaimed_notice(self, visit, *, hospital_id, since):
        found = [r for r in self.notices
                 if r["claimed_by_jti"] is None
                 and self._match(r, visit, hospital_id) and r["received_at"] >= since]
        return max(found, key=lambda r: (r["received_at"], r["id"]))["id"] if found else None

    async def find_waiting_authorisation(self, visit, *, hospital_id, since):
        found = [a for a in self.auths.values() if a["notice_id"] is None
                 and self._match(a, visit, hospital_id) and a["issued_at"] >= since]
        if not found:
            return None
        best = max(found, key=lambda a: a["issued_at"])
        return {"jti": best["jti"], "session_id": best["session_id"]}

    async def claim_notice(self, notice_id, jti):
        grant = self.auths.get(jti)
        record = next(r for r in self.notices if r["id"] == notice_id)
        if grant is None or grant["notice_id"] is not None or record["claimed_by_jti"]:
            return False
        record["claimed_by_jti"], grant["notice_id"] = jti, notice_id
        return True

    # sessions
    async def get_session(self, session_id):
        s = self.sessions.get(session_id)
        return dict(s) if s else None

    async def set_session_confirmation(self, session_id, state, *, grant_jti=None):
        self.sessions[session_id]["confirmation"] = state
        if grant_jti:
            self.sessions[session_id]["grant_jti"] = grant_jti

    async def session_confirmation(self, session_id):
        return await self.get_session(session_id)

    async def sessions_in_confirmation(self, states, *, opened_before):
        return [dict(s) for s in self.sessions.values()
                if s["confirmation"] in states and s["opened_at"] < opened_before]

    async def segments_for(self, session_id):
        return list(self.segments.get(session_id, []))

    async def mark_object_deleted(self, session_id, seq_no):
        for seg in self.segments.get(session_id, []):
            if seg["seq_no"] == seq_no:
                seg["object_deleted_at"] = NOW()

    async def store_receipt(self, **kw):
        self.receipts.append(kw)

    # refusals
    async def record_refusal(self, *, session_id, **kw):
        if session_id in self.refusals:
            return False
        self.refusals[session_id] = dict(kw, session_id=session_id)
        return True

    async def refusal(self, session_id):
        return self.refusals.get(session_id)

    async def complete_refusal(self, session_id):
        self.refusals[session_id]["completed"] = True

    async def refused_visit(self, digest):
        return any(r.get("visit_sha256") == digest for r in self.refusals.values())

    async def erase_session(self, session_id, *, state):
        s = self.sessions[session_id]
        s.update(patient_id="REDACTED", confirmation=state, status=state)
        self.segments.pop(session_id, None)
        self.receipts = [r for r in self.receipts if r["session_id"] != session_id]
        for grant in self.auths.values():
            if grant["session_id"] == session_id:
                grant["patient_id"] = "REDACTED"

    async def delete_notices_for(self, visit):
        before = len(self.notices)
        self.notices = [n for n in self.notices
                        if (n["patient_id"], n["start_time"]) != (visit.patient_id,
                                                                  visit.start_time)]
        return before - len(self.notices)

    # Channel B
    async def cmed_key_valid(self, digest):
        key = self.keys.get(digest)
        return key["label"] if key and not key["revoked"] else None

    async def record_notice(self, visit, *, hospital_id, clinical_record_id):
        nid = len(self.notices) + 1
        self.notices.append(dict(
            id=nid, cmed_hospital_id=visit.cmed_hospital_id, hospital_id=hospital_id,
            patient_id=visit.patient_id, doctor_id=visit.doctor_id,
            start_time=visit.start_time, visit_date=visit.day,
            clinical_record_id=clinical_record_id, claimed_by_jti=None, received_at=NOW()))
        return nid

    async def set_file_names(self, session_id, *, file_stem, local_start, local_end):
        self.sessions[session_id]["file_stem"] = file_stem
        return file_stem

    # audit and alerts
    async def raise_alert(self, **kw):
        self.alerts.append(kw)

    async def audit(self, **kw):
        self.audits.append(kw)


class FakeClinical:
    """aims_clinical, as the Channel B code sees it."""

    def __init__(self):
        self.records, self.quarantined, self.loaded, self.linked = [], [], [], []

    async def receive(self, *, kind, visit, hospital_id, body, body_sha256):
        for r in self.records:
            if (r["kind"], r["body_sha256"]) == (kind, body_sha256):
                return r["id"], True, r["version"]
        version = 1
        if kind == "prescription":
            version += sum(1 for r in self.records if r["kind"] == "prescription"
                           and (r["patient_id"], r["start_time"]) == (visit.patient_id,
                                                                      visit.start_time))
        rid = len(self.records) + 5000
        self.records.append(dict(
            id=rid, kind=kind, cmed_hospital_id=visit.cmed_hospital_id,
            hospital_id=hospital_id, patient_id=visit.patient_id, doctor_id=visit.doctor_id,
            start_time=visit.start_time, visit_date=visit.day, version=version, body=body,
            body_sha256=body_sha256))
        return rid, False, version

    async def quarantine(self, *, kind, raw, problems):
        self.quarantined.append({"kind": kind, "raw": raw, "problems": problems})
        return len(self.quarantined)

    async def load(self, record_id):
        self.loaded.append(record_id)
        return True

    async def load_pending(self, limit=50):
        return 0

    async def erase_visit(self, visit):
        before = len(self.records)
        self.records = [r for r in self.records
                        if (r["patient_id"], r["start_time"]) != (visit.patient_id,
                                                                  visit.start_time)]
        return before - len(self.records)

    async def link_session(self, visit, **kw):
        self.linked.append(kw)
        return True


class FakeBucket:
    def __init__(self):
        self.bucket, self.removed = "aimscribe-audio", []
        self.client = SimpleNamespace(remove_object=lambda b, key: self.removed.append(key))


@pytest.fixture
def server():
    repo = FakeRepo()
    repo.keys[clinical.key_digest(KEY)] = {"label": "cmed-prod", "revoked": False}
    grant_key = Ed25519PrivateKey.generate()
    receipt_key = Ed25519PrivateKey.generate()
    api_v2.ctx.repo = repo
    api_v2.ctx.minio = FakeBucket()
    api_v2.ctx.grants = GrantIssuer(grant_key)
    api_v2.ctx.signer = ReceiptSigner(receipt_key)
    api_v2.ctx.clinical = FakeClinical()
    yield SimpleNamespace(repo=repo, bucket=api_v2.ctx.minio, clinical=api_v2.ctx.clinical,
                          grant_public=grant_key.public_key(),
                          receipt_public=receipt_key.public_key())
    api_v2.ctx.repo = api_v2.ctx.minio = api_v2.ctx.grants = api_v2.ctx.signer = None
    api_v2.ctx.clinical = None


# ============================================================
# Helpers
# ============================================================

def unwrap(result):
    """An endpoint returns a dict on success or a JSONResponse with a code."""
    if isinstance(result, dict):
        return 200, result
    return result.status_code, json.loads(result.body)


class FakeRequest:
    def __init__(self, body, *, key=KEY, raw=None, length=None):
        self._raw = raw if raw is not None else json.dumps(body).encode("utf-8")
        self.headers = {}
        if key is not None:
            self.headers["x-cmed-key"] = key
        if length is not None:
            self.headers["content-length"] = str(length)

    async def body(self):
        return self._raw


def api2(**changes):
    body = {**FIELDS, "demographics": {"name": "Test", "sex": "female", "age_years": 34},
            "paramedic": {"blood_pressure": "120/80"}, "previous_visit": None}
    body.update(changes)
    return body


def prescription(**changes):
    body = {**FIELDS, "issued_at": "2026-09-13T10:26:11+06:00", "diagnoses": ["x"],
            "items": [{"drug": "Paracetamol", "dose": "500 mg"}], "investigations": [],
            "advice": "rest", "follow_up": "2026-10-13"}
    body.update(changes)
    return body


async def send(kind, body, **kw):
    return unwrap(await clinical.receive(FakeRequest(body, **kw), kind))


async def mint(fields=None, device=DEVICE):
    return unwrap(await api_v2.mint_grant(dict(fields or FIELDS), device=device))


def open_session(server, jti=None, *, opened_ago=timedelta(seconds=5), device=DEVICE,
                 segments=0):
    """What /session/open leaves behind, without building a signed chain."""
    sid = next(ULIDS)
    server.repo.sessions[sid] = dict(
        session_id=sid, device_id=device["device_id"], doctor_id="DR0042",
        hospital_id="HOSP003", patient_id="P0012345", confirmation="legacy",
        grant_jti=None, opened_at=NOW() - opened_ago, session_date=date(2026, 9, 13),
        status="active")
    server.repo.segments[sid] = [
        {"seq_no": n, "object_key": f"audio/{sid}/seg_{n:05d}.wav",
         "object_deleted_at": None} for n in range(1, segments + 1)]
    return sid


def visit_of(fields=FIELDS) -> Visit:
    return Visit(patient_id=fields["patient_id"], doctor_id=fields["doctor_id"],
                 cmed_hospital_id=fields["hospital_id"], start_time=fields["start_time"],
                 visit_date=fields["date"])


# ============================================================
# Grants
# ============================================================

async def test_grant_verifies_on_the_recorder_and_names_the_pcs_clinic(server):
    from core import crypto
    status, reply = await mint()
    assert (status, reply["code"], reply["confirmation"]) == (200, "GRANTED", "pending")
    grant = crypto.verify_grant(reply["grant"], server.grant_public,
                                issuer="aimslab", audience="aimscribe-recorder")
    assert (grant.patient_ref, grant.doctor_id, grant.hospital_id) == (
        "P0012345", "DR0042", "HOSP003")
    assert grant.jti == reply["jti"]
    assert ("DR0042", "HOSP003") in server.repo.doctors          # D4: directory, not gate


async def test_api2_first_then_grant_is_confirmed(server):
    """AT-57."""
    status, stored = await send("patient_information", api2())
    assert (status, stored["code"]) == (202, "ACCEPTED")
    status, reply = await mint()
    assert reply["confirmation"] == "confirmed"
    assert server.repo.notices[0]["claimed_by_jti"] == reply["jti"]
    # The recordings side keeps the five fields only - never the body.
    assert "body" not in server.repo.notices[0]


async def test_api2_older_than_five_minutes_confirms_nothing(server):
    """SRS-CNF-03."""
    await send("patient_information", api2())
    server.repo.notices[0]["received_at"] -= timedelta(minutes=6)
    assert (await mint())[1]["confirmation"] == "pending"


async def test_one_api2_confirms_one_grant(server):
    """SRS-CNF-04."""
    await send("patient_information", api2())
    assert (await mint())[1]["confirmation"] == "confirmed"
    assert (await mint())[1]["confirmation"] == "pending"


async def test_the_most_recent_api2_is_used(server):
    """SRS-CNF-05."""
    await send("patient_information", api2(paramedic={"notes": "first"}))
    await send("patient_information", api2(paramedic={"notes": "second"}))
    _, reply = await mint()
    notice = next(n for n in server.repo.notices if n["claimed_by_jti"] == reply["jti"])
    body = next(r["body"] for r in server.clinical.records
                if r["id"] == notice["clinical_record_id"])
    assert body["paramedic"]["notes"] == "second"


async def test_start_time_must_match_character_for_character(server):
    """AT-62."""
    await send("patient_information", api2(start_time="2026-09-13T10:14:33+06:00"))
    assert (await mint())[1]["confirmation"] == "pending"


async def test_clinic_mismatch_is_refused_and_alerted(server):
    """AT-04 / decision D1: CMED's clinic maps to another PC's clinic."""
    server.repo.hospitals["HOSP001"] = "CMED-MIRPUR-01"
    status, reply = await mint({**FIELDS, "hospital_id": "CMED-MIRPUR-01"})
    assert (status, reply["code"]) == (401, "CLINIC_MISMATCH")
    assert server.repo.auths == {}
    assert server.repo.alerts[-1]["alert_type"] == "clinic_mismatch"


async def test_api2_for_another_clinic_is_never_claimed(server):
    """AT-63."""
    server.repo.hospitals["HOSP001"] = "CMED-MIRPUR-01"
    other = {**FIELDS, "hospital_id": "CMED-MIRPUR-01"}
    await send("patient_information", api2(**other))
    assert (await mint(other))[1]["code"] == "CLINIC_MISMATCH"
    assert server.repo.notices[0]["claimed_by_jti"] is None


async def test_unmapped_clinic_works_only_when_it_is_our_own_code(server):
    server.repo.hospitals["HOSP003"] = None
    assert (await mint({**FIELDS, "hospital_id": "HOSP003"}))[0] == 200
    assert (await mint({**FIELDS, "hospital_id": "CMED-UNKNOWN"}))[1]["code"] == \
        "CLINIC_MISMATCH"


@pytest.mark.parametrize("changes,code,field", [
    ({"doctor_id": None}, "MISSING_FIELD", "doctor_id"),
    ({"patient_id": "../x"}, "INVALID_IDENTIFIER", "patient_id"),
    ({"start_time": "2026-09-13T10:14:32"}, "MISSING_FIELD", "start_time"),
    ({"date": "13-09-2026"}, "MISSING_FIELD", "date"),
])
async def test_bad_fields_are_refused(server, changes, code, field):
    fields = {k: v for k, v in {**FIELDS, **changes}.items() if v is not None}
    status, reply = await mint(fields)
    assert (status, reply["code"], reply["field"]) == (400, code, field)


async def test_no_signing_key_means_not_ready(server):
    api_v2.ctx.grants = None
    status, reply = await mint()
    assert (status, reply["code"]) == (503, "AGENT_NOT_READY")


# ============================================================
# Confirmation after the session opens
# ============================================================

async def test_late_api2_confirms_an_open_session(server):
    """AT-59 / AT-60: the recording started first; API 2 arrives afterwards."""
    _, reply = await mint()
    sid = open_session(server)
    assert await api_v2._link_grant(sid, reply["jti"], "P0012345", DEVICE) == "pending"

    await send("patient_information", api2())
    assert server.repo.sessions[sid]["confirmation"] == "confirmed"
    assert any(a["event_type"] == "session.confirmed" for a in server.repo.audits)


async def test_api2_between_grant_and_open(server):
    _, reply = await mint()
    await send("patient_information", api2())
    sid = open_session(server)
    assert await api_v2._link_grant(sid, reply["jti"], "P0012345", DEVICE) == "confirmed"


async def test_a_grant_cannot_be_used_by_another_device(server):
    _, reply = await mint()
    sid = open_session(server, device=OTHER_DEVICE)
    assert await api_v2._link_grant(sid, reply["jti"], "P0012345", OTHER_DEVICE) == \
        "unconfirmed"
    assert server.repo.alerts[-1]["alert_type"] == "grant_link_failed"


async def test_a_retried_open_never_steps_back(server):
    await send("patient_information", api2())
    _, reply = await mint()
    sid = open_session(server)
    await api_v2._link_grant(sid, reply["jti"], "P0012345", DEVICE)
    assert await api_v2._link_grant(sid, reply["jti"], "P0012345", DEVICE) == "confirmed"


async def test_protocol_2_recorders_stay_legacy(server):
    sid = open_session(server)
    assert await api_v2._link_grant(sid, None, "P0012345", DEVICE) == "legacy"


async def test_pending_becomes_unconfirmed_after_two_minutes(server):
    """AT-58."""
    _, reply = await mint()
    recent = open_session(server, opened_ago=timedelta(seconds=30))
    late = open_session(server, opened_ago=timedelta(minutes=3))
    for sid in (recent, late):
        server.repo.sessions[sid].update(confirmation="pending", grant_jti=reply["jti"])

    assert await api_v2._current_confirmation(recent) == "pending"
    assert await api_v2._current_confirmation(late) == "unconfirmed"
    assert server.repo.sessions[late]["confirmation"] == "unconfirmed"
    assert server.repo.alerts[-1]["alert_type"] == "unconfirmed_recording"


async def test_sweep_settles_deadlines(server):
    """AT-75: 24 hours unconfirmed is erased; two minutes pending is flagged."""
    old = open_session(server, opened_ago=timedelta(hours=25), segments=3)
    slow = open_session(server, opened_ago=timedelta(minutes=3))
    kept = open_session(server, opened_ago=timedelta(hours=30), segments=2)
    server.repo.sessions[old]["confirmation"] = "unconfirmed"
    server.repo.sessions[slow]["confirmation"] = "pending"
    server.repo.sessions[kept]["confirmation"] = "confirmed"

    result = await api_v2.maintenance_sweep()
    assert (result["erased"], result["marked_unconfirmed"]) == (1, 1)
    assert server.repo.sessions[old]["confirmation"] == "expired"
    assert server.repo.sessions[old]["patient_id"] == "REDACTED"
    assert len(server.bucket.removed) == 3
    assert server.repo.sessions[slow]["confirmation"] == "unconfirmed"
    assert server.repo.sessions[kept]["confirmation"] == "confirmed"
    assert any(a["event_type"] == "session.expired_unconfirmed" for a in server.repo.audits)


# ============================================================
# Refusal (SRS §7.8a)
# ============================================================

async def test_refusal_erases_the_consultation(server):
    """AT-03, server side."""
    await send("patient_information", api2())
    _, reply = await mint()
    sid = open_session(server, segments=4)
    await api_v2._link_grant(sid, reply["jti"], "P0012345", DEVICE)
    await send("prescription", prescription())

    result = await api_v2.refuse_session(api_v2.RefuseRequest(session_id=sid), device=DEVICE)
    assert (result["status"], result["objects_deleted"]) == ("refused", 4)
    assert server.repo.sessions[sid]["patient_id"] == "REDACTED"
    assert server.clinical.records == []                      # API 2 and prescription gone
    assert server.repo.notices == []
    audit = [a for a in server.repo.audits if a["event_type"] == "session.refused"][0]
    assert "P0012345" not in json.dumps(audit, default=str)   # SRS-CNS-06

    again = await api_v2.refuse_session(api_v2.RefuseRequest(session_id=sid), device=DEVICE)
    assert again["status"] == "refused"
    assert len([a for a in server.repo.audits if a["event_type"] == "session.refused"]) == 1


async def test_clinical_data_after_a_refusal_is_dropped(server):
    """AT-78: answered 202 as usual, and nothing kept."""
    _, reply = await mint()
    sid = open_session(server)
    await api_v2._link_grant(sid, reply["jti"], "P0012345", DEVICE)
    await api_v2.refuse_session(api_v2.RefuseRequest(session_id=sid), device=DEVICE)

    for kind, body in (("patient_information", api2()), ("prescription", prescription())):
        status, stored = await send(kind, body)
        assert (status, stored["code"], stored["record_id"]) == (202, "ACCEPTED", None)
    assert server.clinical.records == []


async def test_offline_refusal_of_an_unknown_session_is_remembered(server):
    sid = next(ULIDS)
    result = await api_v2.refuse_session(api_v2.RefuseRequest(session_id=sid), device=DEVICE)
    assert result["status"] == "refused"
    assert await server.repo.refusal(sid) is not None      # a later open gets 409


async def test_refusal_of_another_devices_session_is_forbidden(server):
    sid = open_session(server, device=OTHER_DEVICE)
    with pytest.raises(api_v2.HTTPException) as err:
        await api_v2.refuse_session(api_v2.RefuseRequest(session_id=sid), device=DEVICE)
    assert err.value.status_code == 403


# ============================================================
# Channel B
# ============================================================

@pytest.mark.parametrize("key", [None, "", "wrong"])
async def test_missing_or_wrong_key_stores_nothing(server, key):
    """AT-68."""
    status, reply = await send("patient_information", api2(), key=key)
    assert (status, reply["code"]) == (401, "INVALID_KEY")
    assert server.clinical.records == []


async def test_revoked_key_is_refused(server):
    server.repo.keys[clinical.key_digest(KEY)]["revoked"] = True
    assert (await send("patient_information", api2()))[1]["code"] == "INVALID_KEY"


async def test_malformed_and_oversized_bodies(server):
    assert (await send("patient_information", None, raw=b"{nope"))[1]["code"] == \
        "MALFORMED_JSON"
    assert (await send("patient_information", None, raw=b"[1]"))[1]["code"] == \
        "MALFORMED_JSON"
    status, reply = await send("patient_information", api2(), length=2_000_000)
    assert (status, reply["code"]) == (413, "TOO_LARGE")
    status, reply = await send("patient_information", None,
                               raw=b'{"x":"' + b"a" * 1_000_001 + b'"}')
    assert (status, reply["code"]) == (413, "TOO_LARGE")


async def test_five_field_problems_are_400(server):
    body = api2()
    del body["start_time"]
    status, reply = await send("patient_information", body)
    assert (status, reply["code"], reply["fields"]) == (400, "MISSING_FIELD", ["start_time"])
    status, reply = await send("prescription", prescription(doctor_id="DR 42"))
    assert (status, reply["code"]) == (400, "INVALID_IDENTIFIER")


async def test_schema_problems_are_quarantined(server):
    """AT-49: stored unchanged, reported, nothing partly loaded."""
    body = api2(demographics="not an object")
    del body["previous_visit"]
    status, reply = await send("patient_information", body)
    assert (status, reply["code"]) == (422, "SCHEMA_INVALID")
    assert {p["field"] for p in reply["fields"]} == {"demographics", "previous_visit"}
    assert json.loads(server.clinical.quarantined[0]["raw"]) == body
    assert server.clinical.records == []
    assert server.repo.alerts[-1]["alert_type"] == "clinical_schema_invalid"

    status, reply = await send("prescription", prescription(items="Paracetamol 500mg"))
    assert status == 422


async def test_the_same_request_twice_is_one_record(server):
    """AT-50."""
    first = await send("patient_information", api2())
    second = await send("patient_information", api2())
    assert (second[0], second[1]["code"]) == (200, "ALREADY_RECEIVED")
    assert second[1]["record_id"] == first[1]["record_id"]
    assert len(server.clinical.records) == 1


async def test_a_changed_prescription_is_a_new_version(server):
    """AT-69."""
    assert (await send("prescription", prescription()))[1]["version"] == 1
    assert (await send("prescription", prescription(advice="rest, fluids")))[1]["version"] == 2
    assert (await send("prescription", prescription()))[1]["code"] == "ALREADY_RECEIVED"


async def test_prescription_before_patient_information(server):
    """AT-70."""
    assert (await send("prescription", prescription()))[0] == 202
    assert (await send("patient_information", api2()))[0] == 202
    assert (await mint())[1]["confirmation"] == "confirmed"


async def test_unknown_fields_are_stored_not_rejected(server):
    """SRS-CHB-11."""
    status, _ = await send("patient_information", api2(blood_group="O+", ward="OPD-2"))
    assert status == 202
    assert server.clinical.records[0]["body"]["blood_group"] == "O+"


async def test_unmapped_clinic_is_stored_but_confirms_nothing(server):
    other = {**FIELDS, "hospital_id": "CMED-NEW-SITE"}
    status, _ = await send("patient_information", api2(**other))
    assert status == 202
    assert server.clinical.records[0]["hospital_id"] is None
    assert server.repo.alerts[-1]["alert_type"] == "unmapped_clinic"


# ============================================================
# Receipts on custody, and the webhook
# ============================================================

async def test_a_receipt_is_issued_the_moment_a_piece_is_verified(server):
    """SRS-REC-15: signed, and verifiable with the receipt public key."""
    sha = integrity.sha256_bytes(b"audio")
    assert await api_v2._issue_custody_receipt("01JB8XQ4M7YZ2K9V3N5P6R8T0W", 3, sha)
    stored = server.repo.receipts[-1]
    assert (stored["scope"], stored["seq_no"]) == ("segment", 3)
    server.receipt_public.verify(
        stored["signature"],
        integrity.digest(integrity.RECEIPT_DOMAIN, integrity.canonical_json(stored["payload"])))

    api_v2.ctx.signer = None
    assert not await api_v2._issue_custody_receipt("01JB8XQ4M7YZ2K9V3N5P6R8T0W", 4, sha)


async def test_the_cmed_webhook_is_off_unless_switched_on(monkeypatch):
    """Decision D2."""
    from webhooks.cmed_webhook import cmed_webhook_enabled
    monkeypatch.delenv("AIMS_CMED_WEBHOOK_ENABLED", raising=False)
    assert cmed_webhook_enabled() is False
    monkeypatch.setenv("AIMS_CMED_WEBHOOK_ENABLED", "true")
    assert cmed_webhook_enabled() is True
