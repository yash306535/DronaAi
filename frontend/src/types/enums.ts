// Domain enumerations mirroring backend `app/models/enums.py`.
// Kept as string-literal unions (the wire format is the enum's string value).

/** The three RBAC roles recognized by the platform (backend Role). */
export type Role = "admin" | "invigilator" | "student";

/** Exam lifecycle states (backend ExamStatus). */
export type ExamStatus = "draft" | "provisioning" | "live" | "completed";

/** Supported question types (backend QuestionType). */
export type QuestionType = "mcq" | "short" | "numerical";

/** Auditor verdict states for a generated paper (backend AuditStatus). */
export type AuditStatus = "pending" | "approved" | "flagged";

/** Exam session lifecycle states (backend SessionStatus). */
export type SessionStatus =
  | "not_started"
  | "active"
  | "submitted"
  | "terminated";

/** Telemetry event kinds (backend SessionEventKind). */
export type SessionEventKind =
  | "tab_blur"
  | "tab_focus"
  | "paste"
  | "copy"
  | "answer_change"
  | "question_view"
  | "heartbeat";

/** Agents that can source an anomaly (backend SourceAgent). */
export type SourceAgent = "guardian" | "sentinel";

/** Anomaly categories (backend AnomalyCategory). */
export type AnomalyCategory =
  | "face_absent"
  | "multiple_faces"
  | "gaze_away"
  | "tab_switch"
  | "paste"
  | "timing"
  | "answer_similarity";

/** Alert severity enumeration (backend AlertSeverity). */
export type AlertSeverity = "info" | "warning" | "danger";
