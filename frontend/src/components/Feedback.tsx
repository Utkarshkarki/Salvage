import type { ReactNode } from "react";
import { ApiError } from "../api/client";

/** Formats INR amounts as the legacy dashboard does (Rs. x,xxx.xx). */
export function formatINR(amount: number): string {
  return `Rs.${amount.toLocaleString("en-IN", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })}`;
}

/** Human message from an unknown error — prefers a readable API detail. */
export function errorMessage(err: unknown): string {
  if (err instanceof ApiError) {
    if (err.status === 0) return err.detail;
    return err.detail || `Request failed with status ${err.status}.`;
  }
  if (err instanceof Error) return err.message;
  return "An unexpected error occurred.";
}

/** Full-width inline error banner with a retry action. */
export function ErrorAlert({ message, onRetry }: { message: string; onRetry?: () => void }) {
  return (
    <div
      role="alert"
      className="my-4 flex flex-wrap items-center justify-between gap-3 rounded-xl border border-escalated-border bg-escalated-soft px-4 py-3 text-sm text-escalated"
    >
      <span className="font-medium">{message}</span>
      {onRetry && (
        <button
          type="button"
          onClick={onRetry}
          className="rounded-lg border border-escalated-border bg-surface px-3 py-1.5 font-semibold text-escalated transition hover:bg-escalated hover:text-white focus:outline-none focus-visible:ring-2 focus-visible:ring-escalated focus-visible:ring-offset-1"
        >
          Retry
        </button>
      )}
    </div>
  );
}

/** Explicit empty-state block (B5.1). */
export function EmptyState({ title, body }: { title: string; body: ReactNode }) {
  return (
    <div className="my-8 rounded-xl border border-dashed border-line bg-surface p-10 text-center">
      <p className="mb-1 text-lg font-semibold text-ink">{title}</p>
      <p className="text-sm text-ink-muted">{body}</p>
    </div>
  );
}

/** Small loading row (for pages that are a single list, not a table). */
export function LoadingRow({ label = "Loading…" }: { label?: string }) {
  return (
    <p role="status" aria-live="polite" className="py-10 text-center text-sm text-ink-muted">
      {label}
    </p>
  );
}
