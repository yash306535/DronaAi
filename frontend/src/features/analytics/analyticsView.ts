// Pure view-model helpers for the analytics feature (task 22).
//
// These translate the raw `AnalyticsView` payload (the backend
// `ExamAnalyticsRead` shape) into the row/series structures the Recharts views
// and the heatmap grid consume. They are kept pure (no React, no Recharts) so
// the band-ordering, percentage clamping, and aggregation rules can be
// unit-tested directly (task 22.1) without rendering.

import type {
  AnalyticsSummary,
  AnalyticsView,
  DifficultyHeatmap,
  PerStudent,
} from "@/features/analytics/types";

/** A single bar in the score-distribution chart. */
export interface DistributionBar {
  /** Fixed band label, e.g. "0-10" or "90-100". */
  band: string;
  /** Count of students whose score fell in this band. */
  count: number;
}

/**
 * The numeric lower bound of a fixed score band label ("30-40" → 30). Labels
 * that don't parse sort last (returns +Infinity) so the chart stays stable even
 * on an unexpected key.
 */
export function bandLowerBound(band: string): number {
  const lead = band.split("-", 1)[0];
  const n = Number.parseInt(lead, 10);
  return Number.isNaN(n) ? Number.POSITIVE_INFINITY : n;
}

/**
 * Build the ordered score-distribution series from the summary (Requirement
 * 10.2). Bands are sorted by their numeric lower bound so "0-10" precedes
 * "90-100" regardless of object key order. A missing/empty distribution yields
 * an empty series.
 */
export function toDistributionBars(
  summary: AnalyticsSummary | undefined,
): DistributionBar[] {
  const distribution = summary?.distribution ?? {};
  return Object.entries(distribution)
    .map(([band, count]) => ({ band, count: Number(count) || 0 }))
    .sort((a, b) => bandLowerBound(a.band) - bandLowerBound(b.band));
}

/** A single row in the difficulty/accuracy heatmap grid. */
export interface HeatmapRow {
  topic: string;
  /** Accuracy 0..100 percent (drives the HeatmapCell color scale). */
  accuracy: number;
  /** Difficulty 0..100 percent. */
  difficulty: number;
}

function clamp100(value: number): number {
  if (Number.isNaN(value)) return 0;
  if (value < 0) return 0;
  if (value > 100) return 100;
  return value;
}

/**
 * Build the heatmap rows from the difficulty-heatmap section (Requirement
 * 10.3), one row per topic, sorted by topic name for a stable grid. Accuracy
 * and difficulty are clamped to the inclusive 0..100 range.
 */
export function toHeatmapRows(
  heatmap: DifficultyHeatmap | undefined,
): HeatmapRow[] {
  const topics = heatmap?.topics ?? {};
  return Object.entries(topics)
    .map(([topic, value]) => ({
      topic,
      accuracy: clamp100(Number(value?.accuracy)),
      difficulty: clamp100(Number(value?.difficulty)),
    }))
    .sort((a, b) => a.topic.localeCompare(b.topic));
}

/** A row in the anomaly summary (a single counted category). */
export interface AnomalySummaryRow {
  label: string;
  count: number;
}

/**
 * Build the anomaly summary rows. The Analyst's summary currently carries a
 * single flagged-anomaly total (`anomalyCount`); this surfaces it as a labeled
 * row so the view renders a consistent counts list. Defaults to 0 when absent.
 */
export function toAnomalySummaryRows(
  summary: AnalyticsSummary | undefined,
): AnomalySummaryRow[] {
  return [
    { label: "Flagged anomalies", count: summary?.anomalyCount ?? 0 },
  ];
}

/** A per-student report row (score, topic accuracy, suggestions). */
export interface StudentReportRow {
  studentId: string;
  score: number;
  topicAccuracy: Array<{ topic: string; accuracy: number }>;
  suggestions: string[];
  suggestionsPending: boolean;
}

/**
 * Build the per-student report rows (Requirement 10.4), sorted by student id
 * for a stable list. Each row exposes the student's score, per-topic accuracy
 * (sorted by topic), suggestions, and whether the suggestions section is still
 * pending (partial report, Requirement 10.6).
 */
export function toStudentReportRows(
  perStudent: PerStudent | undefined,
): StudentReportRow[] {
  const students = perStudent?.students ?? {};
  return Object.entries(students)
    .map(([studentId, entry]) => ({
      studentId,
      score: Number(entry?.score) || 0,
      topicAccuracy: Object.entries(entry?.topicAccuracy ?? {})
        .map(([topic, accuracy]) => ({
          topic,
          accuracy: clamp100(Number(accuracy)),
        }))
        .sort((a, b) => a.topic.localeCompare(b.topic)),
      suggestions: Array.isArray(entry?.suggestions) ? entry.suggestions : [],
      suggestionsPending: entry?.suggestionsStatus === "pending",
    }))
    .sort((a, b) => a.studentId.localeCompare(b.studentId));
}

/** Whether any section of the report is still pending (partial report). */
export function isReportPending(view: AnalyticsView | undefined): boolean {
  if (!view) return false;
  return (
    view.summary?.status === "pending" ||
    view.difficulty_heatmap?.status === "pending" ||
    view.per_student?.status === "pending"
  );
}
