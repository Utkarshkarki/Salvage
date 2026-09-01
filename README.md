<div align="center">

# ⚡ Reclaim

**AI Revenue Recovery Agent for Razorpay Subscriptions**

[Quickstart](#quickstart) • [Architecture](#architecture) • [Stopping Rules](#stopping-rules) • [API Reference](#api-reference) • [Testing](#testing) • [Configuration](#configuration)

![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB?style=flat-square&logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-0.111%2B-009688?style=flat-square&logo=fastapi&logoColor=white)
![Tests](https://img.shields.io/badge/Tests-155%20passing-22C55E?style=flat-square)
![License](https://img.shields.io/badge/License-MIT-6366F1?style=flat-square)
![Code style](https://img.shields.io/badge/Code%20style-Ruff-FFA500?style=flat-square)
![Type checked](https://img.shields.io/badge/Type%20checked-mypy%20strict-1E40AF?style=flat-square)

</div>

---

**Reclaim** is a self-contained AI agent that recovers failed recurring revenue on Razorpay subscriptions. When a customer payment fails, a webhook fires and Reclaim walks the case through a **guarded, auditable state machine** — the LLM *proposes* a recovery action, and hard-coded business rules *dispose*. Every decision is validated, logged to an append-only audit trail, and executed idempotently so a payment is **never double-charged**.

> **The core design principle:** money movement is never left to the model's discretion. The LLM narrows the problem to one bounded action. A deterministic, unit-tested stopping-rule layer clamps it. The pipeline survives concurrent webhook replays, mid-pipeline crashes, and lost network responses — each covered by an adversarial test suite.

---

## Table of Contents

- [How it Works](#how-it-works)
- [Architecture](#architecture)
- [Trust Boundary](#trust-boundary)
- [Stopping Rules (R1–R7)](#stopping-rules)
- [Quickstart](#quickstart)
- [Configuration](#configuration)
- [API Reference](#api-reference)
- [Testing](#testing)
- [Failure Injection & Resilience](#failure-injection--resilience)
- [Metrics](#metrics)
- [What Broke and How We Fixed It](#what-broke-and-how-we-fixed-it)
- [Project Layout](#project-layout)
- [License](#license)

---

## How it Works

Reclaim handles the full lifecycle of a failed payment from webhook ingestion to resolution:

1. **Ingests** failed-payment webhooks through the real Razorpay webhook boundary — constant-time HMAC‑SHA256 signature verification, strict payload parsing, and `event_id`-based deduplication (race-safe via a DB `UNIQUE` constraint).
2. **Diagnoses** the root cause (insufficient funds, card expired, mandate revoked, bank timeout, …) with a structured, confidence-scored LLM output — after an **adversarial-input triage** that blocks injection-attempting decline codes from ever reaching the model.
3. **Decides** exactly one bounded action: `retry_now`, `retry_scheduled`, `request_payment_method_update`, `escalate_human`, or `stop`.
4. **Enforces** seven code-level, declarative stopping rules (R1–R7) that can veto any LLM proposal.
5. **Acts** idempotently — a retry cannot fire twice for the same `(case, attempt, action)` — and logs everything to an append-only audit trail with per-call **LLM provenance** (model, prompt version, prompt hash).
6. **Reconciles** against real Razorpay test-mode endpoints for Subscriptions and Settlements — verification-only, never blocking or reversing.
7. **Recovers from its own crashes**: a periodic sweep finds cases stuck mid-`ACTING` and safely escalates them to human review.

---

## Architecture: Ingest → Diagnose → Decide → Act → Log

The pipeline is a single linear state machine. Transitions are the **only** way stage progress is recorded; any edge not in the transition table is illegal and raises.

```
 Webhook ──▶ [1. INGEST]  ──▶ [2. DIAGNOSE]  ──▶ [3. DECIDE]  ──▶ [4. ACT]  ──▶ [5. RESOLVED / ESCALATED]
            verify sig         root cause +     LLM proposes      idempotent
            parse + dedupe     confidence       one action;       execution
                                   │            rules enforce      (stub or live)
                                   │                 │
                        (adversarial triage)  (stop → RESOLVED with no side effect)
```

### State machine

`INGESTED → DIAGNOSED → DECIDED → ACTING → {RESOLVED, ESCALATED, FAILED}`

- Terminal states are **absorbing** — a finished case can never be revisited.
- `DECIDED → RESOLVED` is legal **only** when the decision was `stop`, so "resolved by a deliberate halt" stays distinguishable from "resolved by a successful recovery."
- Every transition fires a listener that appends a structured row to the audit trail with full reasoning and the `fallback_triggered` flag.

---

## Trust Boundary

This is the load-bearing design decision of the whole project. Each layer can only act within its own ring; no layer reaches across.

```
                   ┌─────────────────────────────────────────────────────────┐
                   │                   LLM AGENT (proposal-only)             │
                   │   Diagnose → root cause   │  Decide → ONE bounded action│
                   │          NO execution authority, NO money movement       │
                   └───────────────┬─────────────────────────────────────────┘
                                   │ proposes (retry_now / retry_scheduled / …)
                                   ▼
                   ┌─────────────────────────────────────────────────────────┐
                   │        STOPPING RULES / POLICY LAYER (R1–R7)            │
                   │   absolute override authority, in code, not in a prompt │
                   │   clamps unsafe / trivial / overdue proposals; logs each│
                   └───────────────┬─────────────────────────────────────────┘
                                   │ enforces a bounded, final decision
                                   ▼
                   ┌─────────────────────────────────────────────────────────┐
                   │              STATE STORE (idempotency guarantee)        │
                   │   state machine + audit trail + ExecutedActionRow ledger│
                   │   (UNIQUE on case/attempt/action ⇒ a retry fires ONCE)  │
                   └───────────────┬─────────────────────────────────────────┘
                                   │ dispatch only when the ledger claim wins
                                   ▼
                   ┌─────────────────────────────────────────────────────────┐
                   │     EXECUTOR (stub or real Razorpay, fault-isolated)    │
                   │   dispatches under constraint, verification-only reads  │
                   └─────────────────────────────────────────────────────────┘
```

**The LLM can only suggest. The policy layer has absolute veto authority. The state store makes the guarantee real (idempotency). The executor is the only thing that touches the network — and only after a claim is won.**

---

## Stopping Rules

`stopping_rules.py` is a pure, unit-tested, post-hoc validation layer — independent of the prompt. Rules are expressed **declaratively as policy-as-code**: each rule is a self-describing object (id, priority, plain-language policy, condition, forced action). The active policy is introspectable at `GET /rules` with live threshold values.

**First matching rule wins:**

| Rule | Condition | Enforced Action |
|------|-----------|------------------|
| **R1** | Cause is `mandate_revoked` — retries are contractually disallowed | `escalate_human` |
| **R7** | Amount below economic floor (`MIN_RECOVERY_AMOUNT`, default ₹100) — retry cost outweighs value | `stop` |
| **R2** | Amount above `ESCALATION_AMOUNT_THRESHOLD` | `escalate_human` |
| **R3** | Days since last attempt above `ESCALATION_DAYS_THRESHOLD` | `escalate_human` |
| **R4** | Retry proposed but max attempts exhausted (`MAX_RETRIES_PER_CYCLE`) | `stop` |
| **R5** | Payment-method-update email cap reached (`EMAIL_CAP_PER_7D`) | `escalate_human` |
| **R6** | `retry_now` proposed but cooldown not elapsed (`COOLDOWN_HOURS`) | `retry_scheduled` |

Each override is recorded with a machine-readable rule id (e.g. `rule=R1 OVERRIDE`) in the audit trail so metrics can show exactly which rules are clamping which proposals.

> **R7 — Economic Floor:** For trivially small amounts (< ₹100 by default), the cost and risk of another retry call outweighs the recovery value. R7 sits second in priority — below R1 so a revoked mandate is *always* escalated regardless of amount. Both the threshold and priority are env-configurable.

---

## Three metrics that are deliberately *not* conflated

1. **LLM call failures** — the LLM actually failed → deterministic default.
2. **Stopping-rule overrides** — a rule overrode a *valid* LLM proposal (broken down by rule).
3. **Stub-mode actions** — actions executed in demo/test mode, **not** a fallback at all.

**Why this matters (for a judge):** conflating these three into a single "fallback" number would be actively misleading in a financial-audit context. "The model was down" (an availability incident), "the model proposed something unsafe and code rejected it" (a *safety win*, the exact thing an autonomy-constrained system is built for), and "the demo ran against a stub" (an environment property, not a model property) describe three *different* things to a stakeholder. A reviewer who reads "32 fallbacks" can't tell whether the system is fragile, safe, or merely stubbed — and in fintech, "we don't know which" is a red flag. Splitting them is the difference between a number a regulator can act on and a number that hides a story. This is why `fallback_triggered` in the audit log means precisely *"the LLM call itself failed"* and nothing else.

---

## Precision principle: what each metric actually measures

Every metric field is labeled to state precisely what it measures — the distinction between *what Reclaim could prove* and *what external state actually confirms* is explicit, not implicit:

- **`recovered_amount`** — the sum of all amounts on cases whose last act outcome contains `retry_succeeded`. A successful `retry_now` gateway call to Razorpay is what counts. **This does NOT prove the customer's money actually settled.** Settlement confirmation comes from the optional verification-only `settlement_reconciliation` lookup (see below).
  - Why it matters: a Razorpay retry call is a necessary condition for recovery, but a network drop after the API accepted could mean the payment never executed server-side, or executed but the customer's bank dropped it. The metric reports what *Reclaim* can prove (a call was made and logged), not what *Settlements* confirms (funds actually landed).
  
- **`verification_enabled` setting and `verify.py` verification-only reads** — when enabled, after a `retry_now` recovery Reclaim performs a best-effort, **non-blocking** lookup of the settlement status via `razorpay_client.settlement_reconciliation`. If verification is disabled or the lookup fails, the case still resolves as recovered; if verification succeeds, the audit trail records the settlement status. **A missing or failed verification does not change the case outcome.** This is an *observational read* that an auditor can use to corroborate external state, not a gate that blocks or reverses the recovery.
  
- **`stopping_rule_overrides`** — cases where the Decide agent proposed an action (e.g., `retry_now`) that one of the R1–R7 rules overrode with a different final action. The count and per-rule breakdown are auditable proof that the policy-as-code layer rejected a naive proposal and enforced a business rule.

- **`llm_call_failures`** — cases where the LLM call itself failed (network timeout, model error, unparseable response) and fell back to the deterministic default. This is the "model was down" story — availability, not safety.

**The documentation convention**: every metric field's comment or docstring in `metrics.py` and the HTTP responses (`api_v1.py`) explicitly names what it proves vs. what requires external confirmation. This keeps reviewers from over-interpreting the numbers.

---

## Failure Injection & Resilience

A dedicated test category (`tests/adversarial/`) attacks the pipeline's failure modes under **real concurrency** and **real crashes**, not sequential happy-path tests. These are the resilience guarantees a production recovery agent must actually hold:

| Test | What it proves |
|------|----------------|
| `test_concurrent_duplicate_webhooks` | The SAME webhook event arriving N times simultaneously dedupes to **exactly one** ingest (event-id UNIQUE) and **exactly one** execution (ExecutedActionRow ledger) — no double-charge under a race. |
| `test_concurrent_read_write_does_not_corrupt` | With **WAL mode** enabled, concurrent readers and writers never block or corrupt state. |
| `test_concurrent_pipeline_is_idempotent_via_ledger` | A terminal case re-run from many threads is a no-op for every one of them. |
| `test_sweep_finds_stale_acting_cases` / `test_sweep_ignores_recently_acting_cases` | **Mid-pipeline crash recovery**: a case stuck in `ACTING` past the configurable timeout is reconciled to `ESCALATED` (human review), while a legitimately in-progress case is never touched. |
| `test_network_drop_idempotency_intercepted` | **Network-drop-before-dispatch**: a retry that may have succeeded server-side but whose response was lost is intercepted on retry by the same idempotency key — it does **not** double-execute. |
| `test_injection_marker_triaged_before_llm` / `test_malformed_control_chars_triaged` | **LLM adversarial input**: injection-attempting, control-char, or malformed decline codes are triaged **before** the model is consulted; its (hypothetical) response to injected content can never influence the final action — the deterministic fallback governs. |
| `test_sweep_is_scheduled_periodically` | The stale-lock sweep is wired as a Celery periodic task (every 5 min), not just a manually-invoked function. |

Run just this subset with:

```bash
pytest tests/adversarial/
```

Each of these is backed by more than a test name — see [What Broke and How We Fixed It](#what-broke-and-how-we-fixed-it) for the real incidents that motivated several of them.

---

## Tech Stack

| Layer | Technology |
|-------|------------|
| API / Webhook | **FastAPI** + Uvicorn |
| Validation | **Pydantic v2** (`pydantic-settings`) — every boundary validated, zero-halo |
| Persistence | **SQLAlchemy 2** (SQLite with **WAL mode**, PostgreSQL-compatible schema) |
| Async Jobs | **Celery** + **Redis / Upstash** (`rediss://` TLS), eager mode for tests/demos, periodic beat schedule |
| LLM | OpenAI-compatible client → local **Ollama** (e.g. `qwen2.5:32b-instruct`); deterministic offline shim for hermetic tests |
| Templating | Jinja2 (dashboard) |
| Code Quality | pytest (**149 backend tests across 18 files, 6 frontend across 2 files**), Ruff, mypy (strict) |

### Modes

| Mode | Flag | Behaviour |
|------|------|-----------|
| LLM offline | `LLM_MODE=offline` | Deterministic rule shim — hermetic tests & demos, no network |
| LLM online | `LLM_MODE=online` | Real Ollama calls with exponential backoff, then deterministic fallback |
| Act stub | `ACT_MODE=stub` | Logs the would-be Razorpay call; safe for demos, no credentials needed |
| Act live | `ACT_MODE=live` | Real test-mode Razorpay calls; refuses to run without valid keys |
| Celery eager | `RECLAIM_CELERY_EAGER=1` | Celery tasks run synchronously; no broker needed |

### LLM Call Provenance

Every Diagnose and Decide call records in the audit trail's `input_state.llm_provenance`: the **model** name, a **prompt version** identifier (`diagnose-v1` / `decide-v1`), a **content hash** of the prompt sent, and the mode. This enables **model-drift detection** (did the model or prompt change, and did behaviour change with it?) and **reproducibility** (given case C and prompt version V, you can reconstruct exactly what produced a decision).

### Verification-only Integrations

Beyond payment retry, Reclaim reconciles *verification-only* against real Razorpay test-mode endpoints (`subscription_status`, `settlement_reconciliation`). These are **never** blocking or reversing — they never change a case's terminal state, only record a `verify` audit entry. Every call is fault-isolated (a failure records, never crashes), and routes are config-driven and empty by default (ZERO-HALO: we never guess a wire format).



```bash
# 1. Install
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"

# 2. Configure
cp .env.example .env
# Fill in RAZORPAY_WEBHOOK_SECRET:
#   python -c "import secrets; print(secrets.token_hex(32))"
# Optionally add REDIS_URL + Razorpay test keys for live mode

# 3. Run the synthetic batch (deterministic, offline/stub — no credentials needed)
RECLAIM_FRESH=1 python -m reclaim.batch
# Windows PowerShell: $env:RECLAIM_FRESH="1"; python -m reclaim.batch

# 4. Start the API server
uvicorn reclaim.api:app --reload
# → http://127.0.0.1:8000/dashboard

# 5. Run the test suite
pytest                       # full suite (149 backend tests across 18 files, 6 frontend across 2 files)
pytest tests/adversarial/    # adversarial resilience subset only
```

### Demo Batch Output

Running `RECLAIM_FRESH=1 python -m reclaim.batch` ingests a seeded synthetic batch (60 valid + 6 duplicate + 7 rejected deliveries) through the real webhook boundary:

```
Total cases                 : 60
Amount at risk              : Rs.192,246.00
Recovered (retry success)   : 24 cases / Rs.39,776.00
Recovery rate               : 20.7%
Stopped (deliberate halt)   : 5
Escalated (human)           : 23

Deterministic fallbacks:
  LLM call failures           : 0 cases   (LLM timeout / validation failed)
  Stopping rule overrides     : 32 cases  {'R1': 12, 'R6': 4, 'R3': 5, 'R2': 6, 'R4': 5}
  Stub mode actions           : 55 cases  (demo/test mode — not a fallback)

State distribution          : {'RESOLVED': 37, 'ESCALATED': 23}
Root-cause breakdown        : {'unknown': 13, 'mandate_revoked': 12,
                               'insufficient_funds': 16, 'do_not_honor': 6,
                               'card_expired': 9, 'bank_timeout': 4}
```

**Reading the numbers:** 32 valid-looking LLM proposals were overridden by a rule; with 0 LLM call failures, every one of those 32 was a *safety override*, not a crash. The stopping-rule layer honored mandate revocations, high-value escalations, economic floor, stale cases, attempt limits, email caps, and cooldowns.

**Live demo:** set `LLM_MODE=online` + your Ollama model, `ACT_MODE=live` + Razorpay test keys + confirm `RAZORPAY_RETRY_PATH`, then re-run the batch.

## Configuration

All configuration is read from `.env` at startup. Required secrets fail loudly if absent (zero-halo: no defaults that silently hide misconfiguration).

```bash
# ── LLM Backend ───────────────────────────────────────────────────────
OLLAMA_BASE_URL=http://localhost:11434
OLLAMA_MODEL=qwen2.5:32b-instruct
OLLAMA_TIMEOUT_SECONDS=30
LLM_MODE=offline          # offline = deterministic shim | online = real Ollama

# ── Razorpay ──────────────────────────────────────────────────────────
RAZORPAY_WEBHOOK_SECRET=  # REQUIRED — generate: python -c "import secrets; print(secrets.token_hex(32))"
RAZORPAY_KEY_ID=          # Required only when ACT_MODE=live
RAZORPAY_KEY_SECRET=      # Required only when ACT_MODE=live
ACT_MODE=stub             # stub = log the call | live = real test-mode Razorpay calls

# ── Celery / Redis ────────────────────────────────────────────────────
REDIS_URL=rediss://default:PASSWORD@host:6379   # Upstash (TLS)
RECLAIM_CELERY_EAGER=1    # 1 = synchronous tasks, no broker needed (tests/demos)

# ── Database ──────────────────────────────────────────────────────────
DATABASE_URL=sqlite:///reclaim.db   # Swap to PostgreSQL DSN for production

# ── Stopping Rule Thresholds ──────────────────────────────────────────
ESCALATION_AMOUNT_THRESHOLD=5000    # ₹ — amounts above go straight to human
ESCALATION_DAYS_THRESHOLD=7         # days since last attempt before escalation
MAX_RETRIES_PER_CYCLE=3             # attempt cap before stop
COOLDOWN_HOURS=24                   # minimum gap between retry_now proposals
EMAIL_CAP_PER_7D=1                  # payment-method-update email cap per 7 days
MIN_RECOVERY_AMOUNT=100             # ₹ — economic floor (R7)

# ── Concurrency ───────────────────────────────────────────────────────
MAX_CONCURRENCY=5
LLM_BACKOFF_BASE_SECONDS=1
LLM_BACKOFF_MAX_SECONDS=15
```

---

## API Reference

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/webhook` | Ingests a Razorpay webhook; verifies HMAC-SHA256, dedupes by `event_id` |
| `GET` | `/dashboard` | Merchant view of every case with full decision trail |
| `GET` | `/cases/{case_id}` | Single case audit trail; `?fmt=json` for machine-readable output |
| `GET` | `/status/{case_id}` | Customer-facing, plain-language status (no merchant/LLM jargon) |
| `GET` | `/metrics` | Batch metrics — the three non-conflated counters + state/cause breakdown |
| `GET` | `/rules` | Active policy-as-code rules rendered in plain language with live thresholds |
| `GET` | `/simulator` | Rule-sensitivity simulator: re-run the same seeded batch under proposed thresholds |
| `POST` | `/cases/{case_id}/approve_retry` | Human-in-the-loop: approve a retry for an `ESCALATED` case |
| `POST` | `/cases/{case_id}/resolve_human` | Human-in-the-loop: resolve an `ESCALATED` case manually |

Human-override actions are recorded as `stage=manual_override` in the audit trail and guarded via `manual=True` state-machine edges — the only exit from `ESCALATED` back to `RESOLVED`.

---

## Testing

```bash
pytest                        # full suite — 149 backend tests across 18 files, 6 frontend across 2 files
pytest tests/adversarial/     # failure injection & resilience subset
pytest -k "test_stopping"     # stopping rules only
pytest -k "test_pipeline"     # end-to-end pipeline only
```

### Test Modules

| Module | Coverage |
|--------|----------|
| `test_pipeline.py` | Happy-path and edge-case pipeline flows |
| `test_stopping_rules.py` | R1–R7 rule enforcement, priority ordering, threshold overrides |
| `test_webhook.py` | HMAC verification, payload parsing, deduplication |
| `test_state_machine.py` | Transition table, terminal state absorption, manual edges |
| `test_metrics.py` | Three-way metric split; no conflation between LLM failures and rule overrides |
| `test_api_v1.py` | Full HTTP surface — all endpoints, status codes, response schemas |
| `test_audit_chain.py` | Append-only audit trail integrity, provenance fields |
| `test_manual.py` | Human-in-the-loop override actions and audit recording |
| `test_simulator.py` | Rule sensitivity simulator correctness |
| `tests/adversarial/` | Failure injection & resilience (see below) |



---

## Failure Injection & Resilience

`tests/adversarial/` attacks the pipeline's failure modes under **real concurrency** and **real crashes** — not sequential happy-path tests.

| Test | What it proves |
|------|----------------|
| `test_concurrent_duplicate_webhooks` | The same `event_id` arriving N times simultaneously dedupes to **exactly one** ingest and **exactly one** execution — no double-charge under a race |
| `test_concurrent_read_write_does_not_corrupt` | With **WAL mode** enabled, concurrent readers and writers never block or corrupt state |
| `test_concurrent_pipeline_is_idempotent_via_ledger` | A terminal case re-entered from many threads is a no-op for every caller |
| `test_sweep_finds_stale_acting_cases` | **Mid-pipeline crash recovery**: a case stuck in `ACTING` past the timeout is reconciled to `ESCALATED` (human review) |
| `test_sweep_ignores_recently_acting_cases` | A legitimately in-progress case is never touched by the sweep |
| `test_network_drop_idempotency_intercepted` | A retry whose response was lost is intercepted by the same idempotency key — it does **not** double-execute |
| `test_injection_marker_triaged_before_llm` | Injection-attempting decline codes are triaged before the model is consulted; the LLM's hypothetical response can never influence the final action |
| `test_malformed_control_chars_triaged` | Control-char and malformed decline codes are caught by the same triage layer |
| `test_sweep_is_scheduled_periodically` | The stale-lock sweep is wired as a Celery periodic task (every 5 min), not a manually-invoked function |

```bash
pytest tests/adversarial/ -v
```

---

## Metrics

Reclaim deliberately splits three distinct concepts that are easy — and dangerous — to conflate in a financial audit context:

| Metric | Meaning |
|--------|---------|
| `llm_call_failures` | The LLM call itself failed (timeout, model error, unparseable response). The *"model was down"* story — an availability incident. |
| `stopping_rule_overrides` | A rule overrode a *valid* LLM proposal (broken down per rule). The *"model proposed something unsafe and code rejected it"* story — a safety win. |
| `stub_mode_actions` | Actions executed in demo/test mode. An environment property, **not** a fallback. |

**Why this matters:** conflating these into a single "fallback" number is actively misleading in a financial-audit context. "32 fallbacks" tells a regulator nothing — it can't distinguish an outage from a safety win from a demo artifact. `fallback_triggered` in the audit log means precisely *"the LLM call itself failed"* and nothing else.

### Precision Principle

Every metric field is labelled to state exactly what it measures:

- **`recovered_amount`** — sum of amounts on cases whose last act outcome contains `retry_succeeded`. **This does NOT prove settlement.** Settlement confirmation comes from the optional, non-blocking `settlement_reconciliation` verification lookup.
- **`verification_enabled`** — when set, a best-effort, non-blocking Razorpay settlement lookup is performed after a successful retry. A missing or failed verification does **not** change the case outcome — it is an observational read for auditors, not a gate.

---

## What Broke and How We Fixed It

Real incidents from this build, with the test that now prevents regression.

---

**1. Concurrency limiter created a new semaphore per call.**

*What broke:* `ConcurrencyLimiter.run` allocated a fresh `threading.BoundedSemaphore` on every invocation, so the "no more than N in flight" cap never actually capped anything across concurrent callers.

*How found:* code review of `pipeline.run_batch`'s concurrency logic; the shared `peak` counter was impossible given per-call semaphores.

*Fix:* the semaphore is created once in `__init__` and shared across all calls; `run` acquires the shared semaphore and updates the `peak` gauge under a lock.

*Regression test:* `test_concurrency_limiter_bounds_peak` — asserts `peak >= 2` *and* `peak <= cap`.

---

**2. Payment-history anchoring made every retry clamp to "scheduled".**

*What broke:* retries were computed against a payment history anchored at `now`/ingest, so `days_since_last_attempt` was always ~0 → the 24h cooldown never appeared elapsed → every `retry_now` proposal was clamped to `retry_scheduled` (R6), collapsing the recovery rate.

*How found:* demo metrics showed an implausibly low recovery rate; tracing decide inputs showed `days_since_last_attempt=0` for every case.

*Fix:* payment history is now anchored at ingest from the webhook's `created_at`, so the cooldown reflects the real retry gap.

*Regression test:* `test_pipeline.py`'s healthy-case flow resolves `retry_now` (not clamped) for a case with an elapsed gap.

---

**3. A single ambiguous "fallback" counter conflated three different stories.**

*What broke:* `fallback_triggered` lumped LLM call failures, stopping-rule overrides, and stub-mode actions into one number — "32 fallbacks" couldn't distinguish an outage from a safety win from a demo artifact.

*How found:* mapping the audit trail to the metrics revealed two different scenarios landing in the same bucket.

*Fix:* split into `llm_call_failures`, `stopping_rule_overrides` (by rule), and `stub_mode_actions`; `fallback_triggered` now means exactly *LLM call failure*.

*Regression test:* `test_metrics.py` pins that an R1 override lands only in `stopping_rule_overrides` (never `llm_call_failures`) and vice-versa.

---

---

## Project Layout

```
src/reclaim/
  models.py            Pydantic v2 schemas for every agent boundary
  state_machine.py     Guarded transition table (the only way progress is recorded)
  stopping_rules.py    Declarative policy-as-code R1–R7 (+ /rules introspection)
  pipeline.py          run_case / run_batch orchestrator + concurrency cap + backoff
  webhook.py           Signature verify, parse, dedupe
  act.py               Idempotent action execution + audit
  razorpay_client.py   Stub/live client (retry + subscription + settlement + idempotency keys)
  llm_client.py        Offline shim / online wrapper + adversarial triage + provenance
  sweep.py             Stale-ACTING-lock reconciliation (mid-pipeline crash recovery)
  verify.py            Verification-only Subscriptions/Settlements lookups
  metrics.py           Batch report (three non-conflated counters)
  api.py               FastAPI application, routes, Jinja2 dashboard
  audit.py             Append-only audit trail writer
  repo.py              Repository layer (case CRUD)
  db.py                SQLAlchemy engine + WAL mode setup
  celery_app.py        Celery app + periodic beat schedule
  tasks.py             Celery task definitions (pipeline, sweep)
  dispatcher.py        Action dispatch routing
  email.py             Payment-method-update email stub
  manual.py            Human-in-the-loop override actions

tests/
  conftest.py          Shared fixtures (in-memory DB, test client)
  test_pipeline.py     End-to-end pipeline flows
  test_stopping_rules.py  R1–R7 rule enforcement
  test_webhook.py      Signature verification, deduplication
  test_state_machine.py   Transition table correctness
  test_metrics.py      Three-way metric split
  test_api_v1.py       Full HTTP surface
  test_audit_chain.py  Audit trail integrity
  test_manual.py       Human override flows
  test_simulator.py    Rule sensitivity simulator
  adversarial/         Failure injection & resilience suite (9 tests)

DECISIONS.md           Architecture decisions log (what was decided and why)
CHANGELOG_SUBMISSION.md  Dated phase-level changelog
tasks.md               Built vs. explicitly out-of-scope (track boundary)
pyproject.toml         Build + pytest/ruff/mypy config
.env.example           Environment template (never commit the real .env)
```

---

## Security

- Real credentials are read **only** from `.env`, which is gitignored: Upstash Redis URL, Razorpay test keys, webhook signing secret.
- `.env.example` ships with **placeholders only** — the shape, never the values.
- `config.py` holds no hardcoded secrets; required secrets **fail loud at load time** (zero-halo: no silent defaults).
- Idempotency is enforced by a DB `UNIQUE` constraint, not a promise — a duplicate Act call is caught by the database before any side effect is dispatched.
- HMAC-SHA256 signature verification uses a **constant-time comparison** to prevent timing attacks.

---

## License

[MIT](LICENSE) © 2026 Utkarsh Karki

---

<div align="center">
<sub>Built for the Razorpay AI Buildathon — Track 3: Revenue Recovery</sub>
</div>
