"""
Recorder updates, the server's half (036): a release, its rollout stage, and
the signed notice a PC inside the stage receives in its heartbeat reply.

The notice is checked here with the recorder's own code (core/updater.py), so
the two sides cannot drift: what this server signs is what a PC accepts, and
nothing signed for another purpose passes as a notice.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi import HTTPException

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND / "src"))
sys.path.insert(0, str(BACKEND / "tests"))
sys.path.insert(0, str(BACKEND.parent / "recorder"))

import api_v2                                   # noqa: E402
import integrity                                # noqa: E402
from core import updater                        # noqa: E402

from test_v3_server import DEVICE, server  # noqa: E402,F401

try:
    from test_db_integration import dbs, enrolled_device, pg, _server, _unserve  # noqa: E402,F401
    HAVE_POSTGRES = True
except pytest.skip.Exception:
    HAVE_POSTGRES = False

postgres = pytest.mark.skipif(not HAVE_POSTGRES, reason="pgserver is not installed")

SHA = "ab" * 32
PC = {"device_id": "dev-1", "hospital_id": "HOSP003", "machine_name": "DHOLPUR-ROOM2",
      "app_version": "3.8.3", "revoked_at": None}


def release(**over):
    r = {"release_id": 4, "version": "3.8.4", "download_url": "https://example.org/s.exe",
         "sha256": SHA, "rollback": False, "stage": "all", "test_devices": [],
         "hospitals": [], "paused": False}
    r.update(over)
    return r


# ============================================================ the stages

@pytest.mark.parametrize("stage,tests,hospitals,reaches", [
    ("off", ["dev-1"], ["HOSP003"], False),
    ("test", [], [], False),
    ("test", ["dev-1"], [], True),
    ("test", ["dholpur-room2"], [], True),             # by name, any case
    ("test", ["OTHER-PC"], ["HOSP003"], False),         # hospitals wait for their stage
    ("hospitals", [], ["HOSP001"], False),
    ("hospitals", [], ["hosp003"], True),
    ("hospitals", ["dev-1"], ["HOSP001"], True),        # test PCs stay in
    ("all", [], [], True),
])
def test_a_release_reaches_only_its_stage(stage, tests, hospitals, reaches):
    r = release(stage=stage, test_devices=tests, hospitals=hospitals)
    assert api_v2.release_reaches(r, PC) is reaches
    assert api_v2.release_reaches({**r, "paused": True}, PC) is False


def test_a_pc_is_offered_only_a_version_it_does_not_run():
    assert api_v2.release_offered(release(), PC, "3.8.3")
    assert not api_v2.release_offered(release(), PC, "3.8.4")
    assert not api_v2.release_offered(release(), PC, "3.9.0")      # never down, unasked
    assert api_v2.release_offered(release(version="3.10.0"), PC, "3.9.9")
    rollback = release(version="3.8.2", rollback=True)
    assert api_v2.release_offered(rollback, PC, "3.8.4")
    assert not api_v2.release_offered(rollback, PC, "3.8.2")
    assert not api_v2.release_offered(None, PC, "3.8.3")


# ============================================================ the heartbeat

def offering(server, r):
    async def current():
        return r
    server.repo.current_recorder_release = current


async def beat(**kw):
    kw.setdefault("app_version", "3.8.3")
    return await api_v2.heartbeat(api_v2.HeartbeatRequest(**kw), device=DEVICE)


@pytest.mark.asyncio
async def test_a_pc_in_the_stage_gets_a_notice_the_recorder_accepts(server):
    offering(server, release())
    reply = await beat()
    notice = reply["update"]
    taken = updater.verify_notice(notice, server.receipt_public, device_id="dev-1",
                                  installed_version="3.8.3")
    assert (taken.version, taken.release_id, taken.sha256, taken.rollback) == \
        ("3.8.4", 4, SHA, False)
    # Named to this PC: another PC cannot use it.
    with pytest.raises(updater.NoticeRefused, match="another PC"):
        updater.verify_notice(notice, server.receipt_public, device_id="dev-2",
                              installed_version="3.8.3")


@pytest.mark.asyncio
async def test_the_notice_has_its_own_signing_domain(server):
    offering(server, release())
    notice = (await beat())["update"]
    data = integrity.canonical_json(notice["payload"])
    signature = bytes.fromhex(notice["signature"])
    server.receipt_public.verify(signature, integrity.digest(integrity.UPDATE_DOMAIN, data))
    for other in (integrity.ORIGINS_DOMAIN, integrity.RECEIPT_DOMAIN, integrity.CHAIN_DOMAIN):
        with pytest.raises(Exception):
            server.receipt_public.verify(signature, integrity.digest(other, data))
    assert len({integrity.UPDATE_DOMAIN, integrity.ORIGINS_DOMAIN, integrity.RECEIPT_DOMAIN,
                integrity.CHAIN_DOMAIN, integrity.AUDIT_DOMAIN}) == 5
    # A purge receipt from this server is not a notice, though the key is the same.
    receipt = api_v2.ctx.signer.sign_session(session_id="S", sha256_hex=SHA,
                                             archived_at=datetime.now(timezone.utc))
    with pytest.raises(updater.NoticeRefused):
        updater.verify_notice(receipt, server.receipt_public, device_id="dev-1",
                              installed_version="3.8.3")


@pytest.mark.asyncio
async def test_no_notice_outside_the_stage_paused_or_already_there(server):
    for r in (release(stage="off"), release(paused=True),
              release(stage="test", test_devices=["someone-else"]), None):
        offering(server, r)
        assert "update" not in await beat()
    offering(server, release())
    assert "update" not in await beat(app_version="3.8.4")


@pytest.mark.asyncio
async def test_no_signing_key_no_notice(server):
    offering(server, release())
    api_v2.ctx.signer = None
    assert "update" not in await beat()


@pytest.mark.asyncio
async def test_a_server_without_releases_still_answers(server):
    # The fake repository knows nothing of releases: the heartbeat must not fail.
    reply = await beat()
    assert reply["status"] == "ok" and "update" not in reply


@pytest.mark.asyncio
async def test_what_the_updater_reports_is_kept_and_a_give_up_alerts_once(server):
    report = {"state": "gave_up", "version": "3.8.4", "release_id": 4, "attempts": 3,
              "reason": "the download's SHA-256 is wrong" + "!" * 900, "junk": {"a": 1}}
    for _ in range(3):
        await beat(update_status=report)
    kept = server.repo.devices["dev-1"]["update_status"]
    assert kept["state"] == "gave_up" and kept["attempts"] == 3
    assert len(kept["reason"]) == 300 and "junk" not in kept
    alerts = [a for a in server.repo.alerts if a["alert_type"] == "recorder_update_failed"]
    assert len(alerts) == 1
    await beat(update_status={"state": "something we never said"})
    assert server.repo.devices["dev-1"]["update_status"]["state"] == "gave_up"


@pytest.mark.asyncio
async def test_the_minimum_version_still_works_alongside(server, monkeypatch):
    monkeypatch.setenv("AIMS_MIN_RECORDER_VERSION", "3.8.4")
    offering(server, release())
    reply = await beat(app_version="3.8.3")
    assert "update" in reply
    assert [a for a in server.repo.alerts if a["alert_type"] == "recorder_outdated"]


def test_update_reports_are_cut_to_size():
    assert api_v2.update_report(None) is None
    assert api_v2.update_report({"state": "nonsense"}) is None
    assert api_v2.update_report({"state": "installed", "version": "3.8.4", "attempts": "x"}) \
        == {"state": "installed", "version": "3.8.4"}


# ============================================================ on PostgreSQL

@postgres
@pytest.mark.asyncio
async def test_releases_rollout_and_pause_on_postgres(dbs, monkeypatch):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    repo = dbs.repo
    device = await enrolled_device(repo)
    _server(dbs)
    key = Ed25519PrivateKey.generate()
    api_v2.ctx.signer = integrity.ReceiptSigner(key)
    try:
        async with dbs.rec.acquire() as conn:          # an earlier test may have added one
            await conn.execute("DELETE FROM recorder_releases")
        added = await api_v2.admin_add_recorder_release(api_v2.RecorderReleaseRequest(
            version="v3.8.4", sha256=SHA.upper(), by="Tester"), None)
        r = added["release"]
        assert (r["version"], r["stage"], r["sha256"]) == ("3.8.4", "off", SHA)
        assert r["download_url"].endswith("/recorder-v3.8.4/AIMScribeSetup.exe")

        async def reply_for(version="3.8.3"):
            return await api_v2.heartbeat(api_v2.HeartbeatRequest(app_version=version),
                                          device=device)

        assert "update" not in await reply_for()                     # off: nobody
        await api_v2.admin_recorder_rollout(r["release_id"], api_v2.RolloutRequest(
            stage="test", test_devices=["PC"], by="Tester"), None)
        notice = (await reply_for())["update"]
        assert updater.verify_notice(notice, key.public_key(),
                                     device_id=str(device["device_id"]),
                                     installed_version="3.8.3").version == "3.8.4"
        await api_v2.admin_recorder_pause(r["release_id"], api_v2.PauseRequest(
            paused=True, by="Tester"), None)
        assert "update" not in await reply_for()
        await api_v2.admin_recorder_pause(r["release_id"], api_v2.PauseRequest(
            paused=False, by="Tester"), None)
        await api_v2.admin_recorder_rollout(r["release_id"], api_v2.RolloutRequest(
            stage="hospitals", hospitals=["HOSP001"], by="Tester"), None)
        assert "update" not in await reply_for()                     # not its hospital
        await api_v2.admin_recorder_rollout(r["release_id"], api_v2.RolloutRequest(
            stage="hospitals", hospitals=["HOSP003"], by="Tester"), None)
        assert "update" in await reply_for()

        # What the PC reports is kept and listed per PC.
        await api_v2.heartbeat(api_v2.HeartbeatRequest(
            app_version="3.8.4", update_status={"state": "installed", "version": "3.8.4"}),
            device=device)
        listing = await api_v2.admin_recorder_releases(None)
        me = next(d for d in listing["devices"] if d["device_id"] == str(device["device_id"]))
        assert me["app_version"] == "3.8.4" and me["update_status"]["state"] == "installed"
        assert me["in_stage"] and not me["offered"] and listing["current"]["stage"] == "hospitals"

        # An older version only as a rollback; only the newest release can be changed.
        with pytest.raises(HTTPException) as older:
            await api_v2.admin_add_recorder_release(api_v2.RecorderReleaseRequest(
                version="3.8.2", sha256=SHA, by="Tester"), None)
        assert older.value.status_code == 422
        back = (await api_v2.admin_add_recorder_release(api_v2.RecorderReleaseRequest(
            version="3.8.2", sha256=SHA, rollback=True, by="Tester"), None))["release"]
        with pytest.raises(HTTPException) as stale:
            await api_v2.admin_recorder_rollout(r["release_id"], api_v2.RolloutRequest(
                stage="all", by="Tester"), None)
        assert stale.value.status_code == 409
        await api_v2.admin_recorder_rollout(back["release_id"], api_v2.RolloutRequest(
            stage="all", by="Tester"), None)
        notice = (await reply_for("3.8.4"))["update"]
        assert notice["payload"]["rollback"] is True
        assert updater.verify_notice(notice, key.public_key(),
                                     device_id=str(device["device_id"]),
                                     installed_version="3.8.4").version == "3.8.2"

        for bad in (dict(version="3.8.x"), dict(version="3.8.5", sha256="xyz"),
                    dict(version="3.8.5", url="http://example.org/s.exe", sha256=SHA)):
            with pytest.raises(HTTPException):
                await api_v2.admin_add_recorder_release(api_v2.RecorderReleaseRequest(
                    by="Tester", **bad), None)
        with pytest.raises(HTTPException):
            await api_v2.admin_recorder_rollout(back["release_id"], api_v2.RolloutRequest(
                stage="test", by="Tester"), None)                   # no test PC named
        async with dbs.rec.acquire() as conn:
            audited = await conn.fetchval(
                "SELECT count(*) FROM audit_log WHERE event_type LIKE 'recorder_release.%'")
        assert audited >= 8
    finally:
        api_v2.ctx.signer = None
        _unserve()


@postgres
@pytest.mark.asyncio
async def test_the_dashboard_shows_each_pcs_version_and_updater(dbs):
    import dashboard
    device = await enrolled_device(dbs.repo)
    await dbs.repo.touch_device(device["device_id"], app_version="3.8.4",
                                update_status={"state": "failed", "reason": "SHA-256"})
    await dbs.repo.touch_device(device["device_id"], app_version="3.8.4")   # kept
    async with dbs.rec.acquire() as conn:
        rooms = (await dashboard._recorders(conn, None))["rooms"]
    me = next(r for r in rooms if r["device_id"] == str(device["device_id"]))
    assert me["app_version"] == "3.8.4"
    assert me["update_status"] == {"state": "failed", "reason": "SHA-256"}
    assert me["update_status_at"]


@postgres
@pytest.mark.asyncio
async def test_a_sha256_is_read_from_the_github_release_when_not_given(dbs, monkeypatch):
    seen = []

    async def published(url):
        seen.append(url)
        return "cd" * 32

    monkeypatch.setattr(api_v2, "_published_sha256", published)
    monkeypatch.setenv("AIMS_RECORDER_RELEASE_URL", "https://downloads.example/{version}/s.exe")
    _server(dbs)
    try:
        async with dbs.rec.acquire() as conn:
            await conn.execute("DELETE FROM recorder_releases")
        r = (await api_v2.admin_add_recorder_release(api_v2.RecorderReleaseRequest(
            version="4.0.0", by="Tester"), None))["release"]
        assert seen == ["https://downloads.example/4.0.0/s.exe"]
        assert r["sha256"] == "cd" * 32
    finally:
        _unserve()
