import type { Config } from "tailwindcss";

// DRONA AI design-system token wiring.
// Mirrors design.md "Design System (Visual Identity)" → "Tailwind Token Wiring".
// The dark "mission control" dashboard theme is activated via data-theme="dashboard"
// (see darkMode variant below) and surfaced through CSS variables in src/index.css.
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  darkMode: ["variant", '&:is([data-theme="dashboard"] *)'],
  theme: {
    extend: {
      colors: {
        // Brand palette
        navy: { 900: "#11203c", 800: "#1b2a4a", 600: "#2f4576", 400: "#5a6ba0" },
        crimson: { 600: "#b3243b", 400: "#d65066" },
        gold: { 500: "#c9a227" },
        // Semantic / severity colors (four-level scale)
        info: "#1b6ec2",
        success: "#1a7f4b",
        warning: "#9a6700",
        danger: "#c0362c",
        // Semantic background tokens (light mode)
        "bg-info": "#e8f1fb",
        "bg-success": "#e6f5ec",
        "bg-warning": "#fdf4e0",
        "bg-danger": "#fce9e7",
        // Semantic border tokens (light mode)
        "border-info": "#9cc4ec",
        "border-warning": "#e9c97a",
        "border-danger": "#eaa9a2",
        // Dark dashboard surfaces (also exposed as CSS vars for runtime theming)
        surface: {
          0: "var(--surface-0)",
          1: "var(--surface-1)",
          2: "var(--surface-2)",
        },
        "on-surface": "var(--on-surface)",
        "on-surface-muted": "var(--on-surface-muted)",
        hairline: "var(--hairline)",
      },
      borderRadius: { md: "8px", lg: "12px" },
      boxShadow: {
        sm: "0 1px 2px rgba(17,32,60,.06)",
        md: "0 4px 16px rgba(17,32,60,.10)",
      },
      fontFamily: {
        sans: ["Inter", "system-ui", "sans-serif"],
        mono: ["JetBrains Mono", "monospace"],
      },
      backgroundImage: {
        "brand-gradient":
          "linear-gradient(135deg,#1b2a4a 0%,#2f4576 60%,#b3243b 140%)",
      },
    },
  },
  plugins: [],
} satisfies Config;
