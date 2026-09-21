# The CMED test site, and how to try the integration

This app stands in for CMED's own site. It is here so the integration can be
built and checked without a clinic, a patient, or a real recording — and so
that what CMED's engineers read is working code rather than a description.

Everything below matches SRS 3.3: §6.1 for Channel A (the browser to the
recorder on the same PC) and §6.2 for Channel B (CMED's server to the AIMS LAB
server).

## What changed in v3, in one paragraph

The page no longer authorises anything. It sends **five plain fields** —
patient, doctor, clinic, start time, date — and the AIMS LAB server decides
whether the recording may happen. There is no signing key in this app any more.
Consent has left the interface entirely: reception asks, and if a patient
declines, the doctor presses Stop on the recorder and chooses "Patient did not
consent", which deletes the recording everywhere. A consultation now ends when
the prescription is built, not when the next patient is opened.

## Running it

```bash
cp .env.local.example .env.local     # AIMS_CMED_KEY and AIMS_SERVER_URL
npm install
npm run dev                          # http://localhost:3000
```

| Page | What it is for |
|---|---|
| `/` → `/dashboard` | The doctor's screen: open a patient, watch the recording, build the prescription |
| `/protocol-test` | The bench: every message, one button each, with the reply code beside it |
| `/api/config-check` | Whether this deployment is configured, without showing any value |

## The order of messages, and what each is for

```
1. API 1   browser  → recorder        the five fields; recording starts at once
2. API 2   CMED srv → AIMS LAB srv    what is known about the patient; this is
                                      what confirms the recording (§5.6)
3. API 3a  browser  → recorder        prescription_built: this consultation may end
4. API 3b  CMED srv → AIMS LAB srv    the prescription itself
```

Two of those are server to server because the key is server to server. The
route that sends them is `src/app/api/cmed/channel-b/route.ts` — 100 lines, and
the only place the key is touched.

## Codes, not wording

Act on `code`, never on `message` (`SRS-IF1-09`). The wording may change; the
codes are the contract (Appendix A, §6.2.4).

| Code | It means | What CMED's site should do |
|---|---|---|
| `RECORDING_STARTED` (200) | Recording, and authorised | Nothing; carry on |
| `RECORDING_PROVISIONAL` (202) | Recording; permission still being checked | Nothing; carry on — treat exactly as above |
| `GATE_NOT_ARMED` (409) | The last consultation has not been finished | Send `prescription_built` for it first |
| `SESSION_ALREADY_ACTIVE` (409) | This patient is already being recorded | Nothing |
| `CLINIC_MISMATCH` (401) | This PC belongs to another clinic | Show the operator; do not retry |
| `AGENT_NOT_READY` (503) | The recorder is not running | Carry on with the consultation; it is not recorded |
| `ACCEPTED` (202) | Channel B stored the message | Nothing |
| `ALREADY_RECEIVED` (200) | The same message arrived twice | Nothing — safe to retry after a timeout |
| `SCHEMA_INVALID` (422) | Stored in quarantine; some fields were wrong | Fix and resend; the visit is not lost |
| `INVALID_KEY` (401) | The key is missing or wrong | Stop and tell AIMS LAB |

**A failure on Channel B must never interrupt a consultation.** Queue it and
retry in the background (`SRS-CHB-05`, `SRS-CHB-06`). A recording whose API 2
arrives late is still confirmed when it arrives.

## Checking messages without a server

Two commands, from the repository root, that need nothing running:

```bash
# A valid message to copy and change
python tools/channel_b_test.py --sample patient_information > api2.json

# The server's own rules, applied locally
python tools/channel_b_test.py --validate api2.json --kind patient_information
```

And, against a test server, every case that matters — a good message, the same
message twice, a broken one, a changed prescription, a wrong key, an oversized
body — checked against the code each should return:

```bash
python tools/channel_b_test.py --server https://test.aimscribe.example \
    --key "$CMED_TEST_KEY" --hospital CMED-TEST-01
```

It prints a table and exits non-zero if anything differs, so it can run in
CMED's own pipeline.

## What this app deliberately does not do

- **It does not hold a signing key.** If you find `AIMS_GRANT_PRIVATE_KEY` set
  anywhere, remove it: it is no longer used, and `/api/config-check` says so.
- **It does not record consent.** Nothing about consent crosses either channel.
- **It does not choose the clinic.** The clinic comes from the PC's enrolment;
  a mismatch is refused rather than filed under the wrong clinic.
- **It does not stop a recording when a tab closes.** Recording belongs to the
  recorder on the PC; reloading only restores the view.
