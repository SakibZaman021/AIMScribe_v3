"""
Security regressions.

Every test here corresponds to a vulnerability that existed in v1 and was
exploitable from any web page a doctor visited. If one of these ever fails,
that hole is back.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from core import crypto
from core.crypto import GrantError, ReceiptError, verify_grant, verify_purge_receipt
from api.websocket_server import GrantGuard, WebSocketManager


# ============================================================
# WebSocket handshake - v1 accepted any page on the machine
# ============================================================

def _manager(security):
    from types import SimpleNamespace
    return WebSocketManager(SimpleNamespace(security=security))


@pytest.mark.asyncio
async def test_allowed_origin_connects(make_security, fake_socket):
    manager = _manager(make_security())
    socket = fake_socket(origin="https://cmed.example")
    assert await manager.connect(socket)
    assert socket.accepted


@pytest.mark.asyncio
@pytest.mark.parametrize("origin", [
    None,                            # non-browser client, or a stripped header
    "null",                          # sandboxed iframe, file:// page
    "https://evil.example",          # arbitrary site the doctor visited
    "https://cmed.example.evil.com", # suffix confusion
    "http://cmed.example",           # wrong scheme
])
async def test_disallowed_origins_are_refused(make_security, fake_socket, origin):
    manager = _manager(make_security())
    socket = fake_socket(origin=origin)
    assert not await manager.connect(socket)
    assert not socket.accepted
    assert socket.close_code == 4403


@pytest.mark.asyncio
async def test_dns_rebinding_is_refused(make_security, fake_socket):
    """
    evil.example resolving to 127.0.0.1 looks same-origin to the browser and
    loopback to us. Only the Host header distinguishes it.
    """
    manager = _manager(make_security())
    socket = fake_socket(origin="https://cmed.example", host="evil.example:5050")
    assert not await manager.connect(socket)
    assert socket.close_code == 4403


@pytest.mark.asyncio
async def test_non_loopback_peer_is_refused(make_security, fake_socket):
    manager = _manager(make_security())
    socket = fake_socket(origin="https://cmed.example", peer="192.168.1.50")
    assert not await manager.connect(socket)


@pytest.mark.asyncio
async def test_wildcard_origin_is_not_a_match(make_security, fake_socket):
    """The allowlist is exact strings; '*' must never behave as a wildcard."""
    manager = _manager(make_security(allowed_origins=frozenset({"*"})))
    socket = fake_socket(origin="https://evil.example")
    assert not await manager.connect(socket)


# ============================================================
# Recording grants
# ============================================================

@pytest.fixture
def grant_keys():
    private = Ed25519PrivateKey.generate()
    pem = private.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return pem, private.public_key()


def _token(private_pem, **overrides):
    now = int(time.time())
    claims = {
        "iss": "cmed", "aud": "aimscribe-recorder", "sub": "DR001",
        "hospital_id": "HOSP001", "patient_ref": "P12345",
        "consent_obtained": True, "consent_method": "verbal_at_reception",
        "iat": now, "exp": now + 60, "jti": f"jti-{now}-{overrides.get('_n', 0)}",
    }
    claims.update({k: v for k, v in overrides.items() if not k.startswith("_")})
    return jwt.encode(claims, private_pem, algorithm="EdDSA")


def test_valid_grant_is_accepted(grant_keys):
    private_pem, public = grant_keys
    grant = verify_grant(_token(private_pem), public,
                         issuer="cmed", audience="aimscribe-recorder")
    assert grant.doctor_id == "DR001"
    assert grant.hospital_id == "HOSP001"
    assert grant.patient_ref == "P12345"


@pytest.mark.parametrize("overrides,reason", [
    ({"exp": int(time.time()) - 10}, "expired"),
    ({"aud": "someone-else"}, "wrong audience"),
    ({"iss": "attacker"}, "wrong issuer"),
    ({"consent_obtained": False}, "no consent"),
    ({"patient_ref": ""}, "missing patient"),
])
def test_bad_grants_are_rejected(grant_keys, overrides, reason):
    private_pem, public = grant_keys
    with pytest.raises(GrantError):
        verify_grant(_token(private_pem, **overrides), public,
                     issuer="cmed", audience="aimscribe-recorder")


@pytest.mark.parametrize("overrides", [
    {"hospital_id": ""},
    {"sub": ""},
    {"hospital_id": "", "sub": ""},
])
def test_grant_without_doctor_or_hospital_is_accepted(grant_keys, overrides):
    """
    CMED no longer knows either, so it must not be required to assert them.

    The doctor and the hospital belong to the machine's enrolment. A grant that
    omits them is normal; one that names them is not trusted on that basis
    either - the controller compares both against the enrolment and raises an
    integrity alert on a mismatch.

    The patient is the one thing only CMED knows, so it stays mandatory.
    """
    private_pem, public = grant_keys
    grant = verify_grant(_token(private_pem, **overrides), public,
                         issuer="cmed", audience="aimscribe-recorder")
    assert grant.patient_ref == "P12345"
    if "hospital_id" in overrides:
        assert grant.hospital_id == ""
    if "sub" in overrides:
        assert grant.doctor_id == ""


def test_grant_signed_by_another_key_is_rejected(grant_keys):
    _, public = grant_keys
    attacker = Ed25519PrivateKey.generate().private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    with pytest.raises(GrantError):
        verify_grant(_token(attacker), public,
                     issuer="cmed", audience="aimscribe-recorder")


def test_unsigned_alg_none_token_is_rejected(grant_keys):
    """The classic JWT bypass. Pinning algorithms=['EdDSA'] is what stops it."""
    _, public = grant_keys
    now = int(time.time())
    forged = jwt.encode(
        {"iss": "cmed", "aud": "aimscribe-recorder", "sub": "DR999",
         "hospital_id": "HOSP001", "patient_ref": "P12345",
         "consent_obtained": True, "iat": now, "exp": now + 60, "jti": "forged"},
        key="", algorithm="none",
    )
    with pytest.raises(GrantError):
        verify_grant(forged, public, issuer="cmed", audience="aimscribe-recorder")


def test_grant_is_single_use(grant_keys):
    """A captured grant must not be replayable until it expires."""
    private_pem, public = grant_keys
    guard = GrantGuard()
    grant = verify_grant(_token(private_pem), public,
                         issuer="cmed", audience="aimscribe-recorder")

    guard.consume(grant)
    with pytest.raises(GrantError):
        guard.consume(grant)


def test_missing_grant_is_rejected(grant_keys):
    _, public = grant_keys
    with pytest.raises(GrantError):
        verify_grant("", public, issuer="cmed", audience="aimscribe-recorder")


# ============================================================
# Purge receipts - the gate on deleting the only local copy
# ============================================================

@pytest.fixture
def receipt_keys():
    private = Ed25519PrivateKey.generate()
    return private, private.public_key()


def _receipt(private, **overrides):
    payload = {
        "session_id": "01J8FQ2K7XABCDEFGHJKMNPQRS",
        "scope": "segment",
        "seq_no": 1,
        "sha256": crypto.sha256_bytes(b"the audio").hex(),
        "archived_at": crypto.iso_utc(datetime.now(timezone.utc)),
    }
    payload.update(overrides)
    signature = private.sign(crypto.receipt_signing_input(payload))
    return payload, signature


def test_valid_receipt_authorises_deletion(receipt_keys):
    private, public = receipt_keys
    payload, signature = _receipt(private)
    verify_purge_receipt(
        payload, signature, public,
        expect_session_id="01J8FQ2K7XABCDEFGHJKMNPQRS",
        expect_sha256=crypto.sha256_bytes(b"the audio"),
        expect_scope="segment", expect_seq_no=1,
    )


def test_receipt_for_a_different_segment_is_rejected(receipt_keys):
    """
    A correctly signed receipt for segment 2 must not authorise deleting
    segment 1. Checking only the signature would allow exactly that.
    """
    private, public = receipt_keys
    payload, signature = _receipt(private, seq_no=2)
    with pytest.raises(ReceiptError):
        verify_purge_receipt(
            payload, signature, public,
            expect_session_id="01J8FQ2K7XABCDEFGHJKMNPQRS",
            expect_sha256=crypto.sha256_bytes(b"the audio"),
            expect_scope="segment", expect_seq_no=1,
        )


def test_receipt_with_mismatched_hash_is_rejected(receipt_keys):
    """The archived bytes must be the bytes we hold, not merely something."""
    private, public = receipt_keys
    payload, signature = _receipt(private, sha256=crypto.sha256_bytes(b"other audio").hex())
    with pytest.raises(ReceiptError):
        verify_purge_receipt(
            payload, signature, public,
            expect_session_id="01J8FQ2K7XABCDEFGHJKMNPQRS",
            expect_sha256=crypto.sha256_bytes(b"the audio"),
            expect_scope="segment", expect_seq_no=1,
        )


def test_forged_receipt_is_rejected(receipt_keys):
    _, public = receipt_keys
    attacker = Ed25519PrivateKey.generate()
    payload, signature = _receipt(attacker)
    with pytest.raises(ReceiptError):
        verify_purge_receipt(
            payload, signature, public,
            expect_session_id="01J8FQ2K7XABCDEFGHJKMNPQRS",
            expect_sha256=crypto.sha256_bytes(b"the audio"),
            expect_scope="segment", expect_seq_no=1,
        )


def test_tampered_receipt_payload_is_rejected(receipt_keys):
    private, public = receipt_keys
    payload, signature = _receipt(private)
    payload["session_id"] = "01J0000000000000000000000X"
    with pytest.raises(ReceiptError):
        verify_purge_receipt(
            payload, signature, public,
            expect_session_id="01J0000000000000000000000X",
            expect_sha256=crypto.sha256_bytes(b"the audio"),
            expect_scope="segment", expect_seq_no=1,
        )


# ============================================================
# Configuration refuses to run unsafely
# ============================================================

def test_validate_flags_wildcard_and_placeholder(make_security, tmp_path):
    from config import (AudioConfig, BackendConfig, Config, OpsConfig,
                        PathConfig, PauseConfig, SegmentConfig, SpoolConfig)

    cfg = Config(
        audio=AudioConfig(), segment=SegmentConfig(),
        spool=SpoolConfig(directory=tmp_path / "spool"),
        backend=BackendConfig(),
        security=make_security(
            allowed_origins=frozenset({"https://*.vercel.app"}),
            local_api_key="change-me-per-install",
            grant_public_key_path=tmp_path / "missing-grant.pem",
            receipt_public_key_path=tmp_path / "missing-receipt.pem",
        ),
        pause=PauseConfig(), paths=PathConfig(), ops=OpsConfig(),
    )

    problems = " ".join(cfg.validate())
    assert "wildcard" in problems
    assert "placeholder" in problems
    assert "Grant public key missing" in problems
    assert "receipt" in problems.lower()


def test_production_warnings_flag_plaintext_backend(make_security, tmp_path):
    from config import (AudioConfig, BackendConfig, Config, OpsConfig,
                        PathConfig, PauseConfig, SegmentConfig, SpoolConfig)

    cfg = Config(
        audio=AudioConfig(), segment=SegmentConfig(),
        spool=SpoolConfig(directory=tmp_path / "spool"),
        backend=BackendConfig(base_url="http://backend.local"),
        security=make_security(enable_docs=True, require_grant=False),
        pause=PauseConfig(), paths=PathConfig(), ops=OpsConfig(),
    )

    warnings = " ".join(cfg.production_warnings())
    assert "cleartext" in warnings
    assert "AIMS_ENABLE_DOCS" in warnings
    assert "AIMS_REQUIRE_GRANT" in warnings
