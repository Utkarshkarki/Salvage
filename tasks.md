# tasks.md — Built vs. explicitly out of scope

A visible map of what Reclaim builds and what it *deliberately* does not, so a
reviewer can see disciplined scoping at a glance (Track 3 boundary decisions).

## Built ✅

### Core pipeline
- [x] Webhook ingest boundary: signature verify, parse, event-id dedupe (race-safe).
- [x] Explicit, guarded state machine (`INGESTED → DIAGNOSED → DECIDED → ACTING → {RESOLVED, ESCALATED, FAILED}`), terminal-absorbing.
- [x] Diagnose → Decide → Enforce → Act → Log orchestration (`run_case` / `run_batch`).
- [x] Stopping rules R1–R7 (policy-as-code, code overrides LLM).
- [x] Idempotent Act layer (ExecutedActionRow UNIQUE ledger) — a retry fires once.
- [x] Append-only audit trail.

### Resilience & hardening
- [x] WAL mode on SQLite + concurrency tests.
- [x] Adversarial resilience test suite (`tests/adversarial/`): concurrent duplicate webhooks, mid-pipeline crash recovery (stale-lock sweep), network-drop idempotency, LLM adversarial-input triage.
- [x] Stale-lock sweep wired as a Celery periodic beat task (every 5 min).
- [x] Distributed-idempotency design note (README).

### Differentiation
- [x] Three-way metrics split (`llm_call_failures` / `stopping_rule_overrides` / `stub_mode_actions`), documented.
- [x] Rule Sensitivity Simulator (`/simulator`).
- [x] Customer-facing status page (`/status/{case_id}`).
- [x] Manual override control plane (`approve_retry` / `resolve_human`, audited `manual_override`).
- [x] Policy-as-code rules + `/rules` introspection page.
- [x] LLM call provenance logging (model / prompt version / prompt hash).
- [x] Verification-only real integrations: Subscriptions status + Settlements reconciliation (fault-isolated, env-gated).

### Submission artifacts
- [x] `DECISIONS.md`, `CHANGELOG_SUBMISSION.md`, `tasks.md`, README (incl. trust-boundary diagram + "What Broke" section).

## Explicitly OUT OF SCOPE (Track 3 / Track 4 boundary)

- [ ] **RazorpayX treasury automation (Track 4)** — not built. Settlement *reconciliation* here is a **verification-only read** (does it match?), never a treasury *operation* (moving/disbursing money). The boundary is explicit: we observe settlement state; we do not automate treasury actions.
- [ ] **Route / multi-party marketplace splits (Track 1)** — not built. No seller payouts, no split machinery.
- [ ] **Multi-region / distributed idempotency store** — designed and documented only. The local UNIQUE constraint is the reference semantics; the distributed version is a note (deliberately not implemented without a real cluster).
- [ ] **Consumer share-token auth on `/status/{case_id}`** — flagged in Phase 2, deferred (no schema migration this phase). Page is `case_id`-addressed for consistency with the dashboard.
- [ ] **Live Razorpay keys / ngrok webhook registration** — requires the operator's real credentials (zero-halo); never guessed. The live paths exist and are a documented config change away.

## How to verify scope

- Every real Razorpay call is **verification-only or a bounded, idempotent retry** — nothing else touches the network.
- `DECISIONS.md` records each boundary decision and _why_ it was drawn there.
