# Reclaim — Build Progress

> Read this file first at the start of every session before doing anything else.

**Status:** Phases 1–5 complete + post-submission hardening (independent-review fixes round). Backend **160 tests / 19 files, all passing** (29.1s full suite); frontend 6/2 untouched. Phase 5 statistical-rigor tooling (multi-seed robustness, controlled counterfactual baseline, tamper-evident audit chain) was found by an independent review to under-deliver — the robustness suite measured one repeated sample (stddev 0) and the baseline's "same world, different policy" was an RNG draw artifact — and is now fixed with regression tests that actually assert seeds differ and strategies share per-case draws. (A1 ngrok/webhook registration still needs the operator's real Razorpay secret — live paths are env-gated and documented.)

## What's done

1. **Pydantic schemas** — `src/reclaim/models.py`: `RecoveryCase` (+`state`), `DiagnoseOutput`, `DecideOutput` (cross-field validator: `scheduled_at` required iff `retry_scheduled`, future-only), `AuditLogEntry` (`fallback_triggered`), `WebhookEvent`.
2. **Explicit state machine** — `src/reclaim/state_machine.py`: guarded transition table, terminal-absorbing, `DECIDED→RESOLVED` only via `via_stop`. Tests in `tests/test_state_machine.py`.
3. **Synthetic batch generator** — `src/reclaim/synthetic.py`: 60 valid + 6 duplicate + 7 rejection deliveries, seeded, all causes represented, above-threshold amounts + long-gap cases.
4. **Webhook signature + dedupe** — `src/reclaim/webhook.py`: constant-time HMAC-SHA256, missing header = hard reject, UNIQUE(event_id) dedupe (insert-then-catch, race safe).
5. **Pipeline orchestrator** — `src/reclaim/pipeline.py`: `run_case` (Diagnose→Decide→enforce→route→Act→Log) + `run_batch` under a `ConcurrencyLimiter` cap + LLM exponential backoff.
6. **Stopping-rule enforcement layer (code, not prompt)** — `src/reclaim/stopping_rules.py`: R1 mandate→escalate, R2 amount→escalate, R3 age→escalate, R4 attempts→stop, R5 email cap→escalate, R6 cooldown→schedule. LLM proposes, code disposes.
7. **Act layer** — `src/reclaim/act.py` (idempotency ledger via `ExecutedActionRow` UNIQUE guard), `razorpay_client.py` (stub/live + idempotency key), `email.py` stub.
8. **Audit writer + read endpoints** — `src/reclaim/audit.py` (append-only), `repo.py` (read helpers), `api.py`: `GET /cases/{id}`, `/dashboard` (HTML), `/metrics`.
9. **Batch-run script** — `python -m reclaim.batch` + `metrics.py` (recovery %, ₹ recovered, cause breakdown, stopped/escalated cases).
10. **Tests** — 63 passing across `test_stopping_rules.py` (13), `test_webhook.py` (13), `test_state_machine.py` (10), `test_models.py` (9), `test_synthetic.py` (9), `test_pipeline.py` (9).
11. **Unambiguous metrics (this session)** — killed three conflations a judge would flag:
    - Split the monolithic "Deterministic fallbacks" counter into **`llm_call_failures`** (LLM actually failed → deterministic default), **`stopping_rule_overrides`** (rule R1–R6 overrode a *valid* LLM proposal, broken down by rule), and **`stub_mode_actions`** (ACT_MODE=stub execution — not a fallback at all). In `pipeline.py` the old `fallback_triggered=(de.fallback_triggered or rule.overridden)` conflation became `llm_failure` vs `stopping_rule_override` as independent `CaseOutcome` fields; audit `fallback_triggered` now means LLM failure only.
    - "Cases that did NOT loop" renamed to **`cases_resolved_without_retry`** with an explicit definition in code: stopped (decision=stop, no side effect) + escalated (human review) — i.e. cases that took no retry action.

## Decisions made (and why)

- **`LLM_MODE=offline` / `ACT_MODE=stub` / eager Celery are the demo defaults** (user confirmed). Real online Ollama + live Razorpay clients are fully implemented but gated behind those env flags — hermetic tests and demo never need network or credentials.
- **Deterministic state machine with narrow LLM workers, NOT a free-form agent** (build mandate): money moves are explainable, bounded, gated. The LLM *proposes*, the code-enforced stopping rules *dispose*.
- **`offline_decide` is a deliberately naive proposer** (almost always `retry_now`) so the stopping-rule overrides are visible and exercised in the batch.
- **Payment history anchored at ingest** from the webhook `created_at` so the 24h cooldown reflects the real retry gap (was always 0 → every retry would've been clamped to scheduled, collapsing recovery rate).
- **Idempotency = DB UNIQUE constraint**, not just a promise: duplicated Act calls claim-fail and are logged no-ops; can never double-charge.
- **Live Razorpay retry path is NOT hardcoded** (`razorpay_retry_path=""`) — the exact endpoint must be confirmed against current docs; live mode refuses rather than guess an API shape (ZERO-HALO).
- **Removed the hardcoded Upstash Redis credential from `config.py`** (was a real secret in source). Now `redis_url` defaults to a local placeholder; real broker comes from `.env`.
- **`fallback_triggered` in the audit log now means ONE thing: the LLM call itself failed** (diagnose or decide). Stopping-rule overrides are recorded separately in the decide outcome string (`OVERRIDE` + `rule=<R#>`), NOT folded into the fallback flag. This is what lets the metrics separate "LLM was down" from "LLM proposed something unsafe and code rejected it" — two very different stories to a stakeholder.
- **`stats.stub_mode_actions` counts only cases that actually executed a side-effecting action** through the stub; `Action.STOP` cases resolve with no side effect and are excluded (60 total − 5 stopped = 55 stub executions in the demo).

12. **GitHub upload (this session)** — connected local project to `https://github.com/Utkarshkarki/Salvage.git` and pushed to `main`. Initialized local repo, renamed branch `master→main`, committed the full project, then joined the remote's initial commit via `git merge origin/main --allow-unrelated-histories` (kept the project's tailored `.gitignore` and replaced the placeholder README with a real one). Verified `.env` (real Upstash/Razorpay/webhook secrets) is **not** tracked — only `.env.example` is committed. History: `684b6b2` (project) ⇄ merge `c1357a5` ← `9ddeeea` (remote initial). Committed README includes the regenerated demo batch metrics (₹39,776 recovered / 20.7%).
13. **PROJECT_MASTER.md (this session)** — generated a comprehensive, production-grade engineering reference (`PROJECT_MASTER.md` at repo root) that documents every file, class, function, schema, and interaction in the codebase: project overview, tech stack/deps (declared + verified installed versions), ASCII architecture tree, per-module breakdown, data models/schema, system data flow, setup/commands, and dev conventions. Verified the current test suite is **68 tests across 7 files** (including the 5 in `test_metrics.py`), all passing — the README/PROGRESS "63 tests / 6 files" figure was stale; that drift is flagged in PROJECT_MASTER.md §1 and the Appendix so it doesn't silently propagate.

## Phase 2 — Track 3 deepening (this build)

Build order follows the Phase 2 prompt. Scope guardrail honored: no RazorpayX treasury
automation (Track 4), no Route multi-party splitting (Track 1). No new frontend framework —
everything stays server-rendered FastAPI/Jinja2/hand-built HTML.

14. **B1: Rule Sensitivity Simulator (`/simulator`)** — form exposing the 6 editable stopping-rule
    thresholds (`escalation_amount_threshold`, `escalation_days_threshold`, `max_retries_per_cycle`,
    `cooldown_hours`, `email_cap_per_7d`). On submit, re-runs the SAME seed-42 synthetic batch on a
    throwaway DB (temp file — a shared in-memory engine can't cross the thread pool) under a
    settings copy with the submitted overrides (real settings never mutated), then shows a
    before/after comparison via `metrics.compute_metrics`. Reuses `pipeline.run_batch` +
    `metrics.compute_metrics` + a new shared `batch.ingest_batch` helper (extracted so the batch CLI
    and simulator exercise the exact same webhook boundary). Baseline reproduces the documented
    demo (0.2069 / ₹39,776 / 23 escalated / 5 stopped); lowering the amount threshold visibly pushes
    escalations up. Tests: `tests/test_simulator.py` (4).
15. **B2: Manual override action buttons** — on `/cases/{case_id}` for ESCALATED cases: "Approve
    manual retry" → `POST /cases/{case_id}/approve_retry` (ESCALATED→ACTING→RESOLVED/FAILED, retry
    executed via the idempotent Act layer) and "Mark resolved by human" → `POST
    /cases/{case_id}/resolve_human` (ESCALATED→RESOLVED directly). Guarded through the state machine
    (new scoped `manual=True` edges — the only way out of the terminal ESCALATED state, never the
    agentic pipeline); each writes `stage="manual_override"`, `agent_reasoning="manual override by
    operator"` audit entries clearly distinct from LLM decisions. Illegal actions → 409. New
    `src/reclaim/manual.py`. Tests: `tests/test_manual.py` (5) + 4 state-machine cases.
16. **B3: Customer status page (`/status/{case_id}`)** — plain-language, customer-safe view read from
    the SAME case/audit data as the merchant dashboard (no separate model). Maps the Diagnose cause
    to a friendly message; renders next-step incl. "we'll retry on <date>" (now persisted on the
    decide audit entry's `input_state` — minimal additive pipeline change). No rule IDs, stage
    names, or LLM/fallback jargon. Tests exercise the plain-language view + no-jargon check.
    **Flagged tradeoff:** currently addressed by raw `case_id` (guessable); a production/public
    deployment should key it on a non-guessable per-case share token (deferred, no schema migration
    this phase).

## In progress (Phase 2)

- Paused at the **external-dependency checkpoint** before A1/A2/A3. A1 needs the REAL Razorpay
  webhook secret (from the Dashboard after ngrok registration) and the Razorpay test-mode keys for
  A2/A3 — never guessed, never the demo secret.

## What's next / Phase 2 remaining

- **A1** — ngrok + Razorpay Dashboard webhook registration + mode-switch docs/config (real secret
  required from user). STILL REQUIRES THE OPERATOR's real secret/test keys — never guessed.
- **A2** — ~~Subscriptions API verification of `mandate_revoked`~~ **DONE in Phase 3**:
  `razorpay_client.subscription_status` + `verify.verify_subscription_status`, verification-only,
  fault-isolated, env-gated (`RAZORPAY_SUBSCRIPTION_PATH`, ZERO-HALO default empty).
- **A3** — ~~Settlements API reconciliation after `retry_now` recovery~~ **DONE in Phase 3**:
  `razorpay_client.settlement_reconciliation` + `verify.verify_settlement_reconciliation`,
  verification-only (never blocks/reverses), Track 3 framing only (not RazorpayX treasury ops).
- Run `python -m reclaim.batch` with `RECLAIM_FRESH=1` to regenerate demo metrics; start the API
  (`uvicorn reclaim.api:app`) and open `/dashboard`, `/simulator`, `/status/{case_id}`, `/rules`.

---

## Phase 3 — Competitive hardening (this build)

Follows the Phase 3 prompt. Worked in the mandated build order; asked where the guardrail
required it (the policy-as-code refactor touches tested R1–R6 — flagged, behavior preserved,
verified by the full suite). **Test count: 81 → 99, all passing.**

17. **Section 1 — Adversarial resilience tests** (`tests/adversarial/`, 11 tests):
    - `test_concurrent_duplicate_webhooks` — same event_id fired from 8 threads => exactly one
      ingest (event-id UNIQUE) + exactly one execution (ExecutedActionRow ledger).
    - `test_sweep_*` — mid-pipeline crash recovery: new `reclaim/sweep.py` finds `ACTING` cases
      past `STALE_LOCK_TIMEOUT_SECONDS` and reconciles them to `ESCALATED`; never touches
      in-progress ones.
    - `test_network_drop_idempotency_intercepted` — a retry that may have succeeded server-side
      is intercepted on retry by the same idempotency key; no double-execute.
    - `test_injection_marker_triaged_before_llm` / `test_malformed_control_chars_triaged` —
      adversarial Diagnose-input triage short-circuits before the model; deterministic fallback
      governs; deliberately NOT counted as an LLM failure.
    - `test_concurrent_read_write_does_not_corrupt` / `test_concurrent_pipeline_is_idempotent_via_ledger`.
18. **Section 2.1 — WAL mode** — `db.build_engine` enables `journal_mode=WAL`,
    `synchronous=NORMAL`, + busy timeout on file-backed SQLite; in-memory skipped. No test regressions.
19. **Section 2.2 — Economic floor rule (R7)** — amount below `MIN_RECOVERY_AMOUNT` (₹100,
    env-configurable) => `STOP` (never auto-retry). Added to the declarative registry at priority 2
    (below R1 mandate-safety). Tests include precedence (R1 wins) + threshold-configurability.
20. **Section 2.3 — Stale-lock sweep as a Celery periodic task** — beat schedule entry
    `reclaim-sweep-stale-acting` every 5 min → `reclaim.tasks.sweep_stale_acting_task`; test
    verifies the schedule + task name.
21. **Section 2.4 — Distributed idempotency design note** — README § "Distributed idempotency":
    contract to preserve (claim-before-dispatch, win-once, TTL-bounded), NOT implemented.
22. **Section 3.1 — Metrics differentiation documented** — README "why this matters" paragraph.
23. **Section 3.2/3.4/3.5 — Simulator, consumer page, override controls** — CONFIRMED done in Phase 2.
24. **Section 3.3 — Real Razorpay integration depth** — added `subscription_status` +
    `settlement_reconciliation` (verification-only, fault-isolated, env-gated paths) + `verify.py`.
    The Payments retry path was already wired (stub/live + idempotency key).
25. **Section 3.6 — Policy-as-code stopping rules** — R1–R7 as a declarative `RuleSpec` registry
    (id, priority, plain-language description, pure condition, forced action); new `GET /rules`
    page renders live policy. **FLAG:** this refactored the tested R1–R6 `enforce()` into a
    data-driven form; behavior preserved (first-match-wins, priority order) and verified by the
    full suite. Additive R7 included from the start.
26. **Section 3.7 — LLM provenance logging** — `input_state.llm_provenance` (model, `*-v1` prompt
    version, prompt content hash, mode) on every Diagnose/Decide audit entry; README note.
27. **Section 4 — Submission artifacts** — `DECISIONS.md`, `CHANGELOG_SUBMISSION.md`, `tasks.md`,
    README "Failure Injection & Resilience" (1.5), trust-boundary diagram (4.4),
    "What Broke and How We Fixed It" (4.3), expanded API surface. **PROGRESS.md** updated.

### Guardrail note (asked before touching)
The policy-as-code refactor (3.6) rewrote `stopping_rules.enforce()` which the Phase 2 tests
cover (R1–R6). This was the single change to already-tested core logic; it was done as a
behavior-preserving, data-driven rewrite (full suite green, 99/99) rather than an inline reorder.

---

## Phase 4 — Production-grade React frontend (this build)

Part A (backend `/api/v1/*` JSON namespace) plus a Vite + React + TypeScript SPA in `frontend/`.
Strictly additive to Phases 1–3: the state machine, 7 stopping rules (R1–R7), the pipeline, the
LLM client, the webhook boundary, the persistence layer, and all 99 pre-Phase-4 tests were left
untouched. **Backend test count: 99 → 117, all passing** (the 18 new `tests/test_api_v1.py` are
HTTP-layer-only — they don't re-prove business logic already covered by `test_manual.py`,
`test_simulator.py`, etc.).

### Part A — `/api/v1/*` JSON namespace

New `src/reclaim/api_v1.py` (mounted in `api.py` below the CORS middleware) — a **parallel
surface** to the HTML routes, each endpoint a thin wrapper over the SAME tested function the
corresponding HTML route calls (never a reimplementation):

- **A1** `GET /api/v1/cases` — filtered + paginated at the SQL query layer (new
  `repo.list_cases`, not a Python slice), `state` filter, `limit`/`offset` bounded (422 outside
  bounds).
- **A2** `GET /api/v1/cases/{case_id}` — the exact `case_detail(fmt=json)` payload shape plus the
  richer audit entries (`agent_reasoning` + `input_state` carrying `llm_provenance`).
- **A3** `GET /api/v1/metrics` — wraps `metrics.compute_metrics` as-is (full 17-key shape).
- **A4** `GET /api/v1/rules` — wraps `stopping_rules.describe_rules` (R1–R7 with live values).
- **A5** `POST /api/v1/simulator/run` — calls the shared `_run_simulated_batch` (seed-42 batch on
  a throwaway temp-file DB under a settings *copy*; real settings never mutated — verified by a
  dedicated test). `_SIM_THRESHOLD_FIELDS` / `_run_simulated_batch` / `_sim_metric_key`,
  `_CAUSE_PLAIN` / `customer_view` were **moved to a new `src/reclaim/api_views.py`** so the HTML
  and JSON surfaces share one implementation and can't drift (`api.py` re-imports them so
  `tests/test_simulator.py` etc. resolve unchanged).
- **A6** `POST /api/v1/cases/{id}/approve_retry`, `/resolve_human` — thin wrappers over
  `manual.py`; return updated case JSON (A2 shape), 404 unknown case, 409 not-ESCALATED
  (`IllegalTransitionError`), matching the HTML routes' error semantics sans redirect.
- **A7** `GET /api/v1/status/{case_id}` — the shared `customer_view` as `{heading, reason,
  next_step}`, 404 if unknown.
- **A8** CORS — middleware allows the Vite dev origins only (`http://localhost:5173`,
  `http://127.0.0.1:5173`, from `RECLAIM_CORS_ORIGINS` in `config.py`). Not a wildcard; the
  production caveat (serve SPA+API from one origin, or narrow the list to the deployed origin) is
  flagged loudly in code + `frontend/README.md`.
- **A9** `tests/test_api_v1.py` — 18 HTTP-layer tests: happy paths, state filter, pagination
  bounds (422 on out-of-range), A2 provenance in the trail, A3/A4 shapes, A5 does-not-mutate-
  real-settings, A6 404/409, A7 404 + no-jargon.

### Part B — React SPA (`frontend/`)

Built in the mandated order: B1–B4 foundation first, then the five pages, cross-cutting B6 applied
per-page, then B7 tests, then Part C.

- **B1 Scaffolding** — `npm create vite` react-ts shape; TypeScript **strict** (`noUnusedLocals`,
  `noUncheckedIndexedAccess`), ESLint (`typescript-eslint` + react-hooks/refresh) + Prettier,
  Tailwind CSS. Design tokens centralized in `tailwind.config.ts` (diagnose=blue, decide=purple,
  act=teal, override=amber, recovered=green, escalated=red, LLM-failure-fallback=orange) — no
  raw hex scattered in components.
- **B2 Types** — hand-maintained `src/types.ts` mirroring the Python enums/fields exactly
  (decision + rationale documented: the surface is small/stable and `test_api_v1.py` asserts the
  wire shapes, so codegen's added build-time + run-live dependency buys nothing now; openapi-
  typescript is the documented upgrade path if the API grows).
- **B3 Data fetching** — TanStack Query (server-state: caching, dedup, auto-refetch of the case
  detail after an override mutation invalidates) + a single typed `apiClient` (`fetch` wrapper:
  base URL, `ApiError` with status + detail, JSON). Justified against the Phase 2 "avoid heavy
  state management" guidance — this is server state, not a client store (no Redux).
- **B5.1 Case List** — state filter, pagination (query-layer, stable ordering via `repo.list_cases`),
  click-through, skeleton loading, explicit empty state, keepPreviousData across pages.
- **B5.2 Case Detail** — full audit trail as distinct stage blocks (badge, decision, outcome chip,
  override tag when outcome contains `OVERRIDE`, fallback tag when `fallback_triggered`,
  provenance); override buttons (Approve manual retry / Mark resolved by human) rendered only for
  ESCALATED, disabled while pending, A6 results invalidate the query, and the **409 case is
  surfaced distinctly** ("already resolved elsewhere") instead of a generic failure.
- **B5.3 Simulator** — editable threshold form → A5, before/after rendered as CSS-based bar
  comparison (no charting library for a single view).
- **B5.4 Rules** — A4 rendered as the live "what governs this system" page.
- **B5.5 Customer Status** — routed OUTSIDE the app Layout (no dashboard chrome), plain-language
  A7 data, no rule ids / stage names / jargon.
- **B6 cross-cutting** — route-level `ErrorBoundary`, loading + error states on every fetcher,
  semantic HTML + heading hierarchy + aria-labels + visible focus rings, responsive (min-width
  tables scroll in `overflow-x-auto`, no horizontal page overflow), `React.lazy`+`Suspense` route
  code-splitting (confirmed in the build: separate chunks per page), no browser console
  errors/warnings.
- **B7 Tests** — Vitest + React Testing Library, **6 tests / 2 files**: the override-action flow
  (buttons render, disabled while pending, distinct 409 vs generic error) and the Simulator form
  (correct payload shape, comparison renders on success). E2E (Playwright/Cypress) deliberately
  **not** added — trade-off documented in `frontend/README.md` (small local demo, read-mostly,
  logic already covered at component + backend-HTTP layers; worth adding a single smoke spec only
  when there's a real deployment target + CI).

### Part C — wiring & verification

- `scripts/dev.sh` (bash) + `scripts/dev.ps1` (Windows) start `uvicorn` and the Vite dev server
  together; two-terminal instructions also in `frontend/README.md`.
- `npm run build` → **clean production bundle, no errors** (verified; 95 modules, per-route chunks,
  `dist/` produced). `npm run lint` clean (0 errors), `npm test` green.
- The Vite dev server proxies `/api → :8000` so no CORS in dev; the built app uses relative URLs.
  The CORS/dev-default caveat is documented in `frontend/README.md`, not presented as deployment-ready.

### Phase 4 files & test total

Backend: `src/reclaim/api_v1.py` (new), `src/reclaim/api_views.py` (new), `tests/test_api_v1.py` (new),
`repo.py` `+list_cases`, `config.py` `+cors_origins`. Frontend: `frontend/**` (Vite + React + TS SPA).
**Full backend suite: 117 collected across 13 files, all passing.** Frontend: 6 tests / 2 files,
production build + lint clean.

---

## Phase 5 — Statistical Rigor & Structural Proof (this build)

Closes specific gaps identified in comparable Track 3 submissions: multi-seed robustness reporting,
counterfactual baseline comparison with conservative assumptions, tamper-evident audit logging,
explicit precision principle in documentation, and structural LLM-isolation verification.
**All additive to Phases 1–4: state machine, R1–R7 rules, pipeline, webhook boundary, persistence,
99 tests, 6 frontend tests — unchanged and untouched.**

### Part A — Multi-seed robustness reporting

**A1** `src/reclaim/robustness.py` — new module: `run_robustness_suite(num_seeds=100)` runs the
batch across N independently seeded synthetic batches, collecting `recovery_rate`, `recovered_amount`,
`amount_at_risk` per run. Reuses existing `synthetic.py` generator — no duplication. CLI entrypoint:
`python -m reclaim.robustness [num_seeds]`.

**A2** Distribution reporting — computes and prints: median, 5th and 95th percentile, standard
deviation of recovery rate and recovered amount across N runs.

**A3** Headline-batch disclosure — identifies where the default demo batch (seed 42) falls within
the distribution (e.g., "17th percentile — below median"). Reports this explicitly rather than
omitting it — **honest disclosure, not a flattering number**. Added to metrics output via new
`headline_batch_percentile` field.

**A4** `tests/test_robustness.py` — 7 tests (planned 8; see count correction above):
runner produces N independent results (different seeds), percentile/stddev math correct
against known small distribution, CLI entry point runs offline, dataclass shapes.

### Part B — Counterfactual baseline comparison

**B1** `src/reclaim/baseline.py` — two naive strategies run against the same seeded batch:
- `do_nothing`: 0 gateway calls, 0 recovered.
- `retry_everything`: one call per case, no stopping rules applied.
- Plus the real `reclaim` policy for side-by-side comparison.

**B2** Comparison metrics — for each strategy: gateway calls made, gross amount recovered,
**policy-blocked value** (cases that real policy would have blocked), and **net recovered after
assumed chargeback cost**. Conservative assumption explicitly documented: 85% chargeback rate for
retries that real policy would have blocked.

**B3** Output — comparison table via CLI (`python -m reclaim.baseline [seed]`) showing: Gateway Calls,
Gross Recovered, Policy-Blocked Value, Net Recovered. Includes comparative analysis: call reduction %,
net economic advantage.

**B4** `tests/test_baseline.py` — 10 tests (planned 12; `_identify_policy_blocked_cases`
removed in the fix round, so its test was dropped): `do_nothing` recovers ₹0,
`retry_everything` makes one call per case, real policy's calls ≤ `retry_everything`,
chargeback-netting math correct.

### Part C — Hash-chained audit log

**C1** Schema extension — additive migration: `AuditLogRow` adds `prev_hash` (previous entry's hash,
64-char hex) and `entry_hash` (this entry's hash, 64-char hex). No existing columns altered.
First entry in chain uses genesis hash `"0" * 64`.

**C2** Write-path integration — `audit.py` updated transparently: `write_audit` computes entry hash
at write time via `_compute_entry_hash(prev_hash + canonical_json)`. External signature unchanged —
all existing callers remain unchanged; hash computation is internal.

**C3** Verification — new `src/reclaim/verify_audit_chain.py` module: `verify_audit_chain(session)`
walks full audit log in order, confirms every entry's `entry_hash` matches fresh recomputation from
`prev_hash` + content. Exits non-zero and reports first broken link if verification fails.
CLI: `python -m reclaim.verify_audit_chain`.

**C4** `tests/test_audit_chain.py` — 11 tests: healthy chain verifies clean, mutating any field
breaks verification from that point, fresh empty log verifies trivially, hash computation
deterministic, hash dataclass shapes.

### Part D — Explicit precision principle in documentation

**D1** README section (new) — added "Precision principle: what each metric actually measures"
after the "Three metrics" section. Explicitly states:
- `recovered_amount` proves a gateway call succeeded, NOT that settlement confirmed.
- `verification_enabled` and verification reads are **non-blocking observational lookups** — never
  change case outcome, only record external state for audit.
- `stopping_rule_overrides` auditable proof that policy-as-code rejected naive proposals.
- `llm_call_failures` means "the LLM was down", availability not safety.

**D2** Metrics field audit — updated docstrings in `src/reclaim/metrics.py`:
- `compute_metrics` docstring clarified: every metric field is documented for what it proves vs.
  what requires external confirmation.
- `recovered_amount` inline comment: "This proves Reclaim made a call; see verification_enabled
  setting for settlement confirmation."
- All returned fields carry precision guidance in comments.

### Part E — Structural LLM-isolation test

**E1** Import-boundary test — `tests/test_llm_isolation.py`: static-analysis test (parse `llm_client.py`
via `ast` module) asserts `llm_client.py` does NOT import `razorpay_client.py` or `act.py` directly,
proving structurally that the LLM module has no code path to execute money-moving actions except
through the reviewed `pipeline.py → stopping_rules.py → act.py` flow.

**E2** Trust-boundary documentation update — README trust-boundary diagram and caption now include:
"This structural guarantee is verified by a test that inspects the import graph. The LLM can only
propose; execution is isolated."

### Phase 5 files & test total

Backend new files:
- `src/reclaim/robustness.py` — multi-seed runner
- `src/reclaim/baseline.py` — counterfactual comparison
- `src/reclaim/verify_audit_chain.py` — audit chain verification
- `db.py` `+prev_hash, +entry_hash` columns on `AuditLogRow`
- `audit.py` — hash computation at write time (transparent to callers)
- `metrics.py` — enhanced docstrings for precision principle

Test files:
- `tests/test_robustness.py` (8 tests)
- `tests/test_baseline.py` (12 tests)
- `tests/test_audit_chain.py` (11 tests)
- `tests/test_llm_isolation.py` (3 tests)

**CORRECTED (2026-09-01): real count is 149, not 151.** The Phase 5 files hold 31
tests, not 34 (test_baseline.py has 10, not 12; test_robustness.py has 7, not 8;
the other two files match their plan): 8+12+11+3 planned → 7+10+11+3 actual = 31.
Plus 1 regression test in `tests/adversarial/test_audit_chain_concurrency.py`
(from the post-submission fix) = 32 new tests since Phase 4's 117. **117 + 32 = 149.
Full suite: 149 collected, 149 passing (verified live).**

Documentation updates:
- README: "Precision principle" section added
- README: trust-boundary diagram caption updated
- `metrics.py`: all metric fields document what they prove vs. what needs external confirmation
- PROGRESS.md: Phase 5 completion logged

---

## Phase 5 — Post-submission bug fixes (two real-batch bugs)

Both surfaced only when the Phase 5 CLI tools ran against REAL batch data
(concurrent writes + an already-populated DB), not the minimal test fixtures.
**Backend tests: 145 → 149, all passing** (measured via the full suite; +4 new
regression tests).

28. **Baseline retry_everything reported 0 calls / ₹0 (Bug 1)** — root cause:
   `run_baseline_comparison` ingested the seeded batch into ``get_db()``, i.e.
   the REAL already-populated ``reclaim.db``; `ingest_event` deduped all 60
   seeded events on UNIQUE(event_id) → `case_ids` empty → 0 calls / ₹0, while
   `reclaim` still read pre-existing recovered data (the tell-tale mismatch).
   Fix: the comparison now ALWAYS runs on a fresh, file-backed temp DB
   (isolated from and never mutating the real DB — same rationale as the
   `/simulator`; file-backed because `run_batch` crosses threads). Verified
   live: `retry_everything` = **60 calls / ₹192,246 gross**. Regression test
   builds a real populated DB (real synthetic batch + full pipeline), then runs
   baseline against it and asserts 60 calls / ₹>0.
29. **Audit chain reported broken at entry N right after a fresh batch (Bug 2)** —
   root cause: a read-then-write race. `write_audit` read "the most recent row
   by id" and inserted; under `ConcurrencyLimiter(max_concurrency=5)` two
   writers can both read the same stale latest row → two entries both linking
   the same predecessor → verification (which requires `row[i].prev ==
   row[i-1].entry_hash`) reports a fork as a break. **Chosen fix: approach (b)**
   — the chain is no longer computed at write time at all; it is derived by a
   single SEQUENTIAL pass (`audit.finalize_audit_chain`, ordered by autoincrement
   id) that runs once after the concurrent write phase, so there is no
   read-then-write sequence to race. DB-agnostic (works under Postgres, no
   SQLite `BEGIN IMMEDIATE`), keeps batch concurrency untouched, and preserves
   tamper-evidence: `verify_audit_chain` stays pure read-only (recomputes +
   compares, never writes). `batch.py` (and baseline) finalize after `run_batch`;
   an unfinalized log now correctly FAILS verification (empty hashes). Regression
   test in `tests/adversarial/test_audit_chain_concurrency.py` writes via the REAL
   write path from 8 threads and asserts the finalized chain verifies clean.
   Verified live on the fresh DB: **✓ Chain is valid (235 entries)**.
30. **Windows console crash on CLI output (discovered while demo-run)** — both
   Phase 5 CLIs printed `₹`/`✓` and crashed on a cp1252 console
   (`UnicodeEncodeError`). Fixed by `sys.stdout.reconfigure(encoding="utf-8",
   errors="replace")` in each CLI `__main__` so the tools never crash on output.
31. **Collection-blocking import fixed + full-suite + CLI verification (2026-09-01)** —
   `tests/test_baseline.py` still imported `_identify_policy_blocked_cases`, the
   function removed during fix #4 (replaced by inline logic). This raised an
   ImportError at collect time and blocked the WHOLE suite. Fixed: import +
   test renamed to `_identify_retry_eligible_cases` (returns a `set`, not a
   `dict`), assertion updated accordingly. **Full suite: 149 collected, 149
   passed** — no failures. Then regenerated a fresh DB and ran all three CLIs
   with real output (RECLAIM_FRESH=1):
   - `python -m reclaim.batch` → 24 recovered / 20.7% / ₹39,776 (matches the
     documented demo);
   - `python -m reclaim.baseline` → do_nothing ₹0 · retry_everything 60 calls /
     ₹55,382 gross / -₹61,895.90 net (85% chargeback on ₹137,974 blocked) ·
     reclaim 24 calls / ₹24,089 net; calls reduced 60.0% vs retry-everything;
   - `python -m reclaim.verify_audit_chain` (against the fresh DB) → **✓ Chain
     is valid (235 entries)**.
32. **`fallback_triggered` conflation deep-dive — SAFE, no second bug (2026-09-02)** —
    Following the `rule_override` split-out (unlogged prior session, 150 tests), a
    possible *second, deeper* instance of the same conflation was investigated:
    does `fallback_triggered` get set `True` anywhere for a stopping-rule override
    (LLM succeeded, R1–R7 overrode a valid proposal) — i.e. was it the original
    signal `metrics.py`'s `"OVERRIDE"` string-hack was built on? **Finding: it is
    already clean.** `fallback_triggered=True` is set in exactly two places, both
    genuine LLM failures: `llm_client.py` (diagnose `except` at :379, decide
    `except` at :398 → deterministic default). It reaches the audit log only via
    `di.fallback_triggered`/`de.fallback_triggered` in `pipeline.py`; the decide
    entry (:214–216) sets `fallback_triggered=de.fallback_triggered` and the
    *independent* `rule_override=rule.overridden`, with an explicit code comment
    "LLM failure only, not rule override". Every other writer sets it `False`
    (`sweep.py:118`, `manual.py`, `verify.py`). **Docstrings already corrected** by
    the same prior change — `audit.py` and `models.py` both describe the narrow
    meaning and the disjoint `fallback_triggered=False, rule_override=True` case;
    no stale "LLM failure OR stopping-rule override" text remains, so no docstring
    change was needed. **No metric/test depended** on `fallback_triggered` being
    true for overrides (`metrics.py` reads it narrowly, stage-gated) — narrowing
    changes no value. **Additive change:** new regression test
    `tests/test_metrics.py::test_audit_entry_disambiguates_llm_failure_from_rule_override`
    that asserts the full disjoint pair **on the real decide audit entry** for both
    cases (LLM-failure → `fallback_triggered=True, rule_override=False`; R1
    override of a valid proposal → `fallback_triggered=False, rule_override=True`).
    Verified live: **151 collected / 151 passed**; fresh DB (`rm reclaim.db*` +
    `python -m reclaim.batch`) → `llm_call_failures` **0**, `stopping_rule_overrides`
    **32** `{'R1': 12, 'R6': 4, 'R3': 5, 'R2': 6, 'R4': 5}`, 24 recovered /
    ₹39,776 / 20.7%; `verify_audit_chain` → **✓ Chain is valid (235 entries)**.
    Also fixed a stray malformed `<parameter ...>` fragment that had corrupted the
    end of PROGRESS.md (leftover from a failed prior edit) — file now ends cleanly
    on item #31.

---

## Phase 5 — Independent adversarial review: fixes (2026-09-02)

An independent review against the source (not the prose) verified the core
pipeline — "LLM proposes, code disposes" is structurally real (no execution
authority in `llm_client.py`, enforced by an AST-boundary test), idempotency is a
DB UNIQUE held under real threads, the three-way metric split is now clean
(`rule_override` boolean, not a string match), the state machine is guarded — but
found the Phase 5 *statistical-rigor* pillar did not hold up in execution, plus
several correctness gaps. **All fixed this round: backend tests 151 → 160, all
passing (29.1s); frontend 6/2 unchanged.**

33. **Robustness suite measured one repeated sample (review #1, highest damage).**
    Root cause: `run_robustness_suite` ingested every seed into a SHARED
    `get_db()`; `generate_batch` emits seed-invariant event ids
    (`evt_{i:05d}`), so seeds 1–99 deduped on UNIQUE(event_id) → every seed
    reported identical metrics, stddev provably 0, "distribution" = one sample
    repeated 100×, and it polluted the real `reclaim.db` with N×60 cases.
    Fixes: (a) `_run_one_seed` now runs each seed on its OWN freshly-created,
    file-backed temp DB (removed on exit) — isolates seeds *and* stops polluting
    the real DB; (b) extracted `_build_report` so the percentile/stddev math is
    unit-testable without re-paying N full batch runs; (c) regression tests —
    `test_robustness_suite_different_outcomes` now asserts **at least two
    distinct (rate, amount) outcomes** across seeds (previously only that the
    seed labels differed, which the buggy code passed), plus two `_build_report`
    unit tests. Dropped the 86-full-run headline-percentile integration test
    (free only under the dedup bug) → the suite got *faster* (40.6s → 29.1s).
    **Decision:** keep the seed-invariant `evt_{i:05d}` ids in `synthetic.py` —
    ids are unique *within* a seed's fresh DB; the isolation, not the id format,
    is the fix.
    Also fixed while running the CLI live: `robustness.py`'s `__main__` was the
    ONE Phase 5 CLI still missing the UTF-8 stdout reconfigure (PROGRESS #30
    claimed both were done; only `baseline.py` had it) — a Windows cp1252
    console crashed printing `₹` after the distribution. Mirrored the same
    `sys.stdout.reconfigure(encoding="utf-8", errors="replace")` fix; CLI now
    completes (`python -m reclaim.robustness 5` → recovery-rate stddev 0.0701,
    recovered-amount stddev ₹9,608 — a genuine distribution).
34. **Baseline "same world, different policy" was not controlled (review #2).**
    Root cause: both strategies created their OWN `random.Random(seed)` and
    drained it at different stream positions (retry_everything drew for all 60
    cases; reclaim only for retry-eligible ones), so the same case faced a
    different draw in each world — the published 20/60 & 11/24 successes
    (₹55,382 / ₹24,089) were a single lucky/unlucky draw artifact. Fix:
    `_case_success(seed, case_id, reason)` derives each draw from
    `Random(f"{seed}:{case_id}")` — a pure per-case function SHARED by both
    strategies; only *which cases are attempted* differs. Regression tests:
    `test_case_success_is_pure_and_order_independent` (a case's draw does not
    move when preceding cases change) + `test_same_world_counterfactual_both_strategies_read_same_draw`
    (recompute both strategies' success counts independently from the per-case
    model on a real run → they reconcile; retry_everything's successes are a
    superset of reclaim's). **The ₹ figures moved and must be re-quoted from the
    controlled counterfactual before being cited again.**
35. **Tamper chain skipped the safety boolean (review #3).** `rule_override` —
    the field anchoring `stopping_rule_overrides` — was omitted from
    `audit_chain._canonical_entry_dict`, so flipping it on a row passed
    verification. Fix: added to both the dict and ORM branches. Regression
    tests: `test_verify_detects_rule_override_tamper` (finalize → flip →
    verify breaks) + `test_canonical_entry_dict_covers_rule_override` (both
    branches).
36. **Second event for an already-tracked subscription crashed ingest (review
    #4 — the one true crash-on-real-input bug).** `RecoveryCaseRow.case_id` is
    UNIQUE; `ingest_event` only rescued the event_id collision, so a *new*
    event_id on an existing subscription fell through to an uncaught
    `IntegrityError` → 500. Fix: on a non-event_id integrity collision, re-query
    by `case_id` and raise an explicit `RazorpayWebhookException`
    ("already tracked", → 422 by the API layer) — a deliberate single-cycle
    boundary, never a crash. Regression test:
    `test_new_event_for_existing_subscription_raises_gracefully`.
    **Decision:** reject as out-of-model rather than model multi-cycle retry
    history per subscription (attempt 2/3/4 + cooldown cycles are separate cases
    in the synthetic batch, not multiple events on one subscription); rationale
    in the webhook comment.
37. **Broker-mode deferred retry moved money with no audit (review #5).**
    `retry_payment_task` called `execute_action` directly — scheduler-fired
    retries (or their failures) were invisible to the audit trail and metrics,
    violating "every money action explained + audited" in the one non-stub path
    that actually fires money. Fix: the fire now writes two `scheduled_retry`
    entries — pre-fire `SCHEDULED_RETRY_FIRING` and post-execution (terminal
    state / outcome / dup flag) — both `fallback_triggered=False,
    rule_override=False` (a scheduler fire is never an LLM failure or a rule
    override). Idempotency preserved by the ledger (duplicate invocation →
    `idempotent_duplicate=True`, still logged). New `tests/test_tasks.py` (3
    tests): fire is audited, duplicate flagged, unknown case is a clean dict
    error.
38. **Baseline string-matched "OVERRIDE" (review #6).** `_identify_retry_eligible_cases`
    and the blocked-value computation matched `"OVERRIDE"` in the outcome string
    — the exact brittle pattern already fixed to a boolean in `metrics.py` — and
    it drives a financial figure. Both now read the explicit `entry.rule_override`
    boolean (decide-entry writes it since 7d66b47).
39. **README hygiene + Phase 5 visibility (review #7).** Counts refreshed from
    the stale 149/155 to **160 backend (19 files) + 6 frontend (2 files)**;
    badge updated; new README section "Statistical Rigor & Structural Proof";
    Project Layout + Test Modules now list `robustness.py`, `baseline.py`,
    `audit_chain.py`, `verify_audit_chain.py`, `test_tasks.py`,
    `test_llm_isolation.py`. The counterfactual wording ("same world, different
    policy") is kept because with #34 it is now literally true.

## In progress / next

- **Baseline ₹ figures RE-QUOTED from the controlled counterfactual
  (2026-09-02, verified live)** — `RAZORPAY_WEBHOOK_SECRET=… python -m
  reclaim.baseline 42` now reports (draw-artifact values in parens): `do_nothing`
  0 / ₹0; `retry_everything` 60 calls / **9 ok** (was 20) / ₹23,491 gross (was
  ₹55,382) / ₹137,974 blocked / **−₹93,786.90 net** (was −₹61,895.90); `reclaim`
  24 calls / **6 ok** (was 11) / **₹15,994 net** (was ₹24,089). The 9/60 and 6/24
  match the reviewer's own independent controlled re-derivation exactly — the
  fix reproduces their prediction. Any submission-facing doc quoting the old
  ₹55,382 / ₹24,089 figures MUST be updated to these.
- **A1** (ngrok webhook registration + live-mode run) still blocked on the
  operator's REAL Razorpay secret/test keys — never guessed.
- **Commit**: the working tree holds this round's fixes + tests (robustness,
  baseline, audit_chain, tasks, webhook) and the README/PROGRESS updates,
  uncommitted on `main`.
