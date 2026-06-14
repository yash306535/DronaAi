// WebSocket message envelope + typed payloads.
// Mirrors backend `app/schemas/ws.py` (WSMessageType / WSMessage) and the
// design's "WebSocket Event Schema" payload examples.

import type { AlertSeverity } from "@/types/enums";

/**
 * The public set of message types carried over the live socket.
 * Mirrors backend WSMessageType. The heartbeat ping/pong control frames are
 * intentionally NOT part of this public schema; they are handled by the
 * connection manager / ws client directly.
 */
export type WSMessageType =
  | "agent.message"
  | "agent.status"
  | "anomaly.detected"
  | "alert.broadcast"
  | "session.update"
  | "report.ready";

/**
 * The shared WebSocket message envelope (design "Message Envelope").
 * Note the wire shape uses camelCase `sessionId` (see backend to_envelope),
 * unlike the snake_case REST schemas.
 */
export interface WSMessage<T = unknown> {
  type: WSMessageType;
  id: string;
  ts: string;
  sessionId?: string;
  source: string;
  payload: T;
}

// --- Typed payloads (design "Payload Examples") ----------------------------

/** `agent.message` — rendered in the live inter-agent communication log. */
export interface AgentMessagePayload {
  to: string;
  text: string;
  level?: AlertSeverity;
}

/** `agent.status` — drives the agent status cards. */
export interface AgentStatusPayload {
  state: string;
  load: number;
  lastActionTs?: string;
}

/** `alert.broadcast` — pushes a card into the live alert feed. */
export interface AlertBroadcastPayload {
  severity: AlertSeverity;
  message: string;
  anomalyId: string;
  reasons: string[];
}

/** `session.update` — session status / integrity score change. */
export interface SessionUpdatePayload {
  status?: string;
  integrityScore?: number;
}

/** `report.ready` — analytics finished for an exam. */
export interface ReportReadyPayload {
  examId: string;
}

/**
 * Maps each message type to its payload shape so listeners can be typed.
 * `anomaly.detected` carries the domain Anomaly shape on the wire.
 */
export interface WSPayloadMap {
  "agent.message": AgentMessagePayload;
  "agent.status": AgentStatusPayload;
  "anomaly.detected": unknown;
  "alert.broadcast": AlertBroadcastPayload;
  "session.update": SessionUpdatePayload;
  "report.ready": ReportReadyPayload;
}

/** A fully-typed envelope for a specific message type. */
export type TypedWSMessage<K extends WSMessageType = WSMessageType> =
  WSMessage<WSPayloadMap[K]> & { type: K };
