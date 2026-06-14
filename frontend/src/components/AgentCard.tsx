import type { ReactNode } from "react";
import { cn } from "@/components/classNames";
import { getSeverityColors, SEVERITY_LABELS } from "@/theme";

/**
 * Live agent lifecycle state shown on the dashboard Agent Status Strip.
 * - idle:     resting; mapped to `success` (clean / nominal)
 * - working:  actively processing; mapped to `info` with a pulse
 * - alerting: just emitted an anomaly; mapped to `danger` (crimson)
 */
export type AgentState = "idle" | "working" | "alerting";

/** Map an agent state to the shared four-level severity scale. */
const STATE_SEVERITY = {
  idle: "success",
  working: "info",
  alerting: "danger",
} as const;

/** Human-readable label for each agent state (the text half of color+text). */
const STATE_LABEL: Record<AgentState, string> = {
  idle: "Idle",
  working: "Working",
  alerting: "Alerting",
};

export interface AgentCardProps {
  /** Agent name, e.g. "Guardian". */
  name: string;
  /** Agent role / one-line goal. */
  role: string;
  /** Current live state driving the status dot. */
  state: AgentState;
  /** Normalized work load in [0,1] driving the load bar. */
  load?: number;
  /** Optional icon rendered inside the tinted chip. */
  icon?: ReactNode;
  className?: string;
}

function clamp01(value: number): number {
  if (Number.isNaN(value)) return 0;
  if (value < 0) return 0;
  if (value > 1) return 1;
  return value;
}

/**
 * Agent status card: surface-1 card, tinted icon chip, name + role, a live
 * state dot, and a load bar. The dot conveys state with BOTH color and a
 * visible (sr-only-friendly) label.
 */
export function AgentCard({
  name,
  role,
  state,
  load = 0,
  icon,
  className,
}: AgentCardProps) {
  const severity = STATE_SEVERITY[state];
  const colors = getSeverityColors(severity);
  const stateLabel = STATE_LABEL[state];
  const loadPct = Math.round(clamp01(load) * 100);

  return (
    <article
      className={cn(
        "flex flex-col gap-3 rounded-md bg-surface-1 p-4 shadow-sm",
        "border border-hairline text-on-surface",
        className,
      )}
      data-agent-state={state}
      data-severity={severity}
    >
      <div className="flex items-center gap-3">
        <span
          aria-hidden="true"
          className="flex h-9 w-9 items-center justify-center rounded-md text-base font-semibold"
          style={{ backgroundColor: colors.background, color: colors.text }}
        >
          {icon ?? name.charAt(0).toUpperCase()}
        </span>
        <div className="min-w-0 flex-1">
          <div className="truncate text-sm font-semibold">{name}</div>
          <div className="truncate text-xs text-on-surface-muted">{role}</div>
        </div>
        <span className="flex items-center gap-1.5">
          <span
            aria-hidden="true"
            className={cn(
              "inline-block h-2.5 w-2.5 rounded-full",
              state === "working" && "animate-pulse",
            )}
            style={{ backgroundColor: colors.text }}
          />
          <span className="text-[11px] font-medium uppercase tracking-wide text-on-surface-muted">
            {stateLabel}
          </span>
        </span>
      </div>

      <div>
        <div className="mb-1 flex justify-between text-[11px] text-on-surface-muted">
          <span>Load</span>
          <span>{loadPct}%</span>
        </div>
        <div
          className="h-1.5 w-full overflow-hidden rounded-full bg-surface-2"
          role="progressbar"
          aria-label={`${name} load`}
          aria-valuenow={loadPct}
          aria-valuemin={0}
          aria-valuemax={100}
        >
          <div
            className="h-full rounded-full transition-[width]"
            style={{ width: `${loadPct}%`, backgroundColor: colors.text }}
          />
        </div>
      </div>

      {/* Severity is never color-only: expose the mapped label to AT. */}
      <span className="sr-only">{`State: ${stateLabel} (${SEVERITY_LABELS[severity]})`}</span>
    </article>
  );
}
