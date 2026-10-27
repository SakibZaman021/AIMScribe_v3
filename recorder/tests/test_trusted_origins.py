"""
CMED's EHR addresses come from the AIMS LAB server, signed, not from the installer.

Every installer is the same for every hospital. The server sends the list in
its heartbeat reply; the recorder takes it only when the signature checks
under its own domain and the version is newer than the one held.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from core import crypto, trusted_origins


def signed(key, origins, version, scope="trusted_origins", domain=None):
    payload = {"scope": scope, "origins": origins, "version": version,
               "issued_at": "2026-10-27T08:00:00Z"}
    data = crypto.digest(domain, crypto.canonical_json(payload)) if domain \
        else trusted_origins.signing_input(payload)
    return {"payload": payload, "signature": key.sign(data).hex()}


@pytest.fixture
def server_key(tmp_path):
    key = Ed25519PrivateKey.generate()
    trusted_origins.configure(tmp_path, key.public_key())
    yield key
    trusted_origins.configure(None, None)


def test_a_signed_list_is_taken_and_kept(server_key, tmp_path):
    assert not trusted_origins.allowed("https://emr.cmed.com.bd")
    assert trusted_origins.apply(signed(server_key, ["https://emr.cmed.com.bd"], 5))
    assert trusted_origins.allowed("https://emr.cmed.com.bd")
    assert not trusted_origins.allowed("https://evil.example")
    # A restart without the internet: the list is read back from disk.
    trusted_origins.configure(tmp_path, server_key.public_key())
    assert trusted_origins.allowed("https://emr.cmed.com.bd") and trusted_origins.version() == 5


def test_a_forged_or_replayed_list_is_refused(server_key):
    assert trusted_origins.apply(signed(server_key, ["https://emr.cmed.com.bd"], 5))
    stranger = Ed25519PrivateKey.generate()
    assert not trusted_origins.apply(signed(stranger, ["https://evil.example"], 6))
    # A purge receipt's signature is not a list's: another domain.
    assert not trusted_origins.apply(signed(server_key, ["https://evil.example"], 7,
                                            domain=crypto.RECEIPT_DOMAIN))
    assert not trusted_origins.apply(signed(server_key, ["https://evil.example"], 8,
                                            scope="segment"))
    # An older list, genuinely signed, replayed after a newer one.
    assert not trusted_origins.apply(signed(server_key, ["https://old.example"], 4))
    assert trusted_origins.current() == frozenset({"https://emr.cmed.com.bd"})


def test_a_newer_list_replaces_the_old_one(server_key):
    trusted_origins.apply(signed(server_key, ["https://emr.cmed.com.bd"], 5))
    assert trusted_origins.apply(signed(server_key, ["https://new.cmed.com.bd"], 6))
    assert trusted_origins.allowed("https://new.cmed.com.bd")
    assert not trusted_origins.allowed("https://emr.cmed.com.bd")    # removed on the server


def test_odd_entries_are_dropped(server_key):
    trusted_origins.apply(signed(server_key, ["https://ok.example", "https://*.example",
                                              "https://x.example/path", " https://sp.example",
                                              "ftp://x.example"], 3))
    assert trusted_origins.current() == frozenset({"https://ok.example"})


def test_the_recorders_origin_check_uses_both_lists(server_key):
    from config import SecurityConfig
    security = SecurityConfig(allowed_origins=frozenset({"https://bench.local"}))
    assert security.origin_allowed("https://bench.local")             # set at install
    assert not security.origin_allowed("https://emr.cmed.com.bd")
    trusted_origins.apply(signed(server_key, ["https://emr.cmed.com.bd"], 9))
    assert security.origin_allowed("https://emr.cmed.com.bd")         # from the server
    assert not security.origin_allowed(None) and not security.origin_allowed("null")
