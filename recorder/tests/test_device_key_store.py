"""
Which key a PC signs with (recorder 3.6).

An installed recorder keeps its identity key inside the PC's TPM chip, where no
program can copy it. A PC enrolled earlier with a software key keeps that key
until it is re-enrolled, because the server holds that key's public half. A PC
whose TPM cannot be used does not start recording with a weaker key instead.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from core import crypto, tpm_key


class FakeChip:
    kind = "p256-tpm"

    def __init__(self):
        self._key = ec.generate_private_key(ec.SECP256R1())

    def sign(self, message):
        r, s = decode_dss_signature(self._key.sign(message, ec.ECDSA(hashes.SHA256())))
        return r.to_bytes(32, "big") + s.to_bytes(32, "big")

    def public_bytes_raw(self):
        return self._key.public_key().public_bytes(Encoding.X962,
                                                   PublicFormat.UncompressedPoint)

    def public_key(self):
        return self._key.public_key()

    def fingerprint(self):
        return crypto.sha256_bytes(self.public_bytes_raw()).hex()[:16]


def cfg(tmp_path, store):
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    return SimpleNamespace(
        security=SimpleNamespace(device_key_path=tmp_path / "keys" / "device.key",
                                 allow_plaintext_keystore=True, device_key_store=store),
        paths=SimpleNamespace(state_dir=state))


def enrolled_as(c, fingerprint):
    (c.paths.state_dir / "device.json").write_text(json.dumps(
        {"device_id": "d", "hospital_id": "H", "key_fingerprint": fingerprint}))


@pytest.fixture
def chip(monkeypatch):
    made = FakeChip()
    monkeypatch.setattr(tpm_key.TpmDeviceKey, "open", classmethod(lambda cls, **kw: made))
    return made


def test_a_bench_pc_uses_a_software_key(tmp_path):
    key = crypto.load_device_key(cfg(tmp_path, "software"))
    assert isinstance(key, crypto.DeviceKey)


def test_an_installed_pc_signs_with_its_chip(tmp_path, chip):
    assert crypto.load_device_key(cfg(tmp_path, "tpm")) is chip


def test_a_pc_enrolled_with_a_software_key_keeps_it(tmp_path, monkeypatch):
    c = cfg(tmp_path, "software")
    soft = crypto.load_device_key(c)
    enrolled_as(c, soft.fingerprint())

    def no_chip(cls, **kw):
        raise AssertionError("an enrolled software PC must not switch keys")
    monkeypatch.setattr(tpm_key.TpmDeviceKey, "open", classmethod(no_chip))
    c.security.device_key_store = "tpm"
    assert crypto.load_device_key(c).fingerprint() == soft.fingerprint()


def test_a_pc_enrolled_with_its_chip_keeps_the_chip(tmp_path, chip):
    c = cfg(tmp_path, "software")
    crypto.load_device_key(c)                    # a stray software key on disk
    enrolled_as(c, chip.fingerprint())
    c.security.device_key_store = "tpm"
    assert crypto.load_device_key(c) is chip


def test_no_usable_tpm_stops_rather_than_falling_back(tmp_path, monkeypatch):
    def unavailable(cls, **kw):
        raise tpm_key.TpmUnavailable("this PC's TPM cannot be used")
    monkeypatch.setattr(tpm_key.TpmDeviceKey, "open", classmethod(unavailable))
    with pytest.raises(crypto.DeviceKeyError, match="TPM"):
        crypto.load_device_key(cfg(tmp_path, "tpm"))
    assert not (tmp_path / "keys" / "device.key").exists()


def test_a_missing_chip_key_points_at_the_installer(tmp_path, monkeypatch):
    def missing(cls, **kw):
        raise tpm_key.TpmKeyMissing("none")

    def not_admin(cls, **kw):
        raise OSError("creating the TPM key failed: 0x80090010 (run as administrator)")
    monkeypatch.setattr(tpm_key.TpmDeviceKey, "open", classmethod(missing))
    monkeypatch.setattr(tpm_key.TpmDeviceKey, "create", classmethod(not_admin))
    with pytest.raises(crypto.DeviceKeyError, match="installer"):
        crypto.load_device_key(cfg(tmp_path, "tpm"))


@pytest.mark.parametrize("outcome,code", [(None, 0), (tpm_key.TpmUnavailable("no"), 9),
                                          (OSError("boom"), 10)])
def test_the_installer_step_reports_what_happened(monkeypatch, outcome, code):
    def create(cls, **kw):
        if outcome is not None:
            raise outcome
        return FakeChip()
    monkeypatch.setattr(tpm_key.TpmDeviceKey, "create", classmethod(create))
    assert crypto.provision_tpm_key() == code


def test_a_chain_signed_by_the_chip_verifies_here_too():
    key = FakeChip()
    genesis = crypto.build_entry(entry_no=0, entry_type="open", payload={"type": "open"},
                                 prev_hash=None, signer=key)
    assert crypto.verify_chain([genesis], device_public_key=key.public_key()).ok
    assert not crypto.verify_chain([genesis],
                                   device_public_key=FakeChip().public_key()).ok


def test_the_enrolment_proof_names_the_token_and_the_key():
    key = FakeChip()
    message = crypto.enrolment_proof_message("token-1", key.public_bytes_raw())
    assert crypto.verify_device(key.public_key(), key.sign(message), message)
    assert message != crypto.enrolment_proof_message("token-2", key.public_bytes_raw())


@pytest.mark.skipif(tpm_key.available() is not None, reason="no usable TPM on this machine")
def test_the_real_chip():
    key = tpm_key.TpmDeviceKey.create(name="AIMScribe Recorder Test Key", machine=False)
    try:
        again = tpm_key.TpmDeviceKey.open(name="AIMScribe Recorder Test Key", machine=False)
        assert again.public_bytes_raw() == key.public_bytes_raw()
        signature = key.sign(b"consultation")
        assert tpm_key.verify(key.public_bytes_raw(), b"consultation", signature)
        assert crypto.verify_device(key.public_key(), signature, b"consultation")
        assert not tpm_key.verify(key.public_bytes_raw(), b"consultatioN", signature)
    finally:
        key.delete()
