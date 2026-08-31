import { Link, useParams } from "react-router-dom";
import { useCaseDetail } from "../hooks/useApi";
import { StateBadge } from "../components/StateBadge";
import { AuditTrail } from "../components/AuditTrail";
import { ManualActions } from "../components/ManualActions";
import { TableSkeleton } from "../components/Skeleton";
import { ErrorAlert, formatINR } from "../components/Feedback";

export default function CaseDetail() {
  const { caseId = "" } = useParams<{ caseId: string }>();
  const { data, isPending, isError, error, refetch } = useCaseDetail(caseId);

  if (isError) {
    return (
      <div>
        <Link to="/" className="text-sm text-diagnose hover:underline">
          ← Back to cases
        </Link>
        <ErrorAlert message={`Could not load case: ${error.message}`} onRetry={refetch} />
      </div>
    );
  }

  if (isPending || !data) {
    return (
      <div>
        <Link to="/" className="text-sm text-diagnose hover:underline">
          ← Back to cases
        </Link>
        <div className="mt-4 space-y-3">
          <TableSkeleton rows={3} columns={3} />
        </div>
      </div>
    );
  }

  const isEscalated = data.state === "ESCALATED";
  const hasFallback = data.fallback_any_stage;

  return (
    <article>
      <Link to="/" className="text-sm text-diagnose hover:underline">
        ← Back to cases
      </Link>

      <header className="mt-3 mb-5">
        <div className="flex flex-wrap items-center gap-3">
          <h1 className="text-2xl font-bold">{data.case_id}</h1>
          <StateBadge state={data.state} />
          <span className="rounded-full bg-act-soft px-2 py-0.5 text-sm font-bold text-act">
            {formatINR(data.amount)}
          </span>
        </div>
        <p className="mt-1 text-sm text-ink-muted">
          Customer <strong className="text-ink">{data.customer_id}</strong>
          {hasFallback && (
            <span className="ml-2 rounded-full border border-fallback-border bg-fallback-soft px-2 py-0.5 text-xs font-semibold text-fallback">
              Had an LLM-failure fallback
            </span>
          )}
        </p>
      </header>

      {/* B5.2 — the override control plane, only for ESCALATED cases. */}
      {isEscalated ? (
        <section className="mb-5" aria-label="Operator actions">
          <ManualActions caseId={data.case_id} />
        </section>
      ) : (
        <p
          className="mb-5 rounded-lg border border-line bg-surface px-4 py-3 text-sm text-ink-muted"
          aria-live="polite"
        >
          This case is in the <strong>{data.state}</strong> state — no manual override actions are
          available.
        </p>
      )}

      <section aria-labelledby="trail-heading">
        <h2 id="trail-heading" className="mb-3 text-lg font-semibold">
          Decision trail
        </h2>
        <AuditTrail trail={data.audit_trail} />
      </section>
    </article>
  );
}
