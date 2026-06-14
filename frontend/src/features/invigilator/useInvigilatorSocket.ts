// Invigilator console live socket hook (task 18).
//
// `useInvigilatorSocket(examId, token)` opens a connection to
// `/ws/invigilator/{exam_id}` (via the shared wsClient) and folds the
// exam-scoped live feed into console state:
//   - `alert.broadcast` → a capped live alert feed for the exam
//   - `session.update`  → per-session integrity/status updates (session tiles)
//
// Like the dashboard hook, the transport (WebSocket impl) is injectable so the
// folding/connection behaviour can be driven deterministically from a vitest
// test with a fake socket and no real server (Req 12.1).

import { useEffect, useRef, useSyncExternalStore } from "react";
import { connect, type WsConnection } from "@/lib/wsClient";
import type {
  AlertBroadcastPayload,
  AlertSeverity,
  SessionUpdatePayload,
  WSMessage,
} from "@/types";

// --- Public state shapes ----------------------------------------------------

/** A live alert-feed entry derived from an `alert.broadcast` envelope. */
export interface InvigilatorAlert {
  id: string;
  source: string;
  sessionId?: string;
  ts: string;
  severity: AlertSeverity;
  message: string;
  anomalyId: string;
  reasons: string[];
}

/** A monitored session entry (keyed by session id), updated live. */
export interface MonitoredSession {
  sessionId: string;
  status?: string;
  integrityScore?: number;
  ts: string;
}

/** The full hook return / store snapshot. */
export interface InvigilatorState {
  sessions: Record<string, MonitoredSession>;
  alerts: InvigilatorAlert[];
  connected: boolean;
}

/** Live alert-feed ring-buffer cap (mirrors the dashboard's 500 cap). */
export const MAX_INVIGILATOR_ALERTS = 500;

export interface InvigilatorSocketOptions {
  /** Override the WebSocket constructor (defaults to the global one). */
  webSocketImpl?: typeof WebSocket;
  /** Override the WS base URL (forwarded to the wsClient). */
  baseUrl?: string;
}

function emptyState(): InvigilatorState {
  return { sessions: {}, alerts: [], connected: false };
}

/**
 * Framework-agnostic controller holding the invigilator console state machine.
 * The React hook is a thin wrapper; tests can drive this directly with a fake
 * socket for deterministic folding assertions.
 */
export class InvigilatorSocketController {
  private state: InvigilatorState = emptyState();
  private readonly listeners = new Set<() => void>();
  private conn: WsConnection | null = null;
  private stopped = false;

  private readonly examId: string;
  private readonly token: string;
  private readonly webSocketImpl?: typeof WebSocket;
  private readonly baseUrl?: string;

  constructor(examId: string, token: string, options: InvigilatorSocketOptions = {}) {
    this.examId = examId;
    this.token = token;
    this.webSocketImpl = options.webSocketImpl;
    this.baseUrl = options.baseUrl;
  }

  // --- External store contract (useSyncExternalStore) ----------------------

  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };

  getSnapshot = (): InvigilatorState => this.state;

  private setState(patch: Partial<InvigilatorState>): void {
    this.state = { ...this.state, ...patch };
    for (const listener of this.listeners) listener();
  }

  // --- Lifecycle -----------------------------------------------------------

  start(): void {
    this.stopped = false;
    this.openConnection();
  }

  stop(): void {
    this.stopped = true;
    if (this.conn) {
      this.conn.close();
      this.conn = null;
    }
  }

  private openConnection(): void {
    if (this.stopped) return;
    this.conn = connect({
      path: `/ws/invigilator/${encodeURIComponent(this.examId)}`,
      token: this.token,
      baseUrl: this.baseUrl,
      webSocketImpl: this.webSocketImpl,
      onOpen: () => this.setState({ connected: true }),
      onClose: () => {
        this.conn = null;
        this.setState({ connected: false });
      },
      onMessage: (message) => this.handleMessage(message),
    });
  }

  // --- Envelope folding ----------------------------------------------------

  private handleMessage(message: WSMessage): void {
    switch (message.type) {
      case "alert.broadcast":
        this.prependAlert(message);
        break;
      case "session.update":
        this.updateSession(message);
        break;
      default:
        // agent.message / agent.status / anomaly.detected / report.ready are
        // not folded into the invigilator console view.
        break;
    }
  }

  private prependAlert(message: WSMessage): void {
    const payload = message.payload as AlertBroadcastPayload;
    const entry: InvigilatorAlert = {
      id: message.id,
      source: message.source,
      sessionId: message.sessionId,
      ts: message.ts,
      severity: payload.severity,
      message: payload.message,
      anomalyId: payload.anomalyId,
      reasons: payload.reasons,
    };
    const next = [entry, ...this.state.alerts];
    const capped =
      next.length > MAX_INVIGILATOR_ALERTS
        ? next.slice(0, MAX_INVIGILATOR_ALERTS)
        : next;
    this.setState({ alerts: capped });
  }

  private updateSession(message: WSMessage): void {
    const sessionId = message.sessionId;
    if (!sessionId) return;
    const payload = message.payload as SessionUpdatePayload;
    const prev = this.state.sessions[sessionId];
    const entry: MonitoredSession = {
      sessionId,
      // Preserve the last-known value when an update omits a field.
      status: payload.status ?? prev?.status,
      integrityScore: payload.integrityScore ?? prev?.integrityScore,
      ts: message.ts,
    };
    this.setState({
      sessions: { ...this.state.sessions, [sessionId]: entry },
    });
  }
}

/**
 * React hook: open the invigilator socket for `examId`/`token` and expose live
 * console state. The controller is recreated whenever the exam or token
 * changes so switching exams rebinds to the correct room.
 */
export function useInvigilatorSocket(
  examId: string,
  token: string,
  options: InvigilatorSocketOptions = {},
): InvigilatorState {
  const optionsRef = useRef(options);
  optionsRef.current = options;

  const controllerRef = useRef<InvigilatorSocketController | null>(null);
  const keyRef = useRef<string | null>(null);
  const key = `${examId}::${token}`;

  if (controllerRef.current === null || keyRef.current !== key) {
    controllerRef.current = new InvigilatorSocketController(
      examId,
      token,
      optionsRef.current,
    );
    keyRef.current = key;
  }

  const controller = controllerRef.current;

  useEffect(() => {
    controller.start();
    return () => controller.stop();
  }, [controller]);

  return useSyncExternalStore(controller.subscribe, controller.getSnapshot);
}
