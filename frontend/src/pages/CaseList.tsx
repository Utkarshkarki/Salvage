import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { useCases } from "../hooks/useApi";
import { StateBadge } from "../components/StateBadge";
import { TableSkeleton } from "../components/Skeleton";
import { EmptyState, ErrorAlert, formatINR } from "../components/Feedback";
import type { CaseState } from "../types";

// The seven states in display order, for the filter dropdown.
const STATE_OPTIONS: Array<{ value: CaseState | "ALL"; label: string }> = [
  { value: "ALL", label: "All states" },
  { value: "INGESTED", label: "Ingested" },
  { value: "DIAGNOSED", label: "Diagnosed" },
  { value: "DECIDED", label: "Decided" },
  { value: "ACTING", label: "Acting" },
  { value: "RESOLVED", label: "Resolved" },
  { value: "ESCALATED", label: "Escalated" },
  { value: "FAILED", label: "Failed" },
];

const PAGE_SIZE = 25;

export default function CaseList() {
  const [state, setState] = useState<CaseState | "ALL">("ALL");
  const [offset, setOffset] = useState(0);

  const params = useMemo(
    () => ({ state: state === "ALL" ? undefined : state, limit: PAGE_SIZE, offset }),
    [state, offset],
  );
  const { data, isPending, isError, error, refetch, isFetching } = useCases(params);

  const selectedState = state === "ALL" ? undefined : state;
  const handleStateChange = (value: string) => {
    setState(value as CaseState | "ALL");
    setOffset(0);
  };

  const totalCount = (data?.count ?? 0) + offset;
  const hasPrev = offset > 0;
  // We only know the page is the last when we get fewer than PAGE_SIZE rows.
  const hasNext = (data?.items.length ?? 0) >= PAGE_SIZE;
  const isLastPageKnownEmpty = data ? data.items.length === 0 && offset > 0 : false;

  return (
    <section aria-labelledby="cases-heading">
      <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
        <h1 id="cases-heading" className="text-2xl font-bold">
          Cases
        </h1>
        <div className="flex items-center gap-3">
          <label htmlFor="state-filter" className="sr-only">
            Filter by state
          </label>
          <select
            id="state-filter"
            value={state}
            onChange={(e) => handleStateChange(e.target.value)}
            className="rounded-lg border border-line bg-surface px-3 py-2 text-sm font-medium text-ink focus:border-diagnose focus:outline-none focus-visible:ring-2 focus-visible:ring-diagnose"
          >
            {STATE_OPTIONS.map((o) => (
              <option key={o.value} value={o.value}>
                {o.label}
              </option>
            ))}
          </select>
        </div>
      </div>

      {isError && (
        <ErrorAlert message={`Could not load cases: ${error.message}`} onRetry={refetch} />
      )}

      {isPending ? (
        <TableSkeleton rows={8} columns={5} />
      ) : !data || data.items.length === 0 ? (
        <EmptyState
          title={isLastPageKnownEmpty ? "No more cases on this page." : "No cases yet"}
          body={
            selectedState
              ? `No cases in the "${selectedState}" state. Try a different filter.`
              : "POST /webhook/razorpay or run `python -m reclaim.batch` to generate cases."
          }
        />
      ) : (
        <>
          <div className="overflow-x-auto rounded-xl border border-line bg-surface shadow-card">
            <table className="w-full min-w-[720px] border-collapse text-left text-sm">
              <caption className="sr-only">Recovery cases</caption>
              <thead className="border-b border-line">
                <tr className="text-xs uppercase tracking-wide text-ink-faint">
                  <th scope="col" className="px-4 py-3 font-semibold">
                    Case
                  </th>
                  <th scope="col" className="px-4 py-3 font-semibold">
                    State
                  </th>
                  <th scope="col" className="px-4 py-3 text-right font-semibold">
                    Amount
                  </th>
                  <th scope="col" className="px-4 py-3 font-semibold">
                    Customer
                  </th>
                  <th scope="col" className="px-4 py-3 font-semibold">
                    Failure
                  </th>
                  <th scope="col" className="px-4 py-3 text-right font-semibold">
                    Attempt
                  </th>
                  <th scope="col" className="px-4 py-3 font-semibold">
                    Created
                  </th>
                </tr>
              </thead>
              <tbody>
                {data.items.map((c) => (
                  <tr
                    key={c.case_id}
                    className={`border-b border-line last:border-0 hover:bg-canvas/60 ${
                      isFetching ? "opacity-60" : ""
                    }`}
                  >
                    <td className="px-4 py-3">
                      <Link
                        to={`/cases/${encodeURIComponent(c.case_id)}`}
                        className="font-semibold text-diagnose underline-offset-2 hover:underline focus:outline-none focus-visible:ring-2 focus-visible:ring-diagnose focus-visible:ring-offset-1"
                      >
                        {c.case_id}
                      </Link>
                    </td>
                    <td className="px-4 py-3">
                      <StateBadge state={c.state} />
                    </td>
                    <td className="px-4 py-3 text-right font-medium tabular-nums">
                      {formatINR(c.amount)}
                    </td>
                    <td className="px-4 py-3 text-ink-muted">{c.customer_id}</td>
                    <td className="px-4 py-3 font-mono text-xs text-ink-muted">
                      {c.failure_reason}
                    </td>
                    <td className="px-4 py-3 text-right tabular-nums">{c.attempt_number}</td>
                    <td className="px-4 py-3 text-ink-muted">
                      {c.created_at ? new Date(c.created_at).toLocaleDateString() : "—"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <div className="mt-4 flex flex-wrap items-center justify-between gap-3">
            <p className="text-sm text-ink-muted" aria-live="polite">
              Showing {offset + 1}–{offset + (data.items.length ?? 0)} of {totalCount}+
            </p>
            <div className="flex gap-2">
              <button
                type="button"
                onClick={() => setOffset((o) => Math.max(0, o - PAGE_SIZE))}
                disabled={!hasPrev}
                className="rounded-lg border border-line bg-surface px-4 py-2 text-sm font-medium transition hover:bg-canvas disabled:cursor-not-allowed disabled:opacity-40 focus:outline-none focus-visible:ring-2 focus-visible:ring-diagnose"
              >
                ← Previous
              </button>
              <button
                type="button"
                onClick={() => setOffset((o) => o + PAGE_SIZE)}
                disabled={!hasNext}
                className="rounded-lg border border-line bg-surface px-4 py-2 text-sm font-medium transition hover:bg-canvas disabled:cursor-not-allowed disabled:opacity-40 focus:outline-none focus-visible:ring-2 focus-visible:ring-diagnose"
              >
                Next →
              </button>
            </div>
          </div>
        </>
      )}
    </section>
  );
}
