# Reclaim Frontend (Phase 4 React SPA)

A Vite + React + TypeScript (strict) single-page app for the Reclaim revenue-
recovery tool. It talks to the backend's `/api/v1/*` JSON namespace (a parallel
surface to the existing Jinja2 pages, which remain live as a fallback).

## Stack

- **Vite + React 18 + TypeScript strict** — SPA (no Next.js: the backend is
  FastAPI/Python, there is no SSR/SEO need, and a second Node server would add
  nothing).
- **Tailwind CSS** — utility-first styling; the color semantics of the legacy
  dashboard are captured as design tokens in `tailwind.config.ts` (diagnose=
  blue, decide=purple, act=teal, override=amber, recovered=green,
  escalated=red, LLM-failure-fallback=orange).
- **TanStack Query** — server-state caching, deduplication, and automatic
  refetch after the manual-override mutations in Case Detail. This is *server*
  state, not client state — no Redux, which would be over-engineering.
- **React Router** + `React.lazy`/`Suspense` — route-based code splitting so
  the initial bundle is the shell, not the whole tool.

## Pages

| Route | Purpose |
|-------|---------|
| `/` | Case list — state filter, pagination, click-through |
| `/cases/:caseId` | Case detail — full audit trail, provenance, override action buttons for ESCALATED cases |
| `/rules` | Active stopping rules (R1–R7) in plain language |
| `/simulator` | Threshold sensitivity — before/after comparison, throwaway DB |
| `/status/:caseId` | Customer-facing status (plain language, **no** app chrome) |

## Local development

Two terminals:

**Terminal 1 — backend (from the repo root):**

```bash
uvicorn reclaim.api:app --reload
```

**Terminal 2 — frontend:**

```bash
cd frontend
npm install
npm run dev
```

The Vite dev server proxies `/api` to `http://127.0.0.1:8000`, so no CORS occurs
in development. Open http://localhost:5173.

> You need a `RAZORPAY_WEBHOOK_SECRET` in the repo-root `.env` (or env) because
> the backend refuses to boot with an empty secret. Run `python -m reclaim.batch`
> (or POST webhooks) to generate cases before exploring the dashboard.

## ⚠️ CORS caveat — do NOT mistake this for deployment-ready

The FastAPI backend enables CORS for the Vite dev origins
(`http://localhost:5173`, `http://127.0.0.1:5173`) via `RECLAIM_CORS_ORIGINS`
(default in `config.py`). This is a **local-demo default only**. It is an
explicit, non-wildcard list — never a wildcard. For any real deployment:

1. **Serve the SPA and API from the same origin** (put the built `dist/` behind
   the same reverse proxy as `/api`). Then CORS is unnecessary entirely — the
   Vite `/api` proxy is only for dev, and the built app calls relative URLs.
2. If the frontend and API must live on different origins, set
   `RECLAIM_CORS_ORIGINS` to exactly the deployed frontend origin(s). Keep
   `allow_credentials` only where you truly need cookies (the API here is
   unauthenticated and bearer-based — there are no credentials to share).

## Scripts

| Command | What it does |
|---------|--------------|
| `npm run dev` | Vite dev server (proxies `/api`) |
| `npm run build` | Type-check + production build into `dist/` |
| `npm run preview` | Serve the production build locally |
| `npm test` | Vitest component tests |
| `npm run lint` | ESLint |
| `npm run format` | Prettier |
| `npm run typecheck` | TypeScript strict check |

## Testing

Vitest + React Testing Library cover the logic-bearing components — the Case
Detail override-action flow (buttons only when ESCALATED, disabled while
pending, distinct 409 vs generic error surfacing) and the Simulator form
(submits the correct payload shape, renders the comparison on success). Static
display-only components are not exhaustively covered.

### E2E (Playwright/Cypress) — trade-off note

We deliberately did **not** add browser-level E2E coverage for this phase.
Reasoning: the remaining scope is a local buildathon demo, the app is
read-mostly, and the interaction-heavy paths (override actions, simulator) are
already exercised at the component level plus the backend HTTP layer
(`tests/test_api_v1.py`). A full Playwright suite would add meaningful tooling
and CI surface for thin marginal value at this point.

If the project proceeds toward a real multi-user deployment, worth adding then:
a single Playwright smoke spec (load each route, confirm no console errors,
exercise one override action end-to-end) would be the right size. We would add
it when there is a real deployment target and CI to run it against, not before.

## Codegen vs. hand-maintained types

We hand-maintain the TypeScript types in `src/types.ts` (matching the backend
vocabulary exactly) rather than generating them from the FastAPI OpenAPI schema
via `openapi-typescript`. The API surface is small (~10 shapes) and already
mirrors Python `StrEnum`s whose machine values *are* the contract — a codegen
step would add a build dependency plus a chicken-and-egg requirement (backend
must run to emit the schema) for no current benefit. The backend
`tests/test_api_v1.py` asserts the wire shapes, which is where drift would
surface. If the API grows, switch to codegen.
