'use client';

/**
 * Doctor dashboard.
 *
 * Rewritten for AIMScribe protocol 2. What changed and why:
 *
 *  - The browser no longer names the doctor or the hospital. It asks this app's
 *    server for a signed grant, and the agent takes identity from that. A page
 *    the doctor happens to visit can no longer start a recording, and a typo can
 *    no longer file a consultation under the wrong hospital.
 *  - Consent is captured before recording can begin, and travels in the grant.
 *  - Pause is a first-class action with a mandatory reason, and a supervisor's
 *    name once it runs long. The pause is written into the recording's hash chain,
 *    so the gap in the audio is explained rather than unaccounted for.
 *  - Commands are acknowledged; state changes arrive as events. v1 conflated the
 *    two, so every change was processed two or three times.
 */

import { useState, useEffect, useCallback, useRef, useMemo } from 'react';
import { useRouter } from 'next/navigation';
import axios from 'axios';
import {
  AimscribeClient,
  AgentStatus,
  DoctorRegister,
  PAUSE_REASONS,
} from '@/lib/aimscribe-client';

const BACKEND_API = process.env.NEXT_PUBLIC_BACKEND_URL
  ? `${process.env.NEXT_PUBLIC_BACKEND_URL}/api/v1`
  : 'http://localhost:6000/api/v1';

/** Beyond this the agent requires a supervisor's name. Matches its default. */
const SUPERVISOR_THRESHOLD_SECONDS = 300;

interface PatientData {
  patient_id: string;
  patient_name: string;
  age: string;
  gender: string;
  health_screening: Record<string, string>;
}



interface NERFields {
  chief_complaints?: { data: string[] };
  drug_history?: { data: string[] };
  on_examination?: { data: string[] };
  systemic_examination?: { data: string[] };
  investigations?: { data: string[] };
  diagnosis?: { data: string[] };
  medications?: { data: any[] };
  advice?: { data: string[] };
  follow_up?: { data: string[] };
  additional_notes?: { data: string[] };
}

export default function DashboardPage() {
  const router = useRouter();

  const [patientData, setPatientData] = useState<PatientData | null>(null);
  // No patient in this tab. Not necessarily a reason to leave: a recording may
  // be running that the doctor still needs to stop.
  const [patientMissing, setPatientMissing] = useState(false);

  const clientRef = useRef<AimscribeClient | null>(null);
  const [connected, setConnected] = useState(false);
  const [status, setStatus] = useState<AgentStatus | null>(null);
  const [busy, setBusy] = useState(false);

  const [consentGiven, setConsentGiven] = useState(false);
  // The consulting room is fixed, the doctor in it is not. The register comes
  // from the agent; the choice is remembered for the session so a doctor seeing
  // twenty patients picks their name once, not twenty times.
  const [register, setRegister] = useState<DoctorRegister | null>(null);
  // Typed here because this stands in for CMED. On the real site these arrive
  // with the trigger from the doctor's own login, and are never chosen by hand.
  const [doctorId, setDoctorId] = useState('');
  const [hospitalId, setHospitalId] = useState('');
  const [showPause, setShowPause] = useState(false);
  // Explicitly widened: PAUSE_REASONS is `as const`, so inference would pin this
  // to the first literal and reject every other reason.
  const [pauseReason, setPauseReason] = useState<string>(PAUSE_REASONS[0].value);
  const [pauseDetail, setPauseDetail] = useState('');
  const [supervisor, setSupervisor] = useState('');
  const [expectedMinutes, setExpectedMinutes] = useState(2);

  const [nerData, setNerData] = useState<NERFields | null>(null);
  const [nerVersion, setNerVersion] = useState(0);
  // The agent reports no session once recording stops, but the transcript and
  // prescription for the last clips arrive after that. Hold the id so the
  // results can still be collected.
  const [resultsFor, setResultsFor] = useState<string | null>(null);
  const [nerFinal, setNerFinal] = useState(false);
  const [editedFields, setEditedFields] = useState<Record<string, string>>({});
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  // ---- patient ----
  //
  // The doctor and hospital are not fetched or chosen here. They arrive with
  // the agent's status, from the enrolment an administrator performed on this
  // machine, so the page displays them rather than deciding them.

  useEffect(() => {
    const stored = sessionStorage.getItem('patientData');
    if (stored) {
      try {
        setPatientData(JSON.parse(stored));
      } catch {
        router.push('/');
      }
    } else {
      router.push('/');
    }

  }, [router]);

  // ---- agent connection ----

  useEffect(() => {
    const client = new AimscribeClient();
    clientRef.current = client;

    const unsubscribe = [
      client.on('connection', ({ connected }: { connected: boolean }) => {
        setConnected(connected);
        if (connected) {
          setError(null);
          // Ask on every connect, not once: a doctor added this morning should
          // appear without anyone restarting anything.
          client
            .doctors()
            .then((next) => {
              setRegister(next);
              // Pre-select the room's usual doctor, so the common case stays one
              // click - and never overwrite a choice already made.
              setDoctorId((current) => {
                if (current && next.doctors.some((d) => d.doctorId === current)) return current;
                const assigned = next.assignedDoctorId ?? '';
                return next.doctors.some((d) => d.doctorId === assigned) ? assigned : '';
              });
            })
            .catch(() => setRegister({ hospitalId: null, assignedDoctorId: null, doctors: [] }));
        }
      }),
      client.on('status', (next: AgentStatus) => setStatus(next)),
      client.on('recording_started', () => {
        setNerData(null);
        setNerVersion(0);
        setNotice('Recording started.');
        client.refreshStatus();
      }),
      client.on('recording_paused', (message: any) => {
        setNotice(`Paused: ${String(message.reason ?? '').replace(/_/g, ' ')}`);
        client.refreshStatus();
      }),
      client.on('recording_resumed', () => {
        setNotice('Recording resumed.');
        client.refreshStatus();
      }),
      client.on('recording_stopped', (message: any) => {
        // A recording can be stopped from the agent's tray icon, or cut short
        // because the next patient was started. Saying so here is the only way
        // the person in front of the screen finds out - otherwise a consultation
        // ends mid-sentence and the page reports a tidy success.
        const reason = String(message?.reason ?? '');
        if (reason && reason !== 'doctor_stopped') {
          setNotice(
            `Recording stopped from outside CMED (${reason.replace(/_/g, ' ')}). ` +
              'The audio so far is saved and queued for upload.'
          );
        } else {
          setNotice('Recording stopped and queued for upload.');
        }
        client.refreshStatus();
      }),
      client.on('segment_sealed', () => client.refreshStatus()),
      client.on('segment_committed', () => client.refreshStatus()),
      client.on('ner_ready', (message: any) => {
        if (Number(message.version ?? 0) > nerVersion) {
          setNerData(message.ner);
          setNerVersion(Number(message.version));
        }
      }),
      client.on('integrity_alert', (message: any) => {
        // These are the doctor's business: a muted microphone means the
        // consultation is not being captured.
        setError(`AIMScribe: ${message.detail ?? message.alert_type}`);
      }),
      client.on('error', (message: any) => setError(message.message ?? 'AIMScribe error')),
    ];

    client.connect();

    const ticker = setInterval(() => client.refreshStatus(), 5000);

    return () => {
      clearInterval(ticker);
      unsubscribe.forEach((off) => off());
      // Recording continues in the agent regardless of this page.
      client.disconnect();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // ---- NER polling (fallback when the webhook cannot reach us) ----

  // Follow the active session, and keep following it after it ends.
  useEffect(() => {
    if (status?.sessionId && status.sessionId !== resultsFor) {
      setResultsFor(status.sessionId);
      setNerFinal(false);
      setNerVersion(0);
      setNerData(null);
    }
  }, [status?.sessionId, resultsFor]);

  useEffect(() => {
    // Polling used to stop the moment recording did. The last clip is still
    // being transcribed at that point, so the doctor pressed Stop and the
    // prescription never appeared - the one moment they actually need it.
    // It now runs until the backend says the results are final.
    if (!resultsFor || nerFinal) return;

    const fetchResults = async () => {
      try {
        const response = await axios.get(`${BACKEND_API}/ner/${resultsFor}`);
        const data = response.data;
        if (data.version > nerVersion && data.fields) {
          setNerData(normalizeNerData(data.fields));
          setNerVersion(data.version);
        }
        if (data.is_final) setNerFinal(true);
      } catch {
        try {
          const fallback = await axios.get(`/api/webhook/ner?session_id=${resultsFor}`);
          if (fallback.data.version > nerVersion && fallback.data.ner) {
            setNerData(fallback.data.ner);
            setNerVersion(fallback.data.version);
          }
        } catch {
          /* both unavailable; try again next tick */
        }
      }
    };

    fetchResults();
    const poll = setInterval(fetchResults, 4000);
    return () => clearInterval(poll);
  }, [resultsFor, nerFinal, nerVersion]);

  // ---- derived ----

  const recordingThisPatient = useMemo(
    () => Boolean(status?.isRecording && patientData &&
                  status.patientRef === patientData.patient_id),
    [status, patientData]
  );

  const recordingOtherPatient = useMemo(
    () => Boolean(status?.isRecording && patientData &&
                  status.patientRef && status.patientRef !== patientData.patient_id),
    [status, patientData]
  );

  const needsSupervisor = expectedMinutes * 60 > SUPERVISOR_THRESHOLD_SECONDS;

  // ---- actions ----

  const withBusy = async (action: () => Promise<unknown>) => {
    setBusy(true);
    setError(null);
    try {
      await action();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Something went wrong.');
    } finally {
      setBusy(false);
    }
  };

  const handleStart = () =>
    withBusy(async () => {
      if (!patientData) throw new Error('No patient selected.');
      if (!consentGiven) {
        throw new Error('Confirm the patient has agreed to be recorded before starting.');
      }
      if (!doctorId.trim()) {
        throw new Error('Enter the doctor ID, as CMED would send it.');
      }
      await clientRef.current!.start({
        patientRef: patientData.patient_id,
        patientName: patientData.patient_name,
        doctorId,
        hospitalId,
        consentObtained: true,
        consentMethod: 'verbal_at_reception',
      });
    });

  const handlePause = () =>
    withBusy(async () => {
      if (pauseReason === 'other' && !pauseDetail.trim()) {
        throw new Error('Describe the reason when choosing "Other".');
      }
      if (needsSupervisor && !supervisor.trim()) {
        throw new Error(
          `A pause longer than ${SUPERVISOR_THRESHOLD_SECONDS / 60} minutes needs a supervisor's name.`
        );
      }
      await clientRef.current!.pause({
        reason: pauseReason,
        reasonDetail: pauseDetail,
        authorisedBy: supervisor,
        expectedSeconds: expectedMinutes * 60,
      });
      setShowPause(false);
      setPauseDetail('');
      setSupervisor('');
    });

  const handleResume = () => withBusy(() => clientRef.current!.resume());

  const handleStop = () =>
    withBusy(async () => {
      if (!window.confirm('Stop recording this consultation?')) return;
      await clientRef.current!.stop();
    });

  const handleSavePrescription = () =>
    withBusy(async () => {
      if (!status?.sessionId) throw new Error('No active session.');
      await axios.post(`${BACKEND_API}/prescription`, {
        session_id: status.sessionId,
        doctor_id: status.doctorId ?? '',
        prescription: { ...nerData, ...editedFields },
      });
      setNotice('Prescription saved.');
    });

  const renderPauseDialog = () => (
    <div className="fixed inset-0 bg-black/40 flex items-center justify-center p-4 z-50">
          <div className="bg-white rounded-xl shadow-xl max-w-md w-full p-6">
            <h3 className="text-lg font-semibold text-gray-800">Pause Recording</h3>
            <p className="text-sm text-gray-600 mt-1">
              The pause, its reason, and who authorised it are recorded with the
              consultation.
            </p>

            <label className="block text-sm font-medium text-gray-700 mt-4 mb-1">Reason</label>
            <select
              value={pauseReason}
              onChange={(e) => setPauseReason(e.target.value)}
              className="w-full px-3 py-2 border border-gray-300 rounded-lg"
            >
              {PAUSE_REASONS.map((reason) => (
                <option key={reason.value} value={reason.value}>{reason.label}</option>
              ))}
            </select>

            <label className="block text-sm font-medium text-gray-700 mt-4 mb-1">
              Detail {pauseReason === 'other' && <span className="text-red-600">*</span>}
            </label>
            <textarea
              rows={2}
              value={pauseDetail}
              onChange={(e) => setPauseDetail(e.target.value)}
              className="w-full px-3 py-2 border border-gray-300 rounded-lg"
              placeholder="Optional note for the record"
            />

            <label className="block text-sm font-medium text-gray-700 mt-4 mb-1">
              Expected length
            </label>
            <select
              value={expectedMinutes}
              onChange={(e) => setExpectedMinutes(Number(e.target.value))}
              className="w-full px-3 py-2 border border-gray-300 rounded-lg"
            >
              {[1, 2, 5, 10, 20].map((minutes) => (
                <option key={minutes} value={minutes}>{minutes} minute(s)</option>
              ))}
            </select>

            {needsSupervisor && (
              <div className="mt-4">
                <label className="block text-sm font-medium text-gray-700 mb-1">
                  Authorising supervisor <span className="text-red-600">*</span>
                </label>
                <input
                  value={supervisor}
                  onChange={(e) => setSupervisor(e.target.value)}
                  className="w-full px-3 py-2 border border-gray-300 rounded-lg"
                  placeholder="Supervisor name or ID"
                />
                <p className="text-xs text-gray-500 mt-1">
                  Required for pauses over {SUPERVISOR_THRESHOLD_SECONDS / 60} minutes.
                </p>
              </div>
            )}

            <div className="flex justify-end gap-3 mt-6">
              <button
                onClick={() => setShowPause(false)}
                className="px-4 py-2 border border-gray-300 rounded-lg text-gray-700 hover:bg-gray-50"
              >
                Cancel
              </button>
              <button
                onClick={handlePause}
                disabled={busy}
                className="px-4 py-2 bg-amber-500 text-white rounded-lg hover:bg-amber-600 disabled:bg-gray-300"
              >
                Pause Recording
              </button>
            </div>
          </div>
        </div>
  );

  // ---- render helpers ----

  const renderArrayField = (label: string, key: string, data: string[] | undefined) => {
    const value = editedFields[key] !== undefined ? editedFields[key] : (data?.join('\n') || '');
    return (
      <div className="mb-4" key={key}>
        <label className="block text-sm font-medium text-gray-700 mb-1">{label}</label>
        <textarea
          className="w-full px-3 py-2 border border-gray-300 rounded-lg focus:ring-2 focus:ring-blue-500 focus:outline-none"
          rows={3}
          value={value}
          onChange={(e) => setEditedFields((prev) => ({ ...prev, [key]: e.target.value }))}
          placeholder={`Enter ${label.toLowerCase()}...`}
        />
      </div>
    );
  };

  if (!patientData) {
    // A recording is running but this tab does not know which patient it is
    // for. Show enough to take control of it - stopping the wrong consultation
    // is recoverable, being unable to stop one is not.
    if (status?.isRecording) {
      return (
        <div className="min-h-screen bg-gray-50 flex items-center justify-center p-6">
          <div className="bg-white rounded-xl shadow p-6 max-w-md w-full space-y-4">
            <div className="flex items-center gap-2">
              <span className="w-3 h-3 bg-red-500 rounded-full recording-pulse" />
              <span className="font-semibold text-red-700">
                {status.isPaused ? 'Paused' : 'Recording in progress'}
              </span>
            </div>
            <p className="text-sm text-gray-600">
              Patient {status.patientRef ?? 'unknown'} · {status.doctorId ?? '—'}
              {status.hospitalId ? ` · ${status.hospitalId}` : ''}
              <br />
              {formatDuration(status.durationSeconds)} · {status.segmentCount} clip(s)
            </p>
            <p className="text-xs text-gray-500">
              This tab has no patient loaded, so the consultation view is not
              available - but the recording can still be controlled from here.
            </p>
            {error && <p className="text-sm text-red-600">{error}</p>}
            <div className="space-y-2">
              {!status.isPaused && (
                <button onClick={() => setShowPause(true)} disabled={busy}
                  className="w-full py-3 rounded-lg font-semibold bg-amber-500 text-white hover:bg-amber-600">
                  Pause Recording
                </button>
              )}
              {status.isPaused && (
                <button onClick={handleResume} disabled={busy}
                  className="w-full py-3 rounded-lg font-semibold bg-green-600 text-white hover:bg-green-700">
                  Resume Recording
                </button>
              )}
              <button onClick={handleStop} disabled={busy}
                className="w-full py-2 rounded-lg font-medium border border-gray-300 text-gray-700 hover:bg-gray-50">
                Stop Recording
              </button>
              <button onClick={() => router.push('/')}
                className="w-full py-2 text-sm text-blue-600 underline">
                Go to patient entry
              </button>
            </div>
          </div>
          {showPause && renderPauseDialog()}
        </div>
      );
    }
    return <div className="p-8 text-center text-gray-500">Loading…</div>;
  }

  const state = status?.state ?? 'unknown';
  const uploadPending = status?.upload?.pending_segments ?? 0;

  return (
    <div className="min-h-screen bg-gray-50">
      <header className="bg-white shadow-sm border-b">
        <div className="max-w-7xl mx-auto px-4 py-4 flex flex-wrap gap-4 justify-between items-center">
          <div className="flex items-center space-x-4">
            <h1 className="text-2xl font-bold text-gray-800">CMED — Doctor Dashboard</h1>
            <div className={`flex items-center space-x-1 text-sm ${connected ? 'text-green-600' : 'text-red-600'}`}>
              <span className={`w-2 h-2 rounded-full ${connected ? 'bg-green-500' : 'bg-red-500'}`} />
              <span>{connected ? 'AIMScribe connected' : 'AIMScribe not running'}</span>
            </div>
          </div>

          <div className="flex items-center gap-4">
            {status?.isPaused && (
              <div className="flex items-center gap-2 bg-amber-100 px-4 py-2 rounded-lg">
                <span className="w-3 h-3 bg-amber-500 rounded-full" />
                <span className="text-amber-800 font-medium">
                  Paused — {status.pause?.reason?.replace(/_/g, ' ')}
                </span>
              </div>
            )}
            {status?.isRecording && !status?.isPaused && (
              <div className="flex items-center gap-2 bg-red-100 px-4 py-2 rounded-lg">
                <span className="w-3 h-3 bg-red-500 rounded-full recording-pulse" />
                <span className="text-red-700 font-medium">
                  Recording {formatDuration(status.durationSeconds)}
                </span>
                <span className="text-xs text-red-600">{status.segmentCount} clips</span>
              </div>
            )}
            {/* Who this machine is enrolled to. Set by an administrator at
                enrolment; the page cannot change it and never sends it. */}
            <div className="text-sm text-gray-600">
              {!status ? 'connecting…' : (status.doctorId ?? 'not enrolled')}
              {status?.hospitalId && (
                <span className="text-gray-400"> · {status.hospitalId}</span>
              )}
            </div>
          </div>
        </div>
      </header>

      {error && (
        <div className="bg-red-100 border-l-4 border-red-500 text-red-700 p-4 max-w-7xl mx-auto mt-4">
          <p>{error}</p>
          <button onClick={() => setError(null)} className="text-sm underline mt-1">Dismiss</button>
        </div>
      )}
      {notice && !error && (
        <div className="bg-blue-50 border-l-4 border-blue-400 text-blue-800 p-3 max-w-7xl mx-auto mt-4 text-sm">
          <span>{notice}</span>
          <button onClick={() => setNotice(null)} className="ml-3 underline">Dismiss</button>
        </div>
      )}
      {uploadPending > 0 && (
        <div className="bg-amber-50 border-l-4 border-amber-400 text-amber-800 p-3 max-w-7xl mx-auto mt-4 text-sm">
          {uploadPending} clip(s) waiting to upload. Audio is held safely on this PC until they do.
        </div>
      )}

      <div className="max-w-7xl mx-auto px-4 py-6">
        <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">

          <div className="lg:col-span-1 space-y-6">
            <div className="bg-white rounded-xl shadow p-6">
              <h2 className="text-lg font-semibold text-gray-800 mb-4">Patient Information</h2>
              <div className="space-y-3">
                <Row label="ID" value={patientData.patient_id} />
                <Row label="Name" value={patientData.patient_name} />
                <Row label="Age" value={patientData.age || '-'} />
                <Row label="Gender" value={patientData.gender || '-'} />
              </div>

              <div className="mt-6 pt-4 border-t">
                <h3 className="text-sm font-semibold text-gray-700 mb-3">Health Screening</h3>
                <div className="grid grid-cols-2 gap-2 text-sm">
                  {patientData.health_screening?.bp_systolic && (
                    <Fact label="BP" value={`${patientData.health_screening.bp_systolic}/${patientData.health_screening.bp_diastolic}`} />
                  )}
                  {patientData.health_screening?.pulse_rate && (
                    <Fact label="Pulse" value={patientData.health_screening.pulse_rate} />
                  )}
                  {patientData.health_screening?.temperature && (
                    <Fact label="Temp" value={`${patientData.health_screening.temperature}°C`} />
                  )}
                  {patientData.health_screening?.height_cm && (
                    <Fact label="Height" value={`${patientData.health_screening.height_cm} cm`} />
                  )}
                  {patientData.health_screening?.weight_kg && (
                    <Fact label="Weight" value={`${patientData.health_screening.weight_kg} kg`} />
                  )}
                </div>
              </div>

              {/* The hospital is shown, not chosen: it comes from the machine's
                  enrolment, and a PC does not move between hospitals. The doctor
                  is chosen, because the room is shared - but only from the
                  register, and the backend checks the choice again when the
                  session opens. */}
              <div className="mt-6 pt-4 border-t">
                <div className="text-sm font-medium text-gray-700">Consulting at</div>
                <div className="text-sm text-gray-600 mt-1">
                  {!status
                    ? 'waiting for AIMScribe on this PC…'
                    : status.hospitalId || 'this PC is not enrolled — contact IT'}
                </div>

                {!status?.isRecording && (
                  <div className="mt-3 space-y-2">
                    <p className="text-xs text-gray-500">
                      On the real CMED site these arrive with the trigger, from the
                      doctor&apos;s own login. Typed here so the pipeline can be tested
                      with any doctor at any hospital.
                    </p>
                    <div className="grid grid-cols-2 gap-2">
                      <label className="text-sm text-gray-700">
                        Doctor ID
                        <input
                          value={doctorId}
                          onChange={(e) => setDoctorId(e.target.value.trim())}
                          placeholder="DR003"
                          list="known-doctors"
                          className="mt-1 w-full border rounded-lg px-3 py-2 text-sm text-gray-800"
                        />
                      </label>
                      <label className="text-sm text-gray-700">
                        Hospital ID
                        <input
                          value={hospitalId}
                          onChange={(e) => setHospitalId(e.target.value.trim())}
                          placeholder={status?.hospitalId ?? 'HOSP001'}
                          className="mt-1 w-full border rounded-lg px-3 py-2 text-sm text-gray-800"
                        />
                      </label>
                    </div>
                    {/* Suggestions only. Anything may be typed, because CMED is
                        the authority on who is on shift and where. */}
                    <datalist id="known-doctors">
                      {(register?.doctors ?? []).map((d) => (
                        <option key={d.doctorId} value={d.doctorId} />
                      ))}
                    </datalist>
                  </div>
                )}
              </div>

              {/* Consent - a precondition, not a formality */}
              {!status?.isRecording && (
                <div className="mt-6 pt-4 border-t">
                  <label className="flex items-start gap-2 text-sm text-gray-700">
                    <input
                      type="checkbox"
                      checked={consentGiven}
                      onChange={(e) => setConsentGiven(e.target.checked)}
                      className="mt-1"
                    />
                    <span>
                      The patient has been told this consultation will be recorded and has agreed.
                    </span>
                  </label>
                </div>
              )}

              <div className="mt-6 space-y-3">
                {!status?.isRecording && (
                  <>
                    <button
                      onClick={handleStart}
                      disabled={!connected || busy || !consentGiven || !doctorId}
                      className={`w-full py-3 rounded-lg font-semibold transition-colors ${
                        !connected || !consentGiven || !doctorId
                          ? 'bg-gray-300 text-gray-500 cursor-not-allowed'
                          : recordingOtherPatient
                          ? 'bg-orange-500 text-white hover:bg-orange-600'
                          : 'bg-green-600 text-white hover:bg-green-700'
                      }`}
                    >
                      {!connected ? 'Waiting for AIMScribe…'
                        : !doctorId ? 'Enter the doctor ID to continue'
                        : !consentGiven ? 'Confirm consent to continue'
                        : recordingOtherPatient ? 'Start (stops previous patient)'
                        : 'Start Consultation'}
                    </button>
                    <p className="text-xs text-gray-500 text-center">
                      Recording runs in AIMScribe on this PC and continues if you close this page.
                    </p>
                  </>
                )}

                {/* Shown for any recording in progress, not only one started
                    from this tab. patientData lives in sessionStorage, which is
                    per tab, so gating on it meant a reload or a second tab hid
                    Pause and Stop while audio was still being captured - a
                    recording you can see but cannot stop. */}
                {status?.isRecording && !status?.isPaused && (
                  <>
                    {recordingOtherPatient && (
                      <p className="text-xs text-amber-700 bg-amber-50 border border-amber-200 rounded p-2">
                        Recording patient {status.patientRef}, not the one shown here.
                      </p>
                    )}
                    <button
                      onClick={() => setShowPause(true)}
                      disabled={busy}
                      className="w-full py-3 rounded-lg font-semibold bg-amber-500 text-white hover:bg-amber-600"
                    >
                      Pause Recording
                    </button>
                    <button
                      onClick={handleStop}
                      disabled={busy}
                      className="w-full py-2 rounded-lg font-medium border border-gray-300 text-gray-700 hover:bg-gray-50"
                    >
                      Stop Recording
                    </button>
                  </>
                )}

                {status?.isPaused && (
                  <>
                    <div className="p-3 bg-amber-50 border border-amber-200 rounded-lg text-sm text-amber-900">
                      <div className="font-medium">Paused</div>
                      <div className="text-xs mt-1">
                        Reason: {status.pause?.reason?.replace(/_/g, ' ')}<br />
                        Authorised by: {status.pause?.authorisedBy}
                      </div>
                      <div className="text-xs mt-2 text-amber-700">
                        This pause is recorded, so the gap in the audio is accounted for.
                      </div>
                    </div>
                    <button
                      onClick={handleResume}
                      disabled={busy}
                      className="w-full py-3 rounded-lg font-semibold bg-green-600 text-white hover:bg-green-700"
                    >
                      Resume Recording
                    </button>
                  </>
                )}

                {recordingOtherPatient && (
                  <p className="text-xs text-orange-600 text-center">
                    AIMScribe is currently recording patient {status?.patientRef}.
                  </p>
                )}
              </div>
            </div>

            <button
              onClick={() => router.push('/')}
              className="w-full py-3 bg-blue-600 text-white rounded-lg font-semibold hover:bg-blue-700"
            >
              + New Patient
            </button>
          </div>

          <div className="lg:col-span-2 bg-white rounded-xl shadow p-6">
            <div className="flex justify-between items-center mb-6">
              <h2 className="text-lg font-semibold text-gray-800">Prescription</h2>
              <div className="flex items-center gap-3">
                <span className="text-xs text-gray-500">state: {state}</span>
                {nerVersion > 0 && (
                  <span className="text-sm text-green-600 bg-green-100 px-3 py-1 rounded-full">
                    NER v{nerVersion}
                  </span>
                )}
              </div>
            </div>

            {nerVersion > 0 && (
              <p className="text-xs text-gray-500 mb-4 -mt-3">
                Fields below are AI suggestions from the consultation audio. Review every
                entry, especially medications, before saving.
              </p>
            )}

            <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
              <div>
                {renderArrayField('Chief Complaints', 'chief_complaints', nerData?.chief_complaints?.data)}
                {renderArrayField('Drug History', 'drug_history', nerData?.drug_history?.data)}
                {renderArrayField('On Examination (O/E)', 'on_examination', nerData?.on_examination?.data)}
                {renderArrayField('Systemic Examination (S/E)', 'systemic_examination', nerData?.systemic_examination?.data)}
                {renderArrayField('Investigations', 'investigations', nerData?.investigations?.data)}
              </div>
              <div>
                {renderArrayField('Diagnosis', 'diagnosis', nerData?.diagnosis?.data)}
                {renderArrayField('Advice', 'advice', nerData?.advice?.data)}
                {renderArrayField('Follow Up', 'follow_up', nerData?.follow_up?.data)}
                {renderArrayField('Additional Notes', 'additional_notes', nerData?.additional_notes?.data)}

                <div className="mb-4">
                  <label className="block text-sm font-medium text-gray-700 mb-1">Medications</label>
                  <div className="border rounded-lg overflow-x-auto">
                    <table className="w-full text-sm">
                      <thead className="bg-gray-50">
                        <tr>
                          <th className="px-2 py-1 text-left">Medicine</th>
                          <th className="px-2 py-1 text-left">Dosage</th>
                          <th className="px-2 py-1 text-left">Schedule</th>
                          <th className="px-2 py-1 text-left">Duration</th>
                          <th className="px-2 py-1 text-left">Instruction</th>
                        </tr>
                      </thead>
                      <tbody>
                        {nerData?.medications?.data?.length ? (
                          nerData.medications.data.map((med: any, idx: number) => (
                            <tr key={idx} className="border-t hover:bg-gray-50">
                              <td className="px-2 py-2 font-medium">{med.name || '-'}</td>
                              <td className="px-2 py-2">{med.dose || '-'}</td>
                              <td className="px-2 py-2">{med.frequency || '-'}</td>
                              <td className="px-2 py-2">{med.duration || '-'}</td>
                              <td className="px-2 py-2 text-gray-600">{med.instruction || '-'}</td>
                            </tr>
                          ))
                        ) : (
                          <tr>
                            <td colSpan={5} className="px-2 py-4 text-center text-gray-400">
                              No medications yet
                            </td>
                          </tr>
                        )}
                      </tbody>
                    </table>
                  </div>
                </div>
              </div>
            </div>

            <div className="flex justify-end space-x-4 mt-6 pt-4 border-t">
              <button
                onClick={handleSavePrescription}
                disabled={!status?.sessionId || busy}
                className="px-6 py-2 bg-green-600 text-white rounded-lg hover:bg-green-700 disabled:bg-gray-300 disabled:cursor-not-allowed"
              >
                Save Prescription
              </button>
              <button
                onClick={() => window.print()}
                className="px-6 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700"
              >
                Print
              </button>
            </div>
          </div>
        </div>
      </div>

      {showPause && renderPauseDialog()}
    </div>
  );
}

// ============================================================
// Small presentational helpers
// ============================================================

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex justify-between">
      <span className="text-gray-600">{label}:</span>
      <span className="font-medium">{value}</span>
    </div>
  );
}

function Fact({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <span className="text-gray-500">{label}:</span>
      <span className="ml-1 font-medium">{value}</span>
    </div>
  );
}

function formatDuration(seconds: number): string {
  const total = Math.floor(seconds || 0);
  const minutes = Math.floor(total / 60);
  return `${String(minutes).padStart(2, '0')}:${String(total % 60).padStart(2, '0')}`;
}

// ============================================================
// NER normalisation - the backend returns several shapes
// ============================================================

function toStringArray(value: any): string[] {
  if (!value) return [];
  if (Array.isArray(value)) {
    return value.map((item) => {
      if (typeof item === 'string') return item;
      if (item && typeof item === 'object') {
        const parts: string[] = [];
        if (item.complaint) parts.push(item.complaint);
        if (item.duration) parts.push(`for ${item.duration}`);
        if (item.name) parts.push(item.name);
        if (item['Name (English)']) parts.push(item['Name (English)']);
        if (!parts.length) {
          Object.values(item).forEach((v) => {
            if (typeof v === 'string' && v) parts.push(v);
          });
        }
        return parts.join(' ') || JSON.stringify(item);
      }
      return String(item);
    });
  }
  if (typeof value === 'object') {
    return Object.entries(value)
      .filter(([, v]) => v && v !== '')
      .map(([k, v]) => `${k.replace(/\s*\((English|Bengali)\)/g, '')}: ${v}`);
  }
  return [String(value)];
}

function normalizeMedications(meds: any): any[] {
  if (!Array.isArray(meds)) return [];
  return meds.map((med) =>
    typeof med === 'string'
      ? { name: med, dose: '', frequency: '', duration: '', instruction: '' }
      : {
          name: med['Name (English)'] || med.name || med.Name || '',
          dose: med['Dosage (English)'] || med.dose || med.Dosage || '',
          frequency: med['Schedule (Bengali)'] || med.frequency || med.Schedule || '',
          duration: med['Duration (Bengali)'] || med.duration || med.Duration || '',
          instruction: med['Instruction (Bengali)'] || med.instruction || med.Instruction || '',
        }
  );
}

function normalizeNerData(fields: any): NERFields {
  if (!fields) return {};
  const wrap = (value: any) => ({ data: toStringArray(value) });
  return {
    chief_complaints: wrap(fields.chief_complaints),
    drug_history: wrap(fields.drug_history),
    on_examination: wrap(fields.on_examination),
    systemic_examination: wrap(fields.systemic_examination),
    investigations: wrap(fields.investigations),
    diagnosis: wrap(fields.diagnosis),
    medications: { data: normalizeMedications(fields.medications) },
    advice: wrap(fields.advice),
    follow_up: wrap(fields.follow_up),
    additional_notes: wrap(fields.additional_notes),
  };
}
