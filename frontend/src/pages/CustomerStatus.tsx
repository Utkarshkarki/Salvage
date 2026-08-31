import { useParams } from "react-router-dom";
import { useCustomerStatus } from "../hooks/useApi";
import { Skeleton } from "../components/Skeleton";

/**
 * B5.5 — customer-facing status page. Deliberately DISTINCT from the internal
 * tool: no rule IDs, no stage names, no LLM/fallback jargon, and no dashboard
 * chrome (this page is routed OUTSIDE the app Layout, so there is no header
 * nav). It reads the same A7 data the internal pages share behind the scenes,
 * but renders only plain language.
 */
export default function CustomerStatus() {
  const { caseId = "" } = useParams<{ caseId: string }>();
  const { data, isPending, isError } = useCustomerStatus(caseId);

  if (isError) {
    // A public customer page should not surface internal error codes; keep it
    // friendly and actionable.
    return (
      <main className="flex min-h-screen items-center justify-center bg-canvas px-4">
        <div className="w-full max-w-md rounded-2xl bg-surface p-8 text-center shadow-card">
          <h1 className="mb-2 text-xl font-bold text-ink">We couldn’t find that reference.</h1>
          <p className="text-sm text-ink-muted">
            The reference you provided isn’t recognised. If you believe this is an error, please
            contact your service provider.
          </p>
        </div>
      </main>
    );
  }

  return (
    <main className="flex min-h-screen items-center justify-center bg-canvas px-4">
      <div className="w-full max-w-md rounded-2xl bg-surface p-8 shadow-card">
        {isPending || !data ? (
          <div role="status" aria-label="Loading status" className="space-y-3">
            <Skeleton className="h-7 w-3/4" />
            <Skeleton className="h-4 w-full" />
            <Skeleton className="h-4 w-full" />
            <Skeleton className="h-16 w-full" />
            <span className="sr-only">Loading…</span>
          </div>
        ) : (
          <>
            <h1 className="mb-1 text-2xl font-bold text-ink">{data.heading}</h1>
            <p className="mb-1 text-xs text-ink-faint">Reference: {caseId}</p>
            <p className="mb-3 text-sm leading-relaxed text-ink-muted">{data.reason}</p>
            <p className="rounded-xl bg-canvas p-4 text-sm leading-relaxed text-ink">
              <strong>What happens next:</strong> {data.next_step}
            </p>
            <p className="mt-4 text-xs text-ink-faint">
              Need help? Contact your service provider. This is a demo view of your payment recovery
              status.
            </p>
          </>
        )}
      </div>
    </main>
  );
}
