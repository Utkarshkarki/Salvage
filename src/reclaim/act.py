"""Act layer — bounded execution of exactly one decided action.

Every external side effect here is:
  * IDEMPOTENT — the ExecutedActionRow ledger (UNIQUE on
    ``(case_id, attempt_number, action)`` + a unique ``idempotency_key``) is
    claimed BEFORE the effect; a re-run of the same action is a logged no-op.
  * FAULT-ISOLATED — every external call is wrapped in try/except; a transport
    failure never crashes the case, it resolves to FAILED after logging.
  * GATED — the state machine must already be at ACTING before this runs (the
    pipeline calls :func:`start_acting` first).

Terminal mapping (from ACTING):
  retry_now                    -> RESOLVED (recovered) or FAILED
  retry_scheduled              -> RESOLVED (cycle done; money pending)
  request_payment_method_update-> RESOLVED (request sent)
  escalate_human               -> ESCALATED
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy.exc import IntegrityError

from .config import Settings, get_settings
from .db import Database, ExecutedActionRow
from .email import send_email_message
from .models import Action, CaseState, DecideOutput, RecoveryCase
from .razorpay_client import RazorpayClient, idempotency_key

logger = logging.getLogger("reclaim.act")


@dataclass(frozen=True)
class ActResult:
    terminal_state: CaseState
    action_taken: str
    outcome: str
    idempotent_duplicate: bool = False
    amount_recovered: float = 0.0


def _claim(db: Database, case: RecoveryCase, action: str) -> tuple[str, bool]:
    """Idempotency claim. Returns ``(idempotency_key, acquired)``.

    ``acquired=True`` means this run won the claim and must perform the side
    effect. ``acquired=False`` means the ledger already holds it — the caller
    must skip the effect entirely (the duplicate guard).
    """
    key = idempotency_key(case.case_id, case.attempt_number, action)
    row = ExecutedActionRow(
        case_id=case.case_id,
        attempt_number=case.attempt_number,
        action=action,
        idempotency_key=key,
        executed_at=case.created_at,  # placeholder; refreshed below on insert
    )
    from .db import utcnow

    row.executed_at = utcnow()
    with db.create_session() as session:
        session.add(row)
        try:
            session.commit()
            logger.info(
                "ACT_CLAIMED case=%s attempt=%d action=%s key=%s",
                case.case_id, case.attempt_number, action, key,
            )
            return key, True
        except IntegrityError:
            session.rollback()
            logger.info("ACT_DUPLICATE key=%s (already executed, skipped)", key)
            return key, False


def _retry_now(db: Database, case: RecoveryCase, settings: Settings) -> bool:
    """Trigger the Razorpay retry. Fault-isolated; returns success bool."""
    client = RazorpayClient(settings)
    try:
        return client.retry_payment(
            case_id=case.case_id,
            subscription_id=case.subscription_id,
            amount=case.amount,
            attempt_number=case.attempt_number,
        )
    except Exception as exc:  # Razorpay unreachable, HTTP error, bad config...
        logger.error("ACT_RETRY_ERROR case=%s err=%s", case.case_id, exc)
        return False


def schedule_retry(
    db: Database, case: RecoveryCase, decision: DecideOutput, settings: Settings
) -> bool:
    """Schedule the future retry via Celery. Returns success.

    In eager mode (tests/demo) there is no live broker, so we record the
    scheduled eta and return — the future attempt is represented by the next
    payment-failed webhook. In broker mode we enqueue with an idempotent task
    id derived from (case, attempt).
    """
    eta = decision.scheduled_at
    if settings.reclaim_celery_eager:
        logger.info(
            "SCHEDULE_STUB case=%s attempt=%d eta=%s (eager: recorded, no async fire)",
            case.case_id, case.attempt_number, eta,
        )
        return True
    from .celery_app import app, task_id

    app.send_task(
        "reclaim.tasks.retry_payment_task",
        args=[case.case_id, case.attempt_number],
        eta=eta,
        task_id=task_id(case.case_id, case.attempt_number),
    )
    return True


def execute_action(
    db: Database,
    case: RecoveryCase,
    decision: DecideOutput,
    settings: Settings | None = None,
) -> ActResult:
    """Execute the bounded decided action, gated + idempotent."""
    settings = settings or get_settings()
    action = decision.action

    if action == Action.STOP:
        # STOP is resolved by the pipeline (DECIDED -> RESOLVED via_stop) and
        # never reaches the Act layer; guard loudly rather than guess.
        raise RuntimeError("execute_action must not be called for Action.stop")

    _key, acquired = _claim(db, case, action.value)
    if not acquired:
        # A replay of an already-executed action: no double side effect.
        return ActResult(
            terminal_state=_dedup_terminal(action),
            action_taken=action.value,
            outcome=f"duplicate_{action.value}_skipped",
            idempotent_duplicate=True,
        )

    try:
        if action == Action.RETRY_NOW:
            if _retry_now(db, case, settings):
                return ActResult(
                    CaseState.RESOLVED, "retry_now", "retry_succeeded",
                    amount_recovered=case.amount,
                )
            return ActResult(CaseState.FAILED, "retry_now", "retry_failed")

        if action == Action.RETRY_SCHEDULED:
            if schedule_retry(db, case, decision, settings):
                return ActResult(CaseState.RESOLVED, "retry_scheduled", "scheduled_retry")
            return ActResult(CaseState.FAILED, "retry_scheduled", "schedule_failed")

        if action == Action.REQUEST_PAYMENT_METHOD_UPDATE:
            send_email_message(
                to=f"customer_{case.customer_id}@reclaim.test",
                template="request_payment_method_update",
                context={"case_id": case.case_id, "amount": case.amount},
                settings=settings,
            )
            return ActResult(CaseState.RESOLVED, "request_payment_method_update", "request_sent")

        if action == Action.ESCALATE_HUMAN:
            send_email_message(
                to="recovery-team@reclaim.test",
                template="escalate_human",
                context={"case_id": case.case_id, "amount": case.amount,
                         "attempt": case.attempt_number},
                settings=settings,
            )
            return ActResult(CaseState.ESCALATED, "escalate_human", "escalated")

    except Exception as exc:  # defensive: any unexpected Act failure -> FAILED
        logger.error("ACT_UNEXPECTED case=%s action=%s err=%s", case.case_id, action.value, exc)
        return ActResult(CaseState.FAILED, action.value, f"action_error:{type(exc).__name__}")

    raise AssertionError(f"unhandled action {action}")  # pragma: no cover


def _dedup_terminal(action: Action) -> CaseState:
    # A re-executed escalate is reported as already-escaped; everything else
    # reports as resolved-but-skipped. Keeps audit truthful about duplicates.
    return CaseState.ESCALATED if action == Action.ESCALATE_HUMAN else CaseState.RESOLVED
