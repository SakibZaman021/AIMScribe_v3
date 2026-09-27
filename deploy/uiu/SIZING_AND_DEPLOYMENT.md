# Deploying AIMScribe on the machines CITS has offered

Two machines are on the table, and they are good for different things.

| | UIU server | The three-month cloud server |
|---|---|---|
| Cores | 8 | 8 |
| Memory | 8 GB — **4 GB after Windows** | 16 GB |
| Storage | to be confirmed | 1 TB |
| Lifetime | ours | **three months, then gone** |

Everything below is measured on the bench, not estimated: 28 rooms recording
30 minutes each at once, 2,434 pieces, 6.1 GB uploaded in one storm.

---

## 1. The first decision: Windows costs half the machine

Windows takes 4 GB of the 8, and Docker Desktop's virtual machine takes a
further slice of what is left. AIMScribe then runs in about 3.5 GB.

On Ubuntu Server the same box gives AIMScribe about **7.5 GB** and faster
disk I/O, because containers are not going through a virtual machine.

It still fits on Windows — this file shows how — but it fits with nothing to
spare, and that is a choice being made, not a fact of the system. If Windows
is required for other reasons, take it; if it is habit, Linux doubles the
machine for free.

---

## 2. What the software actually uses

Measured under the 28-room storm, not at rest:

| Container | Idle | Under load | Ceiling to set |
|---|---|---|---|
| The server (`api`) | 180 MB | 320 MB (4 workers) | 1 GB with 2 workers |
| PostgreSQL | 60 MB | 240 MB | 1 GB |
| Archive worker | 22 MB | ~250 MB mid-merge | 768 MB |
| Redis | 13 MB | 23 MB | 256 MB |
| Object storage (MinIO) | 70 MB | 860 MB | 768 MB |
| Gateway (TLS) | — | ~40 MB | 256 MB |
| PgBouncer | — | ~15 MB | 128 MB |

Every service now has a ceiling in `docker-compose.yml`, so one of them
misbehaving cannot take the machine down with it.

### The 4 GB plan

```
PostgreSQL        1 GB      AIMS_PG_MEMORY=1g,  AIMS_PG_CONF=./postgres/postgresql.4gb.conf
The server        1 GB      AIMS_API_MEMORY=1g, AIMS_SERVER_WORKERS=2
Archive worker  768 MB      AIMS_ARCHIVE_MEMORY=768m, AIMS_ARCHIVE_CPUS=2
Redis           256 MB      AIMS_REDIS_MEMORY=256m
Gateway         256 MB      AIMS_GATEWAY_MEMORY=256m
PgBouncer       128 MB      AIMS_PGBOUNCER_MEMORY=128m
                -------
                 3.4 GB of ceilings, about 1 GB in normal use
```

**Two things must be true for this to fit.**

1. **`AIMS_PG_CONF=./postgres/postgresql.4gb.conf`.** The default config is
   sized for the recommended 64 GB machine: `shared_buffers = 16GB`. On a 4 GB
   box PostgreSQL will not start at all. The small profile gives up caching
   and gives up nothing about durability — every `fsync`, `wal_level` and
   `synchronous_commit` setting is identical.
2. **No MinIO on that machine.** This was tested rather than assumed, and it
   failed: capped at 768 MB, object storage could not take the storm —
   *"upload of piece failed"* repeatedly, and Docker Desktop itself went down
   with it. MinIO buffers in proportion to what is arriving, and 28 rooms
   deliver 6 GB in ninety seconds. So pieces in transit go to **Cloudflare
   R2**, which is the Phase 6b design anyway: R2 for pieces, Amazon Deep
   Archive for copies, the finished WAV on UIU's own disk. Give MinIO 2 GB or
   do not run it — there is no useful middle setting.

   That crash is worth reading twice. It happened on a laptop with 13.7 GB,
   of which Docker had 6.6. On a Windows box where AIMScribe has 4 GB, the
   margin is smaller still, and the failure took the whole engine with it —
   not one container. It is the strongest single argument for Linux on that
   machine.

`AIMS_SERVER_WORKERS=2` rather than 4: each worker is a whole Python process
with its own connection pool, about 300 MB. Two carried 28 rooms on the
bench with every reply inside its target.

---

## 3. Storage: what 20,000 consultations actually weigh

Measured: **5.02 MB per minute** of audio (44.1 kHz, mono, 16-bit WAV — the
format the SRS fixes, and the one the acoustic work needs).

| Average consultation | WAV for 20,000 | Lossless copies (~60%) | Total |
|---|---|---|---|
| 10 minutes | 1.0 TB | 0.6 TB | **1.6 TB** |
| 15 minutes | 1.5 TB | 0.9 TB | **2.4 TB** |
| 20 minutes | 2.0 TB | 1.2 TB | **3.2 TB** |
| 30 minutes | 3.0 TB | 1.8 TB | **4.8 TB** |

Your 5 TB figure matches the long end. Plan for it, because a study that
fills its disk stops collecting.

Everything else is small: both databases together measured **0.5 MB per
recording**, so 20,000 consultations is about **10 GB** — call it 50 GB with
indexes, audit trail and room to breathe. WAL and local backups, 100 GB.

**So: 1 TB NVMe for the system, the databases and working files; 5 TB or more
of separate array for the archive.** The 4×6 TB RAID recommendation stands —
in RAID 10 it gives 12 TB usable, which is the study twice over plus the
years of running afterwards.

**Ask CITS what disks the 8 GB machine has.** Memory can be worked around;
the archive cannot. If that box has a single 1 TB disk, it can run the
system but it cannot hold the study, and the archive needs somewhere else
from day one.

---

## 4. The three-month server: useful, but never the system of record

It is the better machine and it disappears. That combination decides what it
may hold.

**Never on it:** the archive, either database, or the only copy of anything.
When it vanishes, so does everything that lived only there, and the study
ends with it.

**Reasonable uses:**

* **Burn-in and load testing** before the real server is ready — exactly the
  28-room storm in this file, without touching a machine that carries
  patients.
* **Transcription and AI work**, which is compute against recordings that are
  already safe at UIU. If the machine vanishes mid-job, the job is re-run;
  nothing is lost.
* **A second copy**, mirrored continuously *from* UIU — extra safety that
  expires, not storage you depend on.

If it is used for anything, set the exit date in writing on day one and stop
new work **two weeks before** expiry. Moving 1 TB over the internet takes
days, not an afternoon, and the last week is exactly when something will go
wrong.

---

## 5. Do we need a public URL?

It depends on one thing: **where the clinic PCs are.**

| The clinics are | What you need |
|---|---|
| On UIU's own network | **No public URL.** An internal DNS name, a certificate from UIU's own authority. The server never faces the internet |
| Elsewhere, on the internet | A public hostname with TLS — `aimscribe.uiu.ac.bd` — reachable on 443, and **80 open for certificate renewal** |
| Elsewhere, and CITS will not open ports | A tunnel: Cloudflare Tunnel or Tailscale. No inbound port at all, the server dials out |

Three things are true whichever you choose:

* **A name, never an IP.** Certificates are issued for names, and a name
  survives the machine moving. This was proved the hard way on the bench: a
  DHCP lease changed overnight and every recorder pointed at the old number
  went silent.
* **Lower case.** Upload links are signed with the host inside them and are
  sent lower-cased; a capital letter refuses every upload with
  `SignatureDoesNotMatch`.
* **Not port 6000.** Browsers and Node both refuse it. The system uses 6060
  internally and 443 at the edge.

Changing the address later is three settings and no code:
`AIMS_PUBLIC_HOST` on the server, `AIMS_SERVER_URL` at CMED, and
`AIMS_BACKEND_URL` in each recorder's `.env`. See
[CHANGING_THE_SERVER_URL.md](CHANGING_THE_SERVER_URL.md).

---

## 6. Deploying it, in order

Nothing here needs a code change. It is configuration, and then proof.

**1. Get the machine right first.**
   * The archive array mounted and writable (`AIMS_ARCHIVE_PATH`).
   * The databases on the NVMe (`AIMS_DB_PATH`), not the array.
   * A fixed address, a name for it, and the clock synchronised — recordings
     are filed by the clinic's date and matched to CMED by time.

**2. Write `.env`** from `.env.example`, then, on a 4 GB machine, add:

```
AIMS_PG_CONF=./postgres/postgresql.4gb.conf
AIMS_PG_MEMORY=1g
AIMS_PG_SHM=256m
AIMS_API_MEMORY=1g
AIMS_SERVER_WORKERS=2
AIMS_ARCHIVE_MEMORY=768m
AIMS_ARCHIVE_CPUS=2
AIMS_REDIS_MEMORY=256m
AIMS_WORK_TMPFS=1g
```

`AIMS_WORK_TMPFS=1g` matters more than it looks. The template's 6 GB is
sized for two-hour consultations; tmpfs pages count against RAM, so on a
4 GB machine that one setting can reserve more than every container ceiling
combined. Measured against Aalo's 10-minute and Amader Susastho's 20-minute
caps, joining, compressing and encrypting one consultation peaks near
320 MB, so 1 GB is already generous.

**3. Bring it up** — `docker compose up -d` — and wait for every container to
   report healthy. The databases, their roles and all eleven migrations are
   built on the first start; nothing is applied by hand.

**4. Register the clinics and issue CMED's key.** One command per clinic;
   `hospital_id` is immutable once set, so get it right.

**5. Prove it before a patient is in the room.** In this order:

```
python tools/channel_b_test.py --server https://<host> --key <CMED key>
python tools/load_test.py --server https://<host> --admin-key <key> \
       --cmed-key <key> --hospital HOSP001 --rooms 28 --hours 0.5 \
       --minutes 30 --gap 0 --speed 20
python tools/preflight.py --server https://<host> --agent "<agent folder>"
```

   The first checks CMED's two messages against the server's own rules. The
   second is the storm: 28 rooms, 150 MB each, and it must report
   **AT-29: PASSED** with no piece lost. The third is run on each clinic PC
   and includes listening to its microphone.

**6. Restore something.** Take one finished recording, fetch its encrypted
   copy, and run `restore.py`. A copy nobody has ever read is not a backup.
   Do this on day one and once a month after.

**7. Only then** install a recorder on a clinic PC and record a real
   consultation end to end.

---

## 7. What to watch once it is running

* **The dashboard**, `/api/v2/dashboard` — volume by clinic, anything
  unconfirmed, anything not yet copied.
* **Disk on the array.** At 20 minutes average and 40 consultations a day,
  the study grows about **4 GB a day**. A month is 120 GB.
* **`confirmation = unconfirmed`** in `sessions`. A recording CMED never
  describes is erased after 24 hours — correct, and it has already cost one
  real recording on the bench when Channel B was silently failing. If that
  count is not zero every morning, something is wrong with CMED's side.
* **The archive worker's queue.** If recordings arrive faster than it merges
  them, it is short of CPU: raise `AIMS_ARCHIVE_CPUS`, or give it its own
  machine.

---

## 8. If you get to choose the machine

The recommendation has not changed, and the measurements support it:

| | Minimum that works | Recommended |
|---|---|---|
| Cores | 8 | 16 |
| Memory | 8 GB **on Linux** (4 GB is Windows' share) | 32–64 GB |
| System disk | 1 TB NVMe | 2 TB NVMe, mirrored |
| Archive | 5 TB usable | 4 × 6 TB, RAID 10 |

The 8-core, 8 GB machine will run this study if it runs Linux and has the
array. On Windows it will still run it, with two workers and no local object
storage, and no room for anything else on the box.
