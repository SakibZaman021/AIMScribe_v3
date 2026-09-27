# Integrating CMED with AIMScribe

For the Amader Susastho and Aalo EHR teams. It says what CMED builds, what AIMS
LAB provides, and which addresses each side talks to.

The short version: **CMED never handles audio, never enrols a laptop, and never
issues permission to record.** CMED sends two signals from the browser and two
messages from its server. Everything else is ours.

---

## 1. Three surfaces, and who owns each

| Surface | Between | Who builds it |
|---|---|---|
| **Enrolment** | AIMS LAB server ↔ the laptop | **AIMS LAB alone. CMED builds nothing.** |
| **Channel A** | CMED's page ↔ the recorder on the same PC | **CMED** (browser) |
| **Channel B** | CMED's server ↔ AIMS LAB server | **CMED** (server) |

They are independent. A laptop is enrolled once, months before CMED's page ever
opens on it.

---

## 2. Enrolment — what it is, and why CMED is not in it

An enrolment token binds **one laptop** to **one clinic**, once. It is how the
server knows that a recording claiming to be from Dholpur really came from a
Dholpur machine.

```
1. An AIMS LAB administrator mints a token
      POST /api/v2/admin/enrollment-token      (X-Admin-Key)
      → a single-use, short-lived token for one hospital_id

2. The token is installed with the agent
      install.ps1 -EnrollmentToken <token>
      → written to %PROGRAMDATA%\AIMScribe\state\enrollment.token

3. The agent enrols itself on first start
      POST /api/v2/device/enroll  { token, device_pubkey, machine info }
      → { device_id, hospital_id }
      → writes device.json, deletes the token

4. Every start after that reads device.json. Nothing to do again.
```

**A doctor never sees this, and CMED never touches it.** There is no screen, no
activation code to type, no CMED involvement at any step.

**Why it exists.** In v1 the browser typed its own hospital into a text box, and
the archive tree was built from whatever it said. Now the clinic comes from the
machine's enrolment. If CMED's `hospital_id` does not match the laptop's, the
recording is **refused**, not warned about.

The device also registers an Ed25519 public key here. That is what lets the
server prove every integrity-chain entry was signed by this machine.

**One token, one PC, once.** Fourteen rooms means fourteen tokens. A token that
is used, expired, or already spent is refused.

---

## 3. Channel A — CMED's page to the recorder

The recorder runs on the same PC as the browser and listens on loopback only.

```
ws://127.0.0.1:5050/ws
```

### How the page is authorised

Not with a key. Three checks, all of them things a browser cannot forge:

| Check | Rule |
|---|---|
| `Origin` header | Must appear **exactly** in `AIMS_ALLOWED_ORIGINS`. No wildcards, no `null` |
| `Host` header | Must appear in `AIMS_ALLOWED_HOSTS` — this blocks DNS rebinding |
| Peer address | Must be `127.0.0.1` or `::1`. A remote machine cannot connect at all |

**So the only thing CMED must give AIMS LAB for Channel A is the exact origin
its page is served from** — for example `https://ehr.aaloclinic.com`. Every
origin, including staging, must be listed; a preview URL that is not on the list
is refused.

The HTTP routes beside the WebSocket (`/health`, `/api/v1/session/status`,
`/api/v1/doctors`) use an `X-API-Key` header instead, with a key generated per
install. Starting a recording is **only** possible over the WebSocket.

### The two commands CMED sends

Only these two are accepted from the page:

```jsonc
// 1. The patient is opened. The microphone starts now.
{
  "command": "start",
  "request_id": "<any string, echoed back>",
  "trigger": {
    "patient_id":  "P0012345",
    "doctor_id":   "DR0042",
    "hospital_id": "AALO_DHOLPUR",
    "start_time":  "2026-09-13T10:14:32+06:00",   // with time zone
    "date":        "2026-09-13"
  }
}

// 2. The doctor pressed Build Prescription.
{
  "command": "prescription_built",
  "request_id": "...",
  "patient_id": "P0012345",
  "session_id": "<the id returned by start>"
}
```

**`prescription_built` does not stop the recording.** The doctor prints the
prescription, hands it over, then counsels the patient for another minute or
two — and that counselling is exactly the part worth recording. The signal
*arms the gate*: it allows the **next** patient's `start` to close this session.
Recording ends when the next patient is opened.

Every reply carries `request_id`, `status` and `code`. **Act on the code, never
the message** — the wording changes without notice.

| Code | Meaning |
|---|---|
| `RECORDING_STARTED` | Recording, and the server has authorised it |
| `RECORDING_PROVISIONAL` | Recording. Permission still being checked — this is normal |
| `GATE_ARMED` | The prescription signal was accepted |
| `GATE_ALREADY_ARMED` | Sent twice; harmless |
| `PATIENT_MISMATCH` | The signal names a different patient than the open session |
| `CLINIC_MISMATCH` | `hospital_id` is not this laptop's clinic. **Refused** |
| `MISSING_FIELD` | A field is absent or malformed |

`stop`, `pause` and `resume` exist on the socket but are **not CMED's to send** —
they belong to the on-screen control the doctor uses.

---

## 4. Channel B — CMED's server to the AIMS LAB server

Two POSTs, server to server, authenticated with `X-CMED-Key`:

```
POST https://<aims-lab-host>/api/v2/clinical/patient-information   (API 2)
POST https://<aims-lab-host>/api/v2/clinical/prescription          (API 3)
```

Every field, every accepted range and every reply code is in
[`CMED_DATA_CONTRACT.md`](CMED_DATA_CONTRACT.md). Read that before writing code.

**The key is a server credential and must never reach a browser.**

**Neither message may delay the doctor.** Send them from a queue and retry;
retries are safe and produce one row, never two.

---

## 5. What each side hands over

### AIMS LAB gives CMED

| Item | Where it comes from |
|---|---|
| The AIMS LAB server URL | e.g. `https://aimscribe.uiu.ac.bd` |
| The **CMED Channel B key** | `POST /api/v2/admin/cmed-key` — shown **once** |
| The recorder's port and path | `ws://127.0.0.1:5050/ws` — the same on every PC |
| The **local API key** for the HTTP routes | Generated per install, in that PC's `.env` |
| The clinic identifiers | `AALO_KARAIL`, `AALO_MIRPUR`, `AALO_DHOLPUR`, `AALO_SHYAMPUR`, `AALO_NARAYANGANJ`, `AALO_ERSHADNAGAR`, `AMADER_SUSASTHO` |
| This guide and the data contract | |

### CMED gives AIMS LAB

| Item | Why |
|---|---|
| **The exact origin(s) of its page** | Goes into `AIMS_ALLOWED_ORIGINS`. Without it the socket refuses to open |
| **Its own clinic identifiers** | Mapped to ours as `cmed_hospital_id`. An unmapped clinic is **not** refused — worse, it is silently accepted: the clinical record is stored, CMED gets `202 ACCEPTED`, but no confirmation is written and the **recording is erased after 24 hours** |
| A server IP or range, if we are to allow-list it | Optional, for Channel B |

---

## 6. The current settings

Bench values on the AIMS LAB laptop. The UIU server gets its own.

| Setting | Value |
|---|---|
| AIMS LAB server | `http://localhost:6060` (bench) → `https://aimscribe.uiu.ac.bd` |
| Recorder socket | `ws://127.0.0.1:5050/ws` |
| `AIMS_ALLOWED_ORIGINS` | must list CMED's exact origins — **currently only the bench page** |
| Clinics registered | `AALO_KARAIL` · `AALO_MIRPUR` · `AALO_DHOLPUR` · `AALO_SHYAMPUR` · `AALO_NARAYANGANJ` · `AALO_ERSHADNAGAR` · `AMADER_SUSASTHO` |
| `cmed_hospital_id` | Each clinic is mapped to **its own code**, as a placeholder. If CMED uses different identifiers, send them and we remap |

**Not port 6000.** Browsers and Node both refuse it. The server uses 6060
internally and 443 at the edge.

---

## 7. The order to build in

1. **AIMS LAB** maps CMED's clinic identifiers — nothing works until the clinic
   a recording claims can be resolved to a laptop's clinic.
2. **CMED** adds its origins to our allow-list and confirms the socket opens:
   the recorder sends a status event the moment it accepts.
3. **CMED** implements `start` and `prescription_built`, acting on reply codes.
4. **CMED** implements the two Channel B posts and runs the test harness we
   already ship:

   ```
   python tools/channel_b_test.py --local
   python tools/channel_b_test.py --server https://<host> --key <CMED key>
   ```

   It exercises a good message, the same message twice, a broken one, a changed
   prescription, a wrong key and an oversized body, and checks every reply
   against the contract. CMED's own test page at `/protocol-test` shows each
   message and reply side by side.
5. **Both** record one consultation end to end before a patient is in the room.
