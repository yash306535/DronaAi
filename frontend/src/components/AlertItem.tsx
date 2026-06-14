import type { ReactNode } from "react";
import { cn } from "@/components/classNames";
import {
  getSeverityColors,
  SEVERITY_LABELS,
  type Severity,
} from "@/theme";

export interface AlertItemProps {
  /** Severity drives the color of the left bar and the visible text label. */
  severity: Severity;
  /** Short alert title / message. */
  title: string;
  /** Explainability reasons, rendered in the mono typeface. */
  reasons?: string[];
  /** ISO-8601 (or pre-formatted) timestamp string. */
  timestamp?: string;
  /** Optional link target for the related session. */
  sessionHref?: string;
  /** Optional label for the session link (defaults to "View session"). */
  sessionLabel?: string;
  /** Optional leading icon (paired with the text label, never color-only). */
  icon?: ReactNode;
  className?: string;
}

/**
 * A single alert row for the live alert feed.
 *
 * Severity is conveyed by BOTH the mapped color (left bar + label chip) AND a
 * visible text label (Requirement 16.3) — never color alone. `danger` items
 * get a subtle crimson glow. Intended to be rendered inside an `AlertFeed`
 * (or any container with `aria-live="polite"`, Requirement 16.7).
 */
export function AlertItem({
  severity,
  title,
  reasons,
  timestamp,
  sessionHref,
  sessionLabel = "View session",
  icon,
  className,
}: AlertItemProps) {
  const colors = getSeverityColors(severity);
  const label = SEVERITY_LABELS[severity];

  return (
    <article
      className={cn(
        "relative flex gap-3 rounded-md bg-surface-1 p-3 pl-4 text-on-surface shadow-sm",
        severity === "danger" && "shadow-[0_0_0_1px_rgba(179,36,59,0.35),0_4px_16px_rgba(179,36,59,0.25)]",
        className,
      )}
      data-severity={severity}
    >
      {/* Left severity bar (color) */}
      <span
        aria-hidden="true"
        className="absolute left-0 top-0 h-full w-1 rounded-l-md"
        style={{ backgroundColor: colors.text }}
      />

      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2">
          {/* Severity label chip: color + TEXT (never color alone, 16.3) */}
          <span
            className="inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-[11px] font-semibold uppercase tracking-wide"
            style={{ backgroundColor: colors.background, color: colors.text }}
          >
            {icon}
            {label}
          </span>
          <span className="truncate text-sm font-medium">{title}</span>
        </div>

        {reasons && reasons.length > 0 && (
          <ul className="mt-1.5 space-y-0.5 font-mono text-xs text-on-surface-muted">
            {reasons.map((reason, i) => (
              <li key={i}>• {reason}</li>
            ))}
          </ul>
        )}

        {(timestamp || sessionHref) && (
          <div className="mt-1.5 flex items-center gap-3 text-[11px] text-on-surface-muted">
            {timestamp && <time dateTime={timestamp}>{timestamp}</time>}
            {sessionHref && (
              <a
                href={sessionHref}
                className="focus-ring rounded underline hover:text-on-surface"
              >
                {sessionLabel}
              </a>
            )}
          </div>
        )}
      </div>
    </article>
  );
}
