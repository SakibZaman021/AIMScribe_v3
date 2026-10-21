"""
PCs whose identity key lives in their TPM chip (recorder 3.6 on).

A TPM signs ECDSA P-256, so the server accepts two kinds of device key, told
apart by length: 32 bytes Ed25519 (software, up to 3.5) and 65 bytes P-256
(0x04 X Y, inside the chip). Everything a PC signs - chain entries, requests,
refusals, its enrolment - must verify under either. A P-256 PC proves at
enrolment that it holds the key, and signs from its first request.

The chip itself is exercised at the end, on a PC that has one.
"""
from __future__ import annotations

import base64
import sys
from datetime import timedelta
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from fastapi import HTTPException

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND / "src"))
sys.path.insert(0, str(BACKEND / "tests"))
sys.path.insert(0, str(BACKEND.parent / "recorder"))

import api_v2                                   # noqa: E402
import integrity                                # noqa: E402
from core import crypto as agent                # noqa: E402

from test_data_safety import commit, receipts_for, segment_entry, wav  # noqa: E402
from test_reassign import device_key, started   # noqa: E402,F401
from test_v3_server import DEVICE, server       # noqa: E402,F401

asyncio_mark = pytest.mark.asyncio


class P256Key:
    """What the TPM key does, in software: raw r || s signatures, 65-byte point."""
    kind = "p256-tpm"

    def __init__(self):
        self._key = ec.generate_private_key(ec.SECP256R1())

    def sign(self, message: bytes) -> bytes:
        r, s = decode_dss_signature(self._key.sign(message, ec.ECDSA(hashes.SHA256())))
        return r.to_bytes(32, "big") + s.to_bytes(32, "big")

    def public_bytes_raw(self) -> bytes:
        return self._key.public_key().public_bytes(Encoding.X962,
                                                   PublicFormat.UncompressedPoint)

    def public_key(self):
        return self._key.public_key()

    def fingerprint(self) -> str:
        return agent.sha256_bytes(self.public_bytes_raw()).hex()[:16]


@pytest.fixture
def chip():
    return P256Key()


@pytest.fixture
def chip_device(chip):
    return {**DEVICE, "tpm_pubkey": chip.public_bytes_raw()}


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


# ============================================================
# Every signature a PC makes verifies under a P-256 key
# ============================================================

@asyncio_mark
async def test_a_tpm_pc_records_like_any_other(server, chip, chip_device, monkeypatch):
    sid = started(server, chip)
    piece = wav(1.0)
    reply = await commit(sid, segment_entry(server, sid, chip, audio=piece), chip_device,
                         audio=piece, monkeypatch=monkeypatch)
    assert reply["status"] == "committed"
    assert len(receipts_for(server, sid)) == 1


@asyncio_mark
async def test_an_entry_signed_by_another_chip_is_refused(server, chip, chip_device,
                                                         monkeypatch):
    sid = started(server, chip)
    piece = wav(1.0)
    reply = await commit(sid, segment_entry(server, sid, P256Key(), audio=piece),
                         chip_device, audio=piece, monkeypatch=monkeypatch)
    assert reply["status"] == "quarantined"


def test_request_signatures_and_both_key_kinds(chip, device_key):
    message = integrity.device_request_message(
        method="POST", path=b"/api/v2/heartbeat", timestamp="1760000000",
        nonce="n" * 16, body=b"{}")
    for key in (chip, device_key):
        pub = key.public_bytes_raw()
        assert integrity.device_key_problem(pub) is None
        assert integrity.verify_device_signature(pub, message, key.sign(message))
        assert not integrity.verify_device_signature(pub, message + b"x", key.sign(message))
    # One kind's signature never passes as the other's.
    assert not integrity.verify_device_signature(chip.public_bytes_raw(), message,
                                                 device_key.sign(message))


@pytest.mark.parametrize("bad", [b"\x04" * 33, b"\x02" + b"\x01" * 64, b"\x04" + b"\x00" * 64,
                                 b"\x01" * 31])
def test_a_malformed_key_is_not_a_key(bad):
    assert integrity.device_key_problem(bad) is not None
    assert not integrity.verify_by_device_key(bad, b"\x00" * 64, b"m")


def test_the_proof_is_the_same_message_on_both_sides(chip):
    token, pub = "a-token-of-sixteen-plus", chip.public_bytes_raw()
    assert agent.enrolment_proof_message(token, pub) == \
        integrity.enrolment_proof_message(token, pub)


# ============================================================
# Enrolment: a TPM PC proves it holds its key, and signs from the start
# ============================================================

@pytest.fixture
def enrolling(server, monkeypatch):
    calls = []

    async def enroll_device(**kw):
        calls.append(kw)
        return {"device_id": "dev-new", "hospital_id": "HOSP003", "doctor_id": "",
                "device_token": "t" * 43}

    signed = []

    async def mark_device_signs(device_id):
        signed.append(device_id)

    monkeypatch.setattr(server.repo, "enroll_device", enroll_device, raising=False)
    monkeypatch.setattr(server.repo, "mark_device_signs", mark_device_signs)
    return calls, signed


TOKEN = "enrol-" + "x" * 30


def enrol_body(key, *, proof=None, token=TOKEN):
    pub = key.public_bytes_raw()
    if proof is None:
        proof = b64(key.sign(integrity.enrolment_proof_message(token, pub)))
    return api_v2.EnrollRequest(enrollment_token=token, device_pubkey=pub.hex(),
                                key_proof=proof or None, key_storage="tpm")


@asyncio_mark
async def test_a_tpm_pc_enrols_and_must_sign_from_its_first_request(server, chip, enrolling):
    calls, signed = enrolling
    reply = await api_v2.enroll_device(enrol_body(chip))
    assert reply["device_id"] == "dev-new"
    assert calls[0]["tpm_pubkey"] == chip.public_bytes_raw()
    assert signed == ["dev-new"]


@asyncio_mark
async def test_a_tpm_key_without_proof_is_refused(server, chip, enrolling):
    with pytest.raises(HTTPException) as refused:
        await api_v2.enroll_device(enrol_body(chip, proof=""))
    assert refused.value.status_code == 400
    assert enrolling[0] == []


@asyncio_mark
async def test_a_proof_for_another_token_or_key_is_refused(server, chip, enrolling):
    other = b64(P256Key().sign(integrity.enrolment_proof_message(TOKEN,
                                                                 chip.public_bytes_raw())))
    wrong_token = b64(chip.sign(integrity.enrolment_proof_message(
        "another-token-entirely-xx", chip.public_bytes_raw())))
    for proof in (other, wrong_token):
        with pytest.raises(HTTPException) as refused:
            await api_v2.enroll_device(enrol_body(chip, proof=proof))
        assert refused.value.status_code == 400
    assert enrolling[0] == []


@asyncio_mark
async def test_a_software_pc_still_enrols_as_before(server, device_key, enrolling):
    calls, signed = enrolling
    body = api_v2.EnrollRequest(enrollment_token=TOKEN,
                                device_pubkey=device_key.public_bytes_raw().hex())
    assert (await api_v2.enroll_device(body))["device_id"] == "dev-new"
    assert signed == []                 # signs from its first signed request, as before


# ============================================================
# The real chip, where this PC has one
# ============================================================

def _live_chip():
    try:
        from core import tpm_key
    except Exception:
        return None
    if tpm_key.available() is not None:
        return None
    return tpm_key


@pytest.mark.skipif(_live_chip() is None, reason="no usable TPM on this machine")
def test_the_real_chip_signs_what_the_server_accepts_and_keeps_its_key():
    import ctypes
    tpm_key = _live_chip()
    key = tpm_key.TpmDeviceKey.create(name="AIMScribe Server Test Key", machine=False)
    try:
        pub = key.public_bytes_raw()
        entry = agent.build_entry(entry_no=0, entry_type="open", payload={"type": "open"},
                                  prev_hash=None, signer=key)
        parsed = integrity.parse_entry(entry.to_wire())
        assert integrity.verify_entry(parsed, expected_prev=None, device_pubkey=pub).ok
        proof = key.sign(integrity.enrolment_proof_message(TOKEN, pub))
        assert integrity.verify_by_device_key(
            pub, proof, integrity.enrolment_proof_message(TOKEN, pub))

        # The private half cannot be taken out of the chip.
        size = ctypes.c_ulong(0)
        status = key._nc.NCryptExportKey(key._key, None, ctypes.c_wchar_p("ECCPRIVATEBLOB"),
                                         None, None, 0, ctypes.byref(size), 0)
        assert status != 0
    finally:
        key.delete()
