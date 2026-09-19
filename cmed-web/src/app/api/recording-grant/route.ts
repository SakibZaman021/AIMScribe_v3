/**
 * POST /api/recording-grant
 *
 * Issues the signed authorisation the AIMScribe agent needs before it will record.
 * The agent will not start without one, and only this route can produce one.
 *
 * This app stands in for the real CMED site, which triggers a recording when a
 * doctor opens a patient. The real trigger carries five things: doctor, hospital,
 * patient, start time and date. Doctor and hospital travel in the signed grant,
 * because they describe the consultation and neither belongs to the machine.
 *
 * A consulting room runs two shifts. The morning doctors and the afternoon
 * doctors share the same laptops, and a doctor can be moved to another site at a
 * day's notice - so taking either value from the PC's enrolment filed
 * consultations under whoever was enrolled there, silently and in the filename.
 *
 * CMED is the authority on both: doctors log in there and it knows who is on
 * shift. The enrolment still decides whether a machine may record at all, which
 * is the property that actually matters.
 *
 * This route still matters, because the agent refuses to record without a grant
 * - so a random page the doctor visits cannot start a recording, even though
 * this one no longer proves who is asking.
 *
 * Consent is required here, again on the agent, and again by a CHECK constraint
 * in the database.
 */
import { NextRequest, NextResponse } from 'next/server';
import { mintGrant, assertSafeIdentifier } from '@/lib/grant';

// Node runtime: grant signing uses Ed25519 via jose, and the private key must
// never be exposed to an edge deployment we do not control.
export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';

interface GrantRequestBody {
  patient_ref?: string;
  doctor_id?: string;
  hospital_id?: string;
  consent_obtained?: boolean;
  consent_method?: string;
}

export async function POST(request: NextRequest) {
  let body: GrantRequestBody;
  try {
    body = await request.json();
  } catch {
    return NextResponse.json({ error: 'Invalid request body.' }, { status: 400 });
  }

  if (!body.consent_obtained) {
    return NextResponse.json(
      { error: "Record the patient's consent before starting a recording." },
      { status: 400 }
    );
  }

  let patientRef: string;
  let doctorId: string;
  let hospitalId = '';
  try {
    patientRef = assertSafeIdentifier(String(body.patient_ref ?? ''), 'patient_ref');
    // Required. There is no fallback anywhere in the chain: the agent refuses a
    // trigger that names no doctor rather than guessing, because guessing is
    // what filed afternoon consultations under the morning doctor.
    doctorId = assertSafeIdentifier(String(body.doctor_id ?? ''), 'doctor_id');
    if (body.hospital_id) {
      hospitalId = assertSafeIdentifier(String(body.hospital_id), 'hospital_id');
    }
  } catch (error) {
    return NextResponse.json(
      { error: error instanceof Error ? error.message : 'Invalid identifier.' },
      { status: 400 }
    );
  }

  try {
    const { grant, expiresIn } = await mintGrant({
      doctorId,
      doctorName: '',
      hospitalId,
      patientRef,
      consentObtained: true,
      consentMethod: String(body.consent_method ?? 'verbal_at_reception').slice(0, 64),
    });

    return NextResponse.json(
      { grant, expires_in: expiresIn },
      { headers: { 'Cache-Control': 'no-store' } }
    );
  } catch (error) {
    // The message may name the missing environment variable, which belongs in
    // the log and not in a response to the browser.
    console.error('[grant] mint failed:', error);
    return NextResponse.json(
      { error: 'Recording could not be authorised. Contact support.' },
      { status: 500 }
    );
  }
}
