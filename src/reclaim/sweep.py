"""Stale-lock reconciliation: recovery from a mid-pipeline crash.

If the process dies between the DECIDED and a completed ACT, a case can be left
in the ``ACTING`` state forever — the next webhook for that subscription will
never fire because this one was never finished, and the state machine is the
only source of progress. This module is the safety net: a sweep that finds
cases stuck in ``ACTING`` past a configurable timeout and safely reconciles
them to ``ESCALATED`` (human review) rather than leaving them wedged.

Safety properties:
  * It NEVER runs a side-effecting action — escalating is a pure state change
    that hands the outcome to a human. Recovery after a crash is therefore a
    deliberate, human-authorized decision via the manual override path, which
    is itself idempotent (any already-claimed idempotency key is a no-op).
  * It consults the SAME state machine + append-only audit trail as the
    pipeline, so no stage progress bypasses the audit.
  * A legitimately in-progress case (its last ACTING entry is younger than the
    timeout) is left untouched.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from . import repo
from .audit import write_audit
from .config import Settings
from .db import Database, utcnow
from .models import AuditLogEntry, CaseState
from .state_machine import CaseStateMachine

logger = logging.getLogger("reclaim.sweep")

_SWEEP_REASONING = "stale ACTING lock: process interrupted mid-pipeline; swept to human review"
_SWEEP_OUTCOME = "ESCALATED/sweep_stale_lock"


def _as_utc(dt: datetime) -> datetime:
    """Coerce a possibly-naive datetime to a timezone-aware UTC datetime.

    SQLite (unlike Postgres) does not preserve timezone info on read, so audit
    / row timestamps can come back naive even though they were written aware.
    """
    if dt is None:
        return dt
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _entered_acting_at(db: Database, case_id: str) -> datetime | None:
    """Timestamp of the most recent entry into ACTING, from the audit trail.

    The pipeline (and the manual-approve path) writes an ``act`` stage entry
    with ``outcome`` containing "ACTING" the moment the case enters ACTING; we
    use that as the anchor so the timeout reflects when the case actually began
    acting, not when it was created.
    """
    for entry in reversed(repo.audit_trail(db, case_id)):
        if entry.stage == "act" and entry.outcome and "ACTING" in entry.outcome:
            return _as_utc(entry.timestamp)
    return None


def find_stale_acting(
    db: Database, settings: Settings, now: datetime | None = None
) -> list[object]:
    """All case rows stuck in ACTING for longer than ``stale_lock_timeout_seconds``.

    ``now`` is injectable for deterministic tests. Returns ORM ``RecoveryCaseRow``
    objects (read-only here).
    """
    now = now or utcnow()
    stale: list[object] = []
    now = _as_utc(now)
    for row in repo.all_case_rows(db):
        if row.state != CaseState.ACTING.value:
            continue
        entered = _entered_acting_at(db, row.case_id)
        anchor = _as_utc(entered or row.created_at)
        if anchor is None:
            continue
        if (now - anchor).total_seconds() > settings.stale_lock_timeout_seconds:
            stale.append(row)
    return stale


def reconcile_stale_acting(
    db: Database, settings: Settings, now: datetime | None = None
) -> list[str]:
    """Sweep stale ACTING cases to ESCALATED and return their case ids.

    Each swept case: ESCALATED via the state machine, its persisted state
    advanced, and an append-only ``stage="sweep"`` audit entry recorded — so a
    viewer of the trail can see it was the reconciliation sweep, not an agent,
    that moved it. Never touches legitimately in-progress cases.
    """
    swept: list[str] = []
    for row in find_stale_acting(db, settings, now=now):
        machine = CaseStateMachine(initial=CaseState(row.state))
        machine.escalate()  # ACTING -> ESCALATED (guarded, auditable)
        repo.set_case_state(db, row.case_id, CaseState.ESCALATED)
        write_audit(
            db,
            AuditLogEntry(
                case_id=row.case_id,
                stage="sweep",
                agent_reasoning=_SWEEP_REASONING,
                input_state={
                    "case_id": row.case_id,
                    "state": "ACTING",
                    "timeout_seconds": settings.stale_lock_timeout_seconds,
                },
                decision="escalate_human",
                action_taken="escalate_human",
                outcome=_SWEEP_OUTCOME,
                fallback_triggered=False,  # a policy safeguard, not an LLM failure
            ),
        )
        logger.info("SWEEP_STALE_ACTING case=%s -> ESCALATED", row.case_id)
        swept.append(row.case_id)
    return swept
