/**
 * Channel B the way CMED's server does it: per-hospital token, FHIR R4.
 *
 * This file is the working reference for CMED's engineers. It runs on the
 * server only (it holds a private key) and does three things:
 *
 *   getToken()        signs a 5-minute assertion with this hospital's PRIVATE
 *                     key (ES384, RFC 7523) and trades it at
 *                     /api/v2/oauth/token for a 5-minute access token.
 *                     Tokens are reused until 30 s before they expire.
 *   toFhirMessage()   turns an API 2 / API 3b body into a FHIR message Bundle
 *                     (the mapping in backend/src/fhir.py, field for field).
 *   fromFhirReply()   reads the reply message back into status/code/record_id.
 *
 * The private key never leaves this process. AIMS LAB holds only the public
 * half, so nothing AIMS LAB stores can be used to pretend to be this hospital.
 */
import { SignJWT, importPKCS8 } from 'jose';
import { randomUUID } from 'node:crypto';

export type Kind = 'patient_information' | 'prescription';
type Json = Record<string, any>;

export const SYSTEMS = {
  Patient: process.env.AIMS_FHIR_SYSTEM_PATIENT ?? 'urn:aimscribe:cmed:patient',
  Practitioner: process.env.AIMS_FHIR_SYSTEM_PRACTITIONER ?? 'urn:aimscribe:cmed:practitioner',
  Organization: process.env.AIMS_FHIR_SYSTEM_ORGANIZATION ?? 'urn:aimscribe:cmed:hospital',
};
const LOINC = 'http://loinc.org';
const EVENT = { patient_information: 'patient-information', prescription: 'prescription' };

export interface ClientConfig {
  server: string;
  clientId: string;
  keyId: string;
  privateKeyPem: string;
  audience: string;
}

export function clientConfig(): ClientConfig | null {
  const server = (process.env.AIMS_SERVER_URL ?? '').replace(/\/+$/, '');
  const clientId = process.env.AIMS_CMED_CLIENT_ID ?? '';
  const keyId = process.env.AIMS_CMED_KEY_ID ?? '';
  const privateKeyPem = (process.env.AIMS_CMED_PRIVATE_KEY ?? '').replace(/\\n/g, '\n');
  if (!server || !clientId || !keyId || !privateKeyPem) return null;
  const audience = process.env.AIMS_CMED_TOKEN_AUDIENCE
    ?? `${server}/api/v2/oauth/token`;
  return { server, clientId, keyId, privateKeyPem, audience };
}

let cached: { token: string; until: number } | null = null;

export async function getToken(cfg: ClientConfig): Promise<string> {
  if (cached && Date.now() < cached.until) return cached.token;
  const key = await importPKCS8(cfg.privateKeyPem, 'ES384');
  const assertion = await new SignJWT({})
    .setProtectedHeader({ alg: 'ES384', kid: cfg.keyId, typ: 'JWT' })
    .setIssuer(cfg.clientId)
    .setSubject(cfg.clientId)
    .setAudience(cfg.audience)
    .setIssuedAt()
    .setExpirationTime('4m')
    .setJti(randomUUID())
    .sign(key);
  const response = await fetch(`${cfg.server}/api/v2/oauth/token`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    body: new URLSearchParams({
      grant_type: 'client_credentials',
      // SMART v2: create message Bundles; read and search Encounters.
      scope: 'system/Bundle.c system/Encounter.rs',
      client_assertion_type: 'urn:ietf:params:oauth:client-assertion-type:jwt-bearer',
      client_assertion: assertion,
    }),
    signal: AbortSignal.timeout(15000),
    cache: 'no-store',
  });
  const reply = await response.json().catch(() => ({}));
  if (!response.ok || !reply.access_token) {
    cached = null;
    throw new Error(`token refused (${response.status}): ` +
                    `${reply.error ?? ''} ${reply.error_description ?? ''}`.trim());
  }
  cached = { token: reply.access_token,
             until: Date.now() + (Number(reply.expires_in) - 30) * 1000 };
  return cached.token;
}

export function forgetToken() {
  cached = null;
}

/**
 * How long CMED's outbox should wait before resending, or null when it must
 * not resend (any 4xx other than 401 and 429 means: fix the message).
 * Retry-After when the server gives it; otherwise exponential backoff with
 * full jitter, so a clinic's backlog never retries in lock-step.
 */
export function retryDelayMs(status: number, retryAfter: string | null,
                             attempt: number, maxMs = 300_000): number | null {
  if (status !== 0 && status !== 429 && status < 500) return null;
  const seconds = retryAfter && /^\d+$/.test(retryAfter) ? Number(retryAfter) : null;
  if (seconds !== null) return Math.min(seconds * 1000, maxMs);
  return Math.random() * Math.min(maxMs, 2000 * 2 ** attempt);
}

// ------------------------------------------------------------------ body -> FHIR

const id = () => `urn:uuid:${randomUUID()}`;
const ref = (url: string) => ({ reference: url });
const ident = (kind: keyof typeof SYSTEMS, value: string) =>
  [{ system: SYSTEMS[kind], value }];

function vital(code: string, value: number | undefined, unit: string, when?: string) {
  if (typeof value !== 'number') return null;
  return {
    resourceType: 'Observation', status: 'final',
    category: [{ coding: [{ system: 'http://terminology.hl7.org/CodeSystem/observation-category',
                            code: 'vital-signs' }] }],
    code: { coding: [{ system: LOINC, code }] },
    valueQuantity: { value, unit, system: 'http://unitsofmeasure.org', code: unit },
    ...(when ? { effectiveDateTime: when } : {}),
  };
}

function prescriptionResources(p: Json, encounter: string, patient: string,
                               issuedAt?: string): Json[] {
  const out: Json[] = [];
  for (const item of p.items ?? []) {
    const dosage: Json = {};
    const dose = /^\s*([\d.]+)\s*(.*)$/.exec(item.dose ?? '');
    if (dose) dosage.doseAndRate = [{ doseQuantity: { value: Number(dose[1]),
                                                      unit: dose[2] || undefined } }];
    const timing: Json = {};
    if (item.frequency) timing.code = { text: item.frequency };
    const days = /^\s*([\d.]+)\s*(.*)$/.exec(item.duration ?? '');
    if (days) timing.repeat = { boundsDuration: { value: Number(days[1]),
                                                  unit: days[2] || undefined } };
    if (Object.keys(timing).length) dosage.timing = timing;
    if (item.instructions) dosage.patientInstruction = item.instructions;
    out.push({
      resourceType: 'MedicationRequest', status: 'active', intent: 'order',
      subject: ref(patient), encounter: ref(encounter),
      ...(issuedAt ? { authoredOn: issuedAt } : {}),
      medicationCodeableConcept: { text: item.drug },
      dosageInstruction: Object.keys(dosage).length ? [dosage] : undefined,
    });
  }
  for (const d of p.diagnoses ?? []) {
    out.push({ resourceType: 'Condition', subject: ref(patient), encounter: ref(encounter),
               code: { text: typeof d === 'string' ? d : (d.text ?? d.name) } });
  }
  for (const t of p.investigations ?? []) {
    out.push({ resourceType: 'ServiceRequest', status: 'active', intent: 'order',
               subject: ref(patient), encounter: ref(encounter),
               code: { text: typeof t === 'string' ? t : (t.text ?? t.name) } });
  }
  if (p.advice || p.follow_up || p.notes) {
    out.push({ resourceType: 'CarePlan', status: 'active', intent: 'plan',
               subject: ref(patient), encounter: ref(encounter),
               ...(p.advice ? { description: p.advice } : {}),
               ...(p.follow_up ? { period: { start: p.follow_up } } : {}),
               ...(p.notes ? { note: [{ text: p.notes }] } : {}) });
  }
  return out;
}

export function toFhirMessage(kind: Kind, b: Json): Json {
  const enc = id(), pat = id(), doc = id(), org = id();
  const resources: [string, Json][] = [];
  const header = {
    resourceType: 'MessageHeader', id: randomUUID(),
    eventCoding: { system: 'urn:aimscribe:fhir:event', code: EVENT[kind] },
    source: { name: 'CMED', endpoint: 'urn:cmed:server' },
    focus: [ref(enc)],
  };
  resources.push([id(), header]);
  resources.push([enc, {
    resourceType: 'Encounter', status: 'in-progress',
    class: { system: 'http://terminology.hl7.org/CodeSystem/v3-ActCode', code: 'AMB' },
    subject: ref(pat), participant: [{ individual: ref(doc) }],
    serviceProvider: ref(org), period: { start: b.start_time },
  }]);
  const d: Json = b.demographics ?? {};
  resources.push([pat, {
    resourceType: 'Patient', identifier: ident('Patient', b.patient_id),
    ...(d.name ? { name: [{ text: d.name }] } : {}),
    ...(d.sex ? { gender: d.sex } : {}),
    ...(d.date_of_birth ? { birthDate: d.date_of_birth } : {}),
    ...(d.phone ? { telecom: [{ system: 'phone', value: d.phone }] } : {}),
    ...(d.address ? { address: [{ text: d.address }] } : {}),
  }]);
  resources.push([doc, { resourceType: 'Practitioner',
                         identifier: ident('Practitioner', b.doctor_id) }]);
  resources.push([org, { resourceType: 'Organization',
                         identifier: ident('Organization', b.hospital_id) }]);

  if (kind === 'prescription') {
    for (const r of prescriptionResources(b, enc, pat, b.issued_at)) resources.push([id(), r]);
  } else {
    const p: Json = b.paramedic ?? {};
    const when = p.recorded_at;
    const vitals = [
      vital('29463-7', p.weight_kg, 'kg', when), vital('8302-2', p.height_cm, 'cm', when),
      vital('8867-4', p.pulse_bpm, '/min', when), vital('8310-5', p.temperature_c, 'Cel', when),
      vital('59408-5', p.spo2_percent, '%', when),
      vital('30525-0', d.age_years, 'a'),
    ];
    const bp = /^(\d+)\/(\d+)$/.exec(p.blood_pressure ?? '');
    if (bp) vitals.push({
      resourceType: 'Observation', status: 'final',
      code: { coding: [{ system: LOINC, code: '85354-9' }] },
      ...(when ? { effectiveDateTime: when } : {}),
      component: [
        { code: { coding: [{ system: LOINC, code: '8480-6' }] },
          valueQuantity: { value: Number(bp[1]), unit: 'mm[Hg]' } },
        { code: { coding: [{ system: LOINC, code: '8462-4' }] },
          valueQuantity: { value: Number(bp[2]), unit: 'mm[Hg]' } }],
    } as any);
    for (const v of vitals) {
      if (v) resources.push([id(), { ...v, subject: ref(pat), encounter: ref(enc) }]);
    }
    if (p.notes) resources.push([id(), { resourceType: 'Observation', status: 'final',
      subject: ref(pat), encounter: ref(enc), code: { text: 'paramedic notes' },
      note: [{ text: p.notes }] }]);

    const pv = b.previous_visit;
    if (pv && typeof pv === 'object') {
      const prev = id();
      const prevDoc = pv.doctor_id && pv.doctor_id !== b.doctor_id ? id() : doc;
      const prevOrg = pv.hospital_id && pv.hospital_id !== b.hospital_id ? id() : org;
      if (prevDoc !== doc) resources.push([prevDoc, { resourceType: 'Practitioner',
        identifier: ident('Practitioner', pv.doctor_id) }]);
      if (prevOrg !== org) resources.push([prevOrg, { resourceType: 'Organization',
        identifier: ident('Organization', pv.hospital_id) }]);
      resources.push([prev, {
        resourceType: 'Encounter', status: 'finished',
        class: { system: 'http://terminology.hl7.org/CodeSystem/v3-ActCode', code: 'AMB' },
        subject: ref(pat), participant: [{ individual: ref(prevDoc) }],
        serviceProvider: ref(prevOrg), period: { start: pv.start_time ?? pv.date },
      }]);
      for (const r of prescriptionResources(pv.prescription ?? {}, prev, pat)) {
        resources.push([id(), r]);
      }
    }
  }
  return {
    resourceType: 'Bundle', id: randomUUID(), type: 'message',
    timestamp: new Date().toISOString(),
    entry: resources.map(([fullUrl, resource]) => ({ fullUrl, resource })),
  };
}

// ------------------------------------------------------------------ FHIR reply -> plain

export function fromFhirReply(status: number, reply: Json): Json {
  if (reply?.resourceType === 'OperationOutcome') {
    const issue = reply.issue?.[0] ?? {};
    return { status, code: issue.details?.coding?.[0]?.code ?? 'ERROR',
             message: issue.details?.text ?? '', fields: issue.expression, via: 'fhir' };
  }
  const entries = (reply?.entry ?? []).map((e: Json) => e.resource);
  const oo = entries.find((r: Json) => r?.resourceType === 'OperationOutcome');
  const params = entries.find((r: Json) => r?.resourceType === 'Parameters');
  const out: Json = { status, via: 'fhir',
                      message: oo?.issue?.[0]?.details?.text ?? '' };
  for (const p of params?.parameter ?? []) {
    if (p.part) {
      out[p.name] = Object.fromEntries(p.part.map((q: Json) =>
        [q.name, q.valueString ?? q.valueInteger ?? q.valueBoolean]));
    } else {
      out[p.name] = p.valueCode ?? p.valueString ?? p.valueInteger;
    }
  }
  if (oo?.issue?.[0]?.expression) out.fields = oo.issue[0].expression;
  return out;
}
