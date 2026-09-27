# What CMED sends AIMS LAB, field by field

The contract for Channel B — CMED's server to the AIMS LAB server. Every rule
below is the one the server actually applies (`backend/src/clinical.py`,
`backend/src/clinical_model.py`), not a restatement of the SRS.

Audio never reaches CMED, and nothing clinical is ever sent back.

---

## 1. The two messages

| | API 2 — patient information | API 3 — prescription |
|---|---|---|
| Endpoint | `POST /api/v2/clinical/patient-information` | `POST /api/v2/clinical/prescription` |
| Sent when | The patient is opened, at the same moment as the recorder trigger | The doctor builds the prescription |
| Header | `X-CMED-Key: <the key AIMS LAB issued>` | same |
| Body limit | 1 MB | 1 MB |
| Success | `202 ACCEPTED` — stored before the reply is sent | same |

Both are **server to server**. The key must never reach a browser.

**Neither message may delay the doctor.** Send them from a queue and retry on
failure; a recording is never held up by a clinical message, and a late message
is reconciled afterwards.

---

## 2. The five fields — identical in every message

Every API 1, API 2 and API 3 for one visit must carry **exactly** these, with
identical values. They are what ties a recording to its clinical record.

```json
{
  "patient_id":  "P0012345",
  "doctor_id":   "DR0042",
  "hospital_id": "HOSP003",
  "start_time":  "2026-09-13T10:14:32+06:00",
  "date":        "2026-09-13"
}
```

| Field | Rule |
|---|---|
| `patient_id`, `doctor_id`, `hospital_id` | `^[A-Za-z0-9_-]{1,64}$`. They become folder names, so nothing else is accepted |
| `start_time` | ISO 8601 **with a time zone offset**. `+06:00`, not `Z`-less local time |
| `date` | `YYYY-MM-DD`, the clinic's local date |

**`start_time` must be the moment the patient was opened, and must not change.**
API 3 carries the *original* `start_time`, not the time the prescription was
built. It is how the prescription finds its consultation.

---

## 3. API 2 — patient information

```json
{
  "patient_id": "P0012345", "doctor_id": "DR0042", "hospital_id": "HOSP003",
  "start_time": "2026-09-13T10:14:32+06:00", "date": "2026-09-13",

  "demographics": {
    "name": "…", "sex": "female", "age_years": 34,
    "date_of_birth": "1992-03-11", "phone": "01…", "address": "…"
  },

  "paramedic": {
    "recorded_at": "2026-09-13T10:02:10+06:00",
    "weight_kg": 58.4, "height_cm": 157, "blood_pressure": "120/80",
    "pulse_bpm": 78, "temperature_c": 36.8, "spo2_percent": 98,
    "notes": "…"
  },

  "previous_visit": {
    "date": "2026-06-02",
    "prescription": { "items": [ … ], "diagnoses": [ … ], "notes": "…" }
  },

  "female_details": { },
  "male_details":   { }
}
```

**Required:** `demographics` must be an object. `previous_visit` must be
present — **send `null` for a first visit**, do not omit the key. That
distinction is how we tell "first visit" from "CMED forgot".

### Accepted values

| Field | Type | Accepted | Outside that |
|---|---|---|---|
| `name` | text | up to 200 characters | truncated |
| `sex` | text | `female`, `male` | stored as `unknown` |
| `age_years` | number | 0 – 130 | `null` |
| `date_of_birth` | date | `YYYY-MM-DD` | `null` |
| `phone` | text | up to 40 | truncated |
| `address` | text | up to 500 | truncated |
| `recorded_at` | timestamp | ISO 8601 **with offset** | `null` |
| `weight_kg` | number | 0.5 – 499 | `null` |
| `height_cm` | number | 20 – 299 | `null` |
| `blood_pressure` | text | `"120/80"` — systolic 30–300, diastolic 10–250 | kept as text, split columns `null` |
| `pulse_bpm` | whole | 10 – 300 | `null` |
| `temperature_c` | number | 25 – 45 | `null` |
| `spo2_percent` | whole | 0 – 100 | `null` |

**A value we cannot read becomes `null`; it never rejects the message.** One
mistyped temperature must not stop a prescription being stored.

**Anything else inside `paramedic` is kept**, in an `other` object. You do not
need our permission to add a measurement — send it, and it is stored. The same
is true of `female_details` and `male_details`, which are stored as sent.

**The whole body is stored exactly as received**, before any of this is read. No
field is ever guessed, and nothing is discarded because we did not recognise it.

---

## 4. API 3 — the prescription

```json
{
  "patient_id": "P0012345", "doctor_id": "DR0042", "hospital_id": "HOSP003",
  "start_time": "2026-09-13T10:14:32+06:00", "date": "2026-09-13",

  "issued_at": "2026-09-13T10:26:55+06:00",
  "items": [
    { "drug": "Metformin 500 mg", "dose": "1 tablet",
      "frequency": "twice daily", "duration": "30 days",
      "instructions": "after meals" }
  ],
  "diagnoses":      ["Type 2 diabetes mellitus"],
  "investigations": ["HbA1c"],
  "advice": "…", "follow_up": "2026-10-13", "notes": "…"
}
```

**Required:** `issued_at` (a timestamp **with its offset**), and `items`,
`diagnoses` and `investigations` as **lists — empty lists if there are none**.
An empty list means "the doctor ordered none"; a missing key means "CMED did not
send it", and those are different facts.

| Field | Rule |
|---|---|
| `items[].drug` | **Required per entry.** An entry with no `drug` is skipped |
| `items[].dose`, `.frequency`, `.duration` | text, up to 200 |
| `items[].instructions` | text, up to 1000 |
| `diagnoses`, `investigations` | list of strings, each up to 500. A bare string is accepted as a one-entry list. Objects are accepted if they carry `text` or `name` |
| `follow_up` | `YYYY-MM-DD` |
| `advice`, `notes` | text, up to 2000 |

Items keep the order you send them; that order is stored as the line number.

**Sending a corrected prescription is expected.** Send it again with the same
five fields; the file beside the recording is rewritten and the database keeps
both versions.

---

## 5. Where it goes

| What you send | Where it lands |
|---|---|
| The whole body, unchanged | `intake_records` — once per body, always |
| `demographics` | `patients`, `encounter_demographics` |
| `paramedic` | `paramedic_observations` — known fields in columns, the rest in `other` |
| `previous_visit` | A second `encounters` row, filed at midnight on its date so it always sorts before the live visit |
| `items` | `prescription_items`, one row per medicine |
| `diagnoses`, `investigations` | `diagnoses`, `investigations` |
| `female_details`, `male_details` | `female_details`, `male_details` |
| Everything | A copy as `<recording name>.json`, in the same folder as the audio |

The JSON file travels with the audio; the database is the index. Neither is
built from the other.

---

## 6. Replies, and what to do about each

| Code | Meaning | What CMED should do |
|---|---|---|
| `202 ACCEPTED` | Stored | Nothing |
| `202 ALREADY_RECEIVED` | This exact body was already stored | Nothing — **retrying is safe** |
| `400 MALFORMED_JSON` | Not a JSON object | Fix and resend; do not retry unchanged |
| `400 MISSING_FIELD` | A required field is missing or malformed | Fix and resend |
| `400 INVALID_IDENTIFIER` | An id has characters outside `A-Z a-z 0-9 _ -` | Fix and resend |
| `401 INVALID_KEY` | Key missing or wrong | Stop and tell us |
| `413 TOO_LARGE` | Over 1 MB | Split or trim; a body this large is usually a mistake |
| `422 SCHEMA_INVALID` | **Stored in quarantine**, with the fields listed | Not lost. Fix the shape and resend |

**Retries are safe.** The same body twice produces one row, and the reply says
`ALREADY_RECEIVED` rather than failing.

---

## 7. What must not be sent

- **No audio, ever**, in either direction.
- **No consent field.** A patient's refusal is recorded on the recorder's Stop
  button and erases the recording everywhere; CMED sends nothing about it.
- **Nothing extra in the five fields.** They are matched exactly.
- **Not from a browser.** The key is a server credential.

---

## 8. Testing before going live

Both halves are already built:

```
python tools/channel_b_test.py --local            # our rules, nothing running
python tools/channel_b_test.py --server https://<host> --key <CMED key>
```

The second runs every case that matters — a good message, the same message
twice, a broken one, a changed prescription, a wrong key, an oversized body —
and checks each reply against this document. CMED's test page at
`/protocol-test` shows every message and reply side by side.

Please run it against a test server and send us the output before the first
clinic goes live.
