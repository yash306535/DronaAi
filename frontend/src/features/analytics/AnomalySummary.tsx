import { StatPill } from "@/components";
import {
  toAnomalySummaryRows,
  type AnomalySummaryRow,
} from "@/features/analytics/analyticsView";
import type { AnalyticsSummary } from "@/features/analytics/types";

export interface AnomalySummaryProps {
  summary: AnalyticsSummary | undefined;
  /** Test seam: bypass the summary transform with explicit rows. */
  rows?: AnomalySummaryRow[];
}

/**
 * Anomaly summary: counts of flagged anomalies for the exam (Requirement 10.2).
 * Each count is rendered as a labeled {@link StatPill} whose severity escalates
 * with the count (none → info, otherwise danger) so meaning is never carried by
 * color alone.
 */
export function AnomalySummary({ summary, rows }: AnomalySummaryProps) {
  const data = rows ?? toAnomalySummaryRows(summary);

  return (
    <ul
      data-testid="anomaly-summary"
      className="flex flex-wrap items-center gap-3"
    >
      {data.map((row) => (
        <li key={row.label} className="flex items-center gap-2">
          <span className="text-sm text-on-surface-muted">{row.label}</span>
          <StatPill severity={row.count > 0 ? "danger" : "info"}>
            {row.count}
          </StatPill>
        </li>
      ))}
    </ul>
  );
}
