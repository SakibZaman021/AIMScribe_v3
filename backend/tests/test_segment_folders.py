"""
The segment store in the archive's layout (20 Nov 2026).

* A recording's pieces go into its own folder, under clinic, doctor, day and
  visit, named as the archive names them - also for a clinic code with "_"
  (AALO_KARAIL), which used to fall back to a bare ULID.
* CMED's visit_id is on the recording from the moment it opens, not only
  after it closes.
* A recording opened before the change keeps its old addresses to the end.
* The mover puts waiting pieces into the layout, and leaves a recording as it
  was when a copy is not the piece or CMED confirms it during the move.
* The pieces log reads what arrived, what is missing and where it is now.
"""
from __future__ import annotations

import hashlib
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND / "src"))
sys.path.insert(0, str(BACKEND / "tests"))
sys.path.insert(0, str(BACKEND.parent / "recorder"))

import api_v2                                   # noqa: E402
import segment_layout                           # noqa: E402
from core import crypto as agent                # noqa: E402

from test_data_safety import open_request       # noqa: E402
from test_reassign import device, device_key    # noqa: E402,F401
from test_v3_server import DEVICE, NOW, server  # noqa: E402,F401


DHAKA = timezone(timedelta(hours=6))
OPENED = datetime(2026, 11, 19, 10, 52, 25, tzinfo=DHAKA)
SID = "01M7SPZC8F929V47JCX96JWRS5"


# ============================================================
# The folder and the piece names
# ============================================================

def test_a_recording_has_its_own_folder_under_clinic_doctor_day_and_visit():
    visit = api_v2.visit_key("P4347510", "DR4290373", "VIS-T-31508")
    assert api_v2.segment_folder("AALO_KARAIL", "DR4290373", OPENED, SID, visit) == (
        "AALO_KARAIL/DR4290373/2026-11-19/P4347510_DR4290373_VIS-T-31508/105225_JWRS5")


def test_without_a_visit_id_the_visit_folder_is_the_start_time():
    visit = api_v2.visit_key("P1", "DR1", None, fallback=f"{OPENED:%Y%m%dT%H%M%S}")
    assert api_v2.segment_folder("TEST", "DR1", OPENED, SID, visit) == (
        "TEST/DR1/2026-11-19/P1_DR1_20261119T105225/105225_JWRS5")


@pytest.mark.parametrize("hospital,doctor,visit", [
    ("../x", "DR1", "P1_DR1_V1"), ("TEST", "", "P1_DR1_V1"), ("TEST", "DR1", None),
    ("TEST", "DR1", "P1/DR1"), ("TEST", "a b", "P1_DR1_V1")])
def test_an_unsafe_part_gives_no_folder(hospital, doctor, visit):
    assert api_v2.segment_folder(hospital, doctor, OPENED, SID, visit) is None


def test_pieces_are_numbered_in_the_new_folder_and_unchanged_in_the_old():
    new = {"session_id": SID, "object_prefix": "TEST/DR1/2026-11-19/P1_DR1_V1/105225_JWRS5"}
    assert api_v2.piece_key(new, 7) == (
        "audio/TEST/DR1/2026-11-19/P1_DR1_V1/105225_JWRS5/piece_0007.wav")
    # Opened before the change: the same names to the end of the recording.
    old = {"session_id": SID, "object_prefix": "P1_DR1_TEST_105225_20261119_JWRS5",
           "patient_id": "P1", "doctor_id": "DR1", "hospital_id": "TEST",
           "session_date": OPENED.date()}
    assert api_v2.piece_key(old, 7) == (
        "audio/P1_DR1_TEST_105225_20261119_JWRS5/P1_DR1_TEST_20261119_0007_JWRS5.wav")
    bare = {"session_id": SID, "object_prefix": None, "patient_id": "P1",
            "doctor_id": "DR1", "hospital_id": "AALO_KARAIL", "session_date": OPENED.date()}
    assert api_v2.piece_key(bare, 7) == f"audio/{SID}/seg_00007.wav"


def test_a_piece_in_the_new_folder_is_never_taken_for_an_archive_copy():
    """_remove_object refuses archive copies; a piece must still be deletable."""
    key = "audio/AALO_KARAIL/DR1/2026-11-19/P1_DR1_V1/105225_JWRS5/piece_0001.wav"
    assert not api_v2.is_copy_key(key)
    assert api_v2.visit_folder_of(key) is None
    removed = []
    api_v2.ctx.minio = SimpleNamespace(bucket="aimscribe-segments", client=SimpleNamespace(
        remove_object=lambda b, k: removed.append(k)))
    api_v2.ctx.copy = SimpleNamespace(bucket="aimscribe-archive")
    try:
        api_v2._remove_object(key)
    finally:
        api_v2.ctx.minio = api_v2.ctx.copy = None
    assert removed == [key]


# ============================================================
# Opening a recording
# ============================================================

async def _open(server, device_key, device, sid, *, grant=None, hospital="HOSP003"):
    """Open through the real endpoint; return what was stored."""
    opened = NOW()
    genesis = agent.build_entry(
        entry_no=0, entry_type="open",
        payload=agent.open_payload(device_id=str(device["device_id"]), doctor_id="DR0042",
                                   hospital_id=hospital, patient_ref="P0012345",
                                   opened_at=opened, sample_rate=16000, channels=1,
                                   sample_width=2),
        prev_hash=None, signer=device_key)
    request = open_request(sid, genesis, opened.isoformat())
    request.hospital_id = hospital
    request.grant_jti = grant
    request.audio = SimpleNamespace(
        model_dump=lambda: {"sample_rate": 16000, "channels": 1, "sample_width": 2})
    stored = {}

    async def open_session(**kw):
        # The fake has no open of its own: keep the row the real one would.
        stored.update(kw)
        server.repo.sessions[kw["session_id"]] = dict(
            session_id=kw["session_id"], device_id=kw["device_id"],
            doctor_id=kw["doctor_id"], hospital_id=kw["hospital_id"],
            patient_id=kw["patient_id"], opened_at=kw["opened_at"],
            session_date=kw["session_date"], object_prefix=kw["object_prefix"],
            confirmation=None, grant_jti=None, status="active")
        return True

    server.repo.open_session = open_session
    await api_v2.open_session(request, device=device)
    return stored, opened


def _grant(server, device, jti, *, visit_id, patient="P0012345", doctor="DR0042"):
    server.repo.auths[jti] = dict(
        jti=jti, device_id=device["device_id"], hospital_id=device["hospital_id"],
        cmed_hospital_id="CMED-X", patient_id=patient, doctor_id=doctor,
        start_time="2026-11-19T10:52:00+06:00", visit_date="2026-11-19", notice_id=None,
        session_id=None, issued_at=NOW(), visit_id=visit_id)


@pytest.mark.asyncio
async def test_the_visit_id_is_on_the_recording_from_the_start(server, device_key, device):
    _grant(server, device, "jti-visit-1", visit_id="VIS-T-31508")
    sid = "01JB8XQ4M7YZ2K9V3N5P6R8T20"
    stored, opened = await _open(server, device_key, device, sid, grant="jti-visit-1")
    # Saved while the recording is still open - the dashboard shows it at once.
    assert server.repo.sessions[sid]["visit_id"] == "VIS-T-31508"
    local = opened.astimezone(DHAKA)
    assert stored["object_prefix"] == (
        f"{device['hospital_id']}/DR0042/{local:%Y-%m-%d}/P0012345_DR0042_VIS-T-31508"
        f"/{local:%H%M%S}_{sid[-5:]}")


@pytest.mark.asyncio
async def test_another_pcs_grant_does_not_name_the_visit(server, device_key, device):
    _grant(server, {**device, "device_id": "someone-else"}, "jti-visit-2",
           visit_id="VIS-T-99999")
    sid = "01JB8XQ4M7YZ2K9V3N5P6R8T21"
    stored, opened = await _open(server, device_key, device, sid, grant="jti-visit-2")
    assert not server.repo.sessions[sid].get("visit_id")
    local = opened.astimezone(DHAKA)
    assert stored["object_prefix"].endswith(
        f"/P0012345_DR0042_{local:%Y%m%dT%H%M%S}/{local:%H%M%S}_{sid[-5:]}")


@pytest.mark.asyncio
async def test_a_clinic_code_with_an_underscore_gets_a_readable_folder(server, device_key,
                                                                       device):
    karail = {**device, "hospital_id": "AALO_KARAIL"}
    server.repo.hospitals["AALO_KARAIL"] = None
    sid = "01JB8XQ4M7YZ2K9V3N5P6R8T22"
    stored, _ = await _open(server, device_key, karail, sid, hospital="AALO_KARAIL")
    assert stored["object_prefix"].startswith("AALO_KARAIL/DR0042/")
    assert sid not in stored["object_prefix"]


@pytest.mark.asyncio
async def test_the_pc_is_told_an_address_in_the_recordings_folder(server):
    sid = "01JB8XQ4M7YZ2K9V3N5P6R8T23"
    folder = "HOSP003/DR0042/2026-09-13/P0012345_DR0042_V1/101432_R8T23"
    server.repo.sessions[sid] = dict(
        session_id=sid, device_id=DEVICE["device_id"], doctor_id="DR0042",
        hospital_id="HOSP003", patient_id="P0012345", confirmation="legacy",
        opened_at=NOW(), session_date=NOW().date(), status="active", object_prefix=folder)
    reply = await api_v2.authorize_segment(api_v2.AuthorizeRequest(
        session_id=sid, seq_no=3, bytes=1000, sha256="ab" * 32), device=DEVICE)
    assert reply["object_key"] == f"audio/{folder}/piece_0003.wav"
    assert reply["upload_url"].endswith(f"audio/{folder}/piece_0003.wav")


# ============================================================
# The mover
# ============================================================

class Store:
    """The segment bucket: objects by key."""

    def __init__(self, objects):
        self.bucket, self.objects = "aimscribe-segments", dict(objects)
        self.corrupt = set()
        store = self

        class Client:
            def copy_object(self, bucket, target, source):
                data = store.objects[source.object_name]
                store.objects[target] = b"x" + data if target in store.corrupt else data

            def get_object(self, bucket, key):
                data = store.objects[key]
                return SimpleNamespace(stream=lambda n: [data], close=lambda: None,
                                       release_conn=lambda: None)

            def remove_object(self, bucket, key):
                store.objects.pop(key, None)

        self.client = Client()


class MoverRepo:
    def __init__(self, session, pieces, *, still_waiting=True):
        self.session, self.pieces = session, pieces
        self.still_waiting = still_waiting
        self.moved, self.alerts, self.audits = [], [], []

    async def pieces_waiting_to_move(self, limit):
        return [dict(self.session, pieces=len(self.pieces))]

    async def segments_for(self, session_id):
        return [dict(p) for p in self.pieces]

    async def get_authorisation(self, jti):
        return None

    async def hospital_timezone(self, hospital_id):
        return "Asia/Dhaka"

    async def move_pieces(self, session_id, moves, prefix):
        if not self.still_waiting:
            return False
        self.moved.append((moves, prefix))
        for seq, _old, new in moves:
            next(p for p in self.pieces if p["seq_no"] == seq)["object_key"] = new
        return True

    async def raise_alert(self, **kw):
        self.alerts.append(kw)

    async def audit(self, **kw):
        self.audits.append(kw)


def _waiting(n=3):
    old = f"audio/{SID}"
    data = {f"{old}/seg_{i:05d}.wav": f"audio {i}".encode() for i in range(1, n + 1)}
    pieces = [{"seq_no": i, "object_key": f"{old}/seg_{i:05d}.wav",
               "sha256": hashlib.sha256(f"audio {i}".encode()).digest(),
               "bytes": len(f"audio {i}"), "state": "committed", "object_deleted_at": None}
              for i in range(1, n + 1)]
    session = {"session_id": SID, "hospital_id": "AALO_KARAIL", "doctor_id": "DR4290373",
               "patient_id": "P4347510", "opened_at": OPENED, "object_prefix": None,
               "grant_jti": None, "visit_id": "VIS-T-31508", "segment_count": n}
    return session, pieces, data


NEW = "audio/AALO_KARAIL/DR4290373/2026-11-19/P4347510_DR4290373_VIS-T-31508/105225_JWRS5"


@pytest.fixture
def mover():
    yield
    api_v2.ctx.repo = api_v2.ctx.minio = None


@pytest.mark.asyncio
async def test_the_mover_moves_waiting_pieces_and_removes_the_old_ones(mover):
    session, pieces, data = _waiting()
    api_v2.ctx.repo, api_v2.ctx.minio = MoverRepo(session, pieces), Store(data)
    report = await segment_layout.move_waiting(apply=True)
    assert (report["recordings_moved"], report["pieces_moved"]) == (1, 3)
    assert sorted(api_v2.ctx.minio.objects) == [f"{NEW}/piece_000{i}.wav" for i in (1, 2, 3)]
    assert api_v2.ctx.minio.objects[f"{NEW}/piece_0002.wav"] == b"audio 2"
    assert api_v2.ctx.repo.moved[0][1] == NEW[len("audio/"):]
    assert api_v2.ctx.repo.audits[0]["event_type"] == "segments.moved"
    # Run again: nothing left to move.
    session["object_prefix"] = NEW[len("audio/"):]
    again = await segment_layout.move_waiting(apply=True)
    assert again["recordings_moved"] == 0 and not again["failed"]


@pytest.mark.asyncio
async def test_a_dry_run_lists_and_changes_nothing(mover):
    session, pieces, data = _waiting()
    api_v2.ctx.repo, api_v2.ctx.minio = MoverRepo(session, pieces), Store(data)
    report = await segment_layout.move_waiting(apply=False)
    assert report["would_move"][0]["to"] == f"{NEW}/"
    assert report["would_move"][0]["pieces"][0] == {
        "from": f"audio/{SID}/seg_00001.wav", "to": f"{NEW}/piece_0001.wav"}
    assert api_v2.ctx.minio.objects == data and not api_v2.ctx.repo.moved


@pytest.mark.asyncio
async def test_a_copy_that_is_not_the_piece_leaves_the_recording_as_it_was(mover):
    session, pieces, data = _waiting()
    store = Store(data)
    store.corrupt.add(f"{NEW}/piece_0002.wav")
    api_v2.ctx.repo, api_v2.ctx.minio = MoverRepo(session, pieces), store
    report = await segment_layout.move_waiting(apply=True)
    assert report["recordings_moved"] == 0
    assert "piece 2" in report["failed"][0]["why"]
    assert store.objects == data                       # the copies made are removed
    assert not api_v2.ctx.repo.moved
    assert api_v2.ctx.repo.alerts[0]["alert_type"] == "segment_move_failed"


@pytest.mark.asyncio
async def test_confirmed_during_the_move_the_old_pieces_stay_in_use(mover):
    session, pieces, data = _waiting()
    api_v2.ctx.repo = MoverRepo(session, pieces, still_waiting=False)
    api_v2.ctx.minio = Store(data)
    report = await segment_layout.move_waiting(apply=True)
    assert report["recordings_moved"] == 0
    assert "confirmed or changed" in report["skipped"][0]["why"]
    assert api_v2.ctx.minio.objects == data            # old pieces kept, copies removed


@pytest.mark.asyncio
async def test_a_recording_with_pieces_still_to_come_is_not_moved(mover):
    session, pieces, data = _waiting()
    session["segment_count"] = 5
    api_v2.ctx.repo, api_v2.ctx.minio = MoverRepo(session, pieces), Store(data)
    report = await segment_layout.move_waiting(apply=True)
    assert report["skipped"][0]["why"] == "3 of 5 pieces arrived"
    assert api_v2.ctx.minio.objects == data
