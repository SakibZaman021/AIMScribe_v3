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
from reportlab.platypus import KeepTogether, PageBreak, Paragraph, Spacer

from cmed_doc_style import (BAD, BODY, CRIT, GOOD, H1, H2, NOTE, SUB, WARNC, GOODC,
                            build, part, tbl)

OUT = Path(__file__).resolve().parent.parent / "FAILURE_REVIEW.pdf"

H, P, G = "HANDLED", "PARTIAL", "GAP"

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
     "A failed segment write still ends the segmenter thread (core/simple_splitter.py). "
     "Unlikely in practice: audio is deleted from the PC once the server holds it, and a start "
     "is refused when the spool has under 4 minutes of room.",
     "Code read", G,
     "Audio after the failure is lost while the recording looks alive",
     "Catch write errors in the segmenter loop, keep the PCM in memory and retry, raise "
     "<i>disk_full</i>; check free disk space as well as spool size",
     "1 day"),
    ("Recorder - capture", "C3", "Laptop sleeps or the lid is closed mid-consultation",
     "While a recording is open the recorder tells Windows the PC is in use, so idle sleep "
     "cannot happen (core/power.py). The installer sets 'lid close: do nothing' and turns off "
     "USB selective suspend, which could power the speakerphone down.",
     "tests/test_power.py; installer compiled; not tried on a real laptop", H,
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
     "A hidden window receives the shutdown notice, holds it for up to 4 s with 'AIMScribe is "
     "saving the consultation', closes the recording cleanly (reason <i>pc_shutdown</i>) and "
     "lets Windows continue (core/shutdown_guard.py). Shutdown is never refused.",
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
     "Interrupted session closed on restart; a consultation live within 10 min is recorded "
     "again under the same five fields. Two sessions then share one visit and are not tied "
     "to each other.",
     "tests/test_start_recovery.py", P,
     "Second part archived as its own file, not joined to the first",
     "Carry 'continues: &lt;session&gt;' in the new session and let the server join them",
     "1 day"),
    ("Recorder - start and labelling", "S5", "Recorder not running when the doctor opens the patient",
     "The trigger never arrives. The startup task now restarts a stopped recorder within a "
     "minute; CMED's page has no resend, so that consultation is not recorded.",
     "Installer task: logon trigger repeating every minute + restart on failure", P,
     "Consultation not recorded if the recorder was down at the click",
     "Nothing further on our side without CMED; keep the recorder running (it is)", "-"),
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
     "With no prescription there is no signal for the moment the patient left. Until the next "
     "click, or 15 min without CMED activity, a phone call in the room is recorded into that "
     "patient's file.",
     "By design; silence does not reveal a phone call", P,
     "Up to 15 min of the doctor's own time in a follow-up's file",
     "Pause button for personal calls; trim trailing silence on the server", "half a day"),
    ("Recorder - start and labelling", "S10", "Cases the rules cannot settle",
     "A follow-up shorter than 60 s is merged into the next patient (R3); a glance with no "
     "return signal becomes its own part; one patient in two separate parts. Each is kept, "
     "labelled by the best rule and flagged: <i>labelling_review</i> on the dashboard.",
     "backend tests/test_labelling.py (R5 cases)", P,
     "A person must look at the flagged file", "Review list on the dashboard", "half a day"),
    ("Recorder - start and labelling", "S11", "API 3b arrives after the files were archived",
     "The rules run again. If the cut changes, the archive rejoins the parts, checks the result "
     "against the signed hash, cuts again and replaces the files; "
     "<i>relabelled_after_archive</i> is raised.",
     "archive_worker test_parts.py (recut, matching and mismatching hash); "
     "tests/test_labelling_server.py", H, "-", "-", "-"),

    # ---------------------------------------------------------------- recorder <-> server
    ("Recorder to server", "U1", "Laptop clock behind the server by more than 5 s",
     "Grants are verified with 5 s leeway (core/crypto.py verify_grant); PyJWT rejects an "
     "<i>iat</i> in the future. Every grant fails, so every recording is held and never "
     "uploads. A clock more than 65 s ahead fails the same way (expired).",
     "Experiment: laptop 6 s behind -> ImmatureSignatureError; 300 s ahead -> "
     "ExpiredSignatureError", G,
     "Nothing from that PC reaches the server; audio piles up on the laptop",
     "Verify iat/exp against the server's Date header offset (or leeway 10 min), report clock "
     "skew in the heartbeat, and have the installer enable Windows time sync", "half a day"),
    ("Recorder to server", "U2", "Internet down for hours",
     "Recording never waits; spool holds 13.5 h; authorisation retried; uploads and matching "
     "resume on reconnect.",
     "Live outage tests (60 s and 100 s offline) -> archived", H, "Delayed only", "-", "-"),
    ("Recorder to server", "U3", "Server says no (unmapped clinic, revoked device, key mismatch)",
     "Held on the laptop, asked again every 5 min, alert <i>authorisation_held</i>; nothing "
     "deleted.",
     "tests/test_authorisation.py (hold tests)", P,
     "Revoked device holds audio until the spool fills; then new starts are refused",
     "Dashboard list of held sessions per PC with the reason", "half a day"),
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
     "Nothing checks the schema at start-up. With 013 missing every reassign got 500 (seen "
     "live); with 015 missing every click marker fails and that recording stops uploading.",
     "Live: chain_entries_entry_type_check violation until 013 was applied", G,
     "Uploads stall until someone notices; recorders retry for ever",
     "A schema_version table written by each migration and checked at start (refuse to start "
     "or report degraded in /health)", "half a day"),
    ("Server and database", "D2", "Database restarts mid-request",
     "The request fails; the recorder and worker retry; commits are idempotent.",
     "Duplicate-detection tests; not re-run against a restarting database", H,
     "Delayed only", "-", "-"),
    ("Server and database", "D3", "API 2 never arrives for a recording",
     "Recording archived 'unconfirmed' and kept; API 2 attaches whenever it arrives.",
     "Live: earlier TEST_NEXT and TEST_OFF archived unconfirmed", H, "-", "-", "-"),
    ("Server and database", "D4", "A CMED outage makes many recordings unconfirmed",
     "One <i>unconfirmed_recording</i> warning per recording.",
     "Code read", P, "Alert flood during every CMED outage hides real alerts",
     "Raise one alert per clinic per outage, or only after 24 h without API 2", "2 hours"),
    ("Server and database", "D5", "Background audit writer stops",
     "Restarted on demand when dead or bound to a closed loop.",
     "224 backend tests pass against real PostgreSQL", H, "-", "-", "-"),
    ("Server and database", "D6", "The wrong patient's late records reach a relabelled file",
     "A visit finds a recording only through the grant that recording now carries, or one of "
     "its clicks; a first-patient grant left behind by a move confirms and attaches nothing.",
     "tests/test_reassign.py, tests/test_labelling_server.py", H, "-", "-", "-"),

    # ---------------------------------------------------------------- archive, storage, backups
    ("Archive, storage and backups", "A1", "Server disk fills",
     "WAVs released after a verified R2 copy and the retention period; monitor alerts at 15% "
     "free.",
     "archive_worker tests (122)", P,
     "If R2 is down for more than about a week the disk still fills",
     "Release-on-pressure and an alert when copies are overdue more than 2 days", "half a day"),
    ("Archive, storage and backups", "A2", "R2 unreachable or wrong keys",
     "/health reports the bucket state; copies are retried; <i>cloud_copy_overdue</i> after 24 h.",
     "tests/test_health_storage.py", H, "Copies delayed", "-", "-"),
    ("Archive, storage and backups", "A3", "The droplet is lost",
     "Audio is safe in R2. Database backups are written to a folder on the same droplet; "
     "the off-droplet copy is a manual step in the runbook.",
     "Code and runbook read", G,
     "Up to a week of clinical records and the catalogue lost (DigitalOcean weekly backup only)",
     "Upload each encrypted nightly backup to its own R2 bucket", "half a day"),
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
     "API 2 and API 3 must wait in CMED's own queue; recordings are kept regardless and matched "
     "when API 2 arrives.",
     "Depends on CMED's queue", P,
     "Clinical data missing until CMED sends it; lost if CMED does not queue durably",
     "Check during the CMED build session", "CMED"),
    ("CMED boundary", "B2", "Same visit or prescription sent again",
     "Identical: 200 ALREADY_RECEIVED; changed prescription: next version; previous visit "
     "fingerprinted (ALREADY_STORED / UPDATED / STORED).",
     "tests/test_previous_visit.py", H, "-", "-", "-"),
    ("CMED boundary", "B3", "CMED's finished page meets the new recorder",
     "Every reply is one their code already treats as success (RECORDING_STARTED / "
     "PROVISIONAL, SESSION_ALREADY_ACTIVE, GATE_ARMED); PATIENT_NOTED and GATE_NOT_ARMED are no "
     "longer sent. Not yet seen: whether their page sends API 2 for every click and API 1 "
     "again when the doctor goes back.",
     "Our reference page; tools/cmed_build_session.py written, not yet run with CMED", P,
     "If API 2 is missing for a click, that part is archived unconfirmed (kept)",
     "Run tools/cmed_build_session.py with CMED's build", "1 session"),
    ("CMED boundary", "B4", "Unmapped or wrong clinic code",
     "Channel B: stored with an <i>unmapped_clinic</i> alert. Recorder: CLINIC_MISMATCH, held.",
     "tests/test_v3_server.py", H, "Delayed until mapped", "-", "-"),

    # ---------------------------------------------------------------- security
    ("Security", "X1", "Laptop stolen",
     "Spool and the 3-second partial audio are encrypted with a DPAPI-wrapped key; the device "
     "can be revoked. Someone who can log in as that Windows user can unwrap the key.",
     "Code read", P, "Audio readable on a stolen, logged-in laptop",
     "Require BitLocker on clinic PCs (installer check)", "policy"),
    ("Security", "X2", "CMED key leaked", "Revoke and issue a new key (admin endpoints).",
     "tests/test_v3_server.py::test_revoked_key_is_refused", H, "-", "-", "-"),
    ("Security", "X3", "Forged or replayed grant / chain entry",
     "EdDSA only, single-use grants, signed hash chain verified per entry, markers included.",
     "tests/test_authorisation.py, test_chain.py", H, "-", "-", "-"),

    # ---------------------------------------------------------------- deployment & versions
    ("Deployment and versions", "V1", "New recorder against a server without the new endpoints",
     "Markers are kept and retried; the recording cannot finish uploading until /session/marker "
     "(migration 015) exists.",
     "Code read (chain order); runbook states the order", P, "Uploads stall until the server is "
     "upgraded", "Upgrade order: server and migrations 013-015 first, recorders after", "docs"),
    ("Deployment and versions", "V2", "Old recorder builds still installed at clinics",
     "Builds before 16 October refuse or only note a second patient, and lose up to a minute "
     "at shutdown.",
     "Code history", P, "Old PCs keep the old failure modes", "Version gate: server warns per "
     "device below the current build in the dashboard; reinstall all PCs", "half a day"),
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
     "By design (no manual start)", P,
     "Consultations during the outage are not recorded",
     "Ethernet to consulting-room PCs; router and access point on UPS", "clinic"),
    ("Clinic environment", "E2", "Power cut at the clinic",
     "Laptop on battery keeps recording; server and router need UPS (see E1).",
     "Live outage tests", P, "As E1 if the router dies", "UPS for router and CMED server",
     "clinic"),
    ("Clinic environment", "E3", "Windows update restarts the PC mid-consultation",
     "The recording closes cleanly at shutdown (C6); after logon the consultation is resumed "
     "if within 10 min, as a second, unlinked recording (S4).",
     "Shutdown and resume tests", P, "Consultation split into two unlinked files",
     "Set active hours / defer updates on clinic PCs; fix S4", "policy"),
    ("Clinic environment", "E4", "28 rooms opening at once",
     "Grant path moved off shared locks; measured earlier under the 14- and 28-room "
     "simulations.", "Earlier simulations; not re-run against the labelling changes", H,
     "-", "-", "-"),
]

# Ranked by consequence: lost or misfiled audio first, then silent stalls.
TOP = ["U1", "D1", "A3", "B3", "S9", "S4", "S10", "C2", "D4", "U3"]

NEXT = [
    ("U1", "Clock skew: verify grants against the server's time, report skew, enable time sync"),
    ("D1", "Schema version check at server start (013, 014, 015)"),
    ("B3", "Run tools/cmed_build_session.py with CMED's finished build"),
    ("A3", "Nightly encrypted database backup to R2"),
    ("S4", "Tie a resumed recording to the one it continues"),
    ("S10", "Dashboard review list for flagged labels"),
    ("S9", "Trim trailing silence of no-prescription parts"),
    ("D4", "One unconfirmed alert per clinic outage, not per recording"),
    ("C2", "Segmenter survives a failed write; check free disk space"),
    ("U3", "Dashboard list of held sessions per PC"),
]

COLOUR = {H: GOODC, P: WARNC, G: CRIT}


def verdict(v):
    return '<font color="%s"><b>%s</b></font>' % (COLOUR[v].hexval().replace("0x", "#"), v)


def main():
    counts = Counter(r[5] for r in ROWS)
    by_id = {r[1]: r for r in ROWS}
    s = [Paragraph("AIMScribe v3 - Failure Review", H1),
         Paragraph("Every way the system can fail, what it does today, the evidence, and what to "
                   "fix next. Reviewed against the code as of 16 October 2026.", SUB)]

    s.append(tbl([["Verdict", "Count", "Meaning"],
                  [verdict(H), counts[H], "Handled, with a test or a live run as evidence"],
                  [verdict(P), counts[P], "Handled in part, or depends on CMED or the clinic"],
                  [verdict(G), counts[G], "Not handled: audio can be lost, misfiled or stuck"],
                  ["Total", len(ROWS), ""]],
                 [32 * mm, 18 * mm, 124 * mm]))
    s.append(Spacer(1, 6 * mm))
    s.append(Paragraph("The ten that matter most", H2))
    top = [["#", "Failure", "Consequence", "Fix"]]
    for n, rid in enumerate(TOP, 1):
        r = by_id[rid]
        top.append([f"{n}. {rid}", r[2], r[6], f"{r[7]} <i>({r[8]})</i>"])
    s.append(tbl(top, [14 * mm, 46 * mm, 50 * mm, 64 * mm]))
    s.append(Spacer(1, 4 * mm))
    s.append(Paragraph(
        "<b>Most urgent: U1.</b> A laptop clock only six seconds behind the server makes every "
        "grant fail, so nothing from that PC is ever uploaded. Since today's change those "
        "recordings are held rather than deleted, so nothing is lost - but nothing arrives "
        "either. Clinic laptops drift; this will happen.", BAD))

    s.append(PageBreak())
    s.append(part("PART 1", "Every failure mode, by component",
                  "What happens today, with the code it was checked against and the evidence."))
    s.append(Spacer(1, 4 * mm))
    current = None
    for r in ROWS:
        if r[0] != current:
            current = r[0]
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
