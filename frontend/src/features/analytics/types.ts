// Analytics-local types mirroring the backend `ExamAnalyticsRead`
// (app/schemas/analytics.py) and the Analyst agent's internal JSON shapes for
// `summary`, `difficulty_heatmap`, and `per_student` (app/agents/analyst.py).
//
// The backend surfaces those three columns as free-form `dict`s, so the precise
// shape is owned by the Analyst rather than the shared schema. These types are
// kept local to the feature (rather than in src/types) to match the wire shape
// the Analyst actually emits without coupling the shared type module to a
// loosely-typed payload.

/** A section's completion status (Analyst STATUS_READY / STATUS_PENDING). */
export type SectionStatus = "ready" | "pending";

/**
 * Score summary section (Requirement 10.2): a fixed-band distribution covering
 * the full 0..100 range, the arithmetic mean (2 dp), and the flagged-anomaly
 * count.
 */
export interface AnalyticsSummary {
  /** Band label (e.g. "0-10", "90-100") → count of students in that band. */
  distribution: Record<string, number>;
  /** Arithmetic mean score, rounded to 2 decimals. */
  mean: number;
  /** Total count of flagged anomalies across the exam. */
  anomalyCount: number;
  /** Number of students who completed the exam. */
  completedStudents?: number;
  status?: SectionStatus;
}

/** Per-topic accuracy + difficulty, each 0..100 percent (Requirement 10.3). */
export interface HeatmapTopic {
  accuracy: number;
  difficulty: number;
}

/** Difficulty heatmap section: topic → accuracy/difficulty (Requirement 10.3). */
export interface DifficultyHeatmap {
  topics: Record<string, HeatmapTopic>;
  status?: SectionStatus;
}

/** A single student's report entry (score, topic accuracy, suggestions). */
export interface PerStudentEntry {
  score: number;
  topicAccuracy: Record<string, number>;
  suggestions: string[];
  suggestionsStatus?: SectionStatus;
}

/** Per-student section: student id → report + suggestions (Requirement 10.4). */
export interface PerStudent {
  students: Record<string, PerStudentEntry>;
  status?: SectionStatus;
}

/**
 * Full analytics record returned by `GET /analytics/exams/{id}`, matching the
 * backend `ExamAnalyticsRead`. The three section dicts are typed to the
 * Analyst's emitted shape; fields are kept optional/defensive so a partial
 * report (Requirement 10.6) still renders.
 */
export interface AnalyticsView {
  id: string;
  exam_id: string;
  summary: AnalyticsSummary;
  difficulty_heatmap: DifficultyHeatmap;
  per_student: PerStudent;
  generated_at: string;
}
