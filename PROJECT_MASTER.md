# PROJECT_MASTER.md — Reclaim: AI Revenue Recovery Agent

**Repository:** https://github.com/Utkarshkarki/Salvage
**Version:** 0.1.0
**License:** MIT © 2026 Utkarsh Karki
**Status:** Pipeline complete + hardened + **Phase 4 production-grade React frontend** + **Phase 5 statistical rigor & structural proof** (multi-seed robustness, counterfactual baseline, tamper-evident audit chain, LLM-isolation proof) + **Phase 6.1 live Razorpay provenance tier** (live/replay/mocked case tagging, verbatim captured fixtures, a real test-mode failure generator). Backend suite green (**176 tests across 20 files, all passing** — verified in-session) + frontend component suite green (**6 tests across 2 files**, Vitest + React Testing Library). The Phase 6.1 **live** step remains operator-gated: no real Razorpay test-mode delivery has been captured yet (see README's "Live Razorpay Integration").

> This document is the authoritative, file-by-file engineering reference for the Reclaim codebase.
> It does not summarize — it enumerates every directory, file, class, function, schema, and
> interaction in the project, verified against the actual source.

---

## 1. Project Overview & Purpose

**Reclaim** is a self-contained AI agent that **recovers failed recurring revenue on Razorpay
subscriptions.** When a customer's card-failure webhook fires, Reclaim walks the case through a
**guarded, explainable state machine**: an LLM *proposes* a recovery action, and hard-coded business
and stopping rules *dispose*. Every decision is validated with Pydantic, logged to an append-only
audit trail, and executed **idempotently** so a payment is never double-charged. Phase 3 added an
**adversarial-resilience test suite**, real concurrency/crash hardening, and a set of genuine
differentiators so it clears comparable public submissions in this track. Phase 4 added a
**production-grade React SPA** (`frontend/`) plus a **parallel `/api/v1/*`
JSON namespace** (`api_v1.py`), so the same, already-tested pipeline now exposes both a merchant/
operator UI and a machine-readable API surface. Phase 5 added **statistical rigor & structural
proof**: multi-seed robustness reporting, a counterfactual baseline comparison against two naive
strategies, a tamper-evident hash-chained audit log, an explicit precision principle in the docs,
and a structural test proving the LLM cannot reach money-moving code. Phase 6.1 (the current state
of this document) added the **live Razorpay integration / provenance tier**: every case is tagged
at ingest with where its data really came from (`live` real webhook vs `replay` synthetic through
the real boundary vs `mocked` evaluation-only), raw signature-passing payloads are captured
verbatim as replay-traceable fixtures (`capture.py`), and `reclaim-live` can generate a genuine
test-mode failure via the Payments payment-links API. The honest marker of "live integration
complete" is a real `payment.failed` delivery in `fixtures/captured/` — not the code's capability.

It was built for the **Razorpay AI Buildathon — Track 3 (AI agent for revenue recovery)**.

### Who is it for?
- **Buildathon judges / technical reviewers** — the primary audience. The design deliberately
  separates "the LLM was down" from "the LLM proposed something unsafe and code rejected it."
- **Fintech / payments engineers** — anyone evaluating a safe pattern for adding an LLM to a
  money-movement loop without letting the model hold final authority over funds.
- **Demo operators** — the project runs fully deterministically offline (no network, no
  credentials) and is safe by default.

### Core value proposition
> **Money movement is never left to the model's whim.**

The LLM narrows a failed payment to **exactly one bounded action**; a deterministic, unit-tested
stopping-rule layer clamps unsafe proposals in code; idle / overdue / unsafe / trivially-small cases
are deliberately halted or escalated to a human. This yields three hard guarantees:

1. **Explainability** — every stage transition is recorded in an append-only audit trail with full
   agent reasoning and a machine-readable rule id. LLM calls additionally record provenance (model,
   prompt version, prompt content hash).
2. **Safety** — seven code-enforced stopping rules (R1–R7) can override any LLM proposal; the model
   is never the last word on money. Adversarial/malformed input to the Diagnose model is triaged
   **before** the model is consulted.
3. **Idempotency** — a retry can never fire twice for the same `(case, attempt, action)`; enforced
   by a database `UNIQUE` constraint, not a promise. This survives races, network drops, and a
   mid-pipeline process death (via a stale-lock reconciliation sweep).

### What it does (end to end)
- **Ingests** failed-payment webhooks through the real Razorpay webhook boundary: constant-time
  HMAC-SHA256 signature verification, strict payload parsing, and `event_id`-based dedupe
  (race-safe via a DB `UNIQUE` constraint).
- **Diagnoses** the root cause of a failure (insufficient funds, card expired, mandate revoked,
  bank timeout, …) with a structured, confidence-scored output — after a triage step short-circuits
  hostile/malformed decline codes before they reach the model.
- **Decides** exactly one bounded action: `retry_now`, `retry_scheduled`,
  `request_payment_method_update`, `escalate_human`, or `stop`.
- **Enforces** seven code-level stopping rules (policy-as-code, introspectable on `/rules`) that can
  override any LLM proposal.
- **Acts** idempotently — a retry can never fire twice for the same `(case, attempt, action)` —
  and logs everything to an append-only audit trail. Verification-only reads corroborate external
  state (Subscriptions / Settlements) without ever blocking or reversing.
- **Reports** batch metrics that distinguish **LLM failures**, **stopping-rule overrides**, and
  **stub-mode demo actions** (three counters that are deliberately not conflated).
- **Recovers from crashes** synchronously: a case stuck in `ACTING` (process died mid-pipeline) is
  swept to human review by a periodic reconciliation, and a human can then approve a retry or
  resolve the case through the manual- override control plane.

**Note on documentation drift:** earlier drafts of this file cited "68 tests / 7 files" (itself a
correction of the older "63 tests / 6 files"), then "99 tests / 12 files", then "117 tests / 13
files", then "160 backend tests / 19 files" (post-Phase-5 with the correctness-fix rows). This
version has been rewritten against the current, verified state: **176 backend tests across 20
files** (Phase 6.1 adds `test_provenance.py` = 10, and grows `test_api_v1.py` → 21,
`test_metrics.py` → 9, `test_webhook.py` → 15) plus **6 frontend component tests across 2 files**
(`Vitest`), matching `PROGRESS.md` and `README.md`. The per-file counts and module inventory below
reflect the current repository.

---

## 2. Tech Stack & Dependencies

All runtime dependencies are declared in `pyproject.toml`. There is **no lockfile**; versions are
declared as minimums (`>=`). The versions below are (a) the declared constraints from
`pyproject.toml` and (b) the versions actually installed and verified in the author's development
environment.

### Runtime languages
- **Python** — `requires-python = ">=3.11"` (verified on CPython **3.11.9**).

### Runtime dependencies (from `pyproject.toml`, `[project].dependencies`)
| Package | Declared constraint | Verified installed |
|---------|--------------------|--------------------|
| `fastapi` | `>=0.111` | 0.115.14 |
| `uvicorn[standard]` | `>=0.30` | 0.52.4 |
| `pydantic` | `>=2.7` | 2.13.5 |
| `pydantic-settings` | `>=2.3` | 2.13.1 |
| `sqlalchemy` | `>=2.0` | 2.0.25 |
| `celery` | `>=5.4` | 5.6.3 |
| `redis` | `>=5.0` | 7.4.0 |
| `openai` | `>=1.35` | 2.30.0 |
| `httpx` | `>=0.27` | 0.28.1 |
| `jinja2` | `>=3.1` | 3.1.3 |

### Development dependencies (from `pyproject.toml`, `[project.optional-dependencies].dev`)
| Package | Declared constraint | Verified installed |
|---------|--------------------|--------------------|
| `pytest` | `>=8.2` | 8.4.2 |
| `ruff` | `>=0.5` | 0.15.11 |
| `mypy` | `>=1.10` | 2.3.1 |

### Frontend dependencies (from `frontend/package.json` — Phase 4 React SPA)
The SPA is a separate Node/TypeScript package in `frontend/`, with its own `package.json` /
`package-lock.json` (committed). Declared with standard `^` ranges; the exact resolved versions
live in `package-lock.json`.

| Package | Declared range | Role |
|---------|----------------|------|
| `react` / `react-dom` | `^18.3.1` | UI runtime |
| `react-router-dom` | `^6.26.0` | Route-based code-split navigation |
| `@tanstack/react-query` | `^5.51.0` | Server-state caching / dedup / auto-refetch after mutations |
| `typescript` | `^5.5.4` | Strict-typed language (app + tooling tsconfigs) |
| `vite` | `^5.4.0` | Dev server + production build (dev proxy `/api` → `:8000`) |
| `tailwindcss` | `^3.4.7` | Utility CSS; design tokens centralized in `tailwind.config.ts` |
| `vitest` | `^2.0.5` | Component test runner (jsdom) |
| `@testing-library/react` + `user-event` + `jest-dom` | `^16` / `^14.5` / `^6.4` | React Testing Library |
| `eslint` / `typescript-eslint` + react-hooks/refresh | `^9.8` / `^8` | Linting |
| `prettier` | `^3.3.3` | Formatting |
| `@vitejs/plugin-react`, `postcss`, `autoprefixer`, `jsdom`, `@types/*` | — | Build + test toolchain |

### Console entrypoints (`[project.scripts]` in `pyproject.toml`)
All five CLIs are also invocable as `python -m reclaim.*`; the scripts are thin wrappers:
| Script | Entry function | What it runs |
|--------|----------------|--------------|
| `reclaim-batch` | `reclaim.batch:main` | The synthetic-batch demo (60 new cases) |
| `reclaim-robustness` | `reclaim.robustness:main` | Multi-seed robustness suite (default 100 seeds) |
| `reclaim-baseline` | `reclaim.baseline:main` | Counterfactual baseline comparison |
| `reclaim-verify-audit-chain` | `reclaim.verify_audit_chain:main` | Tamper-evidence audit-chain walk |
| `reclaim-live` | `reclaim.live:main` | **(Phase 6.1)** real test-mode failure generator (`create` a Payment Link / `dump` captured fixtures) |

### Technology role map
| Layer | Technology used |
|-------|-----------------|
| API / webhook boundary | FastAPI + Uvicorn |
| Domain + boundary validation | Pydantic v2 (`pydantic-settings`) — "zero-halo" |
| Persistence / ORM | SQLAlchemy 2 (SQLite by default with **WAL mode**; Postgres-compatible schema) |
| Async job queue | Celery + Redis / **Upstash** (`rediss://` over TLS); eager mode for tests/demos; **periodic beat schedule** for the stale-lock reconciliation |
| LLM backend | OpenAI-compatible client (`openai` SDK) → local **Ollama** (e.g. `qwen2.5:32b-instruct`); deterministic offline shim; adversarial-input triage + provenance |
| Outbound HTTP (Razorpay live) | `httpx` |
| Templating (legacy dashboard / status / rules / simulator renders) | Jinja2 (declared; pages currently hand-build HTML) |
| **JSON API namespace (Phase 4)** | FastAPI `APIRouter` mounted at `/api/v1/*` below the **CORS middleware** — thin wrappers over the same tested logic |
| **React SPA (Phase 4)** | Vite + React 18 + TypeScript (strict) + Tailwind CSS + TanStack Query + React Router (`frontend/`) |
| **Statistical rigor (Phase 5)** | `robustness.py` (multi-seed suite), `baseline.py` + `retry_simulator.py` (counterfactual comparison), `audit_chain.py` + `verify_audit_chain.py` (SHA-256 hash-chained audit log) |
| **Provenance tier (Phase 6.1)** | `capture.py` (verbatim captured fixtures + sensitive-scan warnings), `live.py` (real test-mode Payment Link generator + fixture dump), `provenance` tag on every `RecoveryCase` (`live`/`replay`/`mocked`), `provenance_breakdown` + `provenance=` filter in `compute_metrics`; `httpx` for the live `create` call |
| Testing / lint / typing | pytest (incl. thread-based adversarial suite), Ruff, mypy (strict); frontend: Vitest + React Testing Library; `test_llm_isolation.py` (AST-based structural proof) |

### Runtime modes (env-gated, safe by default)
- `LLM_MODE=offline` → deterministic rule shim (hermetic tests & demos, no network).
- `LLM_MODE=online` → real Ollama calls with exponential backoff on transient timeouts, then the
  deterministic fallback.
- `ACT_MODE=stub` → log the would-be Razorpay call (safe for demos, no credentials).
- `ACT_MODE=live` → real test-mode Razorpay calls; **refuses** to run without valid keys and
  refuses to guess an unconfirmed API route (`RAZORPAY_RETRY_PATH` must be set).
- (Phase 6.1) `RAZORPAY_WEBHOOK_CAPTURE_DIR` → where signature-passing payloads are written
  verbatim as captured fixtures (`{event_type}/{event_id}.json`). Empty = capture **disabled**
  (hermetic default). `RAZORPAY_PAYMENT_LINK_PATH` → the Payments payment-links route for
  `reclaim-live create`; empty → the tool **refuses to run** (ZERO-HALO, never guesses a wire
  format).
- `VERIFICATION_ENABLED` → gates the verification-only Subscriptions/Settlements lookups (`1`
  default; `0` makes them silent and hermetic).
- `RECLAIM_CELERY_EAGER=1` → run Celery tasks synchronously, no broker needed.
- `RECLAIM_CORS_ORIGINS` → comma-separated browser origins the API allows for the React SPA
  (default is the Vite dev server only: `http://localhost:5173,http://127.0.0.1:5173`). Never a
  wildcard; for a real deployment serve SPA+API from one origin so CORS is unnecessary.

### Databases
- **SQLite** (default dev engine, `sqlite:///reclaim.db`, plus per-run `reclaim_fresh_*.db` files
  for the batch demo and a temp file for the simulator). File-backed connections run in **WAL
  mode** (`journal_mode=WAL` + `synchronous=NORMAL` + a busy timeout) so concurrent readers and
  writers never block or corrupt. Schema is intentionally **PostgreSQL-compatible**: `JSON` for
  nested payloads, `String` for enums, indexed unique constraints for dedupe + idempotency.
- **Redis / Upstash** — Celery broker and result backend (TLS `rediss://` for Upstash). Not
  required in eager mode.

### Development tools
- **Git** (repo `main` branch; remote `https://github.com/Utkarshkarki/Salvage.git`), `ruff`
  (lint), `mypy` (strict type checking), `pytest` (test runner), `python -m` entrypoints.

---

## 3. Repository Architecture & Directory Structure

### ASCII tree (verified against `git ls-files` and disk)

```
Salvage/
├── .claude/
│   └── settings.local.json          # local, user-specific agent config (gitignored)
├── .env                             # REAL secrets (local only — gitignored)
├── .env.example                     # env template with placeholders (committed)
├── .gitignore                       # tailored ignore rules (kept from the project)
├── CHANGELOG_SUBMISSION.md          # dated, phase-level log of major changes
├── baseline-analysis.html           # Phase 5 counterfactual comparison write-up (stands alone)
├── DECISIONS.md                     # running log of architecture decisions + why
├── LICENSE                          # MIT license text
├── PROGRESS.md                      # living build-progress journal
├── PROJECT_MASTER.md                # this document (generated engineering reference)
├── README.md                        # primary human-facing documentation + demo report
├── pyproject.toml                   # build system, backend deps, pytest/ruff/mypy config
├── reclaim.db                       # SQLite dev database (runtime artifact, gitignored)
├── tasks.md                         # built vs. explicitly out-of-scope (Track 3/4 boundary)
├── scripts/
│   ├── dev.sh                       # launch uvicorn + Vite dev together (bash)
│   └── dev.ps1                      # same, Windows PowerShell
├── src/
│   └── reclaim/
│       ├── __init__.py              # package docstring + __version__ = "0.1.0"
│       ├── act.py                   # idempotent, fault-isolated action execution
│       ├── api.py                   # FastAPI app: webhook + HTML surfaces + CORS + mounts /api/v1
│       ├── api_v1.py                # Phase 4 JSON namespace (/api/v1/*) — thin wrappers (A1–A7)
│       ├── api_views.py             # shared derived views (simulator batch + customer status)
│       ├── audit.py                 # append-only audit-log writer + finalize_audit_chain (Phase 5)
│       ├── audit_chain.py           # Phase 5 hash-chain canonicalization + chain_rows
│       ├── batch.py                 # `python -m reclaim.batch` CLI entrypoint (+chain finalize)
│       ├── baseline.py              # Phase 5 counterfactual baseline comparison (3 strategies)
│       ├── capture.py               # (Phase 6.1) verbatim captured fixtures + sensitive-scan warning
│       ├── celery_app.py            # Celery app + broker config (Upstash TLS / eager / beat)
│       ├── config.py                # pydantic-settings env-driven Settings
│       ├── db.py                    # SQLAlchemy 2 persistence layer + ORM tables + WAL (+hash cols)
│       ├── dispatcher.py            # webhook -> pipeline handoff (eager / celery)
│       ├── email.py                 # email stub (single seam for a real provider)
│       ├── live.py                  # (Phase 6.1) real test-mode failure generator (Payment Link + dump)
│       ├── llm_client.py            # offline shim / online Ollama wrapper + triage + provenance
│       ├── manual.py                # human-in-the-loop override actions (control plane)
│       ├── metrics.py               # batch metrics from audit trail + case states (+precision docs)
│       ├── models.py                # Pydantic v2 schemas for every boundary
│       ├── pipeline.py              # run_case / run_batch orchestrator + concurrency
│       ├── razorpay_client.py       # stub/live client (retry + subscription + settlement)
│       ├── repo.py                  # read/update helpers (incl. query-layer list_cases)
│       ├── retry_simulator.py       # Phase 5 shared retry-success model by decline code
│       ├── robustness.py            # Phase 5 multi-seed robustness suite runner
│       ├── state_machine.py         # guarded transition table + lifecycle machine
│       ├── stopping_rules.py        # declarative policy-as-code R1–R7
│       ├── sweep.py                 # stale-ACTING-lock reconciliation (crash recovery)
│       ├── synthetic.py             # seeded synthetic webhook batch generator
│       ├── tasks.py                 # Celery task entrypoints (incl. periodic sweep)
│       ├── verify.py                # verification-only Subscriptions/Settlements lookups
│       ├── verify_audit_chain.py    # Phase 5 audit-chain tamper-evidence walk (CLI)
│       └── webhook.py               # signature verify, parse, dedupe
├── frontend/                        # Phase 4 — React SPA (Vite + TS strict + Tailwind + React Query)
│   ├── README.md                    # frontend docs: setup, CORS caveat, testing trade-offs
│   ├── package.json / package-lock.json
│   ├── vite.config.ts               # dev server (:5173, /api proxy → :8000) + Vitest config
│   ├── tailwind.config.ts           # design tokens (diagnose/decide/act/override/recovered/…)
│   ├── tsconfig.json / tsconfig.app.json / tsconfig.node.json
│   ├── eslint.config.js / postcss.config.js / .prettierrc / .prettierignore
│   ├── index.html / public/favicon.svg
│   └── src/
│       ├── main.tsx                 # React root: QueryClient + BrowserRouter + StrictMode
│       ├── App.tsx                  # routes + React.lazy/Suspense + ErrorBoundary
│       ├── index.css
│       ├── types.ts                 # hand-maintained TS mirror of the Python enums/shapes
│       ├── api/
│       │   ├── client.ts            # typed fetch wrapper (ApiError carrying status + detail)
│       │   └── endpoints.ts         # one fn per /api/v1 route, typed by ../types
│       ├── hooks/useApi.ts          # TanStack Query hooks + QUERY_KEYS + override mutations
│       ├── components/
│       │   ├── AuditTrail.tsx       # per-stage blocks; override / fallback badges; provenance
│       │   ├── ErrorBoundary.tsx    # route-level, recoverable render fallback
│       │   ├── Feedback.tsx         # formatINR / ErrorAlert / EmptyState / LoadingRow
│       │   ├── Layout.tsx           # app shell (nav: Cases / Rules / Simulator)
│       │   ├── ManualActions.tsx    # A6 override buttons (ESCALATED only; 409 surfaced)
│       │   ├── ManualActions.test.tsx  # 4 Vitest tests (override flow)
│       │   ├── Skeleton.tsx         # CSS skeleton primitives (loading states)
│       │   ├── StateBadge.tsx       # color-coded state pills
│       │   └── test/setup.ts        # Vitest setup (React Testing Library + jest-dom)
│       └── pages/
│           ├── CaseList.tsx         # state filter + query-layer pagination + empty state
│           ├── CaseDetail.tsx       # audit trail + operator override control plane
│           ├── Rules.tsx            # R1–R7 policy-as-code table (live values)
│           ├── Simulator.tsx        # threshold form + before/after bar comparison
│           ├── Simulator.test.tsx   # 2 Vitest tests (payload shape + comparison render)
│           └── CustomerStatus.tsx   # plain-language status, routed OUTSIDE the Layout
└── tests/
    ├── conftest.py                  # hermetic fixtures (settings/db, no .env)
    ├── test_api_v1.py               # 21 tests — Phase 4 HTTP-layer JSON API surface (+Phase 6.1 webhook/capture paths)
    ├── test_audit_chain.py          # 13 tests — Phase 5 hash-chain tamper-evidence
    ├── test_baseline.py             # 12 tests — Phase 5 counterfactual strategies
    ├── test_llm_isolation.py        # 3 tests — Phase 5 structural LLM isolation (AST)
    ├── test_manual.py               # 5 tests — human override actions
    ├── test_metrics.py              # 9 tests — metric counter independence (+Phase 6.1 provenance)
    ├── test_models.py               # 9 tests — Pydantic boundary validation
    ├── test_pipeline.py             # 9 tests — end-to-end fallback/idempotency/state
    ├── test_provenance.py           # 10 tests — Phase 6.1 provenance tier contract
    ├── test_robustness.py           # 8 tests — Phase 5 multi-seed distribution
    ├── test_simulator.py            # 4 tests — rule-sensitivity simulator
    ├── test_state_machine.py        # 14 tests — transition-table legality + manual edges
    ├── test_stopping_rules.py       # 20 tests — every R1–R7 override (policy-as-code)
    ├── test_synthetic.py            # 9 tests — generator invariants
    ├── test_tasks.py                # 3 tests — audited broker-mode retries (Phase 5 fix)
    ├── test_webhook.py              # 15 tests — signature, parse, dedupe (+provenance ingest)
    └── adversarial/                 # Failure Injection & Resilience suite (12 tests)
        ├── __init__.py
        ├── test_audit_chain_concurrency.py  # 1 — Phase 5 race-free chain finalize
        ├── test_concurrency.py      # 3 — duplicate webhooks + concurrent read/write
        ├── test_llm_adversarial.py  # 4 — injection/malformed triage + provenance
        └── test_sweep.py            # 4 — crash recovery + network-drop idempotency

# Untracked tool caches (gitignored): .mypy_cache/, .pytest_cache/, .ruff_cache/
# Frontend runtime/build artifacts (gitignored by frontend/.gitignore): node_modules/, dist/
# Local DB backup snapshots (gitignored): .db-backup-*/
```

### Directory & key file roles
- **`src/reclaim/`** — the entire application, packaged with `setuptools` under
  `[tool.setuptools.packages.find] where = ["src"]`. This is a **src-layout** package (**33
  modules** — Phases 1–6.1; Phase 6.1 adds `capture.py` + `live.py`).
- **`frontend/`** — a **separate Node/TypeScript package** (not part of the Python build): the
  Phase 4 React SPA (Vite + TS strict + Tailwind + TanStack Query). `package-lock.json` is
  committed; `node_modules/` and `dist/` are gitignored by `frontend/.gitignore`.
- **`tests/`** — the full pytest suite (**20 files, 176 tests**), hermetic by construction, with a
  dedicated `tests/adversarial/` category (now **12 failure-injection & resilience tests**, incl.
  the Phase 5 race-free audit-chain finalize regression), the Phase 4 `test_api_v1.py` covering the
  JSON API surface, the Phase 5 `test_audit_chain` / `test_baseline` / `test_robustness` /
  `test_llm_isolation` files, and the Phase 6.1 `test_provenance.py` pinning the provenance-tier
  contract (live-default ingest, explicit replay everywhere, verbatim capture, nullable legacy
  provenance). The separate `frontend/` component suite adds **6 tests across 2 files** (Vitest).
- **Root config/docs** — `pyproject.toml` (Python build + tooling + the five console scripts incl.
  `reclaim-live`), `.env.example` (env shape — now incl. `RAZORPAY_PAYMENT_LINK_PATH` and
  `RAZORPAY_WEBHOOK_CAPTURE_DIR`), `README.md`, `PROGRESS.md`, `DECISIONS.md`,
  `CHANGELOG_SUBMISSION.md`, `tasks.md`, `LICENSE`, `.gitignore` (+`.db-backup-*/` ignore rule),
  `baseline-analysis.html` (Phase 5 counterfactual write-up), plus `scripts/dev.sh` / `dev.ps1`
  (one-command dev servers).

---

## 4. Detailed Component & Module Breakdown

This section goes file-by-file through **every** module in `src/reclaim/`.

---

### 4.1 `src/reclaim/__init__.py`
- **Responsibility:** Package marker + public docstring.
- Declares `__version__ = "0.1.0"`.
- Docstring states the core architecture: *"Deterministic state machine with two narrow LLM
  workers (Diagnose, Decide). Pipeline: Ingest → Diagnose → Decide → Act (bounded) → Log. The LLM
  proposes; code disposes (stopping rules are authoritative)."*

---

### 4.2 `src/reclaim/config.py` — Environment configuration (zero-halo)
- **Responsibility:** Loads all settings from environment variables / `.env` via `pydantic-settings`
  `BaseSettings`. **No hardcoded secrets allowed**; required secrets fail loudly at load time.
- **Class `Settings(BaseSettings)`** — `SettingsConfigDict(env_file=".env", ...)` with
  `extra="ignore"`.
  - **LLM block:** `ollama_base_url` (`http://localhost:11434`), `ollama_model`
    (`qwen2.5:32b-instruct`), `ollama_timeout_seconds` (30.0), `llm_mode`
    (`Literal["offline","online"]`, default `offline`).
  - **Razorpay block:** `razorpay_webhook_secret` (default `""`, **validator requires non-empty**),
    `razorpay_key_id` / `razorpay_key_secret` (optional `str | None`), `razorpay_base_url`
    (`https://api.razorpay.com/v1`), `razorpay_retry_path` (default `""`),
    `razorpay_subscription_path` (default `""`), `razorpay_settlement_path` (default `""`),
    `verification_enabled` (default `True`), `act_mode` (`Literal["stub","live"]`, default `stub`).
    All three routes are empty by default (**never guessed**, ZERO-HALO) but carry documented
    `{subscription_id}` / `{settlement_id}` placeholders for the verification lookups.
  - **Celery/Redis block:** `redis_url` (default `redis://localhost:6379` — a local placeholder,
    **never a real credential**), `reclaim_celery_eager` (default `True`).
  - **Database:** `database_url` (default `sqlite:///reclaim.db`).
  - **Stopping-rule thresholds:** `escalation_amount_threshold` (5000.0),
    `escalation_days_threshold` (7), `max_retries_per_cycle` (3), `cooldown_hours` (24.0),
    `email_cap_per_7d` (1), and the **economic floor** `min_recovery_amount` (100.0 — R7, env
    `MIN_RECOVERY_AMOUNT`).
  - **Stale-lock reconciliation:** `stale_lock_timeout_seconds` (300.0 — how long a case may sit in
    `ACTING` before the sweep reconciles it to `ESCALATED`).
  - **Frontend CORS (Phase 4):** `cors_origins` (default
    `http://localhost:5173,http://127.0.0.1:5173`, env `RECLAIM_CORS_ORIGINS`) — a comma-separated,
    **non-wildcard** allow-list for the React SPA. Applied as `app.add_middleware(CORSMiddleware)` in
    `api.py`; the local-demo default (the Vite dev server) is flagged loudly as not deployment-safe.
  - **Concurrency throttle:** `max_concurrency` (5), `llm_backoff_base_seconds` (1.0),
    `llm_backoff_max_seconds` (15.0).
  - `@field_validator("razorpay_webhook_secret")` → raises `ValueError` if empty (ZERO-HALO).
  - `@field_validator("razorpay_key_id","razorpay_key_secret")` → maps empty strings to `None`.
  - `@property redis_tls` → `True` when URL starts with `rediss://` (Upstash requires TLS).
  - `require_live_credentials()` → raises `RuntimeError` if live mode lacks both keys.
- **`get_settings()`** — `@lru_cache(maxsize=1)` module singleton (secrets loaded once).
- **`clear_settings_cache()`** — drops the cache so tests can reload with different env values.

---

### 4.3 `src/reclaim/models.py` — Pydantic v2 schemas (every boundary)
- **Responsibility:** All domain models, agent I/O, audit and webhook payload shapes. Boundary
  validation everywhere — "no implicit fields, no silently dropped validation."
- **Enums (all `StrEnum`):**
  - `CaseState`: `INGESTED`, `DIAGNOSED`, `DECIDED`, `ACTING`, `RESOLVED`, `ESCALATED`, `FAILED`;
    `is_terminal()` → `True` for RESOLVED/ESCALATED/FAILED.
  - `Cause`: `INSUFFICIENT_FUNDS`, `CARD_EXPIRED`, `BANK_TIMEOUT`, `DO_NOT_HONOR`,
    `MANDATE_REVOKED`, `UNKNOWN`.
  - `Action`: `RETRY_NOW`, `RETRY_SCHEDULED`, `REQUEST_PAYMENT_METHOD_UPDATE`, `ESCALATE_HUMAN`,
    `STOP`.
  - `WebhookType`: `PAYMENT_FAILED` (`payment.failed`), `SUBS_CHARGE_FAILED`
    (`subscription.charged.failed`), `SUBS_PENDING` (`subscription.pending`).
- **`PaymentRecord`** — `status` (min 1 char), `amount` (`>0`), `attempted_at` (datetime). One
  line of customer payment history.
- **`RecoveryCase`** — the persisted state of one recovery workflow.
  - `model_config = ConfigDict(from_attributes=True)`.
  - Fields: `case_id`, `event_id` (globally unique — the dedupe key), `customer_id`,
    `subscription_id`, `failure_reason` (raw bank decline code, e.g. `"R01"`, min 1 char),
    `amount` (INR, `>0`), `attempt_number` (`ge=1`), `customer_tier` (`"standard"`),
    `payment_history` (`list[PaymentRecord]`), `state` (default `INGESTED`), `created_at`
    (default `datetime.now(UTC)`).
  - `days_since_last_attempt(now=None)` → whole days since the most recent failed attempt
    (maxes the payment history `attempted_at`, falls back to `created_at`).
- **`DiagnoseInput`** — `decline_code` (min 1 char), `payment_history`.
- **`DiagnoseOutput`** — `cause: Cause`, `confidence: float` (clamped to `[0,1]` and rounded to 4
  dp by `_clamp_confidence`), `reasoning` (min 1 char).
- **`DecideInput`** — `cause`, `attempt_number` (`ge=1`), `days_since_last_attempt` (`ge=0`),
  `amount` (`>0`), `customer_tier`.
- **`DecideOutput`** — `action: Action`, `scheduled_at: datetime | None`, `reasoning`.
  - Cross-field `@model_validator(mode="after")` `_validate_cross_field_rule`:
    - `retry_scheduled` **requires** a `scheduled_at` datetime.
    - `scheduled_at` is **only** allowed for `retry_scheduled`.
    - `scheduled_at` must be in the **future** (naive datetimes are assumed UTC).
  - (Documented: enforced with a `model_validator`, not a `field_validator`, because Pydantic
    skips field validators when a field uses its default value — `scheduled_at=None`.)
- **`AuditLogEntry`** — one immutable row in the append-only trail: `case_id`, `stage`
  (`"ingest"`, `"diagnose"`, `"decide"`, `"act"`, `"manual_override"`, `"sweep"`, `"verify_*"`…),
  `agent_reasoning`, `input_state` (dict — may carry `llm_provenance`), `decision`, `action_taken`,
  `outcome`, `fallback_triggered` (bool), `timestamp`.
  - `fallback_triggered` means **one** thing: the LLM call itself failed (not a rule override, not a
    triaged adversarial input, not a stub-mode action).
- **`WebhookEvent`** — parsed Razorpay webhook payload (pre-signature): `event_id` (min 1),
  `type: WebhookType`, `payload` (dict).
  - Helper methods, all best-effort and never fabricating ids: `subscription_id()`, `case_id()`
    (=`subscription_id()`), `customer_id()` (default `"unknown"`), `amount()` (converts paise →
    INR: divides by 100), `failure_reason()` (from `error_code` / `reason` / `"unknown"`).
- **`new_event_id()`** → `f"evt_{uuid.uuid4().hex}"`.

---

### 4.4 `src/reclaim/state_machine.py` — Guarded lifecycle state machine
- **Responsibility:** Transitions are the **only** way stage progress is recorded. Any edge not in
  the table is illegal and raises.
- **`TRANSITION_TABLE: dict[CaseState | None, set[CaseState]]`** (`None` = pre-creation):
  ```python
  None      -> {INGESTED}
  INGESTED  -> {DIAGNOSED}
  DIAGNOSED -> {DECIDED}
  DECIDED   -> {ACTING, RESOLVED}
  ACTING    -> {RESOLVED, ESCALATED, FAILED}
  ESCALATED -> {ACTING, RESOLVED}     # manual (human) overrides ONLY — see below
  ```
  - `DECIDED -> RESOLVED` sits in the table but is **additionally gated** by `via_stop` so
    "resolved by a deliberate halt" is distinguishable from "resolved by a successful recovery."
  - `ESCALATED` is reported terminal for metrics, but a deliberate, audited **manual override** may
    re-open it (`ESCALATED -> ACTING` for an approved retry, or `ESCALATED -> RESOLVED` for a
    human close). These edges are only reachable through the `manual=True` helpers below — **never**
    by the agentic pipeline.
- **`IllegalTransitionError(Exception)`** — raised on any forbidden edge.
- **`TransitionListener = Callable[[CaseState | None, str, CaseState], None]**` — injection point
  for the audit-log writer.
- **`validate_transition(current, target, *, via_stop=False, action=None)`** — pure validation
  (no mutation); enforces the table and the DECIDED→RESOLVED stop guard.
- **`is_terminal(state)`** → `state.is_terminal()`.
- **`CaseStateMachine`** — carries a case through its lifecycle, firing the listener on each move.
  - Constructor: `initial: CaseState | None = None`, `on_transition: TransitionListener | None`.
  - Lifecycle helpers (each calls `_move`): `ingest()` (trigger
    `webhook.verified.non_duplicate`), `diagnose()` (`diagnose.completed`), `decide()`
    (`decide.completed`), `start_acting()` (`act.started`), `resolve_as_stopped()`
    (`decision.stop`, via_stop=True, requires current==DECIDED), `resolve()` (`act.succeeded`,
    from ACTING), `escalate()` (`act.escalated`), `fail()` (`act.failed`).
  - **Manual helpers** (the only ways out of terminal ESCALATED, each `manual=True` and never
    called by the pipeline): `approve_retry()` (`ESCALATED->ACTING`, trigger `manual.approve_retry`),
    `resolve_human()` (`ESCALATED->RESOLVED`, trigger `manual.resolve_human`).
  - `_move(target, *, trigger, via_stop=False, action=None, manual=False)` — validates, rejects
    moving out of a terminal state **unless** `manual=True`, updates `self.current`, fires the
    listener, logs `STATE_TRANSITION`.
- **`run_decision_flow(machine, decision)`** — routing helper: must be at `DECIDED`; `STOP` →
    `resolve_as_stopped()` (no side effect); everything else → `start_acting()`.

---

### 4.5 `src/reclaim/stopping_rules.py` — "LLM proposes, code disposes" (policy-as-code, R1–R7)
- **Responsibility:** Deterministic, **pure** (no I/O, no DB), post-hoc validation layer,
  independent of the LLM prompt. Rules are expressed **declaratively** so the policy itself is an
  auditable, introspectable artifact rendered as plain language on `/rules`. First matching rule
  (by priority) wins.
- **`RETRY_ACTIONS = frozenset({RETRY_NOW, RETRY_SCHEDULED})`**.
- **`@dataclass(frozen=True) RuleContext`** — `input_: DecideInput`, `proposed: DecideOutput`,
  `settings: Settings`, `payment_method_update_count: int`.
- **`@dataclass(frozen=True) RuleSpec`** — one declarative rule: `rule_id` (e.g. `"R1"`),
  `priority` (lower = higher), `description` (plain-language policy statement with `{threshold}`
  placeholders), `condition: Callable[[RuleContext], bool]` (pure predicate), `action: Action`
  (forced when it fires), `reason: Callable[[RuleContext], str]`.
- **`@dataclass(frozen=True) RuleOutcome`** — `decision: DecideOutput`, `overridden: bool`,
  `override_reason: str`, `rule: str` (machine id like `"R4"`, `""` when not overridden).
- **`cooldown_elapsed(days_since_last_attempt, cooldown_hours)`** → `days*24 >= cooldown_hours`.
- **`_rebuild(...)`** — rebuilds a `DecideOutput` carrying the clamps' reasoning, then
  re-model-validates so schema guarantees (`scheduled_at` rules) hold; sets a default future
  `scheduled_at` for scheduled retries.
- **`_schedule_future(at)`** — coerces naive→UTC and ensures a future timestamp.
- **`rule_default_schedule_hours()`** → `24.0` (default lead time for forced scheduled retries).
- **`STOPPING_RULES: tuple[RuleSpec, ...]`** — the declarative registry, in priority order:
  | Rule | Priority | Condition | Enforced action |
  |------|----------|-----------|-----------------|
  | **R1** | 1 | cause == `mandate_revoked` | `escalate_human` (retries disallowed) |
  | **R7** | 2 | `amount < min_recovery_amount` (**economic floor**, ₹100) | `stop` (no auto-retry) |
  | **R2** | 3 | `amount > escalation_amount_threshold` | `escalate_human` |
  | **R3** | 4 | `days_since_last_attempt > escalation_days_threshold` | `escalate_human` |
  | **R4** | 5 | retry proposed and `attempt_number > max_retries_per_cycle` | `stop` |
  | **R5** | 6 | `request_payment_method_update` and `payment_method_update_count >= email_cap_per_7d` | `escalate_human` |
  | **R6** | 7 | `retry_now` proposed but cooldown not elapsed | `retry_scheduled` (never retry_now) |
  - **R7 (economic floor)** is the newest rule — a deliberate product decision: for a trivially
    small amount (default < ₹100), the cost/risk of a retry outweighs the recovery value, so the
    case is `STOP`ped. Placed second in priority, below R1, so a revoked mandate is *always*
    escalated to a human regardless of amount.
  - `payment_method_update_count` is passed in by the caller (derived from executed actions);
    default 0 makes R5 inert unless a caller counts a hit.
- **`describe_rules(settings)`** — renders every active rule in plain language with **live**
  threshold values filled in (for the `/rules` page): a list of `{rule_id, priority, action,
  description}` dicts.
- **`enforce(input_, proposed, settings, *, payment_method_update_count=0)`** — walks
  `STOPPING_RULES` in priority order and applies the first matching rule (or leaves the proposal
  standing, re-validating its `scheduled_at`). Field order of the R7 condition makes a
  below-floor amount `STOP` regardless of what the LLM proposed.

---

### 4.6 `src/reclaim/llm_client.py` — LLM client (offline shim + online Ollama) + adversarial triage
- **Responsibility:** Both the offline shim and the online client share one contract:
  `call -> parse+validate -> on ANY failure: retry ONCE with the validation error appended ->
  if still failing: deterministic fallback, fallback_triggered=True`. This is the only way LLM
  failures are handled — no silent guesses.
- **Adversarial-input triage (`triage_diagnose_input`, `TriageResult`):** the `decline_code` field
  is free text that flows into the Diagnose prompt, so it is triaged **before** the LLM is
  consulted. `_INJECTION_MARKERS` (e.g. `"ignore previous"`, `"system:"`, `"<|im_start"`,
  `"### instruction"`, `"do not follow"`, `"disregard"`…) plus a control-character regex, a
  JSON/structure-noise regex, and a 40-char length cap short-circuit hostile input to a
  deterministic `UNKNOWN` fallback. This is deliberately **not** counted as an LLM failure (it is
  a policy guard, not a crash), keeping the three-way metrics honest.
- **Provenance (`_provenance(settings, kind, source)`):** deterministic per-call identity — a dict
  of `model`, `prompt_version` (`"{kind}-v1"`), `prompt_hash` (SHA-256 of
  `"{model}|{kind}|{serialized_prompt}"`), and `mode`. Attached to every Diagnose/Decide call for
  drift detection and reproducibility.
- **`offline_diagnose(input_)`** — deterministic rule table keyed on the raw decline code
  (e.g. `R01`/`R02` → `INSUFFICIENT_FUNDS` @ 0.95; `54`/`F14` → `CARD_EXPIRED` @ 0.97;
  `91`/`Z06` → `BANK_TIMEOUT` @ 0.9; `05`/`N7` → `DO_NOT_HONOR` @ 0.92; `R0`/`PM` →
  `MANDATE_REVOKED` @ 0.98; `255`/`C6` → `UNKNOWN` @ 0.5; anything else → `UNKNOWN` @ 0.2).
- **`offline_decide(input_)`** — a **deliberately naive proposer**. Deterministic pseudo-variety
  derived from the input (no RNG state); almost always proposes `retry_now` so the stopping-rule
  layer's overrides are visible and exercised. Occasionally proposes `retry_scheduled` (now+48h) or
  `request_payment_method_update`.
- **`class LLMClient`** — OpenAI-compatible client pointed at local Ollama (`base_url =
  {ollama_base_url}/v1`, `api_key="ollama"` — ignored by Ollama, required by SDK).
  - `diagnose(input_)` / `decide(input_)` — in offline mode delegate to the shims; in online mode
    build a structured system/user prompt and call `_call_with_json_mode` with
    `response_format={"type":"json_object"}` and `temperature=0.0`.
  - `_call_with_json_mode(model, system, user)` — one retry-on-validation-failure: on attempt 1
    failure appends the validation error and a fresh `user` turn; on attempt 2 failure **raises**
    so the wrapper applies the fallback.
- **`FallbackResult[T]`** — `output: T`, `fallback_triggered: bool`, `provenance: dict | None`.
- **`class LLMWrapper`** — convenience wrapper with fallback + triage + provenance built in:
  - `diagnose(input_)` → runs triage first; if triaged, returns the deterministic `UNKNOWN`
    fallback with `fallback_triggered=False` (and provenance). On any exception falls back to
    `DiagnoseOutput(cause=UNKNOWN, confidence=0.0, reasoning="fallback: <ExType>")` with
    `fallback_triggered=True`.
  - `decide(input_)` → on any exception falls back to **`escalate_human`** (never guess at a
    money-moving action) with `fallback_triggered=True`.

---

### 4.7 `src/reclaim/webhook.py` — Razorpay webhook boundary
- **Responsibility:** Signature verification, schema parsing, and race-safe dedupe.
- **Headers:** `SIGNATURE_HEADER = "X-Razorpay-Signature"`, `EVENT_ID_HEADER =
  "X-Razorpay-Event-Id"`.
- **`RazorpayWebhookException(Exception)`** — rejected at the boundary.
- **`compute_signature(secret, raw_body)`** — HMAC-SHA256 hex digest of the **raw** body.
- **`verify_signature(secret, raw_body, signature)`** — constant-time `hmac.compare_digest`;
  missing header → `False` (hard reject); empty secret → raises; malformed → `False`.
- **`parse_event(raw_body, event_id_hint=None)`** — decode JSON, require a dict, derive an event
  id (`event_id_hint` or `_deterministic_event_id`), then `WebhookEvent.model_validate`. Any
  schema issue raises `RazorpayWebhookException`.
- **`_deterministic_event_id(raw_body, data)`** — SHA-256 of canonical
  `{entity_id, event, created_at}` (first 24 hex chars, `evt_` prefix) so identical bodies dedupe
  even without the event-id header.
- **`event_to_case(event)`** — maps a validated event to `RecoveryCase`; derives `attempt_number`
  from entity (`attempt_number`/`attempts`, min 1); anchors payment history on the failed
  attempt's `created_at` timestamp (so the 24h cooldown reflects the real retry gap).
- **`_payment_history_to_json` / `_payment_history_from_json`** — SQLite JSON columns can't store
  datetimes, so they serialize to ISO strings and back.
- **`_row_to_case(row)`** — rebuilds a `RecoveryCase` view from a persisted row (read path).
- **`ingest_event(db, event, settings)`** — persists a verified event as a new
  `RecoveryCaseRow` (state `INGESTED`); returns `(case, is_new, row_pk)`.
  - **Fast path:** if `event_id` already exists → return the existing case with `is_new=False`.
  - **Race-safe dedupe:** insert-then-catch — on `IntegrityError`, rolls back and re-reads the
    existing row (never re-triggers stages). Idempotency is a DB constraint, not a check-then-insert.

---

### 4.8 `src/reclaim/db.py` — SQLAlchemy 2 persistence layer (WAL-enabled)
- **Responsibility:** Postgres-compatible schema; SQLAlchemy 2.0 with SQLite as the dev engine.
- **`Base(DeclarativeBase)`** — all ORM tables inherit from it.
- **`RecoveryCaseRow`** (`__tablename__ = "recovery_cases"`):
  - `id` PK, `case_id` (String 255, **unique + index**), `event_id` (String 255, **unique** —
    the dedupe key), `customer_id` (255), `subscription_id` (255, index), `failure_reason` (512),
    `failure_reason_raw` (512), `amount` (Float), `attempt_number` (Integer), `customer_tier`
    (32), `payment_history` (JSON, default list), `state` (32, index), `created_at`
    (DateTime timezone), `last_attempt_at` (DateTime, nullable).
- **`AuditLogRow`** (`__tablename__ = "audit_log"`) — **append-only** by contract, **hash-chained
  for tamper-detection** (Phase 5):
  - `id` PK, `case_id` (255, index), `stage` (64), `agent_reasoning` (4096), `input_state` (JSON),
    `decision` (255), `action_taken` (255, nullable), `outcome` (255), `fallback_triggered`
    (Boolean), `timestamp` (DateTime timezone), **`prev_hash`** (String 64, default `""` — the
    previous entry's SHA-256 hash), **`entry_hash`** (String 64, default `""` — this entry's hash).
    The hash columns are derived **post-write** by `audit.finalize_audit_chain` in one sequential
    pass (never at write time — see `audit.py`); an unfinalized log (empty hashes) fails
    `verify_audit_chain`.
- **`ExecutedActionRow`** (`__tablename__ = "executed_actions"`) — the **idempotency ledger**:
  - `id` PK, `case_id` (255, index), `attempt_number` (Integer), `action` (64),
    `idempotency_key` (255, **unique**), `executed_at` (DateTime timezone).
  - `__table_args__` includes `UniqueConstraint("case_id","attempt_number","action",
    name="uq_executed_action")` — the hard guarantee that a duplicated Act call can never
    double-charge.
- **`_set_sqlite_wal(dbapi_connection, connection_record)`** — connect event listener that runs
  `PRAGMA journal_mode=WAL`, `PRAGMA synchronous=NORMAL`, and `PRAGMA busy_timeout=30000` on
  **file-backed** SQLite (in-memory SQLite cannot use WAL).
- **`build_engine(database_url)`** — sets `check_same_thread=False` for SQLite (shared sessions
  across FastAPI threads / Celery workers in dev), `pool_pre_ping=True`, a SQLite busy `timeout`
  for file URLs, and attaches the WAL connect listener for non-memory file URLs.
- **`init_schema(engine)`** — `Base.metadata.create_all(engine)` (idempotent).
- **`class Database`** — thin wrapper owning one engine + session factory
  (`sessionmaker(bind=engine, expire_on_commit=False)`); `create_session()`, `close()`,
  `build_engine`.
- **`get_db()`** — process-wide default `Database` (lazily initialized, cached in `_default_db`).
- **`reset_db_for_tests(db=None)`** — drops the cached default instance so tests can point at a
  fresh file; used by the conftest fixture.
- **`utcnow()`** — `datetime.now(UTC)`.
- **`row_to_dict(row)`** — serializes an ORM row to a plain single-level dict.

---

### 4.9 `src/reclaim/act.py` — Act layer (bounded, idempotent execution)
- **Responsibility:** Execute exactly one decided action. Every external side effect is:
  - **IDEMPOTENT** — the `ExecutedActionRow` ledger (UNIQUE on `(case_id, attempt_number, action)`
    + unique `idempotency_key`) is **claimed before** the effect; a re-run is a logged no-op.
  - **FAULT-ISOLATED** — every external call is wrapped in try/except; a transport failure never
    crashes the case, it resolves to FAILED after logging.
  - **GATED** — the state machine must already be at ACTING (pipeline calls `start_acting` first).
- **Terminal mapping (from ACTING):** `retry_now` → RESOLVED (recovered) or FAILED;
  `retry_scheduled` → RESOLVED (cycle done, money pending); `request_payment_method_update` →
  RESOLVED (request sent); `escalate_human` → ESCALATED.
- **`@dataclass(frozen=True) ActResult`** — `terminal_state`, `action_taken`, `outcome`,
  `idempotent_duplicate` (default False), `amount_recovered` (default 0.0).
- **`_claim(db, case, action)`** — idempotency claim returning `(idempotency_key, acquired)`.
  Builds an `idempotency_key(case_id, attempt_number, action)`, inserts row, commits; on
  `IntegrityError` rolls back and returns `acquired=False` (the duplicate guard).
- **`_retry_now(db, case, settings)`** — calls `RazorpayClient.retry_payment`; fault-isolated,
  returns success bool.
- **`schedule_retry(db, case, decision, settings)`** — in eager mode records the eta and returns
  (no async fire); in broker mode enqueues `reclaim.tasks.retry_payment_task` with
  `eta=decision.scheduled_at` and an idempotent `task_id(case_id, attempt_number)`.
- **`execute_action(db, case, decision, settings=None)`** — the main entry:
  - `STOP` → raises `RuntimeError` (handled by the pipeline, never reaches Act).
  - Claims the ledger; if not acquired → returns a duplicate result (`idempotent_duplicate=True`).
  - Dispatches to `_retry_now` / `schedule_retry` (RETRY_SCHEDULED) / `send_email_message`
    (REQUEST_PAYMENT_METHOD_UPDATE and ESCALATE_HUMAN), mapping outcomes to terminal states and
    recording recovered amounts.
  - Any unexpected exception → `FAILED` with `action_error:<Type>`.
- **`_dedup_terminal(action)`** — a re-executed escalate reports ESCALATED; everything else
  reports RESOLVED-but-skipped (keeps audit truthful about duplicates).

---

### 4.10 `src/reclaim/manual.py` — Human-in-the-loop override actions (control plane)
- **Responsibility:** Deliberate **operator** decisions — the only way any case leaves the terminal
  ESCALATED state. Built Phase 2 as a differentiator (comparable submissions describe read-only /
  trigger-only UIs with no override capability).
- Each action: verifies the case state via the **same** state-machine guard
  (`CaseStateMachine`), writes an audit entry tagged `stage="manual_override"` (so the trail
  clearly distinguishes a human act from an agent decision), and preserves the append-only
  contract.
- **`approve_manual_retry(db, case_id, settings=None)`** — legal only when `ESCALATED`.
  `ESCALATED -> ACTING` (`machine.approve_retry()`), persists, writes a `manual_override` entry,
  executes the retry **through the idempotent Act layer** (`execute_action`), then lands on
  `RESOLVED` or `FAILED` (a duplicate claim is a no-op). Returns the terminal outcome.
- **`resolve_human(db, case_id, settings=None)`** — legal only when `ESCALATED`.
  `ESCALATED -> RESOLVED` directly (`machine.resolve_human()`); no payment action — records that a
  human closed the case. Returns `"resolved"`.
- Both raise `KeyError` on an unknown case and `IllegalTransitionError` on a non-ESCALATED state.

---

### 4.11 `src/reclaim/razorpay_client.py` — Razorpay test-mode client (retry + verification reads)
- **Responsibility:** Stub or live Razorpay calls, always with idempotency keys — plus
  **verification-only** reads (Subscriptions + Settlements) that never block or reverse an action.
- **`IDEMPOTENCY_HEADER = "X-Razorpay-Idempotency-Key"`**.
- **`idempotency_key(case_id, attempt_number, action)`** → `f"reclaim:{case_id}:{attempt_number}:{action}"`
  (deterministic; same tuple → same key).
- **`class RazorpayClient`**:
  - `_require_live()` — calls `settings.require_live_credentials()` and requires a non-empty
    `razorpay_retry_path` (ZERO-HALO: refuse rather than guess the API shape).
  - `subscription_status(subscription_id)` — **verification-only read** of a subscription's current
    status (e.g. whether the mandate is revoked). Stub mode returns a deterministic placeholder;
    live mode GETs the configured Subscriptions endpoint (path from config, `{subscription_id}`
    substituted) with `httpx.get(auth=...)`. Raises on failure so `verify.py` fault-isolates it.
  - `settlement_reconciliation(settlement_id)` — **verification-only read** of settlement state
    after a retry recovery, to corroborate that recovered money actually settled. Stub mode returns
    a deterministic placeholder; live mode GETs the configured Settlement endpoint
    (`{settlement_id}` substituted). Raises on failure.
  - `retry_payment(*, case_id, subscription_id, amount, attempt_number)`:
    - **STUB mode** (default): logs the would-be call and returns `True` (no credentials).
    - **LIVE mode**: `httpx.post` to `{razorpay_base_url}{razorpay_retry_path}` with
      `auth=(key_id, key_secret)`, the idempotency-key header, and
      `{"subscription_id": subscription_id}` body; `raise_for_status` on error; returns `True` on
      success. (Body shape is intentionally deferred to the configured endpoint's contract — never
      guessed.)

---

### 4.12 `src/reclaim/verify.py` — Verification-only Subscriptions + Settlements reconciliation
- **Responsibility:** Best-effort, NON-BLOCKING, VERIFICATION-ONLY lookups against real Razorpay
  test-mode endpoints. They **never** block or reverse an action and **never** change a case's
  terminal state — a verification failure merely records a `verify` audit entry so an auditor can
  see the external state (subscription status / settlement status) at the time of the action. Every
  external call is **fault-isolated**: it can never crash the money flow it is observing.
- Built Phase 3 (previously scoped as A2/A3) — a genuine differentiator, since comparable public
  submissions in this track use mock/stub executors only.
- **`_record(db, case_id, stage, detail, outcome)`** — appends one best-effort verification audit
  entry (`stage="verify_subscription"` / `"verify_settlement"`).
- **`verify_subscription_status(db, settings, case_id)`** — if `verification_enabled` is False,
  returns `None` (silent). Otherwise looks up the case, calls `RazorpayClient.subscription_status`,
  records `VERIFY subscription=<status>` (or `VERIFY_FAILED <ExType>`), returns the raw dict or
  `None`. Never raises.
- **`verify_settlement_reconciliation(db, settings, case_id)`** — same pattern for settlements
  after a `retry_now` recovery (settlement id derived as
  `settle_{subscription_id}_{attempt_number}`). Never raises.

---

### 4.13 `src/reclaim/email.py` — Email stub
- **Responsibility:** Log the would-be transactional email so a real provider can be wired later in
  one place. Never raises — email is best-effort, failures are logged, not fatal.
- **`send_email_message(*, to, template, context, settings=None)`** — logs
  `EMAIL_STUB to=<to> sub=[<template>] ... context=...`. This is the **single seam** for
  outbound email (SendGrid/Postmark/Resend would be injected here).

---

### 4.14 `src/reclaim/dispatcher.py` — Webhook → pipeline handoff
- **Responsibility:** Thin; a fresh case is dispatched **exactly once** (never for duplicates).
- **`submit_case(case_id, event_id, settings=None)`**:
  - Eager mode: lazily imports and runs `pipeline.run_case(case_id)` synchronously in-process.
  - Broker mode: `app.send_task("reclaim.pipeline.run_case_task", args=[case_id],
    task_id=f"reclaim-{case_id}-ingest")`.
  - The pipeline module is imported lazily so the webhook boundary loads independently of it.

---

### 4.15 `src/reclaim/celery_app.py` — Celery app + broker config (periodic beat)
- **Responsibility:** Celery application with Upstash Redis over TLS.
- **`build_app(settings=None)`**:
  - `Celery("reclaim", broker=redis_url, backend=redis_url)`.
  - `conf.update`: JSON serializers, `timezone="UTC"`, `enable_utc=True`,
    `broker_connection_retry_on_startup=True`, `task_always_eager=reclaim_celery_eager`,
    `task_eager_propagates=True`, `task_acks_late=True`, `worker_prefetch_multiplier=1`, TLS
    opts (`broker_use_ssl` / `redis_backend_use_ssl`) when `redis_url` is `rediss://`, and a
    **`beat_schedule`**:
    ```python
    "reclaim-sweep-stale-acting": {
        "task": "reclaim.tasks.sweep_stale_acting_task",
        "schedule": crontab(minute="*/5"),
    }
    ```
    — the stale-lock reconciliation runs every 5 minutes so a case wedged in `ACTING` is recovered
    even if the worker that owned it died.
- **`_tls_opts(settings)`** — returns an explicit SSL dict (CERT_NONE via `None`).
- **`app: CeleryApp = build_app()`** — module-level app imported by tasks and the FastAPI lifespan.
- **`task_id(case_id, attempt_number)`** → `f"reclaim-{case_id}-{attempt_number}"` (deterministic,
  so scheduled retries are themselves idempotent).

---

### 4.16 `src/reclaim/tasks.py` — Celery task entrypoints
- **Responsibility:** Named task bodies that the dispatcher and Act layer enqueue **by string**.
- **`run_case_task(case_id)`** (name `reclaim.pipeline.run_case_task`) — runs
  `pipeline.run_case(case_id)` and returns a summary dict.
- **`sweep_stale_acting_task()`** (name `reclaim.tasks.sweep_stale_acting_task`) — the periodic
  reconciliation body invoked by the beat schedule every 5 minutes: resolves stale `ACTING` cases
  via `reconcile_stale_acting`. Idempotent — re-running is a no-op once they are swept. Returns
  `{"swept_cases": [...] , "count": N}`.
- **`retry_payment_task(case_id, attempt_number)`** (name `reclaim.tasks.retry_payment_task`) —
  executes a deferred retry for a scheduled case; builds a `RETRY_NOW` `DecideOutput` and runs
  `execute_action` (idempotent via the ledger, so duplicates are logged no-ops). Returns a summary
  including `idempotent_duplicate`.

---

### 4.17 `src/reclaim/sweep.py` — Stale-lock reconciliation (mid-pipeline crash recovery)
- **Responsibility:** If the process dies between DECIDED and a completed ACT, a case can be left in
  `ACTING` forever (the next webhook for that subscription never fires, and the state machine is the
  only source of progress). This module is the safety net: it finds cases stuck in `ACTING` past
  `STALE_LOCK_TIMEOUT_SECONDS` and safely reconciles them to `ESCALATED` (human review).
- **Safety properties:** never runs a side-effecting action (escalation is a pure state change that
  hands the outcome to a human); consults the **same** state machine + append-only audit trail; a
  legitimately in-progress case (younger than the timeout) is left untouched.
- **`_as_utc(dt)`** — coerces possibly-naive datetimes (SQLite strips tzinfo on read) to aware UTC.
- **`_entered_acting_at(db, case_id)`** — timestamp of the most recent `act` audit entry whose
  outcome contains `"ACTING"` (the anchor for the timeout).
- **`find_stale_acting(db, settings, now=None)`** — all case rows stuck in `ACTING` longer than the
  timeout (`now` injectable for deterministic tests).
- **`reconcile_stale_acting(db, settings, now=None)`** — for each stale case: `escalate()` via the
  state machine, persist `ESCALATED`, append a `stage="sweep"` audit entry (outcome
  `"ESCALATED/sweep_stale_lock"`), return the swept case ids. Never touches in-progress cases.

---

### 4.18 `src/reclaim/audit.py` — Append-only audit writer (+ Phase 5 hash-chain finalize)
- **Responsibility:** One immutable row per stage transition. Insert-only by contract — never
  updates or deletes. **Phase 5 makes the log tamper-evident** by deriving a SHA-256 hash chain
  across every row (see `audit_chain.py` / `verify_audit_chain.py`).
- **`write_audit(db, entry)`** — persists an `AuditLogRow`. **Never raises**: an audit-write
  failure must not crash the money flow it documents, so it's logged and swallowed (the case
  proceeds regardless). **Deliberately does not compute hash linkage at write time** — computing
  "the most recent row by id" then inserting is a read-then-write sequence that races under the
  batch's concurrent writers (two writes can both read the same stale latest row → a forked chain).
  Instead, chain columns are left at their defaults and derived post-hoc.
- **`finalize_audit_chain(db)`** → `int` — recomputes the full hash chain in **one sequential pass**
  over rows ordered by autoincrement `id`, deriving each row's `prev_hash` / `entry_hash` from the
  *already-committed* predecessor. Single-writer by construction: call it once **after** the
  concurrent write phase (e.g. after `run_batch` in `batch.py` / `baseline.py`), never from the
  concurrent writers. Idempotent and deterministic — rerunning over unchanged content yields the
  same hashes, so later content tampering is still caught. Returns the number of rows chained.

---

### 4.19 `src/reclaim/repo.py` — Read/update helpers
- **Responsibility:** Keeps the pipeline/Act/metrics/sweep/manual layers from reaching into SQL
  directly.
- **`row_to_case(row)`** — public alias for the webhook `_row_to_case` mapping.
- **`PM_UPDATE_ACTION = "request_payment_method_update"`**.
- **`get_case_row(db, case_id)`** → `RecoveryCaseRow | None`.
- **`all_case_rows(db)`** — ordered list of all case rows.
- **`list_cases(db, *, state=None, limit=50, offset=0)`** — (Phase 4) query-layer filtered +
  paginated listing for `GET /api/v1/cases`. Filtering by exact ``state`` and the `limit`/`offset`
  window happen **in SQL** (not a Python slice of the full set), ordered by id, so the endpoint
  stays bounded and pagination is stable as the dataset grows.
- **`set_case_state(db, case_id, state)`** — persists the authoritative state-machine position;
  raises `KeyError` if the row is missing.
- **`audit_trail(db, case_id)`** → full decision trail (oldest first) as `AuditLogEntry` list.
- **`count_recent_payment_method_updates(db, customer_id, within_hours)`** — joins
  `ExecutedActionRow` → `RecoveryCaseRow` on `case_id` to count payment-method-update emails sent
  to a customer within a window (per-customer, across cases). Used to enforce the R5 email cap.

---

### 4.20 `src/reclaim/pipeline.py` — Pipeline orchestrator
- **Responsibility:** `run_case` (Diagnose → Decide → enforce → route → Act → Log) and `run_batch`
  under a concurrency cap + exponential backoff. The state machine is the **only** source of stage
  progress; every transition is persisted to the case row and appended to the audit log.
- **`@dataclass CaseOutcome`** — `case_id`, `terminal_state`, `cause`, `action`, `action_taken`,
  `amount_recovered`, `llm_failure` (diagnose/decide LLM call failed), `stopping_rule_override`
  (R1–R7 overrode a valid proposal), `skipped` (default False).
- **`class ConcurrencyLimiter`** — bounded semaphore around a callable, tracking peak concurrency
  (`max_workers >= 1` required); `peak` is measurable for tests/demo. **The semaphore is created
  once in `__init__` and shared across all calls** (an earlier per-call bug — a fresh semaphore per
  invocation — meant the cap never capped; see README "What Broke"). `run(fn, *args, **kwargs)`.
- **`call_with_backoff(fn, settings)`** — in online mode, exponential backoff
  (base * 2^n, capped) on transient LLM failures; in offline mode it's a no-op passthrough.
- **`run_case(case_id, *, settings=None, db=None)`** — drives one case end-to-end:
  1. Fetches the case; if terminal → returns a `skipped=True` outcome (idempotent rerun).
  2. **Ingest → Diagnose:** `machine.diagnose()`, persist `DIAGNOSED`, build `DiagnoseInput`,
     call `call_with_backoff(wrapper.diagnose)` (which runs adversarial triage first), write audit
     (`fallback_triggered` = LLM failure only) and record the LLM provenance.
  3. **Decide:** `machine.decide()`, persist `DECIDED`, build `DecideInput`, call
     `call_with_backoff(wrapper.decide)`, derive `email_count` via
     `repo.count_recent_payment_method_updates`, run `enforce(...)`, write audit with provenance
     and `outcome="DECIDED rule=<R#> [OVERRIDE]"` (`fallback_triggered` = LLM failure only).
  4. Tracks `llm_failed = di.fallback_triggered or de.fallback_triggered` and
     `rule_overrode = rule.overridden`.
  5. **STOP** → `resolve_as_stopped()`, persist `RESOLVED`, audit `outcome="STOPPED"`, return.
  6. Otherwise → `start_acting()`, persist `ACTING`, audit, call `execute_action`, map to
     terminal state (`resolve`/`escalate`/`fail`), persist, audit final outcome, return `CaseOutcome`.
- **`run_batch(case_ids, *, settings=None, db=None, max_concurrency=None, limiter=None)`** — runs
  cases in parallel via `ThreadPoolExecutor` capped at `max_concurrency`; each case is isolated
  (a failure becomes a `FAILED` outcome, never kills the batch).

---

### 4.21 `src/reclaim/metrics.py` — Batch metrics (three non-conflated counters)
- **Responsibility:** Computes batch-level metrics from the audit trail + case states. No guesses.
- **`_EXECUTED_ACTIONS`** — `{retry_now, retry_scheduled, request_payment_method_update,
  escalate_human}` required for a stub action to count (excludes `STOP` — no side effect).
- **`compute_metrics(db, settings)`** — one scan over all cases + their audit trails.
  **Phase 5 (precision principle):** the function's docstring and every field carry explicit
  "what this proves vs. what needs external confirmation" guidance — e.g. `recovered_amount` proves
  a successful gateway call was made and logged, **not** that settlement confirmed (that
  corroboration comes from the opt-in `verify.py` settlement lookup); the three counters are
  documented as availability vs. safety vs. environment. Returns:
  - `total_cases`, `state_distribution`, `amount_at_risk`, `recovered_cases`,
    `recovered_amount`, `recovery_rate` (fraction), `cause_breakdown`.
  - **Three non-conflated counters:**
    - `llm_call_failures` (+ `llm_failure_cases`) — any diagnose/decide audit entry with
      `fallback_triggered=True`.
    - `stopping_rule_overrides` (+ `stopping_rule_overrides_by_rule`, `rule_override_cases`) —
      decide entries whose outcome contains `"OVERRIDE"`; parses the `rule=<R#>` token.
    - `stub_mode_actions` (+ `stub_mode_cases`) — in stub mode, act entries whose `action_taken`
      is in `_EXECUTED_ACTIONS` (deliberate-halt STOP excluded).
  - `cases_resolved_without_retry` = `stopped_cases + escalated_cases` (explicitly defined),
    plus `stopped_cases`, `escalated_cases`.
- **`_last_act_outcome(db, case_id, needle)`** — scans the trail in reverse for an act outcome
  containing `needle` (e.g. `"retry_succeeded"`).

---

### 4.22 `src/reclaim/synthetic.py` — Synthetic webhook batch generator
- **Responsibility:** Produces a large, varied, **seeded** batch for the demo + tests:
  60 valid unique events (all `Cause` values, spread of amounts incl. above-threshold, attempts up
  to 4, histories with gaps under and over the 7-day window) + 6 duplicate deliveries + 7
  rejections. All derived from a seeded `random.Random`, so `(count, seed)` reproduces an identical
  batch.
- **Code-map constants:** `RAW_CODE_TO_CAUSE` (raw bank code → cause), `CAUSE_TO_RAW_CODES` (cause
  → code list), `CAUSE_TO_DESCRIPTION` (cause → human description string).
- **`@dataclass CaseEnrichment`** — CRM-style context: `customer_tier`, `payment_history`;
  `days_since_last_attempt()`.
- **`@dataclass SyntheticWebhook`** — one raw delivery/rejection: `event_id`, `raw_body`,
  `signature`, `event` (None when body doesn't parse), `valid_delivery`, `cause_hint`, `note`.
- **`@dataclass SyntheticBatch`** — `webhooks` + `enrichments` (keyed by subscription_id);
  `valid_deliveries()`, `unique_valid()` (dedupes by event_id), `summary()`.
- **`generate_batch(*, n_valid=60, n_duplicates=6, n_rejections=7, seed=42,
  webhook_secret="demo-secret")`** — generates deliveries with varied amounts (`~1/9` above
  threshold), attempts (`~1/11` exhausted), gaps (`~1/8` over the 7-day window), tiers (weighted
  standard/silver/gold), payment history; plus below-economic-floor amounts to exercise R7. Builds
  rejections: `malformed-json`, `missing-event-type`, `unmappable-amount`, `tampered-body` (flipped
  last char → signature mismatch), `missing-signature`, and random `empty-body` extras. Ends with a
  safety invariant `assert` on valid counts.
- **`render_delivery(w)`** — serializes a delivery exactly as the HTTP boundary sends it
  (`headers` + `body`).

---

### 4.23 `src/reclaim/batch.py` — Batch-run CLI entrypoint
- **Responsibility:** `python -m reclaim.batch` — ingests the synthetic batch through the **real**
  webhook boundary (signature verify, parse, dedupe), runs every new case through the pipeline
  under the concurrency cap, then prints summary metrics + one example of a case that correctly
  stopped/escalated.
- **`_DEFAULT_FRESH`** — a per-run fresh DB path.
- **`_resolve_url(fresh)`** — when fresh, creates a dedicated `reclaim_fresh_<uuid8>.db` file so
  re-runs start clean.
- **`_build_db()`** — uses `RECLAIM_FRESH=1` env; if fresh, `drop_all` then `init_schema`.
- **`ingest_batch(db, batch, settings)`** — (extracted Phase 2 helper) ingests every valid
  delivery, returning `(new_ids, dupes, rejected)` — shared by the batch CLI and the `/simulator`
  so both exercise the exact same webhook boundary.
- **`main()`** — generates the batch (seed=42, real webhook secret), ingests with
  verify/parse/dedupe counting (new / duplicates / rejected), runs `run_batch(new_ids)`, then calls
  **`finalize_audit_chain(db)`** (Phase 5: computes the hash chain in one sequential pass *after*
  the concurrent writes finish — see `audit.py`), computes + prints metrics, returns 0.
- **Console script** — `reclaim-batch` (`pyproject.toml` `[project.scripts]`) wraps `main()`.
- **`_print_report(metrics, db, new_ids)`** — pretty-prints the report and one graceful example
  case's audit trail (with a ` <-- LLM failure or stopping-rule override` tag).
- **`_resolved_without_retry(db, case_id)`** — True when the case hit a deliberate stop or
  escalation.

---

### 4.24 `src/reclaim/api.py` — FastAPI application (HTML surfaces + CORS + `/api/v1` mount)
- **Responsibility:** HTTP boundary: webhook ingestion + decision trail, operator, customer, policy,
  and simulator surfaces — plus (Phase 4) CORS for the React SPA and the mount point that carries
  the `/api/v1/*` JSON router (`api_v1.py`).
- **Routes (HTML / legacy):**
  - `GET /health`, `POST /webhook/razorpay`, `GET /cases/{case_id}` (+`?fmt=json`),
    `GET /dashboard`, `GET /metrics`.
  - `POST /cases/{case_id}/approve_retry` and `POST /cases/{case_id}/resolve_human` — operator
    manual-override actions for ESCALATED cases (via `manual.py`), audited as `manual_override`,
    illegal action → 409.
  - `GET /status/{case_id}` — customer-facing, plain-language status page (no rule ids / stage
    names / LLM jargon), reads the SAME case + audit data as the dashboard.
  - `GET /rules` — the active policy-as-code stopping rules rendered in plain language with live
    threshold values.
  - `GET /simulator` + `POST /simulator` — rule-sensitivity simulator (see below).
- **CORS middleware (Phase 4):** `app.add_middleware(CORSMiddleware, allow_origins=...)` built
  from `settings.cors_origins` (split on commas, empties dropped). **Explicitly not a wildcard** —
  the default is the Vite dev origin only; the deployment caveat is flagged loudly in code and
  `frontend/README.md`.
- **Router mount (Phase 4):** `app.include_router(api_v1_router)` pulls the `/api/v1/*` JSON
  namespace in below the CORS middleware. `api_v1` is imported at module scope (imported after the
  app is constructed) to avoid a circular import.
- **Shared view moves (Phase 4):** `_SIM_THRESHOLD_FIELDS`, `_run_simulated_batch`,
  `_sim_metric_key`, `_CAUSE_PLAIN`, and `customer_view` are now defined in `api_views.py` (shared
  with the JSON API so HTML and JSON cannot drift) and **re-imported at the top of this module**, so
  the existing `/simulator` HTML route and `tests/test_simulator.py` resolve unchanged.
- **`lifespan(app)`** — async contextmanager: builds `Database`, `init_schema`, stores
  `app.state.db` / `app.state.settings`, logs the llm/act modes, closes DB on shutdown.
- **`app = FastAPI(title="Reclaim — AI Revenue Recovery", lifespan=lifespan)`** — constructed, then
  CORS middleware + the `/api/v1` router are applied.
- **`health()`** → `{"status": "ok", "service": "reclaim"}`.
- **`razorpay_webhook(request, x_razorpay_signature, x_razorpay_event_id)`** — strict boundary
  order: **signature first → 401** on failure; **parse → 422** on schema error; **ingest/dedupe →
  422** on unmappable. A new case calls `dispatcher.submit_case`; returns `duplicate` flag,
  `case_id`, `event_id`, `state`.
- **`get_settings_dep()` / `get_db_dep()`** — read from `app.state` with `get_settings()` /
  `get_db()` fallback.
- **`case_detail(case_id, fmt="html")`** — per-case decision trail; `?fmt=json` returns a
  machine-readable payload (`case_id`, `state`, `amount`, `customer_id`, `fallback_any_stage`,
  `audit_trail`); default renders an HTML page, with operator override buttons when the case is
  ESCALATED (`_ESCALATED_CONTROLS`).
- **`metrics()`** → JSON of `compute_metrics`.
- **`approve_retry_endpoint` / `resolve_human_endpoint`** — thin wrappers over `manual.py`;
  409 on `KeyError`/`IllegalTransitionError`; 303 redirect back to the case page.
- **Customer status page** — `_CAUSE_PLAIN` map (cause → friendly message),
  `_customer_view(row, trail)` (derives cause, scheduled retry date, recovered/stopped flag →
  plain-language `{heading, reason, next_step}`), `customer_status` (404 if unknown),
  `_STATUS_PAGE` renderer.
- **`/rules`** — `rules_page()` + `_RULES_PAGE(settings, rules)` render the `describe_rules`
  output as a table (rule id / priority / forced action / plain-language policy with live values).
- **Simulator** — `_SIM_THRESHOLD_FIELDS` / `_run_simulated_batch` / `_sim_metric_key` now live in
  `api_views.py` (shared with `POST /api/v1/simulator/run`). `_run_simulated_batch(settings,
  overrides)` runs the seed-42 batch on a **throwaway temp-file DB** (a shared in-memory engine can't
  cross the thread pool `run_batch` uses) under a `settings.model_copy(update={database_url,
  ...overrides})` — real settings never mutated; this module keeps `_sim_comparison`, `_SIMULATOR_PAGE`,
  `simulator_form` (GET), `simulator_run` (POST, form-driven, never lets a simulation crash the page).
- **`dashboard()`** — read-only HTML dashboard: every case card + its full decision trail, with a
  link to per-case pages.
- **HTML helpers:** `_entry_to_dict`, `_trail_html`, `_stage_block`, `_state_class`,
  `_outcome_class`, and a large `_DASH_CSS` stylesheet (color-codes diagnostics, decisions,
  overrides, stops, recoveries, escalations, and LLM-failure fallback tags).

---

### 4.25 `src/reclaim/api_views.py` — Shared derived views (HTML ⇄ JSON single source of truth)
- **Responsibility:** (Phase 4) The server-side *derived* views used by BOTH the legacy HTML routes
  (`api.py`) and the JSON API (`api_v1.py`), kept free of any FastAPI `app`/routing so it can be
  imported without a circular dependency (`api_v1.py` must not import `api.py`). Where the HTML and
  JSON surfaces agree on derived logic, it lives here so they **cannot drift**.
- **`_SIM_THRESHOLD_FIELDS`** — the tuple of editable simulator thresholds (`escalation_amount_threshold`,
  `escalation_days_threshold`, `max_retries_per_cycle`, `cooldown_hours`, `email_cap_per_7d`).
- **`_run_simulated_batch(settings, overrides)`** — runs the seed-42 synthetic batch on a throwaway
  temp-file SQLite DB (`sqlite:///<tmp>/sim.db` created in a `tempfile.TemporaryDirectory`) under a
  `settings.model_copy(update={database_url, **overrides})`, returns `compute_metrics` output. The
  real settings object is **never mutated**; `api.py` re-imports this so `/simulator` and
  `POST /api/v1/simulator/run` share one implementation, and `tests/test_simulator.py` (which
  imports it as `reclaim.api._run_simulated_batch`) keeps resolving.
- **`_sim_metric_key(metrics)`** — human "label → value" rows for the before/after comparison
  (total cases, recovery rate, amount recovered, escalated, stopped, the three non-conflated
  counters).
- **`_CAUSE_PLAIN`** — the `cause → plain-language sentence` map for the customer status view.
- **`customer_view(row, trail)`** — builds the customer-safe `{heading, reason, next_step}` snapshot
  from the SAME case row + audit trail the merchant dashboard reads. Pure data, no internal rule
  ids / stage names / LLM jargon. Shared by `GET /status/{case_id}` (HTML) and
  `GET /api/v1/status/{case_id}` (JSON).

---

### 4.26 `src/reclaim/api_v1.py` — Phase 4 JSON API namespace (`/api/v1/*`)
- **Responsibility:** A **parallel surface** to the HTML routes — each endpoint is a **thin wrapper**
  over the SAME already-tested logic in `repo`, `metrics`, `manual`, `stopping_rules`, and
  `api_views`. It deliberately does **not** reimplement business logic, so behavior cannot drift
  between surfaces. Lives in its own module (rather than more routes in `api.py`) to avoid the
  circular import `api.py` ⇄ `api_v1.py` would create via the DB/settings dependencies.
- **`router = APIRouter(prefix="/api/v1")`** — mounted by `api.py` with `app.include_router(...)`
  below the CORS middleware.
- **Pagination safety:** `_MAX_PAGE = 200`, `_DEFAULT_PAGE = 50` (never an unbounded result set).
- **Dependencies:** module-level `get_db_dep(request)` / `get_settings_dep(request)` read
  `request.app.state` with a `get_db()` / `get_settings()` fallback — mirroring `api.py`'s helpers
  without importing from it; tests override them via `app.dependency_overrides`.
- **Serializers:** `_audit_entry_dict(e)` (full record, including `input_state` with
  `llm_provenance`, so the SPA renders provenance + overrides without a second round-trip),
  `_case_summary(r)` (A1 row shape), `_case_detail_json(db, case_id)` (the shared A2 detail shape =
  the legacy `case_detail(fmt=json)` base fields + the richer audit trail; 404 `case_not_found`).
- **Routes (A1–A7):**
  - **A1** `GET /api/v1/cases` — `state` filter + `limit`/`offset` pagination pushed to the SQL
    query layer via `repo.list_cases`; invalid state / out-of-bounds → 422. Returns
    `{items, count}`.
  - **A2** `GET /api/v1/cases/{case_id}` — `_case_detail_json` (audit trail with provenance); 404.
  - **A3** `GET /api/v1/metrics` — `compute_metrics` as-is (full shape).
  - **A4** `GET /api/v1/rules` — `describe_rules` (R1–R7 registry with live values).
  - **A5** `POST /api/v1/simulator/run` — `SimulatorRunRequest` body (the same editable threshold
    subset, all optional); runs `_run_simulated_batch` for baseline (empty overrides) + simulated
    (submitted), returns `{baseline, simulated}`. Never mutates real settings; a thrown error
    becomes HTTP 500 `simulation_failed:<Type>` rather than crashing the API.
  - **A6** `POST /api/v1/cases/{case_id}/approve_retry` and `/resolve_human` — thin wrappers over
    `manual.approve_manual_retry` / `manual.resolve_human`; updated case JSON (A2 shape) on success,
    `404 case_not_found` on `KeyError`, `409 action not legal` on `IllegalTransitionError`
    (matching the HTML routes' error semantics sans redirect).
  - **A7** `GET /api/v1/status/{case_id}` — the shared `customer_view` as `{heading, reason,
    next_step}`; 404 if unknown; **no internal jargon** (asserted by tests).

---

### 4.27 `src/reclaim/audit_chain.py` — Shared hash-chain utilities (Phase 5 tamper-evidence)
- **Responsibility:** The single source of canonical serialization + hash computation shared by the
  write path (`audit.py` finalize) and the verification path (`verify_audit_chain.py`) so both
  compute **identical** hashes. No DB access — purely functional.
- **`GENESIS_HASH`** — `"0" * 64` (the predecessor hash of the first entry in the chain).
- **`_canonical_entry_dict(row_or_dict)`** — builds a canonical dict of every content field
  (all columns **except** the hash columns) that is byte-identical whether the input is an ORM row
  (after the JSON round-trip through SQLite) or a plain dict. Timestamps are normalized to
  `%Y-%m-%dT%H:%M:%SZ` via `strftime` (drops microseconds and ignores tzinfo so the aware write path
  and the naive SQLite-readback verify path agree); `input_state` is normalized to a dict.
- **`compute_entry_hash(prev_hash, entry_dict)`** — `sha256(prev_hash + canonical_json_sorted)` →
  64-char hex. `canonical_json` = `json.dumps(sort_keys=True, separators=(",", ":"))`.
- **`chain_rows(entries)`** — computes the full hash chain for an ordered list in **one pass**,
  returning parallel `(id, prev_hash, entry_hash)` tuples. Each link derives from the *previous
  entry's computed hash*, never from a concurrent read of "the most recent row" — race-free by
  construction, so the audit chain can never fork.

### 4.28 `src/reclaim/verify_audit_chain.py` — Audit-chain verification walk (Phase 5 CLI)
- **Responsibility:** Walks the entire audit log in order and confirms every entry's
  `entry_hash`/`prev_hash` matches a fresh recomputation — tamper-evidence for auditors. Pure
  read-only: recomputes + compares, never writes.
- **`@dataclass(frozen=True) AuditChainVerificationResult`** — `is_valid`, `total_entries`,
  `first_broken_entry_id`, `first_broken_entry_reason`.
- **`verify_audit_chain(session)`** — queries all `AuditLogRow` ordered by `id` (insertion order),
  walks the chain with the shared canonicalization; an empty log verifies as valid; reports the
  **first** break (either an `entry_hash` mismatch or a `prev_hash` mismatch) with the expected vs.
  stored values.
- **CLI** — `python -m reclaim.verify_audit_chain` (and console script `reclaim-verify-audit-chain`):
  reconfigures stdout to UTF-8 (the `✓`/`✗` glyphs crash a cp1252 Windows console otherwise), prints
  `✓ Chain is valid (N entries)` or the broken-link report, exits 0/1.

### 4.29 `src/reclaim/retry_simulator.py` — Shared retry-success simulation model (Phase 5)
- **Responsibility:** The single source of truth for "does a retry on this decline code actually
  succeed?" — the same model the counterfactual baseline (and, by intent, the stub pipeline) face,
  so strategies differ only in *which cases they retry* (stopping rules), not in the odds of success.
- **`RETRY_SUCCESS_RATE: dict[str, float]`** — realistic per-decline-code success probability
  (informed by industry retry data): `R01`/`R02` (insufficient funds) 0.45, `54`/`F14` (card
  expired) 0.05, `91`/`Z06` (bank timeout) 0.60, `05`/`N7` (do not honor) 0.35, `R0`/`PM`
  (mandate revoked) 0.00, `255`/`C6` (unknown) 0.25; unknown codes default to 0.25.
- **`retry_would_succeed(failure_reason, rng=None)`** — samples `rng.random() < rate`; seeded
  `random.Random` makes every counterfactual reproducible.

### 4.30 `src/reclaim/robustness.py` — Multi-seed robustness suite (Phase 5)
- **Responsibility:** Runs the batch across N **independently seeded** synthetic batches and
  reports the *distribution* of recovery outcomes — statistical rigor instead of a single lucky
  (or unlucky) seed.
- **`@dataclass(frozen=True) RobustnessResult`** — `seed`, `recovery_rate`, `recovered_amount`,
  `amount_at_risk`, `recovered_cases`, `total_cases`.
- **`@dataclass(frozen=True) RobustnessReport`** — `runs`, plus distribution stats for recovery rate
  and recovered amount (`median`, `p5`, `p95`, `stddev`), `headline_batch_seed=42`, and
  `headline_batch_percentile` (fraction of runs below the default seed-42 batch's recovery rate).
- **`run_robustness_suite(num_seeds=100, settings=None, db=None)`** — for each seed in
  `range(num_seeds)`: `generate_batch(seed)` → ingest valid deliveries → `run_batch` →
  `compute_metrics`; then computes median/P5/P95/stddev and the seed-42 headline percentile.
  Reuses the existing synthetic generator + pipeline — no duplicated logic.
- **CLI** — `python -m reclaim.robustness [num_seeds]` (console script `reclaim-robustness`):
  prints the recovery-rate and recovered-amount distributions and an **honest headline disclosure**
  of where the default batch falls (e.g. "below median") rather than omitting it.

### 4.31 `src/reclaim/baseline.py` — Counterfactual baseline comparison (Phase 5)
- **Responsibility:** Demonstrates the economic differentiator — compares the **real Reclaim
  policy** against two **naive strategies** on the *same* seeded batch under a realistic
  retry-success model, so the only policy difference is the stopping-rule layer.
- **`@dataclass(frozen=True) StrategyResult`** — `name` (`do_nothing` | `retry_everything` |
  `reclaim`), `gateway_calls`, `cases_succeeded` (the success rate — disambiguates "₹0 by not
  calling" from "₹0 by calling and failing"), `gross_recovered`, `policy_blocked_value`,
  `net_recovered`.
- **`@dataclass(frozen=True) BaselineComparison`** — `seed` + the three `StrategyResult` rows.
- **`ASSUMED_CHARGEBACK_RATE = 0.85`** — conservative documented assumption: retries that the real
  policy would have blocked (R1–R7 stops/escalations) incur an 85% chargeback rate, so the naive
  strategy's "net" is deliberately *not* flattering.
- **Three strategies** (all share the realistic success model):
  - `do_nothing` — 0 gateway calls, ₹0 recovered.
  - `retry_everything` — one call per case, no stopping rules: gross = success-model amount on all
    cases; **net** = gross − (85% × policy-blocked value), where policy-blocked value = the sum of
    amounts the real policy would have stopped/escalated.
  - `reclaim` — the **real** pipeline runs first (via `run_batch`); its retry-eligible cases are
    identified from the audit trail (no `decide` `rule_override` **and** an `act`/`retry_now`), then
    outcomes are recomputed from the **same per-case seeded model** (`_case_success`, keyed
    `Random("seed:case")`) every strategy reads, so the rows differ only in *which cases they
    attempt* — a controlled "same world, different policy" counterfactual. The probabilistic result
    (e.g. seed 42: ₹15,994 net) is honestly distinct from the stub-mode batch report (₹39,776
    deterministic).
- **`_build_fresh_db(settings)`** — the comparison **always runs on a fresh, file-backed temp SQLite
  DB** (isolated from, and never mutating, the real DB). This fixes the original bug where re-ingesting
  the seeded batch into the real `reclaim.db` deduped every event (UNIQUE `event_id`) → 0 calls / ₹0.
  File-backed (not in-memory) because `run_batch` fans out across threads.
- **`run_baseline_comparison(seed=42, settings=None, db=None)`** — generates the batch, ingests,
  runs the real pipeline, finalizes the audit chain, computes all three strategy rows, returns
  `BaselineComparison`. (The `db` argument is accepted for signature stability but deliberately not
  used — a counterfactual cannot be trusted against a polluted DB.)
- **CLI** — `python -m reclaim.baseline [seed]` (console script `reclaim-baseline`): prints the
  strategy table (Gateway Calls / Cases Succeeded / Gross Recovered / Policy-Blocked Value / Net
  Recovered) plus comparative analysis (gateway-call reduction %, net-recovery advantage,
  policy-blocked value insight). Reconfigures stdout to UTF-8 so `₹` never crashes a cp1252 console.
  Live seed-42 result (controlled counterfactual, verified in-session):
  `retry_everything` 60 calls / **9 ok** / ₹23,491 gross / −₹93,786.90 net (85% chargeback on
  ₹137,974 blocked) vs `reclaim` 24 calls / **6 ok** / ₹15,994 net — **60%** fewer gateway calls.
  (The earlier ₹55,382 gross / ₹24,089 net figures were an RNG-alignment artifact — each
  strategy drew from its own `Random(seed)` stream at different positions; the per-case shared
  draw above is the controlled result.)

---

### 4.32 Test files (`tests/` + `frontend/`)
Here the suite is listed per file with exact test counts. **Backend: 160 tests across 19 files.**
**Frontend (Phase 4): 6 component tests across 2 Vitest files** — see the `frontend/` note at the end.

- **`conftest.py`** — hermetic fixtures: an autouse `_no_dotenv` clears the settings cache; a
  `settings` fixture builds a `Settings` with `_env_file=None` (ignores real `.env`), a fixed test
  webhook secret, `llm_mode="offline"`, `act_mode="stub"`, eager Celery, and a per-test SQLite
  file in tmp_path; a `db` fixture builds a `Database`, initializes the schema, resets the cached
  instance, and cleans up. `TEST_WEBHOOK_SECRET` is defined here.
- **`test_api_v1.py` (18 tests, Phase 4)** — HTTP-layer tests of the `/api/v1/*` namespace via a
  `TestClient` with `app.dependency_overrides` on `api_v1.get_db_dep` / `get_settings_dep` pointed at
  the hermetic conftest fixtures (the app lifespan never runs). Deliberately proves the HTTP boundary
  — status codes, wire shapes, params — NOT business logic already covered elsewhere: A1 happy path /
  state filter / pagination bounds (422 on `offset=-1`, `limit=0`, `limit=100000`) / invalid state
  422; A2 detail with `llm_provenance` in the trail + 404; A3 full 17-key metrics shape; A4 R1–R7
  registry shape; A5 comparison returns + real-settings-not-mutated + empty body ⇒ identical; A6
  approve/resolve success (A2 shape), 404 unknown, 409 not-ESCALATED; A7 plain-language no-jargon
  check + 404.
- **`test_metrics.py` (5 tests)** — pins that the three counters are separate: a rule override
  is not an LLM failure; an LLM failure is not a rule override; a STOP executes no stub action;
  a recovered retry counts as a stub action; an R2 escalation is both an override and a stub
  action (counters independent, not mutually exclusive).
- **`test_models.py` (9 tests)** — Pydantic boundary validation: negative amount rejected, zero
  attempt rejected, confidence bounds, `scheduled_at` cross-field rules (required for
  `retry_scheduled`, rejected for other actions, must be future), `days_since_last_attempt` uses
  history, webhook paise→INR conversion, default state is INGESTED.
- **`test_pipeline.py` (9 tests)** — end-to-end: healthy case recovers; mandate-revoked escalates
  and is audited; exhausted attempts stop (no retry); diagnose/decide LLM failures fall back
  deterministically without crashing; decision fallback is idempotent across rerun; retry is
  idempotent via the ledger (single ledger row); dependent cases have distinct claims; concurrency
  limiter bounds peak (asserts the shared-semaphore fix).
- **`test_state_machine.py` (14 tests)** — the original 10 lifecycle/transition-table tests
  (full legal lifecycle; illegal edge; terminal absorbing; DECIDED→RESOLVED requires `via_stop`;
  via_stop legal; decision-flow stop resolves without acting; retry routes through ACTING; escalate/
  fail terminal; listener metadata; decision flow requires DECIDED) plus **4 manual-edge tests**
  (`test_approve_retry_from_escalated`, `test_resolve_human_from_escalated`,
  `test_manual_overrides_require_escalated`, `test_manual_transition_is_audited_via_listener`).
- **`test_stopping_rules.py` (20 tests)** — the original 13 (every R1–R6 forced override; attempt
  at/below max; email cap under; cooldown math; compliant proposal untouched; R1 precedence over
  R2; valid future `scheduled_at` kept; valid Pydantic output) plus **7 Phase-3 tests**
  (`test_rule7_amount_below_floor_forces_stop`, `test_rule7_applies_to_all_retry_proposals`,
  `test_rule7_at_or_above_floor_is_allowed`, `test_rule7_threshold_is_env_configurable`,
  `test_rule1_takes_precedence_over_rule7`, `test_rules_are_declarative_and_described`,
  `test_rules_page_renders_policy_in_plain_language`).
- **`test_manual.py` (5 tests)** — the human-in-the-loop override actions:
  `test_approve_manual_retry_resolves_and_audits`, `test_resolve_human_resolves_and_audits`,
  `test_approve_manual_retry_illegal_on_non_escalated`, `test_resolve_human_illegal_on_non_escalated`,
  `test_manual_override_is_idempotent_via_ledger`.
- **`test_simulator.py` (4 tests)** — the rule-sensitivity simulator:
  `test_simulator_baseline_reproduces_reference_metrics`, `test_simulator_tightens_escalation_when_threshold_lowered`,
  `test_simulator_metric_key_labelling`, `test_simulator_does_not_mutate_real_settings`.
- **`test_synthetic.py` (9 tests)** — batch shape/counts, ≥50 unique events, every cause
  represented, above-threshold amounts and long gaps present, seeded determinism, different seeds
  differ, valid deliveries parse+sign, duplicates exact replays, rejections cover all classes.
- **`test_webhook.py` (13 tests)** — signature (valid/wrong/tampered/missing/empty-secret),
  parsing (valid/bad-JSON/missing-event-type/deterministic-id), event→case mapping
  (valid/unmappable), dedupe (duplicate is a no-op, and a replay never mutates an already-advanced
  case state).
- **`test_audit_chain.py` (11 tests, Phase 5)** — hash-chain tamper-evidence:
  `_canonical_entry_dict` handles ORM rows/dicts identically, `compute_entry_hash` is deterministic,
  empty log verifies trivially, healthy chain verifies clean, mutating any field (or the prev link)
  breaks verification from that point, multi-case chains chain correctly, an **unfinalized log
  (empty hashes) is detected as invalid**, `finalize_audit_chain` is deterministic/idempotent, and
  the dataclass shape.
- **`test_baseline.py` (10 tests, Phase 5)** — counterfactual strategies: dataclass shapes,
  `do_nothing` recovers ₹0, `retry_everything` makes one call per case, retry-eligible detection,
  comparison smoke test, `retry_everything` makes more calls than `reclaim`, the chargeback-netting
  math, the **[real-batch regression]** — running the comparison against a fully populated DB still
  reports 60 calls / ₹>0 (fresh-DB isolation fix), and the CLI import.
- **`test_llm_isolation.py` (3 tests, Phase 5)** — **structural proof** via `ast` parsing:
  `llm_client.py` imports neither `razorpay_client.py` nor `act.py` directly, so the LLM module has
  no code path to execute a money-moving action outside the reviewed pipeline → stopping-rules →
  act flow; plus the import-graph verification and import-boundary checks.
- **`test_robustness.py` (7 tests, Phase 5)** — multi-seed suite: dataclass shapes, the runner
  produces N independent results across seeds, different seeds give (some) different outcomes,
  distribution-stat math (median/p5/p95/stddev) is correct against a known small distribution, the
  headline-batch percentile calculation, and the CLI import.
- **`tests/adversarial/test_audit_chain_concurrency.py` (1 test, Phase 5 regression)** — writes via
  the **real** `write_audit` path from 8 threads (the concurrent batch shape that once forked the
  chain), then asserts the finalized chain verifies clean — locking in the race-free
  `finalize_audit_chain` design.
- **Frontend component tests (6 tests / 2 files, Vitest + React Testing Library)** — cover the
  logic-bearing components, not static display: `components/ManualActions.test.tsx` (4) — override
  buttons render, disable while pending, and surface the distinct 409 "already resolved" case vs a
  generic failure; `pages/Simulator.test.tsx` (2) — the form submits the correct payload shape
  (only filled fields) and renders the before/after comparison on success. E2E (Playwright/Cypress)
  is deliberately deferred — the trade-off is documented in `frontend/README.md`.

---

## 5. Data Models & Schema

### 5.1 Domain/enum vocabulary
| Concept | Values / fields |
|---------|-----------------|
| `CaseState` | `INGESTED`, `DIAGNOSED`, `DECIDED`, `ACTING`, `RESOLVED`, `ESCALATED`, `FAILED` (RESOLVED/ESCALATED/FAILED terminal; ESCALATED re-openable only via manual override) |
| `Cause` | `insufficient_funds`, `card_expired`, `bank_timeout`, `do_not_honor`, `mandate_revoked`, `unknown` |
| `Action` | `retry_now`, `retry_scheduled`, `request_payment_method_update`, `escalate_human`, `stop` |
| `WebhookType` | `payment.failed`, `subscription.charged.failed`, `subscription.pending` |

### 5.2 Relational schema (SQLAlchemy 2, Postgres-compatible, WAL on file-backed SQLite)
**`recovery_cases`**
| Column | Type | Constraints |
|--------|------|-------------|
| `id` | Integer | PK |
| `case_id` | String(255) | **UNIQUE**, index |
| `event_id` | String(255) | **UNIQUE** (dedupe key) |
| `customer_id` | String(255) | |
| `subscription_id` | String(255) | index |
| `failure_reason` | String(512) | |
| `failure_reason_raw` | String(512) | |
| `amount` | Float | |
| `attempt_number` | Integer | |
| `customer_tier` | String(32) | |
| `payment_history` | JSON | default list |
| `state` | String(32) | index |
| `created_at` | DateTime(tz) | |
| `last_attempt_at` | DateTime(tz) | nullable |

**`audit_log`** (append-only; stage may be `ingest`/`diagnose`/`decide`/`act`/`manual_override`/
`sweep`/`verify_*`; `input_state` may carry `llm_provenance`; **hash-chained for tamper-evidence
(Phase 5)**)
| Column | Type | Constraints |
|--------|------|-------------|
| `id` | Integer | PK |
| `case_id` | String(255) | index |
| `stage` | String(64) | |
| `agent_reasoning` | String(4096) | default `""` |
| `input_state` | JSON | default dict |
| `decision` | String(255) | |
| `action_taken` | String(255) | nullable |
| `outcome` | String(255) | |
| `fallback_triggered` | Boolean | default False |
| `timestamp` | DateTime(tz) | |
| `prev_hash` | String(64) | default `""` — previous entry's SHA-256 hash |
| `entry_hash` | String(64) | default `""` — this entry's hash; both derived by `finalize_audit_chain` |

**`executed_actions`** (idempotency ledger)
| Column | Type | Constraints |
|--------|------|-------------|
| `id` | Integer | PK |
| `case_id` | String(255) | index |
| `attempt_number` | Integer | |
| `action` | String(64) | |
| `idempotency_key` | String(255) | **UNIQUE** |
| `executed_at` | DateTime(tz) | |
| (composite) | | `UNIQUE(case_id, attempt_number, action)` → `uq_executed_action` |

### 5.3 Pydantic payload schemas (key invariants)
- **`RecoveryCase`** — `amount > 0`, `attempt_number >= 1`, `failure_reason` non-empty,
  `event_id` globally unique, `case_id` = the recoverable unit (subscription id).
- **`DiagnoseOutput`** — `confidence ∈ [0,1]`, rounded to 4 dp.
- **`DecideOutput`** — action plus an **optional** `scheduled_at` that is **required iff**
  `action == retry_scheduled` and **must be future** when present.
- **`WebhookEvent.amount()`** — converts Razorpay **paise** to INR (÷100).

### 5.4 Idempotency key invariant
`idempotency_key = "reclaim:{case_id}:{attempt_number}:{action}"` — deterministic; the same
`(case, attempt, action)` always yields the same key, backed by the `UNIQUE` ledger constraint so a
duplicate Act can never double-charge. This survives races (concurrent duplicate webhooks), network
drops (response lost after the server accepted), and multi-process duplicates.

### 5.5 Stored provenance shape
On every Diagnose/Decide call the audit entry's `input_state["llm_provenance"]` carries:
`{"model": <ollama_model>, "prompt_version": "diagnose-v1"|"decide-v1", "prompt_hash": <sha256>,
"mode": "offline"|"online"}` — for drift detection and reproducibility.

---

## 6. System Data Flow & Logic

### 6.1 End-to-end lifecycle: a payment-failure webhook

```
 [1] INGEST        [2] DIAGNOSE      [3] DECIDE         [4] ENFORCE          [5] ACT            [6] TERMINAL
  Razorpay webhook -> verify sig      LLM proposes      stopping rules      execute action     RESOLVED / ESCALATED / FAILED
  (X-Razorpay-      parse (Pydantic)  one bounded       R1-R7 dispose       idempotently
   Signature)       triage adverse    action            (policy-as-code,     (stub or live,
   -> 401/422       dedupe            (proposal only)    independent of      claim first)
   (7) SWEEP -> stale ACTING reconciled to ESCALATED    (8) VERIFY -> non-blocking external read
```

**Step-by-step:**

1. **Boundary (webhook):** Razorpay POSTs a `payment.failed` (or `subscription.*`) event to
   `POST /webhook/razorpay` with `X-Razorpay-Signature` and `X-Razorpay-Event-Id` headers.
   - `api.razorpay_webhook` reads the raw body, calls `verify_signature` (constant-time HMAC-SHA256
     over the raw bytes with `RAZORPAY_WEBHOOK_SECRET`). Failure → **401**.
   - `parse_event` decodes JSON and validates via `WebhookEvent` (Pydantic). Failure → **422**.
   - `ingest_event` inserts a `RecoveryCaseRow` (state `INGESTED`). If `event_id` already exists, the
     existing case is returned and no stages re-trigger (dedupe, race-safe via the `UNIQUE` +
     insert-then-catch). Unmappable payload → **422**.
2. **Dispatch:** for a new case, `dispatcher.submit_case` hands off **exactly once** —
   synchronously (`pipeline.run_case` in eager mode) or as a Celery task
   (`reclaim.pipeline.run_case_task` in broker mode).
3. **Diagnose:** `pipeline.run_case` sends the case through `CaseStateMachine.diagnose()`
   (INGESTED → DIAGNOSED), persists it, builds a `DiagnoseInput`, and calls the LLM. Before the
   model runs, `triage_diagnose_input` short-circuits injection-marker/control-char/malformed
   decline codes to a deterministic `UNKNOWN` fallback (**not** an LLM failure). The offline shim
   or online Ollama (`LLMWrapper.diagnose`, with exponential backoff online) returns
   `DiagnoseOutput(cause, confidence, reasoning)`, written to the audit trail with provenance.
4. **Decide:** `machine.decide()` (DIAGNOSED → DECIDED), persists it, builds a `DecideInput`
   (`cause`, `attempt_number`, `days_since_last_attempt`, `amount`, `customer_tier`), calls the LLM
   `decide`, which returns one `DecideOutput(action, scheduled_at?, reasoning)`.
5. **Enforce (the guard):** `stopping_rules.enforce` walks the declarative R1–R7 registry with the
   thresholds from settings and the caller-supplied per-customer `payment_method_update_count`
   (from `repo.count_recent_payment_method_updates`). The first matching rule **overrides** the
   action; the audit records `rule=<R#>` + `OVERRIDE`. `fallback_triggered` is set **only** when the
   LLM call itself failed (not on rule overrides) — this is what lets metrics separate the two.
6. **Route:** `run_decision_flow` — `STOP` → `resolve_as_stopped()` (DECIDED → RESOLVED via `stop`,
   **no side effect**); every other action → `start_acting()` (DECIDED → ACTING).
7. **Act (idempotent):** `act.execute_action` **claims the ledger first**
   (`ExecutedActionRow` UNIQUE on `(case_id, attempt, action)` + unique `idempotency_key`). If the
   claim is already held, it returns a logged duplicate no-op (never double-charges). Otherwise:
   - `retry_now` → `RazorpayClient.retry_payment` (stub logs / live posts with an idempotency-key
     header) → **RESOLVED** (recovered) or **FAILED**.
   - `retry_scheduled` → `schedule_retry` (eager: record eta; broker: enqueue
     `reclaim.tasks.retry_payment_task` at `eta` with an idempotent task id) → **RESOLVED** (money
     pending) or **FAILED**.
   - `request_payment_method_update` / `escalate_human` → `email.send_email_message` stub → RESOLVED
     / ESCALATED.
8. **Verify (non-blocking):** where enabled, `verify.py` performs **verification-only** reads of
   external state (subscription status / settlement reconciliation) after the action, recording a
   `verify_*` audit entry. It never blocks, reverses, or changes terminal state; a failure just
   records.
9. **Log:** each stage appends an immutable `AuditLogRow` (append-only), and the case row's `state`
   is updated at each transition. Terminal states are absorbing (except ESCALATED, re-openable only
   via the manual-override control plane). **Phase 5 (tamper-evidence):** after the concurrent
   write phase completes, `audit.finalize_audit_chain` derives each row's `prev_hash`/`entry_hash`
   in one sequential pass (compute-at-write would race and fork the chain); the batch CLI and
   baseline do this after `run_batch`. `python -m reclaim.verify_audit_chain` (or the
   `reclaim-verify-audit-chain` console script) then proves the log un-tampered — any content
   mutation breaks the chain from that row onward.
10. **Crash recovery (sweep):** if the process dies between DECIDED and a completed ACT, the case
    is left in `ACTING`. The periodic beat task `reclaim.tasks.sweep_stale_acting_task` (every
    5 min) finds stale `ACTING` cases past `STALE_LOCK_TIMEOUT_SECONDS` and reconciles them to
    `ESCALATED` (human review) — never touching legitimately in-progress cases, never running a
    side effect. A human can then approve a retry or resolve it via `/cases/{id}/approve_retry` /
    `/resolve_human`.
11. **Surfacing:** `GET /cases/{case_id}` (HTML or `?fmt=json`), `GET /dashboard` (HTML),
    `GET /metrics` (JSON), `GET /status/{case_id}` (customer-facing, plain language),
    `GET /rules` (active policy), and `GET/POST /simulator` (threshold sensitivity). **Phase 4
    adds a parallel JSON namespace** — `GET /api/v1/cases` (filtered + paginated at the SQL layer),
    `GET /api/v1/cases/{case_id}` (audit trail + provenance), `GET /api/v1/metrics`,
    `GET /api/v1/rules`, `POST /api/v1/simulator/run`, `POST /api/v1/cases/{id}/approve_retry` &
    `/resolve_human`, and `GET /api/v1/status/{case_id}` — each a thin wrapper over the same tested
    logic, consumed by the **React SPA** (`frontend/`): Case List, Case Detail (with override
    control plane), Rules, Simulator, and the plain-language Customer Status (routed outside the
    app chrome). **Phase 5 adds four console surfaces** — `python -m reclaim.batch`
    (`reclaim-batch`), `python -m reclaim.robustness [N]` (`reclaim-robustness`),
    `python -m reclaim.baseline [seed]` (`reclaim-baseline`), and
    `python -m reclaim.verify_audit_chain` (`reclaim-verify-audit-chain`) — the statistical-rigor
    and tamper-evidence tooling, plus the `baseline-analysis.html` standalone write-up.

### 6.2 Metrics computation (read path)
`metrics.compute_metrics` scans every case + its audit trail in a single pass, deriving:
- **LLM call failures** — any diagnose/decide entry with `fallback_triggered=True`.
- **Stopping-rule overrides** — decide entries whose outcome contains `OVERRIDE` (parsed per rule).
- **Stub-mode actions** — in stub mode, act entries with a side-effecting action in
  `_EXECUTED_ACTIONS`.
- **Recovery** — a case is "recovered" when `state == RESOLVED` and its last act outcome contains
  `retry_succeeded`; the amount is added to `recovered_amount`.
- **`cases_resolved_without_retry`** = stopped + escalated (no retry action taken).

**Precision principle (Phase 5):** every field is labeled for what it proves vs. what needs external
confirmation — e.g. `recovered_amount` proves a gateway call was accepted (from the audit outcome),
not that settlement confirmed. **Phase 5 analysis tools** layered on this read path:
- `robustness.run_robustness_suite` — re-runs the whole batch over N seeds and reports the recovery
  distribution (median / P5 / P95 / stddev) + where the seed-42 headline batch falls.
- `baseline.run_baseline_comparison` — counterfactual table (do-nothing / retry-everything / real
  Reclaim policy) under the shared realistic retry-success model with the documented 85% chargeback
  assumption.
- `verify_audit_chain.verify_audit_chain` — proves the audit log is un-tampered.

### 6.3 Reference demo output (from `python -m reclaim.batch`)
```
Total cases                 : 60
Amount at risk              : Rs.192,246.00
Recovered (retry success)   : 24 cases / Rs.39,776.00
Recovery rate               : 20.7%
Stopped (deliberate halt)   : 5
Escalated (human)           : 23
Deterministic fallbacks:
  LLM call failures           : 0 cases
  Stopping rule overrides     : 32 cases {'R1': 12, 'R6': 4, 'R3': 5, 'R2': 6, 'R4': 5}
  Stub mode actions           : 55 cases
Cases resolved without retry: 28
State distribution          : {'RESOLVED': 37, 'ESCALATED': 23}
Root-cause breakdown        : {'unknown': 13, 'mandate_revoked': 12, 'insufficient_funds': 16,
                               'do_not_honor': 6, 'card_expired': 9, 'bank_timeout': 4}
```
*(Regenerate with `RECLAIM_FRESH=1 python -m reclaim.batch`; R7's economic floor can change the
stopped/escalated split if below-floor amounts are present in later batches.)*

---

## 7. Setup, Installation, & Commands

**Prerequisite:** Python **3.11+** (verified on 3.11.9). Optional for live demo: a local Ollama
server and Razorpay test-mode keys; optional for broker mode: an Upstash Redis URL.

### 7.1 Clone
```bash
git clone https://github.com/Utkarshkarki/Salvage.git
cd Salvage
```

### 7.2 Create a virtual environment + install
```bash
python -m venv .venv
# macOS/Linux:
source .venv/bin/activate
# Windows (PowerShell):
.venv\Scripts\activate

pip install -e ".[dev]"
```

### 7.3 Configure environment variables
`cp .env.example .env` (Windows: `copy .env.example .env`), then fill in:

| Variable | Default | Notes |
|----------|---------|-------|
| `OLLAMA_BASE_URL` | `http://localhost:11434` | Online LLM endpoint |
| `OLLAMA_MODEL` | `qwen2.5:32b-instruct` | Online model name |
| `OLLAMA_TIMEOUT_SECONDS` | `30` | Online LLM timeout |
| `LLM_MODE` | `offline` | `offline` (deterministic shim) or `online` (Ollama) |
| `RAZORPAY_WEBHOOK_SECRET` | *(required, empty)* | Generate: `python -c "import secrets; print(secrets.token_hex(32))"` |
| `RAZORPAY_KEY_ID` / `RAZORPAY_KEY_SECRET` | *(empty)* | Test-mode keys, only for `ACT_MODE=live` |
| `RAZORPAY_BASE_URL` | `https://api.razorpay.com/v1` | Live API base |
| `RAZORPAY_RETRY_PATH` | `""` | **Must be set for live retries** — never guessed |
| `RAZORPAY_SUBSCRIPTION_PATH` | `""` | Live Subscriptions read path (may use `{subscription_id}`) |
| `RAZORPAY_SETTLEMENT_PATH` | `""` | Live Settlements read path (may use `{settlement_id}`) |
| `VERIFICATION_ENABLED` | `1` | `0` disables the verification-only external lookups (hermetic) |
| `ACT_MODE` | `stub` | `stub` (log only) or `live` |
| `REDIS_URL` | `redis://localhost:6379` | For Upstash use `rediss://...` (TLS required) |
| `RECLAIM_CELERY_EAGER` | `1` | `1` = synchronous tasks, no broker |
| `DATABASE_URL` | `sqlite:///reclaim.db` | Swap to a Postgres DSN to migrate |
| `ESCALATION_AMOUNT_THRESHOLD` | `5000` | R2 |
| `ESCALATION_DAYS_THRESHOLD` | `7` | R3 |
| `MAX_RETRIES_PER_CYCLE` | `3` | R4 |
| `COOLDOWN_HOURS` | `24` | R6 |
| `EMAIL_CAP_PER_7D` | `1` | R5 |
| `MIN_RECOVERY_AMOUNT` | `100` | **R7 economic floor** — below this, never auto-retry |
| `STALE_LOCK_TIMEOUT_SECONDS` | `300` | How long a case may sit in `ACTING` before the sweep escalates it |
| `RECLAIM_CORS_ORIGINS` | `http://localhost:5173,http://127.0.0.1:5173` | Comma-separated origins the API allows for the React SPA — never a wildcard; narrow for any real deployment |
| `MAX_CONCURRENCY` | `5` | Batch concurrency cap |
| `LLM_BACKOFF_BASE_SECONDS` | `1` | Backoff base |
| `LLM_BACKOFF_MAX_SECONDS` | `15` | Backoff cap |

> **Security:** the real `.env` is gitignored; only `.env.example` (placeholders) is committed.
> Required secrets that are missing (e.g. an empty webhook secret) **fail loud at load time**.
> Live Razorpay network routes are never guessed — each path must be confirmed against current docs
> and supplied in config, or the code refuses to run (ZERO-HALO).

### 7.4 Run the synthetic batch demo (deterministic, offline/stub)
```bash
# macOS/Linux:
RECLAIM_FRESH=1 python -m reclaim.batch
# Windows PowerShell:
$env:RECLAIM_FRESH="1"; python -m reclaim.batch
```
`RECLAIM_FRESH=1` uses a fresh per-run SQLite file so metrics always start clean.

### 7.5 Phase 5 CLI tools — robustness, baseline comparison, audit-chain verification
Four console scripts are declared in `pyproject.toml` `[project.scripts]` (each also runnable via
`python -m reclaim.*`). They are deterministic and offline; each forces UTF-8 stdout so `₹`/`✓`
never crash a cp1252 Windows console.

```bash
reclaim-batch                              # the standard seed-42 demo (as in 7.4)
reclaim-robustness [num_seeds]             # multi-seed distribution (default 100)
reclaim-baseline [seed]                    # counterfactual: do-nothing / retry-everything / reclaim
reclaim-verify-audit-chain                 # proves the audit log is un-tampered (exit 0/1)
```

- **Robustness** — re-runs the batch over N independent seeds and prints the recovery-rate /
  recovered-amount distribution (median, 5th & 95th percentile, stddev) and an **honest headline
  disclosure** of where the seed-42 batch falls.
- **Baseline** — prints the strategy table (Gateway Calls / Cases Succeeded / Gross / Policy-Blocked
  Value / Net), with an always-fresh temp DB (never the real `reclaim.db`) and the documented 85%
  chargeback assumption for policy-blocked retries. Live result: **reclaim ₹15,994 net vs
  retry-everything ₹23,491 gross / −₹93,786.90 net; 60% fewer gateway calls.**
- **Audit chain** — recomputes every `entry_hash`/`prev_hash` and verifies the log. A healthy log
  prints `✓ Chain is valid (N entries)`; any tamper prints the first broken entry id + reason and
  exits non-zero.

### 7.6 Run the API (Python backend)
```bash
uvicorn reclaim.api:app --reload
# then visit (HTML/legacy surfaces):
#   http://127.0.0.1:8000/health
#   http://127.0.0.1:8000/dashboard
#   http://127.0.0.1:8000/cases/{case_id}
#   http://127.0.0.1:8000/status/{case_id}     (customer-facing, plain language)
#   http://127.0.0.1:8000/rules                (active policy-as-code, plain language)
#   http://127.0.0.1:8000/simulator            (rule-sensitivity before/after)
#   http://127.0.0.1:8000/metrics
# JSON namespace (Phase 4, consumed by the React SPA):
#   http://127.0.0.1:8000/api/v1/cases            (filtered + paginated)
#   http://127.0.0.1:8000/api/v1/cases/{case_id}  (audit trail + llm_provenance)
#   http://127.0.0.1:8000/api/v1/metrics
#   http://127.0.0.1:8000/api/v1/rules
#   http://127.0.0.1:8000/api/v1/status/{case_id}
# POST test events to:
#   http://127.0.0.1:8000/webhook/razorpay   (X-Razorpay-Signature + X-Razorpay-Event-Id headers)
```

### 7.7 Run the React frontend (Phase 4 SPA)

The SPA is a separate Node package under `frontend/`. Start the backend (7.6) and the Vite dev
server together with a helper script, or run them in two terminals:

```bash
# one command (choose for your OS):
scripts/dev.sh        # bash  (macOS / Linux / Git-Bash)
scripts/dev.ps1       # Windows PowerShell

# or two terminals:
#   Terminal 1:  uvicorn reclaim.api:app --reload
#   Terminal 2:  cd frontend && npm install && npm run dev
```

Then open **http://localhost:5173** (Cases / Rules / Simulator) and
**http://localhost:5173/status/{case_id}** for the customer-facing page. The Vite dev server
proxies `/api` → `:8000`, so no CORS occurs in development; the built app uses relative URLs. See
`frontend/README.md` for the production/CORS caveat. A `RAZORPAY_WEBHOOK_SECRET` in `.env` is
required (the backend refuses to boot without it) — run `python -m reclaim.batch` or POST webhooks
to generate cases before exploring.

### 7.8 Run the tests (backend + frontend)
```bash
pytest                         # full backend suite — 149 tests / 18 files, all passing (verified)
pytest tests/test_api_v1.py    # Phase 4 JSON API HTTP-layer subset (18 tests)
pytest tests/adversarial/      # failure-injection & resilience subset (12 tests)
pytest tests/test_baseline.py tests/test_robustness.py tests/test_audit_chain.py \
    tests/test_llm_isolation.py   # Phase 5 statistical-rigor & structural-proof subset (31 tests)

cd frontend && npm test        # Phase 4 SPA component suite — 6 tests / 2 files (Vitest)
npm run build                  # type-check + clean production bundle (dist/)
```

### 7.9 Lint & type-check (backend + frontend)
```bash
ruff check src tests   # backend lint (E, F, I, UP, B, SIM, RUF; E501 ignored)
mypy                   # backend strict type checking over the "reclaim" package

cd frontend && npm run lint        # ESLint (typescript-eslint + react-hooks/refresh)
npm run typecheck                  # TypeScript strict
npm run format                     # Prettier
```

### 7.10 Live demo (optional)
1. Set `LLM_MODE=online` + your Ollama URL/model, `ACT_MODE=live` + Razorpay test keys + confirm
   `RAZORPAY_RETRY_PATH` (and optionally `RAZORPAY_SUBSCRIPTION_PATH` /
   `RAZORPAY_SETTLEMENT_PATH`), and wire a real provider into `email.send_email_message`.
2. Re-run `RECLAIM_FRESH=1 python -m reclaim.batch` or start the API and POST real webhooks.

---

## 8. Development Conventions & Guardrails

### 8.1 Packaging & layout
- **`src/` layout** — package under `src/reclaim/` found by `setuptools.packages.find` with
  `where = ["src"]`.
- **Config in `pyproject.toml`** (no `setup.py`/`setup.cfg`).
- **Separate frontend package (Phase 4)** — `frontend/` is an independent Node/TypeScript package
  (Vite, its own `package.json` + committed `package-lock.json`, eslint/prettier/tsconfig files),
  **not** part of the Python build. Both apps are started together by `scripts/dev.sh` / `dev.ps1`.

### 8.2 Linting & formatting (Ruff)
- `line-length = 100`; `target-version = "py311"`; sources `["src", "tests"]`.
- **Selected rules:** `E` (pycodestyle errors), `F` (Pyflakes), `I` (isort/import sorting),
  `UP` (pyupgrade), `B` (bugbear), `SIM` (simplify), `RUF` (Ruff-specific).
- **Ignored:** `E501` (line length — handled by the 100 setting).

### 8.3 Typing (mypy, strict)
- `mypy` configured for `python_version = "3.11"`, package `reclaim`, **`strict = true`**,
  `ignore_missing_imports = true`, `warn_unused_configs = true`.
- The code uses `from __future__ import annotations`, PEP 604 unions (`str | None`), and `Mapped[...]`
  / `mapped_column` SQLAlchemy typing throughout.

### 8.4 Naming & structure patterns
- **Modules are single-purpose files** (one responsibility per module): `state_machine.py`,
  `stopping_rules.py`, `pipeline.py`, `act.py`, `webhook.py`, `sweep.py`, `verify.py`, `manual.py`.
  Phase 4 adds `api_views.py` (shared derived views) and `api_v1.py` (the JSON namespace) so the
  HTML and JSON surfaces stay separate yet can't drift. Phase 5 adds `audit_chain.py` (canonical
  hash-chain serialization), `verify_audit_chain.py` (read-only tamper check), `robustness.py`
  (multi-seed runner), `baseline.py` (counterfactual comparison), and `retry_simulator.py` (the
  shared retry-success model) — each a CLI entrypoint plus a console script.
- **Enums as `StrEnum`** with lowercase machine values (`"retry_now"`); the SPA mirrors these in
  `frontend/src/types.ts` as TS union types whose values are the contract.
- **Dataclasses** (`frozen=True`) for value/result objects (`RuleOutcome`, `ActResult`,
  `CaseOutcome`, `RuleSpec`).
- **Pydantic v2** for every boundary; `model_validator`/`field_validator` for cross-field and
  range invariants. The JSON API adds small request models (e.g. `SimulatorRunRequest`).
- **Logger naming** `logging.getLogger("reclaim.<module>")`, with structured log messages like
  `STATE_TRANSITION current=... trigger=... next=...`, `CASE_INGESTED ...`, `ACT_CLAIMED ...`,
  `WEBHOOK_REJECTED reason=...`, `SWEEP_STALE_ACTING case=...`, `DIAGNOSE_TRIAGED reason=...`,
  `MANUAL_APPROVE_RETRY case=...`, `VERIFY_SUBSCRIPTION case=...`.

### 8.5 Design patterns
- **State machine pattern** — the only way stage progress is recorded; terminal (absorbing) states;
  guarded transitions. ESCALATED is re-openable **only** via `manual=True` operator edges — never by
  the agentic pipeline.
- **"LLM proposes, code disposes"** — the Decide agent's output is always a *proposal*; a pure,
  unit-tested, prompt-independent enforcement layer (`stopping_rules.py`, **policy-as-code**) is
  authoritative. The declarative `RuleSpec` registry makes the policy itself introspectable (`/rules`).
- **Zero-halo** — required secrets fail loud at load/config time; never guess an API route or wire
  format (`RAZORPAY_RETRY_PATH`/`_SUBSCRIPTION`/`_SETTLEMENT` must be confirmed; `require_live_credentials`).
- **Idempotency as a DB constraint** — the `ExecutedActionRow` UNIQUE guard, not a promise; the
  reference semantics a distributed store must preserve (see README "Distributed idempotency").
- **Insert-then-catch dedupe** — `event_id` UNIQUE + `IntegrityError` handling is race-safe.
- **Adversarial-input triage** — `decline_code` is scrubbed of injection/malformed content before
  the LLM; the model's (hypothetical) response to hostile input can never influence the action.
- **Stale-lock reconciliation** — a periodic beat sweep recovers cases wedged in `ACTING`; escalation
  is a pure state change, so recovery after a crash stays human-authorized and idempotent.
- **Verification-only reads** — Subscriptions/Settlements lookups never block/reverse; a failure
  records, never crashes.
- **Three-way metrics** — `llm_call_failures` / `stopping_rule_overrides` / `stub_mode_actions`
  kept distinct; `fallback_triggered` means exactly "the LLM call itself failed".
- **Best-effort side channels** — audit writes and email are logged-and-swallowed so they never
  crash the money flow they document.
- **Deterministic seeding** — synthetic data derives from a seeded `random.Random`, and offline
  LLM shims are pure functions of their input (no RNG state), so demos/tests are hermetic and
  reproducible.
- **Eager-by-default async** — `RECLAIM_CELERY_EAGER=1` makes Celery synchronous for tests/demos;
  broker mode is opt-in (with a periodic beat schedule for the sweep).
- **WAL + busy timeout** — file-backed SQLite uses WAL for concurrent reader/writer support.
- **Parallel surface / shared views (Phase 4)** — the JSON API is a set of thin wrappers over the
  *same* tested functions the HTML routes call (never a reimplementation); `api_views.py` holds the
  derived views (simulator batch, customer status) both surfaces share, so HTML ⇄ JSON behavior
  cannot drift. `api.py` re-imports them so legacy tests resolve unchanged.
- **Query-layer pagination (Phase 4)** — `GET /api/v1/cases` filters + pages inside SQL
  (`repo.list_cases`) with bounded `limit`/`offset` (200 max; 422 outside bounds) — never a Python
  slice of the full set.
- **CORS, non-wildcard, local-demo default (Phase 4)** — the API allows only the Vite dev origins
  by default; the caveat ("serve SPA+API from one origin, or narrow the list") is flagged in code +
  `frontend/README.md`. The dev proxy (`/api` → `:8000`) removes the need for CORS in development.
- **Server-state, not client-state (Phase 4)** — the SPA uses TanStack Query (caching, dedup,
  auto-refetch after override mutations) deliberately *not* a Redux-like client store; manual
  overrides deliberately avoid optimistic UI (a terminal money-path transition — show pending, then
  let the authoritative detail refetch).
- **Hand-maintained TS contract (Phase 4)** — `frontend/src/types.ts` is kept in sync with the
  Python enums/shapes by hand (codegen deferred; the surface is small and the backend
  `test_api_v1.py` asserts the wire shapes, so drift surfaces there). Documented upgrade path:
  `openapi-typescript` if the API grows.
- **Tamper-evident audit log / post-write chain (Phase 5)** — the audit log carries a SHA-256 hash
  chain (`prev_hash` + `entry_hash` per row), but chain linkage is **not** computed at write time
  (a read-then-write sequence that would fork under concurrency). It is derived by
  `audit.finalize_audit_chain` in one **sequential** pass after the concurrent write phase; the
  verifier is pure read-only and reports the first broken link. An unfinalized log fails
  verification. Regression-tested under 8 real writer threads.
- **Honest disclosure / precision principle (Phase 5)** — every metric is labeled for what it
  proves vs. what needs external confirmation (`recovered_amount` = gateway call accepted, not
  settlement confirmed); the robustness suite reports the default batch's **percentile** rather
  than hiding it; the baseline uses a **conservative 85% chargeback** assumption so the naive
  strategy's net is not flattering.
- **Always-fresh counterfactual DB (Phase 5)** — `baseline.run_baseline_comparison` always runs on
  a fresh, file-backed temp DB (never an already-populated real DB, whose `UNIQUE(event_id)` would
  dedupe every seeded event to 0 calls). File-backed because `run_batch` crosses threads.
- **Structural LLM-isolation proof (Phase 5)** — an `ast`-based test statically proves
  `llm_client.py` does not import `razorpay_client.py` / `act.py`, guaranteeing no code path from
  the LLM to a money-moving execution outside the reviewed pipeline → rules → act flow.
- **Shared counterfactual simulation model (Phase 5)** — `retry_simulator.py` owns the
  per-decline-code retry-success probabilities so every strategy (and any future consumer) faces the
  same odds; strategies differ only in *which* cases they retry.
- **Console scripts (Phase 5)** — the four CLIs are declared in `pyproject.toml` `[project.scripts]`
  and each forces UTF-8 stdout (a cp1252 Windows console crashes on `₹`/`✓` otherwise).

### 8.6 Testing conventions
- Hermetic by construction: fixtures ignore the real `.env`, use per-test SQLite files, and run in
  `offline` + `stub` + eager modes.
- **160 backend tests across 19 files**, covering every domain boundary: webhook
  signature/parse/dedupe, state-machine legality (incl. manual edges), all seven stopping rules
  (incl. R7 floor + policy-as-code introspection), Pydantic invariants, synthetic generator
  invariants, end-to-end fallback/idempotency/state flows, metric-counter independence, the rule
  simulator, the manual override control plane, a **Phase 4 HTTP-layer suite for `/api/v1/*`**
  (status codes + wire shapes, overriding the api_v1 deps so the app lifespan never touches the
  real DB), a dedicated **adversarial-resilience** category (real threads + crash + network-drop +
  injection-triage), and the **Phase 5 statistical-rigor & structural-proof additions** — 31 tests
  across `test_audit_chain.py` / `test_baseline.py` / `test_robustness.py` / `test_llm_isolation.py`
  plus a 1-test concurrency regression in `adversarial/` (117 Phase 1–4 + 32 Phase 5 = **149**).
- **Frontend: 6 component tests across 2 files** (`frontend/`), Vitest + React Testing Library
  (jsdom), covering the logic-bearing components — the manual-override flow (buttons, pending
  disable, distinct 409 vs generic) and the simulator form (payload shape, comparison render).
  Static display-only components are not exhaustively covered; E2E is deliberately deferred
  (documented in `frontend/README.md`).
- **Layer separation:** the Phase 4 HTTP tests prove the JSON boundary, not business logic already
  proven by the unit tests — no duplicated coverage; the Phase 5 tests likewise prove the new
  modules' own logic (chain integrity, counterfactual math, distribution stats, structural
  isolation) without reaching into Phase 1–4 behavior.

### 8.7 Documentation & repository conventions
- `README.md` — primary human-facing documentation: trust-boundary diagram (with a Phase 5 caption
  noting the structural LLM-isolation guarantee is *test-verified* via the import graph), "LLM
  proposes / code disposes" + R1–R7, three non-conflated metrics, a **Phase 5 "Precision principle"
  section** (what each metric proves vs. what needs external confirmation), Failure Injection &
  Resilience, verification-only integrations, provenance, security, distributed-idempotency design
  note, and "What Broke and How We Fixed It". (Note: some test-count references still read 99 —
  they lag Phase 4/5; `PROGRESS.md` is the live authority.)
- `baseline-analysis.html` — a standalone, self-styled Phase 5 write-up that documents the
  counterfactual comparison: why the old 100%-success model was unrealistic, the per-decline-code
  success rates, the 85% chargeback assumption, and the resulting strategy table.
- `PROGRESS.md` — a living build-progress journal (done / decisions + why / in progress / next)
  maintained at session start and after each step (per the project's documented convention).
  Phase 5 (including the post-submission real-batch bug fixes and the 145→149 count correction) is
  logged here.
- `DECISIONS.md` — a running log of every non-trivial architecture decision and why (SQLAlchemy,
  Upstash, offline by default, each stopping rule's threshold + rationale, WAL, economic floor,
  policy-as-code, provenance, sweep, verification-only reads, the metrics split — and Phase 5's
  post-write chain-finalize over compute-at-write, and the always-fresh baseline DB).
- `CHANGELOG_SUBMISSION.md` — a dated, phase-level log (Phase 1 core pipeline, Phase 2 UI + real
  API deepening, Phase 3 hardening + differentiation), distinct from raw git noise. (Phase 4/5
  entries can be found in `PROGRESS.md`.)
- `frontend/README.md` — SPA-specific docs: stack, page table, two-terminal + `scripts/dev.*`
  setup, the **CORS caveat (local-demo default only)**, testing rationale, and the codegen-vs-
  hand-maintained-types decision.
- `tasks.md` — built vs. explicitly out-of-scope (the Track 3/4 boundary decisions).
- `.env.example` ships placeholders only; real secrets are never committed.
- `.gitignore` keeps `.env`, `*.db`, caches, `.venv/`, and `.claude/` out of the repo;
  `frontend/.gitignore` keeps `node_modules/` and `dist/` out.
- Remote `main` branch at `https://github.com/Utkarshkarki/Salvage.git`.

---

## Appendix — File inventory summary
| Path | Role |
|------|------|
| `src/reclaim/` | Entire application (31 modules) |
| `tests/` | 160 backend tests across 19 files (incl. `tests/adversarial/`, 12 tests, `test_api_v1.py`, 18 tests, `test_tasks.py`, 3 tests, and the Phase 5 rigor/isolation files) |
| `frontend/` | Phase 4 React SPA — 6 component tests across 2 Vitest files |
| `scripts/` | `dev.sh` / `dev.ps1` — launch backend + Vite together |
| `pyproject.toml` | Python build + deps + pytest/ruff/mypy config + the 4 Phase 5 `[project.scripts]` |
| `frontend/package.json` (+ lock) | Node SPA deps + scripts |
| `baseline-analysis.html` | Phase 5 counterfactual-comparison write-up |
| `.env.example` | Env template (placeholders) |
| `README.md` / `PROGRESS.md` | Docs + build journal (Phase 5 logged in PROGRESS) |
| `DECISIONS.md` / `CHANGELOG_SUBMISSION.md` / `tasks.md` | Submission artifacts (decisions / phase log / scope map) |
| `LICENSE` | MIT |
| `.gitignore` (+ `frontend/.gitignore`) | Tailored ignore rules (root + SPA, incl. `.db-backup-*/`) |
| `reclaim.db` | Dev SQLite database (runtime artifact, gitignored) |

**Module inventory (`src/reclaim/`, 31 files):**
`__init__`, `act`, `api`, `api_v1`, `api_views`, `audit`, `audit_chain`, `batch`, `baseline`,
`celery_app`, `config`, `db`, `dispatcher`, `email`, `llm_client`, `manual`, `metrics`, `models`,
`pipeline`, `razorpay_client`, `repo`, `retry_simulator`, `robustness`, `state_machine`,
`stopping_rules`, `sweep`, `synthetic`, `tasks`, `verify`, `verify_audit_chain`, `webhook`.

**Backend test inventory (`tests/`, 149 tests / 18 files):**
`test_api_v1` (18), `test_audit_chain` (11), `test_baseline` (10), `test_llm_isolation` (3),
`test_manual` (5), `test_metrics` (5), `test_models` (9), `test_pipeline` (9), `test_robustness` (7),
`test_simulator` (4), `test_state_machine` (14), `test_stopping_rules` (20), `test_synthetic` (9),
`test_webhook` (13), `adversarial/test_audit_chain_concurrency` (1),
`adversarial/test_concurrency` (3), `adversarial/test_llm_adversarial` (4),
`adversarial/test_sweep` (4), plus `conftest` fixtures.

**Frontend test inventory (`frontend/`, 6 tests / 2 files):**
`src/components/ManualActions.test.tsx` (4), `src/pages/Simulator.test.tsx` (2).

---

*Documented and verified in-session on 2026-09-02. Backend suite: **149 passed** (18 files) +
frontend suite: **6 passed** (2 Vitest files). This is a Phase-5 refresh of the earlier
"117 tests / 13 files" inventory (itself a refresh of "99 tests / 12 files" and "68 tests /
7 files"); all counts, modules, routes, config fields, frontend files, and tests were re-verified
against the current source before writing.*