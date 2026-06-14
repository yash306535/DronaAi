import type { ReactNode } from "react";
import { cn } from "@/components/classNames";
import {
  getSeverityColors,
  SEVERITY_LABELS,
  type Severity,
} from "@/theme";

export interface StatPillProps {
  /** Severity drives the semantic bg/text pair from the shared mapping. */
  severity: Severity;
  /**
   * Pill content. When omitted, the severity's human-readable label is used so
   * the pill always carries a text label (never color-only, Requirement 16.3).
   */
  children?: ReactNode;
  className?: string;
}

/**
 * A small rounded status pill using the semantic background/text pair from the
 * severity → color mapping. Always renders a visible text label so meaning is
 * never conveyed by color alone (Requirement 16.3).
 */
export function StatPill({ severity, children, className }: StatPillProps) {
  const colors = getSeverityColors(severity);
  const content = children ?? SEVERITY_LABELS[severity];

  return (
    <span
      className={cn(
        "inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-medium",
        className,
      )}
      style={{ backgroundColor: colors.background, color: colors.text }}
      data-severity={severity}
    >
      {content}
    </span>
  );
}
