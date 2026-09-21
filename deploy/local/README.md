# AIMScribe in Docker

The whole system — server, both databases, Redis, object storage and the
archive worker — in one command. Use it on a laptop to try things, and on the
AIMS LAB server to run the study.

```
start.bat            (Windows)              ./start.sh      (Linux)
```

That is the whole of it. The first run takes a few minutes while the images
build; after that it is seconds. When it finishes it prints the server
address, the administrator key, CMED's key, and where to find the folder that
configures a doctor's PC.

To stop, keeping everything: `python bootstrap.py --down`.
To start again from nothing: `python bootstrap.py --reset`.

---

## What starts

| Container | What it is | Where it is |
|---|---|---|
| `aimscribe-v3-server` | the AIMS LAB server | http://localhost:6060 |
| `aimscribe-v3-postgres` | `aims_recordings` and `aims_clinical`, separate roles | localhost:5433 |
| `aimscribe-v3-redis` | queues and locks | inside only |
| `aimscribe-v3-storage` | stands in for Cloudflare R2 and the copy store | console on :9001 |
| `aimscribe-v3-archive-worker` | joins, files, copies, and issues purge receipts | writes `archive\` |
| `aimscribe-v3-cmed-web` | CMED's test site, only with `--cmed` | http://localhost:3000 |

The recorder is not here: it needs a microphone, so it runs on the doctor's
PC. Everything else is a container.

Recordings land in `deploy\local\archive\` — sorted by clinic, doctor and
date, each with its clinical JSON. Point `ARCHIVE_PATH` in `.env` somewhere
else and they land there instead; on the UIU server that is the archive array.

---

## Which system am I looking at?

Version 1 runs in Docker on this machine too, and its containers are named
`aimscribe-*`. Everything here is named **`aimscribe-v3-*`** and carries a
label, so one command lists this stack and nothing else:

```
docker ps --filter label=com.aimslab.system=aimscribe-v3
```

Three more ways to be sure, from the outside:

| Ask | Version 1 | This |
|---|---|---|
| Open the server in a browser | no page - `{"detail":"Not Found"}` | a page that names itself, in green |
| `curl http://localhost:6060/health` | `"version":"6.0.0"` | `"system":"AIMScribe v3 - AIMS LAB server"`, `"srs":"3.3"` |
| `docker compose ls` | `aimscribe_backend_render-main` | `aimscribe-v3` |

They also do not share anything: separate containers, separate volumes,
separate network. Stopping one never touches the other, and
`bootstrap.py --down` only ever stops `aimscribe-v3`.

The one thing they *can* share is a port. If version 1 is already using 6000
on this machine, set `AIMS_PORT` in `.env` to something else - `bootstrap.py`
checks, and refuses to start rather than letting the two of them answer to the
same address.

---

## The links, and which of them work

Right after `start.bat`:

| Link | |
|---|---|
| http://localhost:6060 | the front page: what this is, and where everything else is |
| http://localhost:6060/health | one line saying the server, database, Redis and storage are up |
| http://localhost:6060/api/v2/dashboard | the day as UIU sees it - **it asks for the administrator key** printed at startup |
| http://localhost:9001 | the storage console - user `aimslocal`, password in `.env` |
| http://localhost:3000 | CMED's test site, **only** if you started with `--cmed` |

`http://localhost:6060/api/v2/...` on its own will say `Not Found` or ask for a
credential: those are the recorder's and CMED's doors, not pages to browse.

---

## Connecting a doctor's PC

`bootstrap.py` writes `out\recorder\` — the two public keys the agent pins,
its `.env`, and one enrolment token. Copy that folder to the PC and follow the
README inside; or, on a PC that has the agent unpacked already:

```
python bootstrap.py --agent "C:\Path\To\AIMScribe_Agent"
```

which puts each file where the agent looks for it. Start
`AIMScribe_Agent.exe`: it enrols itself once and shows a tray icon.

The agent's `.env` points at this machine's address on the network, not at
`localhost`, so a PC in another room works with no further changes. If the
machine's address changes — a new network, a new DHCP lease — run
`bootstrap.py` again and copy `out\recorder\.env` across.

---

## The settings

`.env` is written on the first run and never rewritten: every password, both
signing keys, and the key that encrypts the cloud copies. Keep it. Delete it
and the stack comes up with new keys, which means every enrolled PC has to
enrol again.

It is a bench file, not a clinic's. A real deployment's secrets are made the
same way but held by an administrator — see `../uiu`.

| Setting | What it does |
|---|---|
| `AIMS_PORT` | the server's port (6060 - see below) |
| `POSTGRES_PORT` | where psql can reach the database (5433) |
| `ARCHIVE_PATH` | where recordings are filed |
| `AIMS_STORAGE_HOST` | the address upload links name; set for you each run |
| `TZ` | the clinic's zone, for the date a recording is filed under |

---

## Proving it works

```
python tools\channel_b_test.py --server http://localhost:6060 --key <CMED key>
python tools\load_test.py --server http://localhost:6060 ^
       --admin-key <admin key> --cmed-key <CMED key> ^
       --hospital HOSP003 --cmed-hospital CMED-LOCAL-01 --rooms 14
```

The first checks CMED's two messages against the server's own rules. The
second runs a clinic day through it — fourteen rooms, real chains, real
uploads — and with `--cmed-key` it plays CMED's side too, so the recordings
confirm, archive, and are copied. Watch it happen:

```
docker compose logs -f archive-worker
```

The dashboard at http://localhost:6060/api/v2/dashboard asks for the
administrator key and shows the day as UIU sees it.

---

## When it will not start

**"Something is already answering on port 6000."** Another server — often
`tools\bench.py` left running — has the port. Docker will publish it anyway,
both will answer to `localhost`, and requests will be split between two
systems with different databases: half of them refused with credentials the
other one issued. Stop it, or set `AIMS_PORT` to something else.

**The browser says the page "may be temporarily down" while curl works.**
The port is one a browser refuses to dial. Chrome and Firefox keep a list of
them - 6000 is X11's, 6665-6669 are IRC's - and they will not connect
whatever is listening there, so the recorder, the tests and curl all work
while the dashboard appears dead. That is why this stack publishes **6060**
and not 6000. `bootstrap.py` moves the port for you if `.env` names a
blocked one.

**"Docker is not running."** Start Docker Desktop and try again.

**The server never answers.** `docker compose logs api` says why. A first run
on a fresh machine may simply be slow to build.

**A PC says `DEVICE_NOT_ENROLLED`.** The databases were reset while the agent
kept its identity. Delete `state\device.json` and `state\device.token` in its
data folder, give it a fresh token from `out\recorder\`, and start it again.

---

## The same thing, for real

`../uiu` is this stack as UIU runs it: TLS at the edge, PgBouncer, secrets
handed over rather than generated, Cloudflare R2 and Amazon Glacier instead of
MinIO, and backups. The software in the containers is identical — which is the
point of running this one.
