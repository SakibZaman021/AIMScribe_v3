/**
 * Client for the local AIMScribe agent (protocol 2).
 *
 * Runs in the browser and talks to the agent on the same PC over
 * ws://localhost:5050/ws. Three things changed from v1:
 *
 *  - **Starting requires a grant.** The browser can no longer name the doctor or
 *    the hospital; it asks this app's server for a signed grant and forwards it.
 *  - **Commands are acknowledged separately from events.** The agent replies to a
 *    command with `{event:'ack'}` and broadcasts state changes to every connected
 *    tab. v1 did both with the same object, so each change arrived two or three
 *    times and the UI could not tell an acknowledgement from an event.
 *  - **Pause and resume exist.** A pause carries a reason and, past the agent's
 *    threshold, a supervisor's name.
 *
 * Recording is owned by the agent, not by this page. Closing the tab, reloading,
 * or losing the socket does not stop a recording; reconnecting re-syncs state.
 */

export const RECORDER_WS_URL =
  process.env.NEXT_PUBLIC_RECORDER_WS || 'ws://localhost:5050/ws';

export type AgentState = 'idle' | 'recording' | 'paused' | 'closing' | 'unknown';

export interface AgentStatus {
  state: AgentState;
  isRecording: boolean;
  isPaused: boolean;
  sessionId: string | null;
  patientRef: string | null;
  /** The hospital this machine is enrolled at. Authoritative; not a page choice. */
  hospitalId: string | null;
  doctorId: string | null;
  durationSeconds: number;
  pausedSeconds: number;
  segmentCount: number;
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
};

export class AimscribeClient {
  private socket: WebSocket | null = null;
  private listeners = new Map<string, Set<Listener>>();
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private pending = new Map<string, { resolve: (v: any) => void; reject: (e: any) => void }>();
  private closedByUs = false;

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
        this.send({ command: 'status' });
      };

      socket.onmessage = (event) => this.receive(event.data);

      socket.onclose = () => {
        this.connected = false;
        this.socket = null;
        this.failPending('Connection to AIMScribe was lost.');
        this.emit('connection', { connected: false });
        // The agent keeps recording regardless; this only restores the view.
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

  // ---- commands ----

  /**
   * Start recording.
   *
   * Fetches a grant from our own server first. The agent refuses to record
   * without one, and only that route can mint one - so the browser cannot start
   * a recording on its own, even though it now names the doctor.
   */
  async start(options: {
    patientRef: string;
    patientName?: string;
    doctorId: string;
    hospitalId?: string;
    consentObtained: boolean;
    consentMethod?: string;
  }): Promise<any> {
    if (!options.consentObtained) {
      throw new Error('Record the patient\'s consent before starting.');
    }

    const response = await fetch('/api/recording-grant', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        patient_ref: options.patientRef,
        doctor_id: options.doctorId,
        hospital_id: options.hospitalId ?? '',
        consent_obtained: options.consentObtained,
        consent_method: options.consentMethod ?? 'verbal_at_reception',
      }),
    });

    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      throw new Error(data.error || 'Recording could not be authorised.');
    }

    return this.command('start', {
      grant: data.grant,
      session: { patient_name: options.patientName ?? '' },
    });
  }

  /**
   * The doctors credentialed to record at this PC's hospital.
   *
   * Asked of the agent rather than fetched here: the agent holds the device
   * token, and it knows which hospital this machine belongs to. The browser
   * only picks from what comes back.
   */
  async doctors(): Promise<DoctorRegister> {
    const data = await this.command('doctors', {});
    return {
      hospitalId: data?.hospital_id ?? null,
      assignedDoctorId: data?.assigned_doctor_id ?? null,
      doctors: Array.isArray(data?.doctors)
        ? data.doctors.map((d: any) => ({
            doctorId: String(d.doctor_id ?? ''),
            fullName: String(d.full_name ?? d.doctor_id ?? ''),
          }))
        : [],
    };
  }

  async pause(request: PauseRequest): Promise<any> {
    return this.command('pause', {
      reason: request.reason,
      reason_detail: request.reasonDetail ?? '',
      authorised_by: request.authorisedBy ?? '',
      expected_seconds: request.expectedSeconds ?? 0,
    });
  }

  async resume(): Promise<any> {
    return this.command('resume', {});
  }

  async stop(): Promise<any> {
    return this.command('stop', {});
  }

  refreshStatus(): void {
    this.send({ command: 'status' });
  }

  /**
   * Send a command and wait for its acknowledgement.
   *
   * The agent processes one command at a time, so matching the next ack for a
   * given command name is sufficient and avoids inventing a correlation id the
   * agent does not implement.
   */
  private command(name: string, payload: Record<string, unknown>): Promise<any> {
    if (!this.socket || this.socket.readyState !== WebSocket.OPEN) {
      return Promise.reject(new Error('Not connected to AIMScribe.'));
    }

    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(name);
        reject(new Error('AIMScribe did not respond.'));
      }, 20000);

      this.pending.set(name, {
        resolve: (value) => { clearTimeout(timer); resolve(value); },
        reject: (error) => { clearTimeout(timer); reject(error); },
      });

      this.send({ command: name, ...payload });
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

    if (event === 'ack') {
      const waiting = this.pending.get(message.command);
      if (waiting) {
        this.pending.delete(message.command);
        waiting.resolve(message.data ?? {});
      }
      return;
    }

    if (event === 'error') {
      // Refusals ("no recording in progress", "reason required") answer whatever
      // command is outstanding; otherwise it is an unsolicited problem.
      const outstanding = this.pending.keys().next();
      if (!outstanding.done) {
        const key = outstanding.value;
        this.pending.get(key)?.reject(new Error(message.message || 'Command refused.'));
        this.pending.delete(key);
      }
      this.emit('error', message);
      return;
    }

    if (event === 'status' || message.state !== undefined) {
      this.status = mapStatus(message);
      this.emit('status', this.status);
    }

    this.emit(event, message);
  }

  private failPending(reason: string): void {
    this.pending.forEach((entry) => entry.reject(new Error(reason)));
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
    patientRef: message.patient_ref ?? null,
    hospitalId: message.hospital_id ?? null,
    doctorId: message.doctor_id ?? null,
    durationSeconds: Number(message.duration_seconds ?? 0),
    pausedSeconds: Number(message.paused_seconds ?? 0),
    segmentCount: Number(message.segment_count ?? 0),
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

/** Matches AIMS_PAUSE_REASONS on the agent; 'other' requires written detail. */
export const PAUSE_REASONS = [
  { value: 'patient_declined', label: 'Patient declined recording' },
  { value: 'sensitive_personal_matter', label: 'Sensitive personal matter' },
  { value: 'non_clinical_interruption', label: 'Non-clinical interruption' },
  { value: 'other', label: 'Other (describe below)' },
] as const;
