'use client';

/**
 * Manual test - every scenario discussed for labelling, step by step.
 *
 * Each card is one scenario: numbered steps, a button that does each step the
 * way CMED's page would (API 1 / 3a to the recorder on this PC, API 2 / 3b to
 * the AIMS LAB server through this app's server route, which holds CMED's
 * key), the replies as they arrive, and the expected result in plain words.
 *
 * Patient ids start with TEST_ and carry the time the scenario began, so a run
 * never collides with another and everything it made is easy to find and to
 * remove afterwards.
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import {
  AimscribeClient,
  AgentStatus,
  Reply,
  Trigger,
  CODE_NOTES,
  RECORDING_CODES,
  isContinued,
  stopReason,
} from '@/lib/aimscribe-client';

type Step =
  | { kind: 'open'; patient: string; note?: string; skipApi2?: boolean }
  | { kind: 'rx'; patient: string; note?: string }
  | { kind: 'wait'; seconds: number; note: string }
  | { kind: 'api2'; patient: string; note?: string }
  | { kind: 'stop'; note?: string }
  | { kind: 'prev'; variant: 'first' | 'same' | 'changed'; note?: string };

interface Scenario {
  id: string;
  title: string;
  why: string;
  steps: Step[];
  expect: string;
}

const SCENARIOS: Scenario[] = [
  {
    id: 'NORMAL', title: '1. Normal consultation',
    why: 'One patient, a prescription, then the next patient.',
    steps: [
      { kind: 'open', patient: 'A' },
      { kind: 'wait', seconds: 30, note: 'Talk for about 30 seconds.' },
      { kind: 'rx', patient: 'A' },
      { kind: 'open', patient: 'B', note: 'The next patient: a new recording starts.' },
      { kind: 'stop' },
    ],
    expect: 'A is RECORDING_STARTED; the prescription is GATE_ARMED; B starts a NEW ' +
            'recording (a different session id). On the server: one file for A (rule R1) ' +
            'and a short one for B.',
  },
  {
    id: 'WRONG', title: '2. Wrong patient picked, corrected within seconds',
    why: 'The doctor opens A by mistake and corrects to B before talking.',
    steps: [
      { kind: 'open', patient: 'A', note: 'The mistake.' },
      { kind: 'wait', seconds: 8, note: 'Realise it within a few seconds.' },
      { kind: 'open', patient: 'B', note: 'The right patient.' },
      { kind: 'wait', seconds: 40, note: 'Consult B for about 40 seconds.' },
      { kind: 'rx', patient: 'B' },
      { kind: 'stop' },
    ],
    expect: 'B is accepted with continued and the SAME session id as A. Both API 2s are ' +
            'ACCEPTED. On the server: ONE file, under B (rule R3+R1). A\'s API 2 is kept, ' +
            'with no audio.',
  },
  {
    id: 'BACK', title: '3. Deliberate click on another profile, then back',
    why: 'The right patient is open; the doctor looks at another file and returns.',
    steps: [
      { kind: 'open', patient: 'A' },
      { kind: 'wait', seconds: 40, note: 'Consult A for about 40 seconds.' },
      { kind: 'open', patient: 'B', note: 'Look at another profile.' },
      { kind: 'wait', seconds: 10, note: 'Look at it for about 10 seconds.' },
      { kind: 'open', patient: 'A', note: 'Back to A.' },
      { kind: 'rx', patient: 'A' },
      { kind: 'stop' },
    ],
    expect: 'Every click is accepted with continued and the same session. On the server: ' +
            'ONE file, under A (rule R2: the doctor came back). B\'s API 2 is kept, with no ' +
            'audio.',
  },
  {
    id: 'GLANCE', title: '4. Glance in the middle of a consultation',
    why: 'Well into A\'s consultation the doctor glances at B, then writes A\'s prescription.',
    steps: [
      { kind: 'open', patient: 'A' },
      { kind: 'wait', seconds: 90, note: 'Consult A for about 90 seconds.' },
      { kind: 'open', patient: 'B', note: 'A glance at B.' },
      { kind: 'wait', seconds: 10, note: 'About 10 seconds.' },
      { kind: 'rx', patient: 'A', note: 'A\'s prescription, without reopening A.' },
      { kind: 'stop' },
    ],
    expect: 'ONE file, under A (rule R2: a prescription for A counts as coming back).',
  },
  {
    id: 'FOLLOW', title: '5. Follow-up with no prescription, then the next patient',
    why: 'A is a follow-up and gets no prescription; B comes in next.',
    steps: [
      { kind: 'open', patient: 'A' },
      { kind: 'wait', seconds: 70, note: 'Consult A for about 70 seconds. No prescription.' },
      { kind: 'open', patient: 'B', note: 'The next patient (accepted, recording carries on).' },
      { kind: 'wait', seconds: 40, note: 'Consult B for about 40 seconds.' },
      { kind: 'rx', patient: 'B' },
      { kind: 'open', patient: 'C', note: 'The patient after B: a new recording.' },
      { kind: 'stop' },
    ],
    expect: 'B is accepted with continued (same session as A). C starts a new recording. On ' +
            'the server the first recording becomes TWO files: A up to B\'s click (rule R4) ' +
            'and B from that click (rule R1). They join back exactly into the signed original.',
  },
  {
    id: 'COUNSEL', title: '6. Counselling limit after the prescription',
    why: 'After the prescription nobody is opened - the patient left, the next is late.',
    steps: [
      { kind: 'open', patient: 'A' },
      { kind: 'wait', seconds: 20, note: 'Talk for about 20 seconds.' },
      { kind: 'rx', patient: 'A' },
      { kind: 'wait', seconds: 135, note: 'Click nothing for 2 minutes 15 seconds; keep talking.' },
    ],
    expect: 'About 2:00-2:10 after the prescription the event feed shows "Recording ended: ' +
            'Ended by itself 2 minutes after the prescription". No Stop needed.',
  },
  {
    id: 'IDLE', title: '7. No-activity limit (15 minutes)',
    why: 'A follow-up with no prescription, and no click afterwards: the recording must not run all day.',
    steps: [
      { kind: 'open', patient: 'A' },
      { kind: 'wait', seconds: 900, note: 'Leave it for 15 minutes with no clicks (or skip: press Stop).' },
      { kind: 'stop', note: 'Only if you skipped the wait.' },
    ],
    expect: 'If you wait: after 15 minutes with no CMED activity the feed shows "Recording ' +
            'ended: Ended by itself after 15 minutes with no activity in CMED". The file is ' +
            'kept under A (rule R4).',
  },
  {
    id: 'TWICE', title: '8. The same patient clicked twice',
    why: 'A double click, a reload, or the profile reopened.',
    steps: [
      { kind: 'open', patient: 'A' },
      { kind: 'open', patient: 'A', note: 'Again, before the prescription.' },
      { kind: 'rx', patient: 'A' },
      { kind: 'open', patient: 'A', note: 'Again, after the prescription.' },
      { kind: 'stop' },
    ],
    expect: 'The second click is RECORDING_STARTED with continued and the same session (no ' +
            'new recording). The click after the prescription is SESSION_ALREADY_ACTIVE. One ' +
            'file for A.',
  },
  {
    id: 'LATE', title: '9. API 2 arriving late',
    why: 'CMED\'s server was offline; patient information arrives after the consultation.',
    steps: [
      { kind: 'open', patient: 'A', skipApi2: true, note: 'API 1 only - no API 2 yet.' },
      { kind: 'wait', seconds: 30, note: 'Consult for about 30 seconds.' },
      { kind: 'rx', patient: 'A' },
      { kind: 'stop' },
      { kind: 'api2', patient: 'A', note: 'Now send A\'s API 2, late.' },
    ],
    expect: 'The recording is kept even though API 2 is missing. The late API 2 is ACCEPTED ' +
            'and attaches to the recording, which becomes confirmed. Nothing is deleted.',
  },
  {
    id: 'PREV', title: '10. Previous-visit sync',
    why: 'API 2 carries the patient\'s previous prescription; the server stores it once.',
    steps: [
      { kind: 'prev', variant: 'first', note: 'Today\'s API 2, with a previous visit.' },
      { kind: 'prev', variant: 'same', note: 'Another API 2, the same previous visit.' },
      { kind: 'prev', variant: 'changed', note: 'Another, the previous prescription corrected.' },
    ],
    expect: 'previous_visit.status: STORED (or ALREADY_STORED if run before), then ' +
            'ALREADY_STORED, then UPDATED with the next version. No recording is needed.',
  },
];

interface LogLine {
  at: string;
  step: string;
  code: string;
  ok: boolean;
  detail: string;
}

const now = () => new Date().toISOString();
const clock = (iso: string) => iso.slice(11, 19);

async function channelB(kind: 'patient_information' | 'prescription',
                        message: Record<string, unknown>): Promise<Reply> {
  try {
    const response = await fetch('/api/cmed/channel-b', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ kind, message }),
    });
    const body = await response.json().catch(() => ({}));
    return { ok: response.status < 300, status: response.status,
             code: String(body.code ?? ''), message: String(body.message ?? ''), data: body };
  } catch (error) {
    return { ok: false, status: 0, code: 'NOT_DELIVERED',
             message: error instanceof Error ? error.message : String(error) };
  }
}

function api2Body(fields: Trigger, previous: Record<string, unknown> | null) {
  return {
    ...fields,
    demographics: { name: `Test ${fields.patient_id}`, sex: 'female', age_years: 34 },
    paramedic: { blood_pressure: '120/80', pulse_bpm: 78 },
    previous_visit: previous,
  };
}

function prescriptionBody(fields: Trigger) {
  return {
    ...fields,
    issued_at: now(),
    diagnoses: ['Test diagnosis'],
    investigations: [],
    items: [{ drug: 'Test Medicine', dose: '5 mg', frequency: '1+0+0', duration: '7 days' }],
    advice: 'Test advice',
  };
}

function describe(step: Step): string {
  switch (step.kind) {
    case 'open': return `Open patient ${step.patient}` + (step.skipApi2 ? ' (API 1 only)' : '');
    case 'rx': return `Build ${step.patient}'s prescription (3a + 3b)`;
    case 'wait': return step.note;
    case 'api2': return `Send ${step.patient}'s API 2`;
    case 'stop': return 'Stop the recording';
    case 'prev': return {
      first: 'API 2 with a previous visit',
      same: 'API 2 with the same previous visit',
      changed: 'API 2 with the previous prescription changed',
    }[step.variant];
  }
}

function ScenarioCard({ scenario, client, doctorId, hospitalId, status, connected }: {
  scenario: Scenario; client: AimscribeClient | null; doctorId: string;
  hospitalId: string; status: AgentStatus | null; connected: boolean;
}) {
  const [tag, setTag] = useState('');
  const [done, setDone] = useState<number[]>([]);
  const [log, setLog] = useState<LogLine[]>([]);
  const [busy, setBusy] = useState(false);
  const [countdown, setCountdown] = useState<{ step: number; left: number } | null>(null);
  const patients = useRef<Record<string, Trigger>>({});
  const sessions = useRef<Record<string, string>>({});
  const prevStart = useRef<string>('');

  useEffect(() => {
    if (!countdown || countdown.left <= 0) return;
    const timer = setTimeout(() => setCountdown({ ...countdown, left: countdown.left - 1 }), 1000);
    return () => clearTimeout(timer);
  }, [countdown]);

  const note = (step: string, reply: Reply, extra = '') =>
    setLog((lines) => [...lines, {
      at: now(), step, code: `${reply.status} ${reply.code}`, ok: reply.ok,
      detail: [CODE_NOTES[reply.code] ?? reply.message, extra].filter(Boolean).join(' · '),
    }]);

  const begin = () => {
    const stamp = new Date();
    const hhmmss = [stamp.getHours(), stamp.getMinutes(), stamp.getSeconds()]
      .map((n) => String(n).padStart(2, '0')).join('');
    setTag(hhmmss);
    setDone([]);
    setLog([]);
    setCountdown(null);
    patients.current = {};
    sessions.current = {};
    // A previous visit some weeks back, the same for the whole run.
    const earlier = new Date(Date.now() - 21 * 86400 * 1000);
    earlier.setMilliseconds(0);
    prevStart.current = earlier.toISOString().replace('.000Z', '+00:00');
  };

  const fieldsFor = (patient: string): Trigger => {
    const known = patients.current[patient];
    if (known) return known;                // reopening keeps the same visit
    const startedAt = now();
    const fields: Trigger = {
      patient_id: `TEST_${scenario.id}_${patient}_${tag}`,
      doctor_id: doctorId.trim(),
      hospital_id: hospitalId.trim() || status?.hospitalId || '',
      start_time: startedAt,
      date: startedAt.slice(0, 10),
    };
    patients.current[patient] = fields;
    return fields;
  };

  const run = async (index: number) => {
    const step = scenario.steps[index];
    if (!client) return;
    setBusy(true);
    try {
      const label = `${index + 1}. ${describe(step)}`;
      if (step.kind === 'open') {
        const fields = fieldsFor(step.patient);
        const reply = await client.start(fields);
        const session = String(reply.data?.session_id ?? '');
        if (RECORDING_CODES.includes(reply.code) && session) sessions.current[step.patient] = session;
        note(label, reply, [session && `session …${session.slice(-6)}`,
                            isContinued(reply) && 'continued: the recording carries on',
                            reply.data?.supersedes && 'a new recording (the last one closed)']
          .filter(Boolean).join(' · '));
        if (!step.skipApi2 && reply.ok) {
          note(`   ${step.patient}'s API 2`, await channelB('patient_information',
                                                         api2Body(fields, null)));
        }
      } else if (step.kind === 'rx') {
        const fields = fieldsFor(step.patient);
        const session = sessions.current[step.patient] || status?.sessionId || '';
        note(label + ' - 3a', await client.prescriptionBuilt(fields.patient_id, session));
        note(`   ${step.patient}'s API 3b`, await channelB('prescription', prescriptionBody(fields)));
      } else if (step.kind === 'wait') {
        setCountdown({ step: index, left: step.seconds });
      } else if (step.kind === 'api2') {
        note(label, await channelB('patient_information',
                                   api2Body(fieldsFor(step.patient), null)));
      } else if (step.kind === 'stop') {
        note(label, await client.stop());
      } else if (step.kind === 'prev') {
        // Each API 2 is a new visit today, for the same patient, carrying the
        // same earlier visit - the way CMED backfills history.
        const startedAt = now();
        const fields: Trigger = {
          patient_id: `TEST_${scenario.id}_P_${tag}`,
          doctor_id: doctorId.trim(),
          hospital_id: hospitalId.trim() || status?.hospitalId || '',
          start_time: startedAt, date: startedAt.slice(0, 10),
        };
        const previous = {
          doctor_id: doctorId.trim(), hospital_id: fields.hospital_id,
          start_time: prevStart.current, date: prevStart.current.slice(0, 10),
          prescription: {
            items: [{ drug: 'Earlier Medicine',
                      dose: step.variant === 'changed' ? '10 mg' : '5 mg',
                      frequency: '1+0+1', duration: '14 days' }],
            diagnoses: ['Earlier diagnosis'], investigations: [], advice: 'Rest',
          },
        };
        const reply = await channelB('patient_information', api2Body(fields, previous));
        const prev = (reply.data as any)?.previous_visit;
        note(label, reply, prev ? `previous_visit: ${prev.status ?? ''}` +
                                  (prev.version ? ` (version ${prev.version})` : '') : '');
      }
      setDone((list) => (list.includes(index) ? list : [...list, index]));
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="bg-white border rounded-xl p-4">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h2 className="font-semibold text-gray-900">{scenario.title}</h2>
        <button onClick={begin} disabled={busy}
                className="text-sm px-3 py-1 rounded-lg bg-gray-800 text-white disabled:bg-gray-300">
          {tag ? 'Begin again' : 'Begin'}
        </button>
      </div>
      <p className="text-sm text-gray-600 mt-1">{scenario.why}</p>
      {tag && (
        <p className="text-xs text-gray-500 mt-1 font-mono">
          patients: {Array.from(new Set(scenario.steps.flatMap((s) =>
            'patient' in s ? [`TEST_${scenario.id}_${s.patient}_${tag}`] : [])))
            .join(', ') || `TEST_${scenario.id}_P_${tag}`}
        </p>
      )}

      <ol className="mt-3 space-y-2">
        {scenario.steps.map((step, index) => (
          <li key={index} className="flex flex-wrap items-center gap-2 text-sm">
            <button onClick={() => run(index)}
                    disabled={!tag || busy || (!connected && step.kind !== 'prev' && step.kind !== 'api2')}
                    className={`px-3 py-1 rounded-lg text-white ${
                      done.includes(index) ? 'bg-green-600' : 'bg-blue-600'} disabled:bg-gray-300`}>
              {index + 1}
            </button>
            <span className="text-gray-800">{describe(step)}</span>
            {'note' in step && step.note && step.kind !== 'wait' && (
              <span className="text-gray-500">- {step.note}</span>
            )}
            {countdown && countdown.step === index && (
              <span className={`font-mono ${countdown.left > 0 ? 'text-amber-700' : 'text-green-700'}`}>
                {countdown.left > 0 ? `${countdown.left} s` : 'time - go on'}
              </span>
            )}
          </li>
        ))}
      </ol>

      <div className="mt-3 p-3 rounded-lg bg-green-50 border border-green-200 text-sm text-green-900">
        <span className="font-medium">Expected: </span>{scenario.expect}
      </div>

      {log.length > 0 && (
        <div className="mt-3 space-y-1 font-mono text-xs">
          {log.map((line, index) => (
            <div key={index} className={line.ok ? 'text-gray-700' : 'text-red-700'}>
              {clock(line.at)} {line.step} → {line.code}{line.detail ? ` - ${line.detail}` : ''}
            </div>
          ))}
        </div>
      )}
    </section>
  );
}

export default function ManualTestPage() {
  const clientRef = useRef<AimscribeClient | null>(null);
  const [client, setClient] = useState<AimscribeClient | null>(null);
  const [connected, setConnected] = useState(false);
  const [status, setStatus] = useState<AgentStatus | null>(null);
  const [doctorId, setDoctorId] = useState('DR0042');
  const [hospitalId, setHospitalId] = useState('');
  const [events, setEvents] = useState<string[]>([]);

  const feed = useCallback((line: string) =>
    setEvents((list) => [`${clock(now())} ${line}`, ...list].slice(0, 30)), []);

  useEffect(() => {
    const c = new AimscribeClient();
    clientRef.current = c;
    setClient(c);
    const off = [
      c.on('connection', ({ connected: is }: { connected: boolean }) => {
        setConnected(is);
        if (is) {
          c.doctors().then((register) => {
            setHospitalId((current) => current || register.hospitalId || '');
          }).catch(() => undefined);
        }
      }),
      c.on('status', (next: AgentStatus) => setStatus(next)),
      c.on('recording_started', (m: any) =>
        feed(`recording started …${String(m.session_id ?? '').slice(-6)}`)),
      c.on('patient_opened', (m: any) => feed(`click noted: ${m.patient_ref ?? ''}`)),
      c.on('gate_armed', () => feed('prescription signal: armed')),
      c.on('recording_stopped', (m: any) =>
        feed(`Recording ended: ${stopReason(String(m.reason ?? ''))}`)),
      c.on('integrity_alert', (m: any) => feed(`alert: ${m.alert_type ?? ''} ${m.detail ?? ''}`)),
    ];
    c.connect();
    const ticker = setInterval(() => void c.refreshStatus(), 3000);
    return () => {
      clearInterval(ticker);
      off.forEach((stop) => stop());
      c.disconnect();
    };
  }, [feed]);

  return (
    <main className="min-h-screen bg-gray-50 p-6">
      <div className="max-w-5xl mx-auto space-y-5">
        <header>
          <h1 className="text-xl font-semibold text-gray-900">AIMScribe - manual test</h1>
          <p className="text-sm text-gray-600 mt-1">
            Every scenario, one card each. Press <b>Begin</b>, then each numbered step in
            order. Each button does what CMED&apos;s page would: API 1 and the prescription
            signal go to the recorder on this PC; API 2 and the prescription go to the AIMS
            LAB server. Compare the replies with <b>Expected</b>. Test patients start with
            TEST_, so they are easy to remove afterwards.
          </p>
          <p className="text-sm text-gray-600 mt-1">
            The files each recording became are on the AIMS LAB server once it closes: see
            the local manual, or run <code>python tools/cmed_build_session.py</code>.
            One scenario at a time - wait for the last one&apos;s recording to end.
          </p>
        </header>

        <section className="bg-white border rounded-xl p-4 grid gap-3 sm:grid-cols-2">
          <div className="text-sm space-y-1">
            <div>
              <span className={`px-2 py-0.5 rounded-full text-xs ${
                connected ? 'bg-green-100 text-green-800' : 'bg-red-100 text-red-800'}`}>
                {connected ? 'recorder connected' : 'recorder not reachable'}
              </span>
            </div>
            <div>state: <b>{status?.state ?? 'unknown'}</b>
              {status?.armed ? ' · prescription built (armed)' : ''}</div>
            <div className="font-mono text-xs">
              session: {status?.sessionId ?? '-'}<br />
              opened for: {status?.patientRef ?? '-'}<br />
              now on: {status?.focusPatient ?? '-'}
            </div>
          </div>
          <div className="grid gap-2 text-sm">
            <label>Doctor id
              <input value={doctorId} onChange={(e) => setDoctorId(e.target.value)}
                     className="mt-1 w-full border rounded-lg px-3 py-1.5" />
            </label>
            <label>CMED clinic id (from this PC by default)
              <input value={hospitalId} onChange={(e) => setHospitalId(e.target.value)}
                     className="mt-1 w-full border rounded-lg px-3 py-1.5" />
            </label>
          </div>
        </section>

        <section className="bg-white border rounded-xl p-4">
          <h2 className="text-sm font-semibold text-gray-700">What the recorder reports</h2>
          {events.length === 0
            ? <p className="text-sm text-gray-500 mt-1">Nothing yet.</p>
            : <div className="mt-1 font-mono text-xs space-y-0.5">
                {events.map((line, index) => <div key={index}>{line}</div>)}
              </div>}
        </section>

        {SCENARIOS.map((scenario) => (
          <ScenarioCard key={scenario.id} scenario={scenario} client={client}
                        doctorId={doctorId} hospitalId={hospitalId}
                        status={status} connected={connected} />
        ))}
      </div>
    </main>
  );
}
