"""
25 Nov 2026: three things nobody was told on 24 Nov at Aalo Karail.

- GP-Female was off from 08:00 to 20:50 while the clinic saw patients:
  "PC not on today" once the clinic's first patient is 20 minutes old.
- 88 of 171 patients CMED sent had no recording: "patients not recorded"
  when two or more in the last hour were taken by no recording.
- GP-Males' own microphone gave exact zeros (336 pieces): "recording is
  silent" when a recording's pieces are all rms 0 for 8 s or more.
"""
from __future__ import annotations

import hashlib
import os
import sys
import uuid
from datetime import timedelta
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND / "src"))
sys.path.insert(0, str(BACKEND / "tests"))

try:
    from test_db_integration import a_session, dbs, pg  # noqa: E402,F401
    HAVE_POSTGRES = True
except pytest.skip.Exception:
    HAVE_POSTGRES = False

import collectors                                       # noqa: E402

pytestmark = [pytest.mark.asyncio,
              pytest.mark.skipif(not HAVE_POSTGRES, reason="pgserver is not installed")]


async def _clinic_and_pc(repo, name="PC-X"):
    hid = f"H{uuid.uuid4().hex[:8].upper()}"
    await repo.upsert_hospital(hid, f"Clinic {hid}", "Asia/Dhaka")
    token = await repo.create_enrollment_token(hospital_id=hid, doctor_id="", created_by="tests")
    enrolled = await repo.enroll_device(token=token, tpm_pubkey=os.urandom(32),
                                        machine_name=name, os_version="", app_version="3.9.6",
                                        protocol_version=2)
    return hid, await repo.device_by_token(enrolled["device_token"])


async def _patient(conn, hid, minutes_ago, claimed=None):
    await conn.execute("""
        INSERT INTO confirmation_notices (cmed_hospital_id, hospital_id, patient_id, doctor_id,
                                          start_time, visit_date, claimed_by_jti, received_at)
        VALUES ($1, $1, $2, 'D1', '10:00', CURRENT_DATE, $3,
                now() - make_interval(mins => $4))
    """, hid, uuid.uuid4().hex[:10], claimed, minutes_ago)


def _catch(monkeypatch):
    told = []

    async def fake_notify(conn, **kw):
        told.append(kw)
        return 1

    monkeypatch.setattr(collectors, "notify", fake_notify)
    return told


async def test_a_pc_not_on_while_its_clinic_sees_patients_is_told_once_a_day(dbs, monkeypatch):
    told = _catch(monkeypatch)
    hid, off = await _clinic_and_pc(dbs.repo, "GP-Female")
    _, other = await _clinic_and_pc(dbs.repo, "elsewhere")
    async with dbs.rec.acquire() as conn:
        await conn.execute("UPDATE devices SET last_seen_at = now() - interval '1 day', "
                           "approval = 'approved' WHERE device_id = $1", off["device_id"])
        await _patient(conn, hid, minutes_ago=30)
        await collectors.Notifier().pcs_not_on(conn)
        mine = [t for t in told if t["hospital_id"] == hid]
        assert [t["event"] for t in mine] == ["pc_not_on"]
        assert "GP-Female" in mine[0]["subject"] and mine[0]["urgent"]
        # Heard from today: nothing.
        told.clear()
        await conn.execute("UPDATE devices SET last_seen_at = now() WHERE device_id = $1",
                           off["device_id"])
        await collectors.Notifier().pcs_not_on(conn)
        assert not [t for t in told if t["hospital_id"] == hid]
    # A clinic with no patient today is not "open": its PCs are not told.
    assert not [t for t in told if t.get("device_id") == other["device_id"]]


async def test_a_first_patient_just_now_waits_for_the_pc(dbs, monkeypatch):
    told = _catch(monkeypatch)
    hid, off = await _clinic_and_pc(dbs.repo)
    async with dbs.rec.acquire() as conn:
        await conn.execute("UPDATE devices SET last_seen_at = now() - interval '1 day' "
                           "WHERE device_id = $1", off["device_id"])
        await _patient(conn, hid, minutes_ago=5)
        await collectors.Notifier().pcs_not_on(conn)
    assert not [t for t in told if t["hospital_id"] == hid]


async def test_patients_no_recording_took_are_told(dbs, monkeypatch):
    told = _catch(monkeypatch)
    hid, _ = await _clinic_and_pc(dbs.repo)
    quiet, _ = await _clinic_and_pc(dbs.repo)
    async with dbs.rec.acquire() as conn:
        for minutes in (10, 20):
            await _patient(conn, hid, minutes_ago=minutes)
        await _patient(conn, hid, minutes_ago=15, claimed="a-grant")    # recorded
        await _patient(conn, hid, minutes_ago=2)                         # too new to tell
        await _patient(conn, quiet, minutes_ago=10)                      # only one
        await collectors.Notifier().patients_not_recorded(conn)
    mine = [t for t in told if t["hospital_id"] == hid]
    assert len(mine) == 1 and mine[0]["event"] == "patients_not_recorded"
    assert mine[0]["subject"].startswith("2 patients not recorded")
    assert not [t for t in told if t["hospital_id"] == quiet]


async def test_a_recording_of_pure_silence_is_told(dbs, monkeypatch):
    told = _catch(monkeypatch)
    _, pc = await _clinic_and_pc(dbs.repo, "GP-Males")
    silent = await a_session(dbs.repo, pc, opened_ago=timedelta(minutes=2))
    sound = await a_session(dbs.repo, pc, opened_ago=timedelta(minutes=1))
    digest = hashlib.sha256(b"x").digest()
    async with dbs.rec.acquire() as conn:
        await conn.execute("UPDATE sessions SET microphones = ARRAY['Microphone Array (Intel)'] "
                           "WHERE session_id = $1", silent)
        for sid, rms in ((silent, 0.0), (sound, 512.0)):
            for n in (1, 2):
                await conn.execute("""
                    INSERT INTO chain_entries (session_id, entry_no, entry_type, payload,
                                               payload_sha256, prev_hash, entry_hash,
                                               device_signature, occurred_at)
                    VALUES ($1, $2, 'segment', '{}', $3, $3, $3, $3, now())
                """, sid, n, digest)
                await conn.execute("""
                    INSERT INTO segments (session_id, seq_no, entry_no, object_key, sha256,
                                          bytes, duration_seconds, state, rms_mean)
                    VALUES ($1, $2, $2, $3, $4, 100, 5.0, 'committed', $5)
                """, sid, n, f"audio/{sid}/seg_{n:05d}.wav", digest, rms)
        await collectors.Notifier().silent_recordings(conn)
    events = {(t["session_id"], t["event"]) for t in told}
    assert (silent, "silent_recording") in events
    assert not [t for t in told if t.get("session_id") == sound]
    (note,) = [t for t in told if t.get("session_id") == silent]
    assert "Microphone Array (Intel)" in note["body"]
