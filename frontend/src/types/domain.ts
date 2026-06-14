// Domain types mirroring the backend Pydantic schemas (app/schemas/*).
// Field names use the backend's snake_case wire shape so payloads can be
// consumed directly without translation.

import type {
  AlertSeverity,
  AnomalyCategory,
  AuditStatus,
  ExamStatus,
  QuestionType,
  Role,
  SessionEventKind,
  SessionStatus,
  SourceAgent,
} from "@/types/enums";

// --- Auth / User (app/schemas/user.py) -------------------------------------

/** Access + refresh token pair returned by login/refresh (backend TokenPair). */
export interface TokenPair {
  access_token: string;
  refresh_token: string;
  token_type: string;
}

/** Public user profile; never includes the password hash (backend UserRead). */
export interface UserRead {
  id: string;
  email: string;
  full_name: string;
  role: Role;
  created_at: string;
}

// --- Exam (app/schemas/exam.py) --------------------------------------------

/** A single topic entry within a blueprint (backend TopicSpec). */
export interface TopicSpec {
  name: string;
  count: number;
}

/** Exam specification (backend ExamBlueprint). */
export interface ExamBlueprint {
  topics: TopicSpec[];
  total_questions: number;
  difficulty_mix: Record<string, number>;
  question_types: QuestionType[];
}

/** Exam detail returned to admin/invigilator (backend ExamRead). */
export interface ExamRead {
  id: string;
  title: string;
  subject: string;
  // The backend serializes the blueprint as a free-form JSON object on read.
  blueprint: ExamBlueprint | Record<string, unknown>;
  duration_minutes: number;
  starts_at: string;
  status: ExamStatus;
  created_by: string;
}

// --- Questions / Papers (app/schemas/question.py) --------------------------

/**
 * Question as delivered to a student: NO answer key, ever.
 * Mirrors backend StudentQuestion (Req 4.9 / 5.3 / 14.1).
 */
export interface StudentQuestion {
  id: string;
  index: number;
  type: QuestionType;
  prompt: string;
  options: string[] | null;
  topic: string;
  max_marks: number;
}

/** A student's own paper with answer keys stripped (backend StudentPaper). */
export interface StudentPaper {
  id: string;
  exam_id: string;
  questions: StudentQuestion[];
}

/** Internal/admin view of a generated paper (backend PaperRead). */
export interface PaperRead {
  id: string;
  exam_id: string;
  student_id: string;
  seed: string;
  audit_status: AuditStatus;
  created_at: string;
}

// --- Sessions / Answers / Events (app/schemas/session.py) ------------------

/** Session state returned to owning student / invigilator / admin (SessionRead). */
export interface SessionRead {
  id: string;
  exam_id: string;
  student_id: string;
  paper_id: string;
  status: SessionStatus;
  started_at: string | null;
  submitted_at: string | null;
  integrity_score: number;
}

/** Submit or update an answer for an active session (backend AnswerSubmit). */
export interface AnswerSubmit {
  question_id: string;
  response: string;
}

/** Persisted answer; grading fields populated post-exam (backend AnswerRead). */
export interface AnswerRead {
  id: string;
  session_id: string;
  question_id: string;
  response: string;
  time_spent_ms: number;
  is_correct: boolean | null;
  awarded_marks: number | null;
}

/** A single inbound telemetry event (backend SessionEventIn). */
export interface SessionEventIn {
  kind: SessionEventKind;
  payload: Record<string, unknown>;
  client_ts?: string | null;
}

/** Batch of 1-100 session events (backend SessionEventBatch, Req 5.8/5.9). */
export interface SessionEventBatch {
  events: SessionEventIn[];
}

/** Persisted event with authoritative server timestamp (SessionEventRead). */
export interface SessionEventRead {
  id: string;
  session_id: string;
  kind: SessionEventKind;
  payload: Record<string, unknown>;
  client_ts?: string | null;
  server_ts: string;
}

// --- Anomalies / Alerts (app/schemas/anomaly.py) ---------------------------

/** Persisted anomaly view (backend AnomalyRead). */
export interface Anomaly {
  id: string;
  session_id: string;
  source_agent: SourceAgent;
  category: AnomalyCategory;
  score: number;
  reasons: string[];
  evidence: Record<string, unknown>;
  detected_at: string;
  confirmed: boolean;
}

/** Persisted alert view (backend AlertRead). */
export interface Alert {
  id: string;
  anomaly_id: string;
  session_id: string;
  severity: AlertSeverity;
  message: string;
  delivered_ws: boolean;
  delivered_email: boolean;
  created_at: string;
}

// --- Proctoring escalation (app/schemas/proctoring.py) ---------------------

/** Debounced Stage-1 local signal that triggered an escalation (LocalSignal). */
export interface LocalSignal {
  kind: string;
  duration_ms: number;
  confidence_local: number;
}

/** Inbound escalation payload: local signal + single frame (EscalationRequest). */
export interface EscalationRequest {
  local_signal: LocalSignal;
  frame: string;
}

/** Outbound escalation result derived from the Vision verdict (EscalationResponse). */
export interface EscalationResponse {
  anomaly_id: string | null;
  confirmed: boolean;
  category: string;
  score: number;
  reasons: string[];
  action: string;
}
