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


# ---- a beta recording still on the PC when it is reinstalled for the real server ----

from tests.test_authorisation import open_spooled, reload, seal, uploader_for  # noqa: E402


def made_for(spool, device_key, server):
    session = spool.open_session(
        device_key=device_key, device_id="DEV1", doctor_id="DR0042", hospital_id="HOSP003",
        patient_ref="P0012345", consent_method="reception",
        audio={"sample_rate": 44100, "channels": 1, "sample_width": 2},
        trigger={"patient_id": "P0012345", "doctor_id": "DR0042", "hospital_id": "HOSP003",
                 "start_time": "2026-11-26T10:00:00+06:00", "date": "2026-11-26"},
        server=server)
    seal(session)
    return session


def set_to(uploader, url):
    uploader.cfg.backend.base_url = url


async def test_a_beta_recording_is_never_sent_to_the_real_server(spool, device_key):
    test = made_for(spool, device_key, BETA)
    assert reload(spool, device_key, test).meta["server"] == BETA    # survives a restart
    uploader = uploader_for(spool, device_key, responses=[(200, {})])
    set_to(uploader, REAL)
    await uploader.track(test)
    await uploader._drain_once()
    assert uploader.calls == []                                      # nothing asked, nothing sent
    assert uploader.delivery_report()["recordings"] == []            # nor reported
    assert test.directory.exists()                                   # and kept on the PC


async def test_the_servers_own_recordings_and_older_ones_still_go(spool, device_key):
    real = made_for(spool, device_key, REAL + "/")
    older = open_spooled(spool, device_key)                          # before 3.9.9: no server
    seal(older)
    uploader = uploader_for(spool, device_key, responses=[(503, {})])
    set_to(uploader, REAL)
    assert not uploader.made_for_another_server(real)
    assert not uploader.made_for_another_server(older)
    await uploader.track(real)
    await uploader._drain_once()
    assert uploader.calls                                            # it tried to send


async def test_each_new_recording_names_its_server(monkeypatch, spool, device_key):
    from api.protocol import Trigger
    from tests.test_markers import at, ctl_with
    ctl = ctl_with(monkeypatch, spool, device_key)
    ctl.cfg.backend = SimpleNamespace(base_url=REAL)
    await ctl.open_session(Trigger(patient_id="P1", doctor_id="DR0042",
                                   hospital_id="CMED-DHK-BANANI-01", start_time=at(1),
                                   date="2026-09-13"))
    assert ctl._active.spool.meta["server"] == REAL
    await ctl.close()
