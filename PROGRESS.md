# Reclaim — Build Progress

> Read this file first at the start of every session before doing anything else.

**Status:** Pipeline complete. Phase 1 core + Phase 2 deepening done. **Phase 3 (competitive hardening) COMPLETE:** adversarial resilience suite, WAL mode, economic-floor rule R7, stale-lock sweep (Celery periodic), policy-as-code rules + `/rules` page, LLM provenance logging, verification-only Subscriptions/Settlements integration, distributed-idempotency design note, and all submission artifacts. Tests green: **99 collected across 12 files**, all passing. (A1 ngrok/webhook registration still needs the operator's real Razorpay secret — live paths are env-gated and documented.)

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

17. **Section 1 — Adversarial resilience tests** (`tests/adversarial/`, 10 tests):
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
