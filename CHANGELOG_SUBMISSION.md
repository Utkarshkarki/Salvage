# CHANGELOG_SUBMISSION

A dated, phase-level log of major changes across the Reclaim build — the submission-ready view, distinct from raw git-commit noise. Latest first.

---

## Phase 3 — Hardening & differentiation (2026-08-31)

**Adversarial resilience test suite** (`tests/adversarial/`, new)
- `test_concurrent_duplicate_webhooks` — same event_id firing N times concurrently => one ingest + one execution (event-id UNIQUE + ExecutedActionRow ledger), no double-charge under a race.
- `test_sweep_finds_stale_acting_cases` / `test_sweep_ignores_recently_acting_cases` — mid-pipeline crash recovery.
- `test_network_drop_idempotency_intercepted` — a retry that may have succeeded server-side is intercepted on retry (same idempotency key), never double-executes.
- `test_injection_marker_triaged_before_llm` / `test_malformed_control_chars_triaged` — LLM adversarial-input triage.
- `test_concurrent_read_write_does_not_corrupt` / `test_concurrent_pipeline_is_idempotent_via_ledger` — concurrency + WAL.
- `test_sweep_is_scheduled_periodically` — periodic reconciliation wiring.

**Architecture & persistence hardening**
- WAL mode on SQLite (`db.py`) with a busy timeout; concurrent read/write verified.
- New stopping rule **R7 (economic floor)**: amounts below `MIN_RECOVERY_AMOUNT` (₹100) are never auto-retried.
- `reclaim/sweep.py` — stale-`ACTING`-lock reconciliation (mid-pipeline crash recovery) to `ESCALATED`.
- Sweep wired as a Celery **periodic beat task** every 5 minutes (`celery_app.py`, `tasks.py`).
- Distributed-idempotency design note in README.

**Differentiation (build beyond parity)**
- **Policy-as-code** stopping rules: R1–R7 are now a declarative `RuleSpec` registry (id, priority, plain-language description, condition, forced action) rendered on a new **`GET /rules`** page.
- **LLM call provenance logging**: `input_state.llm_provenance` (model, prompt version, prompt hash, mode) on every Diagnose/Decide audit entry.
- **Verification-only real integrations**: `subscription_status` + `settlement_reconciliation` on the Razorpay client, fault-isolated via `reclaim/verify.py` (never blocking/reversing).
- **Metrics differentiation** documented prominently with a "why this matters" rationale for judges.

**Submission artifacts**
- `DECISIONS.md`, `CHANGELOG_SUBMISSION.md`, `tasks.md`.
- README: Failure Injection & Resilience section, trust-boundary architecture diagram, "What Broke and How We Fixed It", updated API surface + test counts.

**Test count:** 81 → **99** (all passing).

---

## Phase 2 — Track 3 deepening (same build as Phase 1, earlier in the session)

- **B1** Rule Sensitivity Simulator (`/simulator`): re-run the same seeded batch under editable thresholds for a before/after comparison; reuses `run_batch`/`compute_metrics`; real settings never mutated.
- **B2** Manual override control plane: `POST /cases/{id}/approve_retry` and `POST /cases/{id}/resolve_human` for ESCALATED cases, audited as `stage=manual_override`, guarded via `manual=True` state-machine edges.
- **B3** Customer-facing status page (`/status/{case_id}`): plain-language, no internal jargon, read from the same audit data.
- Tests: `test_simulator.py`, `test_manual.py`, state-machine manual-edge cases (21 Phase 2 tests).
- Paused A1/A2/A3 external-dependency checkpoint (needed real webhook secret + test keys); **A2/A3 now built in Phase 3** (verification-only, env-gated).

## Phase 1 — Core pipeline (earlier in the session)

- Pydantic schemas for every agent boundary; guarded explicit state machine.
- Real webhook boundary: HMAC-SHA256 signature verify, parse, event-id dedupe (insert-then-catch, race-safe).
- `run_case` / `run_batch` orchestrator with a shared concurrency cap + LLM exponential backoff.
- Stopping-rule enforcement layer (R1–R6), code-not-prompt.
- Idempotent Act layer (ExecutedActionRow UNIQUE ledger), stub/live Razorpay client with idempotency keys.
- Append-only audit trail + read endpoints (`/dashboard`, `/cases/{id}`, `/metrics`).
- Synthetic seeded batch generator; batch CLI + three-way metrics (later split — see Phase 3).
- `LLM_MODE=offline` / `ACT_MODE=stub` / eager Celery as hermetic demo defaults.
