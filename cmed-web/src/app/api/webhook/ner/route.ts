/**
 * POST /api/webhook/ner - receives NER results from the AIMScribe backend.
 *
 * Every payload is HMAC-verified before it is stored. Previously the signature
 * and timestamp headers were read and then ignored ("optional security"), which
 * meant anyone who found this URL could push fabricated chief complaints,
 * diagnoses and **medications with dosages** onto a doctor's prescription screen.
 * That is a patient-safety defect, not only a security one.
 *
 * Verification is over the raw request body, not a re-serialised object: any
 * difference in key order or unicode escaping would change the bytes and break an
 * otherwise valid signature.
 */
import { NextRequest, NextResponse } from 'next/server';
import { createHmac, timingSafeEqual } from 'crypto';

export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';

/** Reject anything older than this, so a captured payload cannot be replayed. */
const TIMESTAMP_TOLERANCE_SECONDS = 300;

/**
 * In-memory store, kept only as a fallback for polling.
 *
 * Serverless caveat: each instance has its own copy, so a POST and a later GET
 * may land on different instances. The dashboard therefore treats the backend as
 * the source of truth and uses this only as a best-effort cache. Point
 * AIMS_NER_STORE at Redis if you need this to be reliable across instances.
 */
const nerStore = new Map<string, { ner: any; version: number; timestamp: number }>();
const MAX_SESSIONS = 500;
const TTL_MS = 60 * 60 * 1000;

function prune(): void {
  const cutoff = Date.now() - TTL_MS;
  for (const [key, value] of Array.from(nerStore.entries())) {
    if (value.timestamp < cutoff) nerStore.delete(key);
  }
  // Hard cap as well as a TTL: session ids come from the payload, so an
  // unbounded map is a memory-exhaustion lever for anyone who can reach this.
  while (nerStore.size > MAX_SESSIONS) {
    const oldest = nerStore.keys().next();
    if (oldest.done) break;
    nerStore.delete(oldest.value);
  }
}

function verifySignature(rawBody: string, signature: string | null, timestamp: string | null): string | null {
  const secret = process.env.AIMSCRIBE_WEBHOOK_SECRET;
  if (!secret) {
    return 'AIMSCRIBE_WEBHOOK_SECRET is not configured';
  }
  if (!signature || !timestamp) {
    return 'missing signature or timestamp';
  }

  const sent = Number(timestamp);
  if (!Number.isFinite(sent)) return 'malformed timestamp';
  const skew = Math.abs(Math.floor(Date.now() / 1000) - sent);
  if (skew > TIMESTAMP_TOLERANCE_SECONDS) {
    return `timestamp is ${skew}s out of tolerance`;
  }

  const expected = createHmac('sha256', secret)
    .update(`${timestamp}.${rawBody}`)
    .digest('hex');
  const provided = signature.replace(/^sha256=/, '');

  const a = Buffer.from(expected, 'utf8');
  const b = Buffer.from(provided, 'utf8');
  // timingSafeEqual throws on length mismatch, so check that first.
  if (a.length !== b.length || !timingSafeEqual(a, b)) {
    return 'signature does not match';
  }
  return null;
}

export async function POST(request: NextRequest) {
  // Read the body as text so the signature covers exactly the bytes sent.
  const rawBody = await request.text();

  const failure = verifySignature(
    rawBody,
    request.headers.get('x-aimscribe-signature'),
    request.headers.get('x-aimscribe-timestamp')
  );

  if (failure) {
    console.warn(`[webhook] rejected: ${failure}`);
    // Deliberately vague to the caller; the detail is in the log.
    return NextResponse.json({ error: 'Unauthorised' }, { status: 401 });
  }

  let body: any;
  try {
    body = JSON.parse(rawBody);
  } catch {
    return NextResponse.json({ error: 'Invalid JSON' }, { status: 400 });
  }

  const sessionId = body?.session?.id;
  if (!sessionId || typeof sessionId !== 'string' || sessionId.length > 128) {
    return NextResponse.json({ error: 'Missing or invalid session id' }, { status: 400 });
  }

  const ner = body.ner ?? {};
  const version = Number(ner.version ?? 1);

  const transformed = {
    chief_complaints: { data: ner.chief_complaints ?? [] },
    drug_history: { data: ner.drug_history ?? [] },
    on_examination: { data: formatExamination(ner.on_examination) },
    systemic_examination: { data: formatExamination(ner.systemic_examination) },
    investigations: { data: ner.investigations ?? [] },
    diagnosis: { data: ner.diagnosis ?? [] },
    medications: { data: ner.medications ?? [] },
    advice: { data: ner.advice ?? [] },
    follow_up: { data: ner.follow_up ? [JSON.stringify(ner.follow_up)] : [] },
    additional_notes: { data: ner.additional_notes ?? [] },
  };

  prune();
  nerStore.set(sessionId, { ner: transformed, version, timestamp: Date.now() });

  console.log(`[webhook] ${body.event ?? 'ner'} v${version} for ${sessionId}`);
  return NextResponse.json({ received: true, session_id: sessionId, version });
}

export async function GET(request: NextRequest) {
  const sessionId = request.nextUrl.searchParams.get('session_id');
  if (!sessionId) {
    return NextResponse.json({ error: 'Missing session_id' }, { status: 400 });
  }

  const entry = nerStore.get(sessionId);
  if (!entry) {
    return NextResponse.json({ version: 0, ner: null });
  }
  return NextResponse.json({
    version: entry.version,
    ner: entry.ner,
    timestamp: entry.timestamp,
  });
}

function formatExamination(exam: any): string[] {
  if (!exam) return [];
  if (Array.isArray(exam)) return exam;
  if (typeof exam === 'object') {
    return Object.entries(exam)
      .filter(([, value]) => value)
      .map(([key, value]) => `${key}: ${value}`);
  }
  return [String(exam)];
}
