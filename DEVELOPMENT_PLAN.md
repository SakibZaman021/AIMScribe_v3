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
| **3. Recorder talks to the new server** | Grant requested from the server alongside capture; "confirming" state; unconfirmed handling; refusal deletes local pieces; alert when a piece waits over 15 minutes; final delivery and clean-up at start-up and shutdown | `AT-01`, `AT-03`, `AT-07`, `AT-08`, `AT-20`, `AT-58`, `AT-59`, `AT-77` |
| **4. On-screen control** | Always-on-top Stop and Pause with reason form; "Patient did not consent" first | `AT-13`–`AT-15` |
| **5. Two databases** | Split into `aims_recordings` and `aims_clinical`; file-name columns; views for current and previous prescription; female and male tables | `AT-50`–`AT-56` |
| **6. Archive and cloud copy** | JSON beside each WAV; one catalogue; FLAC copy, two buckets, deletion order | `AT-33`, `AT-64`–`AT-67`, `AT-74`, `AT-75` |
| **7. UIU hosting** | Compose file for the UIU server: gateway, API, PostgreSQL, PgBouncer, workers, monitoring, backups | `AT-29`, `AT-30`, `AT-73` |
| **8. Tools for CMED and operators** | Test page; test Channel B environment; dummy CMED app on the new protocol; dashboard | `AT-27`, `AT-71`, `AT-72`; §8.9 |

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

**Until Phase 3 is done**, a recorder built from this folder answers every
`start` with `503 AGENT_NOT_READY` in production mode: the server can now issue
grants (Phase 2), but the recorder does not ask for them yet. Run it with `AIMS_REQUIRE_GRANT=false` to try it on a
bench. The first chain entry still carries a consent-method field, now always
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
