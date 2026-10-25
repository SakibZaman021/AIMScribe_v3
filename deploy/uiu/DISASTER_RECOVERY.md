# Disaster recovery

What keeps AIMScribe's data safe, and exactly what to do when something goes
wrong. Every command runs on the server, as `aims`, in
`~/AIMScribe_v3/deploy/uiu`.

## What protects what

| Layer | What it does | How much can be lost |
|---|---|---|
| **WAL archive** | Every change to both databases is compressed into `$AIMS_BACKUP_PATH/wal` as PostgreSQL writes it, encrypted, and sent to the off-site bucket within a minute (`backup.sh`) | at most 5 minutes (`archive_timeout`) |
| **Base backup, nightly** | The whole database server, encrypted, here and off-site; the WAL replays onto it | - |
| **Dumps, nightly** | Each database as SQL, encrypted, here 7 days and off-site 90 | a day, if all else failed |
| **Before every database change** | The deployer backs up both databases before any update that touches `backend/scripts` or `deploy/uiu/postgres`; no backup, no deploy | nothing |
| **Restore drill, monthly** | The backup job rebuilds the server from the off-site copy alone, compares every table with the live one, and records the result; the monitor emails if it fails | - |
| **Erase circuit breaker** | Automatic erasing stops for a clinic when far more of its recordings would go than stray clicks explain; `AIMS_ERASE_PAUSED=true` stops it everywhere | - |
| **Deploy gate** | Only a commit whose tests passed on GitHub is deployed; a migration that drops or deletes fails those tests unless it was approved in review | - |
| **Alerts** | The monitor emails problems, all-clears and a weekly report (`set-alerts.sh`); GitHub's `uptime` workflow emails if the whole server stops answering | - |
| **Audio** | Three homes: the doctor's PC until a receipt, the archive on this server's volume, the encrypted copy in R2 | - |

The off-site backup bucket should have a **bucket lock** (Cloudflare R2 >
the bucket > Settings > Bucket lock rules: whole bucket, 30 days). Then no
key - stolen, or used by mistake - can delete the last month of backups.
The audio bucket must *not* be locked: a patient who withdraws consent has
to be deleted from it.

## Recover the databases as they were at a moment

For a mistake: data deleted or changed by a person, a bug or an update.

1. **Recover a copy** to the minute before the mistake (clinic time). The
   live databases are not touched.

   ```sh
   docker compose run --rm backup pitr recover before_mistake "2026-10-25 14:30"
   ```

   It prints what the copy holds (rows per table, the last recording opened).
   Check those numbers. Omit the time for "as late as possible", after
   losing the server's disk but not the bucket.

2. **Put the recovered copy in place** - recorders keep their audio and
   retry while the server is stopped; CMED's messages are retried by CMED:

   ```sh
   docker compose stop
   sudo mv /srv/aimscribe/db /srv/aimscribe/db.before-restore-$(date +%Y%m%d%H%M)
   sudo mv /srv/aimscribe/backup/restore/before_mistake /srv/aimscribe/db
   docker compose up -d
   docker compose run --rm backup --base     # a new base backup at once
   ```

3. Check `https://<host>/health` and the dashboard. Keep the old folder a
   week, then remove it.

Anything written between the moment recovered to and the restore - a
prescription CMED sent, a recording uploaded - is not in the copy. CMED can
resend; the recorders resend what has no receipt.

## Only one table, or one recording

Recover a copy as above, but do not put it in place. Start it and copy
across only what is needed:

```sh
docker compose run --rm --entrypoint sh backup -c '
  su-exec postgres postgres -D /backups/restore/before_mistake -p 5499 \
     -c listen_addresses=127.0.0.1 -c unix_socket_directories=/tmp \
     -c hba_file=/backups/restore/before_mistake/pg_hba.conf &
  sleep 5; psql -h 127.0.0.1 -p 5499 -U "$PGUSER" aims_recordings'
```

## An update went wrong

The deployer rolls back an update that does not come up healthy, by itself,
and emails. If an update came up but damaged data, its database backup was
taken just before it:

```sh
docker compose run --rm backup fetch latest aims_recordings     # or by name: *_predeploy-<commit>*
docker compose run --rm backup restore /backups/<file> aims_check
```

`restore` never writes over a live database: compare `aims_check`, then copy
what is needed, or recover the whole server to the minute before the deploy
(the time is in `~/.aimscribe-deploy/deployed.log`).

## The server is gone

1. A new droplet, set up as in `SIZING_AND_DEPLOYMENT.md`, with the same
   `.env` (kept off the server, by the owner) - above all the same
   `AIMS_BACKUP_KEY`: without it no backup can be read.
2. Before starting the stack, recover from the bucket:

   ```sh
   docker compose run --rm -e AIMS_RESTORE_FROM=offsite backup pitr recover rebuilt
   sudo mv /srv/aimscribe/backup/restore/rebuilt /srv/aimscribe/db
   docker compose up -d
   ```

3. Point the DNS name at the new server. The archive's audio is copied back
   from R2 by `backend/archive_worker/restore.py`.

## Recordings are about to be erased that should not be

- Everything, now: set `AIMS_ERASE_PAUSED=true` in `.env`, then
  `docker compose up -d api`.
- One clinic the circuit breaker stopped: look at
  `GET /api/v2/admin/erase/status`; if those recordings really are strays,
  release it with `POST /api/v2/admin/erase/release`
  (`{"hospital_id", "released_by", "reason", "hours"}`).
- One recording: `POST /api/v2/admin/session/<id>/confirm`, or a legal hold.

## Rehearse

The backup job rehearses by itself every `AIMS_DRILL_EVERY_DAYS` (30). To run
it now, and see the result the monitor reads:

```sh
docker compose run --rm backup pitr drill
sudo cat /srv/aimscribe/backup/.last_drill
```
