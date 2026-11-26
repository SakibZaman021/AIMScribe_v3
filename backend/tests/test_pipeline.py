"""
The analysis pipeline (053) on a real Postgres: jobs, signed machines, signed
tickets, and every way a result can be refused.

Requests are signed exactly as the agent signs them (pipeline_agent/agent.py),
so a change on either side that breaks the other fails here.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND / "src"))
sys.path.insert(0, str(BACKEND / "tests"))
sys.path.insert(0, str(BACKEND.parent / "pipeline_agent"))

try:
    from test_db_integration import (_server, _unserve, a_session, dbs,  # noqa: E402,F401
                                     enrolled_device, pg)
    HAVE_POSTGRES = True
except pytest.skip.Exception:
    HAVE_POSTGRES = False

import httpx  # noqa: E402
from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402
from fastapi import FastAPI  # noqa: E402

import agent  # noqa: E402
import api_v2  # noqa: E402
import pipeline  # noqa: E402
from grants import GrantIssuer  # noqa: E402

pytestmark = [pytest.mark.asyncio,
              pytest.mark.skipif(not HAVE_POSTGRES, reason="pgserver is not installed")]

AUDIO = os.urandom(5000)
AUDIO_SHA = hashlib.sha256(AUDIO).hexdigest()
RAW = (serialization.Encoding.Raw, serialization.PublicFormat.Raw)
TRANSCRIPT = {"language": "bn", "text": "রোগীর জ্বর তিন দিন",
              "segments": [{"start": 0.0, "end": 2.5, "text": "রোগীর জ্বর তিন দিন"}]}
NER = {"entities": [{"type": "symptom", "text": "জ্বর", "start": 6, "end": 10}]}
METRICS = {"scores": {"drug_recall": 0.8, "dose_match": 1.0}}


class Machine:
    """An enrolled machine and its signing, as the agent does it."""

    def __init__(self, client, machine_id: str, key: Ed25519PrivateKey):
        self.client, self.id, self.key = client, machine_id, key
        self.signer = agent.Server("http://test", machine_id, key)

    async def call(self, method, path, payload=None, *, headers=None, sign=True,
                   reuse=None):
        url = f"http://test/api/v2/pipeline{path}"
        body = b"" if payload is None else json.dumps(
            payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        signed = reuse or (self.signer._headers(method, url, body) if sign else
                           {"X-Pipeline-Machine": self.id})
        all_headers = {**signed, **(headers or {})}
        if payload is not None:
            all_headers["Content-Type"] = "application/json"
        response = await self.client.request(method, url, content=body or None,
                                             headers=all_headers)
        response.signed = signed
        return response


@pytest_asyncio.fixture
async def world(dbs, monkeypatch):
    monkeypatch.setenv("AIMS_PIPELINE", "on")
    monkeypatch.setattr(pipeline, "_last_sweep", 0.0)
    _server(dbs)
    api_v2.ctx.grants = GrantIssuer(Ed25519PrivateKey.generate())
    app = FastAPI()
    app.include_router(pipeline.router)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")

    async def enrol(role, *, ips=None):
        key = Ed25519PrivateKey.generate()
        box = os.urandom(32).hex() if role == "lab" else None
        answer = await pipeline.enrol_machine(pipeline.EnrolRequest(
            name=f"{role}-{uuid.uuid4().hex[:8]}", role=role,
            sign_pubkey=key.public_key().public_bytes(*RAW).hex(), box_pubkey=box,
            allowed_ips=ips, by="tests"), _=None)
        return Machine(client, answer["machine_id"], key)

    async def recording(confirmed=True):
        device = await enrolled_device(dbs.repo)
        sid = await a_session(dbs.repo, device)
        stem = f"P{uuid.uuid4().hex[:6]}_DR0042_HOSP003_101432_102847_20260913"
        key = f"HOSP003/DR0042/2026-09-13/{stem}_visit-x/{stem}.v1.wav.enc"
        async with dbs.rec.acquire() as conn:
            await conn.execute("UPDATE sessions SET confirmation = $2, file_stem = $3 "
                               "WHERE session_id = $1", sid,
                               "confirmed" if confirmed else "unconfirmed", stem)
            await conn.execute("""
                INSERT INTO cloud_copies (session_id, kind, object_key, version, bytes,
                                          sha256, plain_sha256)
                VALUES ($1, 'audio', $2, 1, 6000, $3, $4)
            """, sid, key, os.urandom(32), bytes.fromhex(AUDIO_SHA))
        return sid, key

    yield SimpleNamespace(client=client, enrol=enrol, recording=recording, dbs=dbs)
    await client.aclose()
    api_v2.ctx.grants = None
    _unserve()


async def _claimed(world, lab):
    sid, key = await world.recording()
    await pipeline.sweep(force=True)
    answer = await lab.call("POST", "/claim", {"max": 1, "wait": 0})
    assert answer.status_code == 200, answer.text
    jobs = [j for j in answer.json()["jobs"]]
    assert jobs
    return sid, key, jobs[0]


def _result(job, data, **more):
    return {"ticket": job["ticket"], "audio_sha256": AUDIO_SHA, "model": "rahat-asr",
            "model_version": "1.0", "data": data, **more}


async def test_only_confirmed_copies_are_queued_and_each_once(world):
    lab = await world.enrol("lab")
    sid, _ = await world.recording()
    unconfirmed, _ = await world.recording(confirmed=False)
    await pipeline.sweep(force=True)
    await pipeline.sweep(force=True)
    async with world.dbs.rec.acquire() as conn:
        queued = {r["session_id"]: r["n"] for r in await conn.fetch(
            "SELECT session_id, count(*) n FROM pipeline_jobs WHERE kind = 'lab' "
            "GROUP BY 1")}
    assert queued.get(sid) == 1 and unconfirmed not in queued
    assert lab.id


async def test_a_machine_must_sign_every_request(world):
    lab = await world.enrol("lab")
    assert (await lab.call("POST", "/claim", {"max": 1, "wait": 0},
                           sign=False)).status_code == 401
    forger = Machine(world.client, lab.id, Ed25519PrivateKey.generate())
    assert (await forger.call("POST", "/claim", {"max": 1, "wait": 0})).status_code == 401
    first = await lab.call("POST", "/claim", {"max": 1, "wait": 0})
    assert first.status_code == 200
    replay = await lab.call("POST", "/claim", {"max": 1, "wait": 0}, reuse=first.signed)
    assert replay.status_code == 401 and "nonce" in replay.text


async def test_a_revoked_machine_or_another_address_is_refused(world):
    lab = await world.enrol("lab")
    walled = await world.enrol("lab", ips=["10.9.9.9"])
    assert (await walled.call("POST", "/claim", {"max": 1, "wait": 0})).status_code == 403
    await pipeline.revoke_machine(lab.id, pipeline.RevokeRequest(by="tests"), _=None)
    assert (await lab.call("POST", "/claim", {"max": 1, "wait": 0})).status_code == 403


async def test_a_job_goes_from_audio_to_metrics(world):
    lab, metrics = await world.enrol("lab"), await world.enrol("metrics")
    sid, key, job = await _claimed(world, lab)
    assert job["audio_sha256"] == AUDIO_SHA and job["steps"] == ["transcript", "ner"]
    ok = await lab.call("POST", f"/jobs/{job['job_id']}/results/transcript",
                        _result(job, TRANSCRIPT))
    assert ok.status_code == 200, ok.text
    again = await lab.call("POST", f"/jobs/{job['job_id']}/results/transcript",
                           _result(job, TRANSCRIPT))
    assert again.json()["status"] == "already accepted"
    ner = await lab.call("POST", f"/jobs/{job['job_id']}/results/ner", _result(job, NER))
    assert ner.status_code == 200, ner.text

    claimed = await metrics.call("POST", "/claim", {"max": 1, "wait": 0})
    compare = claimed.json()["jobs"][0]
    assert compare["rec"] == job["rec"] and compare["steps"] == ["metrics"]
    inputs = await metrics.call("GET", f"/jobs/{compare['job_id']}/inputs",
                                headers={"X-Pipeline-Ticket": compare["ticket"]})
    assert inputs.status_code == 200 and inputs.json()["ner"] == NER
    posted = await metrics.call("POST", f"/jobs/{compare['job_id']}/results/metrics",
                                {"ticket": compare["ticket"], "model": "dibbo",
                                 "model_version": "0.1", "data": METRICS})
    assert posted.status_code == 200, posted.text
    feed = await metrics.call("GET", "/metrics")
    assert any(r["rec"] == job["rec"] and r["metrics"] == METRICS
               for r in feed.json()["results"])
    text = await metrics.call("GET", f"/recordings/{job['rec']}/transcript")
    assert text.json()["transcript"]["text"] == TRANSCRIPT["text"]

    pending = await pipeline.results_pending(limit=50, _=None)
    mine = [r for r in pending["sessions"] if r["job_id"] in (job["job_id"],
                                                               compare["job_id"])]
    assert {r["kind"] for r in mine} == {"transcript", "ner", "metrics"}
    for r in mine:
        assert hashlib.sha256(r["body"].encode("utf-8")).hexdigest() == r["body_sha256"]
    place = await pipeline.results_authorize(pipeline.ResultPlaceRequest(
        job_id=job["job_id"], kind="transcript"), _=None)
    assert place["object_key"] == key.replace(".v1.wav.enc", ".v1.transcript.json.enc")


async def test_every_wrong_result_is_refused(world):
    lab, metrics = await world.enrol("lab"), await world.enrol("metrics")
    _, _, job = await _claimed(world, lab)
    path = f"/jobs/{job['job_id']}/results"
    other = GrantIssuer(Ed25519PrivateKey.generate())
    real = api_v2.ctx.grants
    api_v2.ctx.grants = other
    forged = pipeline.issue_ticket({**job, "job_id": job["job_id"], "kind": "lab",
                                    "audio_sha256": bytes.fromhex(AUDIO_SHA)},
                                   {"machine_id": uuid.UUID(lab.id)})
    api_v2.ctx.grants = real
    cases = [
        ("forged ticket", f"{path}/transcript", _result({**job, "ticket": forged},
                                                        TRANSCRIPT), 403),
        ("wrong audio", f"{path}/transcript", _result(job, TRANSCRIPT,
                                                      audio_sha256="0" * 64), 409),
        ("NER first", f"{path}/ner", _result(job, NER), 409),
        ("bad format", f"{path}/transcript", _result(job, {"text": 5}), 422),
        ("unknown kind", f"{path}/notes", _result(job, {}), 404),
    ]
    for why, url, payload, status in cases:
        answer = await lab.call("POST", url, payload)
        assert answer.status_code == status, (why, answer.status_code, answer.text)
    stolen = await metrics.call("POST", f"{path}/transcript", _result(job, TRANSCRIPT))
    assert stolen.status_code == 403
    assert (await lab.call("POST", f"{path}/transcript",
                           _result(job, TRANSCRIPT))).status_code == 200
    changed = await lab.call("POST", f"{path}/transcript",
                             _result(job, {**TRANSCRIPT, "text": "অন্য"}))
    assert changed.status_code == 409


async def test_a_ticket_for_another_job_opens_nothing(world):
    lab = await world.enrol("lab")
    _, _, first = await _claimed(world, lab)
    _, _, second = await _claimed(world, lab)
    swapped = await lab.call("POST", f"/jobs/{second['job_id']}/results/transcript",
                             _result(first, TRANSCRIPT))
    assert swapped.status_code == 403


async def test_failures_back_off_then_park(world):
    lab = await world.enrol("lab")
    _, _, job = await _claimed(world, lab)
    failed = await lab.call("POST", f"/jobs/{job['job_id']}/fail",
                            {"ticket": job["ticket"], "reason": "GPU out of memory"})
    assert failed.json() == {"job_id": job["job_id"], "state": "ready",
                             "next_try_in_seconds": 300}
    async with world.dbs.rec.acquire() as conn:
        await conn.execute("UPDATE pipeline_jobs SET attempts = $2, state = 'leased', "
                           "machine_id = $3, lease_until = now() + interval '1 hour' "
                           "WHERE job_id = $1", job["job_id"], pipeline.MAX_ATTEMPTS,
                           uuid.UUID(lab.id))
    parked = await lab.call("POST", f"/jobs/{job['job_id']}/fail",
                            {"ticket": job["ticket"], "reason": "still failing"})
    assert parked.json()["state"] == "parked"


async def test_a_lease_that_runs_out_returns_the_job(world):
    lab = await world.enrol("lab")
    _, _, job = await _claimed(world, lab)
    async with world.dbs.rec.acquire() as conn:
        await conn.execute("UPDATE pipeline_jobs SET lease_until = now() - interval "
                           "'1 minute' WHERE job_id = $1", job["job_id"])
    await pipeline.sweep(force=True)
    late = await lab.call("POST", f"/jobs/{job['job_id']}/results/transcript",
                          _result(job, TRANSCRIPT))
    assert late.status_code == 409


async def test_switched_off_it_answers_nothing(world, monkeypatch):
    lab = await world.enrol("lab")
    monkeypatch.setenv("AIMS_PIPELINE", "off")
    assert (await lab.call("POST", "/claim", {"max": 1, "wait": 0})).status_code == 404


async def test_the_lab_receives_the_archived_wav_sealed_to_itself(world, tmp_path, monkeypatch):
    """Store -> archive worker (/seal) -> API -> agent: opens, and is the archived WAV."""
    import io
    import threading
    sys.path.insert(0, str(BACKEND / "archive_worker"))
    import cloudcopy
    import pipeline_seal
    import serve
    from test_serve import Store

    copy_key = os.urandom(32)
    store = Store()
    worker = serve.DecryptServer(("127.0.0.1", 0), key=copy_key, worker_key="wk",
                                 concurrency=2, allow_http=True)
    threading.Thread(target=worker.serve_forever, daemon=True).start()
    try:
        box_private, box_public = pipeline_seal.new_box_key()
        key = Ed25519PrivateKey.generate()
        enrolled = await pipeline.enrol_machine(pipeline.EnrolRequest(
            name=f"gpu-{uuid.uuid4().hex[:6]}", role="lab",
            sign_pubkey=key.public_key().public_bytes(*RAW).hex(),
            box_pubkey=box_public.hex(), by="tests"), _=None)
        lab = Machine(world.client, enrolled["machine_id"], key)
        sid, object_key = await world.recording()
        stem = pipeline._stem(object_key)
        source = tmp_path / "a.wav"
        source.write_bytes(AUDIO)
        sealed = cloudcopy.encrypt_file(source, tmp_path / "a.wav.enc", copy_key,
                                        cloudcopy.binding(sid, "audio", stem=stem))
        url = store.put("object", sealed.path.read_bytes())
        api_v2.ctx.copy = SimpleNamespace(
            bucket="copies", get_presigned_download_url=lambda k, expires: url)
        monkeypatch.setenv("AIMS_DECRYPT_URL", f"http://127.0.0.1:{worker.server_address[1]}")
        monkeypatch.setenv("AIMS_WORKER_KEY", "wk")
        await pipeline.sweep(force=True)
        async with world.dbs.rec.acquire() as conn:        # other tests' jobs aside
            await conn.execute("UPDATE pipeline_jobs SET state = 'parked' WHERE kind = 'lab' "
                               "AND state = 'ready' AND session_id <> $1", sid)
        job = (await lab.call("POST", "/claim", {"max": 1, "wait": 0})).json()["jobs"][0]

        refused = await lab.call("GET", f"/jobs/{job['job_id']}/audio")
        assert refused.status_code == 403                       # no ticket, no audio
        answer = await lab.call("GET", f"/jobs/{job['job_id']}/audio",
                                headers={"X-Pipeline-Ticket": job["ticket"]})
        assert answer.status_code == 200, answer.text
        size, chunks = pipeline_seal.open_sealed(io.BytesIO(answer.content).read,
                                                 box_private, job["job_id"])
        opened = b"".join(chunks)
        assert opened == AUDIO and hashlib.sha256(opened).hexdigest() == job["audio_sha256"]
        with pytest.raises(pipeline_seal.SealError):            # nobody else opens it
            n, other = pipeline_seal.open_sealed(io.BytesIO(answer.content).read,
                                                 pipeline_seal.new_box_key()[0], job["job_id"])
            b"".join(other)
    finally:
        worker.shutdown()
        store.http.shutdown()
