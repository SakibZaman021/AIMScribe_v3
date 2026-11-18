"""
One Telegram card per problem, and one pinned live board per hospital (18 Nov 2026).

Why: on 18 Nov one unplugged Jabra on one PC made 38 messages for one
researcher in two hours - a new alert for every consultation, each with its
own "not seen" and "still not fixed" - and the refusals and the pause that
mattered were lost among them.

    card     The first alert of a problem that lasts (the Jabra off, a recording
             fault, a PC not reporting, a recording paused or running long) is
             a short card. It is EDITED as things change - a new consultation
             on that PC, someone pressing Seen, 30 / 60 / 90 minutes, fixed -
             instead of new messages. New consultations while it is open make
             no new alert (collectors.notify folds them into the card).
    replies  "Nobody pressed Seen" and the 60-minute "still not fixed" are sent
             as replies under the card, with sound; "fixed" without sound.
    board    Each researcher has one pinned message per hospital and shift:
             what is recording or paused now, the open problems, this shift's
             counts, and what CMED has not sent. Edited when it changes.

Nothing here deletes anything; every alert is still a row in
collector_notifications and on the dashboard.
"""
from __future__ import annotations

import hashlib
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# What each lasting problem is called on its card, and what the collector does.
TITLES = {"jabra": "Jabra off", "recording": "Recording problem", "pc": "PC not reporting",
          "paused": "Recording paused", "long": "Recording not stopped"}
DO = {"jabra": "Ask the doctor, between patients, to plug the Jabra in.",
      "recording": "Check the PC and the Jabra; call AIMS LAB if it does not recover.",
      "pc": "Check the PC is on, awake and online. Recordings wait on the PC.",
      "paused": "Ask the doctor if the pause is still needed: Resume records again.",
      "long": "Ask the doctor, between patients, to stop it."}
# The Jabra came out in the middle of a consultation: now, not between patients.
DO_NOW = "Go to the room and plug the Jabra back in; the recording carries on meanwhile."
CARD_FAMILIES = frozenset(TITLES)
PARAGRAPH = "\n\n"
# Problems where it matters how many patients went by while it lasted.
COUNTED = frozenset({"jabra", "recording"})


def _family(event: Optional[str]) -> Optional[str]:
    import watchers
    family = watchers.FAMILY.get(event or "")
    return family if family in CARD_FAMILIES else None


def is_card(note: Dict[str, Any]) -> bool:
    """The first alert of a lasting problem, to a collector or a researcher."""
    return (_family(note.get("event")) is not None
            and note.get("audience") in ("collector", "watcher")
            and not str(note.get("event_key") or "").startswith(("esc:", "still:", "fixed:")))


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]


async def where_words(conn, *, session_id=None, device_id=None, hospital_id=None) -> str:
    """'TEST · DESKTOP-S3AS8QK · DRTEST01': which clinic, PC and doctor, in one line."""
    s = None
    if session_id:
        s = await conn.fetchrow("SELECT doctor_id, hospital_id, device_id FROM sessions "
                                "WHERE session_id = $1", session_id)
    dev_id = (s["device_id"] if s else None) or device_id
    dev = await conn.fetchrow("SELECT machine_name, hospital_id, doctor_id FROM devices "
                              "WHERE device_id = $1", dev_id) if dev_id is not None else None
    if s is None and dev_id is not None:
        s = await conn.fetchrow("SELECT doctor_id, hospital_id, device_id FROM sessions "
                                "WHERE device_id = $1 AND opened_at > now() - interval '12 hours' "
                                "ORDER BY opened_at DESC LIMIT 1", dev_id)
    parts = [(s["hospital_id"] if s else None) or (dev["hospital_id"] if dev else None)
             or hospital_id, dev["machine_name"] if dev else None,
             (s["doctor_id"] if s else None) or (dev["doctor_id"] if dev else None)]
    return " · ".join(str(p) for p in parts if p)


async def line(conn, at: Optional[datetime], what: str, *, session_id=None, device_id=None,
               hospital_id=None) -> str:
    """One silent line for a researcher: '12:21 · TEST · PC · DRTEST01 - what happened'."""
    import collectors
    where = await where_words(conn, session_id=session_id, device_id=device_id,
                              hospital_id=hospital_id)
    return f"{collectors._hhmm(at or datetime.now(timezone.utc))} · {where} - {what}"


# ============================================================ the card

async def _consultations(conn, first: Dict[str, Any]) -> Optional[str]:
    """How many consultations began on this PC since the problem did, and the latest."""
    import collectors
    if first.get("device_id") is None:
        return None
    since = None
    if first.get("session_id"):
        since = await conn.fetchval("SELECT opened_at FROM sessions WHERE session_id = $1",
                                    first["session_id"])
    since = since or first.get("happened_at") or first["created_at"]
    row = await conn.fetchrow("""
        SELECT count(*) AS n, max(opened_at) AS latest,
               bool_or(closed_at IS NULL AND opened_at > now() - interval '3 hours'
                       AND confirmation IS DISTINCT FROM 'refused') AS running
          FROM sessions WHERE device_id = $1 AND opened_at >= $2
    """, first["device_id"], since - timedelta(seconds=1))
    if not row or not row["n"]:
        return None
    return (f"Consultations since: {row['n']} (latest started "
            f"{collectors._hhmm(row['latest'])}{', recording now' if row['running'] else ''})")


async def view(conn, first: Dict[str, Any],
               final: Optional[Tuple[str, str, int]] = None) -> Dict[str, Any]:
    """What a problem's card says now: the same for everyone who has it."""
    import collectors
    import watchers
    family = _family(first.get("event")) or "recording"
    now = datetime.now(timezone.utc)
    key = first["event_key"]
    started = first["created_at"]
    if final is not None:
        state, how, minutes = final
    else:
        done = await conn.fetchrow("""
            SELECT resolved_at, resolution FROM collector_notifications
             WHERE event_key = $1 AND resolved_at IS NOT NULL LIMIT 1
        """, key)
        if done is not None:
            state, how = "over", done["resolution"] or "it is over"
            minutes = max(0, int((done["resolved_at"] - started).total_seconds() // 60))
        else:
            try:
                state, how = await watchers._still_wrong(conn, family, first, now)
            except Exception as exc:                # the card is still sent
                logger.warning("Could not look at problem %s: %s", key, exc)
                state, how = "still", ""
            minutes = max(0, int((now - started).total_seconds() // 60))
    return {
        "family": family, "state": state, "how": how, "minutes": minutes,
        "started": started, "now": now,
        "where": await where_words(conn, session_id=first.get("session_id"),
                                   device_id=first.get("device_id"),
                                   hospital_id=first.get("hospital_id")),
        "consultations": await _consultations(conn, first) if family in COUNTED else None,
        "seen": await watchers._seen_text(conn, [key, f"esc:{key}"], started, key),
        "hhmm": collectors._hhmm,
        "event": first.get("event"),
        # What happened, as the first alert said it (its first paragraph).
        "what": (first.get("body") or "").split(PARAGRAPH)[0].strip()[:300],
    }


def render(v: Dict[str, Any], audience: str) -> str:
    hhmm = v["hhmm"]
    open_now = v["state"] == "still"
    icon = "\U0001F534" if open_now else ("✅" if v["state"] in ("fixed", "over")
                                         else "⏹")
    lines = [f"{icon} {TITLES[v['family']]} · {v['where']}"]
    if v.get("what"):
        lines.append(v["what"])
    if v["how"]:
        lines.append(v["how"][:1].upper() + v["how"][1:] + ".")
    lines.append(f"Started {hhmm(v['started'])}"
                 + (f", {v['minutes']} min ago." if open_now and v["minutes"] else "."))
    if v["consultations"]:
        lines.append(v["consultations"])
    lines.append(v["seen"])
    if open_now:
        if v["minutes"] >= 30:
            lines.append(f"Still not fixed after {v['minutes']} min.")
        if audience == "collector":
            lines.append(DO_NOW if v.get("event") == "jabra_unplugged" else DO[v["family"]])
    elif v["state"] == "fixed":
        lines.append(f"Fixed by {hhmm(v['now'])}, after {v['minutes']} min.")
    elif v["state"] == "over":
        lines.append(f"Over after {v['minutes']} min.")
    else:
        lines.append(f"Ended by {hhmm(v['now'])} after {v['minutes']} min, not fixed.")
    return "\n".join(lines)[:4000]


async def text_for(conn, note: Dict[str, Any]) -> str:
    """A new card's text for this person. It has only just happened, so it is open,
    whatever a PC not yet heard from would otherwise suggest."""
    v = await view(conn, note)
    if v["state"] != "still":
        v.update(state="still", how="")
    return render(v, note.get("audience") or "collector")


def _markup(note: Dict[str, Any], open_now: bool) -> Optional[Dict[str, Any]]:
    if note.get("audience") != "collector" or not note.get("urgent"):
        return None
    if not open_now:
        return {"inline_keyboard": []}
    if note.get("seen_at"):
        return {"inline_keyboard": [[{"text": "✅ Seen", "callback_data": "done"}]]}
    return {"inline_keyboard": [[{"text": "✅ Seen - I am on it",
                                  "callback_data": f"seen:{note['id']}"}]]}


def base_key(event_key: str) -> str:
    """The problem's first alert key, from a reminder, escalation or fixed key."""
    key = event_key or ""
    if key.startswith("esc:"):
        return key[4:]
    if key.startswith("fixed:"):
        return key[6:]
    if key.startswith("still:"):
        return key[6:].rsplit(":", 1)[0]
    return key


async def refresh(conn, event_key: str,
                  final: Optional[Tuple[str, str, int]] = None) -> int:
    """Edit every card of this problem to say how it stands now. Returns edits made."""
    import collectors
    key = base_key(event_key)
    rows = await conn.fetch("""
        SELECT id, email, event, event_key, audience, urgent, created_at, happened_at,
               device_id, session_id, place_id, hospital_id, tg_chat_id, tg_message_id,
               card_sha, seen_at, body
          FROM collector_notifications
         WHERE event_key = $1 AND tg_message_id IS NOT NULL
         ORDER BY audience, id
    """, key)
    if not rows:
        return 0
    first = dict(rows[0])
    first["created_at"] = min(r["created_at"] for r in rows)
    v = await view(conn, first, final)
    edits = 0
    for r in rows:
        note = dict(r)
        if not _family(note["event"]):
            continue
        text = render(v, note["audience"])
        markup = _markup(note, v["state"] == "still")
        digest = sha(text + ("|seen" if note.get("seen_at") else "")
                     + ("|open" if v["state"] == "still" else ""))
        if digest == note["card_sha"]:
            continue
        payload: Dict[str, Any] = {"chat_id": note["tg_chat_id"],
                                   "message_id": note["tg_message_id"], "text": text,
                                   "disable_web_page_preview": True}
        if markup is not None:
            payload["reply_markup"] = markup
        result, error = await collectors._telegram("editMessageText", payload)
        if result is not None or (error and "not modified" in error):
            await conn.execute("UPDATE collector_notifications SET card_sha = $2 WHERE id = $1",
                               note["id"], digest)
            edits += 1
        else:
            logger.info("Card %s could not be edited: %s", note["id"], error)
    return edits


# ============================================================ the live board

BOARD_EVERY = 30.0
_BOARD = {"at": 0.0}


async def _shift_now(conn, hospital_id: str, now: datetime) -> Optional[Tuple[str, str]]:
    """(day, morning|evening) while this hospital is in its shift hours and not quiet."""
    import collectors
    import watchers
    if await watchers.quiet_for(conn, hospital_id, now=now) or \
            not await watchers.in_shift_hours(conn, hospital_id, now=now):
        return None
    local = now.astimezone(collectors.zone())
    return local.date().isoformat(), ("morning" if local.time() < watchers.midday_time()
                                      else "evening")


async def board_text(conn, hospital_id: str, day: str, shift: str,
                     now: Optional[datetime] = None) -> Tuple[str, str]:
    """The board's text, and the part of it that decides whether it changed."""
    import api_v2
    import collectors
    import watchers
    from datetime import date as _date
    now = now or datetime.now(timezone.utc)
    hhmm = collectors._hhmm
    start, _, _ = watchers.shift_window(_date.fromisoformat(day), shift)
    name = await conn.fetchval("SELECT name FROM hospitals WHERE hospital_id = $1",
                               hospital_id) or ""
    out = [f"\U0001F4CB {hospital_id} {name} · {shift} shift · live".replace("  ", " ")]

    live = await conn.fetch("""
        SELECT s.doctor_id, s.opened_at, s.paused_since, s.pause_reason, d.machine_name,
               d.microphone, d.microphone_is_clinic, d.last_seen_at
          FROM sessions s LEFT JOIN devices d ON d.device_id = s.device_id
         WHERE s.hospital_id = $1 AND s.closed_at IS NULL
           AND s.opened_at > now() - interval '12 hours'
           AND s.confirmation IS DISTINCT FROM 'refused'
         ORDER BY s.opened_at
    """, hospital_id)
    out.append("Now:")
    if not live:
        out.append("- nothing recording")
    for r in live:
        who = f"{r['machine_name'] or 'a PC'} ({r['doctor_id'] or '?'})"
        if r["last_seen_at"] is not None and r["last_seen_at"] < now - collectors.SILENT_AFTER:
            out.append(f"- {who}: PC not reporting since {hhmm(r['last_seen_at'])}")
        elif r["paused_since"] is not None:
            out.append(f"- {who}: PAUSED since {hhmm(r['paused_since'])}"
                       + (f" ({r['pause_reason'].replace('_', ' ')})"
                          if r["pause_reason"] else ""))
        else:
            mic = ("Jabra" if r["microphone_is_clinic"] else
                   (r["microphone"] or "PC microphone") if r["microphone_is_clinic"] is False
                   else "microphone not reported")
            out.append(f"- {who}: recording since {hhmm(r['opened_at'])}, {mic}")

    problems = await conn.fetch("""
        SELECT DISTINCT ON (n.event_key) n.event_key, n.event, n.subject, n.created_at
          FROM collector_notifications n
         WHERE n.hospital_id = $1 AND n.audience = 'collector' AND n.resolved_at IS NULL
           AND n.channel IS DISTINCT FROM 'held' AND n.event = ANY($2::text[])
           AND n.created_at > now() - $3::interval
         ORDER BY n.event_key, n.created_at
    """, hospital_id, [e for e, f in watchers.FAMILY.items() if f in CARD_FAMILIES],
        watchers.WATCH_FOR)
    out.append("Open problems:" if problems else "Open problems: none")
    for p in sorted(problems, key=lambda p: p["created_at"]):
        out.append(f"- {p['subject'].split(' - ')[0]} (since {hhmm(p['created_at'])})")

    recs = await conn.fetch("""
        SELECT s.session_id, s.close_reason, s.closed_at, s.paused_seconds, s.paused_since,
               s.confirmation, s.patient_id,
               EXISTS (SELECT 1 FROM session_refusals x WHERE x.session_id = s.session_id)
                   AS refused,
               (SELECT count(*) FROM chain_entries c WHERE c.session_id = s.session_id
                  AND c.entry_type = 'pause'
                  AND COALESCE(c.payload->>'reason', '') <> ALL($3::text[])) AS pauses,
               g.cmed_hospital_id AS cmed_code,
               COALESCE(s.visit_id, g.visit_id) AS cmed_visit_id,
               g.start_time AS cmed_start_time
          FROM sessions s
          LEFT JOIN grant_authorisations g ON g.jti = s.grant_jti AND g.patient_id = s.patient_id
         WHERE s.hospital_id = $1 AND s.opened_at >= $2
    """, hospital_id, start, sorted(api_v2.INTERNAL_PAUSES))
    normal = refused = 0
    unusual: Dict[str, int] = {}
    pauses = 0
    paused_s = 0.0
    for r in recs:
        if r["refused"] or r["confirmation"] == "refused":
            refused += 1
        elif r["closed_at"] is not None:
            kind = collectors.stop_kind(r["close_reason"])
            if kind is None:
                normal += 1
            else:
                label = collectors.STOP_LABELS.get(kind, kind.replace("_", " "))
                unusual[label.split(" (")[0]] = unusual.get(label.split(" (")[0], 0) + 1
        pauses += int(r["pauses"] or 0)
        if r["pauses"]:
            paused_s += float(r["paused_seconds"] or 0)
        if r["closed_at"] is None and r["paused_since"] is not None:
            paused_s += max(0.0, (now - r["paused_since"]).total_seconds())
    counts = [f"{len(recs)} recordings", f"{normal} normal"]
    if refused:
        counts.append(f"{refused} refused")
    counts += [f"{n} {what}" for what, n in sorted(unusual.items(), key=lambda kv: -kv[1])]
    if pauses:
        counts.append(f"{pauses} pause{'s' if pauses != 1 else ''} "
                      f"({round(paused_s / 60)} min)")
    out.append("This shift: " + " · ".join(counts))

    # What CMED has not sent, for recordings that ended over 10 minutes ago.
    done = [r for r in recs if r["closed_at"] is not None and not r["refused"]
            and r["confirmation"] != "refused" and r["cmed_code"]
            and r["closed_at"] < now - timedelta(minutes=10)]
    if done:
        try:
            import dashboard
            times = await dashboard._cmed_message_times(done)
            no2 = sum(1 for r in done if not times.get(r["session_id"], {}).get("api2_at"))
            no3 = sum(1 for r in done if not times.get(r["session_id"], {}).get("api3_at"))
            out.append(f"CMED (of {len(done)} ended): API 2 missing {no2} · API 3 missing {no3}")
        except Exception as exc:
            logger.warning("Board: could not read CMED's messages: %s", exc)

    duty = []
    for p in await watchers.on_shift(conn, hospital_id):
        duty += [collectors.person_label(c) for c in p["on_shift"]]
    out.append("Collector on shift: " + (", ".join(duty) if duty else "nobody"))
    body = "\n".join(out)
    return body + f"\nUpdated {hhmm(now)}", sha(body)


async def boards(conn, now: Optional[datetime] = None, force: bool = False) -> int:
    """Every researcher's pinned board for each hospital they look after, kept current."""
    import collectors
    if not collectors.telegram_ready():
        return 0
    if not force and time.monotonic() - _BOARD["at"] < BOARD_EVERY:
        return 0
    _BOARD["at"] = time.monotonic()
    now = now or datetime.now(timezone.utc)
    people = await conn.fetch("""
        SELECT r.email, r.hospital_id, t.chat_id
          FROM researcher_hospitals r
          JOIN portal_people p ON p.email = r.email AND p.removed_at IS NULL
                              AND p.role IN ('researcher', 'admin')
          JOIN collector_telegram t ON t.email = r.email
         WHERE r.removed_at IS NULL
         ORDER BY r.hospital_id, r.email
    """)
    texts: Dict[str, Optional[Tuple[str, str, str]]] = {}
    changed = 0
    for p in people:
        hid = p["hospital_id"]
        if hid not in texts:
            shift = await _shift_now(conn, hid, now)
            if shift is None:
                texts[hid] = None
            else:
                text, digest = await board_text(conn, hid, shift[0], shift[1], now)
                texts[hid] = (f"{shift[0]}:{shift[1]}", text, digest)
        if texts[hid] is None:
            continue
        key, text, digest = texts[hid]
        try:
            changed += await _one_board(conn, p["email"], hid, p["chat_id"], key, text, digest)
        except Exception as exc:
            logger.warning("Board for %s could not be updated: %s", hid, collectors._hide(str(exc)))
    return changed


async def _one_board(conn, email: str, hospital_id: str, chat_id: int, key: str,
                     text: str, digest: str) -> int:
    import collectors
    row = await conn.fetchrow("SELECT chat_id, message_id, text_sha FROM telegram_boards "
                              "WHERE email = $1 AND hospital_id = $2 AND shift_key = $3",
                              email, hospital_id, key)
    if row is not None and row["chat_id"] == chat_id:
        if row["text_sha"] == digest:
            return 0
        result, error = await collectors._telegram("editMessageText", {
            "chat_id": chat_id, "message_id": row["message_id"], "text": text,
            "disable_web_page_preview": True})
        if result is None and not (error and "not modified" in error):
            if error and ("not found" in error or "can't be edited" in error):
                row = None                       # deleted by them: send a new one below
            else:
                return 0
        if row is not None:
            await conn.execute("UPDATE telegram_boards SET text_sha = $4, updated_at = now() "
                               "WHERE email = $1 AND hospital_id = $2 AND shift_key = $3",
                               email, hospital_id, key, digest)
            return 1
    await collectors._pace_chat(chat_id)
    result, error = await collectors._telegram("sendMessage", {
        "chat_id": chat_id, "text": text, "disable_web_page_preview": True,
        "disable_notification": True})
    if not isinstance(result, dict) or not result.get("message_id"):
        logger.info("Board for %s could not be sent: %s", hospital_id, error)
        return 0
    message_id = int(result["message_id"])
    # The board before this one comes off the pins; this one goes on, silently.
    before = await conn.fetchrow("""
        SELECT chat_id, message_id FROM telegram_boards
         WHERE email = $1 AND hospital_id = $2 AND shift_key <> $3
         ORDER BY created_at DESC LIMIT 1
    """, email, hospital_id, key)
    if before is not None:
        await collectors._telegram("unpinChatMessage", {"chat_id": before["chat_id"],
                                                        "message_id": before["message_id"]})
    await collectors._telegram("pinChatMessage", {"chat_id": chat_id, "message_id": message_id,
                                                  "disable_notification": True})
    await conn.execute("""
        INSERT INTO telegram_boards (email, hospital_id, shift_key, chat_id, message_id, text_sha)
        VALUES ($1, $2, $3, $4, $5, $6)
        ON CONFLICT (email, hospital_id, shift_key) DO UPDATE
           SET chat_id = EXCLUDED.chat_id, message_id = EXCLUDED.message_id,
               text_sha = EXCLUDED.text_sha, updated_at = now()
    """, email, hospital_id, key, chat_id, message_id, digest)
    return 1


__all__ = ["is_card", "text_for", "refresh", "line", "where_words", "boards", "board_text",
           "base_key", "TITLES"]
