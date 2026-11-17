"""
3.9.3, the server's half of moving a PC (17 Nov 2026, CMED's PC TEST -> AALO_KARAIL).

* Every heartbeat reply names the PC's clinic, so a PC moved on the dashboard
  learns it within 30 seconds instead of refusing every grant for the new one.
* A recording made before the move arrives naming the old clinic. It is filed
  under the PC's clinic, as before; when an administrator really did move the
  PC between those two clinics that is noted quietly, not as a critical alert.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND / "src"))
sys.path.insert(0, str(BACKEND / "tests"))
sys.path.insert(0, str(BACKEND.parent / "recorder"))

import api_v2                                   # noqa: E402
from core import crypto as agent                # noqa: E402

from test_data_safety import open_request       # noqa: E402
from test_reassign import device, device_key    # noqa: E402,F401
from test_v3_server import DEVICE, NOW, server  # noqa: E402,F401

pytestmark = pytest.mark.asyncio


async def test_the_heartbeat_names_the_pcs_clinic(server):
    reply = await api_v2.heartbeat(api_v2.HeartbeatRequest(), device=DEVICE)
    assert reply["hospital_id"] == DEVICE["hospital_id"]


async def opened_from(server, device_key, device, claimed, sid):
    opened = NOW()
    genesis = agent.build_entry(
        entry_no=0, entry_type="open",
        payload=agent.open_payload(device_id=str(device["device_id"]), doctor_id="DR0042",
                                   hospital_id=claimed, patient_ref="P0012345",
                                   opened_at=opened, sample_rate=16000, channels=1,
                                   sample_width=2),
        prev_hash=None, signer=device_key)
    request = open_request(sid, genesis, opened.isoformat())
    request.hospital_id = claimed
    request.audio = SimpleNamespace(
        model_dump=lambda: {"sample_rate": 16000, "channels": 1, "sample_width": 2})

    class Filed(Exception):
        pass

    async def open_session(**kw):
        # Where the recording is filed; the alert has been raised by then.
        raise Filed(kw["hospital_id"])

    server.repo.open_session = open_session
    with pytest.raises(Filed) as filed:
        await api_v2.open_session(request, device=device)
    return str(filed.value)


async def test_a_recording_from_before_a_move_is_filed_quietly(server, device_key, device):
    moves = []

    async def device_moved_between(device_id, from_clinic, to_clinic):
        moves.append((from_clinic, to_clinic))
        return (from_clinic, to_clinic) == ("TEST", "HOSP003")

    server.repo.device_moved_between = device_moved_between
    filed = await opened_from(server, device_key, device, "TEST",
                              "01JB8XQ4M7YZ2K9V3N5P6R8T10")
    assert filed == "HOSP003"                                # the PC's clinic, as before
    alert = server.repo.alerts[-1]
    assert (alert["alert_type"], alert["severity"]) == ("recorded_before_move", "info")
    assert moves == [("TEST", "HOSP003")]


async def test_a_clinic_the_pc_was_never_moved_from_is_still_critical(server, device_key,
                                                                      device):
    async def device_moved_between(device_id, from_clinic, to_clinic):
        return False

    server.repo.device_moved_between = device_moved_between
    await opened_from(server, device_key, device, "HOSP001", "01JB8XQ4M7YZ2K9V3N5P6R8T11")
    alert = server.repo.alerts[-1]
    assert (alert["alert_type"], alert["severity"]) == ("clinic_mismatch", "critical")
