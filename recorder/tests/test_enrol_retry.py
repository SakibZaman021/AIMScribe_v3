"""
A PC installed while the clinic's internet was down enrols itself once it is back,
without waiting for someone to log off and on again.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from api import trigger_server as ts
from core import enrollment


class Identity(SimpleNamespace):
    pass


def runtime(monkeypatch, outcomes):
    """A Runtime with only what enrolment touches, and a scripted server."""
    monkeypatch.setattr(ts, "ENROL_RETRY_SECONDS", 0.01)
    monkeypatch.setattr(enrollment, "read_pending_token", lambda cfg: "a" * 43)
    monkeypatch.setattr(enrollment, "load_device_token", lambda cfg: "device-token")
    monkeypatch.setattr("core.uploader.build_ssl_context", lambda cfg: None)
    calls = []

    async def enroll(cfg, key, token, ssl_context=None):
        calls.append(token)
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(enrollment, "enroll", enroll)
    rt = object.__new__(ts.Runtime)
    rt.cfg, rt.device_key, rt.identity = object(), object(), None
    rt.problems = [ts.NOT_ENROLLED]
    rt.uploader = SimpleNamespace(token=None, nudged=0)
    rt.uploader.set_device_token = lambda t: setattr(rt.uploader, "token", t)
    rt.uploader.nudge = lambda: setattr(rt.uploader, "nudged", rt.uploader.nudged + 1)
    rt.controller = SimpleNamespace(device_id="", hospital_id="", doctor_id="")
    rt.ws = SimpleNamespace(set_register_source=lambda up, hospital: None)
    return rt, calls


async def test_it_keeps_trying_until_the_server_answers(monkeypatch):
    identity = Identity(device_id="D1", hospital_id="AALO_DHOLPUR", doctor_id="")
    rt, calls = runtime(monkeypatch, [
        enrollment.EnrollmentError("could not reach the backend"),
        enrollment.EnrollmentError("server rejected enrollment (503)"),
        identity])
    await rt._enrol_until_done()
    assert len(calls) == 3
    assert (rt.controller.device_id, rt.controller.hospital_id) == ("D1", "AALO_DHOLPUR")
    assert rt.uploader.token == "device-token" and rt.uploader.nudged == 1
    assert rt.problems == [] and rt.identity is identity


async def test_a_refused_token_is_not_tried_for_ever(monkeypatch):
    rt, calls = runtime(monkeypatch, [
        enrollment.EnrollmentError("server rejected enrollment (401)", rejected=True)])
    await rt._enrol_until_done()
    assert len(calls) == 1 and rt.identity is None
    assert rt.problems == [ts.NOT_ENROLLED]


def test_only_a_4xx_counts_as_refused():
    assert enrollment.EnrollmentError("x", rejected=True).rejected
    assert not enrollment.EnrollmentError("x").rejected
