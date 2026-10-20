#!/usr/bin/env python3
"""
Build LOCAL_MANUAL.pdf - every address and login on this laptop, filled in, and a
step-by-step check for every issue the system was built to handle.

    python tools/make_local_manual.py

It reads the passwords out of deploy/local/.env when you run it, so this file
holds no secret and is safe in the repository. The PDF it writes is NOT:
LOCAL_MANUAL.pdf is in .gitignore and must stay on this machine - it holds every
password of the local system, and both AIMScribe repositories are public.
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

from reportlab.lib.units import mm
from reportlab.platypus import KeepTogether, PageBreak, Paragraph, Preformatted, Spacer

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cmed_doc_style import (BAD, BODY, CODE, GOOD, H1, H2, H3, NOTE, SUB,  # noqa: E402
                            build, part, tbl)

ROOT = Path(__file__).resolve().parent.parent
ENV = ROOT / "deploy" / "local" / ".env"
OUT = ROOT / "LOCAL_MANUAL.pdf"


def read_env(path: Path) -> dict:
    if not path.is_file():
        sys.exit(f"{path} not found. Run deploy/local/bootstrap.py first - it writes the .env.")
    values = {}
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def c(text: str) -> str:
    return f"<font face='Courier'>{text}</font>"


def code(text: str):
    return Preformatted(text, CODE)


W3 = [36 * mm, 70 * mm, 68 * mm]


def check(n, title, protects, steps, see, where):
    """One manual check: why it matters, what to do, what you should see, where."""
    rows = [["", f"{n} &middot; {title}"],
            ["Protects", protects],
            ["Do this", "<br/>".join(f"{i}. {s}" for i, s in enumerate(steps, 1))],
            ["You should see", see],
            ["Where to look", where]]
    return KeepTogether([tbl(rows, [26 * mm, 148 * mm]), Spacer(1, 3 * mm)])


def main() -> int:
    env = read_env(ENV)

    def v(key, fallback="(not in .env)"):
        return c(env.get(key) or fallback)

    port = env.get("AIMS_PORT", "6060")
    pgport = env.get("POSTGRES_PORT", "5433")
    dash = f"http://localhost:{port}/api/v2/dashboard"
    S = []

    S.append(Paragraph("AIMScribe v3 - Local Manual", H1))
    S.append(Paragraph(f"This laptop's system: every address and login, and a check for every "
                       f"issue. Generated {datetime.now():%d %B %Y, %H:%M} from deploy/local/.env.",
                       SUB))
    S.append(Paragraph("<b>This document holds live passwords. Keep it on this machine.</b> "
                       "LOCAL_MANUAL.pdf is in .gitignore: never attach it, upload it or put it "
                       "on GitHub. The DigitalOcean server has different passwords, generated "
                       "there; nothing here applies to it.", BAD))
    S.append(Spacer(1, 3 * mm))
    S.append(tbl([["Part", "What is in it"],
                  ["1", "The words in plain language: droplet, migration, bucket, and the rest"],
                  ["2", "Every page and every login on this laptop"],
                  ["3", "Starting, stopping and checking the system"],
                  ["4", "Manual checks, one for every issue we handled (30)"],
                  ["5", "The automated tests, and cleaning up after testing"],
                  ["6", "If something looks wrong"]],
                 [16 * mm, 158 * mm]))

    # ------------------------------------------------------------------ part 1
    S.append(PageBreak())
    S.append(part("PART 1", "The words, in plain language",
                  "Each is something you will meet in the manuals, the dashboard or the logs."))
    S.append(Spacer(1, 3 * mm))
    S.append(tbl([["Word", "What it means here"],
                  ["Droplet", "DigitalOcean's name for one rented server - a computer in a data "
                              "centre that runs our whole server side. \"A folder on the "
                              "droplet\" means a folder on that one computer's disk: if the "
                              "droplet is lost, so is everything only stored there."],
                  ["Database", "Where the system keeps its records. There are two: "
                               + c("aims_recordings") + " (each recording, its clips, who it "
                               "belongs to, the signed record) and " + c("aims_clinical")
                               + " (what CMED sends: patient details, prescriptions)."],
                  ["Schema", "The shape of a database: which tables exist and which columns "
                             "each has. New code often needs a new table or column."],
                  ["Migration", "A small file (" + c("013_...sql") + " to " + c("016_...sql")
                                + ") that changes the schema of a database that already exists "
                                "- adds the new table or column - without touching the data. A "
                                "brand-new server gets all of them automatically; a server "
                                "already running needs each new one applied once, by hand, "
                                "before the new code arrives. The server now checks at start-up "
                                "and says which file is missing (" + c("/health") + ")."],
                  ["Why migrations happen", "Every time we taught the system something new - "
                                            "the wrong-patient move (013), the offline log "
                                            "(014), per-patient files (015), what each PC "
                                            "reports (016) - the database needed somewhere to "
                                            "keep it. Without the file, the new code has nowhere "
                                            "to write and that step fails."],
                  ["R2 / bucket", "Cloudflare R2 is cloud storage. A bucket is one named "
                                  "storage area in it. We use three: the clips as they upload, "
                                  "the permanent copy of each consultation, and the nightly "
                                  "database backups. On this laptop, MinIO plays R2's part."],
                  ["Backup / off-site", "A nightly encrypted copy of both databases. Off-site "
                                        "means it is also copied off the droplet, to its own R2 "
                                        "bucket, so losing the droplet does not lose it."],
                  ["Recorder / exe / agent", "AIMScribe_Agent.exe on the doctor's PC: records, "
                                             "signs, holds and uploads the audio."],
                  ["Channel A", "CMED's doctor page talking to the recorder on the same PC "
                                "(API 1 = patient opened, API 3a = prescription built)."],
                  ["Channel B", "CMED's server talking to ours (API 2 = patient details, API "
                                "3b = the prescription)."],
                  ["Armed", "The prescription was built, so the next different patient closes "
                            "this recording and starts a new one."],
                  ["Marker / click", "A signed note inside a recording: \"patient X opened at "
                                     "second 66.4\" or \"prescription built\". The server uses "
                                     "them to decide whose audio is whose."],
                  ["Parts", "When one recording held two consultations, the archive cuts it at "
                            "the exact second into one file per patient."],
                  ["Spool", "The recorder's encrypted store of audio on the PC, until the "
                            "server has a verified copy."],
                  ["Grant", "The server's signed permission for one recording."],
                  ["Heartbeat", "Every 30 s each recorder tells the server it is alive, plus its "
                                "version, clock, held recordings and disk encryption."],
                  ["Dashboard", "The operations page: recorders, recordings, alerts, review "
                                "lists."],
                  ["Monitor", "On the server, a watchman that checks disks, backups, the "
                              "schema, certificates and silent recorders, and alerts."]],
                 [34 * mm, 140 * mm]))

    # ------------------------------------------------------------------ part 2
    S.append(PageBreak())
    S.append(part("PART 2", "Every page and every login",
                  "Filled in from deploy/local/.env on this laptop."))
    S.append(Spacer(1, 3 * mm))
    S.append(Paragraph("2.1 &middot; Pages", H2))
    S.append(tbl([["Page", "Address", "How you get in"],
                  ["<b>CMED page - manual tests</b>", c("http://localhost:3000/manual-test"),
                   "no login; every check in Part 4 starts here"],
                  ["CMED page - doctor view", c("http://localhost:3000/dashboard"),
                   "no login; type a doctor id and clinic"],
                  ["CMED page - protocol bench", c("http://localhost:3000/protocol-test"),
                   "no login"],
                  ["<b>Operations dashboard</b>", c(dash),
                   "it asks for the admin key (2.2) and keeps it only in that tab"],
                  ["Server health", c(f"http://localhost:{port}/health"),
                   "nothing; look for " + c('"status":"healthy"') + " and " + c('"schema":"ok"')],
                  ["Database viewer (Adminer)", c("http://localhost:8080"), "see 2.3"],
                  ["Object storage (MinIO)", c("http://localhost:9001"),
                   "user and password from 2.2"],
                  ["The recorder", c("ws://127.0.0.1:5050/ws") + "<br/>"
                   + c("http://127.0.0.1:5050/health"), "the CMED page connects by itself"]],
                 W3))
    S.append(Paragraph("2.2 &middot; Logins and keys", H2))
    S.append(tbl([["What it opens", "Username", "Password or key"],
                  ["Operations dashboard", "-", v("AIMS_ADMIN_KEY")],
                  ["Database superuser", v("POSTGRES_SUPERUSER", "aims_admin"),
                   v("POSTGRES_SUPERUSER_PASSWORD")],
                  ["The server's own login (may add to the audit log, never change it)",
                   c("aims_api"), v("AIMS_API_DB_PASSWORD")],
                  ["aims_recordings (owner; for migrations only, no login on a new server)",
                   c("aims_recordings"), v("AIMS_RECORDINGS_PASSWORD")],
                  ["aims_clinical (writer)", c("aims_clinical_writer"),
                   v("AIMS_CLINICAL_PASSWORD")],
                  ["Monitor (read only)", c("aims_monitor"), v("AIMS_MONITOR_PASSWORD")],
                  ["Object storage", v("MINIO_ACCESS_KEY"), v("MINIO_SECRET_KEY")],
                  ["Redis", "-", v("REDIS_PASSWORD")],
                  ["CMED test hospital client (token + FHIR)", v("AIMS_CMED_CLIENT_ID"),
                   "private key: " + c("deploy\\local\\out\\cmed-keys\\") + " (never "
                   "leaves this PC); key id " + v("AIMS_CMED_KEY_ID")],
                  ["CMED interim static key (X-CMED-Key; refused unless it names its "
                   "hospitals)", "-", v("AIMS_CMED_KEY")],
                  ["Archive worker key", "-", v("AIMS_WORKER_KEY")],
                  ["Cloud-copy encryption key", "-", v("AIMS_COPY_KEY")]],
                 [52 * mm, 50 * mm, 72 * mm]))
    S.append(Paragraph("2.3 &middot; The database viewer", H2))
    S.append(tbl([["Field", "Type exactly"],
                  ["System", "PostgreSQL"],
                  ["Server", c("postgres") + " (the container's name - not localhost)"],
                  ["Username", v("POSTGRES_SUPERUSER", "aims_admin")],
                  ["Password", v("POSTGRES_SUPERUSER_PASSWORD")],
                  ["Database", c("aims_recordings") + " or " + c("aims_clinical")]],
                 [40 * mm, 134 * mm]))
    S.append(Spacer(1, 2 * mm))
    S.append(Paragraph("Without a browser, from the AIMScribe_v3 folder: " + c("python tools/db.py "
                       "sessions") + " (recent recordings), " + c("python tools/db.py patient "
                       "&lt;id&gt;") + ", " + c("python tools/db.py sql \"select ...\"") + ". It "
                       "needs no password and only reads. From another program on this laptop "
                       "the database is on " + c(f"localhost:{pgport}") + ".", BODY))

    # ------------------------------------------------------------------ part 3
    S.append(PageBreak())
    S.append(part("PART 3", "Starting, stopping and checking",
                  "The server side runs in Docker; the recorder is a Windows program."))
    S.append(Spacer(1, 3 * mm))
    S.append(tbl([["To", "Do this"],
                  ["Start everything", "Start Docker Desktop and wait for the whale to settle: "
                                       "every container starts itself. First time ever: "
                                       + c("deploy\\local\\start.bat")],
                  ["Start the recorder", c("recorder\\dist\\AIMScribe_Agent\\AIMScribe_Agent.exe")
                   + " (a clinic PC installs it and it starts at logon)"],
                  ["Stop the recorder", "tray icon &rarr; Exit (administrator), or Task Manager"],
                  ["See the containers", c("cd deploy\\local") + " then " + c("docker compose ps")],
                  ["Stop the server side", c("python deploy\\local\\bootstrap.py --down")
                   + " (keeps all data)"],
                  ["Restart only the server", c("docker compose restart api")],
                  ["Read the server's log", c("docker compose logs --tail 50 api")],
                  ["Prove the recorder answers", c("python tools\\channel_a_test.py")
                   + " &rarr; " + c("24/24 as expected, Channel A: READY")],
                  ["Prove the server answers", "open " + c(f"http://localhost:{port}/health")]],
                 [44 * mm, 130 * mm]))
    S.append(Spacer(1, 2 * mm))
    S.append(Paragraph("<b>Faster tests.</b> Some checks wait minutes by design. To shorten them, "
                       "add these lines to " + c("recorder\\dist\\AIMScribe_Agent\\.env")
                       + " and restart the recorder - and remove them afterwards:", BODY))
    S.append(code("AIMS_COUNSELLING_LIMIT_SECONDS=30      # normally 120\n"
                  "AIMS_NO_ACTIVITY_LIMIT_SECONDS=90      # normally 900\n"
                  "AIMS_PREFERRED_MICROPHONE=Realtek      # this PC's own microphone, to test the\n"
                  "                                       # 'keep it at 100%' check without a Jabra"))

    # ------------------------------------------------------------------ part 4
    S.append(PageBreak())
    S.append(part("PART 4", "Manual checks, one for every issue",
                  "Each starts on the CMED manual-test page unless it says otherwise. The page "
                  "names patients TEST_..., which Part 5 removes afterwards."))
    S.append(Spacer(1, 3 * mm))
    S.append(Paragraph("Where results show: the dashboard's recordings table (one row per file, "
                       "with the rule that labelled it), its Recorders, Needs review and Held on "
                       "PCs panels, its alerts, and the archive folder "
                       + c("deploy\\local\\archive\\&lt;clinic&gt;\\&lt;doctor&gt;\\&lt;date&gt;\\")
                       + ", one folder per patient file holding the .wav, its .json and its "
                       ".manifest.json. Allow about a minute after a recording closes for it to "
                       "be labelled and archived.", NOTE))
    S.append(Spacer(1, 2 * mm))

    S.append(Paragraph("4A &middot; Consultations and labels (CMED manual-test page)", H2))
    checks = [
        ("Normal consultation", "the everyday case",
         ["Card 1: open patient A, talk 30 s", "Build A's prescription", "Open patient B"],
         "A's recording closes at B's click; one file for A, rule R1; a new recording for B",
         "dashboard recordings; archive folder TEST_NORMAL_A_..."),
        ("Wrong patient picked, corrected", "the doctor's wrong pick at the start",
         ["Card 2: open A (the mistake)", "Within 10 s open B, talk 70 s",
          "Build B's prescription", "Stop"],
         "both clicks answered RECORDING_STARTED, the same recording; ONE file, under B, rule "
         "R3+R1; A's API 2 kept, with no audio",
         "dashboard recordings; the file name starts with B's id"),
        ("Deliberate click, then back", "a doctor looking at another file in the first minute",
         ["Card 3: open A, talk 40 s", "Open B for 10 s", "Open A again", "Build A's prescription",
          "Stop"],
         "ONE file, under A, rule R2 - B's look did not cut the recording",
         "dashboard recordings"),
        ("Glance mid-consultation", "a look at another file during a long consultation",
         ["Card 4: open A, talk 90 s", "Open B for 10 s", "Back to A, build A's prescription",
          "Stop"],
         "ONE file, under A, rule R2", "dashboard recordings"),
        ("Follow-up with no prescription, then the next patient",
         "the follow-up case (Sakib, then Salma)",
         ["Card 5: open A, talk 70 s, NO prescription", "Open B, talk 70 s",
          "Build B's prescription", "Stop"],
         "TWO files from one recording: A from 0 to B's click (rule R4), B from there (R1); "
         "joined, they are exactly the signed recording",
         "dashboard recordings; two folders; check 30 shows the join"),
        ("Counselling limit", "no recording of the doctor's time after a prescription",
         ["Card 6: open A, talk 30 s", "Build A's prescription", "Keep talking, click nothing"],
         "the recording closes itself 2:00-2:10 after the prescription (30 s with the faster "
         "setting), reason counselling_limit",
         "the page's event feed; dashboard close reason"),
        ("No-activity limit", "a recording nobody ends",
         ["Card 7 (use the faster setting in Part 3)", "Open A, no prescription, click nothing"],
         "the recording closes itself after 15 min (90 s with the faster setting), reason "
         "no_activity_limit", "the event feed; dashboard close reason"),
        ("Same patient clicked twice", "double clicks and page reloads",
         ["Card 8: open A", "Open A again, then again"],
         "the same recording each time; no extra marker; one file", "dashboard recordings"),
        ("API 2 arriving late", "CMED's server offline, catching up later",
         ["Card 9: open A WITHOUT sending API 2, build the prescription, Stop",
          "Wait until it is archived (unconfirmed)", "Send A's API 2"],
         "the recording turns confirmed and its JSON is rewritten with A's details - nothing "
         "was deleted while it waited", "dashboard confirmation column; the .json"),
        ("Previous-visit sync", "CMED sending an old visit again",
         ["Card 10: send a previous visit", "Send the same again", "Send it changed"],
         "STORED, then ALREADY_STORED, then UPDATED (version 2)", "the card's replies"),
    ]
    n = 0
    for title, protects, steps, see, where in checks:
        n += 1
        S.append(check(n, title, protects, steps, see, where))

    S.append(Paragraph("4B &middot; The doctor's PC", H2))
    pcs = [
        ("Microphone unplugged", "a USB microphone or headset pulled out mid-consultation",
         ["Needs a USB microphone or headset plugged in and chosen as default",
          "Start any card, talk", "Pull the USB plug out, keep talking"],
         "within about 3 s the on-screen control says 'Microphone changed. Still recording, on "
         "this PC's microphone'; the recording continues; alert microphone_switched",
         "the recorder's on-screen control; dashboard alerts"),
        ("Jabra opened by name, kept at 100%", "no one has to choose the microphone in Windows",
         ["Set AIMS_PREFERRED_MICROPHONE to this PC's microphone (Part 3), restart the recorder",
          "Windows Sound settings: turn that microphone down to 40%", "Wait 30-60 s, do not record"],
         "the level goes back to 100% and unmuted by itself", "Windows Sound settings"),
        ("No sleep while recording", "a laptop sleeping during a long consultation",
         ["Windows power settings: sleep after 1 minute", "Start a recording, touch nothing for "
          "3 minutes", "Stop it, wait 2 minutes"],
         "the PC stays awake while recording and sleeps after it stops (restore your setting)",
         "the PC itself"),
        ("Windows restart mid-consultation", "a restart or shutdown during a recording",
         ["Start a recording, talk", "Restart Windows", "After logon, start the recorder (an "
          "installed one starts itself)"],
         "the recording was closed cleanly with reason pc_shutdown; its audio up to the restart "
         "is archived", "dashboard close reason"),
        ("Power cut or crash", "a hard stop that cannot close anything",
         ["Start a recording, talk at least 20 s", "Task Manager: End task on AIMScribe_Agent",
          "Start the recorder again within 10 minutes"],
         "alert unexpected_agent_exit saying how many seconds of the clip in progress were saved "
         "(at most ~3 s lost); the consultation resumes as a second recording linked to the "
         "first (continues / continued_by in both JSONs)",
         "dashboard alerts; the two .json files"),
        ("Disk full", "a laptop disk filling mid-recording",
         ["Not safe to do for real on this PC", "Run the automated check: "
          + c("cd recorder; python -m pytest -q tests/test_disk_full.py")],
         "15 passed: audio kept in memory and written once space returns, one disk_full alert, "
         "new starts refused under 1 GB", "the test output"),
        ("A PC installed while offline", "enrolment when the clinic's internet is down",
         ["Automated: " + c("cd recorder; python -m pytest -q tests/test_enrol_retry.py")],
         "3 passed: the token is tried every minute until the server answers", "the test output"),
    ]
    for title, protects, steps, see, where in pcs:
        n += 1
        S.append(check(n, title, protects, steps, see, where))

    S.append(Paragraph("4C &middot; Network, server and dashboard", H2))
    net = [
        ("Internet down", "a clinic losing its connection to our server",
         ["In deploy\\local: " + c("docker compose stop api"),
          "Do card 1 (API 2 will fail - expected)", "Wait 2 minutes, then "
          + c("docker compose start api")],
         "the recording kept on the PC uploads by itself and is archived; the dashboard's "
         "Recorders panel shows the offline period with what happened in it",
         "dashboard Recorders panel 'Offline this week'"),
        ("Laptop clock wrong", "a slow or fast PC clock stopping uploads",
         ["Windows: Settings &rarr; Time: turn off 'Set time automatically', set the clock 5 "
          "minutes fast", "Do card 1", "Turn 'Set time automatically' back on"],
         "the recording uploads and archives normally; the dashboard's Clock column shows about "
         "-300 s; alert clock_skew (once)", "dashboard Recorders panel and alerts"),
        ("A database migration missing", "new server code on an old database",
         ["Open " + c(f"http://localhost:{port}/health"),
          "Automated, removing each migration in turn: " + c("python tools/run_server_tests.py")],
         c('"schema":"ok"') + " now; if a file were missing it would say which, and the "
         "dashboard would show schema_missing", "/health"),
        ("Old recorder versions", "PCs left on an old build",
         ["Open the dashboard Recorders panel"],
         "this PC shows 3.4.0, not flagged; a PC below 3.4.0 would be flagged and raise "
         "recorder_outdated once a day", "dashboard Recorders panel"),
        ("Disk not encrypted", "a stolen laptop",
         ["Open the dashboard Recorders panel"],
         "Disk column 'not encrypted' for this PC (its C: has no BitLocker) and one "
         "disk_not_encrypted alert a day; turning BitLocker on clears it",
         "dashboard Recorders panel"),
        ("Recordings held on a PC", "the server refusing a PC (revoked, wrong clinic)",
         ["Open the dashboard Held on PCs panel"],
         "empty now; a refused recording would be listed with its PC and reason, kept on the "
         "PC, never deleted", "dashboard Held on PCs"),
        ("Labels that need a person", "the rare case the rules cannot settle",
         ["Open the dashboard Needs review panel"],
         "any recording flagged labelling_review, with its files and the rule used",
         "dashboard Needs review"),
        ("One alert per outage", "an alert flood during a CMED outage",
         ["Do cards 1 and 9 several times without sending API 2"],
         "one unconfirmed_recording alert for the clinic with a count, not one per recording",
         "dashboard alerts"),
        ("False alarm on short recordings", "noise that teaches people to ignore alerts",
         ["Do card 8 (very short recordings)"],
         "no segment_count_mismatch alert - it is raised only if clips are still missing 10 "
         "minutes after the close", "dashboard alerts"),
    ]
    for title, protects, steps, see, where in net:
        n += 1
        S.append(check(n, title, protects, steps, see, where))

    S.append(Paragraph("4D &middot; Archive, storage and backups", H2))
    store = [
        ("The cut files rejoin to the signed recording", "proof a cut file was not altered",
         ["After check 5, from backend\\archive_worker:",
          c("python restore.py join --sha256 &lt;recording's sha256&gt; out.wav "
            "&lt;part 1 .wav&gt; &lt;part 2 .wav&gt;"),
          "The recording's sha256 is in either part's .manifest.json"],
         "'2 part(s) ... matches'", "the command's output"),
        ("Server disk filling", "the archive disk running out",
         ["Automated: " + c("cd backend; python -m pytest -q archive_worker")],
         "all pass, including release-on-pressure: oldest copied-and-verified files leave "
         "first, never one without a cloud copy", "the test output"),
        ("Backups off the server", "losing the droplet losing the backups",
         [c("bash tools/check_backup_offsite.sh")],
         "both databases dumped, encrypted, copied off-site with sizes checked, fetched back "
         "and restored with every recording; everything it made is then removed",
         "the script's output"),
        ("Late prescription changing a cut", "a 3b arriving after the files were archived",
         ["Automated: archive_worker test_parts.py (recut) and backend "
          "test_labelling_server.py"],
         "the parts are rejoined, checked against the signed hash, cut again and replaced",
         "the test output"),
    ]
    for title, protects, steps, see, where in store:
        n += 1
        S.append(check(n, title, protects, steps, see, where))

    # ------------------------------------------------------------------ part 5
    S.append(PageBreak())
    S.append(Paragraph("4E &middot; Security: CMED's login, the closed doors, the audit log", H2))
    kid = env.get("AIMS_CMED_KEY_ID", "&lt;key id&gt;")
    sec = [
        ("CMED's token method and FHIR, all cases", "a stolen or misused CMED credential",
         ["From the AIMScribe_v3 folder:",
          c(f"python tools/channel_b_test.py --server http://localhost:{port} "
            "--client-id cmed-aalo-dholpur --kid " + kid + " --key-file "
            "deploy\\local\\out\\cmed-keys\\cmed-aalo-dholpur.private.pem "
            "--hospital AALO_DHOLPUR --patient TEST_FHIR01")],
         "11/11 as expected: a token for this hospital; the same signed note refused the second "
         "time; another hospital's patient refused (403 HOSPITAL_NOT_ALLOWED); no token refused",
         "the command's output; dashboard alert cmed_hospital_mismatch"),
        ("The CMED page sends FHIR with a token", "the reference CMED copies",
         ["Open " + c("http://localhost:3000/api/config-check"), "Then do card 1"],
         c('"channel_b_token_method": {"set": true ...}') + "; card 1 confirms as before",
         "config-check page; dashboard"),
        ("The old open v1 API is gone", "anyone reading transcripts without a login",
         ["Open " + c(f"http://localhost:{port}/api/v1/transcript/x") + " and "
          + c(f"http://localhost:{port}/docs")],
         "both: Not Found (404)", "the browser"),
        ("FHIR discovery", "a standard FHIR client setting itself up",
         ["Open " + c(f"http://localhost:{port}/fhir/r4/metadata")],
         "a CapabilityStatement naming the token address", "the browser"),
        ("Repeated wrong keys lock that key out - not the address", "guessing keys, "
         "without one bad PC blocking its whole clinic",
         ["Open the dashboard and enter the same wrong admin key 10 times",
          "Then enter the right admin key"],
         "the 11th wrong try answers 429 'Too many requests' (that wrong key, for 15 "
         "minutes; one auth_failures alert); the right key still opens the dashboard at once. "
         "The counters are in Redis, so restarting the api does not clear them - they expire "
         "by themselves", "dashboard alerts"),
        ("Every reply has a request id; errors are problem+json", "tracing a failure across "
         "PC, gateway and server",
         ["Run: " + c(f"curl.exe -i http://localhost:{port}/api/v2/heartbeat -X POST")],
         "401 with an " + c("X-Request-ID") + " header and a body of type "
         + c("application/problem+json") + " carrying the same " + c("request_id")
         + "; the api log line for it shows the id in brackets", "the command's output, "
         + c("docker logs aimscribe-v3-server")),
        ("Alive and ready are separate", "a server that is up but cannot store recordings",
         ["Open " + c(f"http://localhost:{port}/health/live") + " and "
          + c(f"http://localhost:{port}/health/ready"),
          "In deploy\\local: " + c("docker compose stop minio") + ", open /health/ready "
          "again, then " + c("docker compose start minio")],
         "live: 200 always. ready: 200, then 503 while storage is stopped, then 200 again",
         "the browser"),
        ("The audit log cannot be changed by the server", "a break-in hiding its tracks",
         ["In the database viewer, log in as " + c("aims_api") + " (password in 2.2)",
          "Run: " + c("UPDATE audit_log SET event_type = 'x' WHERE false;")],
         "permission denied for table audit_log", "the viewer's output"),
        ("A browser message cannot erase a consultation (recorder 3.5.0 only)",
         "a program posing as CMED's page",
         ["Only after installing the 3.5.0 recorder on this PC - the installed 3.4.0 still "
          "erases on this message, so do not try it with a real recording",
          "On the CMED protocol-test page start a TEST recording, then send Stop with reason "
          + c("patient_did_not_consent")],
         "the recording stops and is KEPT (archived as usual); only the recorder's own "
         "on-screen 'patient refused' control erases", "dashboard recordings"),
        ("A refusal removes the archive too (recorder 3.5.0)", "a refused patient's audio "
         "left on the server or in the cloud",
         ["Make a TEST recording, wait until it is archived (dashboard shows Archived: yes)",
          "Within 7 days, press the recorder's own 'patient refused' for it"],
         "the dashboard no longer lists it; its WAV and JSON are gone from "
         + c("deploy\\local\\archive") + "; its cloud copy is deleted; an audit line "
         "without patient data remains", "dashboard; the archive folder"),
        ("Every PC request is signed (recorder 3.5.0)", "a copied PC token being used "
         "elsewhere",
         ["After the 3.5.0 recorder has run for a minute, in the database viewer: "
          + c("SELECT device_id, signs_requests FROM devices;")],
         c("signs_requests = true") + " for this PC; from then on a request with only its "
         "token is refused (401)", "the viewer's output"),
        ("The installer check", "a clinic laptop that will not install or record",
         ["After installing 3.5.0: Start menu, " + c("AIMScribe - check this PC")],
         "a PASS/WARN/FAIL table; FAIL only for real problems (unsigned files in a "
         "developer build are expected to FAIL here)", "the check window"),
    ]
    for title, protects, steps, see, where in sec:
        n += 1
        S.append(check(n, title, protects, steps, see, where))

    S.append(part("PART 5", "Automated tests, and cleaning up",
                  "Every check above also has automated tests; run them after any change."))
    S.append(Spacer(1, 3 * mm))
    S.append(tbl([["Suite", "From the AIMScribe_v3 folder", "Expect"],
                  ["Recorder", c("cd recorder; python -m pytest -q -p no:cacheprovider tests"),
                   "all pass (about 385)"],
                  ["Archive worker", c("cd backend; python -m pytest -q archive_worker"),
                   "all pass (about 155)"],
                  ["Server, deployment, monitor", c("cd deploy\\uiu; python -m pytest -q tests"),
                   "all pass (about 87)"],
                  ["Server with a real database", c("python tools/run_server_tests.py")
                   + " (starts a throwaway PostgreSQL and removes it afterwards)",
                   "all pass (about 441)"]],
                 [36 * mm, 92 * mm, 46 * mm]))
    S.append(Paragraph("Cleaning up after manual testing", H2))
    S.append(Paragraph("Every check creates patients named TEST_... . Remove them from both "
                       "databases, the object store and the archive:", BODY))
    S.append(code("python tools/purge_test_data.py           # shows what would go\n"
                  "python tools/purge_test_data.py --apply   # removes it"))
    S.append(Paragraph("It works only on this laptop's containers. The audit log and the clinical "
                       "access log keep their entries: they are append-only by design, and the "
                       "server's own login (aims_api) cannot change them - so the system's "
                       "history cannot be quietly rewritten.", NOTE))

    # ------------------------------------------------------------------ part 6
    S.append(part("PART 6", "If something looks wrong", ""))
    S.append(Spacer(1, 3 * mm))
    S.append(tbl([["You see", "Do this"],
                  ["The CMED page says the recorder is not connected",
                   "Start the recorder (Part 3); check " + c("http://127.0.0.1:5050/health")],
                  [c('"status":"degraded"') + " on /health",
                   "Read which part: database, storage or " + c("schema") + ". "
                   + c("docker compose ps") + ", then the api log"],
                  [c('"schema":"missing 0xx_...sql"'),
                   "Apply that file to the database (the DigitalOcean manual shows the "
                   "command); it clears within a minute"],
                  ["A recording never archives", c("python tools/db.py sessions")
                   + " - its status; the dashboard alerts for it; the recorder keeps it and retries"],
                  ["The dashboard asks for a key again", "it keeps the key only in that tab; "
                   "paste the admin key from 2.2"],
                  ["Adminer: connection refused", "Server must be " + c("postgres") + ", not "
                   "localhost"],
                  ["Nothing works after a restart", "start Docker Desktop and wait; every "
                   "container starts itself"]],
                 [62 * mm, 112 * mm]))

    build(S, OUT, "AIMScribe v3 Local Manual",
          "AIMScribe v3 - Local Manual - CONTAINS PASSWORDS - keep on this machine")
    print(f"wrote {OUT} ({n} checks)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
