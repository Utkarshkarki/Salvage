import { lazy, Suspense } from "react";
import { Route, Routes } from "react-router-dom";
import Layout from "./components/Layout";
import { ErrorBoundary } from "./components/ErrorBoundary";
import CustomerStatus from "./pages/CustomerStatus";

// Route-based code splitting (B6): each internal page is loaded only when
// first navigated to, so the initial bundle is the app shell, not the whole
// tool. The Customer Status page is deliberately NOT lazy — it is the one
// customer-facing surface and should render as quickly as possible.
const CaseList = lazy(() => import("./pages/CaseList"));
const CaseDetail = lazy(() => import("./pages/CaseDetail"));
const Rules = lazy(() => import("./pages/Rules"));
const Simulator = lazy(() => import("./pages/Simulator"));

function PageFallback() {
  return (
    <div
      role="status"
      aria-live="polite"
      className="py-16 text-center text-sm text-ink-muted"
    >
      Loading page…
    </div>
  );
}

export default function App() {
  return (
    // Top-level boundary: an unexpected error anywhere still renders a
    // recoverable screen instead of a blank app.
    <ErrorBoundary label="this page">
      <Routes>
        {/* B5.5 — customer-facing, OUTSIDE the app Layout (no dashboard chrome). */}
        <Route path="/status/:caseId" element={<CustomerStatus />} />

        {/* Internal tool pages, inside the shared Layout shell. */}
        <Route element={<Layout />}>
          <Route
            path="/"
            element={
              <Suspense fallback={<PageFallback />}>
                <CaseList />
              </Suspense>
            }
          />
          <Route
            path="/cases/:caseId"
            element={
              <Suspense fallback={<PageFallback />}>
                <CaseDetail />
              </Suspense>
            }
          />
          <Route
            path="/rules"
            element={
              <Suspense fallback={<PageFallback />}>
                <Rules />
              </Suspense>
            }
          />
          <Route
            path="/simulator"
            element={
              <Suspense fallback={<PageFallback />}>
                <Simulator />
              </Suspense>
            }
          />
        </Route>
      </Routes>
    </ErrorBoundary>
  );
}
