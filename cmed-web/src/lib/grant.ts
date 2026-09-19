/**
 * Recording grants.
 *
 * A grant is a short-lived, single-use, Ed25519-signed statement that a named
 * doctor may record a named patient at a named hospital. The AIMScribe agent
 * verifies it against a pinned public key before it will touch the microphone.
 *
 * This is the hinge of the whole security model. Previously any web page the
 * doctor visited could start a recording, and free text could file a
 * consultation under a hospital or a doctor that did not exist.
 *
 * Now the hospital comes from the machine's enrolment and never from here. The
 * doctor is named by the page - a consulting room is shared, so it has to be -
 * but only from the hospital's register, and the backend checks the name again
 * before the session opens. A signed grant is still required, so a stray page
 * cannot record at all.
 *
 * The private key lives only in this server's environment. It never reaches the
 * browser, and the agent only ever holds the public half.
 */
import { SignJWT, importPKCS8, type KeyLike } from 'jose';
import { randomUUID } from 'crypto';

const ALGORITHM = 'EdDSA';

/** Must match AIMS_GRANT_ISSUER / AIMS_GRANT_AUDIENCE on every agent. */
const ISSUER = process.env.AIMS_GRANT_ISSUER || 'cmed';
const AUDIENCE = process.env.AIMS_GRANT_AUDIENCE || 'aimscribe-recorder';

/**
 * Sixty seconds. Long enough for a click to reach the agent, short enough that a
 * captured grant is worthless by the time anyone could replay it - and the agent
 * refuses a repeated jti regardless.
 */
const LIFETIME_SECONDS = 60;

export interface GrantClaims {
  doctorId: string;
  doctorName: string;
  hospitalId: string;
  patientRef: string;
  consentObtained: boolean;
  consentMethod: string;
}

let cachedKey: KeyLike | null = null;

async function signingKey(): Promise<KeyLike> {
  if (cachedKey) return cachedKey;

  const pem = process.env.AIMS_GRANT_PRIVATE_KEY;
  if (!pem) {
    throw new Error(
      'AIMS_GRANT_PRIVATE_KEY is not set. Recording cannot be authorised without it.'
    );
  }

  // Environment variables flatten newlines; restore them before parsing.
  const key = await importPKCS8(pem.replace(/\\n/g, '\n'), ALGORITHM);
  cachedKey = key;
  return key;
}

export async function mintGrant(claims: GrantClaims): Promise<{ grant: string; expiresIn: number }> {
  if (!claims.consentObtained) {
    // Enforced here as well as on the agent and in the database. Consent is a
    // precondition for recording, not a field to fill in afterwards.
    throw new Error('Patient consent must be recorded before a grant can be issued.');
  }

  const key = await signingKey();
  const now = Math.floor(Date.now() / 1000);

  const grant = await new SignJWT({
    doctor_name: claims.doctorName,
    hospital_id: claims.hospitalId,
    patient_ref: claims.patientRef,
    consent_obtained: true,
    consent_method: claims.consentMethod,
  })
    .setProtectedHeader({ alg: ALGORITHM })
    .setIssuer(ISSUER)
    .setAudience(AUDIENCE)
    .setSubject(claims.doctorId)
    .setIssuedAt(now)
    .setExpirationTime(now + LIFETIME_SECONDS)
    .setJti(randomUUID())
    .sign(key);

  return { grant, expiresIn: LIFETIME_SECONDS };
}

/** Identifiers become folder names on the archive volume. Keep them boring. */
const SAFE_ID = /^[A-Za-z0-9_-]{1,64}$/;

export function assertSafeIdentifier(value: string, field: string): string {
  if (!SAFE_ID.test(value)) {
    throw new Error(`${field} must be 1-64 characters of A-Z a-z 0-9 _ -`);
  }
  return value;
}
