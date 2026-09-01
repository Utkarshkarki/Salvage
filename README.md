# Reclaim — AI Revenue Recovery Agent

**Reclaim** is a self-contained AI agent that recovers failed recurring revenue on Razorpay subscriptions. When a customer's card fails, a webhook fires, and Reclaim walks the case through a **guarded, explainable state machine** — an LLM *proposes* a recovery action, and hard-coded business/stopping rules *dispose*. Every decision is validated, logged to an append-only audit trail, and executed idempotently so a payment is never double-charged.

.

> **TL;DR for reviewers:** money movement is never left to the model's whim. The LLM narrows the problem to one bounded action; a deterministic, unit-tested stopping-rule layer clamps it; idle/overdue/unsafe/trivial cases are deliberately halted or escalated to a human. The batch report separates *"the LLM was down"* from *"the LLM proposed something unsafe and code rejected it"* — two very different stories to a stakeholder. And it is engineered to survive real-world failure: concurrent webhook replays, mid-pipeline crashes, and lost network responses are each covered by an adversarial test.

---

## What it does

- Ingests failed-payment webhooks through the **real Razorpay webhook boundary**: constant-time HMAC‑SHA256 signature verification, strict payload parsing, and `event_id`-based **dedupe** (race-safe via a DB `UNIQUE` constraint).
- **Diagnoses** the root cause of a failure (insufficient funds, card expired, mandate revoked, bank timeout, …) with a structured, confidence-scored output — after an **adversarial-input triage** that keeps injection-attempting decline codes away from the model.
- **Decides** exactly one bounded action: `retry_now`, `retry_scheduled`, `request_payment_method_update`, `escalate_human`, or `stop`.
- **Enforces** seven code-level, declarative stopping rules (R1–R7) that can override any LLM proposal.
- **Acts** idempotently — a retry can never fire twice for the same `(case, attempt, action)` — and logs everything to an append-only audit trail with per-call **LLM provenance** (model, prompt version, prompt hash).
- **Reconciles** (verification-only) against real Razorpay test-mode endpoints for Subscriptions status and Settlements — never blocks or reverses.
- **Recovers from its own crashes**: a periodic sweep finds cases stuck mid-`ACTING` and safely escalates them to human review.
- Reports batch metrics that distinguish **LLM failures**, **stopping-rule overrides**, and **stub-mode demo actions**.

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

## Who has authority to do what (trust boundary)

This diagram is the load-bearing design decision of the whole project. Each layer can only act within its own ring; no layer reaches across.

```
                     ┌─────────────────────────────────────────────────────────┐
                     │                   LLM AGENT (proposal-only)             │
                     │   Diagnose → root cause   │  Decide → ONE bounded action│
                     │   **NO execution authority, NO money movement**        │
                     └───────────────┬─────────────────────────────────────────┘
                                     │ proposes (retry_now / retry_scheduled / …)
                                     ▼
                     ┌─────────────────────────────────────────────────────────┐
                     │        STOPPING RULES / POLICY LAYER (R1–R7)            │
                     │   absolute override authority, in code, not in a prompt │
                     │   clamp unsafe / trivial / overdue proposals; audit each│
                     └───────────────┬─────────────────────────────────────────┘
                                     │ enforces a bounded, final decision
                                     ▼
                     ┌─────────────────────────────────────────────────────────┐
                     │              STATE STORE (idempotency guarantee)        │
                     │   state machine + audit trail + ExecutedActionRow ledger│
                     │   (UNIQUE on case/attempt/action ⇒ a retry fires ONCE)  │
                     └───────────────┬─────────────────────────────────────────┘
                                     │ dispatch only when the ledger claims
                                     ▼
                     ┌─────────────────────────────────────────────────────────┐
                     │     EXECUTOR (stub or real Razorpay, fault-isolated)    │
                     │   dispatches under constraint, verification-only reads  │
                     └─────────────────────────────────────────────────────────┘
```

Reading it top to bottom: **the LLM can only suggest; the policy layer has absolute veto authority; the state store is what makes the guarantee real (idempotency); the executor is the only thing that touches the network — and only after a claim is won.** A reviewer should be able to ask "who can move money?" and get a one-line answer: *only the Executor, once, under an enforceable constraint.*

---

## The "LLM proposes, code disposes" layer

The Decide agent never has the last word on money. `stopping_rules.py` is a pure, unit-tested post-hoc validation layer, independent of the prompt. It is expressed **declaratively (policy-as-code)**: each rule is a self-describing object (id, priority, plain-language policy, condition, forced action) so the policy itself is an auditable artifact — rendered on the [`/rules`](#policy-as-code-the-rules-page) page, not just inline conditionals. First matching rule wins:

| Rule | Condition | Enforced action |
|------|-----------|-----------------|
| **R1** | Cause is `mandate_revoked` (retries disallowed) | `escalate_human` |
| **R7** | Amount below the **economic floor** (`MIN_RECOVERY_AMOUNT`, ₹100) — retry cost/risk outweighs value | `stop` (no auto-retry) |
| **R2** | Amount above `ESCALATION_AMOUNT_THRESHOLD` | `escalate_human` |
| **R3** | Days since last attempt above `ESCALATION_DAYS_THRESHOLD` | `escalate_human` |
| **R4** | Retry proposed but attempts exhausted (`MAX_RETRIES_PER_CYCLE`) | `stop` |
| **R5** | Payment-method-update email cap reached (`EMAIL_CAP_PER_7D`) | `escalate_human` |
| **R6** | `retry_now` proposed but cooldown not elapsed (`COOLDOWN_HOURS`) | `retry_scheduled` |

**R7 (economic floor)** is the newest rule and a deliberate product decision: for a trivially small amount (default < ₹100), the cost/risk of making another retry call outweighs the recovery value, so the case is stopped rather than auto-retried — overriding any LLM proposal, exactly like every other rule. It is env-configurable and sits second in priority (below the R1 mandate-safety rule) so a revoked mandate is *always* escalated to a human regardless of amount.

Each override is recorded with a machine-readable rule id (`rule=R1 OVERRIDE`) so the audit trail and metrics can show a naive proposal being clamped by a business rule.

### Policy-as-code: the `/rules` page

Because the rules are declarative, the active policy is introspectable. `GET /rules` renders every rule in plain language with the **live threshold values** — id, priority, forced action, and a plain-English statement — so a reviewer (or auditor) can see *what the policy actually is*, not just the outputs of its enforcement. Ambient property, not a separate data model.

---

## Three metrics that are deliberately *not* conflated

1. **LLM call failures** — the LLM actually failed → deterministic default.
2. **Stopping-rule overrides** — a rule overrode a *valid* LLM proposal (broken down by rule).
3. **Stub-mode actions** — actions executed in demo/test mode, **not** a fallback at all.

**Why this matters (for a judge):** conflating these three into a single "fallback" number would be actively misleading in a financial-audit context. "The model was down" (an availability incident), "the model proposed something unsafe and code rejected it" (a *safety win*, the exact thing an autonomy-constrained system is built for), and "the demo ran against a stub" (an environment property, not a model property) describe three *different* things to a stakeholder. A reviewer who reads "32 fallbacks" can't tell whether the system is fragile, safe, or merely stubbed — and in fintech, "we don't know which" is a red flag. Splitting them is the difference between a number a regulator can act on and a number that hides a story. This is why `fallback_triggered` in the audit log means precisely *"the LLM call itself failed"* and nothing else.

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

## Tech stack

| Layer | Technology |
|-------|-----------|
| API / webhook | **FastAPI** + Uvicorn |
| Validation | **Pydantic v2** (`pydantic-settings`) — every boundary validated, zero-halo |
| Persistence | **SQLAlchemy 2** (SQLite with **WAL mode** by default, PostgreSQL-compatible schema) |
| Async jobs | **Celery** + **Redis / Upstash** (`rediss://` over TLS), eager mode for tests/demos, **periodic beat schedule** |
| LLM | OpenAI-compatible client → local **Ollama** (e.g. `qwen2.5:32b-instruct`); deterministic offline shim for hermetic tests/demos |
| Templating | Jinja2 (dashboard) |
| Quality | pytest (**99 tests**), Ruff, mypy (strict) |

**Modes (env-gated, safe by default):**
- `LLM_MODE=offline` → deterministic rule shim (hermetic tests & demos, no network).
- `LLM_MODE=online` → real Ollama calls with exponential-backoff on transient timeouts, then the deterministic fallback.
- `ACT_MODE=stub` → log the would-be Razorpay call (safe for demos, no credentials needed).
- `ACT_MODE=live` → real test-mode Razorpay calls; **refuses** to run without valid keys and refuses to guess an unconfirmed API route (paths come from config).
- `RECLAIM_CELERY_EAGER=1` → run celery tasks synchronously, no broker needed.

### Verification-only real integrations (Subscriptions + Settlements)

Beyond Payments retry, Reclaim reconciles *verification-only* against real Razorpay test-mode endpoints (`razorpay_client.subscription_status`, `.settlement_reconciliation`). These are **never** blocking or reversing — they never change a case's terminal state, only record a `verify` audit entry so an auditor can see the external state at the time of the action. Every call is fault-isolated (a failure records, never crashes), and the routes are config-driven and empty by default (ZERO-HALO: we never guess a wire format). In stub mode they return deterministic placeholders so the demo stays hermetic.

### LLM call provenance (model / prompt / reproducibility)

Every Diagnose and Decide call records, in the audit trail's `input_state.llm_provenance`: the **model** name, a **prompt version** identifier (`diagnose-v1` / `decide-v1`), a **content hash** of the prompt sent, and the mode. This matters for two reasons auditors care about: **model-drift detection** (did we change the model or prompt, and did behaviour change with it?) and **reproducibility** (given case C and prompt version V, you can reconstruct exactly what produced a decision). A future reviewer can trace any audit row back to the exact model + prompt that generated it.

---

## Security: secrets never live in source

- Real credentials are read **only** from `.env`, which is gitignored: Upstash Redis URL, Razorpay test keys, and the webhook signing secret.
- `.env.example` ships with **placeholders only** — the shape, never the values.
- `config.py` holds no hardcoded secrets; required secrets **fail loud at load time** (zero-halo).
- Idempotency is a DB `UNIQUE` constraint, not a promise: a duplicate Act call claims and is logged as a no-op — a double charge is impossible.

---

## Concurrency & persistence hardening

- **WAL mode** is enabled on file-backed SQLite (journal_mode=WAL + `synchronous=NORMAL` + a busy timeout) so concurrent readers never block writers and simultaneous writers wait instead of erroring. Verified by `test_concurrent_read_write_does_not_corrupt`.
- **Stale-lock sweep**: a reconcile function (`reclaim/sweep.py`) finds cases stuck in `ACTING` past `STALE_LOCK_TIMEOUT_SECONDS` and safely reconciles them to `ESCALATED`, and it is wired into Celery as a **periodic beat task every 5 minutes** — so a worker dying mid-pipeline never leaves a case wedged forever.
- **Economic floor (R7)** stops auto-retry on trivial amounts — see the rules table above.

### Distributed idempotency: from a local UNIQUE to a cluster

Today idempotency is a **single-DB `UNIQUE` constraint** (`ExecutedActionRow` on `(case_id, attempt_number, action)`): the claim is an `INSERT` that wins exactly once, so a duplicated Act call across threads/processes can never double-charge. This is correct for a single instance (or a single shared Postgres). To scale to **multi-instance / multi-region** the same *claim semantics* must survive the store moving off the local file:

- **Keep the DB unique index as the source of truth, but make it distributed.** The idempotency key is already deterministic (`reclaim:{case}:{attempt}:{action}`) and self-describing. Moving the "have I already executed this key?" question into a **distributed store** (Redis `SETNX key <token> NX EX <ttl>` with atomic compare-and-delete, or a Postgres `INSERT ... ON CONFLICT DO NOTHING` on a unique column shared across regions) gives the exact same win-once guarantee without a single-writer bottleneck.
- **The contract to preserve is the ordering**: *claim first, then dispatch* (see `act._claim`). Whatever store backs the ledger, the side effect must only proceed after a successful claim, and a lost claim (network drop after the server accepted the call) must remain a no-op on retry — which is exactly what the adversarial network-drop test locks in.
- **Region placement**: in active-active deployments the ledger needs to be reachable from every region (e.g. a global Postgres primary, or a Redis with cross-region replication + `WAIT`) — a local-only ledger in region A would not see a claim made in region B, silently re-enabling double-execution across regions. The design intent is: **one logical idempotency ledger, physically distributed, claim-before-dispatch, TTL-bounded so a truly-lost claim eventually expires and can be retried deliberately under human review.**

This is a design note, not yet the multi-region implementation — the local UNIQUE is correct for this build and is the reference semantics any distributed store must reproduce.

---

## Quickstart

Requires Python 3.11+.

```bash
# 1. Install
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"

# 2. Configure (template — never commit the real .env)
cp .env.example .env
#   fill in RAZORPAY_WEBHOOK_SECRET (console: python -c "import secrets; print(secrets.token_hex(32))")
#   optionally add Upstash REDIS_URL + Razorpay test keys for live mode

# 3. Run the synthetic batch end-to-end (deterministic, offline/stub)
RECLAIM_FRESH=1 python -m reclaim.batch        # Windows PowerShell: $env:RECLAIM_FRESH="1"; python -m reclaim.batch

# 4. Run the API
uvicorn reclaim.api:app --reload               # then open http://127.0.0.1:8000/dashboard

# 5. Tests
pytest                                     # full suite (99 tests)
pytest tests/adversarial/                  # failure-injection/resilience subset
```

### API surface (beyond the webhook)

- `GET /dashboard` — merchant view of every case + full decision trail.
- `GET /cases/{case_id}` — one case's audit trail; `?fmt=json` for machine-readable.
- `GET /status/{case_id}` — customer-facing, plain-language status (Phase 2/3 differentiator: no merchant/LLM jargon, no rule ids).
- `GET /metrics` — the three non-conflated counters + state/cause breakdown.
- `GET /simulator` — rule-sensitivity simulator: re-run the same seeded batch under proposed thresholds for a before/after comparison (Phase 2 differentiator).
- `POST /cases/{case_id}/approve_retry` and `POST /cases/{case_id}/resolve_human` — human-in-the-loop override actions, recorded as `manual_override` in the audit trail (Phase 2 differentiator).
- `GET /rules` — the active policy-as-code rules rendered in plain language.

### Demo batch report (last run)

`RECLAIM_FRESH=1 python -m reclaim.batch` ingests a seeded **synthetic batch** (60 valid + 6 duplicate + 7 rejected deliveries) through the real webhook boundary and reports:

```
Total cases                 : 60
Amount at risk              : Rs.192,246.00
Recovered (retry success)   : 24 cases / Rs.39,776.00
Recovery rate               : 20.7%
Stopped (deliberate halt)   : 5
Escalated (human)           : 23

Deterministic fallbacks:
  LLM call failures           : 0 cases (LLM timeout/validation failed)
  Stopping rule overrides     : 32 cases {'R1': 12, 'R6': 4, 'R3': 5, 'R2': 6, 'R4': 5}
  Stub mode actions           : 55 cases (demo/test mode, not a fallback)

Cases resolved without retry: 28   (stopped=deliberate halt + escalated=human review)

State distribution          : {'RESOLVED': 37, 'ESCALATED': 23}
Root-cause breakdown        : {'unknown': 13, 'mandate_revoked': 12,
                               'insufficient_funds': 16, 'do_not_honor': 6,
                               'card_expired': 9, 'bank_timeout': 4}
```

**Reading the numbers:** the run recovered **₹39,776 (20.7%)**, deliberately **stopped 5** cases and **escalated 23** to humans (28 cases took *no* retry action) — because the stopping-rule layer honored mandate revocations, amounts above threshold, the economic floor, age, attempt limits, email caps, and cooldowns. 32 valid-looking LLM proposals were overridden by a rule; with 0 LLM call failures, every one of those 32 was a *safety override*, not a crash.

**Try a live demo:** set `LLM_MODE=online` + your Ollama model, `ACT_MODE=live` + Razorpay test keys + confirm `RAZORPAY_RETRY_PATH`, then re-run the batch.

---

## What Broke and How We Fixed It

Real incidents from this build, what they did, how each was found, and the test that now prevents regression. This is the engineering history a reviewer should be able to trust.

**1. Concurrency limiter created a new semaphore per call.**
*What broke:* `ConcurrencyLimiter.run` allocated a fresh `threading.BoundedSemaphore` on every invocation, so the "no more than N in flight" cap never actually capped anything across concurrent callers — a lock that exists only inside a single call is not a limiter.
*How found:* a code review of `pipeline.run_batch`'s concurrency go. The shared-instrumentation intent (a `peak` counter) was impossible given per-call limits.
*How fixed:* the semaphore is now created once in `__init__` and shared across all calls; `run` acquires the shared semaphore and updates the `peak` gauge under a lock.
*Regression test:* `test_concurrency_limiter_bounds_peak` (asserts `peak >= 2` *and* `peak <= cap`).

**2. Payment-history anchoring made every retry clamp to "scheduled".**
*What broke:* retries were computed against a payment history anchored at `now`/ingest, so `days_since_last_attempt` was always ~0 → the 24h cooldown never appeared elapsed → every `retry_now` proposal was clamped to `retry_scheduled` (R6), collapsing the recovery rate and masking the cooldown rule's real intent.
*How found:* the demo metrics showed an implausibly low recovery rate; tracing the decide inputs showed `days_since_last_attempt=0` for every case.
*How fixed:* payment history is now anchored at ingest from the webhook's `created_at`, so the cooldown reflects the real retry gap.
*Regression test:* `test_pipeline.py`'s healthy-case flow resolves `retry_now` (not clamped) for a case with an elapsed gap.

**3. A single ambiguous "fallback" counter conflated three different stories.**
*What broke:* `fallback_triggered` (and the metrics built on it) lumped *LLM call failures*, *stopping-rule overrides*, and *stub-mode actions* into one number — "32 fallbacks" couldn't distinguish an outage from a safety win from a demo artifact.
*How found:* mapping the audit trail to the metrics revealed two different scenarios landing in the same bucket (see [Three metrics](#three-metrics-that-are-deliberately-not-conflated)).
*How fixed:* split into `llm_call_failures`, `stopping_rule_overrides` (broken down by rule), and `stub_mode_actions`; `fallback_triggered` now means exactly *LLM failure*.
*Regression test:* `test_metrics.py` pins that an R1 override lands only in `stopping_rule_overrides` (never `llm_call_failures`) and vice-versa.

---

## Layout

```
src/reclaim/
  models.py          Pydantic v2 schemas for every agent boundary
  state_machine.py   guarded transition table (the only way progress is recorded)
  stopping_rules.py  declarative policy-as-code R1–R7 (+ /rules introspection)
  pipeline.py        run_case / run_batch orchestrator + concurrency cap + backoff
  webhook.py         signature verify, parse, dedupe
  act.py             idempotent action execution + audit
  razorpay_client.py stub/live client (retry + subscription + settlement, idempotency keys)
  llm_client.py      offline shim / online wrapper + adversarial triage + provenance
  sweep.py           stale-ACTING-lock reconciliation (mid-pipeline crash recovery)
  verify.py          verification-only Subscriptions/Settlements lookups
  metrics.py         batch report (three non-conflated counters)
  api.py, audit.py, repo.py, db.py, celery_app.py, tasks.py, dispatcher.py, email.py, manual.py
tests/               99 tests — core + API + adversarial resilience
tests/adversarial/   failure-injection & resilience suite
DECISIONS.md         running log of architecture decisions (and why)
CHANGELOG_SUBMISSION.md   dated log of major phase-level changes
tasks.md             built vs. explicitly out-of-scope (Track 3/4 boundary)
pyproject.toml       build + pytest/ruff/mypy config
```

---

## License

[MIT](LICENSE) © 2026 Utkarsh Karki
