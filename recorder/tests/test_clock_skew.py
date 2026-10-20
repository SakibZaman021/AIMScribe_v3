"""
A laptop clock that is wrong no longer stops a PC from uploading.

Found in the failure review (U1): grants are stamped with the server's clock and
were checked against the laptop's with five seconds of leeway, so a laptop six
seconds slow saw every grant as issued in the future, and one five minutes fast
saw every grant as expired. Nothing from that PC ever reached the server.
"""
from __future__ import annotations

import time
from email.utils import formatdate
from types import SimpleNamespace

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from core.crypto import Grant, GrantError, GrantGuard, verify_grant
from core.uploader import UploadManager


@pytest.fixture
def keys():
    private = Ed25519PrivateKey.generate()
    pem = private.private_bytes(serialization.Encoding.PEM,
                                serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption())
    return pem, private.public_key()


def server_grant(pem, server_time, n=0):
    claims = {"iss": "aimslab", "aud": "aimscribe-recorder", "sub": "DR1",
              "hospital_id": "H1", "patient_ref": "P1", "iat": int(server_time),
              "exp": int(server_time) + 60, "jti": f"jti-{n}-{server_time}"}
    return jwt.encode(claims, pem, algorithm="EdDSA")


def uploader():
    cfg = SimpleNamespace(
        security=SimpleNamespace(grant_issuer="aimslab", grant_audience="aimscribe-recorder"),
        backend=SimpleNamespace(url=lambda e: e, request_timeout=5, retry_backoff=(0.0,)),
        spool=SimpleNamespace(purge_grace_hours=0), app_version="3", protocol_version=2)
    return UploadManager(cfg, device_key=None, spool=None, receipt_public_key=None)


def check(token, public, now):
    return verify_grant(token, public, issuer="aimslab", audience="aimscribe-recorder",
                        now=now)


@pytest.mark.parametrize("pc_off_by", [-6, -300, +300, +3600])
def test_a_wrong_pc_clock_still_accepts_the_servers_grant(keys, pc_off_by):
    pem, public = keys
    server = time.time()
    pc = server + pc_off_by                       # what this laptop's clock says
    token = server_grant(pem, server)

    with pytest.raises(GrantError):               # the old behaviour: the PC's clock
        check(token, public, now=pc)

    up = uploader()                               # one reply from the server
    up.note_server_date(formatdate(server, usegmt=True), sent=pc - 0.05, received=pc + 0.05)
    assert abs(up.clock_offset - (-pc_off_by)) < 1.5
    learned = pc + up.clock_offset                # = up.server_now() on that laptop
    assert check(token, public, now=learned).patient_ref == "P1"


def test_expired_and_future_grants_are_still_refused_on_the_servers_clock(keys):
    pem, public = keys
    server = time.time()
    with pytest.raises(GrantError, match="Expired"):
        check(server_grant(pem, server - 120), public, now=server)
    with pytest.raises(GrantError, match="Immature"):
        check(server_grant(pem, server + 120), public, now=server)


def test_one_slow_reply_does_not_swing_the_offset():
    up = uploader()
    base = time.time()
    for _ in range(4):
        up.note_server_date(formatdate(base + 300, usegmt=True), base, base)
    up.note_server_date(formatdate(base + 9000, usegmt=True), base, base)   # an outlier
    assert 299 <= up.clock_offset <= 301
    up.note_server_date("not a date", base, base)                            # ignored
    up.note_server_date(None, base, base)
    assert 299 <= up.clock_offset <= 301


def test_no_reply_yet_means_the_pcs_own_clock():
    up = uploader()
    assert up.clock_offset is None
    assert abs(up.server_now() - time.time()) < 0.5


def test_a_used_grant_is_refused_again_even_on_a_fast_clock():
    # A laptop running fast sees the grant as long expired; it used to forget it
    # at once and take it a second time.
    guard = GrantGuard()
    grant = Grant(jti="j1", doctor_id="DR1", doctor_name="", hospital_id="H1",
                  patient_ref="P1", consent_obtained=True, consent_method="reception",
                  expires_at=int(time.time()) - 3000, raw="")
    guard.consume(grant)
    with pytest.raises(GrantError, match="already been used"):
        guard.consume(grant)


async def test_the_heartbeat_reports_the_offset(monkeypatch, spool, device_key):
    from tests.fakes import ScriptedUploader, make_controller, trigger
    from core.uploader import AuthOutcome

    class Measured(ScriptedUploader):
        clock_offset = -7.26

    ctl = make_controller(monkeypatch, spool, device_key,
                          Measured([AuthOutcome("granted", "", "")]))
    ctl.cfg.app_version, ctl.cfg.protocol_version = "3.4.0", 2
    assert ctl.heartbeat_payload()["clock_offset_seconds"] == -7.3
    await ctl.close()
