import { cn } from "@/components/classNames";
import { scoreToScaleColor } from "@/components/colorScale";
import { StatPill } from "@/components/StatPill";
import type { Severity } from "@/theme";
import type { SessionStatus } from "@/types";

export interface SessionTileProps {
  /** Display label inside the ring; falls back to derived initials. */
  initials?: string;
  /** Optional thumbnail image URL; takes precedence over initials. */
  thumbnailUrl?: string;
  /** Student / session display name (used for initials + alt text). */
  name?: string;
  /**
   * Integrity score on a 0–100 scale (backend ExamSession.integrity_score).
   * Drives the ring color: low → crimson, high → green.
   */
  integrityScore: number;
  /** Session lifecycle status, shown as a status pill. */
  status: SessionStatus;
  className?: string;
}

/** Map a session status to a severity for its status pill. */
const STATUS_SEVERITY: Record<SessionStatus, Severity> = {
  not_started: "info",
  active: "success",
  submitted: "info",
  terminated: "danger",
};

const STATUS_LABEL: Record<SessionStatus, string> = {
  not_started: "Not started",
  active: "Active",
  submitted: "Submitted",
  terminated: "Terminated",
};

function clamp100(value: number): number {
  if (Number.isNaN(value)) return 0;
  if (value < 0) return 0;
  if (value > 100) return 100;
  return value;
}

function deriveInitials(name?: string, initials?: string): string {
  if (initials) return initials;
  if (!name) return "?";
  const parts = name.trim().split(/\s+/).slice(0, 2);
  return parts.map((p) => p.charAt(0).toUpperCase()).join("") || "?";
}

/**
 * Session grid tile: initials/thumbnail inside an integrity-score ring whose
 * color shifts green→crimson by score (Requirement 16.2 via the shared scale),
 * plus a status pill. The numeric score is rendered as text so the ring's
 * meaning is not conveyed by color alone.
 */
export function SessionTile({
  initials,
  thumbnailUrl,
  name,
  integrityScore,
  status,
  className,
}: SessionTileProps) {
  const score = clamp100(integrityScore);
  const ringColor = scoreToScaleColor(score / 100);
  const label = deriveInitials(name, initials);

  return (
    <article
      className={cn(
        "flex flex-col items-center gap-2 rounded-md bg-surface-1 p-3 text-on-surface shadow-sm",
        className,
      )}
      data-status={status}
    >
      <div
        className="flex h-16 w-16 items-center justify-center rounded-full p-1"
        style={{
          background: `conic-gradient(${ringColor} ${score * 3.6}deg, var(--surface-2) 0deg)`,
        }}
        role="img"
        aria-label={`Integrity score ${score} of 100`}
      >
        <div className="flex h-full w-full items-center justify-center overflow-hidden rounded-full bg-surface-2 text-sm font-semibold">
          {thumbnailUrl ? (
            <img
              src={thumbnailUrl}
              alt={name ? `${name} thumbnail` : "Session thumbnail"}
              className="h-full w-full object-cover"
            />
          ) : (
            <span>{label}</span>
          )}
        </div>
      </div>

      <span className="text-xs font-medium" style={{ color: ringColor }}>
        {score}
      </span>

      <StatPill severity={STATUS_SEVERITY[status]}>
        {STATUS_LABEL[status]}
      </StatPill>
    </article>
  );
}
