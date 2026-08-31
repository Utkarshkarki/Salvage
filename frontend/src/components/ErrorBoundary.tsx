import { Component, type ErrorInfo, type ReactNode } from "react";

interface Props {
  children: ReactNode;
  /** Optional friendly label used in the fallback, e.g. the page name. */
  label?: string;
}

interface State {
  hasError: boolean;
}

/**
 * Route-level error boundary (B6). An unhandled error in one page must not
 * blank the whole app — this catches render-time errors in its subtree and
 * shows a recoverable fallback instead. Kept as a class component because
 * React error boundaries have no function-component equivalent.
 */
export class ErrorBoundary extends Component<Props, State> {
  state: State = { hasError: false };

  static getDerivedStateFromError(): State {
    return { hasError: true };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    // Ideally this would report to a logging sink; at minimum ensure the
    // error is visible in the console rather than silently swallowed.
    console.error("Reclaim page error:", error, info.componentStack);
  }

  handleReset = (): void => {
    this.setState({ hasError: false });
  };

  render(): ReactNode {
    if (this.state.hasError) {
      return (
        <div
          role="alert"
          className="mx-auto mt-16 max-w-md rounded-xl border border-escalated-border bg-escalated-soft p-6 text-center"
        >
          <h1 className="mb-2 text-lg font-semibold text-ink">
            Something went wrong{this.props.label ? ` on ${this.props.label}` : ""}.
          </h1>
          <p className="mb-4 text-sm text-ink-muted">
            An unexpected error occurred while rendering this section. The rest of the app
            is unaffected.
          </p>
          <button
            type="button"
            onClick={this.handleReset}
            className="rounded-lg bg-escalated px-4 py-2 text-sm font-semibold text-white transition hover:opacity-90 focus:outline-none focus-visible:ring-2 focus-visible:ring-escalated focus-visible:ring-offset-2"
          >
            Try again
          </button>
        </div>
      );
    }
    return this.props.children;
  }
}
