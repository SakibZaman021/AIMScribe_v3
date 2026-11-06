"""
CMED's request log (cmed_log.py), through the real middleware, against PostgreSQL:
every token request and API 2 / API 3 is written with its outcome, a client
sees only its own rows (and unattributed refusals from its own address), and
no body or token is ever stored.
"""
from __future__ import annotations

import asyncio
import json
import sys
import uuid
from pathlib import Path

import httpx
import pytest_asyncio
from fastapi import FastAPI

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import cmed_auth                                # noqa: E402
import cmed_log                                 # noqa: E402
import clinical_v4                              # noqa: E402
import security                                 # noqa: E402
from test_cmed_auth_fhir import (               # noqa: E402,F401
    _ec_jwk, assertion, hospital, postgres, token_form,
)
from test_db_integration import dbs, pg         # noqa: E402,F401

AUD = "https://aims.example" + cmed_auth.TOKEN_PATH


@pytest_asyncio.fixture
async def client(hospital):
    app = FastAPI()
    cmed_log.install(app)
    security.install(app)
    app.include_router(cmed_auth.router)
    app.include_router(clinical_v4.router)
    app.include_router(cmed_log.router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=("198.51.100.7", 4000)),
                                 base_url="https://aims.example") as c:
        yield c


async def settled():
    await asyncio.gather(*list(cmed_log._PENDING))


async def token(client, h, private=None):
    reply = await client.post(cmed_auth.TOKEN_PATH, data=token_form(
        assertion(private or h.private, h.kid, h.client_id, aud=AUD)))
    return reply.status_code, reply.json()


async def log(client, access, **params):
    reply = await client.get("/api/v4/clinical/log", params=params,
                             headers={"authorization": f"Bearer {access}"})
    assert reply.status_code == 200, reply.text
    return reply.json()


@postgres
async def test_cmed_sees_its_own_requests_with_the_exact_reason(client, hospital):
    h = hospital
    status, _ = await token(client, h, private=_ec_jwk()[0])         # the wrong key
    assert status == 401
    status, good = await token(client, h)
    assert status == 200
    visit = "VIS-" + uuid.uuid4().hex[:8]
    body = {"visit_id": visit, "patient_id": "PLOG1", "doctor_id": "DR0042",
            "hospital_id": "CMED-DHOLPUR", "start_time": "2025-11-15T11:40:12+06:00",
            "date": "2025-11-15", "demographics": {"sex": "female"}}
    reply = await client.post("/api/v4/clinical/patient-information", json=body,
                              headers={"authorization": f"Bearer {good['access_token']}"})
    assert reply.status_code == 202, reply.text
    reply = await client.post("/api/v4/clinical/patient-information", json=body,
                              headers={"authorization": "Bearer cat_not-a-token"})
    assert reply.status_code == 401
    await settled()

    page = await log(client, good["access_token"])
    assert page["client_id"] == h.client_id
    rows = page["entries"]
    by_api = {}
    for r in rows:
        by_api.setdefault(r["api"], []).append(r)
    refused = [r for r in by_api["API 0 · signed login"] if r["status"] == 401][0]
    assert "signed with a different private key" in refused["reason"]
    assert refused["ip"] == "198.51.100.7" and refused["request_id"] not in (None, "", "-")
    api2 = by_api["API 2 · patient information"]
    assert any(r["code"] == "ACCEPTED" and r["visit_id"] == visit for r in api2)
    # A bad token names no client; it is shown because it came from this address.
    assert any(r["code"] == "INVALID_TOKEN" and not r["client_known"] for r in api2)

    errors = await log(client, good["access_token"], errors_only="true")
    assert errors["entries"] and all(r["status"] >= 400 for r in errors["entries"])
    one = await log(client, good["access_token"], visit_id=visit)
    assert {r["visit_id"] for r in one["entries"]} == {visit}

    async with h.dbs.repo._pool.acquire() as conn:
        stored = json.dumps([dict(r) for r in await conn.fetch(
            "SELECT * FROM cmed_request_log")], default=str)
    assert good["access_token"] not in stored and "female" not in stored


@postgres
async def test_another_client_sees_none_of_it(client, hospital, dbs):
    h = hospital
    _, mine = await token(client, h)
    await settled()
    private, public = _ec_jwk()
    other_id = f"cmed-{uuid.uuid4().hex[:8]}"
    done = await cmed_auth.register_client(cmed_auth.ClientRequest(
        client_id=other_id, label="Other", cmed_hospital_ids=["CMED-OTHER"],
        created_by="tests", jwk=public), None)
    # The other hospital calls from somewhere else.
    app = client._transport.app
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=("203.0.113.50", 1)),
                                 base_url="https://aims.example") as elsewhere:
        reply = await elsewhere.post(cmed_auth.TOKEN_PATH, data=token_form(
            assertion(private, done["kid"], other_id, aud=AUD)))
        theirs = reply.json()
        await settled()
        reply = await elsewhere.get("/api/v4/clinical/log",
                                    headers={"authorization": f"Bearer {theirs['access_token']}"})
    entries = reply.json()["entries"]
    assert entries and all(r["ip"] == "203.0.113.50" for r in entries)
    assert h.client_id not in json.dumps(entries)
    assert (await log(client, mine["access_token"]))["client_id"] == h.client_id


@postgres
async def test_the_log_needs_a_token(client):
    reply = await client.get("/api/v4/clinical/log")
    assert reply.status_code == 401
