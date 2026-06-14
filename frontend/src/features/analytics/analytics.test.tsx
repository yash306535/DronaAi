// Unit tests for the analytics views (task 22.1).
//
// Cover the two required behaviors from a sample analytics payload (the backend
// `ExamAnalyticsRead` shape produced by the Analyst):
//   - the difficulty heatmap renders one accuracy cell per topic on the shared
//     crimson(low)→amber→green(high) color scale (Requirements 10.3, 16.2)
//   - the score distribution renders the fixed score bands in low→high order
//     from the summary (Requirement 10.2)
//
// The color-scale assertions go through the same `scoreToScaleColor` helper the
// shared HeatmapCell uses, so a regression in the cell fill is caught here.

import { render, screen, within } from "@testing-library/react";
import { beforeAll, describe, expect, it } from "vitest";

// Recharts' ResponsiveContainer observes its box via ResizeObserver, which
// jsdom does not implement. Provide a no-op polyfill so the chart wrapper can
// mount in tests (it has no layout box under jsdom regardless).
beforeAll(() => {
  if (!("ResizeObserver" in globalThis)) {
    (globalThis as unknown as { ResizeObserver: unknown }).ResizeObserver =
      class {
        observe() {}
        unobserve() {}
        disconnect() {}
      };
  }
});
import { scoreToScaleColor } from "@/components";
import { DifficultyHeatmap } from "@/features/analytics/DifficultyHeatmap";
import { ScoreDistributionChart } from "@/features/analytics/ScoreDistributionChart";
import {
  toDistributionBars,
  toHeatmapRows,
} from "@/features/analytics/analyticsView";
import type { AnalyticsView } from "@/features/analytics/types";

// A representative analytics payload mirroring the Analyst's emitted JSON.
const SAMPLE: AnalyticsView = {
  id: "analytics-1",
  exam_id: "exam-1",
  summary: {
    // Intentionally out of numeric order to prove the view re-sorts low→high.
    distribution: {
      "90-100": 3,
      "0-10": 1,
      "50-60": 4,
      "40-50": 2,
    },
    mean: 62.5,
    anomalyCount: 5,
    completedStudents: 10,
    status: "ready",
  },
  difficulty_heatmap: {
    topics: {
      Algebra: { accuracy: 90, difficulty: 30 },
      Geometry: { accuracy: 20, difficulty: 80 },
    },
    status: "ready",
  },
  per_student: {
    students: {
      "stu-1": {
        score: 88,
        topicAccuracy: { Algebra: 90, Geometry: 60 },
        suggestions: ["Review geometry proofs"],
        suggestionsStatus: "ready",
      },
    },
    status: "ready",
  },
  generated_at: "2026-06-13T10:00:00.000Z",
};

describe("analytics difficulty heatmap", () => {
  it("renders one accuracy cell per topic on the shared color scale (Req 10.3, 16.2)", () => {
    render(<DifficultyHeatmap heatmap={SAMPLE.difficulty_heatmap} />);

    const table = screen.getByTestId("difficulty-heatmap");

    // High-accuracy topic: cell fill matches the shared scale's green-ward color.
    const algebra = within(table).getByText("Algebra").closest("tr");
    expect(algebra).not.toBeNull();
    const algebraCell = within(algebra as HTMLElement).getByText("90%");
    expect(algebraCell).toHaveStyle({
      backgroundColor: scoreToScaleColor(0.9),
    });
    expect(algebraCell).toHaveAttribute("data-accuracy", "90");

    // Low-accuracy topic: cell fill matches the shared scale's crimson-ward color.
    const geometry = within(table).getByText("Geometry").closest("tr");
    const geometryCell = within(geometry as HTMLElement).getByText("20%");
    expect(geometryCell).toHaveStyle({
      backgroundColor: scoreToScaleColor(0.2),
    });
    expect(geometryCell).toHaveAttribute("data-accuracy", "20");

    // Distinct accuracy → distinct color (the scale is not flat).
    expect(scoreToScaleColor(0.9)).not.toEqual(scoreToScaleColor(0.2));
  });

  it("orders heatmap rows by topic name", () => {
    const rows = toHeatmapRows(SAMPLE.difficulty_heatmap);
    expect(rows.map((r) => r.topic)).toEqual(["Algebra", "Geometry"]);
    expect(rows[0]).toMatchObject({ accuracy: 90, difficulty: 30 });
  });
});

describe("analytics score distribution", () => {
  it("derives the fixed score bands in low→high order from the summary (Req 10.2)", () => {
    const bars = toDistributionBars(SAMPLE.summary);
    expect(bars.map((b) => b.band)).toEqual([
      "0-10",
      "40-50",
      "50-60",
      "90-100",
    ]);
    expect(bars.map((b) => b.count)).toEqual([1, 2, 4, 3]);
  });

  it("renders the distribution chart wrapper with the resolved band count", () => {
    // Pass explicit bars so the render is deterministic under jsdom (Recharts'
    // ResponsiveContainer has no layout box in jsdom).
    const bars = toDistributionBars(SAMPLE.summary);
    render(<ScoreDistributionChart summary={undefined} bars={bars} />);

    const chart = screen.getByTestId("score-distribution");
    expect(chart).toHaveAttribute("data-bands", String(bars.length));
  });

  it("shows an empty-state message when there is no distribution", () => {
    render(<ScoreDistributionChart summary={undefined} />);
    expect(
      screen.getByText(/no score distribution available/i),
    ).toBeInTheDocument();
  });
});
