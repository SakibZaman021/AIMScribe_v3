"""
/health tells the truth about object storage.

It used to answer "minio": "connected" without asking, so a server holding the
wrong R2 keys reported healthy while every upload failed. These check that the
answer comes from the bucket, that a failure makes the server "degraded", and
that the answer is cached so /health stays cheap.

    cd backend && python -m pytest -q tests/test_health_storage.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

pytest.importorskip("pydantic_settings")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import api_v2                     # noqa: E402
import main_fastapi as app_module  # noqa: E402


class Healthy:
    async def healthcheck(self):
        return True


class Bucket:
    def __init__(self, behaviour):
        self.bucket = "aimscribe-segments"
        self.calls = 0
        outer = self

        class Client:
            def bucket_exists(self, name):
                outer.calls += 1
                if behaviour == "raise":
                    raise RuntimeError("SignatureDoesNotMatch")
                if behaviour == "slow":
                    import time
                    time.sleep(2)
                return behaviour == "ok"

        self.client = Client()


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    monkeypatch.setattr(app_module.ctx, "db", Healthy())
    monkeypatch.setattr(app_module.ctx, "redis", Healthy())
    monkeypatch.setattr(api_v2.ctx, "copy", None, raising=False)
    # Migrations are not what these tests are about (test_schema_check is).

    async def schema_ok():
        return "ok"

    monkeypatch.setattr(app_module, "_schema_state", schema_ok)
    app_module._storage_cache.update(at=0.0, answer=None)
    yield
    app_module._storage_cache.update(at=0.0, answer=None)


def health():
    return asyncio.run(app_module._details())


def test_a_reachable_bucket_is_healthy(monkeypatch):
    monkeypatch.setattr(app_module.ctx, "minio", Bucket("ok"))
    answer = health()
    assert (answer["status"], answer["minio"], answer["copy_bucket"]) == \
        ("healthy", "connected", "not configured")
    assert answer["system"].startswith("AIMScribe v3")


def test_wrong_keys_are_degraded_not_connected(monkeypatch):
    monkeypatch.setattr(app_module.ctx, "minio", Bucket("raise"))
    answer = health()
    assert (answer["status"], answer["minio"]) == ("degraded", "refused")


def test_a_missing_bucket_is_degraded(monkeypatch):
    monkeypatch.setattr(app_module.ctx, "minio", Bucket("missing"))
    assert health()["minio"] == "bucket missing"


def test_a_slow_store_does_not_hang_health(monkeypatch):
    monkeypatch.setattr(app_module, "STORAGE_CHECK_TIMEOUT", 0.2)
    monkeypatch.setattr(app_module.ctx, "minio", Bucket("slow"))
    assert health()["minio"] == "unreachable"


def test_a_failing_copy_bucket_is_degraded(monkeypatch):
    monkeypatch.setattr(app_module.ctx, "minio", Bucket("ok"))
    monkeypatch.setattr(api_v2.ctx, "copy", Bucket("raise"))
    answer = health()
    assert (answer["status"], answer["copy_bucket"]) == ("degraded", "refused")


def test_the_answer_is_reused_for_thirty_seconds(monkeypatch):
    bucket = Bucket("ok")
    monkeypatch.setattr(app_module.ctx, "minio", bucket)
    for _ in range(5):
        health()
    assert bucket.calls == 1


def _copy_store_with_the_pieces_bucket(monkeypatch, environment_name):
    monkeypatch.setattr(app_module, "MinIOClient", lambda **kw: ("client", kw["bucket"]))
    monkeypatch.setenv("AIMS_COPY_BUCKET", app_module.settings.minio_bucket)
    monkeypatch.setenv("AIMS_ENVIRONMENT", environment_name)
    return app_module.AsyncAppContext._copy_bucket()


def test_the_real_server_never_keeps_copies_with_the_pieces(monkeypatch):
    """SRS-ARC-12: the real recordings' copies live in their own locked store."""
    assert _copy_store_with_the_pieces_bucket(monkeypatch, "") is None


def test_beta_keeps_its_copies_in_its_one_bucket(monkeypatch):
    """26 Nov 2026: beta's test recordings, pieces and copies, all in aimscribe-beta."""
    made = _copy_store_with_the_pieces_bucket(monkeypatch, "beta")
    assert made == ("client", app_module.settings.minio_bucket)
