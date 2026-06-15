// Pure view-model helpers for the live admin dashboard (task 17.2).
//
// These functions translate the raw live state produced by `useDashboardSocket`
// into the props the shared presentational components expect. They are kept
// pure (no React) so the truncation / severity / agent-state mapping rules can
// be unit-tested directly (task 17.3) without rendering.

import type { AgentState } from "@/components";
import type { Severity } from "@/theme";
import type { AlertSeverity, SessionStatus } from "@/types";
import type {
  AgentStatus,
  DashboardAlert,
  SessionUpdate,
} from "@/features/dashboard/useDashboardSocket";

/**
 * Inter-agent communication log message-text cap (Requirement 12.3): the
 * dashboard truncates any `agent.message` text longer than 2,000 characters
 * before rendering it.
 */
export const MAX_MESSAGE_TEXT_LENGTH = 2_000;

/**
 * Truncate inter-agent message text to {@link MAX_MESSAGE_TEXT_LENGTH}
 * characters (Requirement 12.3). When truncation occurs the final character is
 * replaced with an ellipsis so the result never exceeds the cap and the
 * truncation is visible. Text at or below the cap is returned unchanged.
 */
export function truncateMessageText(text: string): string {
  if (text.length <= MAX_MESSAGE_TEXT_LENGTH) return text;
  return text.slice(0, MAX_MESSAGE_TEXT_LENGTH - 1) + "\u2026";
}

/**
 * Map a live `agent.status` state string onto the shared {@link AgentState}
 * scale used by `AgentCard`. Unknown / resting states fall back to `idle`.
 */
export function toAgentState(state: string | undefined): AgentState {
  switch (state) {
    case "alerting":
    case "danger":
      return "alerting";
    case "working":
    case "active":
    case "busy":
      return "working";
    case "idle":
    case "ready":
    case "":
    case undefined:
      return "idle";
    default:
      return "idle";
  }
}

/**
 * `alert.broadcast` severity → the four-level severity scale. `AlertSeverity`
 * is a strict subset of `Severity`, so this is a safe widening with a defensive
 * fallback for any unexpected value coming off the wire.
 */
export function toAlertSeverity(severity: AlertSeverity | string): Severity {
  switch (severity) {
    case "info":
    case "warning":
    case "danger":
      return severity;
    default:
      return "info";
  }
}

const KNOWN_SESSION_STATUSES: readonly SessionStatus[] = [
  "not_started",
  "active",
  "submitted",
  "terminated",
];

/**
 * Coerce a live `session.update` status string onto the {@link SessionStatus}
 * union expected by `SessionTile`, defaulting to `active` for live sessions
 * with an unrecognized / absent status.
 */
export function toSessionStatus(status: string | undefined): SessionStatus {
  if (status && (KNOWN_SESSION_STATUSES as readonly string[]).includes(status)) {
    return status as SessionStatus;
  }
  return "active";
}

/**
 * Normalize a possibly-fractional (0..1) or already-percentage (0..100)
 * integrity score onto the 0..100 scale `SessionTile` renders. Scores at or
 * below 1 are treated as a 0..1 fraction; higher values are treated as a
 * percentage. Missing scores default to a clean 100.
 */
export function toIntegrityPercent(score: number | undefined): number {
  if (score === undefined || Number.isNaN(score)) return 100;
  const pct = score <= 1 ? score * 100 : score;
  if (pct < 0) return 0;
  if (pct > 100) return 100;
  return pct;
}

/** A single agent slot rendered in the Agent Status Strip. */
export interface AgentSlot {
  name: string;
  role: string;
  state: AgentState;
  load: number;
}

/** Default agent roster + one-line roles, so the strip is populated before
 * any `agent.status` event arrives. Mirrors the design's six-agent lineup. */
export const AGENT_ROSTER: ReadonlyArray<{ name: string; role: string }> = [
  { name: "Guardian", role: "Face + gaze proctoring" },
  { name: "Architect", role: "Unique paper generation" },
  { name: "Sentinel", role: "Behavioral fraud detection" },
  { name: "Analyst", role: "Post-exam analytics" },
  { name: "Herald", role: "Real-time alert broadcasting" },
  { name: "Auditor", role: "Question fairness audit" },
];

/**
 * Build the ordered list of agent slots for the status strip: the known roster
 * overlaid with any live `agent.status` state, followed by any live agents not
 * in the roster (so unexpected agents still surface).
 */
export function buildAgentSlots(
  agents: Record<string, AgentStatus>,
): AgentSlot[] {
  const slots: AgentSlot[] = AGENT_ROSTER.map(({ name, role }) => {
    const live = agents[name];
    return {
      name,
      role,
      state: toAgentState(live?.state),
      load: live?.load ?? 0,
    };
  });

  const known = new Set(AGENT_ROSTER.map((a) => a.name));
  for (const [name, live] of Object.entries(agents)) {
    if (known.has(name)) continue;
    slots.push({
      name,
      role: "Agent",
      state: toAgentState(live.state),
      load: live.load ?? 0,
    });
  }
  return slots;
}

/** Connection indicator status derived from the hook's connected/degraded. */
export type ConnectionStatus = "connected" | "degraded" | "disconnected";

/** Derive the top-bar connection dot status from the live hook flags. */
export function connectionStatus(
  connected: boolean,
  degraded: boolean,
): ConnectionStatus {
  if (!connected) return "disconnected";
  if (degraded) return "degraded";
  return "connected";
}

/** Severity for the connection dot so it reuses the shared color scale. */
export function connectionSeverity(status: ConnectionStatus): Severity {
  switch (status) {
    case "connected":
      return "success";
    case "degraded":
      return "warning";
    case "disconnected":
      return "danger";
  }
}

/** Human-readable label for the connection status (never color-only). */
export function connectionLabel(status: ConnectionStatus): string {
  switch (status) {
    case "connected":
      return "Connected";
    case "degraded":
      return "Degraded";
    case "disconnected":
      return "Disconnected";
  }
}

/** Format an ISO-8601 timestamp as a short HH:MM:SS for the log / feed. */
export function formatClockTime(iso: string | undefined): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  return d.toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

/** A row-ready view model for an alert-feed entry. */
export interface AlertView {
  id: string;
  severity: Severity;
  title: string;
  reasons: string[];
  timestamp: string;
}

/** Map a live dashboard alert to AlertItem-ready props. */
export function toAlertView(alert: DashboardAlert): AlertView {
  return {
    id: alert.id,
    severity: toAlertSeverity(alert.severity),
    title: alert.message,
    reasons: alert.reasons,
    timestamp: formatClockTime(alert.ts),
  };
}

/** A tile-ready view model for a session-grid entry. */
export interface SessionView {
  sessionId: string;
  name: string;
  integrityPercent: number;
  status: SessionStatus;
}

/** Map a live session update to SessionTile-ready props. */
export function toSessionView(session: SessionUpdate): SessionView {
  return {
    sessionId: session.sessionId,
    name: session.sessionId,
    integrityPercent: toIntegrityPercent(session.integrityScore),
    status: toSessionStatus(session.status),
  };
}

/** Map a REST `SessionRead` row (0..100 integrity) to SessionTile-ready props. */
export function restSessionToView(session: {
  id: string;
  student_id: string;
  integrity_score: number;
  status: string;
}): SessionView {
  return {
    sessionId: session.id,
    name: session.id.slice(0, 8),
    integrityPercent: toIntegrityPercent(session.integrity_score),
    status: toSessionStatus(session.status),
  };
}

/** Map a REST `Alert` row to AlertItem-ready props. */
export function restAlertToView(alert: {
  id: string;
  severity: AlertSeverity | string;
  message: string;
  created_at: string;
}): AlertView {
  return {
    id: alert.id,
    severity: toAlertSeverity(alert.severity),
    title: alert.message,
    reasons: [],
    timestamp: formatClockTime(alert.created_at),
  };
}
