import type { CaseState } from "../types";

/**
 * Color-coded state badge (B4 design tokens). Maps each terminal/interesting
 * state onto the theme tokens so the dashboard reads at a glance. RESOLVED,
 * ESCALATED, FAILED get their semantics; everything else is the neutral
 * "other" (blue) treatment like the legacy `st-other` class.
 */
const STATE_TONES: Record<string, string> = {
  RESOLVED: "recovered",
  ESCALATED: "escalated",
  FAILED: "escalated",
};

export function stateTone(state: CaseState): string {
  return STATE_TONES[state] ?? "diagnose";
}

const TONE_CLASSES: Record<string, string> = {
  recovered: "bg-recovered-soft text-recovered border-recovered-border",
  escalated: "bg-escalated-soft text-escalated border-escalated-border",
  diagnose: "bg-diagnose-soft text-diagnose border-diagnose-border",
};

export function StateBadge({ state }: { state: CaseState }) {
  return (
    <span
      className={`inline-flex items-center rounded-full border px-2.5 py-0.5 text-xs font-semibold ${TONE_CLASSES[stateTone(state)] ?? TONE_CLASSES.diagnose}`}
    >
      {state}
    </span>
  );
}
