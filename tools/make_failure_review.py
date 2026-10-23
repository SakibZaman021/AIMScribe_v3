"""
Builds FAILURE_REVIEW.pdf: every way the AIMScribe v3 system can fail, what it
does today, the evidence, and what to fix next.

    python tools/make_failure_review.py

Each row cites the code path it was checked against. "Evidence" names the
test, or the experiment run while writing this review, that shows the
behaviour; where nothing could be run it says so.
"""
from collections import Counter
from pathlib import Path

from reportlab.lib.units import mm
from reportlab.platypus import CondPageBreak, KeepTogether, PageBreak, Paragraph, Spacer

from cmed_doc_style import (BAD, BODY, CRIT, GOOD, H1, H2, NOTE, SUB, TEAL, WARNC, GOODC,
                            build, part, tbl)

OUT = Path(__file__).resolve().parent.parent / "FAILURE_REVIEW.pdf"

H, P, G, O = "HANDLED", "PARTIAL", "GAP", "OUTSIDE"

# (component, id, failure, what happens today, evidence, verdict, consequence, fix, effort)
ROWS = [
    # ---------------------------------------------------------------- recorder: capture
    ("Recorder - capture", "C1", "Microphone unplugged or headset switched mid-consultation",
     "A watchdog notices within about 2.5 s - failed reads, or the read that never returns "
     "when a USB microphone is pulled out - and reopens the Windows default microphone in the "
     "same session (core/recorder.py _watch, _failover). The doctor sees 'Microphone changed. "
     "Still recording'; the server gets <i>microphone_switched</i> with the seconds lost.",
     "tests/test_microphone_failover.py (8, fake PortAudio incl. a read that hangs); "
     "not tried with a real unplug", H,
     "About 2-3 s of audio at the moment of the switch", "-", "-"),
    ("Recorder - capture", "C2", "Laptop disk full (C: drive) while recording",
     "A failed clip write no longer stops the segmenter: clips wait in memory, in order, and "
     "are written every 3 s once space returns (up to 10 minutes of audio); the power-cut "
     "file keeps all of it. <i>disk_full</i> is raised once; a new start is refused while "
     "the drive has under 1 GB free; free space is checked at start and every heartbeat. "
     "A Stop while clips wait answers at once and closes after they are written. Each "
     "recording has its own clip writer, so a clip that waited is written into its own "
     "recording, never the next patient's (fixed in 3.5.0).",
     "tests/test_disk_full.py (15): write failures in clip and journal, order kept, one "
     "alert, memory limit, Stop not blocking, chain untouched", H,
     "Audio beyond 10 minutes of a full disk is counted as lost and reported", "-", "-"),
    ("Recorder - capture", "C3", "Laptop sleeps or the lid is closed mid-consultation",
     "While a recording is open the recorder tells Windows the PC is in use, so idle sleep "
     "cannot happen (core/power.py). 'Lid close: do nothing' and USB selective suspend off "
     "(which could power the speakerphone down) are set by the separate, signed clinic "
     "policy script (recorder/policy), no longer by the installer, so antivirus does not "
     "flag the installer.",
     "tests/test_power.py; policy script run with -WhatIf; not tried on a real laptop", H,
     "-", "-", "-"),
    ("Recorder - capture", "C4", "Power cut or forced shutdown mid-recording",
     "The unsealed part of the current clip is written to disk, encrypted, every 3 s "
     "(spool partial.aimspl). On the next start it is sealed into the chain as the last "
     "segment. Segments themselves are fsynced and renamed; a torn journal line is repaired.",
     "tests/test_partial_audio.py (7), tests/test_spool.py", H,
     "At most about 3 s of audio", "-", "-"),
    ("Recorder - capture", "C5", "Room is silent / microphone muted",
     "Two silent segments raise <i>silent_session</i> once; it reaches the dashboard live or "
     "from the health log. The clinic microphone is kept at 100% and unmuted (see C7).",
     "Live outage test: silent_session arrived from the health log", H,
     "Alert only; a muted session still records nothing", "-", "-"),
    ("Recorder - capture", "C6", "Windows shutdown, restart or sign-out mid-consultation",
     "A hidden window receives the shutdown notice and pauses the recording, showing "
     "'AIMScribe is saving the consultation' only while one is open. It closes it cleanly "
     "(reason <i>pc_shutdown</i>) when the shutdown really happens, and resumes it if the "
     "shutdown is cancelled (core/shutdown_guard.py). Shutdown is never refused.",
     "tests/test_shutdown_guard.py (9, incl. the real hidden window); no real shutdown run", H,
     "-", "-", "-"),
    ("Recorder - capture", "C7", "Jabra speakerphone not the Windows default, or turned down",
     "The recorder opens the microphone whose name contains 'Jabra' whatever the default is, "
     "and keeps its level at 100% and unmuted at start-up and on every idle heartbeat "
     "(core/microphone.py). Without it the PC's own microphone records.",
     "tests/test_microphone_failover.py (Jabra tests); level read and written live on this "
     "PC's microphone; no real Jabra tried", H, "-", "-", "-"),

    # ---------------------------------------------------------------- recorder: start & labels
    ("Recorder - start and labelling", "S1", "Wrong patient opened first, then the right one",
     "Every click while the recording is not armed is accepted (RECORDING_STARTED, same "
     "recording) and signed into the chain with its audio second. The server applies R3: a "
     "first patient open under 60 s, never returned to and never prescribed, belongs to the "
     "next. One file under the right patient.",
     "Live: Rahim then Karim at 8 s -> one file TEST_KARIM (R3+R1); "
     "backend tests/test_labelling.py", H, "-", "-", "-"),
    ("Recorder - start and labelling", "S2", "Recorder crashes after a click, before the close",
     "Click and prescription markers are journalled and delivered in chain order after the "
     "restart; the server labels from them as usual.",
     "tests/test_start_recovery.py (crash after a click), tests/test_markers.py", H,
     "-", "-", "-"),
    ("Recorder - start and labelling", "S3", "Microphone busy at the moment CMED triggers",
     "Start is kept and retried every 5 s for 15 min until it records, or until CMED sends "
     "another patient.",
     "tests/test_start_recovery.py", H, "Seconds of audio at the start", "-", "-"),
    ("Recorder - start and labelling", "S4", "Recorder crashes mid-consultation",
     "A consultation live within 10 min is resumed after the restart and the new recording "
     "names the one it continues (<i>continues_session_id</i>). The server checks it is the "
     "same PC and patient, confirms it with the first part's API 2, and both archive JSONs "
     "carry <i>continues</i> / <i>continued_by</i>.",
     "tests/test_device_state.py, backend tests/test_round2_server.py, archive_worker "
     "test_pressure_and_links.py", H, "Two linked files instead of one", "-", "-"),
    ("Recorder - start and labelling", "S5", "Recorder not running when the doctor opens the patient",
     "The startup task restarts a stopped recorder within a minute. A click made in that "
     "minute reaches nothing on our side; whether CMED's page tries again is CMED's code.",
     "Installer task: logon trigger repeating every minute + restart on failure", O,
     "That one consultation is not recorded",
     "See it in the CMED build session (tools/cmed_build_session.py)", "CMED"),
    ("Recorder - start and labelling", "S6", "Follow-up with no prescription, then the next patient",
     "The click on the next patient is accepted and logged; the server applies R4: a patient "
     "open 60 s or more, not prescribed and not returned from, is a real consultation. The "
     "recording is cut at that click into two files.",
     "Live: Sakib 70 s, Salma clicked, her prescription -> two files, Sakib 0-66.4 s (R4) and "
     "Salma (R1); the two rejoin to the signed hash", H, "-", "-", "-"),
    ("Recorder - start and labelling", "S7", "Recording never stopped",
     "A recording ends at the next click after a prescription; 2 min (+ up to 10 s, at a "
     "pause) after the prescription; after 15 min with no CMED activity; at Windows shutdown; "
     "or with Stop.",
     "Live: closed by itself 121 s after the prescription (counselling_limit); "
     "tests/test_markers.py (both limits)", H, "-", "-", "-"),
    ("Recorder - start and labelling", "S8", "Same trigger sent twice / page reload",
     "Before the prescription: accepted, same recording, no extra marker. After it: "
     "SESSION_ALREADY_ACTIVE. A repeated prescription_built: GATE_ALREADY_ARMED.",
     "tests/test_channel_a.py, tools/channel_a_test.py 24/24", H, "-", "-", "-"),
    ("Recorder - start and labelling", "S9", "Doctor's personal time after a no-prescription visit",
     "With no prescription there is no signal for the moment the patient left, and no "
     "software can tell a phone call in the room from a consultation. Until the next click, "
     "or 15 min without CMED activity, it is recorded into that patient's file.",
     "By design", O, "Up to 15 min of the doctor's own time in a follow-up's file",
     "Doctors use Pause for personal calls (training); the no-activity limit caps it", "clinic"),
    ("Recorder - start and labelling", "S10", "Cases the rules cannot settle",
     "Kept, labelled by the best rule and flagged; the dashboard's <b>Needs review</b> panel "
     "lists every <i>labelling_review</i> and <i>relabelled_after_archive</i> case with its "
     "parts, rule and file names.",
     "backend tests/test_labelling.py, tests/test_round2_server.py (review panel)", H,
     "A person looks at the flagged file from the dashboard", "-", "-"),
    ("Recorder - start and labelling", "S11", "API 3b arrives after the files were archived",
     "The rules run again. If the cut changes, the archive rejoins the parts, checks the result "
     "against the signed hash, cuts again and replaces the files; "
     "<i>relabelled_after_archive</i> is raised.",
     "archive_worker test_parts.py (recut, matching and mismatching hash); "
     "tests/test_labelling_server.py", H, "-", "-", "-"),

    # ---------------------------------------------------------------- recorder <-> server
    ("Recorder to server", "U1", "Laptop clock wrong (behind or ahead of the server)",
     "The recorder measures the server's time from the Date header on every reply and checks "
     "each grant against it (core/uploader.py note_server_date, server_now; core/crypto.py "
     "verify_grant now=). Used grants are remembered by when this PC saw them, not by their "
     "expiry, so a fast clock cannot accept one twice. The offset goes in the heartbeat; the "
     "server raises <i>clock_skew</i> once per 6 h when it is 2 minutes or more. The installer "
     "turns on Windows time sync.",
     "tests/test_clock_skew.py (6 s, 5 min and 1 h either way), "
     "backend tests/test_clock_skew_server.py; Channel A 24/24 on the new build. "
     "Not tried on a laptop with its clock actually set wrong", H,
     "Times stamped by the PC (and so file names) follow its own clock until time sync "
     "corrects it", "-", "-"),
    ("Recorder to server", "U2", "Internet down for hours",
     "Recording never waits; spool holds 13.5 h; authorisation retried; uploads and matching "
     "resume on reconnect.",
     "Live outage tests (60 s and 100 s offline) -> archived", H, "Delayed only", "-", "-"),
    ("Recorder to server", "U3", "Server says no (unmapped clinic, revoked device, key mismatch)",
     "Held on the laptop, asked again every 5 min, nothing deleted. Each heartbeat now "
     "carries the held recordings and why; the dashboard shows them per PC (<b>Held on PCs</b> "
     "panel and the Held column).",
     "tests/test_device_state.py, tests/test_round2_server.py", H,
     "Delayed until the cause is fixed; visible on the dashboard", "-", "-"),
    ("Recorder to server", "U4", "What the PC saw while offline",
     "Offline heartbeats (1/min) and every alert go to a 5 MB health log, delivered on "
     "reconnect and deleted only after the server stores them.",
     "Live outage test: 3 lines delivered then deleted; dashboard showed the period", H,
     "-", "-", "-"),
    ("Recorder to server", "U5", "Duplicate upload after a lost response",
     "Server recognises the stored entry hash and answers duplicate.",
     "Segment, reassign and marker duplicate tests", H, "-", "-", "-"),
    ("Recorder to server", "U6", "A clicked patient's grant refused or invalid",
     "The marker is still delivered (grant_jti null after the hold period) and kept in the "
     "chain; the server finds that patient's API 2 and 3b by the click's five fields instead "
     "(<i>marker_without_grant</i> / <i>marker_grant_invalid</i> alerts).",
     "tests/test_markers.py (grant cases), tests/test_labelling_server.py", H, "-", "-", "-"),
    ("Recorder to server", "U7", "PC installed while the clinic's internet is down",
     "The enrolment token is tried again every minute until the server answers; a token the "
     "server refuses is not retried.",
     "tests/test_enrol_retry.py", H, "-", "-", "-"),

    # ---------------------------------------------------------------- server & database
    ("Server and database", "D1", "A migration not applied on a deployed database",
     "At start-up the server checks for what each migration 002-015 and the clinical schema "
     "leave behind - tables, columns, the entry types the chain accepts - so a skipped or "
     "half-applied migration is named (backend/src/schema_check.py). It is logged as critical, "
     "raised as <i>schema_missing</i>, shown in /health (\"schema\": \"missing 015_...\", "
     "status degraded) and alerted by the monitor. It clears by itself within a minute of "
     "applying the file. AIMS_SCHEMA_STRICT=true refuses to start instead.",
     "tests/test_schema_check.py (9, real PostgreSQL, each migration removed in turn); "
     "deploy/uiu tests (monitor); live: local server reports schema ok", H,
     "-", "-", "-"),
    ("Server and database", "D2", "Database restarts mid-request",
     "The request fails; the recorder and worker retry; commits are idempotent.",
     "Duplicate-detection tests; not re-run against a restarting database", H,
     "Delayed only", "-", "-"),
    ("Server and database", "D3", "API 2 never arrives for a recording",
     "Recording archived 'unconfirmed' and kept; API 2 attaches whenever it arrives.",
     "Live: earlier TEST_NEXT and TEST_OFF archived unconfirmed", H, "-", "-", "-"),
    ("Server and database", "D4", "A CMED outage makes many recordings unconfirmed",
     "One <i>unconfirmed_recording</i> alert per clinic per 6 hours, carrying the count and "
     "the recordings, instead of one per recording.",
     "tests/test_round2_server.py", H, "-", "-", "-"),
    ("Server and database", "D5", "Background audit writer stops",
     "Restarted on demand when dead or bound to a closed loop.",
     "441 backend tests pass against real PostgreSQL", H, "-", "-", "-"),
    ("Server and database", "D6", "The wrong patient's late records reach a relabelled file",
     "A visit finds a recording only through the grant that recording now carries, or one of "
     "its clicks; a first-patient grant left behind by a move confirms and attaches nothing.",
     "tests/test_reassign.py, tests/test_labelling_server.py", H, "-", "-", "-"),

    # ---------------------------------------------------------------- archive, storage, backups
    ("Archive, storage and backups", "A1", "Server disk fills",
     "When the archive disk falls below its floor, the worker releases the oldest local WAVs "
     "that have a proven cloud copy and still match their hash, before the retention period, "
     "until there is room; never one without a verified copy. Copies overdue more than 2 "
     "days already alert.",
     "archive_worker test_pressure_and_links.py (13)", H,
     "If R2 itself is down for weeks, the disk can still fill - and the monitor says so",
     "-", "-"),
    ("Archive, storage and backups", "A2", "R2 unreachable or wrong keys",
     "/health reports the bucket state; copies are retried; <i>cloud_copy_overdue</i> after 24 h.",
     "tests/test_health_storage.py", H, "Copies delayed", "-", "-"),
    ("Archive, storage and backups", "A3", "The droplet is lost",
     "Audio is safe in R2. Each night's encrypted database backup is now also copied to its "
     "own R2 bucket with its own token, size-checked after upload, kept 90 days, and caught "
     "up the next night if R2 could not be reached; unencrypted backups never leave "
     "(deploy/uiu/backup/backup.sh). <i>fetch latest</i> brings one back for restore.sh. The "
     "monitor alerts when last night's backup, or its off-site copy, is over 36 h old.",
     "deploy/uiu tests/test_backup_offsite.py (real backup.sh, stand-in rclone); live: the "
     "real image dumped, encrypted and copied both databases to a test bucket, the copy was "
     "fetched back and restored with all 10 sessions", H,
     "At most one night of clinical records, if the droplet is lost before 02:30", "-", "-"),
    ("Archive, storage and backups", "A4", "Archive worker dies mid-copy or mid-cut",
     "Partial files are discarded and the work restarts; a multi-part cut resumes; pieces "
     "deleted only after every part is copied and verified.",
     "archive_worker test_parts.py, test_worker_copy.py", H, "-", "-", "-"),
    ("Archive, storage and backups", "A5", "A cut file does not match the signed recording",
     "Parts are contiguous and checked to add up to every frame; rejoined, they must equal the "
     "session's signed hash before any re-cut, or the worker refuses and reports.",
     "Live: Sakib + Salma rejoined to the session's sha256 exactly; test_parts.py", H,
     "-", "-", "-"),

    # ---------------------------------------------------------------- CMED boundary
    ("CMED boundary", "B1", "CMED's server offline (power or internet)",
     "API 2 and API 3 must wait in CMED's own queue; recordings wait on our server and "
     "are confirmed whenever either arrives. A clinic CMED is silent for erases nothing; "
     "only a recording CMED never confirms is erased, 7 days after it was made.",
     "Depends on CMED's queue", O,
     "Clinical data missing until CMED sends it",
     "Confirm CMED's outbox in the build session", "CMED"),
    ("CMED boundary", "B2", "Same visit or prescription sent again",
     "Identical: 200 ALREADY_RECEIVED; changed prescription: next version; previous visit "
     "fingerprinted (ALREADY_STORED / UPDATED / STORED).",
     "tests/test_previous_visit.py", H, "-", "-", "-"),
    ("CMED boundary", "B3", "CMED's finished page meets the new recorder",
     "Every reply is one their code already treats as success; nothing new is asked of CMED. "
     "Not yet seen: whether their page sends API 2 for every click and API 1 again on return.",
     "Our reference page and /manual-test; tools/cmed_build_session.py ready", O,
     "If API 2 is missing for a click, that part is archived unconfirmed (kept)",
     "Run tools/cmed_build_session.py with CMED's build", "CMED"),
    ("CMED boundary", "B4", "Unmapped or wrong clinic code",
     "Channel B: stored with an <i>unmapped_clinic</i> alert. Recorder: CLINIC_MISMATCH, held.",
     "tests/test_v3_server.py", H, "Delayed until mapped", "-", "-"),

    # ---------------------------------------------------------------- security
    ("Security", "X1", "Laptop stolen",
     "Spool and partial audio are encrypted with a DPAPI-wrapped key; the device can be "
     "revoked. Each recorder now reports whether its drive is BitLocker-protected; the "
     "dashboard shows it and raises <i>disk_not_encrypted</i> once a day for a PC without it.",
     "tests/test_disk_full.py (BitLocker probe), tests/test_round2_server.py; live: this "
     "PC reported not encrypted", H, "Turning BitLocker on is the clinic's IT", "-", "-"),
    ("Security", "X2", "One hospital's CMED key leaked",
     "That hospital only is exposed: each hospital has its own key pair and 5-minute tokens. "
     "Revoke that hospital's client (POST /api/v2/admin/cmed-client/{id}/revoke) - its keys "
     "and every live token stop at once; the other hospitals are unaffected.",
     "backend tests/test_cmed_auth_fhir.py, test_cmed_hardening.py (revoke, key revocation "
     "ends its tokens)", H, "-", "-", "-"),
    ("Security", "X3", "Forged or replayed grant / chain entry",
     "EdDSA only, single-use grants, signed hash chain verified per entry, markers "
     "included. Each entry must name the clip it covers and follow the head exactly; every "
     "device request is signed with the PC's key (single-use nonce, 5-minute window).",
     "tests/test_authorisation.py, test_chain.py, backend tests/test_data_safety.py", H,
     "-", "-", "-"),

    # ---------------------------------------------------------------- deployment & versions
    ("Deployment and versions", "V1", "New recorder against a server without the new endpoints",
     "The recorder keeps its audio and retries; the server's start-up check names any "
     "missing migration in the log, /health and an alert, and the runbook orders server "
     "first.",
     "tests/test_schema_check.py; live: schema_missing raised when the server started "
     "without 016", H, "Delayed until the server is upgraded", "-", "-"),
    ("Deployment and versions", "V2", "Old recorder builds still installed at clinics",
     "Every heartbeat carries the version; the dashboard flags PCs below "
     "AIMS_MIN_RECORDER_VERSION (3.4.0; raise it to 3.5.0 once the signed installer is "
     "rolled out) and raises <i>recorder_outdated</i> once a day.",
     "tests/test_round2_server.py; live: the 3.3.0 build was flagged, 3.4.0 was not", H,
     "Reinstall flagged PCs", "-", "-"),
    ("Deployment and versions", "V3", "Recorder keys do not match the new server",
     "Recordings are held, not deleted; tools/check_key_pair.py and runbook step 11b check it.",
     "Key-pair check", H, "Delayed until fixed", "-", "-"),
    ("Deployment and versions", "V4", "Upgrading a PC with the installer",
     "The installer keeps that PC's server address, CMED address and local key; the server "
     "address is set once at build time. Unused enrolment tokens last 14 days.",
     "Installer compiled; tools tested; upgrade not run on a real PC", H, "-", "-", "-"),

    # ---------------------------------------------------------------- clinic environment
    ("Clinic environment", "E1", "Router or WiFi down (CMED's local server unreachable)",
     "The doctor cannot open a patient, so no trigger and no recording; a session already "
     "running keeps recording.",
     "By design (no manual start)", O,
     "Consultations during the outage are not recorded",
     "Ethernet to consulting-room PCs; router and access point on UPS", "clinic"),
    ("Clinic environment", "E2", "Power cut at the clinic",
     "A laptop on battery keeps recording; a hard power-off loses at most about 3 s; server "
     "and router need UPS (see E1).",
     "Live outage tests; tests/test_partial_audio.py", O, "As E1 if the router dies",
     "UPS for router and CMED server", "clinic"),
    ("Clinic environment", "E3", "Windows update restarts the PC mid-consultation",
     "The installer sets Windows Update active hours 07:00-23:00 and no automatic restart "
     "while someone is signed in. If a restart still happens, the recording closes cleanly "
     "(C6) and the consultation resumes as a linked second part (S4).",
     "Installer compiled; shutdown and resume tests", H, "-", "-", "-"),
    ("Clinic environment", "E4", "28 rooms opening at once",
     "Grant path moved off shared locks; measured earlier under the 14- and 28-room "
     "simulations.", "Earlier simulations; not re-run against the labelling changes", H,
     "-", "-", "-"),
]

# Ranked by consequence: lost or misfiled audio first, then silent stalls.
TOP = ["B3", "B1", "S5", "E1", "E2", "S9"]

NEXT = [
    ("B3/B1/S5", "One session with CMED's finished build: tools/cmed_build_session.py"),
    ("E1/E2", "Clinic set-up: Ethernet and UPS for the consulting-room PCs and the router"),
    ("S9", "Doctor training: Pause for personal calls"),
    ("X1", "Clinic IT: BitLocker on each PC (the dashboard shows which have it)"),
    ("V2", "Roll out the signed 3.5.0 installer to every PC, then set "
           "AIMS_MIN_RECORDER_VERSION=3.5.0"),
]

COLOUR = {H: GOODC, P: WARNC, G: CRIT, O: TEAL}


def verdict(v):
    return '<font color="%s"><b>%s</b></font>' % (COLOUR[v].hexval().replace("0x", "#"), v)


def main():
    counts = Counter(r[5] for r in ROWS)
    by_id = {r[1]: r for r in ROWS}
    s = [Paragraph("AIMScribe v3 - Failure Review", H1),
         Paragraph("Every way the system can fail, what it does today, the evidence, and what to "
                   "fix next. Reviewed against the code as of 20 October 2026 (recorder 3.5.0, "
                   "after the second quality round).", SUB)]

    s.append(tbl([["Verdict", "Count", "Meaning"],
                  [verdict(H), counts[H], "Handled, with a test or a live run as evidence"],
                  [verdict(P), counts[P], "Handled in part, or depends on CMED or the clinic"],
                  [verdict(G), counts[G], "Not handled: audio can be lost, misfiled or stuck"],
                  [verdict(O), counts[O], "Outside our code: needs CMED, the clinic's "
                                          "hardware or how the doctor works - the action and "
                                          "who owns it are listed"],
                  ["Total", len(ROWS), ""]],
                 [32 * mm, 18 * mm, 124 * mm]))
    s.append(Spacer(1, 6 * mm))
    s.append(Paragraph("What is left, and who acts on it", H2))
    top = [["#", "Failure", "Consequence", "Fix"]]
    for n, rid in enumerate(TOP, 1):
        r = by_id[rid]
        top.append([f"{n}. {rid}", r[2], r[6], f"{r[7]} <i>({r[8]})</i>"])
    s.append(tbl(top, [14 * mm, 46 * mm, 50 * mm, 64 * mm]))
    s.append(Spacer(1, 4 * mm))
    s.append(Paragraph(
        "<b>Nothing is left open in our code.</b> Every remaining item needs an action by "
        "CMED, the clinic or its IT, listed above with its owner. The most important is the "
        "one session with CMED's finished build (B3).", GOOD))

    s.append(PageBreak())
    s.append(part("PART 1", "Every failure mode, by component",
                  "What happens today, with the code it was checked against and the evidence."))
    s.append(Spacer(1, 4 * mm))
    current = None
    for r in ROWS:
        if r[0] != current:
            current = r[0]
            s.append(CondPageBreak(70 * mm))     # never a heading alone at a page foot
            s.append(Spacer(1, 3 * mm))
            s.append(Paragraph(current, H2))
        s.append(KeepTogether([tbl(
            [[verdict(r[5]), f"{r[1]} - {r[2]}"],
             ["Today", r[3]],
             ["Evidence", r[4]],
             ["Consequence", r[6]],
             ["Fix", f"{r[7]}" + (f" <i>(effort: {r[8]})</i>" if r[8] != "-" else "")]],
            [28 * mm, 146 * mm]), Spacer(1, 2.5 * mm)]))

    s.append(PageBreak())
    s.append(part("PART 2", "Recommended next fixes, in order",
                  "Lost or misfiled audio first, then anything that stalls silently."))
    s.append(Spacer(1, 4 * mm))
    nxt = [["Order", "Item", "What"]]
    for n, (rid, what) in enumerate(NEXT, 1):
        nxt.append([str(n), rid, what])
    s.append(tbl(nxt, [16 * mm, 26 * mm, 132 * mm]))
    s.append(Spacer(1, 5 * mm))
    s.append(Paragraph("Not verified in this review", H2))
    for line in ("A real USB microphone pulled out (C1), a real Jabra (C7), a real Windows "
                 "shutdown (C6), a hard power cut (C4) and a real lid close (C3): each is tested "
                 "through the same code with a fake device or message, not on clinic hardware.",
                 "CMED's finished page (B3): tools/cmed_build_session.py is ready, not yet run.",
                 "28-room load (E4) and database restart (D2) were not re-run against today's code.",
                 "Router, UPS and Windows-update behaviour (E1-E3) depend on each clinic's set-up."):
        s.append(Paragraph("&bull; " + line, NOTE))

    build(s, OUT, "AIMScribe v3 Failure Review", "AIMScribe v3 - Failure Review - AIMS LAB, UIU")
    print(dict(counts))


if __name__ == "__main__":
    main()
