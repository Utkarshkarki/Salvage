"""Celery task entrypoints.

These are the names the dispatcher and the Act layer enqueue by string
(``reclaim.pipeline.run_case_task``, ``reclaim.tasks.retry_payment_task``), so
workers resolve them independently of in-process imports. In eager mode
(``RECLAIM_CELERY_EAGER=1``, the demo default) Celery runs them synchronously
with no broker.
"""

from __future__ import annotations

from typing import Any

from .celery_app import app


@app.task(name="reclaim.pipeline.run_case_task")
def run_case_task(case_id: str) -> dict[str, Any]:
    """Enqueue a freshly-ingested case through the recovery pipeline."""
    from .pipeline import run_case

    outcome = run_case(case_id)
    return {
        "case_id": outcome.case_id,
        "terminal_state": outcome.terminal_state.value if outcome.terminal_state else None,
        "action": outcome.action,
    }


@app.task(name="reclaim.tasks.sweep_stale_acting_task")
def sweep_stale_acting_task() -> dict[str, Any]:
    """Periodic reconciliation: sweep stale ACTING cases to ESCALATED.

    Invoked by the Celery beat schedule every 5 minutes. Finds cases stuck in
    ACTING past the configurable timeout (a process died mid-pipeline) and
    safely moves them to human review. Idempotent: re-running is a no-op once
    they have been swept.
    """
    from .config import get_settings
    from .db import get_db
    from .sweep import reconcile_stale_acting

    settings = get_settings()
    db = get_db()
    swept = reconcile_stale_acting(db, settings)
    return {"swept_cases": swept, "count": len(swept)}


@app.task(name="reclaim.tasks.retry_payment_task")
def retry_payment_task(case_id: str, attempt_number: int) -> dict[str, Any]:
    """Execute a deferred retry for a case that was scheduled (broker mode).

    Idempotent by construction: execute_action claims via the ledger, so any
    duplicate invocation is a logged no-op and can never double-charge.

    This path MOVES MONEY, so it is explainable like every other Act: the fire
    is written to the append-only audit trail (stage ``scheduled_retry``) with
    its outcome, so a deferred retry is never invisible to an auditor — even
    though it runs outside the interactive state-machine flow.
    """
    from . import repo
    from .act import execute_action
    from .audit import write_audit
    from .config import get_settings
    from .db import get_db
    from .models import Action, AuditLogEntry, DecideOutput

    settings = get_settings()
    db = get_db()
    row = repo.get_case_row(db, case_id)
    if row is None:
        return {"case_id": case_id, "error": "unknown_case"}
    case = repo.row_to_case(row)
    decision = DecideOutput(
        action=Action.RETRY_NOW,
        reasoning="deferred retry fired by scheduler",
    )

    # Document the fire BEFORE executing: a money action must be visible in the
    # trail even if the wallet fails below.
    write_audit(
        db,
        AuditLogEntry(
            case_id=case_id,
            stage="scheduled_retry",
            agent_reasoning="deferred retry fired by scheduler (broker mode)",
            input_state={"action": "retry_now", "attempt_number": attempt_number},
            decision="retry_now",
            action_taken="retry_now",
            outcome="SCHEDULED_RETRY_FIRING",
            fallback_triggered=False,  # scheduler, never an LLM failure
            rule_override=False,
        ),
    )

    result = execute_action(db, case, decision, settings)

    write_audit(
        db,
        AuditLogEntry(
            case_id=case_id,
            stage="scheduled_retry",
            agent_reasoning="deferred retry executed; see ledger + boot outcome",
            input_state={"action": "retry_now", "attempt_number": attempt_number},
            decision="retry_now",
            action_taken=result.action_taken,
            outcome=f"{result.terminal_state.value}/{result.outcome}"
                    f"{' DUP' if result.idempotent_duplicate else ''}",
            fallback_triggered=False,
            rule_override=False,
        ),
    )
    return {
        "case_id": case_id,
        "attempt_number": attempt_number,
        "terminal_state": result.terminal_state.value,
        "outcome": result.outcome,
        "idempotent_duplicate": result.idempotent_duplicate,
    }
