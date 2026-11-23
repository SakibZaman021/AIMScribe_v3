# The beta server: testing never touches the real field again

Plan agreed with the owner on 24 Nov 2026 (Dhaka).

## The problem

The real server holds two kinds of recording side by side:
- real consultations;
- beta tests (test clinics, test PCs, test CMED pages).

The dashboard, the reports and every research export see both. Deleting the tests now would not stop new ones arriving tomorrow.

## The decision

There will be two servers, built from the same code:

- **The real server.** It holds only the field: the production CMED pages, the clinic PCs, and the real hospitals.
- **The beta server.** It is a second, fully separate copy for every test: test PCs, test CMED pages, Postman. It has its own dashboard.

Nothing from testing can reach the real data afterwards, because tests go to a different server.

## Where the beta server lives

It runs on the same machine as the real server, as a separate set of containers.

The machine has room for it:

| Resource | Size | In use now | Free |
|---|---|---|---|
| CPUs | 4 | about 2% | ample |
| Memory | 8 GB | 1.4 GB | 6.5 GB available |
| Disk | 232 GB | 36 GB | 197 GB |

The beta server adds about 1 GB of memory and almost no CPU.

| | Real server (unchanged) | Beta server (new) |
|---|---|---|
| Address | `https://aimscribe.iriic.uiu.ac.bd` | `https://aimscribe.iriic.uiu.ac.bd:9443` (same name, its own port; the certificate already covers the name) |
| Containers | `aims-*` | `aimsbeta-*`: own database server, API, worker and archive worker |
| Database | `aims_recordings`, `aims_clinical` | its own database server, with the same two databases |
| Archive disk | `/srv/aimscribe/archive` | `/srv/aimscribe-beta/archive` |
| Uploads (R2) | the production bucket | its own bucket |
| Cold copies | the copy store | none for new beta recordings. Moved recordings keep pointing at their existing copies, read with the same key. |
| Grant signing key | as now | the **same** key, so a test PC needs no new installer, only its server address changed |
| Dashboard login | Google, cookie `aims_portal` | Google, cookie `aims_beta_portal` (a cookie ignores the port, so the names must differ) |
| Trusted CMED pages | `https://karail.aaloclinic.org:8443`, `https://10.0.40.211`, `https://corporate.cmedhealth.com` | every testing page: `https://corporate-dev1.web.app`, `https://dev.aaloclinic.org`, `https://postman.aimscribe.test`, `http(s)://karail.aaloclinic.org`, `http://karail.aaloclinic.org:8443` |
| Deploys | from `main`, as now | from the same `main`, at the same time |

## What stays on the real server, and what moves

Counted on the live server on 24 Nov 05:55 Dhaka.

**Stays: 107 recordings.**

| Clinic | PC | Recordings that stay |
|---|---|---|
| AALO_KARAIL | GP-Males | 39, from 23 Nov 16:29 |
| AALO_KARAIL | GP-Female | 35, from 23 Nov 16:29 |
| AMADER_SUSASTHO | Lenovo | 33, from 22 Nov 15:00 |

**Moves to beta: 245 recordings.** This is everything else:

| Clinic | PCs | Recordings |
|---|---|---|
| TEST | DESKTOP-S3AS8QK, E5GGS9R, Sahil, CMED, 0D1PA7I, Hannibal05, MacBook | 112 |
| HOSP_SUSASTHO_TEST | 6DNRG2F, E5GGS9R, S3AS8QK, 5I2FFAL, MacBook, Lenovo | 43 |
| SIM_CLINIC | S3AS8QK | 3 |
| AALO_KARAIL | 0D1PA7I, CMED, EH4I47M, S3AS8QK, DrTZShama | 68 |
| AALO_KARAIL | GP-Males and GP-Female before 16:29 | 5 |
| AMADER_SUSASTHO | Lenovo before 22 Nov 15:00, and 5I2FFAL | 7 |

Each moved recording takes its whole history with it:
- its pieces log, events, alerts and collector notes;
- its CMED messages and clinical record;
- its downloads.

The test clinics, test PCs and test CMED logins move as well.

## Order of work (Dhaka time)

**Day 1: nothing on the real server changes.**
1. Build the beta stack beside the real one. Open port 9443. The real containers are not restarted.
2. Copy the real database into beta (`pg_dump` then restore). It is 29 MB plus 16 MB, a few seconds of read-only work.
3. Copy the archive files of the moved recordings to the beta archive disk.
4. Check beta: health, its dashboard, and every moved recording opening and downloading.

**Day 2: switch, after 20:30 or before 08:00.**
5. Point the test PCs at beta. Each needs one line in `.env` beside the recorder, `AIMS_BACKEND_URL=https://aimscribe.iriic.uiu.ac.bd:9443`, then a recorder restart. This PC (DESKTOP-S3AS8QK) is done from here; the others need someone at the PC or a remote session.
6. Give CMED the beta address for their test logins.
7. Lock the real server's trusted list to the three production pages.
8. Block on the real server every PC except GP-Males, GP-Female and Lenovo. Blocking is reversible.
9. **With the owner's written yes only:** remove the 245 moved recordings and the test clinics, PCs and logins from the real server. Remove the 107 real ones from the beta copy. A full encrypted backup of both databases is taken first and kept.

After that, a new hospital (Aalo Dholpur, Shampur, Narayanganj, Ershadnagar, Mirpur) is added to the real server exactly as Aalo Karail and Amader Susastho were.

## What only the owner can do

1. **Google login for beta:** add `https://aimscribe.iriic.uiu.ac.bd:9443/auth/callback` to the OAuth client's authorised redirect URIs in Google Cloud Console.
2. **R2 bucket for beta:** create a bucket, for example `aimscribe-beta`, in Cloudflare, with an API token for that bucket only. Send the token through `set-r2.sh` on the server, never in chat.
3. **Test PCs that are not this one:** the one-line `.env` change and a restart, in person or by remote desktop.
4. **Every removal in step 9:** a written yes, given after the exact list is shown.

## Will the real dashboard or server be interrupted?

**No.** Building beta adds containers and starts or restarts nothing on the real side. Opening port 9443 and adding the beta site reloads the gateway gracefully, without dropping a connection.

The database copy reads 45 MB in seconds. The archive copy is a disk-to-disk copy on the same machine.

The removals in step 9 are database deletes of a few thousand rows, which take seconds. They run off clinic hours.

## If something goes wrong

- Beta is separate. Stopping it (`docker compose -p aimsbeta down`) leaves the real server exactly as it is.
- Nothing is removed from the real server before beta has been checked and the owner has said yes.
- The backup taken just before step 9 restores the real server as it was.
