<div align="center">

# AIMScribe v3

**Secure clinical audio capture, joined to the prescription of the same visit.**

[![tests](https://github.com/SakibZaman021/AIMScribe_v3/actions/workflows/tests.yml/badge.svg)](https://github.com/SakibZaman021/AIMScribe_v3/actions/workflows/tests.yml)
![Python](https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-async-009688?logo=fastapi&logoColor=white)
![PostgreSQL](https://img.shields.io/badge/PostgreSQL-16-4169E1?logo=postgresql&logoColor=white)
![Windows](https://img.shields.io/badge/Recorder-Windows-0078D6?logo=windows&logoColor=white)
![Docker](https://img.shields.io/badge/Deploy-Docker-2496ED?logo=docker&logoColor=white)

[Architecture](#4-high-level-architecture) ·
[Recorder ↔ server](#7-how-aimscribeexe-communicates-with-the-server) ·
[Data flows](#9-data-flow-diagrams) ·
[Databases](#10-database-dbms-architecture) ·
[ER diagrams](#11-entityrelationship-diagrams) ·
[Security](#13-security-architecture)

</div>

AIMScribe records doctor–patient consultations in clinics running CMED's EHR, verifies
every second of audio with a device-signed hash chain, and links each recording to the
patient information and prescription CMED sends for the same `visit_id` — producing
research datasets of *audio + structured prescription* with a provable chain of custody.

| | |
|---|---|
| 🎙️ **Never loses audio** | Records offline into an encrypted, crash-safe spool; deletes only against server-signed receipts |
| 🔐 **Zero shared secrets** | TPM-bound PC identities, per-hospital CMED key pairs, 5-minute tokens, signed grants |
| 🧾 **Tamper-evident** | Ed25519-signed hash chain per recording, append-only audit log, database-level data guard |
| ✅ **Consent-aware** | Only CMED-confirmed recordings are kept; refusals and unconfirmed audio are erased everywhere |
| 🏥 **One installer, every hospital** | Clinic identity comes from the data; CMED's addresses are distributed signed from the server |
| 🚀 **Safe operations** | CI-gated pull deployments, backup before every schema change, automated restore drills |

---

# System Architecture

> **Scope.** This document describes the production architecture of AIMScribe v3: the
> Windows recorder (`AIMScribe.exe`), the AIMS LAB server, its two PostgreSQL databases,
> the storage tiers, and the integration with CMED's EHR. It is written for engineers who
> operate, extend or audit the system.
>
> **Versions described.** Recorder `3.8.x` · device protocol `2` · CMED clinical interface
> `v4` · database schema through migration `033`.

---

## Contents

1. [What AIMScribe does](#1-what-aimscribe-does)
2. [Glossary](#2-glossary)
3. [Architectural principles](#3-architectural-principles)
4. [High-level architecture](#4-high-level-architecture)
5. [Deployment architecture](#5-deployment-architecture)
6. [Component architecture](#6-component-architecture)
7. [How AIMScribe.exe communicates with the server](#7-how-aimscribeexe-communicates-with-the-server)
8. [CMED integration (Channel A and Channel B)](#8-cmed-integration-channel-a-and-channel-b)
9. [Data flow diagrams](#9-data-flow-diagrams)
10. [Database (DBMS) architecture](#10-database-dbms-architecture)
11. [Entity–relationship diagrams](#11-entityrelationship-diagrams)
12. [Lifecycles and state machines](#12-lifecycles-and-state-machines)
13. [Security architecture](#13-security-architecture)
14. [Reliability, failure modes and operations](#14-reliability-failure-modes-and-operations)
15. [Data retention and erasure](#15-data-retention-and-erasure)
16. [Build, test and release pipeline](#16-build-test-and-release-pipeline)
17. [Configuration reference](#17-configuration-reference)
18. [Repository map](#18-repository-map)

---

## 1. What AIMScribe does

AIMScribe records doctor–patient consultations in clinics that use CMED's EHR, and joins
each recording to the clinical record of the same visit, so that research datasets of
*audio + structured prescription* can be built with a verifiable chain of custody.

| Concern | How it is met |
|---|---|
| **Capture** | A background Windows agent on every consultation PC records from the clinic speakerphone (Jabra Speak2 55), falling back to the PC microphone and never stopping a recording because a device was unplugged. |
| **Control** | CMED's EHR page starts a recording when the doctor opens a patient (API 1). The recording ends when the next patient is opened, when the doctor presses Stop, or two minutes after the prescription is built. |
| **Clinical context** | CMED's server sends the patient information (API 2) and the prescription (API 3) to the AIMS LAB server, keyed by the same `visit_id`. |
| **Integrity** | Every audio segment is hashed into a signed chain on the PC; the server verifies the chain before anything is archived, and issues signed purge receipts before the PC may delete its copy. |
| **Consent and confirmation** | Only recordings that CMED's server confirms (a matching API 2) are kept. Unconfirmed recordings are erased automatically; a patient refusal erases a recording everywhere. |
| **Research access** | An authenticated portal (dashboard, bulk downloads, read-only DB logins) for AIMS LAB staff and researchers. |

---

## 2. Glossary

| Term | Meaning |
|---|---|
| **API 1** | CMED page → recorder on the same PC (`localhost:5050`): *start recording this visit*. WebSocket, or HTTP for testing tools. |
| **API 2** | CMED server → AIMS LAB server: patient information for a visit. |
| **API 3** | CMED server → AIMS LAB server: the prescription for a visit. |
| **Channel A** | The local channel on the consultation PC (API 1). |
| **Channel B** | The server-to-server channel (API 2, API 3). |
| **visit_id** | CMED's identifier of one consultation. Joins API 1, API 2, API 3 and the audio. |
| **Session** | One recording on one PC (`sessions.session_id`, a ULID). |
| **Segment** | A piece of a session's audio (WAV), cut on silence, uploaded as soon as it is sealed. |
| **Chain** | The append-only, hash-linked, device-signed list of events of a session (open, segment, pause, marker, close). |
| **Grant** | A short-lived, single-use, server-signed authorisation to record a given visit on a given PC. |
| **Purge receipt** | A server-signed statement that a segment/session is safely archived and the PC may delete it. |
| **Consultation part** | A labelled time range inside a session belonging to one patient (a session can span profile switches). |
| **Trusted origin** | A web address of CMED's EHR from which the recorder accepts API 1. Kept centrally, distributed signed. |
| **CMED client** | One hospital's server identity at CMED: `client_id` + public key(s) (`kid`). |

---

## 3. Architectural principles

1. **Never lose audio.** Recording never waits for the network. The PC holds an encrypted,
   crash-safe spool; an outage is a delay, never data loss. Local copies are deleted only
   against a signed purge receipt.
2. **The server decides, the client asserts.** The browser page sends five plain fields;
   the server checks them against its own register and mints a grant. No client-side
   secret can authorise a recording.
3. **Evidence over trust.** Each PC has a hardware-bound identity (TPM); each CMED hospital
   has its own key pair; every state change is chained, signed and audited.
4. **Least privilege everywhere.** Separate DB roles, separate storage credentials per
   bucket (the transit token cannot read the copy bucket; the copy token cannot delete),
   the copy-encryption key lives in exactly one process.
5. **Fail safe, not fail open.** Unknown origins, expired tokens, unregistered keys,
   unmigrated schemas and failed CI all *refuse*. An outage never causes a block or an
   erase (erasure has a circuit breaker).
6. **One artefact for everyone.** A single installer serves every hospital; clinic identity
   comes from the data (API 1 `hospital_id`), and CMED's addresses come from the server.
7. **Pull-based operations.** The server pulls releases from GitHub; nothing on GitHub can
   reach the server.

---

## 4. High-level architecture

```mermaid
flowchart LR
    subgraph Clinic["Clinic (one of N hospitals)"]
        direction TB
        DR(["Doctor"])
        subgraph PC["Consultation PC — Windows"]
            EHR["CMED EHR page<br/>(browser)"]
            REC["AIMScribe.exe<br/>recorder agent"]
            MIC[["Jabra Speak2 55<br/>(fallback: PC mic)"]]
        end
    end

    subgraph CMED["CMED (EHR vendor)"]
        CS["CMED server"]
    end

    subgraph LAB["AIMS LAB server (Docker host)"]
        GW["Gateway<br/>Caddy · TLS · auth"]
        API["API<br/>FastAPI"]
        AW["Archive worker"]
        MON["Monitor"]
        BK["Backup"]
        DB[("PostgreSQL 16<br/>aims_recordings<br/>aims_clinical")]
    end

    subgraph Cloud["Object storage"]
        R2[("Transit bucket<br/>S3-compatible (R2)")]
        COPY[("Copy bucket<br/>cold storage, encrypted")]
        OFF[("Off-site backups<br/>encrypted, locked")]
    end

    IDP["Google<br/>OpenID Connect"]
    GH["GitHub<br/>source · CI"]
    STAFF(["AIMS LAB staff<br/>researchers"])

    DR -- "opens patient" --> EHR
    EHR -- "API 1 · localhost WS/HTTP" --> REC
    MIC --> REC
    CS -- "API 2 · API 3 · OAuth2 JWT" --> GW
    EHR -. "patient data" .-> CS
    REC -- "HTTPS · device token + signatures" --> GW
    REC -- "PUT segments (presigned)" --> R2
    GW --> API
    API --> DB
    API -- "presign / delete" --> R2
    AW -- "pull, verify, join WAV" --> R2
    AW -- "encrypted copy" --> COPY
    AW <--> API
    BK -- "nightly + WAL" --> OFF
    BK --> DB
    MON --> API
    MON --> DB
    STAFF -- "dashboard · downloads" --> GW
    GW -- "sign-in" --> IDP
    LAB -- "pull main (autodeploy)" --> GH
```

**Three identities, three channels.**

| Channel | Caller → callee | Authentication |
|---|---|---|
| API 1 (Channel A) | CMED page → recorder, same PC | Browser `Origin` must be in the centrally managed, signed trusted-origin list; loopback only; Windows-session check |
| Recorder ↔ server | `AIMScribe.exe` → AIMS LAB API | TPM-bound device key → device token; per-request signatures and nonces; TLS interception check |
| API 2/3 (Channel B) | CMED server → AIMS LAB API | HL7 SMART Backend Services: per-hospital key pair, signed JWT assertion → 5-minute bearer token |

---

## 5. Deployment architecture

### 5.1 Server topology

All server components run as Docker containers on one host, on three networks. Only the
gateway is reachable from the internet.

```mermaid
flowchart TB
    NET(("Internet"))

    subgraph edge["network: edge"]
        GW["gateway<br/>caddy:2.8<br/>:80 :443 (h1/h2/h3)"]
    end

    subgraph internal["network: internal (no internet)"]
        API["api<br/>FastAPI · uvicorn<br/>:6000"]
        PGB["pgbouncer<br/>transaction pooling"]
        PG[("postgres:16<br/>aims_recordings<br/>aims_clinical")]
        RD[("redis:7<br/>AOF · noeviction")]
        AW["archive-worker<br/>+ decrypt service"]
        WK["worker<br/>(transcription pipeline)"]
        MON["monitor"]
        BK["backup"]
        ADM["dbviewer<br/>adminer (portal-gated)"]
    end

    subgraph egress["network: egress (outbound only)"]
        E(("Object storage<br/>external APIs"))
    end

    NET --> GW
    GW -- "reverse_proxy" --> API
    GW -- "forward_auth /auth/check" --> API
    GW -- "/db (admins, researchers)" --> ADM
    API --> PGB --> PG
    API --> RD
    WK --> RD
    WK --> PGB
    AW -- "/api/v2/archive/* (worker key)" --> API
    API -. egress .-> E
    AW -. egress .-> E
    WK -. egress .-> E
    BK --> PG
    BK -. "off-site copies" .-> NET
    MON --> API
    MON --> PG
    MON -. "alerts (mail/webhook)" .-> NET
    ADM --> PG
```

| Container | Responsibility | Notes |
|---|---|---|
| `gateway` | TLS (ACME), HTTP/3, security headers, CSP, 2 MB body limit, portal auth via `forward_auth`, hard 404 for internal routes (`/api/v1`, `/docs`, `/api/v2/archive`, `/api/v2/maintenance`) | The only public listener |
| `api` | All HTTP APIs: device protocol, grants, Channel B (v4 JSON + FHIR R4), CMED OAuth, portal, dashboard, downloads, maintenance sweep | Stateless; scales horizontally behind PgBouncer |
| `pgbouncer` | Connection pooling to PostgreSQL | Transaction mode |
| `postgres` | Two databases (§10) | Tuned configs for 4 GB / 16 GB hosts; data guard installed |
| `redis` | Rate-limit counters, job queue for the transcription worker | `noeviction`, append-only |
| `archive-worker` | Pulls verified sessions from transit storage, joins WAVs, writes the archive tree, makes the encrypted cloud copy, performs erasures, serves decrypted downloads | The only holder of the copy-encryption key |
| `worker` | Optional transcription / NER pipeline (Azure OpenAI) | Isolated from the integrity layer |
| `backup` | Nightly encrypted dumps, WAL shipping, off-site upload with size check, labelled pre-deploy backups, scheduled restore drill | |
| `monitor` | Disk, DB, backups, archive queue, silent recorders, certificate expiry, CMED silence; alerts before a doctor would notice | |
| `dbviewer` | Adminer behind portal sign-in, per-person read-only DB logins | |

### 5.2 Storage tiers

| Tier | Holds | Lifetime | Who can do what |
|---|---|---|---|
| **Doctor PC spool** | Sealed segments, AES-256-GCM, key wrapped by DPAPI | Until a purge receipt arrives (or 7 days for unconfirmed) | Recorder only |
| **Transit bucket** (S3-compatible, Cloudflare R2) | Segments on their way to the archive | Until archived and copied | PCs: presigned PUT only. API: presign/delete. Cannot reach the copy bucket |
| **Archive** (server disk) | One WAV per consultation part, sorted tree, SHA-256 | `AIMS_ARCHIVE_LOCAL_RETENTION_DAYS` after a proven copy | Archive worker |
| **Copy bucket** (cold storage, second provider) | Encrypted merged WAV + clinical JSON | Long-term research copy | Write/read only, never delete |
| **Off-site backups** | Encrypted DB dumps and WAL | `AIMS_OFFSITE_KEEP_DAYS`, bucket lock | Backup container |

---

## 6. Component architecture

### 6.1 Recorder (`AIMScribe.exe`)

```mermaid
flowchart TB
    subgraph Interfaces["Interfaces"]
        WS["api/websocket_server<br/>WS /ws (API 1)"]
        HT["api/trigger_server<br/>HTTP :5050 (API 1, health, status)"]
        PR["api/protocol<br/>Channel A contract"]
        UI["ui/overlay + overlay_model<br/>Stop / Pause card"]
        TRAY["main<br/>tray app, lifecycle"]
    end

    subgraph Control["Control"]
        SC["core/session_controller<br/>state machine"]
        SK["core/start_keeper<br/>retry a wanted start"]
        TO["core/trusted_origins<br/>signed origin list"]
        PS["core/peer_session<br/>Windows-session check"]
    end

    subgraph Capture["Capture"]
        MICM["core/microphone<br/>Jabra default 100%, fallback"]
        RECM["core/recorder<br/>PortAudio capture thread"]
        SPL["core/simple_splitter<br/>silence-bounded segments"]
    end

    subgraph Durability["Durability & integrity"]
        SP["core/spool<br/>encrypted crash-safe spool"]
        CR["core/crypto<br/>hash chain, Ed25519, AES-GCM, DPAPI"]
        TPM["core/tpm_key<br/>TPM-held identity key"]
        EN["core/enrollment<br/>register / enroll"]
    end

    subgraph Uplink["Uplink"]
        UP["core/uploader<br/>ordered upload, heartbeat, receipts"]
        HL["core/health_log<br/>offline event log"]
        TLS["core/tls_check<br/>interception detection"]
    end

    subgraph Platform["Platform guards"]
        PW["core/power<br/>keep awake"]
        SG["core/shutdown_guard<br/>close on shutdown"]
        SE["core/stop_event<br/>graceful upgrade stop"]
        DK["core/disk<br/>free space, encryption"]
    end

    WS --> PR --> SC
    HT --> PR
    WS --> TO
    HT --> TO
    WS --> PS
    SC --> SK
    SC --> RECM
    MICM --> RECM
    RECM --> SPL --> SP
    SC --> CR
    SP --> CR
    CR --> TPM
    SC --> UP
    UP --> SP
    UP --> HL
    UP --> TLS
    UP --> EN
    UP -- "heartbeat reply" --> TO
    SC --> UI
    TRAY --> SC
    SC --> PW
    SG --> SC
    SE --> SC
    SP --> DK
```

Key behaviours:

* **Capture thread isolation.** The PortAudio thread only reads and enqueues; hashing,
  loudness and file writes happen elsewhere, so the capture never glitches under load.
* **Microphone policy.** On start the Jabra speakerphone is made the Windows default
  (IPolicyConfig), unmuted, set to 100 %. Unplugged → recording continues on the PC
  microphone; replugged → it switches back mid-recording (checked every 5 s).
* **Recording end rules** (only these end a recording): a new `visit_id` arrives in API 1
  (previous one closes immediately, reason `superseded_by_new_patient`); the doctor
  presses Stop; two minutes after the server tells the PC that API 3 arrived (extended
  while speech continues); Windows shutdown / sign-out (closed cleanly by the guard).
* **Offline first start.** A brand-new PC with no internet at its first patient keeps the
  start and retries until it can register; a genuine refusal stops immediately.

### 6.2 Server API (`backend/src`)

```mermaid
flowchart LR
    subgraph Edge["Cross-cutting"]
        SEC["security<br/>client IP, request id,<br/>rate limits, lockouts"]
        CFG["config"]
        SCH["schema_check<br/>refuse unmigrated DB"]
    end

    subgraph Device["Device protocol — /api/v2"]
        A2["api_v2<br/>register, enroll, session, segment,<br/>heartbeat, receipts, admin, sweep"]
        GR["grants<br/>mint signed grants"]
        CF["confirmation<br/>API 2 ⨝ grant matching"]
        LB["labelling<br/>consultation parts"]
        IN["integrity<br/>chain verify, Ed25519 receipts,<br/>signed trusted origins"]
    end

    subgraph ChannelB["Channel B"]
        V4["clinical_v4<br/>/api/v4/clinical"]
        FH["fhir<br/>/fhir/r4 ($process-message)"]
        CL["clinical<br/>legacy channel B"]
        CM["clinical_model<br/>pure mapping rules"]
        CST["clinical_store<br/>intake → tables"]
        AUTH["cmed_auth<br/>SMART Backend Services,<br/>clients, keys, tokens"]
    end

    subgraph Portal["Portal"]
        PO["portal<br/>Google OIDC + PKCE, people"]
        DA["dashboard<br/>/api/v2/dashboard"]
        DL["downloads<br/>single recording"]
        BU["bulk<br/>numbered batches, zip"]
    end

    subgraph Data["Data access"]
        DB2["db_v2<br/>aims_recordings"]
        CDB["clinical_store<br/>aims_clinical"]
    end

    A2 --> GR & CF & LB & IN & DB2
    V4 --> AUTH & CM & CST
    FH --> AUTH & CM & CST
    CL --> AUTH & CST
    V4 --> CF
    CST --> CDB
    PO --> DB2
    DA --> DB2 & CDB
    DL --> DB2
    BU --> DB2
    SEC -.-> A2 & V4 & FH & PO
    SCH -.-> DB2 & CDB
```

| Module | Responsibility |
|---|---|
| `main_fastapi` | App assembly, storage clients, router mounting, lifespan |
| `api_v2` | Device protocol, archive handshake, admin endpoints, maintenance sweep (device trust, alerts, expiries) |
| `grants` | Validates API 1 fields against the register; mints short-lived single-use grants |
| `confirmation` | Pairs a recording's grant with CMED's API 2 on the same five fields → `confirmed` |
| `labelling` | Splits a session into consultation parts when the doctor switches profiles |
| `integrity` | Mirror of the recorder's `core/crypto`: canonical JSON, chain verification, Ed25519 signing with domain separation |
| `cmed_auth` | Per-hospital CMED clients, JWK registration (`kid` = RFC 7638 thumbprint), assertion replay protection (`jti`), 5-minute tokens, CIDR pinning |
| `clinical_v4` | API 2/API 3 in plain JSON keyed by `visit_id`; never rejects for missing optional data (nulls kept) |
| `fhir` | The same messages as HL7 FHIR R4 message Bundles; SMART discovery |
| `clinical_model` / `clinical_store` | Store-then-load: raw message kept in `intake_records`/`visit_messages`, then mapped into relational tables; failures quarantined, never lost |
| `portal`, `dashboard`, `downloads`, `bulk` | Staff/researcher access: sign-in, operational dashboard, decrypt-on-demand downloads, rate-limited batch export |

### 6.3 Archive worker (`backend/archive_worker`)

| Module | Responsibility |
|---|---|
| `worker` | Poll `/api/v2/archive/pending`, download verified segments, archive, report `/archive/complete` so the server can issue purge receipts |
| `archive` | Path-safe archive tree, WAV joining, verification |
| `catalogue` | Local record of what the archive holds (survives a DB outage) |
| `cloudcopy` | Encrypt and upload the merged copy; verify; only then allow local deletion |
| `serve` | Decrypt one copy for a portal download (key never leaves this container) |
| `restore` | Rebuild the archive from the copy bucket (tested by the restore drill) |

---

## 7. How AIMScribe.exe communicates with the server

### 7.1 Transport and identity

* **Transport:** HTTPS to the public host, JSON bodies; segments go **directly** to the
  transit bucket by presigned `PUT`, never through the API.
* **TLS interception check:** the recorder detects antivirus/proxy re-signing and reports
  it rather than silently trusting an inspected channel.
* **Device identity:** an identity key generated **inside the TPM** (fallback: software
  Ed25519 wrapped with machine-scope DPAPI). The server stores only the public key.
* **Device token:** returned once at registration, stored hashed (`devices.token_sha256`).
* **Request signatures and nonces:** requests are signed by the device key; nonces are
  remembered (`device_nonces`) to reject replays.

### 7.2 Registration (first patient on a new PC)

```mermaid
sequenceDiagram
    autonumber
    participant EHR as CMED page
    participant R as AIMScribe.exe
    participant S as AIMS LAB API
    participant DB as aims_recordings

    EHR->>R: API 1 {hospital_id, patient_id, doctor_id, start_time, date, visit_id}
    R->>R: Origin in trusted list? same Windows session?
    alt PC not registered yet
        R->>S: POST /api/v2/device/register {TPM public key, machine, versions, hospital}
        S->>DB: devices (approval = pending, registered_via = self)
        S-->>R: device_id + device token
    end
    Note over R,S: No internet → start is kept and retried,<br/>recording begins as soon as registration succeeds
```

A self-registered PC is **pending** until CMED's server confirms two of its recordings
(then **approved**). It is **blocked** only on evidence (§12.3).

### 7.3 One consultation, end to end

```mermaid
sequenceDiagram
    autonumber
    participant EHR as CMED page
    participant R as AIMScribe.exe
    participant S as AIMS LAB API
    participant T as Transit bucket
    participant C as CMED server
    participant W as Archive worker

    EHR->>R: API 1 (visit V1)
    R->>S: POST /grant/mint (five fields + visit_id)
    S-->>R: signed single-use grant
    R->>S: POST /session/open (grant, chain entry #0)
    S-->>R: session accepted
    R-->>EHR: started {session_id, visit_id}
    C->>S: API 2 patient-information (V1)
    S->>S: confirmation: grant ⨝ API 2 → confirmed

    loop every sealed segment
        R->>S: POST /segment/authorize (sha256, bytes)
        S-->>R: presigned PUT URL
        R->>T: PUT segment (encrypted in transit, TLS)
        R->>S: POST /segment/commit (signed chain entry)
    end

    C->>S: API 3 prescription (V1)
    R->>S: POST /heartbeat (state, mic, spool, trusted_origins_version)
    S-->>R: prescription_received, newer trusted origins (signed)
    Note over R: 2 min after API 3 (or next API 1 / Stop)
    R->>S: POST /session/close (final chain entry)
    S->>S: verify chain (hashes + device signatures)
    W->>S: GET /archive/pending
    W->>T: download segments
    W->>W: join WAV, write archive, encrypted cloud copy
    W->>S: POST /archive/complete
    R->>S: GET /session/{id}/receipts
    S-->>R: Ed25519-signed purge receipts
    R->>R: delete local spool for that session
```

### 7.4 Endpoints used by the recorder (prefix `/api/v2`)

| Endpoint | Purpose |
|---|---|
| `POST /device/register` | Self-registration with the TPM public key (first start) |
| `POST /device/enroll` | Registration with a one-time enrolment token (managed installs) |
| `POST /grant/mint` | Exchange API 1 fields for a signed, single-use grant |
| `POST /session/open` · `/close` | Open/close a session with signed chain entries |
| `POST /session/pause` · `/resume` | Doctor pause/resume |
| `POST /session/marker` | A patient was opened / a prescription was built inside the session |
| `POST /session/reassign` | Move a stretch of audio to the right patient |
| `POST /session/refuse` | Patient declined: signed refusal, erased everywhere |
| `POST /segment/authorize` · `/commit` | Presign a segment upload; commit it to the chain |
| `POST /heartbeat` | Liveness, spool state, microphone, clock offset; reply carries prescription notices and the signed trusted-origin bundle |
| `POST /device/health` | Events logged while offline |
| `GET /session/{id}/receipts` | Signed purge receipts |
| `GET /session/{id}/confirmation` | Whether CMED has confirmed the recording |

### 7.5 Trusted-origin distribution

```mermaid
sequenceDiagram
    participant A as Admin (dashboard)
    participant S as API
    participant DB as cmed_trusted_origins
    participant R as Every recorder

    A->>S: POST /api/v2/admin/trusted-origins {origin}
    S->>DB: insert (audited)
    R->>S: heartbeat {trusted_origins_version: v}
    S-->>R: {payload: {origins, version > v}, signature}
    R->>R: verify Ed25519 over domain "aimscribe.trusted-origins.v1"<br/>reject older versions, persist atomically
    Note over R: Works offline afterwards from the persisted copy
```

The signing domain differs from the purge-receipt domain (`aimscribe.receipt.v2`), so a
signature for one can never be replayed as the other.

---

## 8. CMED integration (Channel A and Channel B)

### 8.1 Channel B authentication (SMART Backend Services)

```mermaid
sequenceDiagram
    participant C as CMED server
    participant S as AIMS LAB API
    participant DB as cmed_clients / keys / tokens

    Note over C: Holds the hospital's private key (ES384)
    C->>S: POST /api/v2/oauth/token<br/>client_assertion = JWT{iss=sub=client_id, aud, jti, exp ≤ 5 min}, kid
    S->>DB: client active? key (kid) active? jti unseen? source IP allowed?
    S-->>C: access_token (5 min, scoped to the client's hospitals)
    C->>S: POST /api/v4/clinical/patient-information (Bearer)
    S-->>C: 202 {visit_id, received, warnings}
    C->>S: POST /api/v4/clinical/prescription (Bearer)
    S-->>C: 202 {visit_id, received}
    C->>S: GET /api/v4/clinical/visit/{visit_id}/status
    S-->>C: {audio, patient_information, prescription, complete}
```

* **One client per hospital**: a leaked key affects one hospital only; keys rotate by
  adding a second `kid`, then revoking the first; clients expire yearly with an alert
  30 days before.
* **Onboarding** requires no shared secret: CMED generates the key pair, sends the public
  JWK and its `kid`; AIMS LAB registers it and confirms the `kid` by phone.

### 8.2 Message handling guarantees

| Guarantee | Mechanism |
|---|---|
| Nothing CMED sends is lost | Raw body stored first (`visit_messages`, `intake_records`); malformed input stored in `*_quarantine` |
| Idempotent | Body hashes (`body_sha256`, `content_sha256`); FHIR bundle ids (`fhir_messages`) |
| Optional data never rejects | Missing/empty fields become `NULL`, reported as `warnings` |
| Versioned prescriptions | `prescription_version`; latest wins, history kept |
| Silence detection | `hospitals.cmed_last_message_at`; alert `cmed_silent` when a clinic records but CMED is silent for 2 h |

---

## 9. Data flow diagrams

### 9.1 Level 0 — context

```mermaid
flowchart LR
    D(["Doctor"]) -- "voice" --> P0
    CE(["CMED EHR page"]) -- "API 1: visit fields" --> P0
    CS(["CMED server"]) -- "API 2: patient info<br/>API 3: prescription" --> P0
    P0(("AIMScribe<br/>system"))
    P0 -- "start/stop replies" --> CE
    P0 -- "202 + visit status" --> CS
    P0 -- "audio + clinical datasets" --> RS(["Researchers"])
    P0 -- "alerts, dashboard" --> OP(["AIMS LAB operators"])
```

### 9.2 Level 1 — processes, stores and trust boundaries

```mermaid
flowchart TB
    subgraph TB1["Trust boundary: consultation PC"]
        P1["1.0 Capture &<br/>segment audio"]
        P2["2.0 Seal, chain,<br/>encrypt to spool"]
        DS1[("D1 Local spool")]
    end

    subgraph TB2["Trust boundary: AIMS LAB server"]
        P3["3.0 Authorise &<br/>register session"]
        P4["4.0 Ingest clinical<br/>messages"]
        P5["5.0 Confirm &<br/>label recording"]
        P6["6.0 Archive, copy,<br/>issue receipts"]
        P7["7.0 Serve portal &<br/>exports"]
        DS2[("D2 aims_recordings")]
        DS3[("D3 aims_clinical")]
        DS4[("D4 Archive disk")]
    end

    subgraph TB3["Trust boundary: cloud storage"]
        DS5[("D5 Transit bucket")]
        DS6[("D6 Copy bucket")]
    end

    EHR(["CMED page"]) -- "API 1" --> P3
    MIC(["Microphone"]) -- "PCM" --> P1
    P1 -- "WAV segments" --> P2
    P2 --> DS1
    DS1 -- "sealed segments" --> DS5
    P2 -- "chain entries" --> P3
    P3 <--> DS2
    CS(["CMED server"]) -- "API 2 / API 3" --> P4
    P4 --> DS3
    P4 -- "visit notice" --> P5
    P5 <--> DS2
    DS5 -- "verified segments" --> P6
    P6 --> DS4
    P6 -- "encrypted copy" --> DS6
    P6 -- "purge receipts" --> DS1
    P6 <--> DS2
    DS2 --> P7
    DS3 --> P7
    DS4 --> P7
    DS6 -- "decrypt on demand" --> P7
    P7 --> RS(["Researchers"])
```

| Flow | Data | Protection |
|---|---|---|
| Microphone → spool | PCM audio | AES-256-GCM at rest, DPAPI-wrapped key |
| PC → transit bucket | Sealed segments | TLS, presigned single-object PUT, SHA-256 committed to the chain |
| PC → API | Chain entries, heartbeats | TLS, device token, device signatures, nonces |
| CMED → API | Patient info, prescriptions (PHI) | TLS, per-hospital OAuth2 token, IP pinning (optional) |
| Archive → copy bucket | Merged WAV, clinical JSON | Encrypted with a key held only by the archive worker; write-only credentials |
| Portal → researcher | Recordings, datasets | Google sign-in, allow-list, per-person DB login, rate limits, audit of every download |

---

## 10. Database (DBMS) architecture

### 10.1 Two databases, one cluster

| Database | Holds | Why separate |
|---|---|---|
| **`aims_recordings`** | Operational and integrity data: hospitals, devices, sessions, segments, chains, grants, receipts, CMED auth, portal, audit | High write rate, strict integrity rules, no clinical content beyond identifiers |
| **`aims_clinical`** | Everything CMED sends: raw messages, patients, visits, prescriptions, diagnoses, vitals | PHI isolated: different grants, different backup handling, separate researcher logins |

The two are joined **logically** (not by foreign key) through `visit_id`, `session_id`,
`file_stem` and `(hospital_id, patient_id)`.

### 10.2 Roles and privileges

| Role | Login | Rights |
|---|---|---|
| `aims_recordings` (owner) | No (migrations use `SET ROLE`) | Owns schema |
| `aims_api` | Yes (API, worker) | Rows only: no DDL, no TRUNCATE, no trigger changes; audit log is insert/select only |
| `aims_monitor` | Yes (monitor) | Read the views it needs |
| `aims_researchers` | No (group) | Read-only research views |
| per-person researcher logins | Yes | Members of `aims_researchers`, created from the portal |
| `aims_role_keeper` | No | Holds role memberships |

### 10.3 Data guard (enforced by PostgreSQL)

Installed by `migrate.sh` in its own superuser-owned schema, on both databases, on every
update:

1. No table, column, schema or materialized view may be dropped.
2. No table may be truncated.
3. No single statement may delete more than 5,000 rows.

A migration that genuinely must destroy data has to be listed in `DESTRUCTIVE_APPROVED`
(enforced by the deployment test gate) and set `aims.allow_destructive` for itself only.

### 10.4 Migrations and schema verification

* Numbered, forward-only SQL scripts: `backend/scripts/0NN_*.sql`, plus
  `backend/scripts/clinical/` for `aims_clinical`.
* Applied by `deploy/uiu/postgres/migrate.sh` **only after** a labelled backup of both
  databases (no backup → no deploy).
* Each applied script is recorded with its checksum; a changed script is reported.
* `schema_check.py` refuses to serve with a database missing a required migration
  (`AIMS_SCHEMA_STRICT`).

### 10.5 Append-only and tamper evidence

* `audit_log` and `chain_entries` are hash-linked (`prev_hash` → `entry_hash`); the API
  role cannot update or delete them.
* `purge_receipts` are Ed25519 signatures (64 bytes, checked by constraint).
* Segments carry SHA-256 (32 bytes, checked by constraint).

### 10.6 Backup and recovery

| Mechanism | Frequency | Target |
|---|---|---|
| Encrypted logical dump of both databases | Nightly + before every deploy that changes the schema | Local + off-site bucket (size-checked) |
| WAL shipping | Every `AIMS_WAL_SHIP_SECONDS` | Off-site |
| Restore drill | Every `AIMS_DRILL_EVERY_DAYS` | Scratch database, row counts compared; monitor alerts if overdue |
| Audio restore | On demand | `archive_worker/restore.py` from the copy bucket |

---

## 11. Entity–relationship diagrams

**Notation:** solid lines are enforced foreign keys; dashed lines are logical references
(same identifier, not FK-enforced — for example because rows outlive their parent or live
in the other database). Only the key and most significant columns are shown.

### 11.1 `aims_recordings` — identity, devices and CMED access

```mermaid
erDiagram
    hospitals ||--o{ devices : "hosts"
    hospitals ||--o{ doctors : "employs"
    hospitals ||--o{ enrollment_tokens : "issues"
    hospitals ||--o{ api_keys : "legacy keys"
    devices ||--o{ device_nonces : "replay guard"
    devices ||--o{ device_health_log : "reports"
    devices |o--o{ enrollment_tokens : "used by"
    devices ||--o{ grant_authorisations : "receives"
    devices ||--o{ integrity_alerts : "raises"
    devices ||--o{ audit_log : "acts in"
    cmed_clients ||--o{ cmed_client_keys : "has keys"
    cmed_clients ||--o{ cmed_access_tokens : "is issued"
    cmed_clients ||..o{ cmed_assertion_jti : "replay guard"
    cmed_clients ||..o{ fhir_messages : "sends"
    hospitals ||..o{ cmed_clients : "cmed_hospital_ids"

    hospitals {
        varchar hospital_id PK
        text name
        text timezone
        varchar cmed_hospital_id "CMED's code"
        timestamptz cmed_last_message_at
        timestamptz cmed_silence_alerted_at
        bool active
    }
    devices {
        uuid device_id PK
        varchar hospital_id FK
        bytea tpm_pubkey "32 B Ed25519 or 65 B P-256"
        bytea token_sha256
        varchar approval "pending|approved|rejected"
        varchar registered_via "token|self"
        timestamptz blocked_at
        timestamptz trust_judged_from
        timestamptz revoked_at
        text microphone
        bool microphone_is_clinic
        text app_version
        timestamptz last_heartbeat_at
    }
    doctors {
        text doctor_id PK
        text hospital_id PK
        text full_name
        bool active
    }
    enrollment_tokens {
        bytea token_sha256 PK
        varchar hospital_id FK
        uuid device_id FK
        timestamptz expires_at
        timestamptz used_at
    }
    api_keys {
        bytea key_sha256 PK
        varchar hospital_id FK
        text scope
        timestamptz revoked_at
    }
    device_nonces {
        uuid device_id PK
        varchar nonce PK
        timestamptz seen_at
    }
    device_health_log {
        uuid device_id PK
        bigint seq PK
        varchar kind
        jsonb data
    }
    grant_authorisations {
        text jti PK
        uuid device_id FK
        varchar hospital_id
        varchar patient_id
        varchar doctor_id
        varchar visit_id
        varchar session_id
        timestamptz expires_at
    }
    integrity_alerts {
        bigint id PK
        uuid device_id FK
        varchar session_id
        text alert_type
        text severity
        timestamptz resolved_at
    }
    audit_log {
        bigint id PK
        uuid device_id FK
        text event_type
        jsonb detail
        bytea prev_hash
        bytea entry_hash
    }
    cmed_clients {
        varchar client_id PK
        varchar_array cmed_hospital_ids
        text_array allowed_cidrs
        timestamptz expires_at
        timestamptz revoked_at
        timestamptz last_used_at
    }
    cmed_client_keys {
        varchar client_id PK
        varchar kid PK "RFC 7638 thumbprint"
        varchar alg "ES384|ES256|RS384"
        jsonb public_jwk
        timestamptz revoked_at
    }
    cmed_access_tokens {
        bytea token_sha256 PK
        varchar client_id FK
        varchar kid
        timestamptz expires_at
    }
    cmed_assertion_jti {
        varchar client_id PK
        varchar jti PK
        timestamptz expires_at
    }
    fhir_messages {
        varchar client_id PK
        varchar bundle_id PK
        bytea body_sha256
    }
    cmed_trusted_origins {
        text origin PK
        text added_by
        timestamptz added_at
        timestamptz removed_at
    }
```

### 11.2 `aims_recordings` — sessions, integrity, archive and erasure

```mermaid
erDiagram
    devices ||--o{ sessions : "records"
    sessions ||..o{ chain_entries : "chain"
    chain_entries ||--o| segments : "commits"
    sessions ||..o{ segments : "audio"
    sessions ||..o{ consultation_parts : "split into"
    sessions ||..o{ session_clicks : "markers"
    sessions ||..o{ purge_receipts : "receipts"
    sessions ||..o{ cloud_copies : "copied as"
    sessions ||..o| erase_tasks : "erased by"
    sessions ||..o| session_refusals : "refused"
    sessions ||--o{ clips : "legacy clips"
    grant_authorisations ||..o| sessions : "authorises"
    confirmation_notices ||..o{ grant_authorisations : "confirms"
    sessions ||..o| sessions : "continues"

    sessions {
        varchar session_id PK "ULID"
        uuid device_id FK
        varchar hospital_id
        varchar patient_id
        varchar doctor_id
        varchar visit_id
        varchar status "active|closed|archived|quarantined|refused|expired"
        varchar confirmation "pending|confirmed|unconfirmed|refused|expired|legacy"
        timestamptz opened_at
        timestamptz closed_at
        text close_reason
        bytea chain_head_hash
        timestamptz chain_verified_at
        text file_stem
        timestamptz prescription_received_at
        text_array microphones
        timestamptz erase_after
        bool legal_hold
        varchar continues_session_id
    }
    chain_entries {
        varchar session_id PK
        int entry_no PK
        varchar entry_type
        jsonb payload
        bytea payload_sha256
        bytea prev_hash
        bytea entry_hash
        bytea device_signature
    }
    segments {
        varchar session_id PK
        int seq_no PK
        int entry_no FK
        text object_key
        bigint bytes
        bytea sha256 "32 bytes"
        varchar state "committed|archived|purged|quarantined"
        real rms_mean
        bool is_final
    }
    consultation_parts {
        varchar session_id PK
        int part_no PK
        float start_seconds
        float end_seconds
        varchar patient_id
        varchar doctor_id
        text file_stem
        text archive_relpath
        bytea archive_sha256
    }
    session_clicks {
        varchar session_id PK
        int entry_no PK
        varchar event "patient_opened|prescription_built"
        float audio_seconds
        varchar patient_id
    }
    purge_receipts {
        varchar session_id
        varchar scope "segment|session"
        int seq_no
        bytea signature "Ed25519, 64 B"
        timestamptz issued_at
    }
    cloud_copies {
        bigint id PK
        varchar session_id
        varchar kind "audio|json"
        text object_key
        bytea sha256
        timestamptz verified_at
    }
    erase_tasks {
        varchar session_id PK
        jsonb files
        jsonb cloud_keys
        timestamptz completed_at
    }
    erase_releases {
        bigint id PK
        varchar hospital_id
        text released_by
        timestamptz until
    }
    session_refusals {
        varchar session_id PK
        uuid device_id FK
        bytea visit_sha256
        bool signed
    }
    confirmation_notices {
        bigint id PK
        varchar cmed_hospital_id
        varchar patient_id
        varchar doctor_id
        date visit_date
        text claimed_by_jti
    }
    grant_authorisations {
        text jti PK
        varchar session_id
        bigint notice_id
        varchar visit_id
    }
    clips {
        int id PK
        varchar session_id FK
        int clip_number
    }
    devices {
        uuid device_id PK
    }
```

### 11.3 `aims_recordings` — portal

```mermaid
erDiagram
    portal_people ||--o{ portal_sessions : "signs in"
    portal_people ||--o{ portal_downloads : "downloads"
    portal_people ||--o{ portal_batches : "requests"
    portal_batches ||--o{ portal_batch_files : "contains"

    portal_people {
        text email PK "lower-case"
        varchar role "admin|researcher"
        text db_user
        timestamptz removed_at
    }
    portal_sessions {
        bytea token_sha256 PK
        text email FK
        timestamptz expires_at
    }
    portal_logins {
        text state PK "OIDC state"
        text nonce
        text verifier "PKCE"
    }
    portal_downloads {
        bigint id PK
        text email FK
        varchar session_id
        int part_no
        varchar outcome
        bigint bytes
    }
    portal_batches {
        bigint id PK
        text email FK
        varchar hospital_id
        date date_from
        date date_to
        int batch_no
        varchar outcome
    }
    portal_batch_files {
        bigint batch_id PK
        int position PK
        varchar session_id
        int part_no
    }
```

### 11.4 `aims_clinical`

Two models coexist: the **v4 visit model** (`patient`, `patient_visit`, keyed by CMED's
`visit_id`) used by interface v4, and the **encounter model** (`patients`, `encounters`,
`prescriptions` and child tables) used by the FHIR and legacy channel B paths.

```mermaid
erDiagram
    patient ||--o{ patient_visit : "visits"
    visit_messages }o..|| patient_visit : "raw API 2/3 (by visit_id)"

    patients ||--o{ encounters : "has"
    intake_records |o--o{ encounters : "patient information"
    encounters ||--o| encounter_demographics : "snapshot"
    encounters ||--o| paramedic_observations : "vitals"
    encounters ||--o| male_details : "sex-specific"
    encounters ||--o| female_details : "sex-specific"
    encounters ||--o{ prescriptions : "versions"
    intake_records |o--o{ prescriptions : "source"
    prescriptions ||--o{ prescription_items : "drugs"
    prescriptions ||--o{ diagnoses : "diagnoses"
    prescriptions ||--o{ investigations : "orders"
    prescriptions ||--o{ consultation_findings : "findings"

    patient {
        varchar hospital_id PK
        varchar hospital_patient_id PK
        varchar sex
        date date_of_birth
        varchar blood_group
    }
    patient_visit {
        bigint id PK
        varchar visit_id "CMED visit"
        varchar hospital_id FK
        varchar hospital_patient_id FK
        varchar aims_id "recording session"
        varchar practitioner_id
        jsonb vitals
        jsonb chief_complaints
        jsonb observations
        jsonb diagnosis "text + ICD-10 when given"
        jsonb lab_orders
        jsonb radiology_orders
        jsonb drug_orders
        jsonb advice
        date follow_up
        int prescription_version
    }
    visit_messages {
        bigint id PK
        varchar kind "patient-information|prescription"
        varchar hospital_id
        varchar visit_id
        int version
        jsonb body
        bytea content_sha256
        jsonb warnings
    }
    visit_quarantine {
        bigint id PK
        varchar kind
        text raw
        jsonb problems
    }
    patients {
        varchar patient_id PK
        varchar sex
        text full_name
        date date_of_birth
    }
    encounters {
        uuid encounter_id PK
        varchar patient_id FK
        varchar hospital_id
        varchar session_id
        text file_stem
        bigint patient_information_id FK
    }
    intake_records {
        bigint id PK
        varchar kind
        varchar hospital_id
        varchar patient_id
        jsonb body
        bytea body_sha256
        text load_error
    }
    intake_quarantine {
        bigint id PK
        varchar kind
        text raw
        jsonb problems
    }
    encounter_demographics {
        uuid encounter_id PK
        text full_name
        smallint age_years
    }
    paramedic_observations {
        uuid encounter_id PK
        numeric weight_kg
        text blood_pressure
        smallint pulse_bpm
        smallint spo2_percent
    }
    male_details {
        uuid encounter_id PK
        jsonb details
    }
    female_details {
        uuid encounter_id PK
        jsonb details
    }
    prescriptions {
        bigint prescription_id PK
        uuid encounter_id FK
        int version
        bigint intake_id FK
        jsonb raw_json
    }
    prescription_items {
        bigint prescription_id PK
        int line_no PK
        text drug
        text dose
        text frequency
        text duration
    }
    diagnoses {
        bigint prescription_id PK
        int line_no PK
        text text
        text code
    }
    investigations {
        bigint prescription_id PK
        int line_no PK
        text name
    }
    consultation_findings {
        bigint prescription_id PK
        int line_no PK
        varchar section
        text item
        text value
    }
    clinical_access_log {
        bigint id PK
        text actor
        text action
        varchar patient_id
    }
```

### 11.5 Cross-database join keys

```mermaid
flowchart LR
    S["aims_recordings.sessions<br/>session_id · visit_id · file_stem"]
    CP["aims_recordings.consultation_parts<br/>file_stem"]
    PV["aims_clinical.patient_visit<br/>visit_id · aims_id"]
    EN["aims_clinical.encounters<br/>session_id · file_stem"]
    S -- "visit_id" --> PV
    S -- "session_id = aims_id" --> PV
    S -- "session_id" --> EN
    CP -- "file_stem" --> EN
```

### 11.6 Table catalogue

<details>
<summary><b>aims_recordings</b> (34 tables, 11 views)</summary>

| Table | Purpose |
|---|---|
| `hospitals` | Clinics, their CMED code, CMED liveness |
| `doctors` | Doctors per clinic |
| `devices` | Recorder PCs: identity, approval/blocking, health, microphone |
| `enrollment_tokens` | One-time tokens for managed enrolment |
| `device_nonces` | Request replay protection |
| `device_health_log` | Events reported by PCs (including offline periods) |
| `grant_authorisations` | Minted recording grants (single use) |
| `confirmation_notices` | API 2 notices waiting to meet a grant |
| `sessions` | Recordings |
| `chain_entries` | Signed, hash-linked events per session |
| `segments` | Audio pieces and their lifecycle |
| `session_clicks` | Patient-opened / prescription-built markers |
| `consultation_parts` | Per-patient parts of a session and their archive files |
| `session_refusals` | Patient refusals |
| `purge_receipts` | Signed permissions for PCs to delete |
| `cloud_copies` | Proven copies in the copy bucket |
| `erase_tasks`, `erase_releases` | Erasure work and the circuit-breaker releases |
| `integrity_alerts` | Everything the system thinks is wrong |
| `audit_log` | Hash-linked, append-only audit trail |
| `cmed_clients`, `cmed_client_keys`, `cmed_access_tokens`, `cmed_assertion_jti` | CMED server identities |
| `cmed_trusted_origins` | EHR addresses recorders obey |
| `fhir_messages` | FHIR bundle de-duplication |
| `api_keys` | Legacy static keys (switched off in production) |
| `portal_people`, `portal_sessions`, `portal_logins` | Portal accounts and sign-in |
| `portal_downloads`, `portal_batches`, `portal_batch_files` | Audited exports |
| `clips` | v1 legacy |

Views: `v_devices`, `v_device_requests`, `v_doctors`, `v_doctor_register`, `v_doctor_activity`,
`v_audio_files`, `v_audio_daily`, `v_audio_problems`, `v_abnormal_closes`,
`v_session_pauses`, `patient_recordings`.
</details>

<details>
<summary><b>aims_clinical</b> (18 tables, 4 views)</summary>

| Table | Purpose |
|---|---|
| `visit_messages` / `visit_quarantine` | v4 raw API 2/API 3, and refused input |
| `patient`, `patient_visit` | v4 relational model keyed by `visit_id` |
| `intake_records` / `intake_quarantine` | Raw FHIR/legacy messages, and refused input |
| `patients`, `encounters`, `encounter_demographics` | Encounter model |
| `paramedic_observations`, `male_details`, `female_details` | Vitals and sex-specific details |
| `prescriptions`, `prescription_items`, `diagnoses`, `investigations`, `consultation_findings` | Prescription content, versioned |
| `clinical_access_log` | Who read which patient's record |

Views: `current_encounter`, `current_prescription`, `previous_prescription`,
`latest_prescriptions`.
</details>

---

## 12. Lifecycles and state machines

### 12.1 Recording (session)

```mermaid
stateDiagram-v2
    [*] --> active : API 1 + grant
    state active {
        [*] --> recording
        recording --> paused : doctor Pause
        paused --> recording : Resume
    }
    active --> closed : next visit_id · Stop · 2 min after API 3 · shutdown
    active --> quarantined : chain fails verification
    closed --> quarantined : chain fails verification
    closed --> archived : chain verified, WAV archived, copy proven
    active --> refused : patient declined
    closed --> expired : still unconfirmed at erase_after
    archived --> [*] : purge receipts issued, PC deletes spool
    refused --> [*] : erased everywhere
    expired --> [*] : erased everywhere
    quarantined --> [*] : reviewed by an operator
```

### 12.2 Confirmation (independent of the above)

```mermaid
stateDiagram-v2
    [*] --> pending
    pending --> confirmed : API 2 matches the grant
    pending --> unconfirmed : no match yet when the recording closes
    unconfirmed --> confirmed : API 2 arrives late (sweep backstop)
    unconfirmed --> expired : erase_after passed (AIMS_UNCONFIRMED_ERASE_DAYS)
    pending --> refused : patient declined
    confirmed --> [*] : kept
    expired --> [*] : erased
    refused --> [*] : erased
```

### 12.3 Device trust

```mermaid
stateDiagram-v2
    [*] --> pending : self-registration
    [*] --> approved : enrolment token
    pending --> approved : CMED confirms 2 recordings
    pending --> blocked : CMED active for the clinic but confirms none of ≥3 recordings / 24 h
    blocked --> pending : admin Unblock (judged afresh)
    pending --> rejected : never recorded for 30 days · admin Reject
    approved --> revoked : admin Revoke
    pending --> revoked : admin Revoke
```

Blocking is **evidence-based**: it requires CMED to have been heard for that clinic after
the PC's recordings (with a grace period). An outage — no CMED messages at all — never
blocks a PC.

---

## 13. Security architecture

### 13.1 Key inventory

| Key | Algorithm | Where it lives | Purpose |
|---|---|---|---|
| Device identity | TPM-held key (fallback Ed25519 + DPAPI) | The PC's TPM | Signs chain entries and requests |
| Spool key | AES-256-GCM | PC, wrapped by DPAPI | Encrypts audio at rest |
| Grant key | Asymmetric signing | API (`AIMS_GRANT_PRIVATE_KEY`) | Signs recording grants |
| Receipt key | Ed25519 | API (`AIMS_RECEIPT_PRIVATE_KEY`) | Purge receipts (`aimscribe.receipt.v2`) and trusted origins (`aimscribe.trusted-origins.v1`) |
| Copy key | 256-bit symmetric | Archive worker only (`AIMS_COPY_KEY`), one sealed offline copy | Encrypts every cloud copy |
| CMED hospital keys | ES384 (ES256/RS384 accepted) | CMED's servers; AIMS LAB holds public JWKs only | Channel B client assertions |
| Backup key | Symmetric | Backup container | Encrypts dumps and WAL |
| Portal identity | Google OIDC + PKCE | Google | Staff sign-in; allow-list decides access |

### 13.2 Threats and controls

| Threat | Control |
|---|---|
| A malicious website on the doctor's PC drives the recorder | Origin allow-list (central, signed, versioned); `null`/absent origin refused; loopback bind; Windows-session check |
| A forged or replayed trusted-origin list | Ed25519 signature with its own domain; monotonic version; invalid entries (wildcards, paths) dropped |
| A copied recorder on another machine | TPM-bound key; device token hashed; request signatures and nonces |
| Unknown PCs filling storage | Pending quota per clinic, registration rate limit per address, evidence-based blocking, 30-day expiry |
| Audio tampering or deletion | Hash chain signed by the device, verified server-side before archive; SHA-256 per segment |
| PC deletes audio too early | Deletes only against server-signed purge receipts |
| A stolen CMED token | 5-minute lifetime, hospital scope, optional CIDR pinning, per-hospital keys, `jti` replay guard |
| A stolen CMED private key | Revoke the `kid`/client from the dashboard: effective immediately |
| Compromised API process | DB role without DDL/TRUNCATE/trigger rights; append-only audit; no access to the copy key or copy-bucket delete |
| Operator mistake / bad migration | Data guard; pre-deploy backup; CI gate; schema check |
| Mass erasure by a bug | Erase circuit breaker (`AIMS_ERASE_BREAKER_*`), manual release audited |
| TLS interception on clinic PCs | Recorder-side interception check and alert |
| Web attacks on the portal | Strict CSP, HSTS, `X-Frame-Options: DENY`, no-referrer, body limit, internal routes 404 at the edge |

---

## 14. Reliability, failure modes and operations

| Failure | Effect | Behaviour |
|---|---|---|
| Clinic internet down | None on recording | Spool holds audio; health events logged and sent later |
| New PC offline at first patient | Start delayed | Start kept and retried; records once registered |
| Jabra unplugged | None | Continues on the PC microphone; switches back when replugged; alert raised |
| AIMS LAB server down | None on recording | Uploads resume; monitor/uptime check alerts |
| CMED server silent | Recordings stay unconfirmed | `cmed_silent` alert after 2 h of clinic recordings; no PC is blocked |
| Transit bucket unreachable | Archive delayed | Archive-queue alert after 30 min |
| Database disk filling | Risk to writes | Floor alerts (`AIMS_DB_FLOOR_GB`, `AIMS_DISK_FLOOR_GB`) |
| Bad release | Not deployed | CI must pass; force-pushed history refused; health check after restart |
| Windows shutdown mid-consultation | None | Shutdown guard closes the recording cleanly |
| Installer upgrade mid-consultation | None | Graceful stop event; recording closed, not killed |

**Monitoring.** `monitor` evaluates disk, database, backups and drill age, archive queue,
silent recorders, certificate expiry and CMED silence during clinic hours, and alerts by
e-mail/webhook. A GitHub `uptime` workflow checks the public health endpoint externally.

---

## 15. Data retention and erasure

| Data | Retention |
|---|---|
| Unconfirmed recording | Erased after `AIMS_UNCONFIRMED_ERASE_DAYS` (default 7) |
| Refused recording | Erased immediately, everywhere (PC, transit, archive, copy) |
| PC spool | Until purge receipts |
| Transit segments | Until archived and the copy is proven |
| Archive WAV on server | `AIMS_ARCHIVE_LOCAL_RETENTION_DAYS` after a proven copy (0 on cloud hosts) |
| Cloud copy | Long-term research copy; `legal_hold` respected |
| Expired tokens, nonces, JTIs | Cleaned in small batches by the maintenance sweep |

Erasure is carried out by the archive worker from `erase_tasks`, reported back, and
audited. A circuit breaker stops erasing if an unusually large share would be erased at
once, until an operator releases it.

---

## 16. Build, test and release pipeline

```mermaid
flowchart LR
    DEV["Commit to main"] --> CI["GitHub Actions: tests<br/>server · recorder · deployment"]
    CI -->|pass| AD["Server autodeploy timer<br/>(pulls every 2 min)"]
    CI -->|fail| X1["Not deployed · alert"]
    AD --> G{"Fast-forward?<br/>no local edits?"}
    G -->|no| X2["Refused · alert"]
    G -->|yes| B["Build images<br/>(old ones keep serving)"]
    B --> M{"Schema change?"}
    M -->|yes| BK["Labelled backup of both DBs"] --> MG["migrate.sh"]
    M -->|no| RS
    MG --> RS["Restart changed services"]
    RS --> H{"/health/ready"}
    H -->|ok| OK["Deployed"]
    H -->|fail| X3["Alert"]
```

* **Server:** every suite runs in CI against a real PostgreSQL; the deployment gate also
  checks migrations against the data guard.
* **Recorder:** built with PyInstaller and packaged with Inno Setup (`recorder/BUILD.bat`);
  release builds are code-signed. One installer for every hospital; the server address is
  preset and CMED addresses arrive from the server.
* **History is append-only on `main`.** Autodeploy refuses a rewritten branch.

---

## 17. Configuration reference

Configuration is by environment (`deploy/uiu/.env`, from `.env.example`). Secrets are
never committed. Main groups:

| Group | Variables (selection) |
|---|---|
| Public endpoint | `AIMS_PUBLIC_HOST`, `AIMS_PUBLIC_BASE_URL`, `AIMS_TRUSTED_PROXIES` |
| Signing keys | `AIMS_GRANT_PRIVATE_KEY`, `AIMS_RECEIPT_PRIVATE_KEY`, `AIMS_COPY_KEY` |
| Devices | `AIMS_SELF_REGISTRATION`, `AIMS_PENDING_DEVICES_PER_CLINIC`, `AIMS_REGISTRATIONS_PER_ADDRESS_PER_DAY`, `AIMS_REQUIRE_DEVICE_SIGNATURES`, `AIMS_MIN_RECORDER_VERSION`, `AIMS_BLOCK_AFTER_RECORDINGS`, `AIMS_BLOCK_AFTER_HOURS`, `AIMS_BLOCK_GRACE_MINUTES`, `AIMS_PENDING_SILENT_DAYS` |
| CMED | `AIMS_CMED_LEGACY_KEYS` (off in production), `AIMS_CMED_SILENT_MINUTES`, `AIMS_FHIR_SYSTEM_*` |
| Storage | `MINIO_*` (transit), `AIMS_COPY_*` (copy), `AIMS_JSON_*`, `AIMS_OFFSITE_*` |
| Limits | `AIMS_RATE_PER_IP_PER_MINUTE`, `AIMS_TOKEN_RATE_PER_MINUTE`, `AIMS_AUTH_FAILURE_LIMIT`, `AIMS_MAX_SEGMENT_BYTES`, `AIMS_BULK_*`, `AIMS_DOWNLOAD_*` |
| Retention | `AIMS_UNCONFIRMED_ERASE_DAYS`, `AIMS_ERASE_BREAKER_*`, `AIMS_ARCHIVE_LOCAL_RETENTION_DAYS`, `AIMS_BACKUP_KEEP_DAYS` |
| Portal | `AIMS_GOOGLE_CLIENT_ID`, `AIMS_GOOGLE_CLIENT_SECRET`, `AIMS_PORTAL_OWNER_EMAIL` |
| Monitoring | `AIMS_ALERT_*`, `AIMS_*_FLOOR_GB`, `AIMS_CLINIC_HOURS`, `AIMS_DRILL_EVERY_DAYS` |

The recorder reads `.env` next to the executable (written by the installer):
`AIMS_BACKEND_URL`, optional extra `AIMS_ALLOWED_ORIGINS`, microphone and segmenting
settings.

---

## 18. Repository map

```text
AIMScribe_v3/
├── recorder/                 Windows recorder (AIMScribe.exe)
│   ├── main.py               tray application, lifecycle
│   ├── config.py             settings from .env
│   ├── api/                  Channel A: WebSocket + HTTP on 127.0.0.1:5050
│   ├── core/                 controller, capture, spool, crypto, TPM, upload
│   ├── ui/                   Stop / Pause overlay
│   ├── installer.iss         Inno Setup script
│   ├── BUILD.bat             build + sign + package
│   └── tests/
├── backend/
│   ├── src/                  FastAPI server (device protocol, Channel B, portal)
│   ├── archive_worker/       archive, cloud copy, erasure, decrypt service
│   ├── scripts/              numbered migrations, clinical/, roles/, guard/
│   ├── docker/               images
│   └── tests/
├── deploy/
│   ├── uiu/                  production compose, Caddy, Postgres, PgBouncer,
│   │                         backup, monitor, autodeploy, runbooks
│   └── local/                single-machine development stack
├── cmed-web/                 stand-in for CMED's page (integration testing)
├── openapi/                  API descriptions
├── tools/                    operator and test tools (CMED key tool, load test, …)
└── .github/workflows/        tests (CI gate), uptime
```

---

*This document is maintained with the code. A change to a protocol, schema or deployment
component should update the relevant section in the same commit.*
