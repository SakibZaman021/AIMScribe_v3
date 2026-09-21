/**
 * Client for the AIMScribe recorder on the same PC (Channel A, SRS 3.3 §6.1).
 *
 * What changed in v3, and why it matters to whoever maintains CMED's real site:
 *
 *  - **The page no longer authorises anything.** It sends five plain fields -
 *    patient, doctor, clinic, start time, date - and the AIMS LAB server
 *    decides. There is no key here and nothing to sign, so this page cannot be
 *    the thing that is stolen (§5, `SRS-GRT-01`).
 *  - **Every command is answered once, with a code.** The reply carries
 *    `request_id`, `status` and `code`; act on the code, never on the wording
 *    (`SRS-IF1-09`). `200 RECORDING_STARTED` and `202 RECORDING_PROVISIONAL`
 *    both mean recording; the second means permission is still being checked.
 *  - **`prescription_built` ends a consultation.** Opening the next patient no
 *    longer cuts the last one off mid-sentence (§7.7): the recorder refuses
 *    with `GATE_NOT_ARMED` until this is sent.
 *
 * Recording belongs to the recorder, not to this page. Closing the tab,
 * reloading, or losing the socket does not stop a recording; reconnecting
 * re-syncs what is on screen.
 */

export const RECORDER_WS_URL =
  process.env.NEXT_PUBLIC_RECORDER_WS || 'ws://localhost:5050/ws';

export type AgentState = 'idle' | 'recording' | 'paused' | 'closing' | 'unknown';

/** The five fields that identify one consultation (§6.1.3). */
export interface Trigger {
  patient_id: string;
  doctor_id: string;
  /** CMED's own clinic identifier. The server maps it to the clinic's code. */
  hospital_id: string;
  /** Exactly as CMED's server wrote it, and sent unchanged in API 2. */
  start_time: string;
  /** YYYY-MM-DD. */
  date: string;
}

/** Every reply, whether it succeeded or not (Appendix A). */
export interface Reply {
  ok: boolean;
  status: number;
  code: string;
  message: string;
  requestId?: string;
  data?: Record<string, unknown>;
}

export interface AgentStatus {
  state: AgentState;
  isRecording: boolean;
  isPaused: boolean;
  sessionId: string | null;
  patientRef: string | null;
  /** The clinic this machine is enrolled at. Authoritative; not a page choice. */
  hospitalId: string | null;
  doctorId: string | null;
  durationSeconds: number;
  pausedSeconds: number;
  segmentCount: number;
  /** What §5.6 decided: pending, confirming, confirmed, unconfirmed. */
  confirmation?: string | null;
  pause?: { reason: string; authorisedBy: string; since: string } | null;
  upload?: {
    online?: boolean;
    pending_segments?: number;
    spool_pressure?: string;
    last_error?: string;
  };
}

export interface DoctorOption {
  doctorId: string;
  fullName: string;
}

export interface DoctorRegister {
  hospitalId: string | null;
  assignedDoctorId: string | null;
  doctors: DoctorOption[];
}

export interface PauseRequest {
  reason: string;
  reasonDetail?: string;
  authorisedBy?: string;
  expectedSeconds?: number;
}

type Listener = (payload: any) => void;

const EMPTY_STATUS: AgentStatus = {
  state: 'unknown',
  isRecording: false,
  isPaused: false,
  sessionId: null,
  patientRef: null,
  hospitalId: null,
  doctorId: null,
  durationSeconds: 0,
  pausedSeconds: 0,
  segmentCount: 0,
  confirmation: null,
};

/** The codes that mean "it is recording" (§6.1.4). */
export const RECORDING_CODES = ['RECORDING_STARTED', 'RECORDING_PROVISIONAL'];

export class AimscribeClient {
  private socket: WebSocket | null = null;
  private listeners = new Map<string, Set<Listener>>();
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private pending = new Map<string, {
    resolve: (reply: Reply) => void; timer: ReturnType<typeof setTimeout>;
  }>();
  private closedByUs = false;
  private counter = 0;

  connected = false;
  status: AgentStatus = { ...EMPTY_STATUS };

  // ---- lifecycle ----

  connect(): void {
    if (this.socket && (this.socket.readyState === WebSocket.OPEN ||
                        this.socket.readyState === WebSocket.CONNECTING)) {
      return;
    }
    this.closedByUs = false;

    try {
      const socket = new WebSocket(RECORDER_WS_URL);

      socket.onopen = () => {
        this.connected = true;
        this.emit('connection', { connected: true });
        void this.command('status', {});
      };

      socket.onmessage = (event) => this.receive(event.data);

      socket.onclose = () => {
        this.connected = false;
        this.socket = null;
        this.failPending('Connection to AIMScribe was lost.');
        this.emit('connection', { connected: false });
        // The recorder keeps recording regardless; this only restores the view.
        if (!this.closedByUs) {
          this.reconnectTimer = setTimeout(() => this.connect(), 3000);
        }
      };

      socket.onerror = () => {
        this.emit('error', {
          message:
            'Cannot reach AIMScribe on this PC. Check that the tray icon is running.',
        });
      };

      this.socket = socket;
    } catch {
      this.emit('error', { message: 'Failed to open a connection to AIMScribe.' });
    }
  }

  disconnect(): void {
    this.closedByUs = true;
    if (this.reconnectTimer) clearTimeout(this.reconnectTimer);
    this.socket?.close();
    this.socket = null;
  }

  // ---- the two commands CMED sends ----

  /**
   * API 1: a patient has been opened (§6.1.3).
   *
   * Sends the five fields and nothing else. No consent flag: consent is asked
   * at reception, and a refusal is recorded on the recorder's own Stop button,
   * which deletes the recording everywhere (§7.8a). Nothing about consent
   * crosses this interface, so CMED has nothing to store or prove.
   */
  async start(trigger: Trigger): Promise<Reply> {
    return this.command('start', { trigger });
  }

  /**
   * API 3, part one: the prescription is built (§6.1.5).
   *
   * Until this arrives, opening the next patient is refused with
   * `GATE_NOT_ARMED` - which is what stops a consultation being cut off
   * because someone clicked ahead.
   */
  async prescriptionBuilt(patientId: string, sessionId: string,
                          occurredAt?: string): Promise<Reply> {
    return this.command('prescription_built', {
      patient_id: patientId,
      session_id: sessionId,
      occurred_at: occurredAt ?? new Date().toISOString(),
    });
  }

  // ---- the rest: for this test app, not for CMED's site ----

  /**
   * The doctors this PC's clinic has seen, as suggestions.
   *
   * A directory, not a gate: CMED is the authority on who is on shift, and a
   * doctor who is not in this list is still recorded (decision D4). Asked of
   * the recorder because it knows which clinic the machine belongs to.
   */
  async doctors(): Promise<DoctorRegister> {
    const reply = await this.command('doctors', {});
    const data: any = reply.data ?? {};
    return {
      hospitalId: data.hospital_id ?? null,
      assignedDoctorId: data.assigned_doctor_id ?? null,
      doctors: Array.isArray(data.doctors)
        ? data.doctors.map((d: any) => ({
            doctorId: String(d.doctor_id ?? ''),
            fullName: String(d.full_name ?? d.doctor_id ?? ''),
          }))
        : [],
    };
  }

  async pause(request: PauseRequest): Promise<Reply> {
    return this.command('pause', {
      reason: request.reason,
      reason_detail: request.reasonDetail ?? '',
      authorised_by: request.authorisedBy ?? '',
      expected_seconds: request.expectedSeconds ?? 0,
    });
  }

  async resume(): Promise<Reply> {
    return this.command('resume', {});
  }

  /**
   * Stop from here rather than from the recorder's own window.
   *
   * The doctor's Stop button is on the recorder, because that is where
   * "Patient did not consent" has to be (§7.8a). This exists so this test
   * page can end a consultation.
   */
  async stop(): Promise<Reply> {
    return this.command('stop', {});
  }

  async refreshStatus(): Promise<Reply> {
    return this.command('status', {});
  }

  /**
   * Send one command and wait for its reply.
   *
   * Replies are matched by `request_id`, so two commands in flight cannot be
   * confused with one another - matching by command name, as this did before,
   * could hand the wrong answer to the wrong caller.
   */
  command(name: string, payload: Record<string, unknown>): Promise<Reply> {
    if (!this.socket || this.socket.readyState !== WebSocket.OPEN) {
      return Promise.resolve({
        ok: false, status: 503, code: 'AGENT_NOT_READY',
        message: 'Not connected to AIMScribe on this PC.',
      });
    }

    const requestId = `cmed-${Date.now()}-${++this.counter}`;
    return new Promise((resolve) => {
      const timer = setTimeout(() => {
        this.pending.delete(requestId);
        resolve({
          ok: false, status: 504, code: 'NO_REPLY',
          message: 'AIMScribe did not answer.', requestId,
        });
      }, 20000);

      this.pending.set(requestId, { resolve, timer });
      this.send({ command: name, request_id: requestId, ...payload });
    });
  }

  private send(message: Record<string, unknown>): void {
    if (this.socket?.readyState === WebSocket.OPEN) {
      this.socket.send(JSON.stringify(message));
    }
  }

  // ---- inbound ----

  private receive(raw: string): void {
    let message: any;
    try {
      message = JSON.parse(raw);
    } catch {
      return;
    }

    const event = message.event;

    if (event === 'ack' || event === 'error') {
      const reply: Reply = {
        ok: Number(message.status ?? 0) < 400,
        status: Number(message.status ?? 0),
        code: String(message.code ?? ''),
        message: String(message.message ?? ''),
        requestId: message.request_id,
        data: message.data,
      };
      const waiting = reply.requestId ? this.pending.get(reply.requestId) : undefined;
      if (waiting && reply.requestId) {
        clearTimeout(waiting.timer);
        this.pending.delete(reply.requestId);
        waiting.resolve(reply);
      } else if (event === 'error') {
        // Nothing was waiting for it: a refusal the recorder raised by itself.
        this.emit('error', reply);
      }
      this.emit('reply', reply);
      return;
    }

    if (event === 'status' || message.state !== undefined) {
      this.status = mapStatus(message);
      this.emit('status', this.status);
    }

    this.emit(event, message);
  }

  private failPending(reason: string): void {
    this.pending.forEach((entry, requestId) => {
      clearTimeout(entry.timer);
      entry.resolve({ ok: false, status: 503, code: 'AGENT_NOT_READY',
                      message: reason, requestId });
    });
    this.pending.clear();
  }

  // ---- events ----

  on(event: string, listener: Listener): () => void {
    if (!this.listeners.has(event)) this.listeners.set(event, new Set());
    this.listeners.get(event)!.add(listener);
    return () => this.listeners.get(event)?.delete(listener);
  }

  private emit(event: string, payload: any): void {
    this.listeners.get(event)?.forEach((listener) => {
      try {
        listener(payload);
      } catch (error) {
        console.error(`[aimscribe] listener for ${event} threw`, error);
      }
    });
  }
}

function mapStatus(message: any): AgentStatus {
  return {
    state: (message.state as AgentState) ?? 'unknown',
    isRecording: Boolean(message.is_recording),
    isPaused: Boolean(message.is_paused),
    sessionId: message.session_id ?? null,
    patientRef: message.patient_ref ?? message.patient_id ?? null,
    hospitalId: message.hospital_id ?? null,
    doctorId: message.doctor_id ?? null,
    durationSeconds: Number(message.duration_seconds ?? 0),
    pausedSeconds: Number(message.paused_seconds ?? 0),
    segmentCount: Number(message.segment_count ?? 0),
    confirmation: message.confirmation ?? null,
    pause: message.pause
      ? {
          reason: message.pause.reason,
          authorisedBy: message.pause.authorised_by,
          since: message.pause.since,
        }
      : null,
    upload: message.upload ?? {},
  };
}

/** What a doctor may be told about a code, in plain words (`SRS-IF1-09`). */
export const CODE_NOTES: Record<string, string> = {
  RECORDING_STARTED: 'Recording.',
  RECORDING_PROVISIONAL: 'Recording. Permission is still being checked.',
  GATE_ARMED: 'This consultation will end when the next patient is opened.',
  GATE_ALREADY_ARMED: 'Already marked as finished.',
  GATE_NOT_ARMED: 'Finish the current consultation first.',
  SESSION_ALREADY_ACTIVE: 'This consultation is already being recorded.',
  CLINIC_MISMATCH: 'This PC belongs to a different clinic.',
  DEVICE_NOT_ENROLLED: 'This PC is not registered with AIMS LAB.',
  AUTHORISATION_FAILED: 'The server did not authorise this recording.',
  AGENT_NOT_READY: 'AIMScribe is not running on this PC.',
  MISSING_FIELD: 'A field is missing or malformed.',
  INVALID_IDENTIFIER: 'An identifier contains characters that are not allowed.',
  NO_REPLY: 'AIMScribe did not answer.',
};

/** Matches the recorder's own list; 'other' requires written detail. */
export const PAUSE_REASONS = [
  { value: 'patient_declined', label: 'Patient declined recording' },
  { value: 'sensitive_personal_matter', label: 'Sensitive personal matter' },
  { value: 'non_clinical_interruption', label: 'Non-clinical interruption' },
  { value: 'other', label: 'Other (describe below)' },
] as const;
