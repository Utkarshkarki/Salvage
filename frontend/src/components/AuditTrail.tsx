import type { AuditLogEntry, LLMProvenance } from "../types";

/** Stage → design token name (B4: diagnose=blue, decide=purple, act=teal). */
const STAGE_TONES: Record<string, string> = {
  diagnose: "diagnose",
  decide: "decide",
  act: "act",
};

const STAGE_CLASSES: Record<string, string> = {
  diagnose: "bg-diagnose text-white",
  decide: "bg-decide text-white",
  act: "bg-act text-white",
};

// Decide an outcome chip's tone from its contents (mirrors _outcome_class).
function outcomeTone(outcome: string | null): string {
  const o = outcome ?? "";
  if (o.includes("OVERRIDE")) return "bg-override-soft text-override border-override-border";
  if (o === "STOPPED") return "bg-stop-soft text-stop border-stop-border";
  if (o.includes("retry_succeeded")) return "bg-recovered-soft text-recovered border-recovered-border";
  if (o.includes("ESCALATED")) return "bg-escalated-soft text-escalated border-escalated-border";
  return "bg-canvas text-ink-muted border-line";
}

function Provenance({ prov }: { prov: LLMProvenance }) {
  return (
    <div className="mt-2 rounded-md border border-line bg-canvas/60 px-3 py-2 text-[11px] leading-relaxed text-ink-muted">
      <span className="font-semibold uppercase tracking-wide text-ink-faint">
        LLM provenance:{" "}
      </span>
      {prov.model} · {prov.prompt_version} · mode={prov.mode} ·{" "}
      <span className="font-mono">hash {prov.prompt_hash.slice(0, 10)}…</span>
    </div>
  );
}

/**
 * One stage as its own visual block — never concatenated text. Each block shows
 * the colored stage badge, decision, outcome chip, reasoning, an override badge
 * when the outcome is an OVERRIDE, an LLM-failure-fallback tag when the model
 * call itself failed, and provenance when present.
 */
function StageBlock({ entry }: { entry: AuditLogEntry }) {
  const stageTone = STAGE_TONES[entry.stage] ?? "other";
  const prov = entry.input_state?.llm_provenance as LLMProvenance | undefined;
  const isOverride = (entry.outcome ?? "").includes("OVERRIDE");
  const isFallback = entry.fallback_triggered;

  return (
    <div
      className={`rounded-lg border-l-4 bg-surface p-4 shadow-card ${stageTone === "diagnose" ? "border-diagnose" : stageTone === "decide" ? "border-decide" : stageTone === "act" ? "border-act" : "border-line"}`}
    >
      <div className="flex flex-wrap items-center gap-2">
        <span
          className={`rounded px-2 py-0.5 text-[10px] font-bold uppercase tracking-wider ${
            STAGE_CLASSES[entry.stage] ?? "bg-ink-faint text-white"
          }`}
        >
          {entry.stage}
        </span>
        <span className="text-sm font-semibold text-ink">{entry.decision ?? "—"}</span>
        <span className={`rounded-full border px-2 py-0.5 text-xs font-semibold ${outcomeTone(entry.outcome)}`}>
          {entry.outcome ?? "—"}
        </span>
        {isOverride && (
          <span className="rounded-full border border-override-border bg-override-soft px-2 py-0.5 text-xs font-bold uppercase tracking-wide text-override">
            Override
          </span>
        )}
        {isFallback && (
          <span
            className="rounded-full border border-fallback-border bg-fallback-soft px-2 py-0.5 text-xs font-bold uppercase tracking-wide text-fallback"
            title="The LLM call itself failed; a deterministic fallback was used"
          >
            LLM failure fallback
          </span>
        )}
        {entry.timestamp && (
          <span className="ml-auto text-xs text-ink-faint">
            {new Date(entry.timestamp).toLocaleString()}
          </span>
        )}
      </div>
      {entry.agent_reasoning && (
        <p className="mt-2 text-sm text-ink-muted">{entry.agent_reasoning}</p>
      )}
      {prov && <Provenance prov={prov} />}
    </div>
  );
}

/** The full append-only decision trail, oldest first. */
export function AuditTrail({ trail }: { trail: AuditLogEntry[] }) {
  if (!trail.length) {
    return (
      <p className="my-4 text-sm italic text-ink-muted">No decision trail recorded.</p>
    );
  }
  return (
    <ol className="space-y-3" aria-label="Decision trail">
      {trail.map((e, i) => (
        <li key={`${e.stage}-${i}`}>
          <StageBlock entry={e} />
        </li>
      ))}
    </ol>
  );
}
