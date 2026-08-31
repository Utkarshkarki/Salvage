import { useMutation } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";
import { runSimulation } from "../api/endpoints";
import type { SimulatorComparison, SimulatorOverrides } from "../types";
import { ErrorAlert, errorMessage, formatINR } from "../components/Feedback";

// The editable threshold subset, in display order (mirrors _SIM_THRESHOLD_FIELDS).
const FIELDS: Array<{ key: keyof SimulatorOverrides; label: string; step: string }> = [
  { key: "escalation_amount_threshold", label: "Escalation amount threshold (₹)", step: "100" },
  { key: "escalation_days_threshold", label: "Escalation days threshold", step: "1" },
  { key: "max_retries_per_cycle", label: "Max retries per cycle", step: "1" },
  { key: "cooldown_hours", label: "Cooldown hours", step: "1" },
  { key: "email_cap_per_7d", label: "Email cap per 7 days", step: "1" },
];

/**
 * A compact horizontal bar comparing a metric before/after the simulated
 * thresholds. Pure CSS (no charting library) — appropriate for a single
 * comparison view (B5.3).
 */
function MetricBar({
  label,
  base,
  sim,
  fmt,
}: {
  label: string;
  base: number;
  sim: number;
  fmt: (n: number) => string;
}) {
  const max = Math.max(Math.abs(base), Math.abs(sim), 1);
  const basePct = (Math.abs(base) / max) * 100;
  const simPct = (Math.abs(sim) / max) * 100;
  const changed = base !== sim;
  return (
    <li className="space-y-1 py-3">
      <div className="flex items-baseline justify-between gap-2 text-sm">
        <span className="font-medium text-ink">{label}</span>
        <span className="tabular-nums text-ink-muted">
          {fmt(base)} <span aria-hidden="true">→</span>{" "}
          <strong className={changed ? "text-recovered" : "text-ink"}>{fmt(sim)}</strong>
        </span>
      </div>
      <div className="flex h-4 w-full gap-4 overflow-hidden">
        <div
          className={`h-4 min-w-[2px] overflow-hidden rounded bg-diagnose/70 ${changed ? "" : "opacity-50"}`}
          style={{ width: `${basePct}%` }}
          title={`Current: ${fmt(base)}`}
        />
        <div
          className={`h-4 min-w-[2px] overflow-hidden rounded bg-act ${changed ? "" : "opacity-50"}`}
          style={{ width: `${simPct}%` }}
          title={`Simulated: ${fmt(sim)}`}
        />
      </div>
    </li>
  );
}

function Comparison({ data }: { data: SimulatorComparison }) {
  const rows: Array<{ label: string; base: number; sim: number; fmt: (n: number) => string }> = [
    {
      label: "Recovery rate",
      base: data.baseline.recovery_rate,
      sim: data.simulated.recovery_rate,
      fmt: (n) => `${(n * 100).toFixed(1)}%`,
    },
    {
      label: "Amount recovered",
      base: data.baseline.recovered_amount,
      sim: data.simulated.recovered_amount,
      fmt: formatINR,
    },
    {
      label: "Escalated (human)",
      base: data.baseline.escalated_cases,
      sim: data.simulated.escalated_cases,
      fmt: String,
    },
    {
      label: "Stopped (deliberate halt)",
      base: data.baseline.stopped_cases,
      sim: data.simulated.stopped_cases,
      fmt: String,
    },
    {
      label: "LLM call failures",
      base: data.baseline.llm_call_failures,
      sim: data.simulated.llm_call_failures,
      fmt: String,
    },
    {
      label: "Stopping-rule overrides",
      base: data.baseline.stopping_rule_overrides,
      sim: data.simulated.stopping_rule_overrides,
      fmt: String,
    },
    {
      label: "Stub-mode actions",
      base: data.baseline.stub_mode_actions,
      sim: data.simulated.stub_mode_actions,
      fmt: String,
    },
  ];
  return (
    <div className="mt-6 rounded-xl border border-line bg-surface p-4 shadow-card">
      <h2 className="mb-1 text-lg font-semibold">Before / After comparison</h2>
      <p className="mb-2 text-sm text-ink-muted">
        Both columns re-run the same seed-42 synthetic batch: left is current thresholds, right is
        your simulated thresholds. Real settings are never mutated.
      </p>
      <ul className="divide-y divide-line">
        {rows.map((r) => (
          <MetricBar key={r.label} {...r} />
        ))}
      </ul>
    </div>
  );
}

export default function Simulator() {
  const [values, setValues] = useState<Record<string, string>>({});
  const mutation = useMutation({
    mutationFn: (overrides: SimulatorOverrides) => runSimulation(overrides),
  });

  const handleChange = (key: string, raw: string) => {
    setValues((v) => ({ ...v, [key]: raw }));
  };

  const handleSubmit = (e: FormEvent) => {
    e.preventDefault();
    const overrides: SimulatorOverrides = {};
    for (const f of FIELDS) {
      const raw = (values[f.key] ?? "").trim();
      if (raw === "") continue;
      const num = Number(raw);
      if (Number.isNaN(num)) continue;
      if (f.key === "escalation_amount_threshold" || f.key === "cooldown_hours") {
        (overrides as Record<string, number>)[f.key] = num;
      } else {
        (overrides as Record<string, number>)[f.key] = Math.round(num);
      }
    }
    mutation.mutate(overrides);
  };

  return (
    <section aria-labelledby="sim-heading">
      <h1 id="sim-heading" className="mb-1 text-2xl font-bold">
        Rule Sensitivity Simulator
      </h1>
      <p className="mb-6 max-w-2xl text-sm text-ink-muted">
        Tune the stopping-rule thresholds and re-run the same seed-42 synthetic batch to see the
        before/after impact. Each run uses a throwaway database; production settings are never
        changed.
      </p>

      <form
        onSubmit={handleSubmit}
        className="max-w-md rounded-xl border border-line bg-surface p-4 shadow-card"
        aria-label="Simulator thresholds"
      >
        <h2 className="mb-3 text-sm font-bold uppercase tracking-wide text-ink-faint">
          Stopping-rule thresholds
        </h2>
        <div className="space-y-3">
          {FIELDS.map((f) => (
            <label key={f.key} className="block">
              <span className="mb-1 block text-sm text-ink-muted">{f.label}</span>
              <input
                type="number"
                inputMode="decimal"
                step={f.step}
                value={values[f.key] ?? ""}
                placeholder="Use current"
                onChange={(e) => handleChange(f.key, e.target.value)}
                className="w-full rounded-lg border border-line bg-surface px-3 py-2 text-sm focus:border-diagnose focus:outline-none focus-visible:ring-2 focus-visible:ring-diagnose"
              />
            </label>
          ))}
        </div>
        <button
          type="submit"
          disabled={mutation.isPending}
          className="mt-4 w-full rounded-lg bg-diagnose px-4 py-2 text-sm font-semibold text-white transition hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-50 focus:outline-none focus-visible:ring-2 focus-visible:ring-diagnose focus-visible:ring-offset-2"
        >
          {mutation.isPending ? "Running simulation…" : "Run simulation"}
        </button>
        {mutation.isError && (
          <div className="mt-3">
            <ErrorAlert message={errorMessage(mutation.error)} />
          </div>
        )}
      </form>

      {mutation.isSuccess && mutation.data && <Comparison data={mutation.data} />}
    </section>
  );
}
