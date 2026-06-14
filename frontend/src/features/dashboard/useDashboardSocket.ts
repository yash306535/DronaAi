// Live admin dashboard WebSocket hook (task 17.1).
//
// `useDashboardSocket(token)` opens a connection to `/ws/dashboard` (via the
// shared wsClient), folds incoming WSMessage envelopes into dashboard state,
// and keeps that connection alive across drops with exponential-backoff
// reconnect + a REST snapshot resync on every (re)connect.
//
// Contract (design "Frontend WS hook contract"):
//   { agents, messages, alerts, sessions, connected, degraded }
//
// The transport (WebSocket impl), the REST snapshot, and the timer scheduler
// are all injectable so the reconnect/backoff and buffer-cap behaviour can be
// driven deterministically from a vitest test with a fake socket + fake timers
// and no real server (Req 12.7, 12.8, 12.9).

import { useEffect, useRef, useSyncExternalStore } from "react";
import { apiClient, type ApiClient } from "@/lib/apiClient";
import { connect, type WsConnection } from "@/lib/wsClient";
import type {
  AgentMessagePayload,
  AgentStatusPayload,
  AlertBroadcastPayload,
  AlertSeverity,
  SessionUpdatePayload,
  WSMessage,
} from "@/types";

// --- Public state shapes ----------------------------------------------------

/** An agent status card entry (keyed by emitting agent name). */
export interface AgentStatus extends AgentStatusPayload {
  source: string;
  ts: string;
}

/** A rendered inter-agent communication log entry. */
export interface AgentMessage {
  id: string;
  source: string;
  sessionId?: string;
  ts: string;
  to: string;
  text: string;
  level?: AlertSeverity;
}

/** A live alert-feed entry derived from an `alert.broadcast` envelope. */
export interface DashboardAlert {
  id: string;
  source: string;
  sessionId?: string;
  ts: string;
  severity: AlertSeverity;
  message: string;
  anomalyId: string;
  reasons: string[];
}

/** A session tile update (keyed by session id). */
export interface SessionUpdate extends SessionUpdatePayload {
  sessionId: string;
  ts: string;
}

/** The full hook return / store snapshot. */
export interface DashboardState {
  agents: Record<string, AgentStatus>;
  messages: AgentMessage[];
  alerts: DashboardAlert[];
  sessions: Record<string, SessionUpdate>;
  connected: boolean;
  degraded: boolean;
}

/** Partial state produced by a REST snapshot resync. */
export interface DashboardSnapshot {
  agents?: Record<string, AgentStatus>;
  sessions?: Record<string, SessionUpdate>;
}

// --- Tuning constants (Req 12.7 / 12.8) ------------------------------------

/** Inter-agent communication log ring-buffer cap (Req 12.7). */
export const MAX_MESSAGES = 500;
/** Reconnect backoff floor in milliseconds (Req 12.8). */
export const BACKOFF_BASE_MS = 1_000;
/** Reconnect backoff ceiling in milliseconds (Req 12.8). */
export const BACKOFF_CAP_MS = 30_000;
/** Maximum number of reconnect attempts (Req 12.8). */
export const MAX_RECONNECT_ATTEMPTS = 10;
/** Delay between REST snapshot retries while degraded (Req 12.9). */
export const SNAPSHOT_RETRY_MS = 2_000;

/**
 * Exponential backoff schedule for reconnect attempt `attempt` (1-indexed):
 * 1s, 2s, 4s, 8s, 16s, then capped at 30s (Req 12.8).
 */
export function backoffDelayMs(attempt: number): number {
  const exp = BACKOFF_BASE_MS * 2 ** (attempt - 1);
  return Math.min(exp, BACKOFF_CAP_MS);
}

// --- Injectable seams (tests) ----------------------------------------------

interface TimerScheduler {
  setTimeout(handler: () => void, ms: number): number;
  clearTimeout(handle: number): void;
}

const defaultScheduler: TimerScheduler = {
  setTimeout: (handler, ms) =>
    globalThis.setTimeout(handler, ms) as unknown as number,
  clearTimeout: (handle) => globalThis.clearTimeout(handle),
};

export interface DashboardSocketOptions {
  /** Override the WebSocket constructor (defaults to the global one). */
  webSocketImpl?: typeof WebSocket;
  /** Override the WS base URL (forwarded to the wsClient). */
  baseUrl?: string;
  /** REST snapshot used to resync state on (re)connect (Req 12.8/12.9). */
  snapshot?: () => Promise<DashboardSnapshot>;
  /** Injectable timer scheduler (defaults to global timers). */
  scheduler?: TimerScheduler;
  /** API client for the default snapshot implementation. */
  api?: ApiClient;
  maxReconnectAttempts?: number;
  snapshotRetryMs?: number;
}

/** Shape returned by `GET /agents/status` (best-effort default snapshot). */
interface AgentStatusCard {
  name: string;
  state: string;
  load: number;
  lastActionTs?: string;
}

/**
 * Default REST snapshot: pulls the live agent status cards from
 * `GET /agents/status` and folds them into the agents map. Tests inject their
 * own snapshot, so this only needs to be a reasonable production default.
 */
function makeDefaultSnapshot(api: ApiClient): () => Promise<DashboardSnapshot> {
  return async () => {
    const cards = await api.get<AgentStatusCard[]>("/agents/status");
    const agents: Record<string, AgentStatus> = {};
    if (Array.isArray(cards)) {
      for (const card of cards) {
        if (!card || typeof card.name !== "string") continue;
        agents[card.name] = {
          source: card.name,
          state: card.state,
          load: card.load,
          lastActionTs: card.lastActionTs,
          ts: card.lastActionTs ?? new Date().toISOString(),
        };
      }
    }
    return { agents };
  };
}

function emptyState(): DashboardState {
  return {
    agents: {},
    messages: [],
    alerts: [],
    sessions: {},
    connected: false,
    degraded: false,
  };
}

/**
 * Framework-agnostic controller holding the dashboard state machine. The React
 * hook is a thin wrapper; tests drive this directly with a fake socket + fake
 * timers for deterministic reconnect/backoff/buffer assertions.
 */
export class DashboardSocketController {
  private state: DashboardState = emptyState();
  private readonly listeners = new Set<() => void>();
  private conn: WsConnection | null = null;
  private reconnectAttempt = 0;
  private reconnectTimer: number | null = null;
  private snapshotTimer: number | null = null;
  private stopped = false;

  private readonly token: string;
  private readonly webSocketImpl?: typeof WebSocket;
  private readonly baseUrl?: string;
  private readonly snapshot: () => Promise<DashboardSnapshot>;
  private readonly scheduler: TimerScheduler;
  private readonly maxReconnectAttempts: number;
  private readonly snapshotRetryMs: number;

  constructor(token: string, options: DashboardSocketOptions = {}) {
    this.token = token;
    this.webSocketImpl = options.webSocketImpl;
    this.baseUrl = options.baseUrl;
    this.scheduler = options.scheduler ?? defaultScheduler;
    this.snapshot =
      options.snapshot ?? makeDefaultSnapshot(options.api ?? apiClient);
    this.maxReconnectAttempts =
      options.maxReconnectAttempts ?? MAX_RECONNECT_ATTEMPTS;
    this.snapshotRetryMs = options.snapshotRetryMs ?? SNAPSHOT_RETRY_MS;
  }

  // --- External store contract (useSyncExternalStore) ----------------------

  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };

  getSnapshot = (): DashboardState => this.state;

  private setState(patch: Partial<DashboardState>): void {
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
    if (this.reconnectTimer !== null) {
      this.scheduler.clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
    if (this.snapshotTimer !== null) {
      this.scheduler.clearTimeout(this.snapshotTimer);
      this.snapshotTimer = null;
    }
    if (this.conn) {
      this.conn.close();
      this.conn = null;
    }
  }

  private openConnection(): void {
    if (this.stopped) return;
    this.conn = connect({
      path: "/ws/dashboard",
      token: this.token,
      baseUrl: this.baseUrl,
      webSocketImpl: this.webSocketImpl,
      onOpen: () => this.handleOpen(),
      onClose: () => this.handleClose(),
      onMessage: (message) => this.handleMessage(message),
    });
  }

  private handleOpen(): void {
    this.reconnectAttempt = 0;
    this.setState({ connected: true });
    // Resync authoritative state from REST on every (re)connect (Req 12.8).
    void this.resync();
  }

  private handleClose(): void {
    this.conn = null;
    this.setState({ connected: false });
    this.scheduleReconnect();
  }

  private scheduleReconnect(): void {
    if (this.stopped) return;
    if (this.reconnectAttempt >= this.maxReconnectAttempts) {
      // Exhausted the retry budget (Req 12.8); stop trying.
      return;
    }
    this.reconnectAttempt += 1;
    const delay = backoffDelayMs(this.reconnectAttempt);
    this.reconnectTimer = this.scheduler.setTimeout(() => {
      this.reconnectTimer = null;
      this.openConnection();
    }, delay);
  }

  /**
   * Resynchronize state via the REST snapshot. On failure, surface a
   * connection-degraded indication and keep retrying (Req 12.9).
   */
  private async resync(): Promise<void> {
    if (this.stopped) return;
    try {
      const snap = await this.snapshot();
      if (this.stopped) return;
      this.setState({
        agents: { ...this.state.agents, ...(snap.agents ?? {}) },
        sessions: { ...this.state.sessions, ...(snap.sessions ?? {}) },
        degraded: false,
      });
    } catch {
      if (this.stopped) return;
      this.setState({ degraded: true });
      this.scheduleSnapshotRetry();
    }
  }

  private scheduleSnapshotRetry(): void {
    if (this.stopped) return;
    if (this.snapshotTimer !== null) {
      this.scheduler.clearTimeout(this.snapshotTimer);
    }
    this.snapshotTimer = this.scheduler.setTimeout(() => {
      this.snapshotTimer = null;
      void this.resync();
    }, this.snapshotRetryMs);
  }

  // --- Envelope folding ----------------------------------------------------

  private handleMessage(message: WSMessage): void {
    switch (message.type) {
      case "agent.message":
        this.appendMessage(message);
        break;
      case "agent.status":
        this.updateAgent(message);
        break;
      case "alert.broadcast":
        this.prependAlert(message);
        break;
      case "session.update":
        this.updateSession(message);
        break;
      default:
        // anomaly.detected / report.ready are not folded into this view.
        break;
    }
  }

  private appendMessage(message: WSMessage): void {
    const payload = message.payload as AgentMessagePayload;
    const entry: AgentMessage = {
      id: message.id,
      source: message.source,
      sessionId: message.sessionId,
      ts: message.ts,
      to: payload.to,
      text: payload.text,
      level: payload.level,
    };
    // Append then cap at the most recent MAX_MESSAGES, evicting oldest (12.7).
    const next = [...this.state.messages, entry];
    const capped =
      next.length > MAX_MESSAGES ? next.slice(next.length - MAX_MESSAGES) : next;
    this.setState({ messages: capped });
  }

  private updateAgent(message: WSMessage): void {
    const payload = message.payload as AgentStatusPayload;
    const entry: AgentStatus = {
      source: message.source,
      state: payload.state,
      load: payload.load,
      lastActionTs: payload.lastActionTs,
      ts: message.ts,
    };
    this.setState({
      agents: { ...this.state.agents, [message.source]: entry },
    });
  }

  private prependAlert(message: WSMessage): void {
    const payload = message.payload as AlertBroadcastPayload;
    const entry: DashboardAlert = {
      id: message.id,
      source: message.source,
      sessionId: message.sessionId,
      ts: message.ts,
      severity: payload.severity,
      message: payload.message,
      anomalyId: payload.anomalyId,
      reasons: payload.reasons,
    };
    this.setState({ alerts: [entry, ...this.state.alerts] });
  }

  private updateSession(message: WSMessage): void {
    const sessionId = message.sessionId;
    if (!sessionId) return;
    const payload = message.payload as SessionUpdatePayload;
    const entry: SessionUpdate = {
      sessionId,
      status: payload.status,
      integrityScore: payload.integrityScore,
      ts: message.ts,
    };
    this.setState({
      sessions: { ...this.state.sessions, [sessionId]: entry },
    });
  }
}

/**
 * React hook: open the dashboard socket for `token` and expose live dashboard
 * state. Connection management (reconnect/backoff/snapshot resync) lives in the
 * controller; the hook just wires it to React's lifecycle and render loop.
 */
export function useDashboardSocket(
  token: string,
  options: DashboardSocketOptions = {},
): DashboardState {
  // Keep the latest options without re-creating the controller every render.
  const optionsRef = useRef(options);
  optionsRef.current = options;

  const controllerRef = useRef<DashboardSocketController | null>(null);
  const tokenRef = useRef<string | null>(null);

  if (controllerRef.current === null || tokenRef.current !== token) {
    controllerRef.current = new DashboardSocketController(
      token,
      optionsRef.current,
    );
    tokenRef.current = token;
  }

  const controller = controllerRef.current;

  useEffect(() => {
    controller.start();
    return () => controller.stop();
  }, [controller]);

  return useSyncExternalStore(controller.subscribe, controller.getSnapshot);
}
