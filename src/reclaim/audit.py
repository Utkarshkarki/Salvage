"""Append-only audit-log writer.

One immutable row per stage transition. Insert-only by contract: the writer
submits a row and commits; it never updates or deletes. ``fallback_triggered``
flags whether the stage hit a deterministic fallback (LLM failure OR a
stopping-rule override) instead of a clean LLM output — the demo's key signal.

Hash-chained for tamper detection (Phase 5). IMPORTANT design note: chain
linkage is NOT computed at write time. Computing it would require reading "the
most recent row by id" and then inserting — a read-then-write sequence that
races under the batch's concurrent writers (two writes can both read the same
stale latest row, forking the chain). Instead, ``write_audit`` appends content
and leaves the chain columns empty; the hash chain is derived by
``finalize_audit_chain`` in a single sequential pass over the log ordered by
autoincrement ``id``. The batch CLI calls ``finalize_audit_chain`` once after
all concurrent writes complete.
"""

from __future__ import annotations

import logging

from .db import AuditLogRow, Database
from .models import AuditLogEntry
from .audit_chain import (
    _canonical_entry_dict,
    chain_rows,
    GENESIS_HASH,
)

logger = logging.getLogger("reclaim.audit")


def write_audit(db: Database, entry: AuditLogEntry) -> None:
    """Persist one append-only audit row.

    Never raises: an audit write failure must not crash the money flow it is
    documenting, so it is logged and swallowed (the case still proceeds).

    Chain columns (``prev_hash`` / ``entry_hash``) are left at their defaults
    — linkage is derived by ``finalize_audit_chain`` in a sequential pass, not
    at write time (see module docstring for why). Safe under concurrency.
    """
    try:
        with db.create_session() as session:
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
                # prev_hash / entry_hash use their column defaults ("").
            )
            session.add(row)
            session.commit()
    except Exception as exc:  # pragma: no cover - best-effort by design
        logger.error("AUDIT_WRITE_FAILED case=%s stage=%s err=%s",
                     entry.case_id, entry.stage, exc)


def finalize_audit_chain(db: Database) -> int:
    """Recompute the audit log's full hash chain in one sequential pass.

    Walks every row ordered by autoincrement ``id`` (append order) and derives
    each entry's ``prev_hash`` / ``entry_hash`` from the *already-committed*
    predecessor. Single-writer by construction: call it once after the
    concurrent write phase (e.g. after ``run_batch``), never from the
    concurrent writers themselves — the pass has no read-then-write race.

    Idempotent and deterministic: re-running over unchanged content yields the
    same hashes, so verification after a fresh finalize still catches later
    content tampering.

    Returns the number of rows chained.
    """
    with db.create_session() as session:
        rows = session.query(AuditLogRow).order_by(AuditLogRow.id.asc()).all()
        # chain_rows is order-preserving on the same list, so zip is correct.
        for row, (_rid, prev_hash, entry_hash) in zip(rows, chain_rows(rows)):
            row.prev_hash = prev_hash
            row.entry_hash = entry_hash
        session.commit()
    return len(rows)
