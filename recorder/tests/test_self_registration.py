"""
3.7: a clinic PC whose key is in its TPM joins without an enrolment token.
The bytes it signs and the id it derives are the server's, byte for byte.
"""
from __future__ import annotations

import hashlib
import time
from types import SimpleNamespace

from core import crypto, enrollment
from core.uploader import UploadManager

KEY = bytes([4]) + bytes(range(1, 65))


def test_the_registration_proof_and_the_pc_id_are_the_servers():
    # The same literals are pinned in backend/tests/test_tpm_devices.py.
    assert crypto.device_id_for_key(KEY) == "ac346361-ca8a-5037-b571-1338ef04dc9d"
    message = crypto.registration_proof_message("CMED-DHOLPUR", KEY, 1790000000)
    assert hashlib.sha256(message).hexdigest() ==         "2c3ce0a13d1e69e1e6c95b2b65513e00f4307939e9b525c07df95fcd4ae12073"


def test_only_a_tpm_key_registers_itself_and_only_when_allowed():
    on = SimpleNamespace(security=SimpleNamespace(self_register=True))
    off = SimpleNamespace(security=SimpleNamespace(self_register=False))
    tpm, software = SimpleNamespace(kind="p256-tpm"), SimpleNamespace(kind="ed25519")
    assert enrollment.can_self_register(on, tpm)
    assert not enrollment.can_self_register(on, software)
    assert not enrollment.can_self_register(off, tpm)


def test_a_revoked_pc_stops_asking_and_says_so_once():
    up = object.__new__(UploadManager)
    up._quiet_until, alerts = 0.0, []
    up._emit = lambda kind, data: alerts.append((kind, data["alert_type"]))
    up._revoked('{"detail":"device has been revoked"}')
    up._revoked('{"detail":"device has been revoked"}')
    assert up._quiet_until > time.monotonic() + 800
    assert alerts == [("integrity_alert", "device_revoked")]
