# The AIMS LAB server at UIU

Everything this server runs is in this folder. A replacement machine is built
from here and nothing else (`SRS-SRV-09`), so if you change something on the
server, change it here too — a setting that exists only on the running machine
is a setting that disappears with it.

| | |
|---|---|
| What runs here | the API, the transcription worker, the archive worker, PostgreSQL, PgBouncer, Redis, the gateway, backups, the monitor |
| Reachable from the internet | port 443 only, and 80 to redirect and renew the certificate (`SRS-TOP-02`, `SRS-SRV-07`) |
| Holds | the recordings archive, both databases, and the key that encrypts every cloud copy |
| Does not hold | anything CMED can reach into — every exchange with CMED is started by CMED |

---

## 1. Before Docker

**The disks, in three kinds** (`SRS-SRV-03`). They are separate so a long merge
never slows a query a recorder is waiting on:

```bash
# Database — mirrored NVMe with power-loss protection (SRS-SRV-02)
mkfs.ext4 /dev/md/db     && mkdir -p /srv/aims/db     && mount /dev/md/db /srv/aims/db

# Archive array — RAID 6, encrypted at rest (SRS-SRV-04)
cryptsetup luksFormat /dev/md/archive
cryptsetup open /dev/md/archive archive
mkfs.ext4 /dev/mapper/archive && mkdir -p /srv/aims/archive
mount /dev/mapper/archive /srv/aims/archive

mkdir -p /srv/aims/backups
```

Put all three in `/etc/fstab` so they come back after a power cut, and give the
archive array a keyfile in `/etc/crypttab` on the encrypted system disk — a
server that waits at a passphrase prompt after a power cut is a server that is
down until someone walks to it.

**The firewall** (`SRS-SRV-07`). Only 443 from the internet; administration only
from named machines on the UIU network:

```bash
ufw default deny incoming
ufw default allow outgoing
ufw allow 80/tcp comment 'certificate renewal and redirect'
ufw allow 443 comment 'AIMScribe'
ufw allow from 10.0.0.0/8 to any port 22 proto tcp comment 'UIU network only'
ufw enable
```

**The UPS** (`SRS-SRV-06`). A recording being written when the power fails can be
damaged, so the server must shut itself down while the battery still holds:

```bash
apt install nut
# /etc/nut/upsmon.conf — shut down at 30% or 10 minutes remaining, whichever first
systemctl enable --now nut-monitor
upsc ups@localhost battery.charge     # check it answers before trusting it
```

**Docker:**

```bash
apt install docker.io docker-compose-v2
systemctl enable --now docker
```

## 2. Settings

```bash
cd /opt/aimscribe/deploy/uiu
cp .env.example .env && chmod 600 .env
$EDITOR .env          # every password and key; the file says how to make each
```

`.env` is the only file on this machine with secrets in it. It is never
committed — both repositories are public.

Two of those values are not replaceable if lost:

- **`AIMS_COPY_KEY`** decrypts every cloud copy. Keep one sealed offline copy.
- **`AIMS_GRANT_PRIVATE_KEY`** signs recording grants; its public half is built
  into every installed recorder. Changing it means reinstalling the fleet.

## 3. Starting

```bash
docker compose up -d
docker compose ps                      # every service "healthy"
docker compose logs -f api
curl -sS https://$AIMS_PUBLIC_HOST/health
```

On the first start PostgreSQL creates both databases, their roles, and the whole
schema — the v1 baseline, then every migration in order, then the clinical
database (`postgres/init/`). That is the same order
`backend/tests/test_db_integration.py` proves against a real PostgreSQL, from the
same baseline file, so a new server gets what the tests run against.

Then, once:

```bash
# CMED's clinic identifiers (SRS-ENR-19)
curl -X POST https://$AIMS_PUBLIC_HOST/api/v2/admin/hospital \
     -H "X-Admin-Key: $AIMS_ADMIN_KEY" -H 'Content-Type: application/json' \
     -d '{"hospital_id":"HOSP003","name":"Dholpur","cmed_hospital_id":"CMED-..."}'

# CMED's Channel B key — shown once, and only CMED keeps it
curl -X POST https://$AIMS_PUBLIC_HOST/api/v2/admin/cmed-key \
     -H "X-Admin-Key: $AIMS_ADMIN_KEY" -H 'Content-Type: application/json' \
     -d '{"label":"cmed-prod","created_by":"aimslab"}'
```

## 4. Moving here from Render and Neon

The recordings already in Neon come across as a dump; nothing in the archive
changes.

```bash
# On a machine that can reach both, with the Neon connection string:
pg_dump "$NEON_URL" --no-owner --format=plain | gzip > neon.sql.gz

# On this server, into the new database:
gunzip < neon.sql.gz | docker compose exec -T postgres \
    psql -v ON_ERROR_STOP=1 -U aims_recordings -d aims_recordings
docker compose exec postgres psql -U aims_recordings -d aims_recordings \
    -c "SELECT count(*) FROM sessions;"          # compare with Neon before cutting over
```

Then point the recorders here by changing the server address in their
configuration, and leave Neon running read-only for a week before deleting
anything.

## 5. Updating without interrupting a consultation

`AT-30`. The gateway holds each request and retries for up to 30 seconds while
the API restarts, so a recorder in the middle of a consultation sees a pause of
a second or two rather than an error — and even if it did see an error, it keeps
recording and retries by itself.

```bash
git pull
docker compose build api worker archive-worker
docker compose up -d --no-deps api          # one at a time, never all at once
docker compose ps                           # wait for "healthy"
docker compose up -d --no-deps worker archive-worker
```

Do not update PostgreSQL or the gateway during clinic hours: those two do stop
the service while they restart.

## 6. If this server is down for an hour

`AT-73`. Nothing is lost. The recorders keep recording to their own encrypted
buffers, hold each piece until this server acknowledges it, and drain when it
comes back. A doctor sees "not yet confirmed" and keeps working. What to check
after it returns:

```bash
docker compose logs --since 2h api | grep -i error
docker compose exec postgres psql -U aims_recordings -d aims_recordings -c \
  "SELECT confirmation, count(*) FROM sessions WHERE opened_at > now() - interval '1 day'
    GROUP BY confirmation;"
docker compose logs --since 2h monitor | tail -20
```

## 7. Backups and the restore drill

Both databases are dumped nightly at `AIMS_BACKUP_AT`, encrypted with
`AIMS_BACKUP_KEY`, and kept for `AIMS_BACKUP_KEEP_DAYS` (`SRS-DAT-16`). Copy
`/srv/aims/backups` off this machine as well — a backup that only exists on the
machine it protects is not a backup.

**Each quarter**, prove they work rather than assuming it (`SRS-STO-06`,
`SRS-ARC-07`, `AT-74`):

```bash
# 1. A database, restored into a scratch copy and counted
docker compose run --rm backup /opt/aims/restore.sh \
    /backups/aims_recordings_20260920_0230.sql.gz.enc aims_restore_test

# 2. A day's audio, brought back from the cloud copy
#    (cold storage: ask for the objects, then come back in a few hours)
python backend/archive_worker/restore.py restore ./from-glacier /srv/aims/restore-test
python backend/archive_worker/restore.py check /srv/aims/restore-test/<name>.wav <sha256>
```

Write the date and the result somewhere durable. A restore drill nobody recorded
is a restore drill nobody did.

## 8. What the monitor watches

`SRS-SRV-08`. Every five minutes, the four things that stop a clinic:

| Check | Raised when | Because |
|---|---|---|
| Disk space | under the floor in `.env`, per volume | recordings stop when the array fills |
| Archive queue | a recording unarchived for 30 minutes | the archive worker has stopped, or cannot reach the bucket |
| Cloud copies | archived over 24 hours ago, still not copied | that recording exists in one place only |
| Alerts | anything critical raised in the last hour | chains, copies and mappings that failed |
| Silent recorders | an enrolled PC quiet for 4 clinic hours | it may be buffering with nobody draining it |
| Certificate | under 21 days, or unreadable | an expired certificate stops the whole fleet at once |

Set `AIMS_ALERT_WEBHOOK` to a chat webhook. Without it the findings are in the
log only, and nobody reads a log at two in the morning.

```bash
docker compose logs -f monitor
docker compose exec monitor cat /state/status.json      # what the dashboard reads
```

## 9. A load test before the first clinic day

`AT-29`: 14 rooms for a clinic day, with no lost pieces and reply times within
§9.1. Run it from a machine that is not this one, so the network is in the test:

```bash
python tools/load_test.py --rooms 14 --hours 8 --server https://$AIMS_PUBLIC_HOST
```

Watch `docker compose stats` while it runs. If PostgreSQL is the bottleneck,
raise `DEFAULT_POOL_SIZE` in the compose file before raising anything else.

## 10. Things that have surprised people here

- **PgBouncer and prepared statements.** Transaction pooling hands a connection
  to whoever needs it next, so a statement prepared on one connection can be
  looked up on another and is not there. The application turns its statement
  cache off when `AIMS_DB_POOLER=transaction`, which the compose file sets. If
  that variable is ever dropped, the symptom is "prepared statement does not
  exist" under load and nowhere else.
- **The archive worker joins recordings in `/tmp`,** which is a RAM disk sized by
  `AIMS_WORK_TMPFS`. A two-hour consultation needs about 2 GB there; the default
  6 GB covers it twice over. Raising it takes memory from PostgreSQL, so raise
  the machine's memory instead.
- **The certificate is renewed by the gateway itself,** which needs port 80 to be
  reachable from the internet. Blocking 80 "for safety" breaks renewal quietly,
  and the fleet stops 90 days later.
- **Timezone.** Every stored time is UTC; the clinic's local clock is computed
  only for file names. `TZ` in `.env` sets what the logs and the backup schedule
  use, not what the database stores.
