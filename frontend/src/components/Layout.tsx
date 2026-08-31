import { NavLink, Outlet } from "react-router-dom";

const NAV_ITEMS = [
  { to: "/", label: "Cases" },
  { to: "/rules", label: "Rules" },
  { to: "/simulator", label: "Simulator" },
] as const;

/**
 * App shell: header nav + routed content. The nav is the only shared chrome;
 * the Customer Status page (B5.5) deliberately does NOT render inside this
 * layout so it reads as a plain, customer-facing view.
 */
export default function Layout() {
  return (
    <div className="min-h-screen bg-canvas text-ink">
      <header className="border-b border-line bg-surface">
        <div className="mx-auto flex max-w-6xl items-center justify-between gap-4 px-4 py-3">
          <NavLink to="/" className="flex items-center gap-2 text-lg font-bold">
            <span aria-hidden="true">🛡️</span>
            <span>Reclaim</span>
          </NavLink>
          <nav aria-label="Primary" className="flex items-center gap-1">
            {NAV_ITEMS.map((item) => (
              <NavLink
                key={item.to}
                to={item.to}
                end={item.to === "/"}
                className={({ isActive }) =>
                  `rounded-lg px-3 py-2 text-sm font-medium transition focus:outline-none focus-visible:ring-2 focus-visible:ring-diagnose ${
                    isActive
                      ? "bg-diagnose-soft text-diagnose"
                      : "text-ink-muted hover:bg-canvas hover:text-ink"
                  }`
                }
              >
                {item.label}
              </NavLink>
            ))}
          </nav>
        </div>
      </header>
      <main className="mx-auto max-w-6xl px-4 py-6">
        <Outlet />
      </main>
    </div>
  );
}
