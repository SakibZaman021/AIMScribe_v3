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


# ---- a PC that has not joined yet asks the server itself, before its first API 1

async def serve_list(bundle):
    from aiohttp import web
    asked = []

    async def handler(request):
        asked.append(request.query.get("have"))
        have = int(request.query.get("have", "0"))
        newer = bundle if bundle and bundle["payload"]["version"] > have else None
        return web.json_response({"trusted_origins": newer})

    app = web.Application()
    app.router.add_get("/api/v2/trusted-origins", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    return runner, f"http://127.0.0.1:{port}", asked


def unjoined(base_url):
    from api.trigger_server import Runtime
    from config import BackendConfig
    runtime = Runtime.__new__(Runtime)
    runtime.cfg = SimpleNamespace(backend=BackendConfig(base_url=base_url))
    runtime.identity = None
    runtime._loop = None
    return runtime


async def test_a_new_pc_fetches_the_list_before_it_has_joined(server_key):
    runner, url, asked = await serve_list(
        signed(server_key, ["https://postman.aimscribe.test"], 4))
    try:
        runtime = unjoined(url)
        assert not trusted_origins.allowed("https://postman.aimscribe.test")
        assert await runtime.fetch_trusted_origins()
        assert trusted_origins.allowed("https://postman.aimscribe.test")
        # Asked again with the version it holds: nothing new, nothing changes.
        assert not await runtime.fetch_trusted_origins()
        assert asked == ["0", "4"]
    finally:
        await runner.cleanup()


async def test_a_list_not_signed_by_the_server_is_not_taken_from_the_open_endpoint(server_key):
    stranger = Ed25519PrivateKey.generate()
    runner, url, _ = await serve_list(signed(stranger, ["https://evil.example"], 9))
    try:
        assert not await unjoined(url).fetch_trusted_origins()
        assert not trusted_origins.allowed("https://evil.example")
    finally:
        await runner.cleanup()


async def test_no_server_is_not_an_error(server_key):
    assert not await unjoined("http://127.0.0.1:9").fetch_trusted_origins()


async def test_it_stops_asking_once_the_heartbeat_takes_over(server_key, monkeypatch):
    runtime = unjoined("http://127.0.0.1:9")
    calls = []

    async def fetch():
        calls.append(1)
        if len(calls) == 2:
            runtime.identity = object()          # joined meanwhile
        return False

    async def no_wait(_):
        return None

    runtime.fetch_trusted_origins = fetch
    monkeypatch.setattr("asyncio.sleep", no_wait)
    await runtime._fetch_origins_until_joined()
    assert len(calls) == 2
