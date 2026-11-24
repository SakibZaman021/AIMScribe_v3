# API 4 — End of visit

**AIMScribe recorder 3.9.8 · for CMED's developers · 25 November 2026**

## 1. What changes

Today a recording ends in one of two ways:

- the doctor opens the **next patient** (API 1 for the next visit), or
- **two minutes after CMED's API 3** (the prescription) reaches AIMS LAB.

From recorder 3.9.8 CMED sends a fourth message, **API 4**, when the doctor closes the visit. **API 4 ends the recording.** The two-minute rule is retired on a PC as soon as that PC receives its first API 4. Until then nothing changes, so CMED can roll API 4 out one clinic at a time.

| Message | Sent by | When | What it does |
|---|---|---|---|
| API 1 | EHR page in the doctor's browser | Doctor opens the patient | Starts the recording |
| API 2 | CMED's server | Demographics saved | Clinical data; closes the doctor's "Patient did not consent" choice |
| API 3 | CMED's server | Prescription saved | Clinical data |
| **API 4** | **EHR page in the doctor's browser** | **Doctor closes the visit** | **Ends the recording** |

The doctor can no longer stop or pause a recording from the recorder's screen; their choices there are logged only. A recording therefore runs whole from API 1 to API 4, or to the next patient's API 1.

## 2. How API 4 is sent

API 4 is sent exactly like API 1: from CMED's EHR page, in the doctor's browser, to the recorder on the same PC.

- **HTTP:** `POST http://127.0.0.1:5050/api/v1/session/end` with `Content-Type: application/json`, or
- **WebSocket** (the connection API 1 may already use): the command `"end"`.

The browser adds the `Origin` header itself. It must be one of the EHR addresses AIMS LAB has trusted (the same list as API 1). No token, key or secret is involved.

### Request

```json
{
  "request_id": "ehr-1764052265000",
  "end": {
    "visit_id":    "V-20261126-000123",
    "patient_id":  "P0012345",
    "doctor_id":   "DR0042",
    "hospital_id": "10034140",
    "end_time":    "2026-11-26T10:31:05+06:00",
    "date":        "2026-11-26"
  }
}
```

| Field | Required | Rule |
|---|---|---|
| `request_id` | no | Echoed back in the reply. Up to 128 characters. |
| `visit_id` | **yes, whenever API 1 sent one** | The same value as API 1. Letters, digits and `-`, up to 64. |
| `patient_id` | **yes** | The same value as API 1. Letters, digits, `_` and `-`, up to 64. |
| `doctor_id` | **yes** | The same value as API 1. |
| `hospital_id` | recommended | The same value as API 1. |
| `end_time` | **yes** | When the doctor closed the visit, ISO 8601 **with time zone**, written by CMED's server. |
| `date` | recommended | `YYYY-MM-DD`, the same as API 1. |

### Replies

Act on `code`, never on `message`.

| HTTP | `code` | Meaning | What CMED does |
|---|---|---|---|
| 200 | `RECORDING_ENDED` | This visit's recording has ended and is kept whole. `data` holds `session_id`, `duration_seconds`, `visit_id`. | Nothing more. |
| 200 | `VISIT_ALREADY_ENDED` | This visit's recording had already ended (a resend, or the next patient was opened first). | Nothing more. |
| 409 | `VISIT_MISMATCH` | The recording now running belongs to another visit. **It continues.** | Nothing; check that the fields are this visit's. |
| 409 | `NO_ACTIVE_SESSION` | Nothing is being recorded on this PC. | Nothing. |
| 400 | `MISSING_FIELD` / `INVALID_IDENTIFIER` | A field is missing or malformed; `message` says which. | Fix the page. |
| 403 | `ORIGIN_NOT_ALLOWED` | This page's address is not trusted. | Ask AIMS LAB to add it. |
| 503 | `AGENT_NOT_READY` | The recorder is starting. | Send again once. |
| — | `AIMSCRIBE_UNREACHABLE` | Set by the script below when the recorder is not running. | Nothing: the consultation goes on. |

## 3. Rules

1. **Send API 4 once per visit, when the doctor closes it.** This is normally after API 3. Sending it again is safe and answers `VISIT_ALREADY_ENDED`.
2. **API 4 only ever ends the visit it names.** If the doctor has already opened the next patient, that patient's recording keeps running, and the reply is `VISIT_ALREADY_ENDED` or `VISIT_MISMATCH`.
3. **Never block the page on API 4.** Whatever the reply, the doctor carries on and CMED's server still sends API 2 and API 3.
4. **If API 4 is never sent,** the recording carries on until the next patient is opened (or, on a PC that has never received API 4, until two minutes after API 3).
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
          hospital_id: fields.hospital_id,
          end_time: endTime,          // from CMED's server, with its time zone
          date: fields.date,
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

## 5. Acceptance tests (on a test PC with recorder 3.9.8)

| # | Do | Expect |
|---|---|---|
| A4-1 | API 1 for visit V1, then API 4 for V1 | `RECORDING_ENDED`. The recorder's dot goes out. The dashboard shows "CMED closed the visit (API 4)". |
| A4-2 | Send the same API 4 again | `VISIT_ALREADY_ENDED`; nothing changes. |
| A4-3 | API 1 for V2, then API 4 naming V1 | `VISIT_ALREADY_ENDED`; V2's recording continues. |
| A4-4 | API 1 for V3, then API 4 for V3 with another `patient_id` | `VISIT_MISMATCH`; the recording continues. |
| A4-5 | API 4 with no recording running | `NO_ACTIVE_SESSION`. |
| A4-6 | API 4 without `end_time`, or with no time zone | `MISSING_FIELD`. |
| A4-7 | API 1 for V4, wait 3 minutes after API 3, no API 4 | On a PC that has received API 4 before, still recording. |
| A4-8 | API 4 from a page that is not trusted | `ORIGIN_NOT_ALLOWED`. |

Postman: duplicate the API 1 request, change the address to `/api/v1/session/end` and the body to the request above. Keep the `Origin` header set to the trusted test page.
