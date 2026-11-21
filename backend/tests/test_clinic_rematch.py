"""
CMED's real traffic was never confirmed (found on the live server, 17 Nov 2026).

CMED's PC (DESKTOP-E5GGS9R) was enrolled at our clinic TEST, whose CMED code
is CMED-TEST - the code Postman sends. CMED's own system names its clinic
"Susastho-Clinic-Test-1". Until 17 Nov that code was linked to no clinic, so
API 2 and API 3 were stored but left no notice; from 17 Nov it was linked to
HOSP_SUSASTHO_TEST, so API 2 filed its notice there while every grant from the
PC was filed under TEST (a clinic mismatch, decision D1). The five fields
matched to the second; the clinics never did. 19 recordings waited, due to be
erased on 23-24 Nov.

These tests are that case, and what putting the set-up right now does.
"""
from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND / "src"))
sys.path.insert(0, str(BACKEND / "tests"))

import api_v2                                   # noqa: E402

from test_v3_server import (DEVICE, FIELDS, api2, mint, open_session,  # noqa: E402,F401
                            prescription, send, server)

pytestmark = pytest.mark.asyncio

SUSASTHO = "CMED-NEW-SITE"                       # the CMED key in the harness speaks for it
CMED_PC = dict(DEVICE, device_id="cmed-pc", hospital_id="TEST")
VISIT = {**FIELDS, "hospital_id": SUSASTHO, "visit_id": "VIS-T-31496"}


def live_layout(server, *, linked=True):
    """TEST answers to CMED-TEST; CMED's real code to its own clinic, or to none yet."""
    server.repo.hospitals.update({"TEST": "CMED-TEST",
                                  "HOSP_SUSASTHO_TEST": SUSASTHO if linked else None})
    server.repo.device_clinics = {"cmed-pc": "TEST"}


async def record(server, fields=VISIT, *, segments=2):
    """CMED's page starts the recorder: a grant, then the session it opens."""
    _, reply = await mint({k: v for k, v in fields.items()}, device=CMED_PC)
    sid = open_session(server, device=CMED_PC, segments=segments)
    server.repo.sessions[sid]["hospital_id"] = reply["hospital_id"]
    await api_v2._link_grant(sid, reply["jti"], fields["patient_id"], CMED_PC)
    return sid, reply


def move_pc(server, clinic="HOSP_SUSASTHO_TEST"):
    server.repo.device_clinics["cmed-pc"] = clinic


async def rematch():
    return await api_v2.rematch_after_clinic_fix(now=True)


# ---------------------------------------------------------------- the failure, as it was

async def test_the_live_case_never_confirms_while_the_pc_sits_at_test(server):
    live_layout(server)
    await send("patient_information", api2(**VISIT))        # 11:50:41, filed at Susastho
    sid, reply = await record(server)                       # 11:52:10, filed at TEST
    assert reply["clinic_mismatch"] is True and reply["hospital_id"] == "TEST"
    assert reply["confirmation"] == "pending"
    assert server.repo.notices[0]["hospital_id"] == "HOSP_SUSASTHO_TEST"
    assert server.repo.alerts[-1]["detail"]["fix"] == "move this PC to clinic HOSP_SUSASTHO_TEST"

    # Nothing changes by itself: another clinic's API 2 confirms nothing (AT-63).
    assert await rematch() == []
    assert server.repo.sessions[sid]["confirmation"] == "pending"
    assert server.repo.notices[0]["claimed_by_jti"] is None


# ---------------------------------------------------------------- putting it right

async def test_moving_the_pc_confirms_what_waited_and_files_it_at_the_right_clinic(server):
    live_layout(server)
    await send("patient_information", api2(**VISIT))
    sid, reply = await record(server)

    move_pc(server)
    assert await rematch() == [sid]
    session = server.repo.sessions[sid]
    assert session["confirmation"] == "confirmed"
    assert session["hospital_id"] == "HOSP_SUSASTHO_TEST"
    assert server.repo.auths[reply["jti"]]["hospital_id"] == "HOSP_SUSASTHO_TEST"
    assert server.repo.notices[0]["claimed_by_jti"] == reply["jti"]
    entry = next(a for a in server.repo.audits if a["event_type"] == "session.confirmed")
    assert entry["detail"] == {"rematched": True, "clinic": "HOSP_SUSASTHO_TEST",
                               "filed_at_before": "TEST"}
    # The interface-4 visit row names its recording.
    assert server.clinical.visit_links[-1] == {
        "cmed_hospital_id": SUSASTHO, "visit_id": "VIS-T-31496",
        "patient_id": FIELDS["patient_id"], "session_id": sid}
    # And once is enough.
    assert await rematch() == []


async def test_messages_sent_before_the_code_was_linked_still_confirm(server):
    """16 Nov: Susastho-Clinic-Test-1 was linked to no clinic, so no notice was kept."""
    live_layout(server, linked=False)
    await send("patient_information", api2(**VISIT))
    await send("prescription", prescription(**VISIT))
    assert server.repo.notices == []                        # stored, confirms nothing
    sid, _ = await record(server)

    server.repo.hospitals["HOSP_SUSASTHO_TEST"] = SUSASTHO   # the code is linked ...
    assert await rematch() == []                            # ... the PC is still at TEST
    move_pc(server)                                         # ... and now it is moved
    assert await rematch() == [sid]
    notice = server.repo.notices[0]
    assert notice["hospital_id"] == "HOSP_SUSASTHO_TEST"
    api2_record = next(r for r in server.clinical.records if r["kind"] == "patient_information")
    assert notice["clinical_record_id"] == api2_record["id"]   # API 2 preferred over API 3


async def test_linking_the_code_to_the_pcs_own_clinic_works_too(server):
    """The other way to put it right: CMED's code is this PC's clinic's code."""
    live_layout(server, linked=False)
    server.repo.hospitals["TEST"] = None
    await send("patient_information", api2(**VISIT))
    sid, _ = await record(server)
    server.repo.hospitals["TEST"] = SUSASTHO
    assert await rematch() == [sid]
    assert server.repo.sessions[sid]["hospital_id"] == "TEST"


async def test_without_any_message_from_cmed_nothing_is_confirmed(server):
    live_layout(server)
    sid, _ = await record(server)
    move_pc(server)
    assert await rematch() == []
    assert server.repo.sessions[sid]["confirmation"] == "pending"


async def test_a_different_start_second_is_still_another_visit(server):
    live_layout(server)
    await send("patient_information", api2(**{**VISIT, "start_time": "2026-09-13T10:14:33+06:00"}))
    sid, _ = await record(server)
    move_pc(server)
    assert await rematch() == []


async def test_one_message_confirms_the_newest_recording_of_the_visit(server):
    """P4347460 on 17 Nov: two recordings of one visit (1 piece, then 22)."""
    live_layout(server)
    await send("patient_information", api2(**VISIT))
    first, _ = await record(server, segments=1)
    second, _ = await record(server, segments=22)
    for sid in (first, second):                             # the newer grant is issued later
        server.repo.auths[server.repo.sessions[sid]["grant_jti"]]["issued_at"] += (
            timedelta(seconds=1) if sid == second else timedelta(0))
    move_pc(server)
    # CMED's message is claimed by the newest; the earlier one is the same
    # visit from the same PC, and is confirmed with it (17 Nov decision).
    assert await rematch() == [second, first]
    jti_of = {sid: server.repo.sessions[sid]["grant_jti"] for sid in (first, second)}
    assert server.repo.notices[0]["claimed_by_jti"] == jti_of[second]
    assert server.repo.sessions[first]["confirmation"] == "confirmed"


async def test_a_refused_or_erased_recording_is_left_alone(server):
    live_layout(server)
    await send("patient_information", api2(**VISIT))
    sid, _ = await record(server)
    server.repo.sessions[sid]["confirmation"] = "refused"
    move_pc(server)
    assert await rematch() == []


async def test_the_sweep_rematches_every_few_minutes_not_every_pass(server, monkeypatch):
    calls = []

    async def counted(limit=500):
        calls.append(limit)
        return []
    import clinical
    monkeypatch.setattr(clinical, "rematch_waiting", counted)
    monkeypatch.setattr(api_v2, "_LAST_REMATCH", None)
    await api_v2.rematch_after_clinic_fix()
    await api_v2.rematch_after_clinic_fix()
    assert len(calls) == 1
    await api_v2.rematch_after_clinic_fix(now=True)          # a move or a link: at once
    assert len(calls) == 2


async def test_a_failing_rematch_never_fails_its_caller(server, monkeypatch):
    async def broken(limit=500):
        raise RuntimeError("database gone")
    import clinical
    monkeypatch.setattr(clinical, "rematch_waiting", broken)
    assert await api_v2.rematch_after_clinic_fix(now=True) == []


# ---------------------------------------------------------------- the same, on PostgreSQL

try:
    from test_db_integration import a_session, dbs, enrolled_device, pg  # noqa: E402,F401
    HAVE_POSTGRES = True
except pytest.skip.Exception:
    HAVE_POSTGRES = False

postgres = pytest.mark.skipif(not HAVE_POSTGRES, reason="pgserver is not installed")


@postgres
@pytest.mark.parametrize("linked_first", [True, False])
async def test_the_live_case_on_postgres(dbs, linked_first):
    """
    The PC enrolled at HOSP003; CMED's real code names HOSP_SUS. `linked_first`:
    the code was linked before API 2 came (17 Nov), or only afterwards (16 Nov).
    """
    import json as _json
    import uuid
    import clinical
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from grants import GrantIssuer

    repo = dbs.repo
    device = await enrolled_device(repo)
    code = f"Susastho-{uuid.uuid4().hex[:8]}"
    await repo.upsert_hospital("HOSP_SUS", "Susastho test", "Asia/Dhaka")
    await repo.set_cmed_hospital_id("HOSP_SUS", code if linked_first else None)
    key = await repo.create_cmed_key(label="cmed-real", created_by="tests", expires_at=None,
                                     cmed_hospital_ids=[code])
    api_v2.forget_directory()
    api_v2.ctx.repo, api_v2.ctx.clinical = repo, dbs.clinical
    api_v2.ctx.grants = GrantIssuer(Ed25519PrivateKey.generate())
    try:
        fields = {"patient_id": f"P{uuid.uuid4().hex[:7]}", "doctor_id": "DR4290371",
                  "hospital_id": code, "start_time": "2026-11-17T11:50:39+06:00",
                  "date": "2026-11-17"}

        class Request:
            headers = {"x-cmed-key": key}

            async def body(self):
                return _json.dumps({**fields, "demographics": {"sex": "male",
                                                               "age_years": 34}}).encode()

        assert (await clinical.receive(Request(), "patient_information")).status_code == 202
        minted = await api_v2.mint_grant(dict(fields), device=device)
        assert minted["clinic_mismatch"] is True and minted["hospital_id"] == "HOSP003"
        sid = await a_session(repo, device)
        assert await api_v2._link_grant(sid, minted["jti"], fields["patient_id"],
                                        device) == "pending"

        assert await clinical.rematch_waiting() == []            # nothing until it is put right
        if not linked_first:
            await repo.set_cmed_hospital_id("HOSP_SUS", code)
            assert await clinical.rematch_waiting() == []        # linked, PC still elsewhere
        await repo.move_device(device["device_id"], "HOSP_SUS", register_again=False)
        assert await clinical.rematch_waiting() == [sid]

        session = await repo.get_session(sid)
        grant = await repo.get_authorisation(minted["jti"])
        assert (session["confirmation"], session["hospital_id"]) == ("confirmed", "HOSP_SUS")
        assert grant["hospital_id"] == "HOSP_SUS" and grant["notice_id"] is not None
        assert await clinical.rematch_waiting() == []            # once
    finally:
        api_v2.ctx.repo = api_v2.ctx.clinical = api_v2.ctx.grants = None
        api_v2.forget_directory()


# ---------------------------------------------------------------- the visit's other recordings

async def confirm_same_visit():
    return await api_v2.confirm_same_visit_safely()


async def test_every_recording_of_a_confirmed_visit_from_the_same_pc_is_kept(server):
    """VIS-T-31492 on 16 Nov: five starts for one patient; the 16-piece one waited to be erased."""
    live_layout(server)
    await send("patient_information", api2(**VISIT))
    first, _ = await record(server, segments=16)
    second, _ = await record(server, segments=2)
    move_pc(server)
    await rematch()
    states = {sid: server.repo.sessions[sid]["confirmation"] for sid in (first, second)}
    assert sorted(states.values()) == ["confirmed", "confirmed"]
    for sid in (first, second):
        assert server.repo.sessions[sid]["hospital_id"] == "HOSP_SUSASTHO_TEST"
    entry = [a for a in server.repo.audits if a["event_type"] == "session.confirmed"
             and "same_visit_as" in a["detail"]]
    assert len(entry) == 1


async def test_a_restart_in_normal_running_is_confirmed_by_the_sweep(server):
    """API 2 first, then two starts at the right clinic: the second used to wait for ever."""
    await send("patient_information", api2())
    _, one = await mint()
    sid1 = open_session(server)
    await api_v2._link_grant(sid1, one["jti"], "P0012345", DEVICE)
    _, two = await mint()
    sid2 = open_session(server)
    assert await api_v2._link_grant(sid2, two["jti"], "P0012345", DEVICE) == "pending"
    assert await confirm_same_visit() == [sid2]
    assert server.repo.sessions[sid2]["confirmation"] == "confirmed"
    assert await confirm_same_visit() == []


async def test_another_pcs_recording_of_the_visit_is_never_taken_in(server):
    other = dict(DEVICE, device_id="dev-2")
    await send("patient_information", api2())
    _, one = await mint()
    sid1 = open_session(server)
    await api_v2._link_grant(sid1, one["jti"], "P0012345", DEVICE)
    _, two = await mint(device=other)
    sid2 = open_session(server, device=other)
    await api_v2._link_grant(sid2, two["jti"], "P0012345", other)
    assert await confirm_same_visit() == []
    assert server.repo.sessions[sid2]["confirmation"] == "pending"


async def test_a_refused_visit_keeps_its_other_recordings_out(server, monkeypatch):
    """VIS-T-31493: the patient refused; their other recordings must not be confirmed."""
    await send("patient_information", api2())
    _, one = await mint()
    sid1 = open_session(server)
    await api_v2._link_grant(sid1, one["jti"], "P0012345", DEVICE)
    _, two = await mint()
    sid2 = open_session(server)
    await api_v2._link_grant(sid2, two["jti"], "P0012345", DEVICE)

    async def refused(digest):
        return True
    monkeypatch.setattr(server.repo, "refused_visit", refused)
    assert await confirm_same_visit() == []


@postgres
async def test_a_visits_other_recordings_on_postgres(dbs):
    """The SQL of waiting_same_visit: same PC, same five fields, filed with the confirmed one."""
    import uuid
    import clinical
    from datetime import datetime, timezone
    from test_db_integration import visit as a_visit

    repo = dbs.repo
    device = await enrolled_device(repo)
    api_v2.ctx.repo, api_v2.ctx.clinical = repo, dbs.clinical
    try:
        v = a_visit(patient_id=f"P{uuid.uuid4().hex[:7]}")
        notice = await repo.record_notice(v, hospital_id="HOSP003", clinical_record_id=1)
        sids = []
        for n in range(3):
            jti = uuid.uuid4().hex
            await repo.create_authorisation(jti=jti, device_id=device["device_id"],
                                            hospital_id="HOSP003", visit=v,
                                            expires_at=datetime.now(timezone.utc))
            sid = await a_session(repo, device)
            await repo.link_authorisation(jti, sid)
            if n == 0:
                assert await repo.claim_notice(notice, jti)
                await repo.set_session_confirmation(sid, "confirmed", grant_jti=jti)
            else:
                await repo.set_session_confirmation(sid, "unconfirmed", grant_jti=jti)
            sids.append(sid)

        assert sorted(await clinical.confirm_same_visit()) == sorted(sids[1:])
        for sid in sids:
            assert (await repo.get_session(sid))["confirmation"] == "confirmed"
        assert await clinical.confirm_same_visit() == []
    finally:
        api_v2.ctx.repo = api_v2.ctx.clinical = None


# ------------------------------------------- CMED's message taken by a grant never used
#
# VIS-T-31509, 21 Nov 2026: a new PC joined the server 11 s into its first
# recording. The grant issued at that moment claimed CMED's one API 2; the PC
# opened the recording only at 09:30, under a new grant, and recorded the
# visit again at 11:53. Both waited to be erased: no confirmed recording
# existed to vouch for them.

def unused_grant_expires(server, jti, *, ago=timedelta(seconds=1)):
    server.repo.auths[jti]["expires_at"] = NOW_() - ago


def NOW_():
    from test_v3_server import NOW
    return NOW()


async def the_31509_case(server, *, device=DEVICE):
    await send("patient_information", api2())               # 00:02:39
    _, taken = await mint()                                   # 00:02:47: claims it, never used
    assert taken["confirmation"] == "confirmed"
    _, later = await mint(device=device)                      # 09:30
    sid = open_session(server, device=device)
    assert await api_v2._link_grant(sid, later["jti"], "P0012345", device) == "pending"
    return sid, taken


async def test_a_message_taken_by_a_grant_never_used_confirms_the_pcs_recordings(server):
    sid, taken = await the_31509_case(server)
    unused_grant_expires(server, taken["jti"])
    _, again = await mint()                                   # 11:53, the same visit again
    sid2 = open_session(server)
    await api_v2._link_grant(sid2, again["jti"], "P0012345", DEVICE)

    assert sorted(await confirm_same_visit()) == sorted([sid, sid2])
    for s in (sid, sid2):
        assert server.repo.sessions[s]["confirmation"] == "confirmed"
    why = [a["detail"] for a in server.repo.audits if a["event_type"] == "session.confirmed"]
    assert why == [{"notice_used_by_unused_grant": taken["jti"], "clinic": "HOSP003"}] * 2
    assert await confirm_same_visit() == []                   # once


async def test_a_grant_still_valid_may_yet_become_its_recording(server):
    sid, taken = await the_31509_case(server)
    unused_grant_expires(server, taken["jti"], ago=-timedelta(seconds=30))   # not expired
    assert await confirm_same_visit() == []
    assert server.repo.sessions[sid]["confirmation"] == "pending"


async def test_an_unused_grant_on_another_pc_vouches_for_nothing(server):
    other = dict(DEVICE, device_id="dev-2")
    sid, taken = await the_31509_case(server, device=other)
    unused_grant_expires(server, taken["jti"])
    assert await confirm_same_visit() == []
    assert server.repo.sessions[sid]["confirmation"] == "pending"


async def test_a_refusal_still_keeps_the_visit_out(server, monkeypatch):
    sid, taken = await the_31509_case(server)
    unused_grant_expires(server, taken["jti"])

    async def refused(digest):
        return True
    monkeypatch.setattr(server.repo, "refused_visit", refused)
    assert await confirm_same_visit() == []


async def test_a_grant_that_did_become_a_recording_is_not_unused(server):
    """Its recording waits too (not confirmed): nothing vouches for the visit."""
    sid, taken = await the_31509_case(server)
    unused_grant_expires(server, taken["jti"])
    other = open_session(server)
    server.repo.sessions[other]["confirmation"] = "refused"
    server.repo.sessions[other]["grant_jti"] = taken["jti"]
    server.repo.auths[taken["jti"]]["session_id"] = other
    assert await confirm_same_visit() == []


@postgres
async def test_a_message_taken_by_a_grant_never_used_on_postgres(dbs):
    import uuid
    import clinical
    from datetime import datetime, timezone
    from test_db_integration import visit as a_visit

    repo = dbs.repo
    device, elsewhere = await enrolled_device(repo), await enrolled_device(repo)
    api_v2.ctx.repo, api_v2.ctx.clinical = repo, dbs.clinical
    now = datetime.now(timezone.utc)
    try:
        async def case(*, expires, later_device=device):
            v = a_visit(patient_id=f"P{uuid.uuid4().hex[:7]}")
            notice = await repo.record_notice(v, hospital_id="HOSP003", clinical_record_id=1)
            taken = uuid.uuid4().hex
            await repo.create_authorisation(jti=taken, device_id=device["device_id"],
                                            hospital_id="HOSP003", visit=v, expires_at=expires)
            assert await repo.claim_notice(notice, taken)
            jti = uuid.uuid4().hex
            await repo.create_authorisation(jti=jti, device_id=later_device["device_id"],
                                            hospital_id="HOSP003", visit=v, expires_at=now)
            sid = await a_session(repo, later_device)
            await repo.link_authorisation(jti, sid)
            await repo.set_session_confirmation(sid, "unconfirmed", grant_jti=jti)
            return sid, taken

        waits_valid, _ = await case(expires=now + timedelta(minutes=5))
        waits_other, _ = await case(expires=now - timedelta(minutes=1), later_device=elsewhere)
        kept, taken = await case(expires=now - timedelta(minutes=1))

        assert await clinical.confirm_same_visit() == [kept]
        assert (await repo.get_session(kept))["confirmation"] == "confirmed"
        for sid in (waits_valid, waits_other):
            assert (await repo.get_session(sid))["confirmation"] == "unconfirmed"
        assert await clinical.confirm_same_visit() == []
    finally:
        api_v2.ctx.repo = api_v2.ctx.clinical = None
