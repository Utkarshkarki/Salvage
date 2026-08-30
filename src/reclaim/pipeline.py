"""Pipeline orchestrator: Ingest -> Diagnose -> Decide -> Act -> Log.

``run_case`` drives one RecoveryCase through the whole pipeline. The state
machine is the ONLY source of stage progress; every transition is persisted to
the case row AND appended to the audit log with full reasoning + the
``fallback_triggered`` flag.

``run_batch`` processes many cases under a configurable concurrency cap (so a
50+ case demo doesn't overwhelm local Ollama) with exponential backoff on
transient LLM failures.

The deciding principle stands: the LLM *proposes* (Diagnose, Decide), the
stopping-rule layer in code *disposes* (enforce the bounded/gated actions).
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any

from . import repo
from .act import execute_action
from .audit import write_audit
from .config import Settings, get_settings
from .db import Database, get_db
from .llm_client import LLMWrapper
from .models import (
    Action,
    AuditLogEntry,
    CaseState,
    DecideInput,
    DiagnoseInput,
)
from .state_machine import CaseStateMachine
from .stopping_rules import RuleOutcome, enforce

logger = logging.getLogger("reclaim.pipeline")


@dataclass
class CaseOutcome:
    """What one pipeline run decided/did — feeds the batch metrics.

    Distinguishes three independent flags:
    - llm_failure: LLM call itself failed and fell back to deterministic default
    - stopping_rule_override: stopping rule (R1-R6) overrode a valid LLM proposal
    - stub_mode_action: action executed in stub/demo mode (not a fallback at all)
    """

    case_id: str
    terminal_state: CaseState | None
    cause: str
    action: str
    action_taken: str
    amount_recovered: float
    llm_failure: bool  # true if diagnose or decide LLM call failed
    stopping_rule_override: bool  # true if a rule overrode the LLM proposal
    skipped: bool = False


# ---------------------------------------------------------------------------
# Concurrency throttle + backoff
# ---------------------------------------------------------------------------


class ConcurrencyLimiter:
    """Bounded semaphore around a callable, tracking peak concurrency.

    Provides the hard "no more than N in flight at once" guarantee and a
    measurable ``peak`` for tests/demo to assert against.
    """

    def __init__(self, max_workers: int) -> None:
        if max_workers < 1:
            raise ValueError("max_workers must be >= 1")
        self._max = max_workers
        self._sem = threading.BoundedSemaphore(max_workers)  # shared across calls
        self._active = 0
        self.peak = 0
        self._lock = threading.Lock()

    def run(self, fn: Any, *args: Any, **kwargs: Any) -> Any:
        with self._sem:  # hard cap on in-flight work, shared across all callers
            with self._lock:
                self._active += 1
                self.peak = max(self.peak, self._active)
            try:
                return fn(*args, **kwargs)
            finally:
                with self._lock:
                    self._active -= 1


def call_with_backoff(fn: Any, settings: Settings) -> Any:
    """Exponential backoff around an LLM call (online mode).

    In ``offline`` mode LLM calls are deterministic and never block/throw, so
    this is a no-op passthrough. In ``online`` mode a transient Ollama timeout
    backs off (base * 2^n, capped) before surfacing; the LLM wrapper then still
    applies its deterministic fallback.
    """
    if settings.llm_mode != "online":
        return fn()
    delay = settings.llm_backoff_base_seconds
    limit = settings.llm_backoff_max_seconds
    while True:
        try:
            return fn()
        except Exception:
            delay = min(delay * 2, limit)
            if delay >= limit:
                raise
            logger.warning("LLM_BACKOFF sleeping %.1fs", delay)
            time.sleep(delay)


# ---------------------------------------------------------------------------
# Single-case run
# ---------------------------------------------------------------------------


def run_case(
    case_id: str, *, settings: Settings | None = None, db: Database | None = None
) -> CaseOutcome:
    """Drive one case end-to-end. Safe to call multiple times (idempotent for
    terminal cases; Act is idempotent via the ledger)."""
    settings = settings or get_settings()
    db = db or get_db()
    wrapper = LLMWrapper(settings)

    row = repo.get_case_row(db, case_id)
    if row is None:
        raise KeyError(f"unknown case_id {case_id}")
    case = repo.row_to_case(row)

    if case.state.is_terminal():
        logger.info("RUN_SKIP case=%s state=%s (already terminal)", case_id, case.state.value)
        return CaseOutcome(
            case_id, case.state, case.failure_reason, "", "", 0.0,
            False, False, skipped=True,
        )

    machine = CaseStateMachine(initial=case.state)

    # ---------------- Diagnose ----------------
    machine.diagnose()
    repo.set_case_state(db, case_id, CaseState.DIAGNOSED)
    di_input = DiagnoseInput(
        decline_code=case.failure_reason,
        payment_history=case.payment_history,
    )
    di = call_with_backoff(lambda: wrapper.diagnose(di_input), settings)
    write_audit(
        db,
        AuditLogEntry(
            case_id=case_id, stage="diagnose",
            agent_reasoning=di.output.reasoning,
            input_state=di_input.model_dump(mode="json"),
            decision=f"cause={di.output.cause.value} conf={di.output.confidence}",
            action_taken=None, outcome="DIAGNOSED",
            fallback_triggered=di.fallback_triggered,
        ),
    )

    # ---------------- Decide ----------------
    machine.decide()
    repo.set_case_state(db, case_id, CaseState.DECIDED)
    de_input = DecideInput(
        cause=di.output.cause,
        attempt_number=case.attempt_number,
        days_since_last_attempt=case.days_since_last_attempt(),
        amount=case.amount,
        customer_tier=case.customer_tier,
    )
    de = call_with_backoff(lambda: wrapper.decide(de_input), settings)
    email_count = repo.count_recent_payment_method_updates(
        db, case.customer_id, settings.email_cap_per_7d * 24.0
    )
    rule: RuleOutcome = enforce(
        de_input, de.output, settings, payment_method_update_count=email_count
    )
    write_audit(
        db,
        AuditLogEntry(
            case_id=case_id, stage="decide",
            agent_reasoning=rule.decision.reasoning,
            input_state=de_input.model_dump(mode="json"),
            decision=rule.decision.action.value,
            action_taken=None,
            outcome=f"DECIDED rule={rule.rule or 'none'}"
                    f"{' OVERRIDE' if rule.overridden else ''}",
            fallback_triggered=de.fallback_triggered,  # LLM failure only, not rule override
        ),
    )

    # Track separate metrics: LLM failure vs stopping rule override
    llm_failed = di.fallback_triggered or de.fallback_triggered
    rule_overrode = rule.overridden

    action = rule.decision.action

    # ---------------- STOP: resolve without any side effect ----------------
    if action == Action.STOP:
        machine.resolve_as_stopped()
        repo.set_case_state(db, case_id, CaseState.RESOLVED)
        write_audit(
            db,
            AuditLogEntry(
                case_id=case_id, stage="act",
                agent_reasoning=rule.decision.reasoning,
                input_state=de_input.model_dump(mode="json"),
                decision="stop", action_taken="stop", outcome="STOPPED",
                fallback_triggered=de.fallback_triggered,
            ),
        )
        return CaseOutcome(
            case_id, CaseState.RESOLVED, di.output.cause.value, action.value,
            "stop", 0.0, llm_failed, rule_overrode,
        )

    # ---------------- Act: bounded execution ----------------
    machine.start_acting()
    repo.set_case_state(db, case_id, CaseState.ACTING)
    write_audit(
        db,
        AuditLogEntry(
            case_id=case_id, stage="act",
            agent_reasoning=rule.decision.reasoning,
            input_state=de_input.model_dump(mode="json"),
            decision=action.value, action_taken=action.value, outcome="ACTING",
            fallback_triggered=de.fallback_triggered,
        ),
    )

    result = execute_action(db, case, rule.decision, settings)
    if result.terminal_state == CaseState.RESOLVED:
        machine.resolve()
    elif result.terminal_state == CaseState.ESCALATED:
        machine.escalate()
    else:
        machine.fail()
    repo.set_case_state(db, case_id, result.terminal_state)

    write_audit(
        db,
        AuditLogEntry(
            case_id=case_id, stage="act",
            agent_reasoning=rule.decision.reasoning,
            input_state=de_input.model_dump(mode="json"),
            decision=action.value,
            action_taken=result.action_taken,
            outcome=f"{result.terminal_state.value}/{result.outcome}"
                    f"{' DUP' if result.idempotent_duplicate else ''}",
            fallback_triggered=de.fallback_triggered,
        ),
    )

    return CaseOutcome(
        case_id, result.terminal_state, di.output.cause.value, action.value,
        result.action_taken, result.amount_recovered,
        llm_failed, rule_overrode,
    )


# ---------------------------------------------------------------------------
# Batch run under a concurrency cap
# ---------------------------------------------------------------------------


def run_batch(
    case_ids: list[str],
    *,
    settings: Settings | None = None,
    db: Database | None = None,
    max_concurrency: int | None = None,
    limiter: ConcurrencyLimiter | None = None,
) -> list[CaseOutcome]:
    """Run many cases in parallel, capped at ``max_concurrency`` in flight.

    Returns outcomes for completed futures; a failure in one case is isolated
    to that case (logged, re-raised) and never kills the batch.
    """
    settings = settings or get_settings()
    db = db or get_db()
    cap = max_concurrency or settings.max_concurrency
    limiter = limiter or ConcurrencyLimiter(cap)

    def _guarded(cid: str) -> CaseOutcome:
        try:
            return limiter.run(run_case, cid, settings=settings, db=db)
        except Exception as exc:  # isolate one case, never the batch
            logger.error("BATCH_CASE_FAILED case=%s err=%s", cid, exc)
            return CaseOutcome(
                cid, CaseState.FAILED, "", "", "", 0.0, False, False,
            )

    with ThreadPoolExecutor(max_workers=cap) as pool:
        futures = {pool.submit(_guarded, cid): cid for cid in case_ids}
        results: list[CaseOutcome] = []
        for fut in as_completed(futures):
            results.append(fut.result())
    return results
