import { cn } from "@/components/classNames";
import { scoreToScaleColor } from "@/components/colorScale";

export interface HeatmapCellProps {
  /**
   * Accuracy value in the inclusive range 0–100 percent (Analyst heatmap,
   * Requirement 10.3). Drives the fill on the crimson(low)→amber→green(high)
   * scale.
   */
  accuracy: number;
  /** Optional label rendered inside the cell (e.g. a topic name or score). */
  label?: string;
  /** Optional accessible description; defaults to the accuracy percentage. */
  title?: string;
  className?: string;
}

function clamp100(value: number): number {
  if (Number.isNaN(value)) return 0;
  if (value < 0) return 0;
  if (value > 100) return 100;
  return value;
}

/**
 * A single difficulty/accuracy heatmap cell. The background is driven by the
 * accuracy value on the shared crimson(low)→amber→green(high) scale so low
 * accuracy reads as danger and high accuracy as success.
 */
export function HeatmapCell({
  accuracy,
  label,
  title,
  className,
}: HeatmapCellProps) {
  const pct = clamp100(accuracy);
  const bg = scoreToScaleColor(pct / 100);

  return (
    <div
      className={cn(
        "flex min-h-8 min-w-8 items-center justify-center rounded px-2 py-1 text-xs font-medium text-white",
        className,
      )}
      style={{ backgroundColor: bg }}
      title={title ?? `Accuracy ${pct}%`}
      data-accuracy={pct}
    >
      {label}
    </div>
  );
}
