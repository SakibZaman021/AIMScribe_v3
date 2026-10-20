/**
 * GET /api/config-check - is this deployment configured?
 *
 * Reports whether each required setting is present and well-formed. It never
 * returns a value, a key, or any part of one.
 *
 * This exists because a misconfigured deployment looks exactly like a working
 * one from outside: recording simply refuses to start, or clinical messages
 * quietly fail, with wording that reads like user error.
 *
 * Two things changed in v3, and both make this shorter:
 *
 *  - **There is no signing key here any more.** The AIMS LAB server issues
 *    recording grants (§5); this app sends five plain fields and has nothing
 *    to sign. One less secret to hold is one less secret to leak.
 *  - **Channel B needs a key**, and it lives on this server only. It is
 *    checked here by shape, never shown (`SRS-CHB-01`, `AT-72`).
 */
import { NextResponse } from 'next/server';

export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';

export async function GET() {
  const cmedKey = process.env.AIMS_CMED_KEY ?? '';
  const clientId = process.env.AIMS_CMED_CLIENT_ID ?? '';
  const keyId = process.env.AIMS_CMED_KEY_ID ?? '';
  const privateKey = process.env.AIMS_CMED_PRIVATE_KEY ?? '';
  const tokenMethod = Boolean(clientId && keyId && privateKey);
  const server = process.env.AIMS_SERVER_URL ?? '';
  const webhook = process.env.AIMSCRIBE_WEBHOOK_SECRET ?? '';
  const legacyGrantKey = process.env.AIMS_GRANT_PRIVATE_KEY ?? '';

  return NextResponse.json({
    ok: (tokenMethod || Boolean(cmedKey)) && server.startsWith('https://'),

    // The method CMED moves to: this hospital's own client, a private key held
    // here only, 5-minute tokens, FHIR R4. Reported by shape, never by value.
    channel_b_token_method: {
      set: tokenMethod,
      client_id: clientId || null,
      key_id_set: Boolean(keyId),
      private_key_looks_like_pem: privateKey.includes('BEGIN PRIVATE KEY'),
      note: 'Preferred. When set, API 2 and API 3b go as FHIR R4 with a bearer token.',
    },

    channel_b_key: {
      set: Boolean(cmedKey),
      length: cmedKey.length,
      note: 'The interim static key, issued for this hospital only. Used only ' +
            'when the token method is not configured.',
    },

    aims_server: {
      set: Boolean(server),
      is_https: server.startsWith('https://'),
      note: 'The AIMS LAB server this app sends clinical messages to.',
    },

    webhook_secret: {
      set: Boolean(webhook),
      length: webhook.length,
      note: 'Only needed while the AI webhook is switched on; it is off in v3 ' +
            '(decision D2).',
    },

    recorder_ws:
      process.env.NEXT_PUBLIC_RECORDER_WS ?? '(default) ws://localhost:5050/ws',

    // v3: the page sends five fields and the server decides. A key left here
    // from the old arrangement is no longer used - and is worth removing.
    legacy_grant_key_present: Boolean(legacyGrantKey),
    legacy_grant_key_note: legacyGrantKey
      ? 'AIMS_GRANT_PRIVATE_KEY is set but no longer used: the AIMS LAB ' +
        'server issues grants now (§5). Remove it from this environment.'
      : 'Not set, which is correct for v3.',

    doctors:
      'Not configured here. CMED names the doctor in each trigger; the ' +
      'register on the AIMS LAB side is a directory, not a gate.',
  });
}
