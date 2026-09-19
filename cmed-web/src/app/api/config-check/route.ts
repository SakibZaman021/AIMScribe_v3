/**
 * GET /api/config-check - is this deployment configured?
 *
 * Reports whether each required variable is present and well-formed. It never
 * returns a value, a key, or any part of one.
 *
 * This exists because a misconfigured deployment is indistinguishable from a
 * working one from the outside: recording simply refuses to start, with a
 * message that reads like user error.
 *
 * There is no doctor list here. Doctors are bound to machines at enrolment, so
 * the register is a property of the fleet rather than of this app - query
 * `v_doctors` in the database to see it.
 */
import { NextResponse } from 'next/server';

export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';

export async function GET() {
  const grant = process.env.AIMS_GRANT_PRIVATE_KEY ?? '';
  const webhook = process.env.AIMSCRIBE_WEBHOOK_SECRET ?? '';

  return NextResponse.json({
    ok: grant.includes('BEGIN PRIVATE KEY'),

    grant_key: {
      set: Boolean(grant),
      length: grant.length,
      looks_like_pem: grant.includes('BEGIN PRIVATE KEY'),
      // A PEM pasted with \n escapes instead of real line breaks parses as one
      // line and fails when the first grant is signed, not at startup.
      has_real_newlines: grant.includes('\n'),
      has_escaped_newlines: grant.includes('\\n'),
      note: 'Without this, no recording can be authorised.',
    },

    webhook_secret: {
      set: Boolean(webhook),
      length: webhook.length,
      note: 'Must match the backend, or NER webhooks are rejected.',
    },

    grant_issuer: process.env.AIMS_GRANT_ISSUER ?? '(default) cmed',
    grant_audience: process.env.AIMS_GRANT_AUDIENCE ?? '(default) aimscribe-recorder',
    backend_url: process.env.NEXT_PUBLIC_BACKEND_URL ?? '(not set)',
    recorder_ws: process.env.NEXT_PUBLIC_RECORDER_WS ?? '(default) ws://localhost:5050/ws',

    doctors:
      'Not configured here. Each PC is enrolled to one doctor by an ' +
      'administrator; see v_doctors and v_doctor_activity in the database.',
  });
}
