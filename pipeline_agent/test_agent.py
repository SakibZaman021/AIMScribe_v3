"""
The pipeline agent's handling of one job (agent.Job), against a stand-in server
and a stand-in model command.

    cd pipeline_agent && python -m pytest -q test_agent.py
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

import agent
import pipeline_seal

AUDIO = os.urandom(4000)
MODEL = Path(tempfile.gettempdir()) / "aimscribe_fake_model.py"
MODEL.write_text("""
import json, sys
args = dict(zip(sys.argv[1::2], sys.argv[2::2]))
out = args["--out"]
if "--audio" in args:
    json.dump({"language": "bn", "text": "জ্বর"}, open(out + "/transcript.json", "w", encoding="utf-8"))
    json.dump({"entities": [{"type": "symptom", "text": "জ্বর"}]}, open(out + "/ner.json", "w", encoding="utf-8"))
else:
    json.dump({"scores": {"recall": 1.0}}, open(out + "/metrics.json", "w"))
json.dump({"model": "fake", "model_version": "1"}, open(out + "/model.json", "w"))
""", encoding="utf-8")


class Response:
    def __init__(self, body=b"", payload=None):
        self.raw, self.payload = io.BytesIO(body), payload

    def json(self):
        return self.payload

    def close(self):
        pass


class FakeServer:
    def __init__(self, sealed=b"", inputs=None):
        self.sealed, self.inputs, self.calls = sealed, inputs, []

    def call(self, method, path, payload=None, **kw):
        self.calls.append((method, path, payload, kw.get("headers")))
        if path.endswith("/audio"):
            return Response(self.sealed)
        if path.endswith("/inputs"):
            return Response(payload=self.inputs)
        return Response(payload={"status": "accepted"})


def a_job(sha=None, job_id="lab_" + "a" * 32):
    return {"job_id": job_id, "ticket": "T", "rec": "rec_" + "b" * 32,
            "audio_sha256": sha or hashlib.sha256(AUDIO).hexdigest()}


def command():
    return f'"{sys.executable}" "{MODEL}"'


def test_a_lab_job_opens_checks_runs_posts_and_leaves_nothing(tmp_path):
    private, public = pipeline_seal.new_box_key()
    job = a_job()
    sealed = b"".join(pipeline_seal.seal([AUDIO], len(AUDIO), public, job["job_id"]))
    server = FakeServer(sealed)
    agent.Job(server, job, "lab", command(), tmp_path, private).run()
    posted = [(p, b) for m, p, b, h in server.calls if "/results/" in p]
    assert [p.rsplit("/", 1)[-1] for p, _ in posted] == ["transcript", "ner"]
    for _, body in posted:
        assert body["ticket"] == "T" and body["audio_sha256"] == job["audio_sha256"]
    audio_call = next(c for c in server.calls if c[1].endswith("/audio"))
    assert audio_call[3] == {"X-Pipeline-Ticket": "T"}
    assert list(tmp_path.iterdir()) == []                      # no audio, no files kept


def test_audio_that_is_not_the_archived_wav_is_never_transcribed(tmp_path):
    private, public = pipeline_seal.new_box_key()
    job = a_job(sha="0" * 64)
    sealed = b"".join(pipeline_seal.seal([AUDIO], len(AUDIO), public, job["job_id"]))
    server = FakeServer(sealed)
    agent.Job(server, job, "lab", command(), tmp_path, private).run()
    assert not any("/results/" in c[1] for c in server.calls)
    failed = [c for c in server.calls if c[1].endswith("/fail")]
    assert failed and "hash" in failed[0][2]["reason"]
    assert list(tmp_path.iterdir()) == []


def test_a_metrics_job_posts_its_metrics(tmp_path):
    server = FakeServer(inputs={"job_id": "cmp_x", "prescription": {}, "ner": {}})
    agent.Job(server, a_job(job_id="cmp_" + "c" * 32), "metrics", command(),
              tmp_path, None).run()
    posted = [c for c in server.calls if c[1].endswith("/results/metrics")]
    assert posted and posted[0][2]["data"] == {"scores": {"recall": 1.0}}


def test_keygen_never_overwrites_keys(tmp_path, capsys):
    agent.keygen(tmp_path / "keys")
    shown = capsys.readouterr().out
    assert "sign_pubkey:" in shown and "box_pubkey:" in shown
    with pytest.raises(SystemExit):
        agent.keygen(tmp_path / "keys")
