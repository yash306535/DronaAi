// Design tokens for DRONA AI (typed mirror of the CSS variables in index.css
// and the Tailwind config). Mirrors design.md "Design System (Visual Identity)".
//
// These constants let TS/React code reference brand values without hard-coding
// hexes. The canonical source for runtime theming remains the CSS variables;
// these are the build-time counterparts kept in sync with them.

/** Navy/crimson/gold brand palette. */
export const brand = {
  navy: { 900: "#11203c", 800: "#1b2a4a", 600: "#2f4576", 400: "#5a6ba0" },
  crimson: { 600: "#b3243b", 400: "#d65066" },
  gold: { 500: "#c9a227" },
} as const;

/** The brand gradient reused from the battle-plan hero. */
export const brandGradient =
  "linear-gradient(135deg,#1b2a4a 0%,#2f4576 60%,#b3243b 140%)";

/** Semantic color tokens (light mode), mirroring the battle-plan :root. */
export const semantic = {
  text: {
    primary: "#1a1d24",
    secondary: "#5a6270",
    tertiary: "#8a93a2",
    info: "#1b6ec2",
    success: "#1a7f4b",
    warning: "#9a6700",
    danger: "#c0362c",
  },
  background: {
    primary: "#ffffff",
    secondary: "#f4f6f9",
    info: "#e8f1fb",
    success: "#e6f5ec",
    warning: "#fdf4e0",
    danger: "#fce9e7",
  },
  border: {
    secondary: "#cfd6e0",
    tertiary: "#e3e8ee",
    info: "#9cc4ec",
    warning: "#e9c97a",
    danger: "#eaa9a2",
  },
} as const;

/** Dark "mission control" dashboard surface tokens (data-theme="dashboard"). */
export const dashboardSurfaces = {
  surface0: "#11203c",
  surface1: "#182a4a",
  surface2: "#21365c",
  onSurface: "#e8edf6",
  onSurfaceMuted: "#9fb0cc",
  hairline: "rgba(255,255,255,.08)",
} as const;

/** Radii and elevation tokens. */
export const radius = { md: "8px", lg: "12px" } as const;
export const shadow = {
  sm: "0 1px 2px rgba(17,32,60,.06)",
  md: "0 4px 16px rgba(17,32,60,.10)",
} as const;

/** Typography tokens. */
export const typography = {
  fontSans:
    '"Inter", -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif',
  fontMono: '"JetBrains Mono", "SFMono-Regular", Menlo, monospace',
} as const;
