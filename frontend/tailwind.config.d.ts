import type { Config } from "tailwindcss";
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
declare const config: Config;
export default config;
