"""
3.9.9 (26 Nov 2026): the EHR page that sent API 1 travels with the grant
request, so the server can refuse a page it does not trust - the real server
takes recordings only from the clinics' own pages.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from api.protocol import Trigger



FIVE = {"patient_id": "P1", "doctor_id": "DR0042", "hospital_id": "CMED-DHK-BANANI-01",
        "start_time": "2026-09-13T10:14:32+06:00", "date": "2026-09-13"}


def start(**extra):
    return json.dumps({"command": "start", "request_id": "r1",
                       "trigger": {**FIVE, **extra}})


class Opened:
    def __init__(self):
        self.triggers = []

    async def open_session(self, trigger, *, grant=None):
        self.triggers.append(trigger)
        return {"session_id": "S1", "started_at": "2026-09-13T04:14:33Z",
                "authorisation": "pending", "confirmation": "confirming"}


def manager(make_security):
    from api.websocket_server import WebSocketManager
    m = WebSocketManager(SimpleNamespace(security=make_security(), app_version="3.9.9"))
    opened = Opened()
    m.set_controller(opened)
    return m, opened


def test_the_page_goes_with_the_grant_request_only_when_known():
    assert "origin" not in Trigger(**FIVE).fields()
    sent = Trigger(**FIVE, visit_id="VIS-A", origin="https://10.0.40.211").fields()
    assert sent["origin"] == "https://10.0.40.211" and sent["visit_id"] == "VIS-A"
    # A recording resumed after a restart is rebuilt from what was sent.
    assert Trigger(**sent).origin == "https://10.0.40.211"


async def test_a_socket_start_carries_its_page(make_security):
    m, opened = manager(make_security)
    socket = SimpleNamespace(headers={"origin": "https://karail.aaloclinic.org:8443"})
    reply = await m.handle_message(socket, start())
    assert reply["status"] in (200, 202)
    assert opened.triggers[-1].origin == "https://karail.aaloclinic.org:8443"


async def test_an_http_start_carries_its_page_and_a_page_cannot_name_another(make_security):
    m, opened = manager(make_security)
    await m.handle_message(object(), start(origin="https://10.0.40.211"),
                           origin="https://dev.aaloclinic.org")
    # The browser's Origin wins; a field in the message is not read.
    assert opened.triggers[-1].origin == "https://dev.aaloclinic.org"
    await m.handle_message(object(), start())
    assert opened.triggers[-1].origin == ""
