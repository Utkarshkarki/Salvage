"""Manual (human) override actions for the control plane.

These are deliberate OPERATOR decisions, not agent decisions — the only way any
case leaves the terminal ESCALATED state. Every action:
  * Verifies the case is in a state where the action is legal, via the SAME
    state-machine guard logic (state_machine.CaseStateMachine) — never bypassed.
  * Writes an audit entry tagged ``stage="manual_override"`` so a viewer of the
    audit trail can immediately tell this was a human action, not the LLM's.
  * Preserves the append-only contract of the audit trail.

Two actions today:
  * approve_manual_retry  ESCALATED -> ACTING -> RESOLVED/FAILED (operator
                          authorises a retry that the agent deliberately did
                          not take).
  * resolve_human         ESCALATED -> RESOLVED directly (operator closes the
                          case by hand).
"""

from __future__ import annotations

import logging

from . import repo
from .act import execute_action
from .audit import write_audit
from .config import Settings, get_settings
from .db import Database
from .models import Action, AuditLogEntry, CaseState, DecideOutput
from .state_machine import CaseStateMachine, IllegalTransitionError

logger = logging.getLogger("reclaim.manual")

# Human action labels used in the audit trail. Kept distinct from agent stage
# names ("diagnose"/"decide"/"act") so a reader sees immediately it is manual.
_HUMAN_REASONING = "manual override by operator"


def approve_manual_retry(
    db: Database, case_id: str, settings: Settings | None = None
) -> str:
    """Authorise a retry for an ESCALATED case.

    Legal only when the case is ESCALATED. Transitions ESCALATED -> ACTING,
    executes the retry through the (idempotent) Act layer, then lands on
    RESOLVED or FAILED. Returns the terminal outcome.
    """
    settings = settings or get_settings()
    row = repo.get_case_row(db, case_id)
    if row is None:
        raise KeyError(f"unknown case_id {case_id}")
    case = repo.row_to_case(row)

    machine = CaseStateMachine(initial=case.state)
    if case.state != CaseState.ESCALATED:
        raise IllegalTransitionError(
            f"approve_manual_retry requires ESCALATED, got {case.state}"
        )

    # ESCALATED -> ACTING under the operator's authority.
    machine.approve_retry()
    repo.set_case_state(db, case_id, CaseState.ACTING)
    write_audit(
        db,
        AuditLogEntry(
            case_id=case_id,
            stage="manual_override",
            agent_reasoning=_HUMAN_REASONING,
            input_state={"case_id": case_id, "state": "ESCALATED"},
            decision="manual_approve_retry",
            action_taken="approve_manual_retry",
            outcome="ACTING/manual_approve_retry",
            fallback_triggered=False,
        ),
    )

    # Execute the retry exactly like the pipeline would — idempotent via the
    # ledger, fault-isolated via the Act layer. A duplicate claim is a no-op.
    decision = DecideOutput(
        action=Action.RETRY_NOW,
        reasoning="manual override by operator: approved manual retry",
    )
    result = execute_action(db, case, decision, settings)

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
            case_id=case_id,
            stage="manual_override",
            agent_reasoning=_HUMAN_REASONING,
            input_state={"case_id": case_id, "state": "ACTING"},
            decision="manual_approve_retry",
            action_taken=result.action_taken,
            outcome=f"{result.terminal_state.value}/manual_retry"
                    f"{' DUP' if result.idempotent_duplicate else ''}",
            fallback_triggered=False,
        ),
    )
    logger.info("MANUAL_APPROVE_RETRY case=%s terminal=%s", case_id, result.terminal_state.value)
    return result.terminal_state.value


def resolve_human(db: Database, case_id: str, settings: Settings | None = None) -> str:
    """Close an ESCALATED case by hand: ESCALATED -> RESOLVED directly.

    No payment action is taken — this records that a human resolved the case.
    Legal only when the case is ESCALATED.
    """
    settings = settings or get_settings()  # noqa: F841 (exists for symmetry/future use)
    row = repo.get_case_row(db, case_id)
    if row is None:
        raise KeyError(f"unknown case_id {case_id}")
    case = repo.row_to_case(row)

    machine = CaseStateMachine(initial=case.state)
    if case.state != CaseState.ESCALATED:
        raise IllegalTransitionError(
            f"resolve_human requires ESCALATED, got {case.state}"
        )

    machine.resolve_human()  # ESCALATED -> RESOLVED
    repo.set_case_state(db, case_id, CaseState.RESOLVED)
    write_audit(
        db,
        AuditLogEntry(
            case_id=case_id,
            stage="manual_override",
            agent_reasoning=_HUMAN_REASONING,
            input_state={"case_id": case_id, "state": "ESCALATED"},
            decision="manual_resolve_human",
            action_taken="resolve_human",
            outcome="RESOLVED/manual_resolve_human",
            fallback_triggered=False,
        ),
    )
    logger.info("MANUAL_RESOLVE_HUMAN case=%s", case_id)
    return CaseState.RESOLVED.value
