# When UIU gives us the server address

The address is not written into any code. It is one setting in four places,
and one of them — the recorders — is the one that takes planning, because a
doctor's PC is enrolled **against** a server, not merely pointed at one.

Say UIU gives us `aimscribe.uiu.ac.bd`.

## The four places

| Who | Setting | Where |
|---|---|---|
| The server itself | `AIMS_PUBLIC_HOST=aimscribe.uiu.ac.bd` | `deploy/uiu/.env` — the gateway gets its certificate for this name |
| The archive worker | nothing | it talks to the API inside the machine (`http://api:6060`) |
| CMED | `AIMS_SERVER_URL=https://aimscribe.uiu.ac.bd` | CMED's own server environment (`cmed-web/.env.local` in the test app) |
| Every recorder | `AIMS_BACKEND_URL=https://aimscribe.uiu.ac.bd` | `.env` beside `AIMScribe_Agent.exe` on each PC, written by the installer |

Nothing else. No code changes, no rebuild of the server, and CMED's page never
learns the address at all — it talks to the recorder on `localhost` and to its
own server, which is where the AIMS LAB address lives.

## What the address has to be before anything else happens

1. A name, not an IP. A certificate is issued for a name, and a recorder that
   pins a name keeps working when the machine behind it changes.
2. Resolvable from the clinics, not only from inside UIU. Test it from a clinic
   laptop before rolling anything out: `nslookup aimscribe.uiu.ac.bd`.
3. Reachable on 443 from the clinics, and on **80 from the internet** — port 80
   is how the certificate renews itself. Blocking it works for 90 days and then
   stops the whole fleet.
4. Fixed. Changing it later is the fleet visit described below, so it is worth
   asking UIU for a name we can keep even if the machine moves.

## The recorders: why this is not just an edit

Each PC enrolled against a server and holds a device token issued **by that
server**, plus the public half of that server's grant key. Point it at a
different server and:

- its device token is unknown there, so every recording is refused;
- the grants that server issues are signed with a key the PC does not have
  pinned, so they would be refused even if the token worked.

The recorder notices this itself. It records the address it enrolled against,
and on start it says so:

    Enrolled against https://old-server but configured for https://new-server.
    Re-enroll if the backend moved.

That message was produced by the end-to-end simulation the first time it ran, so
it is not theory.

### Moving the fleet, in order

1. Stand the UIU server up and let it get its certificate. Check
   `curl https://aimscribe.uiu.ac.bd/health` from a clinic.
2. Move the data: `pg_dump` from Neon, restore into `aims_recordings` (the
   procedure is in `README.md` §4). Leave Neon running, read-only, for a week.
3. Register the clinics and issue CMED's key on the new server; give CMED the
   new `AIMS_SERVER_URL` and key together, and let them switch.
4. Build one installer carrying the new address and the new server's grant public
   key (`recorder/keys/aimslab_grant_pub.pem`), and mint one enrolment token per
   PC (`backend/scripts/mint_enrolment_tokens.py`).
5. Visit the PCs — or run the installer remotely — **one clinic at a time, after
   clinic hours**. Each PC: install, which writes the new `.env` and the new key,
   then enrol with its token. Its spool survives the upgrade; anything still
   waiting is delivered to the old server first if it can be, and otherwise
   stays until the machine is enrolled again.
6. When every PC reports in on the dashboard (`/api/v2/dashboard`, "Recorders"),
   stop the old server accepting new sessions.

### If the address must change without re-enrolling

It can be done, but only if the same database moves with it: the device tokens
live in `devices`, so a server restored from the same dump accepts the same PCs.
Then each PC needs only its `.env` changed and the recorder restarted — and the
grant key must be carried across too, or every recording is refused. Treat this
as the exception; the numbered list above is the safe path.

## A test address and a real one

CMED will want somewhere to point their test system that is not the clinics'
server. Two names, two deployments, two CMED keys:

    aimscribe.uiu.ac.bd          the clinics
    aimscribe-test.uiu.ac.bd     CMED's integration work

The test one can be the same stack with its own `.env`, its own database and its
own buckets. `tools/channel_b_test.py --server https://aimscribe-test.uiu.ac.bd`
is what CMED runs against it.
