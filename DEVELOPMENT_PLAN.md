# AIMScribe v3 — Development Plan

The upgrade of the existing code to **SRS 3.2** (14 September 2026). We build on
the old project; nothing is rewritten from scratch.

## Where things are

| Folder | What it is | Came from |
|---|---|---|
| `recorder/` | The recorder on the doctor's PC (`AIMScribe_Agent.exe`) | GitHub `AIMScribe.exe` at `548b3a6` |
| `cmed-web/` | The dummy CMED app — becomes our test harness for the new protocol | same |
| `tools/` | Small operator scripts | same |
| `backend/` | The AIMS LAB server, database scripts, archive worker | GitHub `AIMScribe_Backend_Render` at `03a54c3` |

The first commit in this folder (`df6c647`) is the old code, unchanged. Every
later commit is a change against it, so `git diff df6c647` shows the whole
upgrade. This folder is local only; nothing here is pushed to GitHub yet.

## Decisions taken for v3

| # | Decision | Date |
|---|---|---|
| D1 | A recording is always filed under the **PC's enrolled hospital**. If CMED's `hospital_id` does not map to it, the recording is **refused** and an alert raised (`SRS-INV-01`, `SRS-GRT-10`). The old code used CMED's hospital and only warned; that is reversed. | 19 Sep 2026 |
| D2 | The old webhook that pushes AI-extracted prescription fields to CMED is **switched off** in v3 (kept in the code, disabled), because nothing clinical goes back to CMED (`SRS-DAT-03`). Revisit when a draft-prescription feature is designed. | 19 Sep 2026 |
| D3 | CMED sends nothing about consent. A refusal is recorded on the Stop button and deletes the recording everywhere (`SRS-CNS-01`–`10`). | 14 Sep 2026 |
| D4 | The doctor register stays a **directory, not a gate**: a doctor the server has not seen is added, not refused. The old code removed that gate on purpose after it stopped real consultations, and CMED's API 2 now proves each consultation. `SRS-GRT-03` asks for the check; the SRS should be updated to match. | 19 Sep 2026 |
| D5 | Phase 2 runs on the **existing server** (Render, Neon, Cloudflare R2) and keeps the same `/api/v2` API. Moving to the UIU server later changes hosting only (Phase 7). | 19 Sep 2026 |

## What the old code does, against SRS 3.2

| Area | Old code | SRS 3.2 needs |
|---|---|---|
| Starting a recording | Page sends a grant it signed itself, with consent inside | Page sends five plain fields; the server issues the grant (`§5`, `§6.1.3`) |
| Hospital | Taken from CMED, mismatch only warned | Taken from the PC; mismatch refused (D1) |
| Replies to the page | `event`/`data`, no `request_id`, `status` or `code` | Envelope with `request_id`, `status`, `code` (`§6.1.4`, Appendix A) |
| Ending a consultation | Any new `start` ends the current one at once | Gate: only after `prescription_built` (`§7.7`) |
| Stop and Pause | Tray menu and page commands | On-screen control with reasons; "Patient did not consent" deletes (`§7.8`, `§7.8a`) |
| Buffer on the PC | 40 GB; alerts at 50% and 80% | 4 GB; alert at 25% or 15 minutes (`SRS-SPL-06`, `-07`) |
| Deleting the PC's copy | 24 hours after a receipt; receipt only after archiving | At once; receipt on custody (`SRS-REC-15`–`18`) |
| Confirmation | None | Match every recording against CMED's API 2 (`§5.6`) |
| Channel B | None (an old `/api/v1/prescription` route exists) | `patient-information` and `prescription` with `X-CMED-Key` (`§6.2`) |
| Databases | One database mixing recording tables and old clinical tables | `aims_recordings` and `aims_clinical`, separate credentials (`§8.7`) |
| Archive | WAV plus manifest; SQLite catalogue; `D:/` path | WAV plus clinical JSON, same name; one catalogue; UIU path (`§7.10`, `§8.8`) |
| Cloud | One bucket; pieces deleted after archiving | Segment bucket plus locked copy bucket; FLAC copy before deletion (`SRS-ARC-08`–`16`) |
| Hosting | Render and Neon | UIU server, Docker Compose (`§10.3`) |
| AI results to CMED | Webhook pushes NER output | Off (D2) |

## Order of work

Each phase ends with its tests passing. Acceptance tests (`AT-nn`) are from SRS §12.1.

| Phase | Work | Done when |
|---|---|---|
| **1. Recorder, Channel A** — *done* | Reply envelope; five-field trigger; `prescription_built` and the gate; clinic from the PC, mismatch refused (D1); consent removed from the grant; buffer 4 GB, alert at 25%; no deletion wait | Unit tests; `AT-02`, `AT-09`–`AT-12`, `AT-31`, `AT-32` on a bench |
| **2. Server, authorisation and Channel B** — *done* | `POST /grant/mint` with register checks and D1; `clinical/patient-information` and `clinical/prescription` with `X-CMED-Key`; confirmation notices and matching; receipts on custody; refusal endpoint; webhook off (D2) | `AT-04`, `AT-57`–`AT-63`, `AT-68`–`AT-70`, `AT-78` |
| **3. Recorder talks to the new server** — *done* | Grant requested from the server alongside capture; "confirming" state; unconfirmed handling; refusal deletes local pieces; alert when a piece waits over 15 minutes; final delivery and clean-up at start-up and shutdown | `AT-01`, `AT-03`, `AT-07`, `AT-08`, `AT-20`, `AT-58`, `AT-59`, `AT-77` |
| **4. On-screen control** — *done* | Always-on-top Stop and Pause with reason form; "Patient did not consent" first | `AT-13`–`AT-15` |
| **5. Two databases** — *done* | Split into `aims_recordings` and `aims_clinical`; file-name columns; views for current and previous prescription; female and male tables | `AT-50`–`AT-56` |
| **6. Archive and cloud copy** — *done* | JSON beside each WAV; one catalogue; FLAC copy, two buckets, deletion order | `AT-33`, `AT-64`–`AT-67`, `AT-74`, `AT-75` |
| **7. UIU hosting** — *done* | Compose file for the UIU server: gateway, API, PostgreSQL, PgBouncer, workers, monitoring, backups | `AT-29`, `AT-30`, `AT-73` |
| **8. Tools for CMED and operators** — *done* | Test page; test Channel B environment; dummy CMED app on the new protocol; dashboard | `AT-27`, `AT-71`, `AT-72`; §8.9 |

## Rules while building

- Doctor PCs get an installer only: no `.bat`, no terminal.
- No secret in any file that could reach a public repository.
- Chain breaks are fixed in code, never by editing data.
- Old research audio in `D:\AIMSLAB_AUDIO_STORAGE` is never deleted.
- The speech-recognition code in `backend/src/processing/` is out of scope and left as it is.

## Progress

| Date | Phase | What changed | Tests |
|---|---|---|---|
| 19 Sep 2026 | 1 | `recorder/api/protocol.py` holds the Channel A contract: the five-field trigger, `prescription_built`, and every Appendix A code. The WebSocket server answers every command with `request_id`, `status` and `code`, and gets the grant from an authoriser (connected to the server in Phase 3). The session controller has the gate, files every recording under the PC's clinic and refuses a mismatch (D1), and carries reply codes on its refusals. Grants no longer need a consent claim. Buffer 4 GB, alert at 25%, deletion straight after a receipt. The development client sends the new messages. | 167 recorder tests pass (115 before) |

*(Resolved in Phase 3: the recorder now asks the server for each grant.)* The first chain entry still carries a consent-method field, now always
`reception`, so the chain format - and the shared wire vectors - are unchanged.
| 19 Sep 2026 | 2 | On the existing `/api/v2` server: `POST /grant/mint` issues 60-second Ed25519 grants for the PC's own clinic and refuses a mismatch (D1); `clinical/patient-information` and `clinical/prescription` take CMED's key, store a repeated request once, version changed prescriptions, and quarantine malformed ones with `422`; every recording is confirmed against API 2 (`pending`, `confirmed`, `unconfirmed` after two minutes, erased after 24 hours by `POST /maintenance/sweep`, which the archive worker calls each pass); receipts are issued the moment a piece is verified; `POST /session/refuse` erases a refused consultation and drops its clinical data; the AI webhook to CMED is off (D2). Sessions from old recorders are `legacy` and archive as before. | 144 server tests pass (105 before); 167 recorder tests unchanged |

### Putting Phase 2 on the current server

1. Apply `backend/scripts/010_v3_confirmation_channel_b.sql` to the Neon database (additive; existing sessions become `legacy`).
2. Generate an Ed25519 key pair. Put the private key in `AIMS_GRANT_PRIVATE_KEY` on Render; the public key goes into the recorder installer in Phase 3.
3. Leave `AIMS_CMED_WEBHOOK_ENABLED` unset (off).
4. Map each clinic: `POST /api/v2/admin/hospital` with `cmed_hospital_id`, once CMED gives its identifiers.
5. Issue CMED's key: `POST /api/v2/admin/cmed-key` (shown once; CMED keeps it on its server only).

Until the archive gains the cloud copy (Phase 6), `archive/complete` still deletes
pieces from R2 after archiving, as the old code did.

| 19 Sep 2026 | 3 | The recorder records first and asks the server alongside (SRS-GRT-07): the page gets `200 RECORDING_STARTED` when the grant arrives within 1.5 s, `202 RECORDING_PROVISIONAL` otherwise, and the recorder keeps asking. Every grant is verified against the pinned key and must be for this patient, doctor and PC's clinic, and unused. Nothing is uploaded before a grant; a hard refusal stops the recording and deletes it unsent (SRS-GRT-08). Where each consultation stands - waiting, granted, refused by the server, refused by the patient - is journalled, so a restart or an offline morning resumes correctly and the upload loop asks for grants once a consultation has closed. The session carries its grant to `/session/open`; while recording, the recorder asks every five seconds whether CMED's API 2 has confirmed it and shows unconfirmed after two minutes, never cutting. "Patient did not consent" stops at once, deletes every local piece and reports the refusal until the server acknowledges. An alert fires when a piece waits over 15 minutes; shutdown makes one last bounded delivery. The grant key is now `aimslab_grant_pub.pem`, issuer `aimslab`, in config and both installers. | 202 recorder tests pass (167 before); 144 server tests unchanged |

### Before building a recorder installer from this folder

`recorder/keys/aimslab_grant_pub.pem` is the old development key, renamed. Replace it
with the public half of the server's `AIMS_GRANT_PRIVATE_KEY` (Phase 2, step 2), or
every grant will be refused and every recording deleted. The on-screen Stop and
Pause control (Phase 4) offers "Patient did not consent".

| 19 Sep 2026 | 4 | The on-screen control (`recorder/ui/`): a small always-on-top window at the top right, shown only while recording or paused, with a red circular Stop and a blue rectangular Pause, usable by keyboard. Stop cuts the microphone the instant it is pressed, recorded as a pause so the gap is explained; the reason form then closes the session with the reason and the doctor's comment, or Cancel resumes. "Patient did not consent" is first and asks once - "Delete this recording? It cannot be recovered." - with No returning to the list. Pause takes effect only after a reason; the same button then resumes. Plain-words notes: checking with CMED, confirmed, not yet confirmed, microphone muted, and why a recording was not kept - each once per consultation, never blocking. The rules live in `overlay_model.py` (no tkinter) and are tested there; the window was checked on screen at 125% scaling. `BUILD.bat` no longer excludes tkinter, which would have shipped a recorder without the window. `AIMS_OVERLAY=false` turns it off on a bench. | 226 recorder tests pass (202 before) |

**Still open for the control.** The Stop and Pause reason lists are placeholders
until the clinical team agrees them (**OD-07**); they are one table in
`ui/overlay_model.py`. Level prompts for a quiet patient or clipping (`SRS-LVL-03`,
`-05`) need the per-piece level figures (`SRS-LVL-01`), which the recorder does not
compute yet.

| 19 Sep 2026 | 5 | CMED's clinical data moves to its own database, `aims_clinical` (`backend/scripts/clinical/`), reached through `AIMS_CLINICAL_DATABASE_URL`. The recordings database keeps only a five-field `confirmation_notices` row per API 2 - never a name or a body (SRS-DBA-20); this corrects Phase 2, which kept the bodies beside the recordings (`010` edited in place, as it was never deployed). Every message is first stored as received (`intake_records`, once per body) and then loaded into rows: patients, encounters (live and previous visit), demographics, paramedic observations, female and male details, prescriptions with one row per medicine, diagnoses and investigations; views give the current and previous prescription. A load that fails keeps the message and is retried by the sweep. Clinical reads are logged in an append-only table. Closing a session names its recording `PatientID_DoctorID_HospitalID_HHMMSS_HHMMSS_YYYYMMDD` (`011`), unique, and links the visit's encounter to it. A refusal erases the visit in both databases. The SQL is now tested on a real PostgreSQL 16, which found a parameter-type bug in `set_session_confirmation` and `erase_session` that would have failed every session open; fixed. | 152 server tests, 20 PostgreSQL tests, 226 recorder tests pass |

### Putting Phase 5 on the current server

1. Create a second Neon database, `aims_clinical`, and apply `backend/scripts/clinical/001_aims_clinical.sql`, then `002_roles.sql` (roles only; set their passwords in Neon, never in the repo).
2. Set `AIMS_CLINICAL_DATABASE_URL` on Render to the writer role's connection string. Without it the server falls back to the main database and logs a critical warning on every start.
3. Apply `backend/scripts/011_v3_file_names.sql` to the recordings database (needs `pg_trgm`, which Neon has). `010` is applied as it now stands; if the Phase 2 version was ever applied somewhere, drop its `clinical_records` and `clinical_quarantine` tables after checking they are empty.

**Testing the SQL.** `backend/tests/test_db_integration.py` builds both databases on a
portable PostgreSQL and runs against them; it is skipped unless the `pgserver`
package is installed (on this PC: `C:\Users\USER\AppData\Local\aimspg`, a short path
because the package breaks under long ones). The v1 scripts do not replay from an
empty database, so the test starts from the v1 tables as they stand after migration
001. The portable build has no `pgcrypto` or `pg_trgm`; the test leaves those lines
out, and only there.

**Still open for the databases.** Female and male details are kept whole in a
`details` column until the clinical team agrees their fields (**OD-15**); paramedic
readings outside the agreed columns are kept in `other` (**OD-16**).

| 20 Sep 2026 | 6 | Each archived consultation now keeps a JSON file beside its WAV, under the same name (§8.8): what CMED holds about the visit - patient, paramedic readings, previous visit, the current prescription - assembled by the server, because only it can read the clinical database, and written by the worker as the recording is archived. Pieces are no longer deleted when a recording is archived. The worker compresses the archived WAV to FLAC, decodes it again and compares the samples, encrypts the FLAC and the JSON with a key that stays at UIU, uploads both to a separate copy bucket, reads them back, and only then does the server record the copy and delete the pieces (`SRS-ARC-09`, in that order). A failed step keeps everything and raises an alert; a copy still missing a day later is raised by the sweep. A prescription that arrives after archiving marks the recording, and the JSON is written again and uploaded as a new object, the earlier ones kept (`SRS-ARC-13`). `restore.py` brings a copy back to a WAV and checks it against the catalogue, so the copy is a restore path and not a hope. The recording's name now comes from the server (`SRS-SES-05`) instead of being built again at UIU. | 97 server tests, 92 worker tests, 30 PostgreSQL tests, 226 recorder tests pass |

### Putting Phase 6 on the current server

**Where each half goes (6b).** The two halves of the journey have opposite costs,
so they go to different providers: **Cloudflare R2** keeps the pieces, because the
UIU server downloads every one of them and R2 charges nothing for that; **Amazon
S3 Glacier Deep Archive** keeps the lossless copy, because it is written once,
read almost never, and costs about a fifteenth of R2 to hold for years. The
clinical JSON is two kilobytes a visit, so it stays warm where it can be read at
once. SRS 3.3 records the change and the reasoning (§10.4).

1. Apply `backend/scripts/012_v3_cloud_copy.sql` to the recordings database (additive).
2. In AWS, make the copy bucket: versioning on, Object Lock in **governance** mode (not compliance - a copy may one day have to be erased for a refusal), and a lifecycle rule moving objects to `DEEP_ARCHIVE` at day 0. Choose the region for where the data may sit, not for price: `ap-south-1` (Mumbai) or `ap-southeast-1` (Singapore).
3. Issue an IAM user for it with `PutObject` and `GetObject`/`HeadObject` on that bucket and nothing else — **no delete**. Check that the existing R2 token, the one that deletes pieces, has no access to it, and that this one cannot touch the segment bucket (`AT-67`, `SRS-ARC-12`). The two credentials are the whole protection.
4. Make the warm bucket for the JSON — an R2 bucket is simplest — with its own token.
5. On Render, set `AIMS_COPY_BUCKET`, `AIMS_COPY_ENDPOINT` (e.g. `s3.ap-south-1.amazonaws.com`), `AIMS_COPY_ACCESS_KEY`, `AIMS_COPY_SECRET_KEY`, `AIMS_COPY_REGION` (the real region, not `auto`), and the matching `AIMS_JSON_*`. Leave `AIMS_JSON_*` unset to keep the JSON with the audio.
6. On the UIU machine, generate the copy key and set `AIMS_COPY_KEY` in the worker's `.env` only:
   `python -c "import base64,os; print(base64.b64encode(os.urandom(32)).decode())"`
   Keep one sealed offline copy of it. It never goes to Render, to either provider, or into either repository — and without it every cloud copy is unreadable, including ours.
7. `pip install -r backend/archive_worker/requirements.txt` (this adds `soundfile` and `cryptography`), or rebuild the worker image.

Until step 5 is done the server keeps the old behaviour - pieces deleted as soon
as a recording is archived - and says so in the log on every archive. Until step 6
is done the worker archives as usual and makes no copies.

**Put the restore drill in the calendar.** Copies are no longer fetched back one
by one, so the quarterly drill is what proves they can still become recordings
(`AT-74`, `SRS-ARC-07`): restore one day's copies to a spare machine with
`restore.py` and compare them with the archive. Allow a day - cold storage takes
hours to hand anything back.

**Restoring.** `python restore.py restore <file or folder> <destination>` decrypts
each object and decodes the FLAC back to WAV, refusing any file that does not
decode cleanly; `python restore.py check <file> <sha256>` compares it with the
hash in the catalogue (`AT-74`). Both need `AIMS_COPY_KEY`.

**Still open for the archive.** The copy runs on the same machine as the archive
and in the same process; if copying ever falls behind the recordings, it is the
part to move to its own worker. `SRS-ARC-14` asks for the copy the same night,
which the sweep now reports but nothing enforces.

| 20 Sep 2026 | 6b | The cloud copy moves to cold storage at a second provider. R2 keeps the pieces, where every download is free; the lossless copy goes to S3 Glacier Deep Archive at about $1 per TB a month against R2's $15, and the two-kilobyte clinical JSON is kept warm in its own bucket (`AIMS_JSON_*`), because cold storage bills a minimum object size and answers in hours. With that, a copy can no longer be downloaded again to check it - and doing so would have cost more each month in traffic than the copies cost to keep - so the server now asks the store what it holds and compares the size and the store's own fingerprint with what UIU sent, refusing anything that does not match: nothing is recorded, no piece is deleted, and a critical alert is raised. The quarterly restore drill is what now proves a copy usable. SRS 3.3 records the change (§10.4, `SRS-STO-01`, `SRS-ARC-09` step 5, `SRS-ARC-12` governance lock, `AT-65b`). | 103 server tests, 93 worker tests, 30 PostgreSQL tests, 226 recorder tests pass |

| 20 Sep 2026 | 7 | The whole server, as files: `deploy/uiu/` holds the Compose stack - gateway, API, transcription worker, archive worker, PostgreSQL 16, PgBouncer, Redis, nightly backups and a monitor - so a replacement machine is built from the repository and nothing else (`SRS-SRV-09`). Only the gateway publishes ports, 443 and the 80 that renews the certificate; the database, Redis and every worker sit on a Docker network with no route out (`SRS-TOP-02`, `SRS-SRV-07`). On first start PostgreSQL creates both databases with their own roles and applies the v1 baseline, every migration in order and the clinical schema - the same order and the same baseline file the PostgreSQL tests use, so a new server gets what the tests ran against. PgBouncer pools in transaction mode, and the driver's statement cache is turned off where that applies (`AIMS_DB_POOLER`), which is the failure that would otherwise appear only under load. Backups run nightly, encrypted, with a restore script that refuses to write over a live database. The monitor watches the four things that stop a clinic - disks, the archive queue, cloud copies falling behind, silent recorders, and the certificate - and says each thing once rather than every five minutes (`SRS-SRV-08`). `tools/load_test.py` runs a clinic day of fourteen rooms against a real server using the recorder's own signing code, and reports against §9.1 (`AT-29`). The README is the build: disks, LUKS, firewall, UPS, the move from Neon, updating one service at a time without interrupting a consultation (`AT-30`), what to check after an outage (`AT-73`), and the quarterly restore drill. | 51 deployment tests, 17 load-test tests, 103 server tests, 99 worker tests, 30 PostgreSQL tests, 226 recorder tests pass |

### Putting Phase 7 on the UIU machine

`deploy/uiu/README.md` is the procedure, start to finish. In short: prepare the
three kinds of disk and the firewall, fill in `.env`, `docker compose up -d`,
map the clinics and issue CMED's key, then move the data from Neon. Run
`tools/load_test.py` before the first clinic day, not after.

**Two things that must not be lost.** `AIMS_COPY_KEY` decrypts every cloud copy;
`AIMS_GRANT_PRIVATE_KEY` signs the grants whose public half is built into every
installed recorder. Each needs one sealed offline copy, and neither belongs in
either repository.

**Still open for the server.** Hardware, hosting and the line into the building
are `SRS-SRV-01`-`06` and are bought, not written: ECC memory, power-loss-
protected NVMe, RAID 6, a UPS that shuts the machine down cleanly, and a
symmetric 50 Mbit/s line (**OD-18**). The compose stack assumes them and says so,
but cannot check them from inside a container.

| 21 Sep 2026 | 8 | The dashboard (§8.9) answers one question - is anything wrong today, and where: volume by clinic and doctor, confirmation, quarantines and early stops against the room they happened in, every recorder and what it is holding, recordings that CMED never described and messages with no recording, and what is not yet safe in two places. Every count opens into the rows behind it, and a restore test can be written down where the dashboard can show it. It reads the recordings database only and holds no credential for the clinical one, so it cannot show a name (`SRS-DSH-06`); the page is one HTML file with no build step and keeps the administrator key in memory, never in storage. `tools/channel_b_test.py` gives CMED both halves of a test environment: a local check that applies the server's own rules to a message with nothing running, and a run of every case that matters against a test server - a good message, the same message twice, a broken one, a changed prescription, a wrong key, an oversized body - each checked against the code §6.2.4 promises. The dummy CMED app moves to the v3 protocol: it mints no grants (that code is deleted), sends the five fields and acts on the reply code, sends API 2 and API 3 from its own server with the key that never reaches a browser (`AT-72`), and has a protocol bench at `/protocol-test` showing every message and reply side by side. Consent left the interface: the page says where a refusal is recorded instead of collecting one. | 41 PostgreSQL tests (11 for the dashboard), 39 tool tests, 103 server tests, 99 worker tests, 51 deployment tests, 226 recorder tests; the CMED app type-checks and builds |

### Using what Phase 8 added

**The dashboard** is at `https://<server>/api/v2/dashboard`, behind the
administrator key. Leave it open on a wall screen: it refreshes every minute,
and clicking any count shows the recordings behind it. After a restore drill,
record it (`POST /api/v2/dashboard/restore-test`) so the "last restore test"
figure stops saying never.

**For CMED**, `cmed-web/README.md` is the integration in two pages: the order of
the four messages, the codes to act on, and the two commands that check a
message with nothing running. The protocol bench at `/protocol-test` is where
their engineers should start.

**Still open.** The dashboard has no chart library and draws its bars in CSS -
enough to see a pattern, not enough for analysis; if the research team wants
trends, that is a separate tool reading the same endpoints. `SRS-DSH-04` asks
for speech levels per room, which needs the per-piece level figures the
recorder does not compute yet (`SRS-LVL-01`).
