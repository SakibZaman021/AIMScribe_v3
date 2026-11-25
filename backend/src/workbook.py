"""
The shift summary as one Excel workbook per hospital (20 Nov 2026).

The lab's design, agreed sheet by sheet: the old summary CSV mixed recordings
and alerts in one list, so nothing could be read at a glance, and every
researcher got every hospital. Now each researcher gets, for each hospital
they look after, one workbook of four sheets:

    1 Summary          numbers only, one row per doctor and a total
    2 Audio files      one block per audio file (its details in merged
                       cells) with a row per pause and stop beside it, and
                       where its audio is: on the doctor's PC, pieces on the
                       server, archived, cloud copy
    3 Missing from CMED  the files whose API 2 or API 3 never came, with the
                       five fields CMED matches a visit on (no names)
    4 Alerts per audio file  one block per file, a row per alert; then the
                       alerts that belong to no recording, by PC

Every sheet opens with the same header (hospital, shift, time range, the
collector on shift). Red: missing or not confirmed; amber: paused or not on
the Jabra; green: confirmed. Nothing here writes to the database.
"""
from __future__ import annotations

import io
import json
import logging
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# The recorder pauses by itself while the Stop form is open (older recorders)
# and while Windows asks to shut down: not the doctor's pause.
INTERNAL_PAUSES = ("stop_requested", "pc_shutdown_pending")

PAUSE_WORDS = {
    "patient_declined": "Patient asked to pause",
    "sensitive_personal_matter": "Sensitive personal matter",
    "non_clinical_interruption": "Interruption not about the patient",
    "other": "Other",
}

# Stopped early, one Summary column each (the reasons the PC's Stop form offers).
EARLY_COLUMNS = (("patient_left", "Patient left"),
                 ("urgent_interruption", "Emergency or interruption"),
                 ("technical", "Technical"),
                 ("other", "Other"))
TECHNICAL = {"technical_problem", "recovered_after_unclean_shutdown", "pc_shutdown",
             "agent_shutdown"}

# What counts as an alert about a recording, for sheet 4: what people were
# told, once per alert (not each person's copy, nor reminders or lines).
ALERT_EVENTS = ("jabra_unplugged", "jabra_not_plugged", "jabra_back", "recording_problem",
                "recording_without_jabra", "stopped_unusually", "stopped_technical",
                "recording_too_long", "still_paused", "pc_silent", "no_shift_started",
                "pc_not_on", "patients_not_recorded", "silent_recording",
                "repeated_stops", "repeated_unusual")
ALERT_NAMES = {
    "jabra_unplugged": "Jabra unplugged", "jabra_not_plugged": "Jabra not plugged in",
    "jabra_back": "Jabra back", "recording_problem": "Recording problem",
    "recording_without_jabra": "Recorded without the Jabra",
    "stopped_unusually": "Stopped unusually", "stopped_technical": "Stopped: technical",
    "recording_too_long": "Recording not stopped", "still_paused": "Still paused after 2 min",
    "pc_not_on": "PC not on today", "patients_not_recorded": "Patients not recorded",
    "silent_recording": "Recording is silent",
    "pc_silent": "PC not reporting", "no_shift_started": "Nobody started the shift",
    "repeated_stops": "Repeated unusual stops",
    "repeated_unusual": "Same unusual event 3 times: contact the manager",
}


# ============================================================ pauses

def _when(value) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def pair_pauses(entries: List[Dict[str, Any]], closed_at: Optional[datetime] = None,
                now: Optional[datetime] = None) -> List[Dict[str, Any]]:
    """
    A recording's pauses the doctor chose, in order: when each began, how long
    it lasted and the reason. From its pause / resume chain entries (each a
    dict with entry_type and payload). A pause with no resume lasted until the
    recording ended (or until now, while it is still open).
    """
    out: List[Dict[str, Any]] = []
    current: Optional[Dict[str, Any]] = None
    internal = False
    for e in entries:
        payload = e.get("payload") or {}
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except ValueError:
                payload = {}
        if e.get("entry_type") == "pause":
            reason = str(payload.get("reason") or "")
            internal = reason in INTERNAL_PAUSES
            if internal:
                current = None
                continue
            current = {"at": _when(payload.get("at")) or _when(e.get("occurred_at")),
                       "reason": reason, "detail": str(payload.get("reason_detail") or ""),
                       "seconds": None}
            out.append(current)
        elif e.get("entry_type") == "resume":
            if current is not None and not internal:
                try:
                    current["seconds"] = float(payload.get("paused_seconds"))
                except (TypeError, ValueError):
                    resumed = _when(payload.get("at"))
                    if resumed and current["at"]:
                        current["seconds"] = (resumed - current["at"]).total_seconds()
            current, internal = None, False
    for p in out:
        if p["seconds"] is None and p["at"] is not None:
            end = closed_at or now or datetime.now(timezone.utc)
            p["seconds"] = max(0.0, (end - p["at"]).total_seconds())
            p["open"] = closed_at is None
    return out


def pause_words(p: Dict[str, Any]) -> str:
    words = PAUSE_WORDS.get(p["reason"], p["reason"].replace("_", " ").capitalize() or "No reason")
    return f"{words}: {p['detail']}" if p.get("detail") else words


# ============================================================ gathering

async def _collectors_on_shift(conn, hospital_id: str, start: datetime, end: datetime) -> str:
    import collectors
    rows = await conn.fetch("""
        SELECT DISTINCT """ + collectors.label_sql("p", "c.email") + """ AS who
          FROM collector_shifts c
          JOIN place_hospitals ph ON ph.place_id = c.place_id AND ph.hospital_id = $1
               AND ph.removed_at IS NULL
          LEFT JOIN portal_people p ON p.email = c.email
         WHERE c.started_at < $3 AND COALESCE(c.ended_at, now()) > $2
    """, hospital_id, start, end)
    return ", ".join(sorted(r["who"] for r in rows)) or "nobody"


async def gather(conn, hospital_id: str, start: datetime, end: datetime) -> Dict[str, Any]:
    """Everything the four sheets show, for one hospital and one time range."""
    import collectors
    import dashboard
    now = datetime.now(timezone.utc)
    recs = [dict(r) for r in await conn.fetch("""
        SELECT s.session_id, s.file_stem, s.patient_id, s.doctor_id, s.hospital_id,
               s.opened_at, s.closed_at, s.total_duration_seconds, s.close_reason,
               s.confirmation, s.segment_count, s.archived_at, s.copied_at, s.microphones,
               s.manifest->>'close_detail' AS close_detail, s.device_id,
               COALESCE(s.visit_id, g.visit_id) AS cmed_visit_id,
               g.cmed_hospital_id AS cmed_code, g.start_time AS cmed_start_time,
               d.machine_name AS pc,
               (SELECT r.refused_at FROM session_refusals r
                 WHERE r.session_id = s.session_id LIMIT 1) AS refused_at,
               (SELECT count(*) FROM segments x WHERE x.session_id = s.session_id) AS pieces,
               (SELECT count(*) FROM integrity_alerts a WHERE a.session_id = s.session_id
                  AND a.alert_type = 'microphone_switched') AS switches,
               EXISTS (SELECT 1 FROM integrity_alerts a WHERE a.session_id = s.session_id
                         AND a.alert_type = 'silent_session') AS no_sound,
               EXISTS (SELECT 1 FROM devices dv, jsonb_array_elements(
                           COALESCE(dv.held_sessions, '[]'::jsonb)) h
                        WHERE dv.device_id = s.device_id
                          AND h->>'session_id' = s.session_id) AS held
          FROM sessions s
          LEFT JOIN grant_authorisations g ON g.jti = s.grant_jti AND g.patient_id = s.patient_id
          LEFT JOIN devices d ON d.device_id = s.device_id
         WHERE s.hospital_id = $1 AND s.opened_at >= $2 AND s.opened_at < $3
         ORDER BY s.opened_at
    """, hospital_id, start, end)]
    ids = [r["session_id"] for r in recs]

    chain: Dict[str, List[Dict[str, Any]]] = {}
    if ids:
        for c in await conn.fetch("""
            SELECT session_id, entry_type, payload, occurred_at FROM chain_entries
             WHERE session_id = ANY($1::text[]) AND entry_type IN ('pause', 'resume')
             ORDER BY session_id, entry_no
        """, ids):
            chain.setdefault(c["session_id"], []).append(dict(c))

    try:
        times = await dashboard._cmed_message_times(recs)
    except Exception as exc:                       # the workbook goes without them
        logger.warning("Workbook: could not read CMED's messages: %s", exc)
        times = {}

    # One row per alert (its first copy), with who pressed Seen and when it ended.
    alerts = [dict(a) for a in await conn.fetch("""
        SELECT DISTINCT ON (n.event_key) n.event_key, n.event, n.subject, n.created_at,
               n.session_id, n.device_id, d.machine_name AS pc,
               (SELECT min(m.seen_at) FROM collector_notifications m
                 WHERE m.event_key IN (n.event_key, 'esc:' || n.event_key)
                   AND m.audience = 'collector') AS seen_at,
               (SELECT """ + collectors.label_sql("p", "m.email") + """
                  FROM collector_notifications m LEFT JOIN portal_people p ON p.email = m.email
                 WHERE m.event_key IN (n.event_key, 'esc:' || n.event_key)
                   AND m.audience = 'collector' AND m.seen_at IS NOT NULL
                 ORDER BY m.seen_at LIMIT 1) AS seen_by,
               (SELECT min(m.resolved_at) FROM collector_notifications m
                 WHERE m.event_key = n.event_key) AS resolved_at,
               (SELECT min(m.resolution) FROM collector_notifications m
                 WHERE m.event_key = n.event_key) AS resolution,
               (SELECT count(DISTINCT m.event_key) FROM collector_notifications m
                 WHERE m.event_key LIKE 'still:' || n.event_key || ':%') AS reminders
          FROM collector_notifications n
          LEFT JOIN devices d ON d.device_id = n.device_id
         WHERE n.hospital_id = $1 AND n.created_at >= $2 AND n.created_at < $3
           AND n.event = ANY($4::text[])
         -- The collector's copy first: a researcher's copy may be a one-line message.
         ORDER BY n.event_key, (n.audience <> 'collector'), n.created_at
    """, hospital_id, start, end, list(ALERT_EVENTS))]
    alerts.sort(key=lambda a: a["created_at"])

    for r in recs:
        r["pauses"] = pair_pauses(chain.get(r["session_id"], []), r["closed_at"], now)
        t = times.get(r["session_id"], {})
        r["cmed_matched"] = bool(t.get("cmed_matched", bool(r["cmed_code"])))
        r["api2_at"] = _when(t.get("api2_at"))
        r["api3_at"] = _when(t.get("api3_at"))
        r["refused"] = r["refused_at"] is not None or r["confirmation"] == "refused"
        r["alerts"] = [a for a in alerts if a["session_id"] == r["session_id"]]
    loose = [a for a in alerts if a["session_id"] not in set(ids)]
    return {"recordings": recs, "loose_alerts": loose, "now": now,
            "collectors": await _collectors_on_shift(conn, hospital_id, start, end),
            "hospital_name": await conn.fetchval(
                "SELECT name FROM hospitals WHERE hospital_id = $1", hospital_id) or ""}


# ============================================================ words for one file

def _local(moment: Optional[datetime]) -> Optional[datetime]:
    import collectors
    return moment.astimezone(collectors.zone()) if moment else None


def _hhmm(moment) -> str:
    m = _local(moment)
    return f"{m:%H:%M}" if m else ""


def ended(r: Dict[str, Any]) -> Tuple[str, Optional[str]]:
    """(how it ended in words, the Summary column it counts in: None for a normal end)."""
    import collectors
    if r["refused"]:
        return "Patient did not consent (deleted)", "refused"
    if r["closed_at"] is None:
        return "Still recording", "open"
    reason = r["close_reason"] or ""
    if reason == "superseded_by_new_patient":
        return "Next patient", None
    if reason in ("counselling_limit", "prescription_built"):
        return "2 min after the prescription", None
    if reason == "consultation_ended":
        return "CMED closed the visit (API 4)", None
    kind = collectors.stop_kind(reason)
    if kind is None:
        return "Doctor finished", None
    words = collectors.STOP_LABELS.get(kind, kind.replace("_", " "))
    if r.get("close_detail"):
        words = f"{words}: {r['close_detail']}"
    column = "technical" if kind in TECHNICAL else (
        kind if kind in ("patient_left", "urgent_interruption") else "other")
    return "Stop: " + words[:1].upper() + words[1:], column


def microphone(r: Dict[str, Any]) -> Tuple[str, str]:
    """(words, kind): kind is jabra, not_jabra, no_sound or unknown."""
    import collectors
    if r["no_sound"]:
        return "No sound (muted or not working)", "no_sound"
    label = collectors.microphone_label(r["microphones"], int(r["switches"] or 0))
    kind = label.get("kind")
    if kind == "jabra":
        return "Jabra all through", "jabra"
    if kind == "mixed":
        return "Switched off the Jabra", "not_jabra"
    if kind == "pc":
        return "PC microphone only", "not_jabra"
    return "Not reported", "unknown"


def where(r: Dict[str, Any]) -> Dict[str, Any]:
    """Where this file's audio is: on the PC, pieces on the server, archived, cloud."""
    pieces = int(r["pieces"] or 0)
    declared = int(r["segment_count"] or 0)
    archived = r["archived_at"] is not None
    if r["refused"]:
        return {"pc": "Deleted", "pieces": "–", "archived": "–", "cloud": "–", "state": "deleted"}
    if r["closed_at"] is None:
        pc, state = "Yes, still recording", "pc"
    elif r["held"]:
        pc, state = "Held: the server refused it", "pc"
    elif not archived and declared and pieces < declared:
        pc, state = f"Yes, {declared - pieces} pieces waiting", "pc"
    else:
        pc, state = "No", ("archived" if archived else "server")
    if archived:
        on_server = "All, joined"
    elif r["closed_at"] is None:
        on_server = f"{pieces} so far"
    else:
        on_server = f"{pieces} of {declared}" if declared else f"{pieces}"
    return {"pc": pc, "pieces": on_server,
            "archived": f"{_local(r['archived_at']):%Y-%m-%d %H:%M}" if archived else "not yet",
            "cloud": f"{_local(r['copied_at']):%Y-%m-%d %H:%M}" if r["copied_at"] else "not yet",
            "state": state}


def file_name(r: Dict[str, Any]) -> str:
    return r["file_stem"] or r["session_id"]


def _api(r: Dict[str, Any], key: str) -> str:
    if r["refused"] or not r["cmed_matched"]:
        return "–"
    value = r.get(key)
    if value:
        return _hhmm(value)
    return "missing" if r["closed_at"] is not None else "not yet"


def _cmed(r: Dict[str, Any]) -> str:
    if r["refused"]:
        return "Refused"
    return {"confirmed": "Confirmed", "unconfirmed": "Not confirmed",
            "pending": "Waiting", "expired": "Expired", "legacy": "–"}.get(
        r["confirmation"] or "", r["confirmation"] or "–")


# ============================================================ the sheets

HEADER_FILL = "14213D"
RED, AMBER, GREEN = "F8D7D3", "FCE8C8", "DCEFE3"


def _styles():
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    thin = Side(style="thin", color="C9CED6")
    return {
        "head_font": Font(bold=True, color="FFFFFF"),
        "head_fill": PatternFill("solid", fgColor=HEADER_FILL),
        "title_font": Font(bold=True, size=13, color=HEADER_FILL),
        "info_font": Font(color="4A5568"),
        "bold": Font(bold=True),
        "red": PatternFill("solid", fgColor=RED),
        "amber": PatternFill("solid", fgColor=AMBER),
        "green": PatternFill("solid", fgColor=GREEN),
        "border": Border(left=thin, right=thin, top=thin, bottom=thin),
        "wrap": Alignment(wrap_text=True, vertical="top"),
        "top": Alignment(vertical="top", wrap_text=True),
    }


def _sheet_header(ws, st, title: str, info: Dict[str, str], columns: List[Tuple[str, int]]) -> int:
    """The same header on every sheet; returns the first data row."""
    ws.cell(row=1, column=1, value=title).font = st["title_font"]
    ws.cell(row=2, column=1, value=f"{info['hospital']} · {info['shift']} · "
                                   f"{info['range']}").font = st["info_font"]
    ws.cell(row=3, column=1, value=f"Collector on shift: {info['collectors']}").font = \
        st["info_font"]
    for col, (name, width) in enumerate(columns, start=1):
        cell = ws.cell(row=5, column=col, value=name)
        cell.font, cell.fill, cell.border = st["head_font"], st["head_fill"], st["border"]
        cell.alignment = st["wrap"]
        ws.column_dimensions[cell.column_letter].width = width
    ws.row_dimensions[5].height = 32
    ws.freeze_panes = "B6"
    return 6


def _put(ws, st, row: int, col: int, value, fill: Optional[str] = None):
    cell = ws.cell(row=row, column=col, value=value)
    cell.border, cell.alignment = st["border"], st["top"]
    if fill:
        cell.fill = st[fill]
    return cell


def _merge_block(ws, st, first: int, last: int, columns: int) -> None:
    """One file's details as one cell each across its rows."""
    if last <= first:
        return
    for col in range(1, columns + 1):
        ws.merge_cells(start_row=first, start_column=col, end_row=last, end_column=col)


SUMMARY_COLUMNS = [
    ("Doctor (DID)", 14), ("Patients", 10), ("Audio files", 10),
    ("Still on doctor's PC", 12), ("Pieces on server, not joined", 13), ("Archived", 10),
    ("Cloud copy", 10), ("CMED confirmed", 11), ("API 2 got", 9), ("API 2 missing", 9),
    ("API 3 got", 9), ("API 3 missing", 9), ("Jabra", 8), ("Not Jabra", 9),
    ("No sound", 9), ("Normal end", 9)] + [(name, 12) for _, name in EARLY_COLUMNS] + [
    ("Refused", 9), ("Files paused", 9), ("Pauses", 8), ("Paused min", 9), ("Alerts", 8)]


def _summary_row(recs: List[Dict[str, Any]]) -> List[Any]:
    early = {key: 0 for key, _ in EARLY_COLUMNS}
    counts = {k: 0 for k in ("pc", "server", "archived", "cloud", "confirmed", "api2", "api2_missing",
                             "api3", "api3_missing", "jabra", "not_jabra", "no_sound",
                             "normal", "refused", "files_paused", "pauses", "alerts")}
    paused_s = 0.0
    for r in recs:
        place = where(r)
        if place["state"] == "pc":
            counts["pc"] += 1
        elif place["state"] == "server":
            counts["server"] += 1
        elif place["state"] == "archived":
            counts["archived"] += 1
        if r["copied_at"]:
            counts["cloud"] += 1
        if r["confirmation"] == "confirmed":
            counts["confirmed"] += 1
        if r["cmed_matched"] and not r["refused"]:
            # Missing only once the recording has ended; while it runs, "not yet".
            for name in ("api2", "api3"):
                if r[f"{name}_at"]:
                    counts[name] += 1
                elif r["closed_at"] is not None:
                    counts[f"{name}_missing"] += 1
        _, kind = microphone(r)
        if kind in ("jabra", "not_jabra", "no_sound"):
            counts[kind] += 1
        _, column = ended(r)
        if column is None:
            counts["normal"] += 1
        elif column == "refused":
            counts["refused"] += 1
        elif column in early:
            early[column] += 1
        if r["pauses"]:
            counts["files_paused"] += 1
            counts["pauses"] += len(r["pauses"])
            paused_s += sum(p["seconds"] or 0 for p in r["pauses"])
        counts["alerts"] += len(r["alerts"])
    # 26 Nov 2026: patients with a recording kept. A refused recording's patient
    # ID is wiped (REDACTED), so counting it made 44 refusals look like "1 patient";
    # refusals are counted in their own column.
    kept = {r["patient_id"] for r in recs if not r["refused"]}
    return ([len(kept), len(recs), counts["pc"], counts["server"],
             counts["archived"], counts["cloud"], counts["confirmed"], counts["api2"],
             counts["api2_missing"], counts["api3"], counts["api3_missing"], counts["jabra"],
             counts["not_jabra"], counts["no_sound"], counts["normal"]]
            + [early[key] for key, _ in EARLY_COLUMNS]
            + [counts["refused"], counts["files_paused"], counts["pauses"],
               round(paused_s / 60, 1), counts["alerts"]])


def _summary_sheet(ws, st, data, info) -> None:
    row = _sheet_header(ws, st, "1 · Summary", info, SUMMARY_COLUMNS)
    by_doctor: Dict[str, List[Dict[str, Any]]] = {}
    for r in data["recordings"]:
        by_doctor.setdefault(r["doctor_id"] or "unknown", []).append(r)
    warn = {"Still on doctor's PC": "amber", "API 2 missing": "red", "API 3 missing": "red",
            "Not Jabra": "amber", "No sound": "red"}
    names = [name for name, _ in SUMMARY_COLUMNS]
    for doctor in sorted(by_doctor):
        values = [doctor] + _summary_row(by_doctor[doctor])
        for col, value in enumerate(values, start=1):
            fill = warn.get(names[col - 1]) if isinstance(value, (int, float)) and value else None
            _put(ws, st, row, col, value, fill)
        row += 1
    total = ["Total"] + _summary_row(data["recordings"])
    for col, value in enumerate(total, start=1):
        _put(ws, st, row, col, value).font = st["bold"]
    if not data["recordings"]:
        _put(ws, st, row + 1, 1, "No recordings in this shift.")


FILE_COLUMNS = [("File name", 30), ("PID", 14), ("DID", 10), ("HID", 14), ("VID", 16),
                ("Date", 11), ("Start", 7), ("End", 7), ("Min", 6), ("How it ended", 28),
                ("Microphone", 20), ("CMED", 13), ("API 2", 8), ("API 3", 8),
                ("On doctor's PC", 18), ("Pieces on server", 12), ("Archived (WAV)", 16),
                ("Cloud copy", 16), ("Pauses", 7), ("Paused min", 8), ("Alerts (see sheet 4)", 10)]
EVENT_COLUMNS = [("Event time", 8), ("Event", 9), ("Reason", 34), ("Event min", 8)]


def _file_values(r: Dict[str, Any]) -> List[Tuple[Any, Optional[str]]]:
    """A file's details for sheet 2, each with its colour."""
    how, _ = ended(r)
    mic, kind = microphone(r)
    place = where(r)
    cmed = _cmed(r)
    api2, api3 = _api(r, "api2_at"), _api(r, "api3_at")
    paused_min = round(sum(p["seconds"] or 0 for p in r["pauses"]) / 60, 1)
    opened = _local(r["opened_at"])
    return [
        (file_name(r), None), (r["patient_id"], None), (r["doctor_id"], None),
        (r["hospital_id"], None), (r["cmed_visit_id"] or "", None),
        (f"{opened:%Y-%m-%d}" if opened else "", None), (_hhmm(r["opened_at"]), None),
        (_hhmm(r["closed_at"]), None),
        (round(float(r["total_duration_seconds"] or 0) / 60, 1), None),
        (how, "amber" if how.startswith("Stop") or r["refused"] else None),
        (mic, "red" if kind == "no_sound" else ("amber" if kind == "not_jabra" else None)),
        (cmed, "green" if cmed == "Confirmed" else ("red" if cmed == "Not confirmed" else None)),
        (api2, "red" if api2 == "missing" else ("green" if api2 not in ("–", "not yet") else None)),
        (api3, "red" if api3 == "missing" else ("green" if api3 not in ("–", "not yet") else None)),
        (place["pc"], "amber" if place["state"] == "pc" else None),
        (place["pieces"], None),
        (place["archived"], "green" if place["archived"] not in ("not yet", "–") else None),
        (place["cloud"], "green" if place["cloud"] not in ("not yet", "–") else None),
        (len(r["pauses"]), "amber" if r["pauses"] else None),
        (paused_min, "amber" if r["pauses"] else None),
        (len(r["alerts"]), None),
    ]


# 3.9.8: what the recorder logs from the doctor's on-screen choices, and from
# CMED's API 4. None of them stopped the recording.
DOCTOR_LOG = {
    "doctor_mark": "Doctor's reason (recording continued)",
    "sensitive_start": "Sensitive part marked (to remove)",
    "sensitive_end": "Sensitive part ended",
    "api4_end": "CMED closed the visit (API 4)",
    "api4_mismatch": "API 4 for another visit (ignored)",
}


def _audio_at(seconds: Any) -> str:
    """Where in the recording, as m:ss - what the lab trims by."""
    try:
        total = int(float(seconds))
    except (TypeError, ValueError):
        return ""
    return f"at {total // 60}:{total % 60:02d} of the audio"


def _file_events(r: Dict[str, Any]) -> List[List[Any]]:
    """A file's pauses and how it stopped, in time order: [time, event, reason, minutes]."""
    events: List[Tuple[datetime, List[Any]]] = []
    for p in r["pauses"]:
        events.append((p["at"] or r["opened_at"],
                       [_hhmm(p["at"]), "Pause", pause_words(p),
                        round((p["seconds"] or 0) / 60, 1)]))
    for e in (r.get("manifest_events") or []):          # recorder 3.9.5's own log
        at = _when(e.get("at"))
        if e.get("event") in ("stop_pressed", "stop_cancelled") and at:
            events.append((at, [_hhmm(at), "Stop pressed" if e["event"] == "stop_pressed"
                                else "Stop cancelled", e.get("detail") or "", ""]))
        # 3.9.8: the doctor's logged choices; the recording did not stop.
        elif e.get("event") in DOCTOR_LOG and at:
            where = _audio_at(e.get("audio_seconds"))
            words = DOCTOR_LOG[e["event"]]
            detail = e.get("detail") or ""
            events.append((at, [_hhmm(at), words,
                                f"{detail}{' - ' + where if where else ''}", ""]))
    how, column = ended(r)
    if r["refused"]:
        events.append((r["refused_at"] or r["opened_at"],
                       [_hhmm(r["refused_at"]), "Stop", "Patient did not consent (deleted)", ""]))
    elif r["closed_at"] is not None:
        events.append((r["closed_at"], [_hhmm(r["closed_at"]),
                                        "Stop" if column else "Ended",
                                        how[6:] if how.startswith("Stop: ") else how, ""]))
    events.sort(key=lambda e: e[0] or datetime.min.replace(tzinfo=timezone.utc))
    return [e for _, e in events]


def _files_sheet(ws, st, data, info) -> None:
    columns = FILE_COLUMNS + EVENT_COLUMNS
    row = _sheet_header(ws, st, "2 · Audio files, with their pauses and stops", info, columns)
    width = len(FILE_COLUMNS)
    for r in data["recordings"]:
        values = _file_values(r)
        events = _file_events(r) or [["", "", "", ""]]
        first = row
        for i, event in enumerate(events):
            for col, (value, fill) in enumerate(values, start=1):
                _put(ws, st, row, col, value if i == 0 else None, fill)
            for k, value in enumerate(event):
                fill = "amber" if k == 1 and value == "Pause" else None
                _put(ws, st, row, width + 1 + k, value, fill)
            row += 1
        _merge_block(ws, st, first, row - 1, width)
    if not data["recordings"]:
        _put(ws, st, row, 1, "No recordings in this shift.")


MISSING_COLUMNS = [("File name", 30), ("CMED clinic code", 14), ("DID", 10), ("PID", 14),
                   ("VID", 16), ("Date", 11), ("CMED start time", 26), ("Recording start", 10),
                   ("Recording end", 10), ("API 2", 9), ("API 3", 9)]


def _missing_sheet(ws, st, data, info) -> None:
    row = _sheet_header(ws, st, "3 · Missing from CMED (no names; to send to CMED)", info,
                        MISSING_COLUMNS)
    late = data["now"] - timedelta(minutes=10)
    found = 0
    for r in data["recordings"]:
        if r["refused"] or not r["cmed_matched"] or r["closed_at"] is None \
                or r["closed_at"] > late or (r["api2_at"] and r["api3_at"]):
            continue
        opened = _local(r["opened_at"])
        values = [file_name(r), r["cmed_code"], r["doctor_id"], r["patient_id"],
                  r["cmed_visit_id"] or "", f"{opened:%Y-%m-%d}" if opened else "",
                  r["cmed_start_time"] or "", _hhmm(r["opened_at"]), _hhmm(r["closed_at"]),
                  _api(r, "api2_at"), _api(r, "api3_at")]
        for col, value in enumerate(values, start=1):
            _put(ws, st, row, col, value, "red" if value == "missing" else None)
        row += 1
        found += 1
    if not found:
        _put(ws, st, row, 1, "Nothing missing: CMED sent API 2 and API 3 for every recording.")


ALERT_COLUMNS = [("File name", 30), ("PID", 14), ("DID", 10), ("VID", 16), ("Date", 11),
                 ("Start", 7), ("End", 7), ("Alert time", 8), ("Alert", 30), ("Seen by", 22),
                 ("Seen after (min)", 9), ("Fixed after (min)", 9), ("How fixed", 34),
                 ("Reminders", 9)]


# An alert nobody marked fixed or ended within the follow-up time is not "open".
FOLLOW_UP = timedelta(hours=6)


def _alert_values(a: Dict[str, Any]) -> List[Any]:
    seen_after = round((a["seen_at"] - a["created_at"]).total_seconds() / 60) \
        if a["seen_at"] else ""
    fixed_after = round((a["resolved_at"] - a["created_at"]).total_seconds() / 60) \
        if a["resolved_at"] else ""
    what = ALERT_NAMES.get(a["event"], a["event"])
    subject = a.get("subject") or ""
    if a["event"] == "stopped_unusually" and subject.startswith("Recording stopped: "):
        what = "Stopped: " + subject.split(" - ")[0][len("Recording stopped: "):]
    elif a["event"] == "stopped_unusually" and " - " in subject:
        what = "Stopped: " + subject.split(" - ", 1)[1]      # a one-line message
    lasting = a["event"] in ("recording_without_jabra", "jabra_unplugged", "recording_problem",
                             "pc_silent", "still_paused", "recording_too_long")
    if a["resolution"]:
        how = a["resolution"]
    elif lasting and not a["resolved_at"]:
        recent = datetime.now(timezone.utc) - a["created_at"] < FOLLOW_UP
        how = "open" if recent else "not followed up"
    else:
        how = ""
    return [_hhmm(a["created_at"]), what, a["seen_by"] or "", seen_after, fixed_after, how,
            a["reminders"] or ""]


def _alerts_sheet(ws, st, data, info) -> None:
    row = _sheet_header(ws, st, "4 · Alerts per audio file", info, ALERT_COLUMNS)
    width = 7
    any_rows = False
    for r in data["recordings"]:
        if not r["alerts"]:
            continue
        any_rows = True
        opened = _local(r["opened_at"])
        details = [file_name(r), r["patient_id"], r["doctor_id"], r["cmed_visit_id"] or "",
                   f"{opened:%Y-%m-%d}" if opened else "", _hhmm(r["opened_at"]),
                   _hhmm(r["closed_at"])]
        first = row
        for i, a in enumerate(r["alerts"]):
            for col, value in enumerate(details, start=1):
                _put(ws, st, row, col, value if i == 0 else None)
            for k, value in enumerate(_alert_values(a)):
                fill = "red" if k == 5 and value == "open" else None
                _put(ws, st, row, width + 1 + k, value, fill)
            row += 1
        _merge_block(ws, st, first, row - 1, width)
    if data["loose_alerts"]:
        any_rows = True
        _put(ws, st, row, 1, "Not tied to a recording").font = st["bold"]
        row += 1
        by_pc: Dict[str, List[Dict[str, Any]]] = {}
        for a in data["loose_alerts"]:
            by_pc.setdefault(f"PC {a['pc']}" if a["pc"] else "The clinic", []).append(a)
        for pc in sorted(by_pc):
            first = row
            for i, a in enumerate(by_pc[pc]):
                _put(ws, st, row, 1, pc if i == 0 else None)
                for col in range(2, width + 1):
                    _put(ws, st, row, col, None)
                for k, value in enumerate(_alert_values(a)):
                    _put(ws, st, row, width + 1 + k, value)
                row += 1
            _merge_block(ws, st, first, row - 1, width)
    if not any_rows:
        _put(ws, st, row, 1, "No alerts in this shift.")


# ============================================================ the workbook

SHIFT_WORDS = {"morning": "Morning shift", "evening": "Evening shift", "day": "Whole day"}


async def build(conn, hospital_id: str, day: date, shift: str) -> Tuple[str, bytes]:
    """(file name, the .xlsx) for one hospital and one shift."""
    from openpyxl import Workbook
    import watchers
    start, end, words = watchers.shift_window(day, shift)
    data = await gather(conn, hospital_id, start, end)
    if data["recordings"]:
        manifests = {m["session_id"]: m["events"] for m in await conn.fetch("""
            SELECT session_id, manifest->'events' AS events FROM sessions
             WHERE session_id = ANY($1::text[]) AND manifest ? 'events'
        """, [r["session_id"] for r in data["recordings"]])}
        for r in data["recordings"]:
            events = manifests.get(r["session_id"])
            if isinstance(events, str):
                try:
                    events = json.loads(events)
                except ValueError:
                    events = None
            r["manifest_events"] = events if isinstance(events, list) else []
    info = {"hospital": f"{hospital_id} {data['hospital_name']}".strip(),
            "shift": f"{SHIFT_WORDS.get(shift, shift)}, {day:%d %b %Y}",
            "range": words, "collectors": data["collectors"]}
    wb = Workbook()
    st = _styles()
    sheets = [("Summary", _summary_sheet), ("Audio files", _files_sheet),
              ("Missing from CMED", _missing_sheet), ("Alerts per audio file", _alerts_sheet)]
    for i, (title, fill) in enumerate(sheets):
        ws = wb.active if i == 0 else wb.create_sheet()
        ws.title = title
        fill(ws, st, data, info)
    buf = io.BytesIO()
    wb.save(buf)
    return f"aimscribe_{hospital_id}_{day.isoformat()}_{shift}.xlsx", buf.getvalue()


_CACHE: Dict[Tuple[str, str, str], Tuple[float, str, bytes]] = {}


async def build_cached(conn, hospital_id: str, day: date, shift: str) -> Tuple[str, bytes]:
    """The same workbook for every researcher of a hospital, built once per 10 minutes."""
    key = (hospital_id, day.isoformat(), shift)
    now = time.monotonic()
    hit = _CACHE.get(key)
    if hit is None or now - hit[0] > 600:
        name, content = await build(conn, hospital_id, day, shift)
        for old in [k for k, v in _CACHE.items() if now - v[0] > 600]:
            _CACHE.pop(old, None)
        hit = (now, name, content)
        _CACHE[key] = hit
    return hit[1], hit[2]


XLSX_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

__all__ = ["build", "build_cached", "pair_pauses", "pause_words", "XLSX_TYPE"]
