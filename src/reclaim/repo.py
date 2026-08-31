"""Read/update helpers over the persistence layer.

Kept separate from Act so the pipeline reads state, counts enforcement inputs,
and persists transitions without reaching into SQL directly.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from .db import AuditLogRow, Database, ExecutedActionRow, RecoveryCaseRow
from .models import AuditLogEntry, CaseState, RecoveryCase
from .webhook import _row_to_case as _webhook_row_to_case  # shared mapping

logger = logging.getLogger("reclaim.repo")


def row_to_case(row: RecoveryCaseRow) -> RecoveryCase:
    """Public alias for the row -> RecoveryCase mapping (shared with webhook)."""
    return _webhook_row_to_case(row)

PM_UPDATE_ACTION = "request_payment_method_update"


def get_case_row(db: Database, case_id: str) -> RecoveryCaseRow | None:
    with db.create_session() as session:
        return session.query(RecoveryCaseRow).filter_by(case_id=case_id).first()


def all_case_rows(db: Database) -> list[RecoveryCaseRow]:
    with db.create_session() as session:
        return list(session.query(RecoveryCaseRow).order_by(RecoveryCaseRow.id).all())


def list_cases(
    db: Database,
    *,
    state: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[RecoveryCaseRow]:
    """Query-layer filtered + paginated case listing (for the /api/v1/cases route).

    Returns up to ``limit`` case rows starting at ``offset``, optionally
    filtered by an exact ``state``. All filtering/pagination happens in SQL —
    not by slicing the full result set in Python — so the endpoint stays bounded
    as the dataset grows. Rows are ordered by id (ingest order), which keeps
    pagination stable across pages.
    """
    with db.create_session() as session:
        query = session.query(RecoveryCaseRow).order_by(RecoveryCaseRow.id)
        if state is not None:
            query = query.filter(RecoveryCaseRow.state == state)
        return list(query.offset(offset).limit(limit).all())


def set_case_state(db: Database, case_id: str, state: CaseState) -> None:
    """Persist the authoritative state machine position for a case."""
    with db.create_session() as session:
        row = session.query(RecoveryCaseRow).filter_by(case_id=case_id).first()
        if row is None:
            raise KeyError(f"no case row for {case_id}")
        row.state = state.value
        session.commit()


def audit_trail(db: Database, case_id: str) -> list[AuditLogEntry]:
    """Full decision trail for one case, oldest first."""
    with db.create_session() as session:
        rows = (
            session.query(AuditLogRow)
            .filter_by(case_id=case_id)
            .order_by(AuditLogRow.id.asc())
            .all()
        )
    return [
        AuditLogEntry(
            case_id=r.case_id,
            stage=r.stage,
            agent_reasoning=r.agent_reasoning,
            input_state=r.input_state or {},
            decision=r.decision,
            action_taken=r.action_taken,
            outcome=r.outcome,
            fallback_triggered=r.fallback_triggered,
            timestamp=r.timestamp,
        )
        for r in rows
    ]


def count_recent_payment_method_updates(
    db: Database, customer_id: str, within_hours: float
) -> int:
    """Payment-method-update emails sent to this customer in the window.

    Joins executed actions -> cases on case_id, so the 7-day email cap is
    enforced per customer regardless of which case the email was tied to.
    """
    cutoff = datetime.now(UTC) - timedelta(hours=within_hours)
    with db.create_session() as session:
        rows = (
            session.query(ExecutedActionRow)
            .join(RecoveryCaseRow, RecoveryCaseRow.case_id == ExecutedActionRow.case_id)
            .filter(RecoveryCaseRow.customer_id == customer_id)
            .filter(ExecutedActionRow.action == PM_UPDATE_ACTION)
            .filter(ExecutedActionRow.executed_at >= cutoff)
            .all()
        )
    return len(rows)
