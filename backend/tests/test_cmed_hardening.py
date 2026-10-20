"""
Regression tests for the CMED-interface review: every finding has one.

    lockout by credential, not address        shared counters (Redis)
    malformed FHIR -> 400, never 500           strict JSON (NaN, surrogates, NUL)
    identifiers: several, official, systems    one moment, one start_time
    FHIR conformance (subject, MessageDefinition, scopes, 200)
    token lifecycle (aud, kid revocation, renewal, expiry alert, clock skew)
    interim key Deprecation/Sunset             request ids and problem+json
    /health, /health/live, /health/ready       published OpenAPI contract
    the TypeScript reference client            HL7 validator (when installed)
"""
from __future__ import annotations

import copy
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import jwt
import pytest
import pytest_asyncio
from fastapi import FastAPI, Header, HTTPException

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "backend" / "src"
sys.path.insert(0, str(SRC))
sys.path.append(str(ROOT / "tools"))


def _app_module():
    """main_fastapi, with the server's own `config` (a tool may have loaded the recorder's)."""
    pytest.importorskip("pydantic_settings")
    loaded = sys.modules.get("config")
    if loaded is not None and Path(getattr(loaded, "__file__", "")).parent != SRC:
        del sys.modules["config"]
    if str(SRC) in sys.path:
        sys.path.remove(str(SRC))
    sys.path.insert(0, str(SRC))
    import main_fastapi
    return main_fastapi

import api_v2                                   # noqa: E402
import clinical                                 # noqa: E402
import cmed_auth                                # noqa: E402
import cmed_fhir_client as client_ref           # noqa: E402
import confirmation as conf                     # noqa: E402
import fhir                                     # noqa: E402
import security                                 # noqa: E402
from test_cmed_auth_fhir import (               # noqa: E402,F401
    FULL_API2, FULL_API3, RX, VITALS, Req, _ec_jwk, _token, assertion, fhir_message,
    get_token, hospital, token_form,
)
from test_v3_server import CMED_ID, FIELDS, KEY, api2, prescription, server, unwrap  # noqa

try:
    from test_db_integration import dbs, pg     # noqa: E402,F401
    HAVE_POSTGRES = True
except pytest.skip.Exception:
    HAVE_POSTGRES = False

asyncio = pytest.mark.asyncio


def postgres(test):
    return pytest.mark.skipif(not HAVE_POSTGRES, reason="pgserver is not installed")(
        asyncio(test))


@pytest.fixture(autouse=True)
def fresh_counters(monkeypatch):
    """Every test starts with no failures, no lockouts and no Redis."""
    monkeypatch.setattr(security, "_redis", lambda: None)
    for tracker in (security.AUTH_FAILURES, security.ANON_FAILURES):
        tracker._fails.clear()
        tracker._locked.clear()
    security._TRUSTED.clear()
    security._LOCAL_LIMITERS.clear()
    security._ALLOWLIST.update(at=time.monotonic(), nets=[])
    yield


# ============================================================
# A tiny app behind the real middleware, driven over raw ASGI
# ============================================================

def mini_app() -> FastAPI:
    app = FastAPI()
    security.install(app)

    @app.get("/device")
    async def device(x_device_token: str = Header(None)):
        if x_device_token != "good":
            raise HTTPException(status_code=401, detail="unknown device token")
        return {"ok": True}

    @app.get("/anon")
    async def anon():
        raise HTTPException(status_code=401, detail="who are you")

    @app.get("/boom")
    async def boom():
        raise RuntimeError("secret internals: /srv/aimscribe/db")

    @app.get("/fhir/r4/nothing")
    async def nothing():
        raise HTTPException(status_code=404, detail="no")

    return app


async def call(app, path, *, headers=None, client="203.0.113.9"):
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
             "method": "GET", "path": path, "raw_path": path.encode(), "query_string": b"",
             "root_path": "", "scheme": "http", "server": ("test", 80),
             "client": (client, 5000),
             "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]}
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)
    try:
        await app(scope, receive, send)
    except Exception:
        if not any(m["type"] == "http.response.start" for m in sent):
            raise
    start = next(m for m in sent if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return start["status"], {k.decode(): v.decode() for k, v in start["headers"]}, \
        json.loads(body or b"{}")


@asyncio
async def test_one_stale_pc_never_locks_out_its_clinic():
    app = mini_app()
    for _ in range(security.CREDENTIAL_FAILURES):
        status, _, _ = await call(app, "/device", headers={"X-Device-Token": "stale"})
        assert status == 401
    status, _, _ = await call(app, "/device", headers={"X-Device-Token": "stale"})
    assert status == 429                                  # that token is locked out
    # Every other PC behind the same clinic address carries on.
    status, _, body = await call(app, "/device", headers={"X-Device-Token": "good"})
    assert (status, body) == (200, {"ok": True})


@asyncio
async def test_anonymous_noise_needs_far_more_failures_and_spares_known_addresses():
    app = mini_app()
    await call(app, "/device", headers={"X-Device-Token": "good"}, client="198.51.100.1")
    for _ in range(security.ANONYMOUS_FAILURES):
        await call(app, "/anon", client="198.51.100.1")     # trusted: never locked
        await call(app, "/anon", client="198.51.100.2")
    assert (await call(app, "/anon", client="198.51.100.1"))[0] == 401
    assert (await call(app, "/anon", client="198.51.100.2"))[0] == 429


@asyncio
async def test_an_address_on_a_cmed_allow_list_is_never_locked_or_limited(monkeypatch):
    security._ALLOWLIST.update(at=time.monotonic(), nets=security._networks("203.0.113.0/24"))
    monkeypatch.setattr(security, "PER_ADDRESS_RATE", 3)
    app = mini_app()
    for _ in range(security.ANONYMOUS_FAILURES + 5):
        status, _, _ = await call(app, "/anon", client="203.0.113.50")
    assert status == 401


class FakeRedis:
    """Just the commands security.py uses, with real expiry."""

    def __init__(self):
        self.data, self.expiry = {}, {}

    def _live(self, key):
        if key in self.expiry and self.expiry[key] < time.time():
            self.data.pop(key, None)
            self.expiry.pop(key, None)
        return key in self.data

    async def incr(self, key):
        self._live(key)
        self.data[key] = int(self.data.get(key, 0)) + 1
        return self.data[key]

    async def expire(self, key, seconds):
        self.expiry[key] = time.time() + seconds

    async def ttl(self, key):
        return int(self.expiry[key] - time.time()) if self._live(key) else -2

    async def set(self, key, value, ex=None, nx=False):
        if nx and self._live(key):
            return None
        self.data[key] = value
        if ex:
            self.expiry[key] = time.time() + ex
        return True

    async def delete(self, key):
        self.data.pop(key, None)

    async def exists(self, key):
        return 1 if self._live(key) else 0


@asyncio
async def test_counters_are_shared_through_redis(monkeypatch):
    shared = FakeRedis()
    monkeypatch.setattr(security, "_redis", lambda: shared)
    started = [await security.record_failure("cred", "x") for _ in range(10)]
    assert started == [False] * 9 + [True]
    # A second API process sees the same lockout: nothing is in this process.
    security.AUTH_FAILURES._locked.clear()
    assert await security.locked_for("cred", "x") > 800
    allowed = [(await security.rate_allow("ip", "a", 3))[0] for _ in range(4)]
    assert allowed == [True, True, True, False]


@asyncio
async def test_a_redis_outage_falls_back_without_failing_requests(monkeypatch):
    class Broken:
        async def incr(self, key):
            raise ConnectionError("redis gone")
        ttl = exists = incr
    monkeypatch.setattr(security, "_redis", lambda: Broken())
    assert (await security.rate_allow("ip", "a", 3))[0] is True
    assert await security.locked_for("cred", "y") == 0


# ============================================================
# Errors: request ids and RFC 9457
# ============================================================

@asyncio
async def test_every_error_is_problem_json_with_the_request_id():
    status, headers, body = await call(mini_app(), "/device",
                                       headers={"X-Device-Token": "nope",
                                                "X-Request-ID": "caddy-1234567890"})
    assert status == 401
    assert headers["content-type"].startswith("application/problem+json")
    assert headers["x-request-id"] == "caddy-1234567890"
    assert body["request_id"] == "caddy-1234567890"
    assert body["code"] == "UNAUTHORIZED" and body["status"] == 401
    assert body["detail"] == "unknown device token"        # what old recorders read


@asyncio
async def test_an_unsafe_inbound_request_id_is_replaced():
    _, headers, _ = await call(mini_app(), "/device", headers={"X-Request-ID": "bad id\r\nx"})
    assert len(headers["x-request-id"]) == 32


@asyncio
async def test_a_crash_names_no_internals():
    status, _, body = await call(mini_app(), "/boom")
    assert status == 500 and body["code"] == "INTERNAL_ERROR"
    assert "srv" not in json.dumps(body) and body["reference"] == body["request_id"]


@asyncio
async def test_fhir_paths_get_operation_outcomes_even_from_the_framework():
    status, headers, body = await call(mini_app(), "/fhir/r4/nothing")
    assert status == 404 and body["resourceType"] == "OperationOutcome"
    assert headers["content-type"].startswith("application/fhir+json")


def test_log_lines_carry_the_request_id():
    import logging
    token = security.REQUEST_ID.set("abc12345")
    try:
        record = logging.getLogRecordFactory()("x", logging.INFO, __file__, 1, "m", (), None)
    finally:
        security.REQUEST_ID.reset(token)
    assert record.request_id == "abc12345"


# ============================================================
# Addresses
# ============================================================

@pytest.mark.parametrize("xff, expected", [
    ("198.51.100.7", "198.51.100.7"), ("203.0.113.9:5555", "203.0.113.9"),
    ("[2001:db8::1]:443", "2001:db8::1"), ("::ffff:198.51.100.7", "198.51.100.7"),
    ("1.2.3.4, ", "xff-unparsed"), ("garbage", "xff-unparsed"),
])
def test_forwarded_addresses_are_read_however_written(xff, expected):
    assert security.client_ip(Req(headers={"x-forwarded-for": xff}, peer="172.18.0.5")) \
        == expected


def test_ipv4_mapped_peers_and_allow_lists():
    assert security.client_ip(Req(headers={"x-forwarded-for": "198.51.100.7"},
                                  peer="::ffff:172.18.0.5")) == "198.51.100.7"
    assert security.ip_allowed("::ffff:203.0.113.9", ["203.0.113.0/24"])


# ============================================================
# Malformed FHIR: always 400, never 500
# ============================================================

def _api2_message():
    return fhir_message("patient-information", extra=VITALS)


def _rx_message():
    return fhir_message("prescription", extra=RX)


def _res(message, kind):
    return next(e["resource"] for e in message["entry"] if e["resource"]["resourceType"] == kind)


def _set(kind, path, value, *, message=_api2_message):
    def build():
        m = copy.deepcopy(message())       # the shared example resources stay untouched
        target = _res(m, kind)
        *parents, last = path
        for p in parents:
            target = target[p]
        target[last] = value
        return m
    return build


def _rx_set(path, value):
    return _set("MedicationRequest", path, value, message=_rx_message)


BIG = 10 ** 400
CRASHERS = {
    "period is a string": _set("Encounter", ["period"], "2026"),
    "period is a list": _set("Encounter", ["period"], ["x"]),
    "participant is a number": _set("Encounter", ["participant"], 5),
    "identifier is a number": _set("Patient", ["identifier"], 5),
    "eventCoding.code is a list": _set("MessageHeader", ["eventCoding", "code"], ["x"]),
    "eventCoding.code is an object": _set("MessageHeader", ["eventCoding", "code"], {"a": 1}),
    "focus is an object": _set("MessageHeader", ["focus"], {"reference": "x"}),
    "focus is a number": _set("MessageHeader", ["focus"], 5),
    "Observation.code.coding is a number": _set("Observation", ["code", "coding"], 5),
    "Observation.category is a number": _set("Observation", ["category"], 5),
    "Observation.note is a number": _set("Observation", ["note"], 5),
    "Observation.component is a number": _set("Observation", ["component"], 5),
    "valueQuantity is a 400-digit integer": _set("Observation", ["valueQuantity", "value"], BIG),
    "valueQuantity is infinite": _set("Observation", ["valueQuantity", "value"], math.inf),
    "valueQuantity is NaN": _set("Observation", ["valueQuantity", "value"], math.nan),
    "Patient.name is a number": _set("Patient", ["name"], 5),
    "name[0].text is a number": _set("Patient", ["name"], [{"text": 5}]),
    "name[0].given is a number": _set("Patient", ["name"], [{"given": 5}]),
    "telecom is a number": _set("Patient", ["telecom"], 5),
    "address[0].line is a number": _set("Patient", ["address"], [{"line": 5}]),
    "medication coding is a number": _rx_set(["medicationCodeableConcept"],
                                             {"coding": 5}),
    "dosageInstruction is an object": _rx_set(["dosageInstruction"], {"text": "x"}),
    "dosageInstruction is a number": _rx_set(["dosageInstruction"], 5),
    "doseAndRate is an object": _rx_set(["dosageInstruction"], [{"doseAndRate": {"a": 1}}]),
    "timing.repeat.period is text": _rx_set(["dosageInstruction"], [{"timing": {"repeat": {
        "frequency": 2, "period": "abc", "periodUnit": "d"}}}]),
    "timing.repeat.period is null": _rx_set(["dosageInstruction"], [{"timing": {"repeat": {
        "frequency": 2, "period": None, "periodUnit": "d"}}}]),
    "timing.repeat.period is a list": _rx_set(["dosageInstruction"], [{"timing": {"repeat": {
        "frequency": 2, "period": [1], "periodUnit": "d"}}}]),
    "timing.repeat.period is huge": _rx_set(["dosageInstruction"], [{"timing": {"repeat": {
        "frequency": 2, "period": BIG, "periodUnit": "d"}}}]),
    "additionalInstruction is a number": _rx_set(["dosageInstruction"],
                                                 [{"additionalInstruction": 5}]),
    "dispenseRequest is text": _rx_set(["dispenseRequest"], "x"),
    "CarePlan.period is text": _set("CarePlan", ["period"], "2026", message=_rx_message),
}


@pytest.mark.parametrize("name", sorted(CRASHERS))
def test_no_malformed_shape_crashes_the_reader(name):
    message = CRASHERS[name]()
    try:
        kind, body = fhir.to_body(fhir.Bundle(message))
    except fhir.FhirError:
        return                                           # refused by name: right
    # Read: then it must be a body the rest of the pipeline can store.
    json.dumps(body, allow_nan=False)
    clinical.problems_with(kind, body)


@asyncio
async def test_the_endpoint_answers_any_unreadable_message_with_400(server, monkeypatch):
    def explode(*a, **k):
        raise TypeError("a shape nobody foresaw")
    monkeypatch.setattr(fhir, "to_body", explode)
    reply = await fhir.process_message(Req(_rx_message(), headers=_token(server)))
    assert reply.status_code == 400
    assert json.loads(reply.body)["issue"][0]["details"]["coding"][0]["code"] == "MALFORMED_FHIR"


class RawReq(Req):
    def __init__(self, raw: bytes, **kw):
        super().__init__(None, **kw)
        self._raw = raw


@pytest.mark.parametrize("raw", [
    b'{"a": NaN}', b'{"a": Infinity}', b'{"a": -Infinity}',
    b'{"name": "\\ud800"}', b'{"name": "a\\u0000b"}', b'[1, 2]', b'{"a": 1',
    b'\xff\xfe',
])
@asyncio
async def test_bodies_postgres_would_refuse_are_malformed_json(raw):
    with pytest.raises(clinical._Refusal) as err:
        await clinical._read_json(RawReq(raw))
    assert err.value.code == "MALFORMED_JSON"


@asyncio
async def test_a_unicode_digit_count_is_a_400_not_a_500(server):
    reply = await fhir.search_encounters(Req(headers=_token(server),
                                             query={"patient.identifier": "P1", "_count": "²"}))
    assert reply.status_code == 400
    reply = await clinical.patient_visits("P1", Req(headers=_token(server),
                                                    query={"limit": "²"}))
    assert reply.status_code == 400


# ============================================================
# Identifiers
# ============================================================

def _patient(*ids):
    return {"resourceType": "Patient", "identifier": list(ids)}


def test_the_agreed_system_wins_among_several():
    p = _patient({"system": "urn:mrn", "value": "M1"},
                 {"system": fhir.SYSTEMS["Patient"], "value": "P1"})
    assert fhir.identifier(p, "Patient") == "P1"


def test_a_hospitals_own_system_wins_first():
    p = _patient({"system": "https://cmed.example/pid", "value": "C9"},
                 {"system": fhir.SYSTEMS["Patient"], "value": "P1"})
    assert fhir.identifier(p, "Patient", {"Patient": "https://cmed.example/pid"}) == "C9"


def test_the_official_one_is_used_when_no_system_matches():
    p = _patient({"system": "urn:mrn", "value": "M1"},
                 {"system": "urn:nid", "value": "N1", "use": "official"})
    assert fhir.identifier(p, "Patient") == "N1"


def test_only_genuine_ambiguity_is_refused():
    p = _patient({"system": "urn:mrn", "value": "M1"}, {"system": "urn:nid", "value": "N1"})
    with pytest.raises(fhir.FhirError, match="2 identifiers"):
        fhir.identifier(p, "Patient")
    same = _patient({"system": "urn:a", "value": "X"}, {"system": "urn:b", "value": "X"})
    assert fhir.identifier(same, "Patient") == "X"


@asyncio
async def test_search_with_another_system_finds_nothing(server):
    called = []

    async def visits_for(*a, **k):
        called.append(1)
        return []
    server.clinical.visits_for = visits_for
    reply = await fhir.search_encounters(Req(headers=_token(server), query={
        "patient.identifier": "urn:someone-else|P0012345"}))
    assert reply.status_code == 200 and json.loads(reply.body)["total"] == 0
    assert called == []


# ============================================================
# One moment, one start_time
# ============================================================

@pytest.mark.parametrize("written", [
    "2026-09-13T10:14:32+06:00", "2026-09-13T04:14:32Z", "2026-09-13T04:14:32.000Z",
    "2026-09-13T04:14:32.123456+00:00", "2026-09-13T11:14:32+07:00",
])
def test_every_spelling_of_one_moment_is_one_visit(written):
    visit = conf.parse_visit({**FIELDS, "start_time": written})
    assert visit.start_time == "2026-09-13T10:14:32+06:00"
    assert visit == conf.parse_visit(FIELDS)


def test_the_date_is_the_clinics_day_of_that_moment():
    late = {**FIELDS, "start_time": "2026-09-12T19:30:00Z", "date": "2026-09-12"}
    assert conf.parse_visit(late).visit_date == "2026-09-13"      # 01:30 in Dhaka
    with pytest.raises(conf.FieldError, match="local date"):
        conf.parse_visit(late, strict_date=True)


@asyncio
async def test_cmed_messages_with_a_contradicting_date_are_refused(server):
    body = api2(start_time="2026-09-12T19:30:00Z", date="2026-09-12")
    reply = await clinical.receive(Req(body, headers=_token(server)), "patient_information")
    status, answer = unwrap(reply)
    assert (status, answer["code"], answer["fields"]) == (400, "MISSING_FIELD", ["date"])


@asyncio
async def test_one_message_written_two_ways_is_stored_once(server):
    headers = _token(server)
    first = unwrap(await clinical.receive(Req(api2(), headers=headers), "patient_information"))
    again = unwrap(await clinical.receive(
        Req(api2(start_time="2026-09-13T04:14:32.000Z"), headers=headers),
        "patient_information"))
    assert first[0] == 202 and again[1]["code"] == "ALREADY_RECEIVED"


# ============================================================
# FHIR conformance
# ============================================================

CLINICAL = ("MedicationRequest", "Condition", "ServiceRequest", "CarePlan", "Observation")
REQUIRED = {                         # R4 elements with min 1 that the clients produce
    "MessageHeader": ("eventCoding", "source"),
    "Encounter": ("status", "class"),
    "MedicationRequest": ("status", "intent", "subject"),
    "Condition": ("subject",),
    "ServiceRequest": ("status", "intent", "subject"),
    "CarePlan": ("status", "intent", "subject"),
    "Observation": ("status", "code"),
}


def structural_problems(bundle):
    found = []
    for n, e in enumerate(bundle["entry"]):
        r = e["resource"]
        for name in REQUIRED.get(r["resourceType"], ()):
            if not r.get(name):
                found.append(f"entry[{n}] {r['resourceType']}.{name} missing")
        if r["resourceType"] in CLINICAL and not r.get("subject"):
            found.append(f"entry[{n}] {r['resourceType']}.subject missing")
        if r["resourceType"] == "MessageHeader" and not (r.get("source") or {}).get("endpoint"):
            found.append("MessageHeader.source.endpoint missing")
    return found


@pytest.mark.parametrize("kind, body", [("patient_information", FULL_API2),
                                        ("prescription", FULL_API3)])
def test_the_reference_client_writes_complete_r4_resources(kind, body):
    assert structural_problems(client_ref.to_fhir(kind, body)) == []


@asyncio
async def test_message_definitions_and_capability_point_at_each_other():
    cap = json.loads((await fhir.metadata(Req())).body)
    for item in cap["messaging"][0]["supportedMessage"]:
        ident = item["definition"].rsplit("/", 1)[-1]
        definition = json.loads((await fhir.read_message_definition(ident, Req())).body)
        assert definition["url"] == item["definition"]
        assert definition["resourceType"] == "MessageDefinition"
        assert definition["eventCoding"]["code"] in fhir.EVENTS
    assert (await fhir.read_message_definition("nope", Req())).status_code == 404


@asyncio
async def test_smart_scopes_are_valid_v2_names_and_old_ones_still_work():
    smart = json.loads((await fhir.smart_config_root(Req())).body)
    assert smart["scopes_supported"] == ["system/Bundle.c", "system/Encounter.rs"]
    assert cmed_auth.canonical_scopes(["system/clinical.write", "system/Encounter.read"]) == \
        {"system/Bundle.c", "system/Encounter.rs"}
    assert cmed_auth.granted_scopes("system/MessageHeader.write", cmed_auth.ALL_SCOPES) == \
        {"system/Bundle.c"}


@asyncio
async def test_old_scope_names_on_a_token_are_still_honoured(server):
    headers = _token(server, scopes=["system/clinical.write", "system/Encounter.read"])
    status, _ = unwrap(await clinical.receive(Req(api2(), headers=headers),
                                              "patient_information"))
    assert status == 202


@asyncio
async def test_one_encounter_can_be_read_but_only_at_the_tokens_hospitals(server):
    seen = {}

    async def encounter_by_id(encounter_id, **kw):
        seen.update(kw)
        return None if "ELSEWHERE" in kw["hospitals"] else {
            "_key": None, "patient_id": "P1", "doctor_id": "D1", "hospital_id": CMED_ID,
            "start_time": FIELDS["start_time"], "date": FIELDS["date"], "source": "live",
            "prescription_version": None, "prescription_fingerprint": None,
            "received_at": None}
    server.clinical.encounter_by_id = encounter_by_id
    eid = str(uuid.uuid4())
    reply = await fhir.read_encounter(eid, Req(headers=_token(server)))
    assert reply.status_code == 200 and json.loads(reply.body)["id"] == eid
    assert seen["hospitals"] == [CMED_ID]
    other = await fhir.read_encounter(eid, Req(headers=_token(server, hospitals=["ELSEWHERE"])))
    assert other.status_code == 404
    assert (await fhir.read_encounter("not-a-uuid", Req(headers=_token(server)))).status_code \
        == 404


@pytest.mark.skipif(not (shutil.which("java") and os.getenv("AIMS_FHIR_VALIDATOR")),
                    reason="set AIMS_FHIR_VALIDATOR to the HL7 validator_cli.jar (and have "
                           "java) to validate the example messages against R4")
@pytest.mark.parametrize("kind, body", [("patient_information", FULL_API2),
                                        ("prescription", FULL_API3)])
def test_the_hl7_validator_accepts_the_reference_messages(kind, body, tmp_path):
    path = tmp_path / f"{kind}.json"
    path.write_text(json.dumps(client_ref.to_fhir(kind, body)), encoding="utf-8")
    run = subprocess.run(["java", "-jar", os.environ["AIMS_FHIR_VALIDATOR"], str(path),
                          "-version", "4.0.1"], capture_output=True, text=True, timeout=600)
    assert "*FAILURE*" not in run.stdout and run.returncode == 0, run.stdout[-3000:]


def test_the_typescript_reference_client_round_trips_exactly(tmp_path):
    npx, node = shutil.which("npx"), shutil.which("node")
    web = ROOT / "cmed-web"
    if not (npx and node and (web / "node_modules" / "typescript").exists()):
        pytest.skip("node and cmed-web's node_modules are needed")
    out = tmp_path / "ts"
    built = subprocess.run([npx, "tsc", str(web / "src" / "lib" / "cmed-fhir.ts"), "--outDir",
                            str(out), "--module", "commonjs", "--target", "es2020",
                            "--esModuleInterop", "--skipLibCheck"],
                           capture_output=True, text=True, cwd=web, timeout=180,
                           shell=os.name == "nt")
    assert built.returncode == 0, built.stdout + built.stderr
    script = (f"const m = require({json.dumps(str(out / 'cmed-fhir.js'))});"
              f"console.log(JSON.stringify([m.toFhirMessage('patient_information', "
              f"{json.dumps(FULL_API2)}), m.toFhirMessage('prescription', "
              f"{json.dumps(FULL_API3)})]));")
    env = {**os.environ, "NODE_PATH": str(web / "node_modules")}
    run = subprocess.run([node, "-e", script], capture_output=True, text=True, env=env,
                         timeout=60)
    assert run.returncode == 0, run.stderr
    api2_msg, rx_msg = json.loads(run.stdout)
    assert fhir.to_body(fhir.Bundle(api2_msg)) == ("patient_information", FULL_API2)
    assert fhir.to_body(fhir.Bundle(rx_msg)) == ("prescription", FULL_API3)
    assert structural_problems(api2_msg) == [] and structural_problems(rx_msg) == []


# ============================================================
# The interim key says it is going away
# ============================================================

@asyncio
async def test_static_key_replies_carry_deprecation_and_sunset(server):
    reply = await clinical.receive(Req(api2(), headers={"x-cmed-key": KEY}),
                                   "patient_information")
    assert reply.status_code == 202
    assert reply.headers["deprecation"].startswith("@")
    assert reply.headers["sunset"].endswith("GMT")
    token_reply = await clinical.receive(Req(prescription(), headers=_token(server)),
                                         "prescription")
    assert "deprecation" not in token_reply.headers


@asyncio
async def test_every_channel_b_reply_names_its_request():
    token = security.REQUEST_ID.set("rid-12345678")
    try:
        reply = await clinical.receive(Req(api2()), "patient_information")
    finally:
        security.REQUEST_ID.reset(token)
    assert json.loads(reply.body)["request_id"] == "rid-12345678"


# ============================================================
# /health
# ============================================================

def _health_request(headers=None):
    return SimpleNamespace(headers=headers or {})


@asyncio
async def test_public_health_says_only_whether_it_is_up(monkeypatch):
    app_module = _app_module()

    async def details():
        return {"status": "degraded", "database": "connected", "schema": "missing 019",
                "minio": "connected", "version": "3.5.0", "system": app_module.SYSTEM_NAME}
    monkeypatch.setattr(app_module, "_details", details)
    monkeypatch.setenv("AIMS_ADMIN_KEY", "adminkey")
    public = await app_module.health(_health_request({"x-forwarded-for": "198.51.100.7"}))
    assert public == {"status": "degraded", "system": app_module.SYSTEM_NAME}
    internal = await app_module.health(_health_request())
    assert internal["schema"] == "missing 019"                 # the monitor still reads it
    admin = await app_module.health(_health_request({"x-forwarded-for": "198.51.100.7",
                                                     "x-admin-key": "adminkey"}))
    assert admin["version"] == "3.5.0"
    ready = await app_module.health_ready(_health_request())
    assert ready.status_code == 503 and json.loads(ready.body)["ready"] is False
    assert (await app_module.health_live()) == {"status": "alive"}


# ============================================================
# The published contract
# ============================================================

OPENAPI = ROOT / "openapi" / "channel-b.json"


def channel_b_schema():
    full = _app_module().app.openapi()
    keep = {"cmed-auth", "clinical", "fhir"}
    paths = {}
    for path, item in full["paths"].items():
        ops = {m: op for m, op in item.items() if keep & set(op.get("tags", []))}
        if ops and "/admin/" not in path:
            paths[path] = ops
    used = json.dumps(paths)
    schemas = {k: v for k, v in full.get("components", {}).get("schemas", {}).items()
               if f"#/components/schemas/{k}" in used}
    return {"openapi": full["openapi"],
            "info": {"title": "AIMScribe v3 - Channel B (CMED server to AIMS LAB)",
                     "version": full["info"]["version"]},
            "paths": dict(sorted(paths.items())),
            "components": {"schemas": dict(sorted(schemas.items()))}}


def test_the_published_openapi_contract_matches_the_server():
    current = channel_b_schema()
    if os.getenv("AIMS_WRITE_OPENAPI"):
        OPENAPI.parent.mkdir(exist_ok=True)
        OPENAPI.write_text(json.dumps(current, indent=2, sort_keys=True) + "\n",
                           encoding="utf-8")
    assert OPENAPI.exists(), "run once with AIMS_WRITE_OPENAPI=1 to publish it"
    published = json.loads(OPENAPI.read_text(encoding="utf-8"))
    assert published == json.loads(json.dumps(current)), \
        "Channel B changed: review, then regenerate with AIMS_WRITE_OPENAPI=1"
    for path in ("/api/v2/oauth/token", "/fhir/r4/$process-message", "/fhir/r4/Encounter",
                 "/api/v2/clinical/patient-information"):
        assert path in published["paths"]


# ============================================================
# Token lifecycle, against PostgreSQL
# ============================================================

@postgres
async def test_a_kid_postgres_cannot_store_is_refused_before_the_database(hospital):
    signed = assertion(hospital.private, "\x00x", hospital.client_id)
    status, answer = await get_token(hospital, signed)
    assert (status, answer["error"]) == (400, "invalid_client")


@postgres
@pytest.mark.parametrize("exp", ["1792508759", 10 ** 30])
async def test_exp_must_be_a_sane_whole_number(hospital, exp):
    signed = jwt.encode({"iss": hospital.client_id, "sub": hospital.client_id,
                         "aud": "https://aims.example" + cmed_auth.TOKEN_PATH,
                         "exp": exp, "jti": uuid.uuid4().hex},
                        hospital.private, algorithm="ES384", headers={"kid": hospital.kid})
    status, answer = await get_token(hospital, signed)
    assert status in (400, 401) and answer["error"] == "invalid_client"


@postgres
async def test_clock_skew_is_said_plainly_and_not_counted_as_a_wrong_key(hospital):
    signed = assertion(hospital.private, hospital.kid, hospital.client_id, lifetime=-600)
    for _ in range(security.CREDENTIAL_FAILURES + 2):
        status, answer = await get_token(hospital, signed)
        assert (status, answer["error"]) == (400, "invalid_client")
    assert "clock skew" in answer["error_description"]
    good = assertion(hospital.private, hospital.kid, hospital.client_id)
    assert (await get_token(hospital, good))[0] == 200


@postgres
async def test_another_address_spamming_in_cmeds_name_cannot_lock_cmed_out(hospital):
    for _ in range(security.CREDENTIAL_FAILURES + 1):
        bad = assertion(_ec_jwk()[0], hospital.kid, hospital.client_id)
        reply = await cmed_auth.token(Req(form=token_form(bad), peer="192.0.2.66"))
    assert reply.status_code == 429
    good = assertion(hospital.private, hospital.kid, hospital.client_id)
    assert (await get_token(hospital, good))[0] == 200       # CMED's own address


@postgres
async def test_with_a_public_base_url_only_that_audience_is_accepted(hospital, monkeypatch):
    monkeypatch.setenv("AIMS_PUBLIC_BASE_URL", "https://prod.example")
    signed = assertion(hospital.private, hospital.kid, hospital.client_id)   # aud: aims.example
    status, _ = await get_token(hospital, signed)
    assert status == 401
    right = assertion(hospital.private, hospital.kid, hospital.client_id,
                      aud="https://prod.example" + cmed_auth.TOKEN_PATH)
    assert (await get_token(hospital, right))[0] == 200


@postgres
async def test_a_scope_refusal_does_not_spend_the_assertion(hospital):
    signed = assertion(hospital.private, hospital.kid, hospital.client_id)
    status, answer = await get_token(hospital, signed, scope="user/*.read")
    assert (status, answer["error"]) == (400, "invalid_scope")
    assert (await get_token(hospital, signed))[0] == 200


@postgres
async def test_revoking_a_key_stops_the_tokens_it_bought(hospital):
    _, answer = await get_token(hospital, assertion(hospital.private, hospital.kid,
                                                    hospital.client_id))
    bearer = {"authorization": f"Bearer {answer['access_token']}"}
    assert (await clinical.patient_visits("PX", Req(headers=bearer))).status_code == 200
    await cmed_auth.revoke_key(hospital.client_id, hospital.kid, None)
    assert (await clinical.patient_visits("PX", Req(headers=bearer))).status_code == 401


@postgres
async def test_renewal_and_the_expiry_warning(hospital):
    async with hospital.dbs.repo._pool.acquire() as conn:
        await conn.execute("UPDATE cmed_clients SET expires_at = now() + interval '10 days' "
                           "WHERE client_id = $1", hospital.client_id)
    soon = await cmed_auth.expiring_clients(30)
    assert hospital.client_id in {c["client_id"] for c in soon}
    await cmed_auth.expiring_clients(30)                       # once a day, not twice
    async with hospital.dbs.repo._pool.acquire() as conn:
        alerts = await conn.fetchval(
            "SELECT count(*) FROM integrity_alerts WHERE alert_type = 'cmed_client_expiring' "
            "AND detail->>'client_id' = $1", hospital.client_id)
    assert alerts == 1
    renewed = await cmed_auth.renew_client(hospital.client_id,
                                           cmed_auth.RenewRequest(days=365, created_by="t"),
                                           None)
    assert renewed["expires_at"] > soon[0]["expires_at"]
    assert hospital.client_id not in {c["client_id"]
                                      for c in await cmed_auth.expiring_clients(30, alert=False)}


@postgres
async def test_a_hospitals_registered_identifier_system_is_used(dbs):
    api_v2.ctx.repo, api_v2.ctx.clinical = dbs.repo, dbs.clinical
    private, public = _ec_jwk()
    cid = f"cmed-{uuid.uuid4().hex[:8]}"
    await cmed_auth.register_client(cmed_auth.ClientRequest(
        client_id=cid, label="t", cmed_hospital_ids=["CMED-DHOLPUR"], created_by="t",
        jwk=public, identifier_systems={"Patient": "https://cmed.example/pid"}), None)
    assert (await cmed_auth.client_systems(cid)) == {"Patient": "https://cmed.example/pid"}


@postgres
async def test_the_same_bundle_id_with_other_content_is_flagged(hospital):
    _, answer = await get_token(hospital, assertion(hospital.private, hospital.kid,
                                                    hospital.client_id))
    bearer = {"authorization": f"Bearer {answer['access_token']}"}
    five = {**FIELDS, "hospital_id": "CMED-DHOLPUR", "patient_id": "PBID1"}
    first = fhir_message("prescription", five, extra=RX)
    second = fhir_message("prescription", {**five, "patient_id": "PBID2"}, extra=RX)
    second["id"] = first["id"] = "same-bundle-id-" + uuid.uuid4().hex[:6]
    for message in (first, second):
        reply = await fhir.process_message(Req(message, headers=bearer))
        assert reply.status_code == 200, reply.body
    async with hospital.dbs.repo._pool.acquire() as conn:
        flagged = await conn.fetchval(
            "SELECT count(*) FROM integrity_alerts WHERE alert_type = 'fhir_bundle_id_reused' "
            "AND detail->>'bundle_id' = $1", first["id"])
    assert flagged == 1


# ============================================================
# The reference client's outbox behaviour
# ============================================================

def test_the_outbox_honours_retry_after_and_jitters_otherwise(monkeypatch):
    replies = iter([(429, {}, {"Retry-After": "7"}), (503, {}, {}), (200, {"ok": 1}, {})])
    monkeypatch.setattr(client_ref, "_send", lambda *a, **k: next(replies))

    class Tokens:
        def get(self):
            return "t"

        def forget(self):
            pass
    waits = []
    status, reply = client_ref.deliver("https://x", Tokens(), {}, sleep=waits.append)
    assert (status, reply) == (200, {"ok": 1})
    assert waits[0] == 7 and 0 <= waits[1] <= 4


def test_the_outbox_never_resends_a_message_the_server_refused(monkeypatch):
    calls = []
    monkeypatch.setattr(client_ref, "_send",
                        lambda *a, **k: calls.append(1) or (422, {"code": "SCHEMA_INVALID"}, {}))

    class Tokens:
        def get(self):
            return "t"

        def forget(self):
            pass
    status, _ = client_ref.deliver("https://x", Tokens(), {}, sleep=lambda s: None)
    assert status == 422 and len(calls) == 1
