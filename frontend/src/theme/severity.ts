// Severity → color mapping for DRONA AI.
//
// A single, consistent mapping used across alerts, anomaly badges, agent states,
// and charts. Mirrors design.md "Design System (Visual Identity)" →
// "Severity → Color Mapping". Severity is NEVER conveyed by color alone
// (see Requirement 16.3) — components must pair these colors with an icon and a
// visible text label; this module only owns the color half of that contract.

/** The four-level severity scale used everywhere in the product. */
export type Severity = "info" | "success" | "warning" | "danger";

/** Ordered list of the four severity levels, low → high signal. */
export const SEVERITY_LEVELS: readonly Severity[] = [
  "info",
  "success",
  "warning",
  "danger",
] as const;

/** The color triplet applied to a given severity across every surface. */
export interface SeverityColors {
  /** Foreground / text color. */
  readonly text: string;
  /** Background fill color. */
  readonly background: string;
  /**
   * Border color. `success` has no dedicated border token in the design system,
   * so it falls back to its text color for a consistent, accessible outline.
   */
  readonly border: string;
}

/**
 * Severity → color mapping. Values are the design-system hexes (see design.md).
 * `success` has no dedicated border token, so it reuses its text color.
 */
export const SEVERITY_COLORS: Readonly<Record<Severity, SeverityColors>> = {
  info: { text: "#1b6ec2", background: "#e8f1fb", border: "#9cc4ec" },
  success: { text: "#1a7f4b", background: "#e6f5ec", border: "#1a7f4b" },
  warning: { text: "#9a6700", background: "#fdf4e0", border: "#e9c97a" },
  danger: { text: "#c0362c", background: "#fce9e7", border: "#eaa9a2" },
} as const;

/** Human-readable label for a severity level, for the text half of 16.3. */
export const SEVERITY_LABELS: Readonly<Record<Severity, string>> = {
  info: "Info",
  success: "Success",
  warning: "Warning",
  danger: "Danger",
} as const;

/** Returns the color triplet for a severity level. */
export function getSeverityColors(severity: Severity): SeverityColors {
  return SEVERITY_COLORS[severity];
}
