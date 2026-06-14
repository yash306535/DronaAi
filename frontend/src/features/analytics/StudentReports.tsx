import { HeatmapCell, StatPill } from "@/components";
import {
  toStudentReportRows,
  type StudentReportRow,
} from "@/features/analytics/analyticsView";
import type { PerStudent } from "@/features/analytics/types";

export interface StudentReportsProps {
  perStudent: PerStudent | undefined;
  /** Test seam: bypass the per-student transform with explicit rows. */
  rows?: StudentReportRow[];
}

/**
 * Per-student reports + improvement suggestions (Requirement 10.4). Each card
 * shows the student's score, per-topic accuracy (using the shared
 * {@link HeatmapCell} color scale), and the list of suggestions. When the
 * suggestions section is still pending (partial report, Requirement 10.6) a
 * pending pill is shown instead of an empty list.
 */
export function StudentReports({ perStudent, rows }: StudentReportsProps) {
  const data = rows ?? toStudentReportRows(perStudent);

  if (data.length === 0) {
    return (
      <p className="text-sm text-on-surface-muted">
        No student reports available yet.
      </p>
    );
  }

  return (
    <ul data-testid="student-reports" className="flex flex-col gap-3">
      {data.map((row) => (
        <li
          key={row.studentId}
          data-student={row.studentId}
          className="rounded-md border border-hairline bg-surface-1 p-3"
        >
          <div className="flex items-center justify-between gap-2">
            <span className="font-mono text-xs text-on-surface-muted">
              {row.studentId}
            </span>
            <span className="text-sm font-semibold">
              Score {row.score.toFixed(2)}%
            </span>
          </div>

          {row.topicAccuracy.length > 0 && (
            <div className="mt-2 flex flex-wrap gap-2">
              {row.topicAccuracy.map((t) => (
                <HeatmapCell
                  key={t.topic}
                  accuracy={t.accuracy}
                  label={`${t.topic} ${t.accuracy}%`}
                  title={`${t.topic}: accuracy ${t.accuracy}%`}
                />
              ))}
            </div>
          )}

          <div className="mt-3">
            <div className="mb-1 flex items-center gap-2">
              <h4 className="text-xs font-semibold uppercase tracking-wide text-on-surface-muted">
                Suggestions
              </h4>
              {row.suggestionsPending && (
                <StatPill severity="warning">Pending</StatPill>
              )}
            </div>
            {row.suggestions.length > 0 ? (
              <ul className="list-disc space-y-0.5 pl-5 text-sm">
                {row.suggestions.map((s, i) => (
                  <li key={i}>{s}</li>
                ))}
              </ul>
            ) : (
              <p className="text-sm text-on-surface-muted">
                {row.suggestionsPending
                  ? "Suggestions are still being generated."
                  : "No suggestions."}
              </p>
            )}
          </div>
        </li>
      ))}
    </ul>
  );
}
