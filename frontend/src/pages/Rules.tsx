import { useRules } from "../hooks/useApi";
import { TableSkeleton } from "../components/Skeleton";
import { ErrorAlert, EmptyState } from "../components/Feedback";

/**
 * B5.4 — the live "what governs this system" page. Renders the R1–R7 stopping
 * rules (A4) in plain language with the live threshold values interpolated.
 * This is the introspectable policy artifact: the same data the legacy /rules
 * HTML page shows.
 */
export default function Rules() {
  const { data, isPending, isError, error, refetch } = useRules();

  const actionLabel = (action: string) => action.replace(/_/g, " ");

  return (
    <section aria-labelledby="rules-heading">
      <h1 id="rules-heading" className="mb-1 text-2xl font-bold">
        Active Stopping Rules
      </h1>
      <p className="mb-6 max-w-2xl text-sm text-ink-muted">
        These rules are enforced in code over every LLM proposal — the model never has the last word
        on money. They are expressed declaratively and rendered here in plain language so the policy
        itself is an auditable artifact. The first matching rule (by priority) overrides the LLM’s
        proposal.
      </p>

      {isError && (
        <ErrorAlert message={`Could not load rules: ${error.message}`} onRetry={refetch} />
      )}

      {isPending ? (
        <TableSkeleton rows={7} columns={4} />
      ) : !data || data.length === 0 ? (
        <EmptyState title="No rules configured" body="The stopping-rule registry is empty." />
      ) : (
        <div className="overflow-x-auto rounded-xl border border-line bg-surface shadow-card">
          <table className="w-full min-w-[560px] border-collapse text-left text-sm">
            <caption className="sr-only">Active stopping rules</caption>
            <thead className="border-b border-line">
              <tr className="text-xs uppercase tracking-wide text-ink-faint">
                <th scope="col" className="px-4 py-3 font-semibold">
                  Rule
                </th>
                <th scope="col" className="px-4 py-3 font-semibold">
                  Priority
                </th>
                <th scope="col" className="px-4 py-3 font-semibold">
                  Forced action
                </th>
                <th scope="col" className="px-4 py-3 font-semibold">
                  Policy (live values)
                </th>
              </tr>
            </thead>
            <tbody>
              {data.map((rule) => (
                <tr key={rule.rule_id} className="border-b border-line last:border-0">
                  <td className="px-4 py-3 font-bold text-decide">{rule.rule_id}</td>
                  <td className="px-4 py-3 text-center text-ink-muted">{rule.priority}</td>
                  <td className="px-4 py-3 font-medium capitalize">{actionLabel(rule.action)}</td>
                  <td className="px-4 py-3 text-ink-muted">{rule.description}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
