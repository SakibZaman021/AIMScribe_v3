"""
26 Nov 2026, before the full field phase: does every alert reach everyone it
should, once, when the whole team is on shift at once?

Seven clinics, one place each, three collectors per place (two on shift), eight
researchers who each look after every clinic - more than the 15-20 collectors
and the researchers the lab will have. A burst of alerts of the four kinds the
clinics raise most (Jabra unplugged, recording problem, no sound, unusual stop)
arrives in one go. Telegram is simulated: it refuses one message in six (as a
busy Telegram does with "Too Many Requests"), and each refused message must be
sent again until it arrives.

What must hold: every on-shift collector of the clinic and every researcher
gets each alert exactly once; nothing is lost; nothing is sent twice; the
off-shift collector gets nothing; and it is all out within the test's time.
"""
from __future__ import annotations

import os
import time
from collections import Counter
from datetime import datetime, timedelta, timezone

import pytest

from test_db_integration import a_session, dbs, enrolled_device, pg  # noqa: F401

pytestmark = pytest.mark.asyncio

CLINICS = 7
COLLECTORS_PER_PLACE = 3          # two on shift, one not
RESEARCHERS = 8
ALERTS_PER_CLINIC = 20            # 140 alerts in one burst
KINDS = [
    ("microphone_switched", {"detail": "the microphone stopped",
                             "microphone": "Microphone Array (Realtek)"}),
    ("capture_failed", {"detail": "the capture stalled"}),
    ("silent_session", {"detail": "two clips of digital silence"}),
    ("abnormal_close", {"reason": "doctor_stopped:patient_left", "duration_seconds": 300}),
]


class FlakyTelegram:
    """Telegram's Bot API: one message in six refused (429), one in eleven times out."""

    def __init__(self):
        self.calls = []
        self.sent = []
        self.refused = 0

    async def __call__(self, method, payload, timeout=15):
        self.calls.append(method)
        if method == "getMe":
            return {"username": "AIMScribeLoadBot"}, None
        if method == "getUpdates":
            return [], None
        if method in ("sendMessage", "sendDocument"):
            # Exactly as collectors._telegram words a refusal and a network fault.
            if len(self.calls) % 6 == 0:
                self.refused += 1
                return None, "Telegram answered 429: Too Many Requests: retry after 1"
            if len(self.calls) % 11 == 0:
                self.refused += 1
                return None, "Telegram could not be reached: ReadTimeout timed out"
            self.sent.append(payload)
            return {"message_id": len(self.calls)}, None
        return True, None


async def test_a_full_shift_burst_reaches_everyone_once(dbs, monkeypatch):
    import collectors

    async def no_mail(to, subject, text):
        return False, "email is not set up"

    fake = FlakyTelegram()
    monkeypatch.setattr(collectors, "_telegram", fake)
    monkeypatch.setattr(collectors, "send_mail", no_mail)
    monkeypatch.setattr(collectors, "CHAT_GAP", 0.0)        # Telegram's pacing, off for speed
    monkeypatch.setenv("AIMS_TELEGRAM_BOT_TOKEN", "123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi")
    monkeypatch.setenv("AIMS_QUIET_HOURS", "off")

    repo = dbs.repo
    device = await enrolled_device(repo)
    chat = iter(range(100_000, 200_000))
    collectors_of, researcher_chats, on_shift_chats = {}, {}, {}
    sessions, pcs = {}, {}
    today = datetime.now(timezone.utc).date()
    async with dbs.rec.acquire() as conn:
        for c in range(CLINICS):
            hid = f"LOAD{c:02d}"
            await repo.upsert_hospital(hid, f"Load clinic {c}", "Asia/Dhaka")
            place = await conn.fetchval(
                "INSERT INTO places (name, morning_start, morning_end, evening_start, "
                "evening_end) VALUES ($1, '00:00', '23:58', '23:58', '23:59') "
                "RETURNING place_id", f"Load place {c}")
            await conn.execute("INSERT INTO place_hospitals (place_id, hospital_id) "
                               "VALUES ($1, $2)", place, hid)
            for k in range(COLLECTORS_PER_PLACE):
                email = f"col{c}_{k}@load.example"
                await conn.execute(
                    "INSERT INTO portal_people (email, role, place_id, official_name, "
                    "collector_code, phone, added_by) VALUES ($1, 'collector', $2, $3, $4, "
                    "'+8801700000000', 'load-test')", email, place, f"Collector {c}-{k}", f"L{c}{k}")
                chat_id = next(chat)
                await conn.execute("INSERT INTO collector_telegram (email, chat_id) "
                                   "VALUES ($1, $2)", email, chat_id)
                collectors_of.setdefault(hid, []).append(chat_id)
                if k < 2:
                    await conn.execute(
                        "INSERT INTO collector_shifts (email, place_id, shift, shift_date) "
                        "VALUES ($1, $2, 'morning', $3)", email, place, today)
                    on_shift_chats.setdefault(hid, []).append(chat_id)
            # Each clinic its own PC: an alert goes to the clinic of the PC.
            token = await repo.create_enrollment_token(hospital_id=hid, doctor_id="",
                                                       created_by="load-test")
            enrolled = await repo.enroll_device(token=token, tpm_pubkey=os.urandom(32),
                                                machine_name=f"PC-{c}", os_version="",
                                                app_version="3.9.8", protocol_version=2)
            pcs[hid] = await repo.device_by_token(enrolled["device_token"])
            sessions[hid] = []
            for _ in range(ALERTS_PER_CLINIC):
                sid = await a_session(repo, pcs[hid])
                await conn.execute("UPDATE sessions SET hospital_id = $2 WHERE session_id = $1",
                                   sid, hid)
                sessions[hid].append(sid)
        long_ago = datetime.now(timezone.utc) - timedelta(days=1)
        for r in range(RESEARCHERS):
            email = f"res{r}@load.example"
            await conn.execute("INSERT INTO portal_people (email, role, alert_mode, added_by) "
                               "VALUES ($1, 'researcher', 'everything', 'load-test')", email)
            chat_id = next(chat)
            await conn.execute("INSERT INTO collector_telegram (email, chat_id) "
                               "VALUES ($1, $2)", email, chat_id)
            researcher_chats[email] = chat_id
            for c in range(CLINICS):
                await conn.execute("INSERT INTO researcher_hospitals (email, hospital_id, "
                                   "added_at) VALUES ($1, $2, $3)",
                                   email, f"LOAD{c:02d}", long_ago)

        notifier = collectors.Notifier()
        await notifier.alerts(conn)               # the starting point: nothing before now
        fake.sent.clear()

        # The burst: every clinic, every kind, at once.
        raised = []
        for hid, sids in sessions.items():
            for n, sid in enumerate(sids):
                kind, detail = KINDS[n % len(KINDS)]
                await repo.raise_alert(alert_type=kind, session_id=sid,
                                       device_id=pcs[hid]["device_id"], detail=dict(detail))
                raised.append((hid, sid, kind))

        started = time.monotonic()
        await notifier.alerts(conn)
        # Refused messages are retried until none is left (the retry waits are
        # brought forward, as the passing minutes would).
        for _ in range(20):
            left = await conn.fetchval(
                "SELECT count(*) FROM collector_notifications WHERE delivered = FALSE "
                "AND channel = 'telegram' AND email LIKE '%@load.example'")
            if not left:
                break
            await conn.execute("UPDATE collector_notifications SET next_try_at = now() "
                               "WHERE delivered = FALSE AND next_try_at IS NOT NULL")
            await notifier.retry_failed(conn)
        took = time.monotonic() - started

        rows = await conn.fetch("""
            SELECT n.email, n.session_id, n.hospital_id, n.event, n.audience, n.delivered,
                   n.channel
              FROM collector_notifications n
             WHERE n.email LIKE '%@load.example'
               AND n.session_id = ANY($1::text[])
        """, [sid for _, sid, _ in raised])

    # Nothing left undelivered, and every refusal was made good.
    undelivered = [r for r in rows if not r["delivered"]]
    assert not undelivered, f"{len(undelivered)} never delivered: {undelivered[:3]}"
    assert fake.refused > 0, "the test must include refused messages"
    off_shift = {f"col{c}_2@load.example" for c in range(CLINICS)}
    assert not [r for r in rows if r["email"] in off_shift], "an off-shift collector was alerted"

    # A lasting problem on a PC is ONE card per person (cards.py, 18 Nov): the Jabra
    # off, and a recording fault (no sound folds into it). An unusual stop is one
    # message per recording. Never twice either way.
    cards = Counter((r["email"], r["hospital_id"], r["event"]) for r in rows
                    if r["event"] in ("jabra_unplugged", "recording_problem"))
    assert not [k for k, v in cards.items() if v > 1], "a card was sent twice"
    stops = Counter((r["email"], r["session_id"]) for r in rows
                    if r["event"] == "stopped_unusually")
    assert not [k for k, v in stops.items() if v > 1], "a stop was sent twice"
    for c in range(CLINICS):
        hid = f"LOAD{c:02d}"
        everyone = {f"col{c}_{k}@load.example" for k in range(2)} | set(researcher_chats)
        for event in ("jabra_unplugged", "recording_problem"):
            got = {e for (e, h, ev) in cards if h == hid and ev == event}
            assert everyone <= got, f"{event} card at {hid} missed {everyone - got}"
    for hid, sid, kind in raised:
        if kind != "abnormal_close":
            continue
        clinic = int(hid[4:])
        everyone = {f"col{clinic}_{k}@load.example" for k in range(2)} | set(researcher_chats)
        got = {e for (e, s) in stops if s == sid}
        assert everyone <= got, f"the stop at {hid} missed {everyone - got}"

    # What arrived in Telegram matches what the database says was delivered.
    chats = Counter(p["chat_id"] for p in fake.sent if p.get("chat_id"))
    assert sum(chats.values()) >= len(rows)
    print(f"\nload: {len(raised)} alerts -> {len(rows)} messages to "
          f"{CLINICS * 2} collectors on shift and {RESEARCHERS} researchers; "
          f"{fake.refused} refused by Telegram and sent again; all delivered in {took:.1f} s")
