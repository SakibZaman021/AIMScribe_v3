"""
CMED's identity per hospital, and API 2 / API 3b in FHIR R4.

Without a database: the scope rules, the public-key checks, rate limiting and
lockout, which address a request is from, the hospital binding of every
message, and the FHIR mapping - a FHIR message must store exactly what the
JSON form of the same message stores.

Against PostgreSQL (when pgserver is installed): the whole SMART Backend
Services flow - register a hospital, sign an assertion, get a token, send a
FHIR message with it - and every way an assertion must be refused.
"""
from __future__ import annotations

import json
import sys
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import jwt
import pytest
import pytest_asyncio
from cryptography.hazmat.primitives.asymmetric import ec, rsa

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import api_v2                                   # noqa: E402
import clinical                                 # noqa: E402
import cmed_auth                                # noqa: E402
import fhir                                     # noqa: E402
import security                                 # noqa: E402
from test_v3_server import (                    # noqa: E402,F401
    CMED_ID, FIELDS, KEY, api2, prescription, server, unwrap,
)

try:
    from test_db_integration import dbs, pg     # noqa: E402,F401
    HAVE_POSTGRES = True
except pytest.skip.Exception:
    HAVE_POSTGRES = False

asyncio = pytest.mark.asyncio


def postgres(test):
    return pytest.mark.skipif(not HAVE_POSTGRES, reason="pgserver is not installed")(
        asyncio(test))


class Req:
    """Enough of a Starlette request for the endpoints under test."""

    def __init__(self, body=None, *, headers=None, form=None, query=None,
                 peer="203.0.113.9", url="https://aims.example/api/v2/oauth/token"):
        self._raw = json.dumps(body).encode() if body is not None else b""
        self.headers = {"host": "aims.example", **(headers or {})}
        self._form = form or {}
        self.query_params = query or {}
        self.client = SimpleNamespace(host=peer)
        self.url = SimpleNamespace(scheme="https", netloc="aims.example", path="/",
                                   __str__=lambda: url)
        self.method = "POST"

    async def body(self):
        return self._raw

    async def form(self):
        return self._form


# ============================================================
# Scopes, keys, limits (no database)
# ============================================================

def test_scopes_map_smart_spellings_and_never_grant_more_than_allowed():
    both = (cmed_auth.WRITE, cmed_auth.READ)
    assert cmed_auth.granted_scopes("", both) == set(both)
    assert cmed_auth.granted_scopes("system/*.write", both) == {cmed_auth.WRITE}
    assert cmed_auth.granted_scopes("system/*.cruds", both) == set(both)
    assert cmed_auth.granted_scopes("system/Encounter.rs", [cmed_auth.WRITE]) == set()
    assert cmed_auth.granted_scopes("user/*.* launch", both) == set()


def _ec_jwk(curve=ec.SECP384R1()):
    private = ec.generate_private_key(curve)
    public = json.loads(jwt.algorithms.ECAlgorithm.to_jwk(private.public_key()))
    return private, public


def test_a_private_key_is_refused_even_by_mistake():
    private, public = _ec_jwk()
    leaked = json.loads(jwt.algorithms.ECAlgorithm.to_jwk(private))
    with pytest.raises(ValueError, match="PRIVATE"):
        cmed_auth.check_public_jwk(leaked, "ES384")
    assert cmed_auth.check_public_jwk(public, "ES384") == cmed_auth.jwk_thumbprint(public)


def test_key_type_curve_and_size_must_fit_the_algorithm():
    _, p256 = _ec_jwk(ec.SECP256R1())
    with pytest.raises(ValueError):
        cmed_auth.check_public_jwk(p256, "ES384")
    assert cmed_auth.check_public_jwk(p256, "ES256")
    small = rsa.generate_private_key(public_exponent=65537, key_size=1024).public_key()
    with pytest.raises(ValueError, match="2048"):
        cmed_auth.check_public_jwk(json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(small)),
                                   "RS384")


def test_the_thumbprint_is_rfc7638():
    # RFC 7638 §3.1's worked example.
    jwk = {"kty": "RSA", "e": "AQAB", "n": (
        "0vx7agoebGcQSuuPiLJXZptN9nndrQmbXEps2aiAFbWhM78LhWx4cbbfAAtVT86zwu1RK7aPFFxuhDR1L6"
        "tSoc_BJECPebWKRXjBZCiFV4n3oknjhMstn64tZ_2W-5JsGY4Hc5n9yBXArwl93lqt7_RN5w6Cf0h4QyQ5"
        "v-65YGjQR0_FDW2QvzqY368QQMicAtaSqzs8KJZgnYb9c7d0zgdAZHzu6qMQvRL5hajrn1n91CbOpbIS"
        "D08qNLyrdkt-bFTWhAI4vMQFh6WeZu0fM4lFd2NcRwr3XPksINHaQ-G_xBniIqbw0Ls1jF44-csFCur-kE"
        "gU8awapJzKnqDKgw")}
    assert cmed_auth.jwk_thumbprint(jwk) == "NzbLsXh8uDCcd-6MNwXF4W_7noWXFZAfHkxZsRGC9Xs"


def test_rate_limiter_refills_over_time():
    now = [0.0]
    limit = security.RateLimiter(3, 60, clock=lambda: now[0])
    assert [limit.allow("a")[0] for _ in range(4)] == [True, True, True, False]
    assert limit.allow("b")[0]                       # another caller is unaffected
    now[0] = 20.0                                    # one token back every 20 s
    assert limit.allow("a")[0] and not limit.allow("a")[0]


def test_repeated_wrong_credentials_lock_the_address_out_once():
    now = [0.0]
    t = security.FailureTracker(limit=3, window=60, lockout=100, clock=lambda: now[0])
    assert [t.fail("x") for _ in range(3)] == [False, False, True]
    assert t.locked("x") == 100 and t.locked("y") == 0
    assert t.fail("x") is False                      # no second alert in one lockout
    now[0] = 101
    assert t.locked("x") == 0


def test_forwarded_address_is_believed_only_from_the_proxy():
    spoof = {"x-forwarded-for": "198.51.100.7"}
    assert security.client_ip(Req(headers=spoof, peer="203.0.113.9")) == "203.0.113.9"
    caddy = {"x-forwarded-for": "1.2.3.4, 198.51.100.7"}
    assert security.client_ip(Req(headers=caddy, peer="172.18.0.5")) == "198.51.100.7"
    assert security.ip_allowed("198.51.100.7", ["198.51.100.0/24"])
    assert not security.ip_allowed("198.51.100.7", ["203.0.113.0/24"])
    assert security.ip_allowed("anything", None)


# ============================================================
# Every message is bound to the credential's hospital
# ============================================================

def _token(server, *, hospitals=(CMED_ID,), scopes=(cmed_auth.WRITE, cmed_auth.READ),
           cidrs=None):
    token = "cat_" + uuid.uuid4().hex
    server.repo.tokens[cmed_auth._digest(token)] = {
        "client_id": "cmed-test", "cmed_hospital_ids": list(hospitals),
        "scopes": list(scopes), "allowed_cidrs": cidrs}
    return {"authorization": f"Bearer {token}"}


async def _json(server, kind, body, headers):
    return unwrap(await clinical.receive(Req(body, headers=headers), kind))


@asyncio
async def test_a_token_for_one_hospital_cannot_write_for_another(server):
    headers = _token(server, hospitals=["CMED-MIRPUR-01"])
    status, reply = await _json(server, "patient_information", api2(), headers)
    assert (status, reply["code"]) == (403, "HOSPITAL_NOT_ALLOWED")
    assert server.clinical.records == []
    alert = server.repo.alerts[-1]
    assert alert["alert_type"] == "cmed_hospital_mismatch"
    assert alert["severity"] == "critical"


@asyncio
async def test_a_token_for_this_hospital_is_accepted(server):
    status, reply = await _json(server, "patient_information", api2(), _token(server))
    assert (status, reply["code"]) == (202, "ACCEPTED")


@asyncio
async def test_a_read_only_token_cannot_write(server):
    headers = _token(server, scopes=[cmed_auth.READ])
    status, reply = await _json(server, "prescription", prescription(), headers)
    assert (status, reply["code"]) == (403, "INSUFFICIENT_SCOPE")


@asyncio
async def test_a_token_is_refused_from_an_address_outside_the_list(server):
    headers = _token(server, cidrs=["198.51.100.0/24"])
    status, reply = await _json(server, "patient_information", api2(), headers)
    assert (status, reply["code"]) == (401, "INVALID_TOKEN")


@asyncio
async def test_an_unknown_token_is_refused(server):
    status, reply = await _json(server, "patient_information", api2(),
                                {"authorization": "Bearer cat_nope"})
    assert (status, reply["code"]) == (401, "INVALID_TOKEN")


@asyncio
async def test_a_static_key_naming_no_hospital_is_refused(server):
    server.repo.keys[clinical.key_digest(KEY)]["hospitals"] = None
    status, reply = await _json(server, "patient_information", api2(), {"x-cmed-key": KEY})
    assert (status, reply["code"]) == (403, "KEY_NOT_BOUND")


@asyncio
async def test_static_keys_can_be_switched_off(server, monkeypatch):
    monkeypatch.setenv("AIMS_CMED_LEGACY_KEYS", "off")
    status, reply = await _json(server, "patient_information", api2(), {"x-cmed-key": KEY})
    assert (status, reply["code"]) == (401, "INVALID_KEY")


@asyncio
async def test_visits_are_listed_only_for_the_credentials_hospitals(server):
    seen = {}

    async def visits_for(patient_id, **kw):
        seen.update(kw)
        return []
    server.clinical.visits_for = visits_for
    reply = await clinical.patient_visits("P0012345", Req(headers=_token(server)))
    assert reply.status_code == 200
    assert seen["hospitals"] == [CMED_ID]


# ============================================================
# FHIR R4
# ============================================================

def fhir_message(event, five=FIELDS, *, extra=(), previous=None):
    enc, pat, doc, org = (f"urn:uuid:{uuid.uuid4()}" for _ in range(4))
    entries = [
        {"fullUrl": f"urn:uuid:{uuid.uuid4()}", "resource": {
            "resourceType": "MessageHeader", "id": "msg-1",
            "eventCoding": {"system": fhir.EVENT_SYSTEM, "code": event},
            "source": {"endpoint": "https://cmed.example"},
            "focus": [{"reference": enc}]}},
        {"fullUrl": enc, "resource": {
            "resourceType": "Encounter", "status": "in-progress",
            "class": {"code": "AMB"},
            "subject": {"reference": pat},
            "participant": [{"individual": {"reference": doc}}],
            "serviceProvider": {"reference": org},
            "period": {"start": five["start_time"]}}},
        {"fullUrl": pat, "resource": {
            "resourceType": "Patient",
            "identifier": [{"system": fhir.SYSTEMS["Patient"], "value": five["patient_id"]}],
            "name": [{"text": "Test"}], "gender": "female"}},
        {"fullUrl": doc, "resource": {
            "resourceType": "Practitioner",
            "identifier": [{"system": "https://cmed.example/doctor", "value": five["doctor_id"]}]}},
        {"fullUrl": org, "resource": {
            "resourceType": "Organization",
            "identifier": [{"value": five["hospital_id"]}]}},
    ]
    for res in extra:
        entries.append({"fullUrl": f"urn:uuid:{uuid.uuid4()}", "resource": res})
    if previous is not None:
        prev = f"urn:uuid:{uuid.uuid4()}"
        entries.append({"fullUrl": prev, "resource": {
            "resourceType": "Encounter", "status": "finished", "subject": {"reference": pat},
            "participant": [{"individual": {"reference": doc}}],
            "serviceProvider": {"reference": org},
            "period": {"start": previous["start_time"]}}})
        for res in previous["resources"]:
            entries.append({"fullUrl": f"urn:uuid:{uuid.uuid4()}",
                            "resource": {**res, "encounter": {"reference": prev}}})
    return {"resourceType": "Bundle", "id": "b-1", "type": "message",
            "timestamp": "2026-09-13T10:26:11+06:00", "entry": entries}


def loinc(code, value, unit, **more):
    return {"resourceType": "Observation", "status": "final",
            "category": [{"coding": [{"code": "vital-signs"}]}],
            "code": {"coding": [{"system": fhir.LOINC, "code": code}]},
            "valueQuantity": {"value": value, "code": unit},
            "effectiveDateTime": "2026-09-13T10:02:10+06:00", **more}


VITALS = [
    loinc("29463-7", 58400, "g"),
    loinc("8302-2", 157, "cm"),
    loinc("8867-4", 78, "/min"),
    loinc("8310-5", 98.6, "[degF]"),
    loinc("59408-5", 98, "%"),
    {"resourceType": "Observation", "status": "final",
     "code": {"coding": [{"system": fhir.LOINC, "code": "85354-9"}]},
     "component": [
         {"code": {"coding": [{"code": "8480-6"}]}, "valueQuantity": {"value": 120}},
         {"code": {"coding": [{"code": "8462-4"}]}, "valueQuantity": {"value": 80}}]},
    {"resourceType": "Observation", "status": "final",
     "code": {"coding": [{"system": fhir.LOINC, "code": "30525-0"}]},
     "valueQuantity": {"value": 34, "code": "a"}},
    {"resourceType": "Observation", "status": "final", "code": {"text": "blood_sugar"},
     "valueQuantity": {"value": 7.2, "code": "mmol/L"}},
]

RX = [
    {"resourceType": "MedicationRequest", "status": "active", "intent": "order",
     "authoredOn": "2026-09-13T10:26:11+06:00",
     "medicationCodeableConcept": {"text": "Paracetamol"},
     "dosageInstruction": [{"doseAndRate": [{"doseQuantity": {"value": 500, "unit": "mg"}}],
                            "timing": {"code": {"text": "1+1+1"},
                                       "repeat": {"boundsDuration": {"value": 5,
                                                                     "unit": "days"}}},
                            "patientInstruction": "after meals"}]},
    {"resourceType": "Condition", "code": {"text": "x"}},
    {"resourceType": "CarePlan", "status": "active", "intent": "plan",
     "description": "rest", "period": {"start": "2026-10-13"}},
]


def test_api2_in_fhir_reads_as_the_json_body():
    kind, body = fhir.to_body(fhir.Bundle(fhir_message("patient-information", extra=VITALS)))
    assert kind == "patient_information"
    assert {k: body[k] for k in FIELDS} == FIELDS
    # The name was sent; it is not read, and the reply will say so.
    assert body["demographics"] == {"sex": "female", "age_years": 34, "not_stored": ["name"]}
    assert body["paramedic"] == {
        "recorded_at": "2026-09-13T10:02:10+06:00", "weight_kg": 58.4, "height_cm": 157,
        "pulse_bpm": 78, "temperature_c": 37, "spo2_percent": 98,
        "blood_pressure": "120/80", "blood_sugar": 7.2}
    assert body["previous_visit"] is None
    assert clinical.problems_with(kind, body) == []


def test_api3b_in_fhir_reads_as_the_json_body():
    kind, body = fhir.to_body(fhir.Bundle(fhir_message("prescription", extra=RX)))
    assert kind == "prescription"
    assert body["items"] == [{"drug": "Paracetamol", "dose": "500 mg", "frequency": "1+1+1",
                              "duration": "5 days", "instructions": "after meals"}]
    assert (body["diagnoses"], body["investigations"]) == (["x"], [])
    assert (body["issued_at"], body["advice"], body["follow_up"]) == \
        ("2026-09-13T10:26:11+06:00", "rest", "2026-10-13")
    assert clinical.problems_with(kind, body) == []


def test_a_previous_visit_travels_as_a_second_encounter():
    previous = {"start_time": "2026-06-02T09:40:05+06:00", "resources": RX[:2]}
    _, body = fhir.to_body(fhir.Bundle(fhir_message("patient-information",
                                                    previous=previous)))
    pv = body["previous_visit"]
    assert (pv["date"], pv["start_time"], pv["hospital_id"]) == \
        ("2026-06-02", "2026-06-02T09:40:05+06:00", CMED_ID)
    assert pv["prescription"]["items"][0]["drug"] == "Paracetamol"
    assert clinical.problems_with("patient_information", body) == []


@pytest.mark.parametrize("breakage, where", [
    (lambda m: m.update(type="collection"), "Bundle.type"),
    (lambda m: m["entry"][0]["resource"]["eventCoding"].update(code="other"),
     "MessageHeader.eventCoding.code"),
    (lambda m: m["entry"][1]["resource"].pop("serviceProvider"), "Encounter.serviceProvider"),
    (lambda m: m["entry"][1]["resource"]["period"].update(start="2026-09-13"),
     "Encounter.period.start"),
    (lambda m: m["entry"][2]["resource"].update(identifier=[]), "Patient.identifier"),
])
def test_an_unreadable_message_names_the_element(breakage, where):
    message = fhir_message("patient-information")
    breakage(message)
    with pytest.raises(fhir.FhirError) as err:
        fhir.to_body(fhir.Bundle(message))
    assert err.value.where == where


@asyncio
async def test_a_fhir_message_stores_what_the_json_form_stores(server):
    message = fhir_message("prescription", extra=RX)
    reply = await fhir.process_message(Req(message, headers=_token(server)))
    # Synchronous FHIR messaging: 200 with the response message; our code is inside.
    assert reply.status_code == 200
    answer = json.loads(reply.body)
    assert answer["type"] == "message"
    header, oo, params = (e["resource"] for e in answer["entry"])
    assert header["response"] == {"identifier": "msg-1", "code": "ok"}
    assert {p["name"]: p.get("valueCode") or p.get("valueString") or p.get("valueInteger")
            for p in params["parameter"]}["code"] == "ACCEPTED"
    stored = server.clinical.records[0]["body"]
    _, expected = fhir.to_body(fhir.Bundle(message))
    assert stored == expected


@asyncio
async def test_fhir_is_bound_to_the_hospital_too(server):
    reply = await fhir.process_message(Req(fhir_message("prescription", extra=RX),
                                           headers=_token(server, hospitals=["ELSEWHERE"])))
    assert reply.status_code == 403
    oo = json.loads(reply.body)["entry"][1]["resource"]
    assert oo["issue"][0]["details"]["coding"][0]["code"] == "HOSPITAL_NOT_ALLOWED"


@asyncio
async def test_fhir_without_a_token_gets_an_operation_outcome(server):
    reply = await fhir.process_message(Req(fhir_message("prescription")))
    assert reply.status_code == 401
    assert json.loads(reply.body)["resourceType"] == "OperationOutcome"
    assert "Bearer" in reply.headers["www-authenticate"]


@asyncio
async def test_the_capability_statement_points_at_the_token_url():
    reply = await fhir.metadata(Req())
    cap = json.loads(reply.body)
    assert cap["fhirVersion"] == "4.0.1"
    uris = cap["rest"][0]["security"]["extension"][0]["extension"]
    assert uris[0]["valueUri"].endswith(cmed_auth.TOKEN_PATH)
    smart = json.loads((await fhir.smart_config_root(Req())).body)
    assert smart["token_endpoint_auth_methods_supported"] == ["private_key_jwt"]


# ============================================================
# The whole token flow, against PostgreSQL
# ============================================================

TOKEN_URL = "https://aims.example" + cmed_auth.TOKEN_PATH


def assertion(private, kid, client_id, *, aud=TOKEN_URL, lifetime=240, jti=None, alg="ES384"):
    now = int(time.time())
    return jwt.encode({"iss": client_id, "sub": client_id, "aud": aud, "iat": now,
                       "exp": now + lifetime, "jti": jti or uuid.uuid4().hex},
                      private, algorithm=alg, headers={"kid": kid})


def token_form(signed, scope="system/*.write system/*.read"):
    return {"grant_type": "client_credentials", "scope": scope,
            "client_assertion_type": cmed_auth.ASSERTION_TYPE,
            "client_assertion": signed}


@pytest_asyncio.fixture
async def hospital(dbs):
    api_v2.ctx.repo, api_v2.ctx.clinical = dbs.repo, dbs.clinical
    api_v2.forget_directory()
    private, public = _ec_jwk()
    client_id = f"cmed-{uuid.uuid4().hex[:8]}"
    done = await cmed_auth.register_client(cmed_auth.ClientRequest(
        client_id=client_id, label="Test hospital", cmed_hospital_ids=["CMED-DHOLPUR"],
        created_by="tests", jwk=public), None)
    security.AUTH_FAILURES._locked.clear()
    return SimpleNamespace(private=private, kid=done["kid"], client_id=client_id, dbs=dbs)


async def get_token(h, signed, **form):
    reply = await cmed_auth.token(Req(form=token_form(signed, **form)))
    return reply.status_code, json.loads(reply.body)


@postgres
async def test_sign_get_a_token_and_send_fhir_with_it(hospital):
    status, answer = await get_token(hospital, assertion(hospital.private, hospital.kid,
                                                         hospital.client_id))
    assert status == 200, answer
    assert answer["expires_in"] == 300 and answer["hospital_ids"] == ["CMED-DHOLPUR"]
    five = {**FIELDS, "hospital_id": "CMED-DHOLPUR", "patient_id": "PFHIR1"}
    reply = await fhir.process_message(Req(
        fhir_message("patient-information", five, extra=VITALS),
        headers={"authorization": f"Bearer {answer['access_token']}"}))
    assert reply.status_code == 200, reply.body

    # The same token for another hospital's message: refused.
    other = {**five, "hospital_id": "CMED-ELSEWHERE"}
    reply = await fhir.process_message(Req(
        fhir_message("patient-information", other),
        headers={"authorization": f"Bearer {answer['access_token']}"}))
    assert reply.status_code == 403


@postgres
async def test_an_assertion_cannot_be_used_twice(hospital):
    signed = assertion(hospital.private, hospital.kid, hospital.client_id)
    assert (await get_token(hospital, signed))[0] == 200
    status, answer = await get_token(hospital, signed)
    assert (status, answer["error"]) == (401, "invalid_client")
    assert "already used" in answer["error_description"]


@postgres
@pytest.mark.parametrize("change", ["wrong_key", "wrong_aud", "too_long", "expired",
                                    "unknown_kid", "none_alg"])
async def test_bad_assertions_are_refused(hospital, change):
    h = hospital
    if change == "wrong_key":
        signed = assertion(_ec_jwk()[0], h.kid, h.client_id)
    elif change == "wrong_aud":
        signed = assertion(h.private, h.kid, h.client_id, aud="https://evil.example/token")
    elif change == "too_long":
        signed = assertion(h.private, h.kid, h.client_id, lifetime=3600)
    elif change == "expired":
        signed = assertion(h.private, h.kid, h.client_id, lifetime=-600)
    elif change == "unknown_kid":
        signed = assertion(h.private, "not-a-kid", h.client_id)
    else:
        signed = jwt.encode({"iss": h.client_id, "sub": h.client_id, "aud": TOKEN_URL,
                             "exp": int(time.time()) + 60, "jti": uuid.uuid4().hex},
                            None, algorithm="none", headers={"kid": h.kid})
    status, answer = await get_token(h, signed)
    assert status in (400, 401) and answer["error"] == "invalid_client"


@postgres
async def test_a_refused_token_names_the_exact_cause(hospital):
    h = hospital

    async def why(private, kid, client_id):
        status, answer = await get_token(h, assertion(private, kid, client_id))
        assert (status, answer["error"]) == (401, "invalid_client")
        return answer["error_description"]

    assert 'no client "cmed-nobody"' in await why(h.private, h.kid, "cmed-nobody")
    assert f'kid "not-a-kid" is not registered for client "{h.client_id}"' in \
        await why(h.private, "not-a-kid", h.client_id)
    assert "signed with a different private key" in await why(_ec_jwk()[0], h.kid, h.client_id)
    await cmed_auth.revoke_client(h.client_id, None)
    assert f'client "{h.client_id}" was revoked' in await why(h.private, h.kid, h.client_id)


@postgres
async def test_revoking_a_hospital_stops_its_live_tokens_at_once(hospital):
    _, answer = await get_token(hospital, assertion(hospital.private, hospital.kid,
                                                    hospital.client_id))
    await cmed_auth.revoke_client(hospital.client_id, None)
    reply = await clinical.patient_visits("PFHIR1", Req(
        headers={"authorization": f"Bearer {answer['access_token']}"}))
    assert reply.status_code == 401
    status, _ = await get_token(hospital, assertion(hospital.private, hospital.kid,
                                                    hospital.client_id))
    assert status == 401


@postgres
async def test_only_the_tokens_hash_is_stored(hospital):
    _, answer = await get_token(hospital, assertion(hospital.private, hospital.kid,
                                                    hospital.client_id))
    async with hospital.dbs.repo._pool.acquire() as conn:
        rows = await conn.fetch("SELECT token_sha256 FROM cmed_access_tokens")
    assert all(answer["access_token"].encode() not in bytes(r["token_sha256"]) for r in rows)
    assert cmed_auth._digest(answer["access_token"]) in {bytes(r["token_sha256"]) for r in rows}


# ============================================================
# A PC cannot take over another PC's recording
# ============================================================

@asyncio
async def test_another_pc_cannot_open_a_recording_it_does_not_own(server):
    from fastapi import HTTPException
    from test_v3_server import DEVICE, OTHER_DEVICE, open_session
    sid = open_session(server, device=DEVICE)
    body = api_v2.OpenSessionRequest.model_construct(
        session_id=sid, doctor_id="DR0042", patient_ref="P0012345", hospital_id="HOSP003")
    with pytest.raises(HTTPException) as err:
        await api_v2.open_session(body, device=OTHER_DEVICE)
    assert err.value.status_code == 409
    assert server.repo.alerts[-1]["alert_type"] == "session_owner_mismatch"
    assert server.repo.sessions[sid]["device_id"] == DEVICE["device_id"]


# ============================================================
# CMED's reference client and this server agree, field for field
# ============================================================

def _reference_client():
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
    import cmed_fhir_client
    return cmed_fhir_client


FULL_API2 = {**FIELDS,
             # Sex and date of birth only: name, phone and address are not sent.
             "demographics": {"sex": "female", "age_years": 34,
                              "date_of_birth": "1992-03-11"},
             "paramedic": {"recorded_at": "2026-09-13T10:02:10+06:00", "weight_kg": 58.4,
                           "height_cm": 157, "blood_pressure": "120/80", "pulse_bpm": 78,
                           "temperature_c": 36.8, "spo2_percent": 98, "notes": "anxious"},
             "previous_visit": {"doctor_id": "DR0007", "hospital_id": "CMED-OTHER",
                                "start_time": "2026-06-02T09:40:05+06:00",
                                "date": "2026-06-02",
                                "prescription": {"items": [{"drug": "Metformin 500 mg",
                                                            "dose": "1 tablet",
                                                            "frequency": "1+0+1",
                                                            "duration": "30 days",
                                                            "instructions": "after meals"}],
                                                 "diagnoses": ["Type 2 diabetes mellitus"],
                                                 "investigations": ["HbA1c"],
                                                 "advice": "walk daily",
                                                 "follow_up": "2026-06-16", "notes": "r",
                                                 "findings": [{"section": "chief_complaint",
                                                               "item": "Thirst", "value": None,
                                                               "detail": {}}]}}}
FULL_API3 = {**FIELDS, "issued_at": "2026-09-13T10:26:55+06:00",
             "items": [{"drug": "Metformin 500 mg", "dose": "1 tablet",
                        "frequency": "1+0+1", "duration": "30 days",
                        "instructions": "after meals"},
                       {"drug": "Napa", "dose": "500 mg"}],
             "diagnoses": ["Type 2 diabetes mellitus"], "investigations": ["HbA1c", "CBC"],
             "advice": "walk daily", "follow_up": "2026-10-13", "notes": "n",
             # The doctor's findings travel with the prescription.
             "findings": [
                 {"section": "chief_complaint", "item": "Fever", "value": None,
                  "detail": {"severity": "moderate", "duration": "3 days"}},
                 {"section": "history", "item": "family", "value": "Diabetes (mother)",
                  "detail": {}},
                 {"section": "allergy", "item": "medicine", "value": "Penicillin", "detail": {}},
                 {"section": "on_examination", "item": "jaundice", "value": "absent",
                  "detail": {}},
                 {"section": "systemic_examination", "item": "heart", "value": "S1 S2 normal",
                  "detail": {"note": "no murmur"}},
                 {"section": "gynae_obstetric", "item": "obstetric", "value": "G2 P2",
                  "detail": {}}]}


@pytest.mark.parametrize("kind, body", [("patient_information", FULL_API2),
                                        ("prescription", FULL_API3),
                                        ("patient_information",
                                         {**FULL_API2, "previous_visit": None})])
def test_the_reference_client_round_trips_exactly(kind, body):
    message = _reference_client().to_fhir(kind, body)
    assert fhir.to_body(fhir.Bundle(message)) == (kind, body)
