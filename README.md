# Reclaim — AI Revenue Recovery Agent

**Reclaim** is a self-contained AI agent that recovers failed recurring revenue on Razorpay subscriptions. When a customer's card fails, a webhook fires, and Reclaim walks the case through a **guarded, explainable state machine** — an LLM *proposes* a recovery action, and hard-coded business/stopping rules *dispose*. Every decision is validated, logged to an append-only audit trail, and executed idempotently so a payment is never double-charged.

Built for the **Razorpay AI Buildathon — Track 3 (AI agent for revenue recovery)**.

> **TL;DR for reviewers:** money movement is never left to the model's whim. The LLM narrows the problem to one bounded action; a deterministic, unit-tested stopping-rule layer clamps it; idle/overdue/unsafe cases are deliberately halted or escalated to a human. The batch report separates *"the LLM was down"* from *"the LLM proposed something unsafe and code rejected it"* — two very different stories to a stakeholder.

---

## What it does

- Ingests failed-payment webhooks through the **real Razorpay webhook boundary**: constant-time HMAC‑SHA256 signature verification, strict payload parsing, and `event_id`-based **dedupe** (race-safe via a DB `UNIQUE` constraint).
- **Diagnoses** the root cause of a failure (insufficient funds, card expired, mandate revoked, bank timeout, …) with a structured, confidence-scored output.
- **Decides** exactly one bounded action: `retry_now`, `retry_scheduled`, `request_payment_method_update`, `escalate_human`, or `stop`.
- **Enforces** six code-level stopping rules that can override any LLM proposal.
- **Acts** idempotently — a retry can never fire twice for the same `(case, attempt, action)` — and logs everything to an append-only audit trail.
- Reports batch metrics that distinguish **LLM failures**, **stopping-rule overrides**, and **stub-mode demo actions**.

---

## Architecture: Ingest → Diagnose → Decide → Act → Log

The pipeline is a single linear state machine. Transitions are the **only** way stage progress is recorded; any edge not in the transition table is illegal and raises.

```
 Webhook ──▶ [1. INGEST]  ──▶ [2. DIAGNOSE]  ──▶ [3. DECIDE]  ──▶ [4. ACT]  ──▶ [5. RESOLVED / ESCALATED]
            verify sig         root cause +     LLM proposes      idempotent
            parse + dedupe     confidence       one action;       execution
                                               rules enforce      (stub or live)
                                                    │
                                         (stop → RESOLVED with no side effect)
```

### State machine

`INGESTED → DIAGNOSED → DECIDED → ACTING → {RESOLVED, ESCALATED, FAILED}`

- Terminal states are **absorbing** — a finished case can never be revisited.
- `DECIDED → RESOLVED` is legal **only** when the decision was `stop`, so "resolved by a deliberate halt" stays distinguishable from "resolved by a successful recovery."
- Every transition fires a listener that appends a structured row to the audit trail with full reasoning and the `fallback_triggered` flag.

### The "LLM proposes, code disposes" layer

The Decide agent never has the last word on money. `stopping_rules.py` is a pure, unit-tested post-hoc validation layer, independent of the prompt. First matching rule wins:

| Rule | Condition | Enforced action |
|------|-----------|-----------------|
| **R1** | Cause is `mandate_revoked` (retries disallowed) | `escalate_human` |
| **R2** | Amount above `ESCALATION_AMOUNT_THRESHOLD` | `escalate_human` |
| **R3** | Days since last attempt above `ESCALATION_DAYS_THRESHOLD` | `escalate_human` |
| **R4** | Retry proposed but attempts exhausted (`MAX_RETRIES_PER_CYCLE`) | `stop` |
| **R5** | Payment-method-update email cap reached (`EMAIL_CAP_PER_7D`) | `escalate_human` |
| **R6** | `retry_now` proposed but cooldown not elapsed (`COOLDOWN_HOURS`) | `retry_scheduled` |

Each override is recorded with a machine-readable rule id (`rule=R1 OVERRIDE`) so the audit trail and metrics can show a naive proposal being clamped by a business rule.

### Three metrics that are deliberately *not* conflated

1. **LLM call failures** — the LLM actually failed → deterministic default.
2. **Stopping-rule overrides** — a rule overrode a *valid* LLM proposal (broken down by rule).
3. **Stub-mode actions** — actions executed in demo/test mode, **not** a fallback at all.

---

## Tech stack

| Layer | Technology |
|-------|-----------|
| API / webhook | **FastAPI** + Uvicorn |
| Validation | **Pydantic v2** (`pydantic-settings`) — every boundary validated, zero-halo |
| Persistence | **SQLAlchemy 2** (SQLite by default, PostgreSQL-compatible schema) |
| Async jobs | **Celery** + **Redis / Upstash** (`rediss://` over TLS), eager mode for tests/demos |
| LLM | OpenAI-compatible client → local **Ollama** (e.g. `qwen2.5:32b-instruct`); deterministic offline shim for hermetic tests/demos |
| Templating | Jinja2 (dashboard) |
| Quality | pytest (63 tests), Ruff, mypy (strict) |

**Modes (env-gated, safe by default):**
- `LLM_MODE=offline` → deterministic rule shim (hermetic tests & demos, no network).
- `LLM_MODE=online` → real Ollama calls with exponential-backoff on transient timeouts, then the deterministic fallback.
- `ACT_MODE=stub` → log the would-be Razorpay call (safe for demos, no credentials needed).
- `ACT_MODE=live` → real test-mode Razorpay calls; **refuses** to run without valid keys and refuses to guess an unconfirmed API route (`RAZORPAY_RETRY_PATH` must be set).
- `RECLAIM_CELERY_EAGER=1` → run celery tasks synchronously, no broker needed.

---

## Security: secrets never live in source

- Real credentials are read **only** from `.env`, which is gitignored: Upstash Redis URL, Razorpay test keys, and the webhook signing secret.
- `.env.example` ships with **placeholders only** — the shape, never the values.
- `config.py` holds no hardcoded secrets; required secrets **fail loud at load time** (zero-halo).
- Idempotency is a DB `UNIQUE` constraint, not a promise: a duplicate Act call claims and is logged as a no-op — a double charge is impossible.

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
pytest
```

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

**Reading the numbers:** the run recovered **₹39,776 (20.7%)**, deliberately **stopped 5** cases and **escalated 23** to humans (28 cases took *no* retry action) — because the stopping-rule layer honored mandate revocations, amounts above threshold, age, attempt limits, email caps, and cooldowns. 32 valid-looking LLM proposals were overridden by a rule; with 0 LLM call failures, every one of those 32 was a *safety override*, not a crash.

**Try a live demo:** set `LLM_MODE=online` + your Ollama model, `ACT_MODE=live` + Razorpay test keys + confirm `RAZORPAY_RETRY_PATH`, then re-run the batch.

---

## Layout

```
src/reclaim/
  models.py          Pydantic v2 schemas for every agent boundary
  state_machine.py   guarded transition table (the only way progress is recorded)
  stopping_rules.py  code-enforced R1–R6 (pure, unit-tested)
  pipeline.py        run_case / run_batch orchestrator + concurrency cap + backoff
  webhook.py         signature verify, parse, dedupe
  act.py             idempotent action execution + audit
  razorpay_client.py stub/live Razorpay client (idempotency keys)
  llm_client.py      offline shim / online Ollama wrapper
  metrics.py         batch report (three non-conflated counters)
  api.py, audit.py, repo.py, db.py, celery_app.py, tasks.py, dispatcher.py, email.py
tests/               63 tests — stopping rules, webhook, state machine, models,
                     synthetic, pipeline, metrics
pyproject.toml       build + pytest/ruff/mypy config
```

---

## License

[MIT](LICENSE) © 2026 Utkarsh Karki
