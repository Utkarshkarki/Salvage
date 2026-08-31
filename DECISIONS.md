# DECISIONS.md — Architecture decisions log

A running, honest log of every non-trivial decision made across the Reclaim build and *why*. This is written from the actual history of what was decided (see `PROGRESS.md`), not hindsight-embroidered rationale. Newest entries at the top per phase.

---

## Phase 3 — hardening & differentiation

### WAL mode on SQLite
**Decision:** enable `journal_mode=WAL` + `synchronous=NORMAL` + a busy timeout on file-backed SQLite (`db.py.build_engine`).
**Why:** better concurrent reader/writer behaviour (a reader never blocks a writer and vice-versa), and the busy timeout lets simultaneous writers wait for the lock instead of raising "database is locked". Required for the concurrent-duplicate-dedupe and concurrent-read/write tests to be reliable. In-memory SQLite can't use WAL, so it's only attached to file URLs. Confirmed not to break the existing suite.

### Economic floor rule (R7)
**Decision:** add stopping rule **R7** — a case whose amount is below `MIN_RECOVERY_AMOUNT` (default ₹100, env-configurable) is forced to `STOP` (never auto-retried).
**Why:** for a trivially small amount, the cost/risk of a retry call outweighs the recovery value. Enforced in code like every other rule (it overrides any LLM proposal), placed second in priority — below R1 (mandate safety) so a revoked mandate is **always** escalated to a human regardless of amount.

### Mid-pipeline crash recovery (stale-lock sweep)
**Decision:** build `reclaim/sweep.py` — a reconciliation that finds cases stuck in `ACTING` past `STALE_LOCK_TIMEOUT_SECONDS` and moves them to `ESCALATED` — and wire it as a Celery periodic beat task (every 5 minutes).
**Why:** a process can die between DECIDED and a completed ACT, leaving a case in `ACTING` forever (the next webhook never fires). The sweep hands such cases to human review rather than leaving them wedged. It never runs a side effect (escalation is a pure state change), and recovery after a crash stays human-authorized + idempotent.

### LLM adversarial-input triage
**Decision:** triage the `decline_code` field (and Diagnose input) before the model is consulted; injection-marker / control-char / over-long / structured-JSON strings short-circuit to a deterministic UNKNOWN fallback.
**Why:** the decline code is free text that flows into the Diagnose prompt. A hostile value must never steer the diagnosis. Triaging before the call means the LLM's (hypothetical) response to injected content can **never** influence the final action. This is deliberately **not** counted as an LLM failure (it isn't a crash — it's a policy guard), keeping the three-way metrics honest.

### LLM call provenance logging
**Decision:** record, per Diagnose/Decide call, the model name, a prompt version id, a content hash of the prompt, and the mode (`input_state.llm_provenance` on the audit entry).
**Why:** model-drift detection and reproducibility — an auditor can trace any audit row back to the exact model + prompt that produced it.

### Verification-only real integrations (Subscriptions + Settlements)
**Decision:** add `subscription_status` and `settlement_reconciliation` to the Razorpay client, wrapped in a best-effort, fault-isolated, verification-only `verify.py`; routes are config-driven and empty by default (ZERO-HALO — never guess a wire format).
**Why:** verification-only reads (never blocking, never reversing, never changing terminal state) give real integration depth without ever letting a read endanger a recovery or a settlement. Track 3 framing only — this is reconciliation of *revenue recovery*, not treasury/RazorpayX automation.

### Distributed idempotency design note
**Decision:** document (README) how the single-DB UNIQUE idempotency constraint would evolve to a distributed store; not implemented.
**Why:** the local UNIQUE is correct for this build and is the reference semantics; the note pins the *contract* to preserve (claim-before-dispatch, win-once, TTL-bounded) so a multi-region implementer doesn't silently re-enable double-execution.

### Policy-as-code stopping rules
**Decision:** express R1–R7 as a declarative `RuleSpec` registry (id, priority, plain-language description, pure condition, forced action) and render it on a `/rules` page.
**Why:** the rules themselves become an auditable, introspectable artifact — a reviewer/auditor can see *what the policy is*, not just the outputs of enforcing it. Kept behavior-identical (first-match-wins, priority order preserved) and verified by the full existing suite.

---

## Phase 2 — Track 3 deepening

### Rule Sensitivity Simulator (`/simulator`)
**Decision:** a form that re-runs the SAME seed-42 synthetic batch under editable threshold overrides on a throwaway DB and shows a before/after metrics comparison.
**Why:** lets an operator ask "what if we tightened the amount threshold?" before changing anything in production. Reuses `run_batch` + `compute_metrics` as-is (no duplicated pipeline); real settings are never mutated.

### Customer status page (`/status/{case_id}`)
**Decision:** a plain-language, customer-safe view read from the SAME case/audit data, with no internal rule ids, stage names, or LLM/fallback jargon. Deliberately addressed by raw `case_id` this phase.
**Why:** comparable submissions in this space are merchant/ops-only; a consumer-facing surface is a differentiator. Tradeoff flagged: production should key it on a non-guessable per-case share token (deferred, no schema migration).

### Manual override / human-in-the-loop control plane
**Decision:** POST approve-retry / resolve-human actions for ESCALATED cases, guarded through the state machine as `manual=True` edges (the only way out of terminal ESCALATED) and audited as `stage=manual_override`.
**Why:** comparable submissions describe read-only/trigger-only UIs with no override capability. A human-authorised retry must be possible and must be clearly distinguished in the audit trail from agent decisions.

---

## Phase 1 — core pipeline

### Deterministic state machine with narrow LLM workers, NOT a free-form agent
**Decision:** a guarded, transition-table state machine; the LLM fills narrow Diagnose/Decide workers, and code-enforced stopping rules dispose.
**Why (build mandate):** money moves must be explainable, bounded, gated. The LLM *proposes*, the code disposes.

### `LLM_MODE=offline` + `ACT_MODE=stub` + eager Celery as the demo defaults
**Decision:** offline deterministic shim, stub executor, eager Celery are the defaults; real online Ollama + live Razorpay clients are fully implemented but gated behind env flags.
**Why:** hermetic tests and demos never need network or credentials; the real paths exist and are a config change away.

### SQLAlchemy over raw sqlite3
**Decision:** SQLAlchemy 2 ORM with a PostgreSQL-compatible schema (JSON for nested payloads, String for enums, indexed unique constraints).
**Why:** the schema is the same shape under Postgres; swapping `DATABASE_URL` migrates the dev engine to a production store without a rewrite. `check_same_thread=False` allows shared sessions in dev.

### Upstash Redis over a self-hosted/local broker (as the Celery/beat broker)
**Decision:** broker/backend = Redis under TLS (`rediss://`, Upstash), with eager mode as the no-broker default in tests/demo.
**Why:** a managed, TLS-secured Redis is the maintainable choice for the periodic beat schedule and distributed task queue; the demo/test path never needs it. (Removed a hardcoded Upstash credential from source — real broker comes from `.env`.)

### Idempotency = DB UNIQUE constraint, not just a promise
**Decision:** each Act claim is an `INSERT` on an `ExecutedActionRow` row with a UNIQUE `(case_id, attempt_number, action)` + unique `idempotency_key`, claimed BEFORE the side effect; a duplicate claim is a logged no-op.
**Why:** a duplicated Act call can never double-retry/double-charge. This is the load-bearing guarantee, and is what the adversarial concurrent/network-drop tests verify.

### Payment history anchored at ingest
**Decision:** anchor the case's payment history on the webhook's `created_at` so `days_since_last_attempt` (and the 24h cooldown rule) reflects the real retry gap.
**Why:** without it, the gap was always ~0 → every retry clamped to `scheduled` (R6), collapsing the recovery rate (and masking the cooldown rule's real intent).

### Live retry path NOT hardcoded
**Decision:** `razorpay_retry_path` is empty by default; live mode refuses to run rather than guess the exact Razorpay retry route.
**Why (ZERO-HALO):** the endpoint changes and varies by API version; inventing a wire format is worse than refusing loudly.

### `offline_decide` is a deliberately naive proposer
**Decision:** the offline Decide shim almost always proposes `retry_now` (even when unsafe).
**Why:** so the stopping-rule overrides are visible and exercised in the demo/metrics — the "LLM proposes, code disposes" dynamic is the point.

### The three-way metrics split
**Decision:** split the monolithic "Deterministic fallbacks" counter into `llm_call_failures`, `stopping_rule_overrides` (by rule), and `stub_mode_actions`; `fallback_triggered` now means exactly "the LLM call itself failed".
**Why:** conflating these is misleading for a financial audit — "the model was down", "the model proposed something unsafe and code rejected it", and "the demo ran against a stub" are three different stories. See [README § Three metrics].

### `cases_resolved_without_retry` defined explicitly
**Decision:** = stopped (decision=stop, no side effect) + escalated (human review).
**Why:** "cases that did NOT loop" needs a code-level definition a stakeholder can act on, not a slogan.
