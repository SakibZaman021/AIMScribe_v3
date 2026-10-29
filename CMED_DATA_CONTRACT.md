# What CMED sends AIMS LAB, field by field

The contract for Channel B — CMED's server to the AIMS LAB server. Every rule
below is the one the server actually applies (`backend/src/clinical.py`,
`backend/src/clinical_model.py`, and for the FHIR form `backend/src/fhir.py`),
not a restatement of the SRS.

Audio never reaches CMED, and nothing clinical is ever sent back.

---

## 1. The two messages

| | API 2 — patient information | API 3 — prescription |
|---|---|---|
| Endpoint | `POST /fhir/r4/$process-message`, event `patient-information` | same, event `prescription` |
| Sent when | The patient is opened, at the same moment as the recorder trigger (API 1, the doctor's browser to AIMScribe.exe) | The doctor builds the prescription |
| Carries | **What the paramedic collected**: sex, date of birth or age, health screening. And the **previous prescription, only if we do not hold it** (§3) | **The whole consultation**: the doctor's findings (chief complaints, history, allergies, O/E, S/E, G/H and O/H) and the prescription (investigations, diagnosis, medicines, advice, next visit) |
| Header | `Authorization: Bearer <that hospital's 5-minute token>` (§1a) | same |
| Body limit | 1 MB | 1 MB |
| Success | HTTP `200`, code `ACCEPTED` in `Parameters` — stored before the reply is sent (interim JSON form: `202 ACCEPTED`) | same |

Both are **server to server**. The private key and the token must never reach a
browser. The bodies in §2–§4 are the logical content: the FHIR message carries
exactly them (§1a).

**Neither message may delay the doctor.** Send them from an outbox and retry on
failure; a recording is never held up by a clinical message, and a late message
is reconciled afterwards.

**Only the five fields (§2) can refuse a message.** Everything else is
accepted however it is written - the body is stored exactly as received - and
read the way clinics write it (§3, Accepted values). Anything converted, read
in another form, or not usable comes back in the reply as `warnings`, so CMED
can correct it at source while nothing waits. A refused API 2 would leave its
recording unconfirmed, and an unconfirmed recording is erased after its grace
period; one odd value must never cost a consultation.

---

## 1a. Who may send — one client per hospital, and FHIR R4

CMED runs the software for many hospitals, so **each hospital has its own
identity here**, not one key for all of them (HL7 SMART Backend Services:
OAuth 2.0 client credentials with a signed JWT assertion, RFC 7523).
Implemented in `backend/src/cmed_auth.py` and `backend/src/fhir.py`.

1. **CMED makes a key pair per hospital** (ES384 recommended; ES256 and RS384
   with 2048+ bits also accepted) and sends AIMS LAB only the **public** key as
   a JWK. The private key never leaves CMED's server; ours refuses a private key.
2. **AIMS LAB registers it** for that hospital's CMED id
   (`POST /api/v2/admin/cmed-client`), optionally with CMED's server addresses,
   and returns the `client_id` and `kid`. A client lives up to a year; we renew
   it in one call (`POST /api/v2/admin/cmed-client/{id}/renew`) and are alerted
   30 days before it expires. Revoking a key also ends every live token it
   issued.
3. **To send, CMED signs an assertion** — `iss` = `sub` = client id,
   `aud` = `https://<aims-host>/api/v2/oauth/token`, a fresh `jti` (8–256
   characters, accepted once), `exp` at most 5 minutes ahead, header `kid` — and
   posts it to `POST /api/v2/oauth/token` with
   `grant_type=client_credentials`,
   `client_assertion_type=urn:ietf:params:oauth:client-assertion-type:jwt-bearer`
   and `scope=system/Bundle.c system/Encounter.rs` (SMART v2; the earlier
   `system/clinical.write`, `system/Encounter.read` and SMART v1 spellings are
   still accepted). **CMED's server clock must be on NTP**: we allow ±60 s, and
   a clock outside that gets `400 invalid_client` "clock skew", never a lockout.
4. **It receives a 5-minute access token** for that hospital only
   (`"hospital_ids": [...]` in the reply). Reuse it until ~30 s before expiry.
5. **Every call carries `Authorization: Bearer <token>`.** A message whose
   `hospital_id` the token does not speak for is refused with
   `403 HOSPITAL_NOT_ALLOWED` and raises an alert here.

| Endpoint | Purpose |
|---|---|
| `POST /api/v2/oauth/token` | the token (form-encoded, signed assertion) |
| `POST /fhir/r4/$process-message` | API 2 and API 3 as a FHIR message Bundle, `application/fhir+json` |
| `GET /fhir/r4/Encounter?patient.identifier=<id>&_count=50` | the visits held (§3), as a searchset Bundle |
| `GET /fhir/r4/Encounter/<id>` | one visit, only if it is at a hospital the token speaks for |
| `GET /fhir/r4/metadata`, `GET /fhir/r4/.well-known/smart-configuration` | CapabilityStatement and discovery; no token needed |
| `GET /fhir/r4/MessageDefinition/aimscribe-patient-information` (and `aimscribe-prescription`) | the two message definitions; no token needed |

**The message.** A Bundle of `"type": "message"` whose first entry is a
MessageHeader with `eventCoding.code` `patient-information` (API 2) or
`prescription` (API 3), and `focus` → this visit's Encounter. The five fields:
`Encounter.subject` → Patient.identifier (patient_id),
`Encounter.participant.individual` → Practitioner.identifier (doctor_id),
`Encounter.serviceProvider` → Organization.identifier (hospital_id),
`Encounter.period.start` (start_time, with offset; `date` is its clinic-local
date). Any spelling of the same moment matches — `04:14:32.000Z` and
`10:14:32+06:00` are one visit; we store clinic time, whole seconds, `+06:00`.
Identifier systems: we **propose** `urn:aimscribe:cmed:patient`,
`urn:aimscribe:cmed:practitioner`, `urn:aimscribe:cmed:hospital`, and would
rather register CMED's own per hospital. A resource may carry several
identifiers; we take, in order, the one with that hospital's registered system,
the one with the proposed system, the one marked `use: official`, or the only
one. Several with none of these is refused, naming the element.
Demographics come from Patient; paramedic values from LOINC Observations
(29463-7 weight, 8302-2 height, 85354-9 with 8480-6/8462-4 blood pressure,
8867-4 pulse, 8310-5 temperature, 59408-5 SpO2, 30525-0 age); a second
Encounter is `previous_visit`; MedicationRequest → `items`, Condition →
`diagnoses`, ServiceRequest → `investigations`, CarePlan → `advice`
(description), `follow_up` (period.start), `notes` (note). The server turns the
Bundle into **exactly the body described in §2–§4 below** and applies the same
rules. The full mapping, complete example messages and replies are in
CMED_1_SOFTWARE_INTEGRATION.pdf, Part H; working clients are
`tools/cmed_fhir_client.py` (Python) and `cmed-web/src/lib/cmed-fhir.ts`
(TypeScript).

**The reply** is a FHIR response message: MessageHeader (`response.code` `ok`
or `fatal-error`), an OperationOutcome, and a Parameters resource carrying
`code`, `record_id`, `version`, `previous_visit` and `received_at` — the same
facts as the JSON replies in §6. A message that cannot be read or is not
authorised gets an OperationOutcome alone, the code in
`issue[0].details.coding[0].code` and the element in `issue[0].expression`.

**The interim static key.** `X-CMED-Key` with plain JSON to the
`/api/v2/clinical/...` endpoints still works during the switch-over, but only
with a key issued for named hospitals (expiring after at most 90 days); a key
naming none gets `403 KEY_NOT_BOUND`. AIMS LAB switches static keys off
(`AIMS_CMED_LEGACY_KEYS=off`) once CMED is on tokens. **Move to tokens and FHIR
before the first clinic goes live.**

---

## 2. The five fields — identical in every message

**Why API 2 and API 3 carry them, when API 1 already did.** API 1 starts the
audio on the doctor's PC, and the recording reaches us labelled with these five
values. API 2 and API 3 come separately, from CMED's server, minutes or hours
later. The five values are the only link between them — a label, not extra
data. Without them a prescription could not be joined to its consultation, and
a recording could not be confirmed (we keep only recordings CMED's server
confirms).

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
    "sex": "female", "age_years": 34, "date_of_birth": "1992-03-11"
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

**Not sent: the patient's name, phone or address.** Sex and date of birth (or
age) are all the research needs of who the patient is. If they are sent anyway
they are removed before anything is stored, and the reply says so.
**Not sent in API 2: anything the doctor records** — it comes with the
prescription, in API 3 (§4).

**Expected:** `demographics` as an object, on **every** visit — first, second,
tenth — and `previous_visit` present as a key: **send `null` when there is no
prior visit to give**, rather than omitting it. That distinction is how we tell
"nothing to send" from "CMED forgot". Either one missing is accepted with a
warning; today's visit is stored without it.

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

**History from another hospital may fill a gap, never change that hospital's
record.** If the earlier visit was at another CMED hospital and we already hold
it with a different prescription, nothing is stored and the reply says
`NOT_PERMITTED`; only that hospital's own client can correct it.

The rest of API 2 — today's demographics and measurements — is stored as usual
whatever happens to `previous_visit`, because it is today's visit we need.

**The date-only shape is still accepted**, `{"date": …, "prescription": …}`,
but it names no doctor or time, so we cannot tell which visit it was. If any
visit we hold for that patient that day has the same prescription, nothing is
stored; otherwise it is stored under doctor and clinic `UNKNOWN`. The reply
says `UNIDENTIFIED`, with `"stored": true` or `false`. Send the five fields
instead; this shape exists only so nothing already built breaks.

**Half a name is never used.** `doctor_id`, `hospital_id` and `start_time` are
sent together or not at all; a doctor without a time could file a prescription
against the wrong visit, which is worse than not filing it. With half a name
the previous visit is kept by its date alone (as the date-only shape is). A
previous visit that cannot be used at all — another patient, this visit or a
later one, no date, no prescription — is set aside, with
`previous_visit.status` `NOT_USED` and the reason. **In every case today's
API 2 is accepted**: it is what confirms the recording.

#### Ask before sending: what do you already hold?

```
GET /fhir/r4/Encounter?patient.identifier={patient_id}&_count=50
Authorization: Bearer <token>
```

Returned as a FHIR searchset Bundle of Encounters (prescription version and
fingerprint in extensions; the `next` link pages on). **Only visits at the
hospital(s) the token speaks for are listed.** The same content, in the JSON
form of the switch-over endpoint
`GET /api/v2/clinical/patient/{patient_id}/visits?limit=50&cursor=...`:

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

| Field | Preferred | Also read (and converted) | Not understood |
|---|---|---|---|
| `sex` | `female`, `male` | `F`/`M`, any case, `woman`/`man`, `মহিলা`/`নারী`/`পুরুষ` | `unknown`, warning |
| `age_years` | number 0–130 | `"34 years"`, `"34Y"`, `"6 months"` (0), `"2y 3m"` (2), Bangla digits | empty, warning |
| `date_of_birth` | `YYYY-MM-DD` | `11/03/1992` (day first), `11-03-1992`, `11 Mar 1992` | empty, warning |
| `recorded_at` | ISO 8601 with offset | without offset: clinic time (+06:00) | empty |
| `weight_kg` | kg, 0.5–499 | `"58 kg"`, `"128 lb"`, grams | empty, warning |
| `height_cm` | cm, 20–299 | `"5'2\""`, `"5 ft 2 in"`, `"1.57 m"`, `1.57` (metres) | empty, warning — `5.2` is **not** guessed |
| `blood_pressure` | `"120/80"` | `"120/80 mmHg"`, `"120 / 80"`, `"120-80"` | kept as text, warning |
| `pulse_bpm` | 10–300 | `"78 bpm"`, `"78/min"` | empty, warning |
| `temperature_c` | °C, 25–45 | `"98.6 F"`, and 98.6 with no unit (86–113 can only be °F) | empty, warning |
| `spo2_percent` | 0–100 | `"98%"`, `0.98` | empty, warning |

Bangla (`০–৯`) and Arabic-Indic digits are read as 0–9 everywhere.

**A value we cannot read never rejects the message.** It is left out of its
column — never guessed — and kept beside it (paramedic `other`), as well as in
the stored body; the reply's `warnings` names it. One mistyped temperature must
not stop a prescription, or a recording, being kept.

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
  "advice": "…", "follow_up": "2026-10-13", "notes": "…",

  "chief_complaints": [ { "complaint": "Fever", "severity": "moderate",
                          "duration": "3 days" } ],
  "history":   { "family": "Diabetes (mother)", "personal": "Non-smoker",
                 "past": "…", "drug": "…" },
  "allergies": { "food": "…", "medicine": "Penicillin", "pollen": "…",
                 "dust": "…", "mites": "…" },
  "on_examination":       { "anaemia": "absent", "jaundice": "absent",
                            "dehydration": "mild", "oedema": "absent",
                            "cyanosis": "absent", "lymph_nodes": "not palpable" },
  "systemic_examination": { "heart": "S1 S2 normal", "lungs": "clear",
                            "abdomen": "soft" },
  "gynae_obstetric":      { "obstetric": "G2 P2", "menstrual": "regular" }
}
```

**The doctor's findings** travel with the prescription: chief complaints (each
with any detail, such as severity and duration), history, allergies,
on-examination, systemic examination, and gynaecological and obstetric history.
Each is stored as one row of `consultation_findings` (section, item, value,
detail), so every finding can be counted. In FHIR each is one Observation whose
category (system `urn:aimscribe:fhir:finding`) names the section —
`chief-complaint`, `history`, `allergy`, `on-examination`,
`systemic-examination`, `gynae-obstetric` — with `code.text` the item and
`valueString` the finding; allergies may also be `AllergyIntolerance`. A
corrected finding makes the next version, like any other correction. The
prescription fingerprint (§3) is unchanged: it covers the six keys only.

**Expected:** `issued_at` (a timestamp with its offset; without one it is
read as clinic time), and `items`, `diagnoses` and `investigations` as **lists —
empty lists if there are none**. An empty list means "the doctor ordered none";
a missing key means "CMED did not send it", and those are different facts.
Either way the prescription is accepted; a missing one is a warning.

| Field | Rule |
|---|---|
| `items[].drug` | The medicine's name. Also read from `medicine`, `medicine_name`, `name`. A line with no name is left out — the rest is stored — and named in a warning |
| `items[].dose`, `.frequency`, `.duration` | text, up to 200 (also `dosage`; `days`) |
| `items[].instructions` | text, up to 1000 (also `instruction`) |
| `items` | a list; one object, or a medicine name alone, is read as a list of one |
| `diagnoses`, `investigations` | list of strings, each up to 500. A bare string is a one-entry list. Objects are accepted if they carry `text` or `name` |
| `follow_up` | `YYYY-MM-DD`. Also `20/09/2026` (day first), `20 Sep 2026`, and **`"7 days"`, `"2 weeks"`, `"1 month"`, `"৭ দিন পর"`** — counted from the visit date. Anything else: empty, warning |
| `advice`, `notes` | text, up to 2000 |

Items keep the order you send them; that order is stored as the line number.

**Sending a corrected prescription is expected.** Send it again with the same
five fields; the file beside the recording is rewritten and the database keeps
both versions. Only a real change is a new version: the same prescription with
a new `issued_at`, or with its keys in another order, has the same fingerprint
(see §3) and is answered `200 ALREADY_RECEIVED` with the version already held.

**An old message replayed never rolls a visit back.** A prescription whose
content this visit already received, in any version, is answered
`200 ALREADY_RECEIVED` with that version's number, whatever its `issued_at`;
the newest version stays the one shown. So A, then B (version 2), then A again
is `ALREADY_RECEIVED` for version 1, and the visit keeps B. Only content never
sent before for the visit becomes a new version, and its `202 ACCEPTED` reply
lists `changed_sections`: the fields that differ from the version shown before
(for example `["advice", "drug_orders"]`).

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
| `demographics` | `patients`, `encounter_demographics` — sex, date of birth, age; never a name, phone or address |
| `paramedic` | `paramedic_observations` — known fields in columns, the rest in `other` |
| `previous_visit` | The earlier visit's own `encounters` row — the one already held, or a new one under its real doctor and clinic — with its prescription as a new version only if it changed. The date-only shape is filed under `UNKNOWN`, at midnight on its date |
| `items` | `prescription_items`, one row per medicine |
| The doctor's findings (API 3) | `consultation_findings`, one row per finding |
| `diagnoses`, `investigations` | `diagnoses`, `investigations` |
| `female_details`, `male_details` | `female_details`, `male_details` |
| Everything | A copy as `<recording name>.json`, in the same folder as the audio |

The JSON file travels with the audio; the database is the index. Neither is
built from the other.

---

## 6. Replies, and what to do about each

| Code | Meaning | What CMED should do |
|---|---|---|
| `ACCEPTED` (FHIR: HTTP 200; interim JSON: 202) | Stored | Remove it from the outbox. For API 2, read `previous_visit.status` (§3) |
| `ALREADY_RECEIVED` (HTTP 200) | Already held: this exact body, or (API 3) a prescription with the same fingerprint as the newest version | **Remove it from the outbox and never send it again.** Retrying was safe |
| `400 MALFORMED_JSON` | Not a JSON object | Fix and resend; do not retry unchanged |
| `400 MISSING_FIELD` | A required field is missing or malformed | Fix and resend |
| `400 INVALID_IDENTIFIER` | An id has characters outside `A-Z a-z 0-9 _ -` | Fix and resend |
| `400 MALFORMED_FHIR` | The Bundle cannot be read; the element is named | Fix and resend |
| `401 INVALID_TOKEN` | Token missing, unknown, expired or revoked | Get a new token once and resend; if the token request itself is refused (`401 invalid_client`), stop and tell us |
| `403 INSUFFICIENT_SCOPE` | The token lacks the scope | Ask for both scopes |
| `403 HOSPITAL_NOT_ALLOWED` | The message is for a hospital this token does not speak for | Use that hospital's own client. Do not retry; it raises an alert here |
| `401 INVALID_KEY`, `403 KEY_NOT_BOUND` | Interim static key wrong, or naming no hospital | Stop and tell us |
| `413 TOO_LARGE` | Over 1 MB | Split or trim; a body this large is usually a mistake |
| `warnings` in a `200`/`202` reply | **Accepted.** Each warning is `{field, sent, note}`: what was converted, read in another form, or not usable | Remove from the outbox as usual. Fix the field at source when convenient; never resend because of a warning |
| `429 RATE_LIMITED`, `5xx`, a timeout, no connection | Too fast (1,200 requests/min and 60 token requests/min per address, shared by every server process), a locked-out credential (10 wrong tries with the same credential in 5 minutes lock that credential — not the address — for 15 minutes), busy, or no internet. Honour `Retry-After` | **Keep it in the outbox** and retry with growing delay. Never drop it |

**Send from an outbox.** Each Aalo branch keeps working on its local CMED server
through an internet or power cut, and while it is cut off neither message can
reach us. Write each one to a table on that server at the moment of the click,
send in order (a visit's API 2 before its API 3), retry while we cannot be
reached, and remove a message only on code `ACCEPTED` or `ALREADY_RECEIVED`
(HTTP 200; 202 in the interim JSON form). A message that arrives a
day late attaches to its recording; a message never sent is the only failure.

**Every reply carries a request id** (`X-Request-ID`, and `request_id` in
the body; in FHIR replies in Parameters and `OperationOutcome.issue.diagnostics`).
Quote it when reporting a problem. Errors outside FHIR and the token endpoint
are RFC 9457 `application/problem+json`. Replies to the interim static key
carry `Deprecation`, `Sunset` and `Link` headers. The machine-readable
contract is `openapi/channel-b.json`.

**Retries are safe.** The same body twice produces one row, and the reply says
`ALREADY_RECEIVED` rather than failing. For API 2 the reply also carries
`"previous_visit": {"status": …, "version": …, "record_id": …}` (§3); on a
repeat of a body that stored it, the status is `ALREADY_STORED`.

---

## 7. What must not be sent

- **No audio, ever**, in either direction.
- **No consent field.** A patient's refusal is recorded on the recorder's own
  on-screen control, signed by the PC, and erases the recording everywhere —
  including the archive file and its cloud copy. CMED sends nothing about it,
  and nothing CMED's page sends can erase a recording (recorder 3.5.0+).
- **Nothing extra in the five fields.** They are matched exactly.
- **Not from a browser.** The private key and the token are server credentials.
- **No private key to AIMS LAB.** Send the public JWK only.

---

## 8. Testing before going live

Both halves are already built:

```
python tools/channel_b_test.py --local            # our rules, nothing running
python tools/channel_b_test.py --server https://<host> \
    --client-id <client id> --kid <kid> --key-file <hospital>.private.pem \
    --hospital <CMED hospital id>
```

The second runs every case that matters with a real token and FHIR — a token,
a replayed assertion, a good message, the same message twice, a broken one, a
changed prescription, a nameless medicine, another hospital's message, no
token, an oversized body — and checks each reply against this document. CMED's test page at
`/protocol-test` shows every message and reply side by side.

Please run it against a test server and send us the output before the first
clinic goes live.
