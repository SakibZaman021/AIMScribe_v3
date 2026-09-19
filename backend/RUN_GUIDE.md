# AIMScribe Backend — Run Guide

Getting the backend running, locally and on Render. For what it does and why,
see [`README.md`](README.md).

---

## What has to be running

The backend is **two processes** against four external services:

| Process | Responsibility |
|---|---|
| API (`src/main_fastapi.py`) | `/api/v2` integrity routes + `/api/v1` AI routes |
| Worker (`src/worker_async.py`) | Transcription and NER, off the Redis queue |

| Service | Local | Production |
|---|---|---|
| Postgres | Docker | Neon, `sslmode=require` |
| Redis | Docker | Managed Redis (`REDIS_SSL=true` for Upstash) |
| S3-compatible storage | MinIO in Docker | Cloudflare R2, `MINIO_REGION=auto` |
| Azure OpenAI | — | `gpt-4o-transcribe`, `gpt-5.2-chat` |

The API alone will accept and archive recordings. Without the worker there are
no transcripts and no NER — but the audio still reaches the archive and is still
receipted. That separation is deliberate.

---

## Local setup

### 1. Python

```powershell
python -m venv venv
.\venv\Scripts\activate
python -m pip install --upgrade pip
pip install -r requirements.txt
```

### 2. Backing services

```powershell
docker compose up -d
docker compose ps
```

Expect `aimscribe-postgres`, `aimscribe-redis` and `aimscribe-minio` healthy.

### 3. Environment

Create `.env` in the repository root. It is gitignored — **this repository is
public, so never commit real values.**

```env
POSTGRES_HOST=localhost
POSTGRES_PORT=5432
POSTGRES_DB=aimscribe_db
POSTGRES_USER=aimscribe_user
POSTGRES_PASSWORD=<local password>
POSTGRES_SSLMODE=prefer

AZURE_TRANSCRIBE_ENDPOINT=https://<resource>.cognitiveservices.azure.com/
AZURE_TRANSCRIBE_API_KEY=<key>
AZURE_TRANSCRIBE_DEPLOYMENT=gpt-4o-transcribe
AZURE_TRANSCRIBE_API_VERSION=2025-03-01-preview

AZURE_NER_ENDPOINT=https://<resource>.openai.azure.com/
AZURE_NER_API_KEY=<key>
AZURE_NER_DEPLOYMENT=gpt-5.2-chat

MINIO_ENDPOINT=localhost:9000
MINIO_ACCESS_KEY=aimscribe
MINIO_SECRET_KEY=aimscribe123
MINIO_BUCKET=aimscribe-audio
MINIO_SECURE=false
MINIO_REGION=us-east-1

REDIS_HOST=localhost
REDIS_PORT=6379

AIMS_ADMIN_KEY=<random 32+ bytes, base64url>
AIMS_WORKER_KEY=<random 32+ bytes, base64url>
AIMS_RECEIPT_PRIVATE_KEY=<Ed25519 PKCS#8 PEM>
AIMS_ALLOWED_ORIGINS=http://localhost:3000
```

`AIMS_RECEIPT_PRIVATE_KEY` signs purge receipts. Its public half must be the
`aimslab_receipt_pub.pem` pinned on every agent — if they do not match, agents
verify every receipt as invalid and **never delete local audio**. Generate a
matching pair with `recorder/scripts/dev_keys.py` in the agent repository.

### 4. Schema

```powershell
python scripts\setup.py
```

Applies `scripts/init_database.sql` and the numbered migrations
(`002_v2_integrity.sql` … `009_close_reason.sql`) in order. Verify with:

```powershell
python scripts\check_schema.py
```

### 5. Verify Azure

```powershell
python tests\test_azure_apis.py
```

### 6. Run

Two terminals:

```powershell
.\venv\Scripts\activate
uvicorn src.main_fastapi:app --host 0.0.0.0 --port 6000 --reload
```

```powershell
.\venv\Scripts\activate
python src\worker_async.py
```

```powershell
curl http://localhost:6000/health
```

| Service | URL | Local credentials |
|---|---|---|
| API | http://localhost:6000 | — |
| MinIO console | http://localhost:9001 | `aimscribe` / `aimscribe123` |
| Postgres | localhost:5432 | `aimscribe_user` / `aimscribe123` |
| Redis | localhost:6379 | — |

> `src/main.py` (Flask) and `src/worker.py` (sync) are earlier entry points kept
> for reference. The deployed app is `src/main_fastapi.py`.

---

## Production (Render)

Start command:

```
uvicorn src.main_fastapi:app --host 0.0.0.0 --port $PORT
```

The worker runs as a second service with the same environment, started with
`python src/worker_async.py`.

Differences from local:

| Setting | Production value |
|---|---|
| `POSTGRES_SSLMODE` | `require` |
| `MINIO_ENDPOINT` | `<account>.r2.cloudflarestorage.com` |
| `MINIO_REGION` | **`auto`** |
| `MINIO_SECURE` | `true` |
| `REDIS_SSL` | `true` |
| `AIMS_ALLOWED_ORIGINS` | the exact CMED origin |

Every secret is set in the Render dashboard, never in the repository.

### After deploying

```powershell
curl https://aimscribe-backend-render.onrender.com/health
```

Then confirm an admin route answers with the key set in Render:

```powershell
curl -X POST https://aimscribe-backend-render.onrender.com/api/v2/admin/hospital `
  -H "X-Admin-Key: <key>" -H "Content-Type: application/json" `
  -d '{"hospital_id":"HOSP001","name":"Karail","timezone":"Asia/Dhaka"}'
```

---

## The archive worker

Runs on the AIMS LAB server, not on Render. It listens on nothing.

```powershell
$env:AIMS_BACKEND_URL  = "https://aimscribe-backend-render.onrender.com"
$env:AIMS_WORKER_KEY   = "<worker key>"
$env:AIMS_ARCHIVE_ROOT = "D:\AIMSLAB_AUDIO_STORAGE"
python archive_worker\worker.py
```

It refuses to start if `AIMS_BACKEND_URL` is not HTTPS, and refuses to write
below `AIMS_DISK_HEADROOM_BYTES` (20 GB), leaving sessions pending rather than
filling the volume.

---

## Troubleshooting

### `SignatureDoesNotMatch` on every presigned URL

`MINIO_REGION` is wrong. R2 requires `auto`; the region is part of the SigV4
signature.

### Agents enrol but never delete local audio

The receipt key pair does not match. `AIMS_RECEIPT_PRIVATE_KEY` here must be the
private half of the `aimslab_receipt_pub.pem` pinned on the agents. This is
fail-safe by design — audio is retained, not lost — but the spool will grow.

### Sessions stay `pending` forever

The archive worker is not running, cannot reach the backend, or the volume is
below its headroom. No receipts are issued, so no PC deletes anything. Check the
worker's log first.

### A session is quarantined

The chain failed verification, or a committed object's hash did not match on
re-read. Look at `quarantine_reason` on the session and the matching
`integrity_alerts` row. **Do not attempt to repair the chain** — a chain that can
be hand-repaired proves nothing. Fix the code path that broke it.

### `audit_log is append-only; DELETE is not permitted`

Working as intended. Remove `audit_log` from whatever cleanup raised it; the
whole transaction rolled back, so nothing was deleted.

### `column "..." does not exist`

Migrations were not fully applied. Run `python scripts\check_schema.py`, then
apply the missing numbered migration.

### Transcripts never appear

NER waits for `NER_TRIGGER_CLIPS` (2) clips, roughly a minute of speech. If
clips are arriving and transcripts are not, check the worker process and Azure
connectivity with `tests/test_azure_apis.py`.

---

## Quick reference

```powershell
docker compose up -d                                        # backing services
python scripts\setup.py                                     # schema
python scripts\check_schema.py                              # verify schema
uvicorn src.main_fastapi:app --host 0.0.0.0 --port 6000     # API
python src\worker_async.py                                  # AI worker
python archive_worker\worker.py                             # archive (AIMS LAB only)
python scripts\mint_enrolment_tokens.py <laptops.csv>       # enrolment tokens
curl http://localhost:6000/health                           # health
```
