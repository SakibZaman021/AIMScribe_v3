'use client';

/**
 * The protocol bench (§6.1, §6.2) - for CMED's engineers, not for a clinic.
 *
 * Every message either channel carries, one button each, with the exact
 * request and the exact reply shown side by side. It exists because the
 * integration is judged on codes, not on wording: a page has to act on
 * `RECORDING_PROVISIONAL` the same way it acts on `RECORDING_STARTED`, and
 * on `GATE_NOT_ARMED` by finishing the current consultation first.
 *
 * Nothing here needs a patient. The identifiers say TEST out loud, because a
 * test that has to be run against real patients never gets run.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  AimscribeClient,
  AgentStatus,
  Reply,
  Trigger,
  CODE_NOTES,
  RECORDING_CODES,
} from '@/lib/aimscribe-client';

interface Exchange {
  at: string;
  what: string;
  sent: unknown;
  got: Reply;
}

const nowIso = () => new Date().toISOString();

export default function ProtocolTestPage() {
  const clientRef = useRef<AimscribeClient | null>(null);
  const [connected, setConnected] = useState(false);
  const [status, setStatus] = useState<AgentStatus | null>(null);
  const [log, setLog] = useState<Exchange[]>([]);
  const [busy, setBusy] = useState(false);

  const [patientId, setPatientId] = useState('TESTP0001');
  const [doctorId, setDoctorId] = useState('TESTDR01');
  const [hospitalId, setHospitalId] = useState('CMED-TEST-01');
  const [visit, setVisit] = useState<Trigger | null>(null);

  useEffect(() => {
    const client = new AimscribeClient();
    clientRef.current = client;
    const off = [
      client.on('connection', ({ connected: is }: { connected: boolean }) =>
        setConnected(is)),
      client.on('status', (next: AgentStatus) => setStatus(next)),
    ];
    client.connect();
    const ticker = setInterval(() => void client.refreshStatus(), 5000);
    return () => {
      clearInterval(ticker);
      off.forEach((stop) => stop());
      client.disconnect();
    };
  }, []);

  const record = useCallback((what: string, sent: unknown, got: Reply) => {
    setLog((entries) => [{ at: nowIso(), what, sent, got }, ...entries].slice(0, 40));
  }, []);

  const run = (what: string, sent: unknown, action: () => Promise<Reply>) =>
    async () => {
      setBusy(true);
      try {
        record(what, sent, await action());
      } finally {
        setBusy(false);
      }
    };

  const trigger = useMemo<Trigger>(() => {
    const startedAt = nowIso();
    return {
      patient_id: patientId.trim(),
      doctor_id: doctorId.trim(),
      hospital_id: hospitalId.trim(),
      start_time: startedAt,
      date: startedAt.slice(0, 10),
    };
  }, [patientId, doctorId, hospitalId]);

  async function channelB(kind: 'patient_information' | 'prescription',
                          message: Record<string, unknown>): Promise<Reply> {
    const response = await fetch('/api/cmed/channel-b', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ kind, message }),
    });
    const body = await response.json().catch(() => ({}));
    return {
      ok: response.status < 300,
      status: response.status,
      code: String(body.code ?? ''),
      message: String(body.message ?? ''),
      data: body,
    };
  }

  // ---- the cases ----

  const openPatient = () => {
    const fields = trigger;
    return run('API 1 — start (five fields)', fields, async () => {
      const reply = await clientRef.current!.start(fields);
      if (RECORDING_CODES.includes(reply.code)) setVisit(fields);
      return reply;
    })();
  };

  const sendApi2 = () => {
    const fields = visit ?? trigger;
    const message = {
      ...fields,
      demographics: { name: 'Test Patient', sex: 'female', age_years: 34 },
      paramedic: { blood_pressure: '120/80', pulse_bpm: 78 },
      previous_visit: null,
    };
    return run('API 2 — patient information (Channel B)', message,
               () => channelB('patient_information', message))();
  };

  const sendApi3 = () => {
    const fields = visit ?? trigger;
    const message = {
      ...fields,
      issued_at: nowIso(),
      diagnoses: ['Test diagnosis'],
      investigations: [],
      items: [{ drug: 'Test Medicine', dose: '5 mg', frequency: '1+0+0' }],
    };
    return run('API 3 — prescription (Channel B)', message,
               () => channelB('prescription', message))();
  };

  const armGate = () => {
    const fields = visit ?? trigger;
    return run('API 3 — prescription_built (Channel A)',
               { patient_id: fields.patient_id, session_id: status?.sessionId ?? '' },
               () => clientRef.current!.prescriptionBuilt(
                 fields.patient_id, status?.sessionId ?? ''))();
  };

  const openSecondPatient = () =>
    run('Open the next patient before the prescription is built',
        { ...trigger, patient_id: `${patientId.trim()}B` },
        () => clientRef.current!.start({ ...trigger,
                                         patient_id: `${patientId.trim()}B` }))();

  const missingField = () =>
    run('A trigger with no start_time (should be refused)',
        { patient_id: patientId, doctor_id: doctorId, hospital_id: hospitalId },
        () => clientRef.current!.command('start', {
          trigger: { patient_id: patientId.trim(), doctor_id: doctorId.trim(),
                     hospital_id: hospitalId.trim(), date: nowIso().slice(0, 10) },
        }))();

  const stop = () => run('Stop', {}, () => clientRef.current!.stop())();

  const cases: Array<{ label: string; hint: string; go: () => void; tone: string }> = [
    { label: '1. Open a patient', tone: 'bg-green-600',
      hint: '200 RECORDING_STARTED, or 202 RECORDING_PROVISIONAL while the ' +
            'server is still deciding. Both mean it is recording.',
      go: openPatient },
    { label: '2. Send API 2', tone: 'bg-blue-600',
      hint: '202 ACCEPTED. This is what confirms the recording (§5.6).',
      go: sendApi2 },
    { label: '3. Send API 2 again', tone: 'bg-blue-500',
      hint: '200 ALREADY_RECEIVED — a retry after a timeout is safe.',
      go: sendApi2 },
    { label: '4. Open the next patient too early', tone: 'bg-amber-600',
      hint: '409 GATE_NOT_ARMED — finish this consultation first (§7.7).',
      go: openSecondPatient },
    { label: '5. Prescription built', tone: 'bg-blue-700',
      hint: '200 GATE_ARMED — the next patient may now be opened.',
      go: armGate },
    { label: '6. Send the prescription', tone: 'bg-blue-600',
      hint: '202 ACCEPTED, with the version.', go: sendApi3 },
    { label: '7. A trigger missing a field', tone: 'bg-gray-600',
      hint: '400 MISSING_FIELD, naming the field.', go: missingField },
    { label: '8. Stop', tone: 'bg-gray-700',
      hint: 'Ends the consultation from here. A patient who refuses is stopped ' +
            'on the recorder itself, where the reason list is (§7.8a).',
      go: stop },
  ];

  return (
    <main className="min-h-screen bg-gray-50 p-6">
      <div className="max-w-6xl mx-auto space-y-6">
        <header>
          <h1 className="text-xl font-semibold text-gray-900">
            AIMScribe — protocol bench
          </h1>
          <p className="text-sm text-gray-600 mt-1">
            Every message CMED sends, one button each, with the reply code beside
            it. For integration work; not for a clinic.
          </p>
        </header>

        <section className="bg-white border rounded-xl p-4">
          <div className="flex flex-wrap items-center gap-3 text-sm">
            <span className={`px-2 py-1 rounded-full text-xs ${
              connected ? 'bg-green-100 text-green-800' : 'bg-red-100 text-red-800'}`}>
              {connected ? 'recorder connected' : 'recorder not reachable'}
            </span>
            <span className="text-gray-600">
              state: <strong>{status?.state ?? 'unknown'}</strong>
            </span>
            {status?.sessionId && (
              <span className="text-gray-600 font-mono text-xs">
                session {status.sessionId}
              </span>
            )}
            {status?.confirmation && (
              <span className="text-gray-600">
                confirmation: <strong>{status.confirmation}</strong>
              </span>
            )}
            <span className="text-gray-600">
              clinic on this PC: <strong>{status?.hospitalId ?? '—'}</strong>
            </span>
          </div>

          <div className="grid gap-3 sm:grid-cols-3 mt-4">
            <label className="text-sm text-gray-700">Patient
              <input value={patientId} onChange={(e) => setPatientId(e.target.value)}
                     className="mt-1 w-full border rounded-lg px-3 py-2 text-sm" />
            </label>
            <label className="text-sm text-gray-700">Doctor
              <input value={doctorId} onChange={(e) => setDoctorId(e.target.value)}
                     className="mt-1 w-full border rounded-lg px-3 py-2 text-sm" />
            </label>
            <label className="text-sm text-gray-700">CMED clinic id
              <input value={hospitalId} onChange={(e) => setHospitalId(e.target.value)}
                     className="mt-1 w-full border rounded-lg px-3 py-2 text-sm" />
            </label>
          </div>
          <p className="text-xs text-gray-500 mt-2">
            The clinic the recording is filed under comes from the PC, not from
            here. A clinic id that maps to a different one is refused
            (CLINIC_MISMATCH), which is the behaviour to expect.
          </p>
        </section>

        <section className="grid gap-3 sm:grid-cols-2">
          {cases.map((item) => (
            <div key={item.label} className="bg-white border rounded-xl p-4">
              <button onClick={item.go} disabled={busy}
                      className={`w-full py-2 rounded-lg text-white font-medium ${item.tone} disabled:bg-gray-300`}>
                {item.label}
              </button>
              <p className="text-xs text-gray-600 mt-2">{item.hint}</p>
            </div>
          ))}
        </section>

        <section>
          <h2 className="text-sm font-semibold text-gray-700 mb-2">
            What was sent, and what came back
          </h2>
          {log.length === 0 && (
            <p className="text-sm text-gray-500">Nothing yet.</p>
          )}
          <div className="space-y-3">
            {log.map((entry, index) => (
              <div key={`${entry.at}-${index}`} className="bg-white border rounded-xl p-4">
                <div className="flex flex-wrap items-center gap-3">
                  <span className="text-sm font-medium text-gray-800">{entry.what}</span>
                  <span className={`px-2 py-0.5 rounded text-xs font-mono ${
                    entry.got.ok ? 'bg-green-100 text-green-800'
                                 : 'bg-red-100 text-red-800'}`}>
                    {entry.got.status} {entry.got.code}
                  </span>
                  <span className="text-xs text-gray-500">
                    {CODE_NOTES[entry.got.code] ?? entry.got.message}
                  </span>
                  <span className="text-xs text-gray-400 ml-auto font-mono">
                    {entry.at.slice(11, 19)}
                  </span>
                </div>
                <pre className="mt-2 bg-gray-50 border rounded p-2 text-xs overflow-auto">
{JSON.stringify(entry.sent, null, 2)}
                </pre>
              </div>
            ))}
          </div>
        </section>
      </div>
    </main>
  );
}
