import { useState } from "react";
import { ApiError } from "../api/client";
import { useManualAction } from "../hooks/useApi";

/**
 * Operator control plane for an ESCALATED case (B5.2). Two deliberate HUMAN
 * decisions that call A6 — "Approve manual retry" and "Mark resolved by human".
 *
 * Behavior:
 *  - Rendered only when the case is ESCALATED (the caller gates this).
 *  - Buttons are disabled while a request is pending (clear pending state).
 *  - A 409 is surfaced distinctly: the case left ESCALATED (e.g. someone else
 *    already resolved it), so the surrounding detail is re-queried.
 *  - Optimistic UI is deliberately NOT used here: the outcome of an override is
 *    a terminal money-path transition, so we show a pending state and let React
 *    Query refetch the authoritative detail on success rather than guessing.
 */
export function ManualActions({ caseId }: { caseId: string }) {
  const { approveRetry, resolveHuman } = useManualAction(caseId);
  const [actionError, setActionError] = useState<string | null>(null);

  const pending = approveRetry.isPending || resolveHuman.isPending;

  const run = async (mut: ReturnType<typeof useManualAction>["approveRetry"], label: string) => {
    setActionError(null);
    try {
      await mut.mutateAsync();
    } catch (err) {
      if (err instanceof ApiError && err.status === 409) {
        setActionError(
          "This case is no longer being escalated — it was already resolved, " +
            "so this action could not be applied.",
        );
      } else {
        setActionError(
          err instanceof Error ? `${label} failed: ${err.message}` : `${label} failed.`,
        );
      }
    }
  };

  return (
    <div className="rounded-xl border border-override-border bg-override-soft p-4">
      <h2 className="mb-1 text-sm font-bold uppercase tracking-wide text-override">
        Operator actions
      </h2>
      <p className="mb-3 text-sm text-ink-muted">
        These are <strong>human decisions</strong>, not the agent’s — they are written to the audit
        trail as <code className="rounded bg-surface px-1">manual_override</code>.
      </p>
      <div className="flex flex-wrap gap-3">
        <button
          type="button"
          disabled={pending}
          onClick={() => void run(approveRetry, "Approve manual retry")}
          className="rounded-lg bg-override px-4 py-2 text-sm font-semibold text-white transition hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-50 focus:outline-none focus-visible:ring-2 focus-visible:ring-override focus-visible:ring-offset-2"
        >
          {approveRetry.isPending ? "Approving retry…" : "Approve manual retry"}
        </button>
        <button
          type="button"
          disabled={pending}
          onClick={() => void run(resolveHuman, "Mark resolved by human")}
          className="rounded-lg bg-recovered px-4 py-2 text-sm font-semibold text-white transition hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-50 focus:outline-none focus-visible:ring-2 focus-visible:ring-recovered focus-visible:ring-offset-2"
        >
          {resolveHuman.isPending ? "Resolving…" : "Mark resolved by human"}
        </button>
      </div>
      {actionError && (
        <p role="alert" className="mt-3 text-sm font-medium text-escalated">
          {actionError}
        </p>
      )}
    </div>
  );
}
