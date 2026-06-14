// Unit tests for the live admin dashboard event rendering (task 17.3).
//
// These render the DashboardView under jsdom with a deterministic `state`
// snapshot (the live socket is bypassed via the `state` prop) so no real
// server is needed. They cover the required behaviors:
//   - agent.message text is truncated at 2,000 characters (Req 12.3)
//   - alert items render their severity color AND a visible text label (Req 12.5)
//   - agent status cards render the mapped live state (Req 12.4)

import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { DashboardView } from "./DashboardView";
import { MAX_MESSAGE_TEXT_LENGTH } from "./dashboardView";
import type {
  DashboardSocketOptions,
  DashboardState,
} from "./useDashboardSocket";
import { SEVERITY_COLORS, SEVERITY_LABELS } from "@/theme";

// Minimal fake WebSocket so the always-mounted live socket stays network-free.
// The DashboardView renders from the injected `state` prop, but useDashboardSocket
// still mounts; this fake + a no-op snapshot keep it inert under jsdom.
class FakeWebSocket {
  readyState = 0;
  constructor(public url: string) {}
  addEventListener(): void {}
  removeEventListener(): void {}
  send(): void {}
  close(): void {}
}

const inertSocketOptions: DashboardSocketOptions = {
  webSocketImpl: FakeWebSocket as unknown as typeof WebSocket,
  baseUrl: "ws://test.local",
  snapshot: async () => ({}),
};

function baseState(overrides: Partial<DashboardState> = {}): DashboardState {
  return {
    agents: {},
    messages: [],
    alerts: [],
    sessions: {},
    connected: true,
    degraded: false,
    ...overrides,
  };
}

describe("DashboardView event rendering", () => {
  it("truncates agent.message text longer than 2,000 characters (Req 12.3)", () => {
    const longText = "x".repeat(5_000);
    const state = baseState({
      messages: [
        {
          id: "m1",
          source: "Guardian",
          ts: "2026-06-13T10:00:00.000Z",
          to: "Herald",
          text: longText,
        },
      ],
    });

    render(<DashboardView token="t" state={state} socketOptions={inertSocketOptions} />);

    const log = screen.getByLabelText("Inter-agent communication log");
    const rendered = log.textContent ?? "";

    // The full 5,000-char string must not be present verbatim.
    expect(rendered.includes(longText)).toBe(false);
    // The longest unbroken run of the message char must respect the cap.
    const longestRun = (rendered.match(/x+/g) ?? []).reduce(
      (max, run) => Math.max(max, run.length),
      0,
    );
    expect(longestRun).toBeLessThanOrEqual(MAX_MESSAGE_TEXT_LENGTH);
    // Source → target are both shown.
    expect(within(log).getByText("Guardian")).toBeInTheDocument();
    expect(within(log).getByText("Herald")).toBeInTheDocument();
  });

  it("does not truncate agent.message text at or below the cap (Req 12.3)", () => {
    const text = "Face absent confirmed for 8s in Session #4";
    const state = baseState({
      messages: [
        {
          id: "m1",
          source: "Guardian",
          ts: "2026-06-13T10:00:00.000Z",
          to: "Herald",
          text,
        },
      ],
    });

    render(<DashboardView token="t" state={state} socketOptions={inertSocketOptions} />);

    const log = screen.getByLabelText("Inter-agent communication log");
    expect(log.textContent).toContain(text);
  });

  it("renders alert items with severity color AND a visible text label (Req 12.5)", () => {
    const state = baseState({
      alerts: [
        {
          id: "a1",
          source: "Herald",
          sessionId: "sess_4",
          ts: "2026-06-13T10:00:00.000Z",
          severity: "danger",
          message: "Possible impersonation in Session #4 (2 faces)",
          anomalyId: "x1",
          reasons: ["Vision confirmed 2 faces"],
        },
        {
          id: "a2",
          source: "Sentinel",
          sessionId: "sess_2",
          ts: "2026-06-13T10:01:00.000Z",
          severity: "warning",
          message: "Repeated tab switching",
          anomalyId: "x2",
          reasons: ["6 tab switches in 2 minutes"],
        },
      ],
    });

    render(<DashboardView token="t" state={state} socketOptions={inertSocketOptions} />);

    const feed = screen.getByLabelText("Live alert feed");

    // Visible severity text labels (never color-only, Req 16.3 / 12.5).
    expect(within(feed).getByText(SEVERITY_LABELS.danger)).toBeInTheDocument();
    expect(within(feed).getByText(SEVERITY_LABELS.warning)).toBeInTheDocument();

    // Messages + reasons are shown.
    expect(
      within(feed).getByText("Possible impersonation in Session #4 (2 faces)"),
    ).toBeInTheDocument();
    expect(
      within(feed).getByText("• Vision confirmed 2 faces"),
    ).toBeInTheDocument();

    // The danger item carries its severity data attribute + color bar.
    const dangerItem = feed.querySelector('[data-severity="danger"]');
    expect(dangerItem).not.toBeNull();
    const colorBar = dangerItem?.querySelector("span[aria-hidden='true']");
    expect(colorBar).not.toBeNull();
    expect((colorBar as HTMLElement).style.backgroundColor).not.toBe("");
    // Sanity: the design-system danger color is a real triplet.
    expect(SEVERITY_COLORS.danger.text).toMatch(/^#/);
  });

  it("renders an agent status card per agent with its live state (Req 12.4)", () => {
    const state = baseState({
      agents: {
        Guardian: {
          source: "Guardian",
          state: "alerting",
          load: 0.8,
          ts: "2026-06-13T10:00:00.000Z",
        },
        Architect: {
          source: "Architect",
          state: "working",
          load: 0.4,
          ts: "2026-06-13T10:00:00.000Z",
        },
      },
    });

    render(<DashboardView token="t" state={state} socketOptions={inertSocketOptions} />);

    const strip = screen.getByLabelText("Agent status");

    // Default roster is rendered (Guardian + Architect among them).
    const guardian = within(strip)
      .getByText("Guardian")
      .closest("article") as HTMLElement;
    expect(guardian.getAttribute("data-agent-state")).toBe("alerting");
    expect(guardian.getAttribute("data-severity")).toBe("danger");

    const architect = within(strip)
      .getByText("Architect")
      .closest("article") as HTMLElement;
    expect(architect.getAttribute("data-agent-state")).toBe("working");
    expect(architect.getAttribute("data-severity")).toBe("info");

    // An agent with no live status falls back to idle.
    const sentinel = within(strip)
      .getByText("Sentinel")
      .closest("article") as HTMLElement;
    expect(sentinel.getAttribute("data-agent-state")).toBe("idle");
  });
});
