import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { AgentCard } from "@/components/AgentCard";
import { AlertFeed } from "@/components/AlertFeed";
import { AlertItem } from "@/components/AlertItem";
import { StatPill } from "@/components/StatPill";
import {
  getSeverityColors,
  SEVERITY_LABELS,
  SEVERITY_LEVELS,
  type Severity,
} from "@/theme";

/**
 * Task 15.5 — shared-component accessibility + severity-consistency tests.
 * Validates: Requirements 16.2, 16.3, 16.7
 */

const ALERT_SEVERITIES: Severity[] = ["info", "success", "warning", "danger"];

describe("severity → color consistency across components (Req 16.2)", () => {
  it("AlertItem applies the mapped severity text color to its left bar", () => {
    for (const severity of ALERT_SEVERITIES) {
      const { container, unmount } = render(
        <AlertItem severity={severity} title="Anomaly detected" />,
      );
      const bar = container.querySelector("[aria-hidden='true']");
      expect(bar).not.toBeNull();
      expect((bar as HTMLElement).style.backgroundColor).not.toBe("");
      unmount();
    }
  });

  it("StatPill and AlertItem use the SAME bg/text pair for a given severity", () => {
    for (const severity of ALERT_SEVERITIES) {
      const colors = getSeverityColors(severity);

      const pill = render(<StatPill severity={severity} />);
      const pillEl = pill.getByText(SEVERITY_LABELS[severity]);
      // background/text come from the shared mapping
      expect(pillEl.style.backgroundColor).toBe(toRgb(colors.background));
      expect(pillEl.style.color).toBe(toRgb(colors.text));
      pill.unmount();

      const alert = render(
        <AlertItem severity={severity} title="t" />,
      );
      const chip = alert.getByText(SEVERITY_LABELS[severity]);
      expect(chip.style.backgroundColor).toBe(toRgb(colors.background));
      expect(chip.style.color).toBe(toRgb(colors.text));
      alert.unmount();
    }
  });

  it("AgentCard maps each agent state to a consistent severity color", () => {
    const cases = [
      { state: "idle", severity: "success" },
      { state: "working", severity: "info" },
      { state: "alerting", severity: "danger" },
    ] as const;

    for (const { state, severity } of cases) {
      const { container, unmount } = render(
        <AgentCard name="Guardian" role="Proctoring" state={state} load={0.5} />,
      );
      const card = container.querySelector(`[data-agent-state='${state}']`);
      expect(card).not.toBeNull();
      expect(card?.getAttribute("data-severity")).toBe(severity);
      unmount();
    }
  });
});

describe("severity is never conveyed by color alone (Req 16.3)", () => {
  it("AlertItem renders a visible severity text label for every severity", () => {
    for (const severity of ALERT_SEVERITIES) {
      const { unmount } = render(
        <AlertItem severity={severity} title="Possible impersonation" />,
      );
      expect(screen.getByText(SEVERITY_LABELS[severity])).toBeInTheDocument();
      unmount();
    }
  });

  it("StatPill always carries a text label even with no children", () => {
    for (const severity of SEVERITY_LEVELS) {
      const { unmount } = render(<StatPill severity={severity} />);
      expect(screen.getByText(SEVERITY_LABELS[severity])).toBeInTheDocument();
      unmount();
    }
  });

  it("AlertItem renders the title and reasons text alongside the color bar", () => {
    render(
      <AlertItem
        severity="danger"
        title="Possible impersonation in Session #4"
        reasons={["Vision confirmed 2 faces", "Local multi-face for 6.1s"]}
      />,
    );
    expect(
      screen.getByText("Possible impersonation in Session #4"),
    ).toBeInTheDocument();
    expect(screen.getByText("• Vision confirmed 2 faces")).toBeInTheDocument();
  });
});

describe("alert feed live region (Req 16.7)", () => {
  it("exposes the feed as an aria-live=polite region", () => {
    render(
      <AlertFeed>
        <AlertItem severity="danger" title="New alert" />
      </AlertFeed>,
    );
    const region = screen.getByLabelText("Live alert feed");
    expect(region).toHaveAttribute("aria-live", "polite");
  });

  it("announces alerts inserted into the feed region", () => {
    render(
      <AlertFeed>
        <AlertItem severity="warning" title="Soft anomaly" />
      </AlertFeed>,
    );
    const region = screen.getByLabelText("Live alert feed");
    expect(within(region).getByText("Soft anomaly")).toBeInTheDocument();
  });
});

/** jsdom serializes inline color styles as `rgb(r, g, b)`; convert hex to match. */
function toRgb(hex: string): string {
  const h = hex.replace("#", "");
  const r = parseInt(h.slice(0, 2), 16);
  const g = parseInt(h.slice(2, 4), 16);
  const b = parseInt(h.slice(4, 6), 16);
  return `rgb(${r}, ${g}, ${b})`;
}
