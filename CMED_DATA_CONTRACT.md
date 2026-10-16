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

**Neither message may delay the doctor.** Send them from an outbox and retry on
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
    "doctor_id": "DR0007", "hospital_id": "HOSP003",
    "start_time": "2026-06-02T09:40:05+06:00", "date": "2026-06-02",
    "prescription": { "items": [ … ], "diagnoses": [ … ], "investigations": [ … ],
                      "advice": "…", "follow_up": "2026-06-16", "notes": "…" }
  },

  "female_details": { },
  "male_details":   { }
}
```

**Required:** `demographics` must be an object, on **every** visit — first,
second, tenth. `previous_visit` must be present as a key; **send `null` when
there is no prior visit to give**, rather than omitting it. That distinction is
how we tell "nothing to send" from "CMED forgot".

### `previous_visit` — the history rule

`previous_visit` is a **backfill**, not a repeat. It exists so a prior
consultation we never saw still reaches us, and so a prescription that was
corrected after that visit reaches us in its corrected form.

**Name the earlier visit by its own five fields.** `doctor_id`, `hospital_id`,
`start_time` and `date` are that visit's own, exactly as CMED sent them when it
happened; `patient_id` may be given and must be this patient. With them we find
the visit itself — the live one recorded at the time, or one an earlier API 2
brought — and never file it under today's doctor.

| What we find for that visit | What we store | `previous_visit.status` in the reply |
|---|---|---|
| Held, and its newest prescription has the **same fingerprint** | **Nothing** | `ALREADY_STORED` — that history is held; there is no need to send it again |
| Held, and the prescription **differs** | It, as that visit's **next version**. The newest version is the one read | `UPDATED`, with the `version` |
| Not held | That visit, under its real doctor and clinic | `STORED` |
| `previous_visit` was `null` | — | `NOT_SENT` |
| The body is safe but has not loaded yet | — (the sweep finishes it) | `PENDING` |

The rest of API 2 — today's demographics and measurements — is stored as usual
whatever happens to `previous_visit`, because it is today's visit we need.

**The date-only shape is still accepted**, `{"date": …, "prescription": …}`,
but it names no doctor or time, so we cannot tell which visit it was. If any
visit we hold for that patient that day has the same prescription, nothing is
stored; otherwise it is stored under doctor and clinic `UNKNOWN`. The reply
says `UNIDENTIFIED`, with `"stored": true` or `false`. Send the five fields
instead; this shape exists only so nothing already built breaks.

**Half a name is refused.** `doctor_id`, `hospital_id` and `start_time` are sent
together or not at all; a doctor without a time could file a prescription
against the wrong visit, which is worse than not filing it. A previous visit
that is this visit, or later than it, is also refused. These come back as
`422 SCHEMA_INVALID` with the field named.

#### Ask before sending: what do you already hold?

```
GET /api/v2/clinical/patient/{patient_id}/visits?limit=50&cursor=...
X-CMED-Key: <the same key>
```

```json
{ "status": 200, "code": "OK", "patient_id": "P0012345",
  "visits": [
    { "patient_id": "P0012345", "doctor_id": "DR0007", "hospital_id": "HOSP003",
      "start_time": "2026-06-02T09:40:05+06:00", "date": "2026-06-02",
      "source": "live", "prescription_version": 2,
      "prescription_fingerprint": "f1e71094…ec4806",
      "received_at": "2026-06-02T03:58:11.204+00:00" } ],
  "next_cursor": null }
```

Newest first. `limit` is 1 to 200 (50 if not given); when a page is full,
`next_cursor` is the value to pass as `cursor` for the next page, unchanged.
`before=YYYY-MM-DD` optionally limits the list to visits before that date. Only
identifiers and fingerprints come back — never a name, a medicine or a
diagnosis — and every request is logged against the patient. A patient we
hold nothing for gets an empty list. The errors are those of the other two
endpoints: `401 INVALID_KEY`, `400 INVALID_IDENTIFIER`, and `400 MISSING_FIELD`
naming `limit` or `before`.

**The rule CMED follows on a returning patient:**

1. Ask for the visits.
2. For the previous visit, compute the fingerprint of the prescription CMED
   holds now.
3. Listed with the same fingerprint → send `previous_visit: null`.
   Not listed, or listed with a different fingerprint → send `previous_visit`.

The decision then rests on what we hold, not on CMED's memory of what it sent
— which is wrong after a quarantined message, a refused consultation, or a
database restored from a backup. **If the question cannot be asked** (our
server is down, the request times out), **send `previous_visit` anyway.** It
costs about two kilobytes, and the reply's `ALREADY_STORED` says it was not
needed.

#### The prescription fingerprint

Sixty-four lowercase hex characters: SHA-256 of one exact JSON text built from
what the doctor prescribed, and nothing else.

1. Take these six keys, and only these:

   | Key | Value |
   |---|---|
   | `items` | a list, in the order prescribed; each entry exactly `drug`, `dose`, `frequency`, `duration`, `instructions` |
   | `diagnoses` | a list of strings |
   | `investigations` | a list of strings |
   | `advice` | a string, or `null` |
   | `follow_up` | `YYYY-MM-DD`, or `null` |
   | `notes` | a string, or `null` |

2. Every text value has the spaces at its ends removed. An empty string, or a
   text key that was not sent, is `null`; a list that was not sent is `[]`. An item with no
   `drug` is left out. A diagnosis or investigation sent as an object counts
   as its `text`, or else its `name`.
3. Write it as JSON with the **keys sorted** at every level, **no spaces**
   (separators `,` and `:`), and characters such as Bangla written as
   themselves, not as `\u` escapes. Encode as UTF-8 and hash with SHA-256.

`issued_at`, the order of the keys, spacing, and any field not in the table
make no difference — a prescription sent again is recognised as the same one.
A change to any medicine, its order, a diagnosis, the advice, the follow-up or
the notes is a different fingerprint.

**Worked example.** This prescription:

```json
{ "issued_at": "2026-10-01T10:20:00+06:00",
  "items": [ { "drug": "Paracetamol", "dose": "500 mg",
               "frequency": "1+1+1", "duration": "5 days" } ],
  "diagnoses": ["Fever"], "investigations": ["CBC"],
  "advice": "Rest and fluids", "follow_up": "2026-10-08" }
```

becomes exactly this text (one line):

```
{"advice":"Rest and fluids","diagnoses":["Fever"],"follow_up":"2026-10-08","investigations":["CBC"],"items":[{"dose":"500 mg","drug":"Paracetamol","duration":"5 days","frequency":"1+1+1","instructions":null}],"notes":null}
```

whose fingerprint is

```
f1e7109404fd5d4010c358f0b3afdec79699955299a089eaf2c830fb60ec4806
```

CMED's implementation should reproduce that hex before it is used; our test
suite pins the same value (`backend/tests/test_previous_visit.py`).

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
both versions. Only a real change is a new version: the same prescription with
a new `issued_at`, or with its keys in another order, has the same fingerprint
(see §3) and is answered `200 ALREADY_RECEIVED` with the version already held.

**An old message replayed never rolls a visit back.** If an outbox sends
version 1 again, byte for byte, after version 2 was stored — the usual thing
after an outage — it is answered `ALREADY_RECEIVED` for version 1 and version 2
stays the newest. A doctor who really goes back to the first prescription
produces a new `issued_at`, and that is stored as version 3.

**A late message is never too late.** API 2 and API 3 may arrive hours or days
after the consultation, after its recording was closed or archived. They are
stored, tied to the recording, and the JSON file beside it is written again.
They add to the recording; whether they arrive does not decide whether the
recording is kept.

---

## 5. Where it goes

| What you send | Where it lands |
|---|---|
| The whole body, unchanged | `intake_records` — once per body, always |
| `demographics` | `patients`, `encounter_demographics` |
| `paramedic` | `paramedic_observations` — known fields in columns, the rest in `other` |
| `previous_visit` | The earlier visit's own `encounters` row — the one already held, or a new one under its real doctor and clinic — with its prescription as a new version only if it changed. The date-only shape is filed under `UNKNOWN`, at midnight on its date |
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
| `202 ACCEPTED` | Stored | Remove it from the outbox. For API 2, read `previous_visit.status` (§3) |
| `200 ALREADY_RECEIVED` | Already held: this exact body, or (API 3) a prescription with the same fingerprint as the newest version | **Remove it from the outbox and never send it again.** Retrying was safe |
| `400 MALFORMED_JSON` | Not a JSON object | Fix and resend; do not retry unchanged |
| `400 MISSING_FIELD` | A required field is missing or malformed | Fix and resend |
| `400 INVALID_IDENTIFIER` | An id has characters outside `A-Z a-z 0-9 _ -` | Fix and resend |
| `401 INVALID_KEY` | Key missing or wrong | Stop and tell us |
| `413 TOO_LARGE` | Over 1 MB | Split or trim; a body this large is usually a mistake |
| `422 SCHEMA_INVALID` | **Stored in quarantine**, with the fields listed | Not lost. Fix the shape and resend |
| `429`, `5xx`, a timeout, no connection | Busy, a problem on our side, or no internet | **Keep it in the outbox** and retry with growing delay. Never drop it |

**Send from an outbox.** Each Aalo branch keeps working on its local CMED server
through an internet or power cut, and while it is cut off neither message can
reach us. Write each one to a table on that server at the moment of the click,
send in order (a visit's API 2 before its API 3), retry while we cannot be
reached, and remove a message only on `202` or `200`. A message that arrives a
day late attaches to its recording; a message never sent is the only failure.

**Retries are safe.** The same body twice produces one row, and the reply says
`ALREADY_RECEIVED` rather than failing. For API 2 the reply also carries
`"previous_visit": {"status": …, "version": …, "record_id": …}` (§3); on a
repeat of a body that stored it, the status is `ALREADY_STORED`.

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
