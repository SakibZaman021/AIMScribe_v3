# API 4 — End of visit

**AIMScribe recorder 3.9.8 · for CMED's developers · 25 November 2026**

## 1. What changes

A recording ends in one of these ways, **set per clinic by AIMS LAB**:

| Clinic | How its recordings end | Does CMED send API 4? |
|---|---|---|
| **Amader Susastho** | **Two minutes after CMED's API 3** (the prescription), always | **No.** If it is sent, it is logged and changes nothing. |
| **Aalo Clinic** | **On CMED's API 4**, once CMED tells AIMS LAB that API 4 is live there. Until then, two minutes after API 3, as today. | **Yes**, when the doctor closes the visit. |

In every clinic the recording also ends when the doctor opens the **next patient** (the next API 1). Two safety ends apply everywhere: 15 minutes after API 3 if a clinic on API 4 never receives it, and 45 minutes with no message from CMED at all.

AIMS LAB switches a clinic to API 4 on its server, the day CMED says API 4 is live there; every PC in that clinic learns it within 30 seconds. Nothing is installed again. For CMED's own tests, AIMS LAB switches CMED's test clinic first.

| Message | Sent by | When | What it does |
|---|---|---|---|
| API 1 | EHR page in the doctor's browser | Doctor opens the patient | Starts the recording |
| API 2 | CMED's server | Demographics saved | Clinical data; closes the doctor's "Patient did not consent" choice |
| API 3 | CMED's server | Prescription saved | Clinical data; at a two-minute clinic, the recording ends two minutes later |
| **API 4** | **EHR page in the doctor's browser** | **Doctor closes the visit** | **Ends the recording (Aalo Clinic, once live)** |

The doctor can no longer stop or pause a recording from the recorder's screen; their choices there are logged only.

## 2. How API 4 is sent

API 4 is sent like API 1 (handbook section 7.2) when the doctor closes the visit: from CMED's EHR page, in the doctor's browser, to the recorder on the same PC, with **four fields**: `visit_id`, `patient_id` and `doctor_id` (the same values as that visit's API 1) and `end_time`. `start_time`, `date` and `hospital_id` are not sent: the recorder has them from API 1.

- **HTTP:** `POST http://127.0.0.1:5050/api/v1/session/end` with `Content-Type: application/json`, or
- **WebSocket** (the connection API 1 may already use): the command `"end"`.

The browser adds the `Origin` header itself. It must be one of the EHR addresses AIMS LAB has trusted (the same list as API 1). No token, key or secret is involved.

**No gateway.** API 1 and API 4 never leave the laptop: 127.0.0.1 is the laptop itself, and each doctor laptop has its own recorder. Only CMED's server talks to AIMS LAB's API gateway, `https://aimscribe.iriic.uiu.ac.bd` (API 0, API 2, API 3, status, log, with a Bearer token); API 4 changes nothing there.

### Request (the same visit as the handbook's API 1 example)

```json
API 1 (unchanged):
{"request_id": "ehr-1730960072000",
 "trigger": {"visit_id": "VIS-1001", "patient_id": "P0012345", "doctor_id": "DR0042",
             "hospital_id": "CMED-HOSP-01", "start_time": "2026-11-07T10:14:32+06:00",
             "date": "2026-11-07"}}

API 4:
POST http://127.0.0.1:5050/api/v1/session/end
{"request_id": "ehr-1730960791000",
 "end": {"visit_id": "VIS-1001", "patient_id": "P0012345", "doctor_id": "DR0042",
         "end_time": "2026-11-07T10:26:31+06:00"}}
```

Tested on recorder 3.9.8 on 25 Nov 2026: this request, sent for the visit being recorded, answered `RECORDING_ENDED`; sent again, `VISIT_ALREADY_ENDED`; sent naming another visit, `VISIT_MISMATCH` and the recording continued.

| Field | Required | Rule |
|---|---|---|
| `request_id` | no | Echoed back in the reply. Up to 128 characters. |
| `visit_id` | **yes** | The same value as API 1. Ends only this visit's recording. |
| `patient_id` | **yes** | The same value as API 1. |
| `doctor_id` | **yes** | The same value as API 1. |
| `end_time` | **yes** | When the doctor closed the visit, ISO 8601 **with time zone**, written by CMED's server. |
| `hospital_id`, `start_time`, `date` | not sent | The recorder has them from API 1. Ignored if sent. |

AIMS LAB's server receives nothing new from CMED for API 4: the recorder ends the recording and reports it itself.

### Replies

Act on `code`, never on `message`.

| HTTP | `code` | Meaning | What CMED does |
|---|---|---|---|
| 200 | `RECORDING_ENDED` | **The confirmation**, sent only after the microphone has stopped and the audio is saved on the laptop. `data`: `recording_stopped: true`, `stopped_at`, CMED's `end_time` (kept by AIMS LAB for the label), `duration_seconds`, `audio_saved`, `segment_count`, `session_id`, `visit_id`. | Nothing more. |
| 200 | `VISIT_ALREADY_ENDED` | This visit's recording had already ended (a resend, or the next patient was opened first). `data` repeats `recording_stopped: true`, `stopped_at` and `end_time`. | Nothing more. |
| 200 | `RECORDING_CONTINUES` | This clinic's recordings end two minutes after API 3 (Amader Susastho; Aalo until API 4 is live there). API 4 is logged with its `end_time`; `data.recording_stopped` is `false`. | Nothing more. |
| 409 | `VISIT_MISMATCH` | The recording now running belongs to another visit. **It continues.** | Nothing; check that the fields are this visit's. |
| 409 | `NO_ACTIVE_SESSION` | Nothing is being recorded on this PC. | Nothing. |
| 400 | `MISSING_FIELD` / `INVALID_IDENTIFIER` | A field is missing or malformed; `message` says which. | Fix the page. |
| 403 | `ORIGIN_NOT_ALLOWED` | This page's address is not trusted. | Ask AIMS LAB to add it. |
| 503 | `AGENT_NOT_READY` | The recorder is starting. | Send again once. |
| — | `AIMSCRIBE_UNREACHABLE` | Set by the script below when the recorder is not running. | Nothing: the consultation goes on. |

Later, CMED's server can also read it on the gateway: `GET https://aimscribe.iriic.uiu.ac.bd/api/v4/clinical/visit/{visit_id}/status` carries `"api4": {"received": true, "end_time": ..., "recording_stopped": true, "stopped_at": ...}` when API 4 ended the recording.

## 3. Rules

1. **Send API 4 once per visit, when the doctor closes it** (Aalo Clinic). This is normally after API 3. Sending it again is safe and answers `VISIT_ALREADY_ENDED`.
2. **API 4 only ever ends the visit it names.** If the doctor has already opened the next patient, that patient's recording keeps running, and the reply is `VISIT_ALREADY_ENDED` or `VISIT_MISMATCH`.
3. **Never block the page on API 4.** Whatever the reply, the doctor carries on and CMED's server still sends API 2 and API 3.
4. **API 3 is still needed everywhere.** It is the end at Amader Susastho, and at Aalo until API 4 is live; afterwards it starts the 15-minute safety end.
5. **The values must be the visit's own,** exactly as in API 1, made by CMED's server, never typed or built in the browser.

## 4. The browser function

Add this beside `startRecording` in `browser/aimscribe-api1.js`, and call it when the doctor closes the visit:

```javascript
const AIMSCRIBE_END = 'http://127.0.0.1:5050/api/v1/session/end';

async function endRecording(fields, endTime) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 20000);
  try {
    const res = await fetch(AIMSCRIBE_END, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        request_id: 'ehr-' + Date.now(),
        end: {
          visit_id: fields.visit_id,
          patient_id: fields.patient_id,
          doctor_id: fields.doctor_id,
          end_time: endTime,          // from CMED's server, with its time zone
        },
      }),
      signal: controller.signal,
    });
    return await res.json();          // reply.code: RECORDING_ENDED, VISIT_ALREADY_ENDED, ...
  } catch (e) {
    return { code: 'AIMSCRIBE_UNREACHABLE', message: String(e) };
  } finally {
    clearTimeout(timer);
  }
}

// In the "visit closed" code of the EHR page:
//   endRecording(visit.aimscribe_fields, visit.closed_at).then((r) => console.log('AIMScribe:', r.code));
```

## 5. Acceptance tests (on a test PC with recorder 3.9.8, its test clinic switched to API 4 by AIMS LAB)

| # | Do | Expect |
|---|---|---|
| A4-1 | API 1 for visit V1, then API 4 for V1 | `RECORDING_ENDED` with `recording_stopped: true`. The recorder's dot goes out. |
| A4-2 | Send the same API 4 again | `VISIT_ALREADY_ENDED`; nothing changes. |
| A4-3 | API 1 for V2, then API 4 naming V1 | `VISIT_ALREADY_ENDED`; V2's recording continues. |
| A4-4 | API 1 for V3, then API 4 for V3 with another `patient_id` | `VISIT_MISMATCH`; the recording continues. |
| A4-5 | API 4 with no recording running | `NO_ACTIVE_SESSION`. |
| A4-6 | API 4 without `end_time`, or with no time zone | `MISSING_FIELD`. |
| A4-7 | API 1 for V4, API 3, wait 3 minutes, no API 4 | Still recording (ends at API 4, or 15 minutes after API 3). |
| A4-8 | API 4 from a page that is not trusted | `ORIGIN_NOT_ALLOWED`. |
| A4-9 | On a PC of a two-minute clinic (Amader Susastho): API 1, then API 4 | `RECORDING_CONTINUES`; the recording ends two minutes after API 3. |

Postman: duplicate the API 1 request, change the address to `/api/v1/session/end` and the body to the request above. Keep the `Origin` header set to the trusted test page.
