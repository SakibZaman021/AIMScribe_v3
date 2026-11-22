"""
049 (23 Nov 2026): who may download what.

    CMED staff   the clinical record (JSON) of every hospital; never the audio,
                 one at a time or in batches; no admin tools, no database
                 viewer, no change to who has access
    researcher   audio and record of the hospitals assigned to them only;
                 they still see every hospital
"""
from __future__ import annotations

import hashlib
import json
import sys
from datetime import date
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND / "src"))
sys.path.insert(0, str(BACKEND / "tests"))

try:
    from test_db_integration import (_DL_WAV, _bulk_app, _bulk_recording,  # noqa: E402,F401
                                     _copied_recording, _researcher, _sign_in, _unserve,
                                     dbs, pg)
    HAVE_POSTGRES = True
except pytest.skip.Exception:
    HAVE_POSTGRES = False

pytestmark = [pytest.mark.asyncio,
              pytest.mark.skipif(not HAVE_POSTGRES, reason="pgserver is not installed")]

SAME = {"Sec-Fetch-Site": "same-origin", "Accept": "application/json"}
H = {"X-AIMS-Portal": "1"}


async def _with_record_copy(dbs, patient):
    """A copied recording of HOSP003 with its clinical record (JSON) copy as well."""
    record = json.dumps({"patient": patient, "rx": ["paracetamol"]}).encode()
    sid, stem = await _copied_recording(dbs, patient)
    await dbs.repo.record_cloud_copy(
        session_id=sid, kind="json",
        object_key=f"copies/HOSP003/DR0042/2026-09-13/{stem}.v1.json.enc",
        version=1, byte_length=len(record) + 200, sha256=b"4" * 32,
        plain_sha256=hashlib.sha256(record).digest())
    return sid, record


def _worker(record):
    import httpx

    def worker(request):
        kind = json.loads(request.content)["expect"]["kind"]
        body = record if kind == "json" else _DL_WAV
        return httpx.Response(200, content=body, headers={"Content-Length": str(len(body))})
    return worker


async def test_cmed_staff_download_the_record_never_the_audio_and_change_nothing(
        dbs, monkeypatch):
    sid, record = await _with_record_copy(dbs, "PCMED1")
    day = date(2026, 8, 3)
    await _bulk_recording(dbs, "PCMED2", "DRCMED", day, 5)
    sel = {"hospital_id": "HOSP003", "from": day.isoformat(), "to": day.isoformat()}
    who = {"value": "owner@uiu.example"}
    async with _bulk_app(dbs, monkeypatch, who, _worker(record)) as client:
        await _sign_in(client)
        added = await client.post("/api/v2/admin/people", headers=H,
                                  json={"email": "staff@cmed.example", "role": "cmed"})
        assert added.status_code == 200 and added.json()["role"] == "cmed", added.text
        # No database login for CMED staff.
        assert (await client.post("/api/v2/admin/people/db-login", headers=H,
                                  json={"email": "staff@cmed.example"})).status_code == 409
        client.cookies.clear()
        who["value"] = "staff@cmed.example"
        await _sign_in(client)
        me = (await client.get("/auth/me")).json()
        assert me["role"] == "cmed" and "download_hospitals" not in me

        # The clinical record of any hospital: yes.
        got = await client.get(f"/api/v2/dashboard/recordings/{sid}/record", headers=SAME)
        assert got.status_code == 200 and got.content == record
        # The audio: never - one at a time, or in a batch.
        audio = await client.get(f"/api/v2/dashboard/recordings/{sid}/audio", headers=SAME)
        assert audio.status_code == 403 and "not the audio" in audio.json()["detail"]
        assert (await client.get("/api/v2/dashboard/bulk/preview", params=sel)).status_code == 403
        assert (await client.post("/api/v2/dashboard/bulk/batches", json=sel, headers=H)
                ).status_code == 403
        assert (await client.get("/api/v2/dashboard/bulk/batches/1/zip", headers=SAME)
                ).status_code == 403
        # The gateway: no admin tools, no database viewer, no batches; the dashboard, yes.
        for path, status in (("/api/v2/admin/people", 403), ("/db/", 403),
                             ("/api/v2/dashboard/bulk/preview", 403),
                             ("/api/v2/dashboard/recordings", 200), ("/dashboard", 200)):
            check = await client.get("/auth/check", headers={"X-Forwarded-Uri": path})
            assert check.status_code == status, path
        # Nor may they change who has access.
        assert (await client.post("/api/v2/admin/people", headers=H,
                                  json={"email": "another@cmed.example", "role": "admin"})
                ).status_code == 403
        assert (await client.get("/api/v2/admin/people")).status_code == 403
    try:
        async with dbs.rec.acquire() as conn:
            keys = [r["object_key"] for r in await conn.fetch(
                "SELECT object_key FROM portal_downloads WHERE email = 'staff@cmed.example'")]
            another = await conn.fetchval("SELECT role FROM portal_people "
                                          "WHERE email = 'another@cmed.example'")
        assert len(keys) == 1 and keys[0].endswith(".json.enc")
        assert another is None
    finally:
        _unserve()


async def test_a_researcher_downloads_only_the_hospitals_assigned_to_them(dbs, monkeypatch):
    sid, record = await _with_record_copy(dbs, "PRES049")
    day = date(2026, 8, 4)
    await _bulk_recording(dbs, "PRES049B", "DRRES", day, 6)
    sel = {"hospital_id": "HOSP003", "from": day.isoformat(), "to": day.isoformat()}
    who = {"value": ""}
    async with _bulk_app(dbs, monkeypatch, who, _worker(record)) as client:
        # Assigned another hospital only: sees HOSP003, downloads nothing of it.
        await _researcher(client, who, "elsewhere@uni.example", hospitals=("HOSP777",))
        assert (await client.get("/auth/me")).json()["download_hospitals"] == ["HOSP777"]
        for kind in ("audio", "record"):
            refused = await client.get(f"/api/v2/dashboard/recordings/{sid}/{kind}",
                                       headers=SAME)
            assert refused.status_code == 403, kind
            assert refused.json()["detail"].startswith("Not your hospital: HOSP003")
        assert (await client.get("/api/v2/dashboard/bulk/preview", params=sel)).status_code == 403
        assert (await client.post("/api/v2/dashboard/bulk/batches", json=sel, headers=H)
                ).status_code == 403
        every = await client.get("/api/v2/dashboard/bulk/preview",
                                 params={k: v for k, v in sel.items() if k != "hospital_id"})
        assert every.status_code == 403 and "HOSP777" in every.json()["detail"]
        # They still see every hospital's recordings.
        assert (await client.get("/auth/check", headers={
            "X-Forwarded-Uri": "/api/v2/dashboard/recordings"})).status_code == 200

        # No hospital at all: nothing to download.
        client.cookies.clear()
        await _researcher(client, who, "unassigned@uni.example", hospitals=())
        none = await client.get(f"/api/v2/dashboard/recordings/{sid}/audio", headers=SAME)
        assert none.status_code == 403 and "no hospital is assigned" in none.json()["detail"]

        # Their own hospital: audio, record and a batch.
        client.cookies.clear()
        await _researcher(client, who, "own@uni.example", hospitals=("HOSP003",))
        audio = await client.get(f"/api/v2/dashboard/recordings/{sid}/audio", headers=SAME)
        assert audio.status_code == 200 and audio.content == _DL_WAV
        got = await client.get(f"/api/v2/dashboard/recordings/{sid}/record", headers=SAME)
        assert got.status_code == 200 and got.content == record
        preview = await client.get("/api/v2/dashboard/bulk/preview", params=sel)
        assert preview.status_code == 200 and preview.json()["total"] >= 1
        batch = await client.post("/api/v2/dashboard/bulk/batches", json=sel, headers=H)
        assert batch.status_code == 200, batch.text

        # The hospital taken away later: the batch already made is refused too.
        import portal
        async with portal._pool().acquire() as conn:
            await conn.execute("UPDATE researcher_hospitals SET removed_at = now() "
                               "WHERE email = 'own@uni.example'")
        check = (await client.get(f"/api/v2/dashboard/bulk/batches/{batch.json()['id']}/check")
                 ).json()
        assert check["ok"] is False and check["status"] == 403
    _unserve()
