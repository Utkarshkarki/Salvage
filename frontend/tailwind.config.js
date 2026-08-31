/**
 * Reclaim design tokens — the single source of truth for the visual language.
 * These map 1:1 onto the color semantics of the legacy Jinja2 dashboard so the
 * React SPA reads the same at a glance:
 *
 *   diagnose = blue          decide = purple        act = teal
 *   override = amber         recovered = green      escalated = red
 *   stop = slate             LLM-failure-fallback = orange
 *
 * Every component references these tokens (e.g. `bg-diagnose`), never raw hex.
 * If a color must change it changes here, in one place.
 */
var config = {
    content: ["./index.html", "./src/**/*.{ts,tsx}"],
    theme: {
        extend: {
            colors: {
                diagnose: {
                    DEFAULT: "#2563eb",
                    soft: "#eff6ff",
                    border: "#93c5fd",
                },
                decide: {
                    DEFAULT: "#7c3aed",
                    soft: "#f5f3ff",
                    border: "#c4b5fd",
                },
                act: {
                    DEFAULT: "#0d9488",
                    soft: "#f0fdfa",
                    border: "#5eead4",
                },
                override: {
                    DEFAULT: "#b45309",
                    soft: "#fffbeb",
                    border: "#fcd34d",
                },
                recovered: {
                    DEFAULT: "#166534",
                    soft: "#f0fdf4",
                    border: "#86efac",
                },
                escalated: {
                    DEFAULT: "#991b1b",
                    soft: "#fef2f2",
                    border: "#fca5a5",
                },
                stop: {
                    DEFAULT: "#334155",
                    soft: "#f8fafc",
                    border: "#cbd5e1",
                },
                fallback: {
                    DEFAULT: "#9a3412",
                    soft: "#fff7ed",
                    border: "#fed7aa",
                },
                ink: {
                    DEFAULT: "#0f172a",
                    muted: "#475569",
                    faint: "#64748b",
                },
                surface: "#ffffff",
                canvas: "#f1f5f9",
                line: "#e2e8f0",
            },
            boxShadow: {
                card: "0 1px 2px rgba(15,23,42,.06)",
            },
        },
    },
    plugins: [],
};
export default config;
