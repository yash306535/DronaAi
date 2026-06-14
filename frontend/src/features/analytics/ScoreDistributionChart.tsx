import {
  Bar,
  BarChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { brand } from "@/theme";
import {
  toDistributionBars,
  type DistributionBar,
} from "@/features/analytics/analyticsView";
import type { AnalyticsSummary } from "@/features/analytics/types";

export interface ScoreDistributionChartProps {
  summary: AnalyticsSummary | undefined;
  /** Test seam: bypass the summary transform with explicit bars. */
  bars?: DistributionBar[];
}

/**
 * Score distribution bar chart over the Analyst's fixed score bands
 * (Requirement 10.2). Bars are ordered low→high band; each bar's height is the
 * count of students whose score fell in that band.
 */
export function ScoreDistributionChart({
  summary,
  bars,
}: ScoreDistributionChartProps) {
  const data = bars ?? toDistributionBars(summary);

  if (data.length === 0) {
    return (
      <p className="text-sm text-on-surface-muted">
        No score distribution available yet.
      </p>
    );
  }

  return (
    <div
      data-testid="score-distribution"
      data-bands={data.length}
      className="h-64 w-full"
    >
      <ResponsiveContainer width="100%" height="100%">
        <BarChart data={data} margin={{ top: 8, right: 8, bottom: 8, left: 0 }}>
          <CartesianGrid strokeDasharray="3 3" stroke="rgba(90,107,160,0.25)" />
          <XAxis
            dataKey="band"
            tick={{ fontSize: 11, fill: "currentColor" }}
            stroke="currentColor"
          />
          <YAxis
            allowDecimals={false}
            tick={{ fontSize: 11, fill: "currentColor" }}
            stroke="currentColor"
          />
          <Tooltip
            cursor={{ fill: "rgba(47,69,118,0.2)" }}
            contentStyle={{
              background: brand.navy[800],
              border: "none",
              borderRadius: 6,
              color: "#fff",
              fontSize: 12,
            }}
          />
          <Bar
            dataKey="count"
            name="Students"
            fill={brand.crimson[600]}
            radius={[4, 4, 0, 0]}
          />
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}
