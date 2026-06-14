// Shared color-scale helpers for score-driven visuals (SessionTile integrity
// ring and Heatmap cell). Both map a normalized value onto the same
// crimson(low) → amber(mid) → green(high) gradient drawn from the brand /
// semantic palette, so "low" always reads as danger and "high" as success.

import { brand, semantic } from "@/theme";

/** Endpoint colors of the low→high score scale (crimson → amber → green). */
const SCALE = {
  low: brand.crimson[600], // #b3243b
  mid: semantic.text.warning, // #9a6700 (amber/gold-ish warning)
  high: semantic.text.success, // #1a7f4b
} as const;

function clamp01(value: number): number {
  if (Number.isNaN(value)) return 0;
  if (value < 0) return 0;
  if (value > 1) return 1;
  return value;
}

function hexToRgb(hex: string): [number, number, number] {
  const h = hex.replace("#", "");
  return [
    parseInt(h.slice(0, 2), 16),
    parseInt(h.slice(2, 4), 16),
    parseInt(h.slice(4, 6), 16),
  ];
}

function rgbToHex(r: number, g: number, b: number): string {
  const to2 = (n: number) =>
    Math.round(clampByte(n)).toString(16).padStart(2, "0");
  return `#${to2(r)}${to2(g)}${to2(b)}`;
}

function clampByte(n: number): number {
  if (n < 0) return 0;
  if (n > 255) return 255;
  return n;
}

function lerp(a: number, b: number, t: number): number {
  return a + (b - a) * t;
}

function mix(fromHex: string, toHex: string, t: number): string {
  const [r1, g1, b1] = hexToRgb(fromHex);
  const [r2, g2, b2] = hexToRgb(toHex);
  return rgbToHex(lerp(r1, r2, t), lerp(g1, g2, t), lerp(b1, b2, t));
}

/**
 * Map a normalized score in [0,1] to a color on the
 * crimson(low) → amber(mid) → green(high) scale.
 *
 * - 0.0 → crimson (lowest accuracy / integrity)
 * - 0.5 → amber
 * - 1.0 → green (highest accuracy / integrity)
 *
 * Inputs outside [0,1] are clamped to the nearest bound.
 */
export function scoreToScaleColor(score: number): string {
  const t = clamp01(score);
  if (t <= 0.5) {
    return mix(SCALE.low, SCALE.mid, t / 0.5);
  }
  return mix(SCALE.mid, SCALE.high, (t - 0.5) / 0.5);
}
