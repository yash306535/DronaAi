import { describe, expect, it } from "vitest";
import {
  getSeverityColors,
  SEVERITY_COLORS,
  SEVERITY_LABELS,
  SEVERITY_LEVELS,
  type Severity,
} from "./severity";

describe("severity color mapping", () => {
  it("exposes exactly the four-level severity scale", () => {
    expect(SEVERITY_LEVELS).toEqual(["info", "success", "warning", "danger"]);
  });

  it("maps each severity to the design-system color triplet (Requirements 16.2)", () => {
    expect(SEVERITY_COLORS.info).toEqual({
      text: "#1b6ec2",
      background: "#e8f1fb",
      border: "#9cc4ec",
    });
    expect(SEVERITY_COLORS.success).toEqual({
      text: "#1a7f4b",
      background: "#e6f5ec",
      border: "#1a7f4b",
    });
    expect(SEVERITY_COLORS.warning).toEqual({
      text: "#9a6700",
      background: "#fdf4e0",
      border: "#e9c97a",
    });
    expect(SEVERITY_COLORS.danger).toEqual({
      text: "#c0362c",
      background: "#fce9e7",
      border: "#eaa9a2",
    });
  });

  it("provides a text label for every severity (Requirements 16.3)", () => {
    for (const level of SEVERITY_LEVELS) {
      expect(SEVERITY_LABELS[level]).toBeTruthy();
    }
  });

  it("getSeverityColors returns the same triplet as the table for each level", () => {
    for (const level of SEVERITY_LEVELS) {
      expect(getSeverityColors(level)).toBe(SEVERITY_COLORS[level]);
    }
  });

  it("defines complete, non-empty color fields for every severity level", () => {
    const fields: (keyof (typeof SEVERITY_COLORS)[Severity])[] = [
      "text",
      "background",
      "border",
    ];
    for (const level of SEVERITY_LEVELS) {
      for (const field of fields) {
        expect(SEVERITY_COLORS[level][field]).toMatch(/^#[0-9a-f]{6}$/i);
      }
    }
  });
});
