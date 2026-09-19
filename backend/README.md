# AIMScribe Backend

The server side of AIMScribe: clinical consultation recording for AIMS LAB.

This service does **two separate jobs** that share a database and are otherwise
independent:

| Job | Path | What it guarantees |
|---|---|---|
| **Integrity & archive** (protocol 2) | `/api/v2/*` | Every recording reaches the hospital's archive complete, unaltered, and provably so |
| **Bengali AI pipeline** (v1) | `/api/v1/*` | Transcription and medical entity extraction for prescriptions |

They are deliberately decoupled: if transcription fails, the audio is still
archived and receipted. If the AI is down, nothing about the evidential path
changes.

The Windows agent and the CMED web app live in
[AIMScribe.exe](https://github.com/SakibZaman021/AIMScribe.exe).

---

## Table of contents

- [Deployment](#deployment)
- [Repository layout](#repository-layout)
- [The integrity protocol](#the-integrity-protocol-apiv2)
- [The AI pipeline](#the-ai-pipeline-apiv1)
- [The archive worker](#the-archive-worker)
- [Database](#database)
- [Configuration](#configuration)
- [Running locally](#running-locally)
- [Administration](#administration)
- [Security posture](#security-posture)

---

## Deployment

```
   Consulting-room PCs              Vercel                    Render
   ───────────────────              ──────                    ──────
   AIMScribe agent  ──────────────► CMED web ────┐            FastAPI
   (25 laptops, 7 clinics)          (grants)     │            ├── /api/v2  integrity
          │                                      └──────────► └── /api/v1  AI
          │  presigned PUT                                          │
          ▼                                                         │
   Cloudflare R2  ◄──────────────────────────────────────────────────┤
   (transit only)                                                   │
          │                                              Neon Postgres
          │  presigned GET                               Redis (queue + cache)
          ▼                                              Azure OpenAI
   AIMS LAB server
   archive_worker  ──── outbound only ────────────────► Render
   D:\AIMSLAB_AUDIO_STORAGE
```

The AIMS LAB server holds the actual patient audio and **has no inbound ports**.
It pulls work from the backend and receives short-lived presigned URLs for
exactly the objects it needs, so it never holds bucket credentials either.

| Component | Runs on |
|---|---|
| API | Render — `https://aimscribe-backend-render.onrender.com` |
| Database | Neon (managed Postgres), `sslmode=require` |
| Transit object storage | Cloudflare R2, S3 API, bucket `aimscribe-audio` |
| Queue and cache | Redis |
| Transcription | Azure OpenAI `gpt-4o-transcribe` |
| NER | Azure OpenAI `gpt-5.2-chat` |
| Archive | AIMS LAB server, `D:\AIMSLAB_AUDIO_STORAGE` |

> **R2 is configured through the `MINIO_*` variables** because it is reached with
> the same S3-compatible client. `MINIO_REGION` must be `auto` for R2 — the
> region is part of the SigV4 signature, and a mismatch fails every presigned
> request with `SignatureDoesNotMatch`.

---

## Repository layout

```
src/
  main_fastapi.py      the deployed app: v1 routes + mounts the v2 router
  api_v2.py            protocol 2 — enrolment, sessions, chain, archive, admin
  db_v2.py             V2Repository
  integrity.py         chain parsing/verification, ReceiptSigner, identifier safety
  config.py            pydantic-settings, read from environment
  worker_async.py      transcription + NER worker
  main.py              legacy Flask entry point (superseded by main_fastapi)
  worker.py            legacy sync worker
  database/            postgres.py (sync), postgres_async.py (asyncpg)
  message_queue/       redis_client.py, redis_async.py
  storage/             minio_client.py — S3 client, used against R2
  processing/          transcriber_v2..v4, ner_extractor
  prompts/             NER and agent prompt templates
  webhooks/            cmed_webhook.py

archive_worker/        runs on the AIMS LAB server, outbound only
  worker.py            the poll loop
  archive.py           joining, hashing, path construction
  catalogue.py         _index.json / manifest.json

scripts/
  init_database.sql    base schema
  002_v2_integrity.sql … 009_close_reason.sql   migrations, applied in order
  mint_enrolment_tokens.py                      fleet enrolment
  check_schema.py
```

---

## The integrity protocol (`/api/v2`)

Three flows, three credentials. **Every route here requires authentication** —
the v1 routes have none.

| Header | Principal |
|---|---|
| `X-Device-Token` | An enrolled agent. Scoped to its own sessions. |
| `X-Worker-Key` | The archive worker. |
| `X-Admin-Key` | An administrator. |

### Agent flow

```
POST /device/enroll          one-time token → device_id, hospital_id, device_token
POST /session/open           ULID minted on the PC; genesis chain entry
POST /segment/authorize      presigned PUT for exactly one segment
POST /segment/commit         ← the important one
POST /session/pause|resume   signed lifecycle entries
POST /session/close          whole chain verified server-side
GET  /session/{id}/receipts  purge receipts, once archived
POST /heartbeat              state, spool pressure, pending segments
```

> **`/segment/commit` re-reads the uploaded object from R2 and recomputes its
> SHA-256 before storing anything.** A client's claim about what it uploaded is
> never taken on trust. A mismatch quarantines the session.

### Worker flow

```
GET  /archive/pending        sessions closed, verified, not archived
POST /archive/complete       → backend issues the purge receipts
```

### Admin flow

```
POST /admin/hospital                 create or rename (never changes hospital_id)
POST /admin/doctor                   directory entry for reports; grants nothing
POST /admin/enrollment-token         single use, ttl_hours ge=1 le=720
POST /admin/device/{id}/revoke       immediate
GET  /admin/alerts                   open integrity alerts
GET  /doctors?hospital_id=           device-authenticated
```

### Identity, and what the client may assert

| Identity | Source | Client can set it? |
|---|---|---|
| Hospital | The device's enrolment record | **No** |
| Doctor | The CMED grant, per consultation | Only from CMED's register |
| Patient | The CMED grant | Only from CMED |

A consulting room runs two shifts on one laptop, so the doctor cannot belong to
the machine. The hospital never changes for a PC, so it is never asked of the
client. `hospital_id` is also the top-level archive folder name and **must never
change**; display names may be changed freely.

### The hash chain

Each session carries an append-only Ed25519 chain keyed by
`(session_id, entry_no)`, covering open, every segment, every pause and resume,
and close. Each entry commits to the previous entry's hash, and every entry is
signed by the device key registered at enrolment.

The chain is verified **on the server at close**. A broken chain quarantines the
session rather than archiving it. A quarantined session is never repaired by
hand — a chain that can be hand-repaired proves nothing.

A retried entry is verified in full against its *own* `prev_hash`, which runs
every check except comparison with the stored head — the only check a legitimate
retry can fail, since the head moved past it precisely because it was accepted.

---

## The AI pipeline (`/api/v1`)

```
committed segment
      │
      ▼
Redis queue ──► worker_async ──► Azure OpenAI gpt-4o-transcribe
                                    Bengali, diarised:
                                    [ডাক্তার] [রোগী] [রোগীর সাথী]
                                          │
                                          ▼
                              transcripts (cumulative per session)
                                          │
                            ≥ NER_TRIGGER_CLIPS (2), or final
                                          ▼
                              ner_extractor → gpt-5.2-chat
                              9 extractors in parallel
                                          ▼
                                    ner_results
                                          │
                              CMED dashboard → prescription
```

The nine extractors: chief complaints, symptoms, diagnosis, medications, tests,
examination, follow-up, advice, referral.

Patient baseline and previous medications are fetched for context and cached in
Redis for an hour — this is what makes "continue the same medicine" work on a
follow-up visit.

NER runs from two clips onward so structure appears while the consultation is
still going, then again at close against the full transcript.

### Sample output

```json
{
  "chief_complaints": [{ "complaint": "জ্বর", "duration": "৩ দিন" }],
  "symptoms": [{ "symptom": "মাথা ব্যথা", "severity": "moderate", "duration": "৩ দিন" }],
  "diagnosis": [{ "condition": "Viral Fever", "type": "provisional" }],
  "medications": [{
    "name": "Paracetamol", "dose": "500mg",
    "frequency": "৩ বার", "duration": "৫ দিন", "instructions": "খাবার পরে"
  }],
  "tests": [{ "test": "CBC", "urgency": "routine" }],
  "follow_up": { "days": 5, "condition": "জ্বর না কমলে" },
  "advice": ["প্রচুর পানি পান করুন", "বিশ্রাম নিন"]
}
```

### v1 routes

`/api/v1/session/create`, `/upload/request`, `/upload/complete`,
`/session/{id}/status`, `/transcript/{id}`, `/ner/{id}`, `/doctor-review`,
`/prescription`, plus `/health`.

---

## The archive worker

Runs on the AIMS LAB server. **Listens on no port and accepts no connections.**

```
1. GET  /api/v2/archive/pending    sessions closed, verified, not archived
2. download each segment           verify sha256 against the manifest
3. join into one WAV               atomic write, then fsync
4. re-read from disk and hash      proves the bytes actually landed
5. write manifest.json, _index.json
6. POST /api/v2/archive/complete   backend issues the purge receipts
```

A session is reported complete only after step 4. Any failure leaves it pending,
the agent keeps its local audio, and the next pass retries.

### Archive layout

```
D:\AIMSLAB_AUDIO_STORAGE\<HOSPITAL>\<DOCTOR>\<YYYY-MM-DD>\<CONSULTATION>\<CONSULTATION>.wav
```

The date is the **hospital's local date**, resolved through its configured
timezone. Using UTC would file an evening consultation at UTC+6 under the
previous day — permanently, since `session_date` decides the folder once.

### Running it

```powershell
$env:AIMS_BACKEND_URL  = "https://aimscribe-backend-render.onrender.com"
$env:AIMS_WORKER_KEY   = "<worker key>"
$env:AIMS_ARCHIVE_ROOT = "D:\AIMSLAB_AUDIO_STORAGE"
python archive_worker\worker.py
```

| Variable | Default | Purpose |
|---|---|---|
| `AIMS_POLL_SECONDS` | 30 | Poll interval |
| `AIMS_BATCH_SIZE` | 5 | Sessions per pass |
| `AIMS_DISK_HEADROOM_BYTES` | 20 GB | Refuses to write below this |
| `AIMS_DOWNLOAD_TIMEOUT` | 600 | Per segment |
| `AIMS_VERIFY_TLS` | true | Never disable |

The worker refuses to start if `AIMS_BACKEND_URL` is not HTTPS.

---

## Database

Managed Postgres on Neon. Apply `scripts/init_database.sql`, then the numbered
migrations in order.

### Protocol-2 tables

| Table | Holds |
|---|---|
| `hospitals` | `hospital_id` (**immutable** — the archive folder name), display name, timezone |
| `doctors` | Directory for reports. Grants nothing. |
| `devices` | One row per enrolled PC: pubkey, machine facts, `revoked_at` |
| `enrollment_tokens` | `token_sha256` (**bytea; the plaintext is never stored**), expiry, `used_at`, `device_id` |
| `sessions` | ULID id, identities, consent, times, `close_reason`, manifest, quarantine, archive path/hash/bytes, retention, legal hold |
| `chain_entries` | `(session_id, entry_no)`, type, payload, hashes, signature |
| `segments`, `clips` | Per-clip metadata |
| `purge_receipts` | Signed proofs the agent may act on |
| `integrity_alerts` | The operator's queue |
| `audit_log` | **Append-only**, hash-linked |
| `used_grants` | Server-side `jti` replay record |
| `api_keys` | Hashed keys with scope and expiry |

### AI tables

`patients`, `health_screenings`, `transcripts`, `ner_results`,
`previous_visits`, `prescription_data`, `doctor_reviews`.

### Views

`v_doctors`, `v_doctor_activity`, `v_doctor_register`, `v_audio_files`,
`v_abnormal_closes`, `v_session_pauses`, `patient_recordings`.

> `patient_recordings` is a **view**. `playing_with_neon` is a leftover
> provisioning sample and can be dropped.

### The append-only audit log

A trigger raises on both `UPDATE` and `DELETE`:

```
audit_log is append-only; DELETE is not permitted
```

This is load bearing. The record that a session was opened, archived and later
removed must outlive the session rows. **Any cleanup that includes `audit_log`
rolls back its entire transaction and deletes nothing.**

Child tables have foreign keys without cascades. To delete a set of sessions,
attempt each child table inside a savepoint and retry the ones a constraint
still blocks until the blocked set stops shrinking — that settles the order from
the live constraint graph rather than hard-coding one a later migration would
invalidate.

---

## Configuration

All from the environment; `.env` is read locally and is **gitignored**.

| Variable | Notes |
|---|---|
| `POSTGRES_HOST` / `_PORT` / `_DB` / `_USER` / `_PASSWORD` | Neon; `POSTGRES_SSLMODE=require` |
| `POSTGRES_POOL_MIN` / `_MAX` | Default 2 / 10 |
| `AZURE_TRANSCRIBE_ENDPOINT` / `_API_KEY` / `_DEPLOYMENT` / `_API_VERSION` | `gpt-4o-transcribe` |
| `AZURE_NER_ENDPOINT` / `_API_KEY` / `_DEPLOYMENT` | `gpt-5.2-chat` |
| `MINIO_ENDPOINT` | R2 S3 endpoint |
| `MINIO_ACCESS_KEY` / `_SECRET_KEY` / `_BUCKET` | `aimscribe-audio` |
| `MINIO_REGION` | **`auto`** for R2 |
| `MINIO_SECURE` | `true` |
| `REDIS_HOST` / `_PORT` / `_PASSWORD` / `_SSL` | `REDIS_SSL=true` for Upstash |
| `AIMS_ADMIN_KEY` | Administration routes |
| `AIMS_WORKER_KEY` | Archive worker |
| `AIMS_RECEIPT_PRIVATE_KEY` | Ed25519; signs purge receipts. Its public half is pinned on every agent. |
| `AIMS_ALLOWED_ORIGINS` | Exact browser origins for CORS |
| `AIMSCRIBE_WEBHOOK_SECRET` | CMED NER webhook |
| `NER_TRIGGER_CLIPS` | Default 2 |
| `WORKER_CONCURRENCY` | |

Never commit real values. The repository is public.

---

## Running locally

```powershell
python -m venv venv
.\venv\Scripts\activate
pip install -r requirements.txt
```

Provide Postgres, Redis and an S3-compatible store (Docker is fine for all
three), fill in `.env`, then:

```powershell
python scripts\setup.py            # apply schema
python tests\test_azure_apis.py    # verify Azure connectivity
```

Two processes:

```powershell
uvicorn src.main_fastapi:app --host 0.0.0.0 --port 6000   # API
python src\worker_async.py                                # transcription + NER
```

Check with `curl http://localhost:6000/health`.

`src/main.py` and `src/worker.py` are the older Flask/sync entry points, kept for
reference. The deployed app is `src/main_fastapi.py`.

---

## Administration

### Mint enrolment tokens

One row per **laptop**. A token binds a machine to a hospital and decides
nothing else — the doctor arrives with each consultation from CMED.

```csv
hospital_id,hospital_name,room,doctor_id,doctor_name
HOSP003,Dholpur,Room 1,,
HOSP004,Shyampur,Room 1,,
```

```powershell
$env:AIMS_TOKEN_TTL_HOURS = "720"     # 30 days; 720 is the hard maximum
python scripts\mint_enrolment_tokens.py scripts\laptops_add_20260804.csv
```

Writes one instruction sheet per PC plus a token-free `register.csv`.

> The database stores only `sha256(token)`. **The plaintext exists nowhere but
> that generated sheet.** If it is lost, mint a new one. Delete the folder once
> the machines are installed.

Write a new dated CSV rather than editing an existing one — these files are read
as the register of what is deployed.

### Current fleet

| `hospital_id` | Clinic |
|---|---|
| `HOSP001` | Karail |
| `HOSP002` | Mirpur |
| `HOSP003` | Dholpur |
| `HOSP004` | Shyampur |

Naryanganj, Ershadnagar and Amader Susastho have no id yet.

---

## Security posture

**Enforced today**

- Every `/api/v2` route authenticated; sessions scoped to the owning device.
- Server-side re-hash of every uploaded object at commit.
- Ed25519 chain per session, verified server-side at close; broken chain ⇒ quarantine.
- Purge receipts, so local audio is deleted only against proof of a verified archive copy.
- Append-only audit log enforced by trigger.
- Enrolment tokens single-use, hospital-bound, stored only as SHA-256.
- CORS restricted to exact origins, `allow_credentials=False`.
- Archive worker holds no bucket credentials and accepts no inbound connections.

**Known gaps**

- `/api/v1` routes remain unauthenticated.
- `AIMSCRIBE_WEBHOOK_SECRET` is hardcoded and should be rotated.
- `D:\AIMSLAB_AUDIO_STORAGE` has no backup.

Report vulnerabilities privately to the AIMS LAB team; see [`SECURITY.md`](SECURITY.md).

---

© 2026 AIMS LAB. Proprietary.
