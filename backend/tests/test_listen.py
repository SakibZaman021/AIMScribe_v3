"""
Listening on the dashboard (26 Nov 2026).

A researcher or an admin plays a recording in the page - play, pause, drag
to any point - without downloading it. The point of each test: the bytes a
player asks for are exactly those bytes of the archived WAV, stitched from as
many worker answers as it takes; the people who may hear it are the people
who may download it; every listen is in the audit log, once; and a listen is
not a download (no portal_downloads row, no daily limit).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND / "src"))
sys.path.insert(0, str(BACKEND / "tests"))

try:
    from test_db_integration import (_DL_WAV, _copied_recording, _download_app,  # noqa: E402
                                     _researcher, _sign_in, _unserve, dbs, pg)  # noqa: F401
    HAVE_POSTGRES = True
except pytest.skip.Exception:
    HAVE_POSTGRES = False

pytestmark = [pytest.mark.asyncio,
              pytest.mark.skipif(not HAVE_POSTGRES, reason="pgserver is not installed")]

SAME = {"Sec-Fetch-Site": "same-origin"}
H = {"X-AIMS-Portal": "1"}
WINDOW = 1000                      # the stand-in worker answers at most this many bytes


def _range_worker(calls):
    """The archive worker's /range, standing in: slices the WAV, a window at a time."""
    import httpx

    def worker(request):
        assert request.url.path == "/range", request.url.path
        assert request.headers["X-Worker-Key"] == "the-worker-key"
        ask = json.loads(request.content)
        calls.append(ask)
        size, start, end = len(_DL_WAV), ask["start"], ask.get("end")
        if start >= size:
            return httpx.Response(416, json={"detail": "past the end"},
                                  headers={"X-Plain-Size": str(size)})
        stop = min(size - 1, start + WINDOW - 1, size - 1 if end is None else end)
        return httpx.Response(200, content=_DL_WAV[start:stop + 1], headers={
            "X-Plain-Size": str(size), "X-Start": str(start), "X-End": str(stop)})
    return worker


async def test_the_player_gets_exactly_the_bytes_it_asks_for(dbs, monkeypatch):
    sid, _ = await _copied_recording(dbs, "PLISTEN1")
    import downloads
    # A machine started minutes ago (the clock counts from its start): the
    # first listen is still logged.
    monkeypatch.setattr(downloads, "_now", lambda: 100.0)
    calls: list = []
    who = {"value": "owner@uiu.example"}
    size = len(_DL_WAV)
    async with _download_app(dbs, monkeypatch, who, _range_worker(calls)) as client:
        await _sign_in(client)
        url = f"/api/v2/dashboard/recordings/{sid}/listen"
        ok = await client.get(url, params={"check": 1}, headers=SAME)
        assert ok.status_code == 200 and ok.json()["ok"] is True
        assert not calls                                      # a check decrypts nothing

        whole = await client.get(url, headers=SAME)
        assert whole.status_code == 200 and whole.content == _DL_WAV
        assert whole.headers["accept-ranges"] == "bytes"
        assert whole.headers["cache-control"] == "no-store"
        assert len(calls) == -(-size // WINDOW)               # stitched from every window

        for asked, first, last in (("bytes=0-", 0, size - 1),
                                   ("bytes=5000-", 5000, size - 1),
                                   ("bytes=100-2500", 100, 2500),
                                   ("bytes=7000-7000", 7000, 7000),
                                   ("bytes=-100", size - 100, size - 1),
                                   ("bytes=9000-99999", 9000, size - 1)):
            got = await client.get(url, headers={**SAME, "Range": asked})
            assert got.status_code == 206, asked
            assert got.headers["content-range"] == f"bytes {first}-{last}/{size}", asked
            assert got.content == _DL_WAV[first:last + 1], asked

        past = await client.get(url, headers={**SAME, "Range": f"bytes={size}-"})
        assert past.status_code == 416 and past.headers["content-range"] == f"bytes */{size}"

        # From another site: refused, nothing decrypted.
        before = len(calls)
        assert (await client.get(url, headers={"Sec-Fetch-Site": "cross-site",
                                               "Range": "bytes=0-"})).status_code == 403
        assert len(calls) == before
    try:
        async with dbs.rec.acquire() as conn:
            heard = await conn.fetchval(
                "SELECT count(*) FROM audit_log WHERE session_id = $1 "
                "AND event_type = 'recording.listened'", sid)
            downloads = await conn.fetchval(
                "SELECT count(*) FROM portal_downloads WHERE session_id = $1", sid)
        assert heard == 1                    # once per person, recording and half hour
        assert downloads == 0                # a listen is not a download
    finally:
        _unserve()


async def test_only_those_who_may_download_may_listen(dbs, monkeypatch):
    sid, _ = await _copied_recording(dbs, "PLISTEN2")
    calls: list = []
    who = {"value": ""}
    async with _download_app(dbs, monkeypatch, who, _range_worker(calls)) as client:
        url = f"/api/v2/dashboard/recordings/{sid}/listen"
        # Not signed in.
        assert (await client.get(url, headers={**SAME, "Accept": "application/json"})
                ).status_code == 401

        # A researcher of another hospital: refused, in words the page shows.
        await _researcher(client, who, "elsewhere@uni.example", hospitals=("HOSP777",))
        for params in ({"check": 1}, {}):
            refused = await client.get(url, params=params, headers={
                **SAME, "Accept": "application/json", "Range": "bytes=0-"})
            assert refused.status_code == 403
            assert refused.json()["detail"].startswith("Not your hospital: HOSP003")

        # CMED staff: never the audio.
        client.cookies.clear()
        who["value"] = "owner@uiu.example"
        await _sign_in(client)
        await client.post("/api/v2/admin/people", headers=H,
                          json={"email": "staff@cmed.example", "role": "cmed"})
        client.cookies.clear()
        who["value"] = "staff@cmed.example"
        await _sign_in(client)
        cmed = await client.get(url, params={"check": 1},
                                headers={**SAME, "Accept": "application/json"})
        assert cmed.status_code == 403 and "not the audio" in cmed.json()["detail"]
        assert not calls

        # Their own hospital: yes.
        client.cookies.clear()
        await _researcher(client, who, "own@uni.example", hospitals=("HOSP003",))
        got = await client.get(url, headers={**SAME, "Range": "bytes=10-20"})
        assert got.status_code == 206 and got.content == _DL_WAV[10:21]
    _unserve()


async def test_a_worker_that_refuses_the_copy_is_shown_not_played(dbs, monkeypatch):
    import httpx
    sid, _ = await _copied_recording(dbs, "PLISTEN3")

    def worker(request):
        return httpx.Response(409, json={"detail": "the copy of x does not decrypt"})

    who = {"value": "owner@uiu.example"}
    async with _download_app(dbs, monkeypatch, who, worker) as client:
        await _sign_in(client)
        got = await client.get(f"/api/v2/dashboard/recordings/{sid}/listen",
                               headers={**SAME, "Accept": "application/json",
                                        "Range": "bytes=0-"})
        assert got.status_code == 409 and "did not pass its check" in got.json()["detail"]
    _unserve()


async def test_recordings_filter_by_morning_or_evening_shift(dbs, monkeypatch):
    """26 Nov 2026: the Recordings panel's Shift menu - by the clinic's clock when
    the recording opened, before or after 14:30 (the midday summary)."""
    from datetime import date, datetime, timezone

    from test_db_integration import _bulk_recording, _portal_app
    day = date(2026, 8, 9)
    morning = await _bulk_recording(dbs, "PSHIFTM", "DRSHIFT", day, 1)   # 10:01 Dhaka
    evening = await _bulk_recording(dbs, "PSHIFTE", "DRSHIFT", day, 2)
    edge = await _bulk_recording(dbs, "PSHIFTX", "DRSHIFT", day, 3)
    async with dbs.rec.acquire() as conn:
        await conn.execute("UPDATE sessions SET opened_at = $2 WHERE session_id = $1",
                           evening, datetime(2026, 8, 9, 11, 0, tzinfo=timezone.utc))  # 17:00
        await conn.execute("UPDATE sessions SET opened_at = $2 WHERE session_id = $1",
                           edge, datetime(2026, 8, 9, 8, 30, tzinfo=timezone.utc))     # 14:30
    monkeypatch.setenv("TZ", "Asia/Dhaka")
    monkeypatch.delenv("AIMS_MIDDAY_SUMMARY_AT", raising=False)
    who = {"value": "owner@uiu.example"}
    sel = {"from": day.isoformat(), "to": day.isoformat(), "hospital_id": "HOSP003"}
    async with _portal_app(dbs, monkeypatch, google_email=who) as client:
        await _sign_in(client)

        async def ids(**more):
            got = await client.get("/api/v2/dashboard/recordings", params={**sel, **more})
            assert got.status_code == 200, got.text
            return {r["session_id"] for r in got.json()["recordings"]}

        assert await ids() == {morning, evening, edge}
        assert await ids(shift="morning") == {morning}
        assert await ids(shift="evening") == {evening, edge}
        assert await ids(shift="nonsense") == {morning, evening, edge}
        facets = (await client.get("/api/v2/dashboard/recordings/facets",
                                   params={**sel, "shift": "evening"})).json()
        assert facets["counts"]["total"] == 2
        assert facets["doctors"] == [{"doctor_id": "DRSHIFT", "recordings": 2}]
    _unserve()
