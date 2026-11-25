"""
3.9.9 (26 Nov 2026): a field team tests a new PC on beta, then reinstalls it
for the real server. Uninstall leaves ProgramData, so the PC still holds
beta's login; the real server does not know it. The PC puts that login aside
and joins the real server by itself. A PC whose old login the new server does
accept (the test PCs moved to beta, a clone of the real server) keeps it.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from core import enrollment
from core.enrollment import DeviceIdentity

REAL = "https://aimscribe.iriic.uiu.ac.bd"
BETA = "https://aimscribe.iriic.uiu.ac.bd:9443"


class Key:
    def fingerprint(self):
        return "fp-1"


def setup(tmp_path, *, joined_at, set_to):
    cfg = SimpleNamespace(
        paths=SimpleNamespace(state_dir=tmp_path),
        backend=SimpleNamespace(base_url=set_to, request_timeout=5,
                                url=lambda p: set_to + "/api/v2" + p),
        security=SimpleNamespace(allow_plaintext_keystore=True))
    identity = DeviceIdentity(device_id="D1", hospital_id="AALO_KARAIL", doctor_id="",
                              enrolled_at="2026-11-26T09:00:00Z", backend_url=joined_at,
                              key_fingerprint="fp-1")
    enrollment.identity_path(cfg).write_text(identity.to_json(), encoding="utf-8")
    enrollment.store_device_token(cfg, f"token-from-{joined_at}")
    return cfg


def server_says(monkeypatch, unknown: bool):
    async def answer(cfg, ssl_context=None):
        return unknown
    monkeypatch.setattr(enrollment, "login_unknown_here", answer)


async def test_a_beta_login_is_put_aside_and_the_pc_joins_the_real_server(tmp_path, monkeypatch):
    cfg = setup(tmp_path, joined_at=BETA, set_to=REAL)
    server_says(monkeypatch, unknown=True)
    assert await enrollment.ensure_enrolled(cfg, Key()) is None      # joins at its next patient
    assert not enrollment.identity_path(cfg).exists()
    assert enrollment.load_device_token(cfg) is None
    kept = tmp_path / "servers" / "aimscribe.iriic.uiu.ac.bd_9443"
    assert (kept / "device.json").is_file() and (kept / "device.token").is_file()


async def test_going_back_to_beta_brings_its_login_back(tmp_path, monkeypatch):
    cfg = setup(tmp_path, joined_at=BETA, set_to=REAL)
    server_says(monkeypatch, unknown=True)
    await enrollment.ensure_enrolled(cfg, Key())
    # It joined the real server; later it is pointed back at beta.
    real = DeviceIdentity(device_id="D1", hospital_id="AALO_KARAIL", doctor_id="",
                          enrolled_at="", backend_url=REAL, key_fingerprint="fp-1")
    enrollment.identity_path(cfg).write_text(real.to_json(), encoding="utf-8")
    enrollment.store_device_token(cfg, "token-from-real")
    cfg.backend.base_url = BETA
    back = await enrollment.ensure_enrolled(cfg, Key())
    assert back is not None and back.backend_url == BETA
    assert enrollment.load_device_token(cfg) == f"token-from-{BETA}"
    assert (tmp_path / "servers" / "aimscribe.iriic.uiu.ac.bd_443" / "device.json").is_file()


async def test_a_login_the_new_server_accepts_is_kept(tmp_path, monkeypatch):
    cfg = setup(tmp_path, joined_at=REAL, set_to=BETA)
    server_says(monkeypatch, unknown=False)
    kept = await enrollment.ensure_enrolled(cfg, Key())
    assert kept is not None and kept.device_id == "D1"
    assert enrollment.load_device_token(cfg) == f"token-from-{REAL}"


async def test_the_same_server_is_never_asked(tmp_path, monkeypatch):
    cfg = setup(tmp_path, joined_at=REAL, set_to=REAL + "/")
    async def never(cfg, ssl_context=None):
        raise AssertionError("asked")
    monkeypatch.setattr(enrollment, "login_unknown_here", never)
    assert (await enrollment.ensure_enrolled(cfg, Key())).device_id == "D1"
