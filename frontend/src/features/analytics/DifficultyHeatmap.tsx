import { HeatmapCell } from "@/components";
import {
  toHeatmapRows,
  type HeatmapRow,
} from "@/features/analytics/analyticsView";
import type { DifficultyHeatmap as DifficultyHeatmapData } from "@/features/analytics/types";

export interface DifficultyHeatmapProps {
  heatmap: DifficultyHeatmapData | undefined;
  /** Test seam: bypass the heatmap transform with explicit rows. */
  rows?: HeatmapRow[];
}

/**
 * Per-topic difficulty/accuracy heatmap (Requirement 10.3). One row per topic;
 * the accuracy cell uses the shared {@link HeatmapCell} color scale
 * (crimson low → amber → green high) so low accuracy reads as danger. The
 * difficulty value is shown alongside as a percentage.
 */
export function DifficultyHeatmap({ heatmap, rows }: DifficultyHeatmapProps) {
  const data = rows ?? toHeatmapRows(heatmap);

  if (data.length === 0) {
    return (
      <p className="text-sm text-on-surface-muted">
        No topic heatmap available yet.
      </p>
    );
  }

  return (
    <table
      data-testid="difficulty-heatmap"
      className="w-full border-separate border-spacing-y-1 text-sm"
    >
      <thead>
        <tr className="text-left text-xs uppercase tracking-wide text-on-surface-muted">
          <th scope="col" className="px-2 py-1 font-semibold">
            Topic
          </th>
          <th scope="col" className="px-2 py-1 font-semibold">
            Accuracy
          </th>
          <th scope="col" className="px-2 py-1 font-semibold">
            Difficulty
          </th>
        </tr>
      </thead>
      <tbody>
        {data.map((row) => (
          <tr key={row.topic} data-topic={row.topic}>
            <td className="px-2 py-1 font-medium">{row.topic}</td>
            <td className="px-2 py-1">
              <HeatmapCell
                accuracy={row.accuracy}
                label={`${row.accuracy}%`}
                title={`${row.topic}: accuracy ${row.accuracy}%`}
              />
            </td>
            <td className="px-2 py-1 font-mono text-xs text-on-surface-muted">
              {row.difficulty}%
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
