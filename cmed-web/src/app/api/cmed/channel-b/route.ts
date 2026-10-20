/**
 * POST /api/cmed/channel-b — this app's server to the AIMS LAB server (§6.2).
 *
 * Channel B is server to server, and this route is why: this hospital's
 * private key lives in this process's environment and is used here. It is
 * never sent to the browser, never in a page, never in a bundle
 * (`SRS-CHB-01`, `AT-72`) — search this app's network traffic for it and it
 * is not there.
 *
 * Two ways to authenticate, the first preferred:
 *
 *   token + FHIR    AIMS_CMED_CLIENT_ID, AIMS_CMED_KEY_ID, AIMS_CMED_PRIVATE_KEY
 *                   set: a signed assertion buys a 5-minute token for this
 *                   hospital, and the message goes as FHIR R4 to
 *                   /fhir/r4/$process-message (src/lib/cmed-fhir.ts).
 *   static key      AIMS_CMED_KEY set instead: the interim key, issued for
 *                   this hospital only, sent as X-CMED-Key with plain JSON.
 *
 * Two messages go through it:
 *
 *   patient_information  API 2 — the moment a patient is opened. This is what
 *                        confirms a recording (§5.6); without it a recording
 *                        is kept out of the dataset.
 *   prescription         API 3, part two — when the prescription is built.
 *
 * A failure here must never stop a consultation. The real CMED site should
 * queue and retry in the background (`SRS-CHB-05`, `SRS-CHB-06`, `AT-71`);
 * this test app reports the failure and lets the operator retry, which is
 * honest about what happened rather than pretending it was delivered.
 */
import { NextRequest, NextResponse } from 'next/server';
import { clientConfig, forgetToken, fromFhirReply, getToken, retryDelayMs,
         toFhirMessage }
  from '@/lib/cmed-fhir';

// Node runtime: the key must not reach an edge deployment we do not control.
export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';

const KINDS = {
  patient_information: 'patient-information',
  prescription: 'prescription',
} as const;

type Kind = keyof typeof KINDS;

export async function POST(request: NextRequest) {
  const key = process.env.AIMS_CMED_KEY ?? '';
  const server = (process.env.AIMS_SERVER_URL ?? '').replace(/\/+$/, '');
  const fhirClient = clientConfig();

  if (!server || (!key && !fhirClient)) {
    return NextResponse.json(
      {
        status: 503,
        code: 'NOT_CONFIGURED',
        message:
          'AIMS_SERVER_URL and either the hospital client (AIMS_CMED_CLIENT_ID, ' +
          'AIMS_CMED_KEY_ID, AIMS_CMED_PRIVATE_KEY) or AIMS_CMED_KEY are not set. ' +
          'Channel B cannot be used until they are.',
      },
      { status: 503 },
    );
  }

  let body: { kind?: string; message?: unknown };
  try {
    body = await request.json();
  } catch {
    return NextResponse.json(
      { status: 400, code: 'MALFORMED_JSON', message: 'The body is not JSON.' },
      { status: 400 },
    );
  }

  const kind = String(body.kind ?? '') as Kind;
  if (!(kind in KINDS)) {
    return NextResponse.json(
      {
        status: 400,
        code: 'UNKNOWN_KIND',
        message: "kind must be 'patient_information' or 'prescription'.",
      },
      { status: 400 },
    );
  }
  if (typeof body.message !== 'object' || body.message === null) {
    return NextResponse.json(
      { status: 400, code: 'MISSING_FIELD', message: 'message must be an object.' },
      { status: 400 },
    );
  }

  try {
    if (fhirClient) {
      const message = toFhirMessage(kind, body.message as Record<string, any>);
      const send = async () => fetch(`${server}/fhir/r4/$process-message`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/fhir+json',
                   Authorization: `Bearer ${await getToken(fhirClient)}` },
        body: JSON.stringify(message),
        signal: AbortSignal.timeout(20000),
        cache: 'no-store',
      });
      let response = await send();
      if (response.status === 401) {     // the token was revoked or expired early
        forgetToken();
        response = await send();
      }
      // This page waits once, briefly, for a busy server; CMED's real outbox
      // keeps the message and uses retryDelayMs for every later attempt.
      const wait = retryDelayMs(response.status, response.headers.get('retry-after'), 0, 10_000);
      if (wait !== null && response.status !== 401) {
        await new Promise((resolve) => setTimeout(resolve, wait));
        response = await send();
      }
      const reply = await response.json().catch(() => ({}));
      return NextResponse.json(fromFhirReply(response.status, reply),
                               { status: response.status });
    }

    const response = await fetch(`${server}/api/v2/clinical/${KINDS[kind]}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CMED-Key': key },
      body: JSON.stringify(body.message),
      // A consultation must not wait on this. The real site queues and
      // retries; here it simply gives up and says so.
      signal: AbortSignal.timeout(20000),
      cache: 'no-store',
    });

    const reply = await response.json().catch(() => ({}));
    // The reply is passed through as it came: the codes in §6.2.4 are what
    // CMED's own code has to handle, so seeing them here is the point.
    return NextResponse.json(reply, { status: response.status });
  } catch (error) {
    const reason = error instanceof Error ? error.message : String(error);
    return NextResponse.json(
      {
        status: 502,
        code: 'NOT_DELIVERED',
        message: `The AIMS LAB server could not be reached: ${reason}. ` +
                 'Nothing was stored; send it again.',
      },
      { status: 502 },
    );
  }
}
