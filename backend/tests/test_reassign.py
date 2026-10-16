"""
A wrong patient picked at the start, corrected by the prescription.

The recorder signs a `reassign` entry into the chain when the prescription is
built for a patient opened during the recording; this server applies it. The
entries here are built and signed by the recorder's own code, so the two halves
are tested against each other rather than against a description of each other.
"""
from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND / "src"))
sys.path.insert(0, str(BACKEND / "tests"))
sys.path.insert(0, str(BACKEND.parent / "recorder"))

import api_v2                                   # noqa: E402
from core import crypto as agent                # noqa: E402

from test_v3_server import (                    # noqa: E402,F401
    DEVICE, FIELDS, NOW, api2, mint, open_session, send, server, visit_of)

pytestmark = pytest.mark.asyncio

RIGHT = {**FIELDS, "patient_id": "P0099999", "start_time": "2026-09-13T10:15:12+06:00"}


@pytest.fixture
def device_key(tmp_path):
    return agent.DeviceKey.load_or_create(tmp_path / "device.key", allow_plaintext=True)


@pytest.fixture
def device(device_key):
    return {**DEVICE, "tpm_pubkey": device_key.public_bytes_raw()}


def started(server, device_key, *, opened_ago=timedelta(seconds=60)):
    """A recording opened under the wrong patient, its chain as the server holds it."""
    sid = open_session(server, opened_ago=opened_ago)
    genesis = agent.build_entry(entry_no=0, entry_type="open", payload={"type": "open"},
                                prev_hash=None, signer=device_key)
    server.repo.chains[sid] = [genesis]
    return sid


def reassign_entry(server, sid, device_key, *, to=RIGHT, opened_after=timedelta(seconds=40)):
    head = server.repo.chains[sid][-1]
    opened = server.repo.sessions[sid]["opened_at"] + opened_after
    return agent.build_entry(
        entry_no=head.entry_no + 1, entry_type="reassign",
        payload=agent.reassign_payload(from_fields=FIELDS, to_fields=to,
                                       patient_opened_at=opened, at=NOW()),
        prev_hash=head.entry_hash, signer=device_key)


async def deliver(sid, entry, device, grant_jti):
    return await api_v2.reassign_session(api_v2.ReassignRequest(
        session_id=sid, chain_entry=entry.to_wire(), grant_jti=grant_jti), device=device)


async def test_the_recording_moves_to_the_patient_the_prescription_named(
        server, device_key, device):
    sid = started(server, device_key)
    _, grant = await mint(RIGHT)
    result = await deliver(sid, reassign_entry(server, sid, device_key), device, grant["jti"])

    assert (result["status"], result["reassigned"]) == ("recorded", True)
    session = server.repo.sessions[sid]
    assert (session["patient_id"], session["grant_jti"]) == ("P0099999", grant["jti"])
    assert server.repo.chains[sid][-1].entry_type == "reassign"
    assert any(a["event_type"] == "session.reassigned" for a in server.repo.audits)
    assert not any(a["alert_type"] == "possibly_two_consultations"
                   for a in server.repo.alerts)


async def test_the_right_patients_api2_confirms_the_moved_recording(
        server, device_key, device):
    await send("patient_information", api2(**RIGHT))
    sid = started(server, device_key)
    _, grant = await mint(RIGHT)
    result = await deliver(sid, reassign_entry(server, sid, device_key), device, grant["jti"])
    assert result["confirmation"] == "confirmed"
    assert server.repo.sessions[sid]["confirmation"] == "confirmed"


async def test_the_wrong_patients_late_records_stay_off_the_moved_recording(
        server, device_key, device):
    """Found in review: the first grant still named the recording after the move."""
    sid = started(server, device_key)
    _, wrong = await mint(FIELDS)
    server.repo.auths[wrong["jti"]]["session_id"] = sid
    server.repo.sessions[sid].update(grant_jti=wrong["jti"], confirmation="pending")
    _, grant = await mint(RIGHT)
    await deliver(sid, reassign_entry(server, sid, device_key), device, grant["jti"])
    assert server.repo.sessions[sid]["confirmation"] == "pending"

    await send("patient_information", api2(**FIELDS))       # the wrong one, late
    assert server.repo.sessions[sid]["confirmation"] == "pending"
    assert await server.repo.session_for_visit(visit_of(FIELDS)) is None
    assert (await server.repo.session_for_visit(visit_of(RIGHT)))["session_id"] == sid

    await send("patient_information", api2(**RIGHT))        # the right one confirms it
    assert server.repo.sessions[sid]["confirmation"] == "confirmed"


async def test_a_move_that_arrives_after_close_renames_the_file(
        server, device_key, device):
    """The PC was offline: the recording closed, and was named, before the move."""
    sid = started(server, device_key)
    session = server.repo.sessions[sid]
    session.update(closed_at=session["opened_at"] + timedelta(minutes=6))
    await api_v2._record_file_name(sid)
    assert session["file_stem"].startswith("P0012345_")       # the wrong patient

    _, grant = await mint(RIGHT)
    await deliver(sid, reassign_entry(server, sid, device_key), device, grant["jti"])
    assert session["file_stem"].startswith("P0099999_")       # the right one


async def test_a_move_long_into_the_recording_is_flagged_for_a_person(
        server, device_key, device):
    sid = started(server, device_key, opened_ago=timedelta(minutes=30))
    _, grant = await mint(RIGHT)
    entry = reassign_entry(server, sid, device_key, opened_after=timedelta(minutes=18))
    assert (await deliver(sid, entry, device, grant["jti"]))["reassigned"] is True
    flagged = [a for a in server.repo.alerts if a["alert_type"] == "possibly_two_consultations"]
    assert flagged and flagged[0]["detail"]["minutes_into_recording"] == 18.0


@pytest.mark.parametrize("grant_for", [None, "another patient"])
async def test_without_a_valid_grant_the_entry_is_kept_and_nothing_moves(
        server, device_key, device, grant_for):
    sid = started(server, device_key)
    jti = None
    if grant_for:
        _, other = await mint({**RIGHT, "patient_id": "P0011111"})
        jti = other["jti"]
    result = await deliver(sid, reassign_entry(server, sid, device_key), device, jti)
    assert (result["status"], result["reassigned"]) == ("recorded", False)
    assert server.repo.sessions[sid]["patient_id"] == "P0012345"     # untouched, kept
    assert len(server.repo.chains[sid]) == 2                          # chain intact
    assert server.repo.alerts[-1]["alert_type"] == "reassign_not_applied"


async def test_a_redelivered_entry_is_a_duplicate_and_an_early_one_waits(
        server, device_key, device):
    sid = started(server, device_key)
    _, grant = await mint(RIGHT)
    entry = reassign_entry(server, sid, device_key)
    await deliver(sid, entry, device, grant["jti"])
    assert (await deliver(sid, entry, device, grant["jti"]))["duplicate"] is True

    other = started(server, device_key)
    head = server.repo.chains[other][-1]
    early = agent.build_entry(entry_no=5, entry_type="reassign",
                              payload={"type": "reassign", "to": RIGHT},
                              prev_hash=b"\x01" * 32, signer=device_key)
    assert (await deliver(other, early, device, grant["jti"]))["status"] == "deferred"
    assert server.repo.chains[other] == [head]
