"""Append-only audit-log writer.

One immutable row per stage transition. Insert-only by contract: the writer
submits a row and commits; it never updates or deletes. ``fallback_triggered``
flags whether the stage hit a deterministic fallback (LLM failure OR a
stopping-rule override) instead of a clean LLM output — the demo's key signal.
"""

from __future__ import annotations

import logging

from .db import AuditLogRow, Database
from .models import AuditLogEntry

logger = logging.getLogger("reclaim.audit")


def write_audit(db: Database, entry: AuditLogEntry) -> None:
    """Persist one append-only audit row.

    Never raises: an audit write failure must not crash the money flow it is
    documenting, so it is logged and swallowed (the case still proceeds).
    """
    row = AuditLogRow(
        case_id=entry.case_id,
        stage=entry.stage,
        agent_reasoning=entry.agent_reasoning,
        input_state=entry.input_state,
        decision=entry.decision,
        action_taken=entry.action_taken,
        outcome=entry.outcome,
        fallback_triggered=entry.fallback_triggered,
        timestamp=entry.timestamp,
    )
    try:
        with db.create_session() as session:
            session.add(row)
            session.commit()
    except Exception as exc:  # pragma: no cover - best-effort by design
        logger.error("AUDIT_WRITE_FAILED case=%s stage=%s err=%s",
                     entry.case_id, entry.stage, exc)
