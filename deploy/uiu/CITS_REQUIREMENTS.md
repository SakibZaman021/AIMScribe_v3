# AIMScribe on a UIU server — what we need from CITS

AIMS LAB, UIU · prepared 27 September 2026

AIMScribe records doctor–patient consultations in seven partner clinics, files each
recording on a UIU server with its clinical record beside it, and builds a research
dataset from them. This document asks for one machine, one DNS name, and a small
number of firewall and policy decisions.

Every number below is measured on our own bench, not estimated. The bench runs
28 rooms at once — deliberately twice the real load — with 2,434 audio pieces and
6.1 GB in a single burst.

---

## 1. The deployment we are sizing for

| | Value |
|---|---|
| Clinics | 7 — six Aalo Clinic branches and Amader Susastho |
| Aalo Clinic branches | Karail, Mirpur, Dholpur, Shyampur, Narayanganj, Ershadnagar |
| **Consulting rooms** | **14** |
| **Recordings at the same time, at most** | **14** — one per room |
| Laptops | 14, one per room |
| Doctors | **30**, sharing those 14 rooms across two shifts |
| Shift 1 | 08:30 – 14:20 |
| Shift 2 | 15:00 – 21:00 |
| Clinic days | Six days a week |
| Consultation length | Aalo Clinic **max 10 min**; Amader Susastho **15–20 min** |
| Study target | 20,000 consultations |

**Derived, at 5.02 MB per minute of audio (measured):**

| | Value |
|---|---|
| Room-hours per day | 166 |
| Audio per day, rooms fully occupied | **~50 GB** |
| Audio per day, realistic occupancy | 30–35 GB |
| The 20,000-consultation study, on the array | **~1.15 TB** |
| A full year at these hours | **10–16 TB** |
| Database | 0.5 MB per recording — 50 GB with indexes and audit trail |

> This is a **storage and bandwidth** system, not a processor-heavy one. Average
> request rate across all 14 rooms is about 1.2 per second, peaking near 4.

**Size by rooms, not by doctors.** A room records one consultation at a time, so
30 doctors working two shifts in 14 rooms produce 14 simultaneous recordings, not
30. Every figure above follows from the room count.

---

## 2. What the server will store

This matters for classification: **most of it is patient-identifiable health
information.** Voice recordings identify a person even with no name attached.

### On the archive array

One folder per consultation, under `hospital / doctor / date`:

| File | Contents | Sensitivity |
|---|---|---|
| `<name>.wav` | The consultation audio. WAV, 44.1 kHz, mono, 16-bit | **PHI** |
| `<name>.json` | Patient ID and name, sex, age, doctor, clinic, times, paramedic observations (blood pressure, pulse), diagnoses, prescription with one entry per medicine, investigations ordered, and the previous visit's prescription for a returning patient | **PHI — highest** |
| `<name>.manifest.json` | SHA-256 hashes, the piece list, and the signed integrity chain. No clinical content | Internal |
| `_index.json` | One per day folder, so a day can be read without the database | Internal |

Audio is 44.1 kHz uncompressed by requirement — the acoustic research depends on
it, and a lossy format cannot be checked against the integrity chain.

### In PostgreSQL — two separate databases, two separate roles

| Database | Holds | Sensitivity |
|---|---|---|
| `aims_recordings` | Sessions, integrity chains, purge receipts, device enrolments, confirmation notices, audit log. **Contains no patient names by design** | Internal |
| `aims_clinical` | Patients, encounters, demographics, paramedic observations, prescriptions, diagnoses, investigations, every message as received, and an append-only log of who read what | **PHI** |

Neither role can reach the other's database. The operational dashboard holds only
a read-only credential for `aims_recordings`, so it is structurally incapable of
displaying a patient's name.

### Not on UIU disks

| Where | What | Note |
|---|---|---|
| Cloudflare R2 | Audio pieces in transit from the clinic PCs | Deleted once verified at UIU |
| Cold storage, second provider | One lossless FLAC copy plus its JSON | **Encrypted at UIU before upload, with a key that never leaves UIU.** The provider holds data it cannot read |
| Doctor's PC | Encrypted pieces awaiting delivery | Deleted the moment UIU confirms a good copy — normally within seconds. Nothing is kept |

### Retention

**Open decision, and it belongs to the ethics committee, not to us.** At 10–16 TB
a year, retention is the single largest cost in the system. We need a stated
period before final storage is purchased. Audit logs are kept indefinitely and are
never edited.

---

## 3. The machine

| | Minimum that works | Recommended |
|---|---|---|
| Cores | 8 | 16 |
| Memory | **8 GB on Linux** | 32–64 GB ECC |
| System / database disk | 1 TB NVMe | 2 TB NVMe, mirrored, power-loss protected |
| Archive array | **2 TB usable** for the study | **4 × 6 TB, RAID 10 — 12 TB usable** |
| Internet | 50 Mbit/s symmetric | 100 Mbit/s symmetric |
| Power | UPS | UPS with automatic clean shutdown |

**The three storage areas must be separate.** Database on NVMe, archive on the
array, working space on its own volume. A large merge must not slow a database
write that a recorder is waiting on.

**Please tell us what disks the 8 GB machine has.** Memory can be worked around
and we have measured exactly how. The archive cannot: if that box has a single
1 TB disk, it can run the system but cannot hold the study, and the array is
needed from day one.

**Why ECC is asked for.** The system's entire purpose is proving recordings were
not altered. Uncorrected memory errors undermine that claim at the source.

**Why power-loss protection on the database disk.** Consumer SSDs acknowledge a
write while it is still in volatile memory and lose it in a power cut — precisely
the failure the integrity chain exists to detect.

---

## 4. Operating system: Ubuntu Server 24.04 LTS — decided

**AIMS LAB has chosen Linux for this server.** The section below explains
why, for the record.

**On an 8 GB machine it is worth about 4 GB of usable memory.** Windows plus
Docker Desktop's virtual machine consume roughly half the box before AIMScribe
starts; on Ubuntu the same hardware gives AIMScribe about 7.5 GB and faster disk
I/O, because containers do not run inside a VM.

It will run on Windows — we have a tested 4 GB configuration — but with nothing
spare. During load testing on Windows, local object storage under memory pressure
failed and **took the whole Docker engine down with it**, not just one container.
That is our strongest single argument for Linux on a small machine.

Day-to-day operation needs no Linux skill: everything is inspected through web
pages from an ordinary Windows laptop. Linux knowledge is needed only to repair
the machine itself, so **we need to know who at CITS supports a Linux server
when hardware or the OS itself fails.**

Required software: Docker Engine with the Compose plugin, and root/sudo on the host.

---

## 5. Network and firewall — what we need changed

### Inbound

| Port | Protocol | Purpose | Source |
|---|---|---|---|
| **443** | TCP **and UDP** | Everything: the recorders, CMED's server-to-server messages, the dashboard. UDP carries HTTP/3 | Internet, or UIU network only if all clinic PCs are on it |
| **80** | TCP | **Certificate renewal only** (ACME). Serves nothing else and redirects to 443 | Internet |
| 22 | TCP | Administration | **Named UIU machines only** |

Nothing else is published. One gateway holds the only open ports; every other
service is reachable from nowhere but inside the machine.

**Port 6000 must not be proposed as an alternative.** Browsers and Node.js both
refuse it outright. The system uses 6060 internally and 443 at the edge.

### Outbound

| Destination | Port | Purpose |
|---|---|---|
| Cloudflare R2 | 443 | Download audio pieces; upload the encrypted copy |
| Cold storage provider | 443 | The lossless second copy |
| NTP servers | 123/UDP | **Mandatory** — see below |
| Docker Hub / ghcr.io | 443 | Container images, at build and upgrade time |

### Five policy points that will break the system if missed

1. **A DNS name, never an IP address, and lower case.** `aimscribe.uiu.ac.bd`,
   with an A record to the server. Certificates are issued for names, and a name
   survives the machine moving. We proved this the hard way: a DHCP lease changed
   overnight and every recorder pointing at the old number went silent. Upload
   links are signed with the host inside them, so a single capital letter refuses
   every upload.

2. **No TLS interception or transparent proxy on the outbound path.** Upload
   authorisation is cryptographically signed over the host and path. An
   intercepting middlebox breaks certificate validation and can invalidate those
   signatures. If UIU requires a proxy, we need an explicit bypass for the
   storage endpoints.

3. **No aggressive idle-connection timeout.** Recorders reuse a connection every
   30 seconds and the server holds them for 75. A firewall that silently drops
   idle connections sooner causes exactly the stall we already found and fixed in
   our own software.

4. **Clock synchronisation is a correctness requirement, not hygiene.** Every
   recording is filed under the *clinic's local date* and matched to CMED's
   clinical record by timestamp. A drifting clock files consultations in the wrong
   day's folder permanently, because the folder is decided once.

5. **A static address**, or a DHCP reservation that is documented as permanent.

### If CITS will not open an inbound port

The design already tolerates this. The archive component opens **no ports at all**
and makes only outbound connections. For the parts that must be reachable, a
**Cloudflare Tunnel or Tailscale** works with no inbound rule whatsoever — the
server dials out. We are happy to take this route if it is easier to approve.

### Bandwidth, concretely

| When | Direction | Rate |
|---|---|---|
| Clinic hours, 08:30–21:00 | Inbound | ~15 Mbit/s sustained |
| Overnight | Outbound | ~20 GB, about an hour at 50 Mbit/s |
| Each clinic site (2 rooms) | Outbound from the clinic | ~1.4 Mbit/s |

---

## 6. Operational policy

- **No automatic reboots or patch windows between 08:00 and 21:30, six days a
  week.** A reboot mid-consultation is survivable — the clinic PCs buffer and
  retry — but it costs recordings we cannot re-take.
- **The archive volume must be exempt from any quota sweep or automatic
  cleanup.** It grows continuously and by design.
- **Disks encrypted at rest**, and the archive readable only by the AIMScribe
  services and named staff.
- **UPS that shuts the machine down cleanly** before its battery ends. A recording
  being written during an unclean power loss can be damaged.
- **Monitoring alerts** on disk space, failed jobs, silent recorders and
  certificate expiry — we provide these; they need somewhere to send mail.

---

## 7. What we do *not* need

Worth stating, because it narrows the approval considerably:

- No inbound access to the database, ever.
- No public file shares, no FTP, no SMB exposure.
- No access to any other UIU system, network or directory.
- No Windows licences.
- No CITS involvement in day-to-day running.
- No credentials held on the archive machine beyond one shared key: it receives
  short-lived, single-purpose URLs, so a compromise there cannot enumerate storage
  or forge an integrity receipt.

---

## 8. How we will prove it works before any patient is recorded

In this order, all scripted:

1. **Channel B check** — CMED's two message types validated against the server's
   own rules.
2. **The load test** — 28 rooms, 30 minutes each, 150 MB per room, delivered at
   once. It must report no piece lost. This is twice our real concurrency.
3. **Per-PC preflight** on each clinic machine, including listening to its
   microphone.
4. **A restore** — take one finished recording, fetch its encrypted cloud copy and
   rebuild it. A copy nobody has ever read is not a backup. Day one, and monthly
   after.

---

## 9. The short version

| We need | Detail |
|---|---|
| A machine | 8 cores, 8 GB **on Ubuntu Server 24.04**, 1 TB NVMe |
| **An archive array** | **2 TB usable minimum; 12 TB strongly preferred** |
| A DNS name | `aimscribe.uiu.ac.bd`, lower case, static address |
| Inbound | 443 TCP+UDP, 80 TCP for certificates, SSH from named machines |
| Outbound | 443 to two storage providers, NTP, container registries |
| No middlebox | No TLS interception, no short idle timeouts |
| Policy | No patch window in clinic hours, no quota sweep on the array, UPS |
| A decision we need from UIU | **How long audio is retained** — it sets the final storage bill |
| A question we need answered | **Who supports the Linux host if the OS or hardware fails?** |

Contact: AIMS LAB, United International University.
