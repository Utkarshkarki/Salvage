"""
Hash-chain utilities for audit log tamper-evidence.

Shared canonical serialization and hash computation used by both
the write path (audit.py) and verification path (verify_audit_chain.py).
"""

import hashlib
import json
from datetime import datetime

from .db import AuditLogRow

# Genesis hash for the first entry in the chain
GENESIS_HASH = "0" * 64


def _canonical_entry_dict(row_or_dict) -> dict:
    """
    Build a canonical dict representation of an audit entry for hashing.

    IMPORTANT: This must produce identical output for the same data regardless of
    whether it comes from an ORM row (after JSON round-trip through SQLite) or a dict.
    We use ISO format with 'Z' suffix for UTC to ensure consistency.

    Args:
        row_or_dict: Either an AuditLogRow ORM object or a dict with the same fields

    Returns:
        Canonical dict with all fields except hash columns, suitable for deterministic hashing
    """
    # Handle both ORM row and dict input
    if isinstance(row_or_dict, dict):
        case_id = row_or_dict.get("case_id", "")
        stage = row_or_dict.get("stage", "")
        agent_reasoning = row_or_dict.get("agent_reasoning", "")
        input_state = row_or_dict.get("input_state", {})
        decision = row_or_dict.get("decision", "")
        action_taken = row_or_dict.get("action_taken")
        outcome = row_or_dict.get("outcome", "")
        fallback_triggered = row_or_dict.get("fallback_triggered", False)
        rule_override = row_or_dict.get("rule_override", False)
        timestamp = row_or_dict.get("timestamp")
    else:
        # ORM row
        case_id = row_or_dict.case_id
        stage = row_or_dict.stage
        agent_reasoning = row_or_dict.agent_reasoning
        input_state = row_or_dict.input_state
        decision = row_or_dict.decision
        action_taken = row_or_dict.action_taken
        outcome = row_or_dict.outcome
        fallback_triggered = row_or_dict.fallback_triggered
        rule_override = row_or_dict.rule_override
        timestamp = row_or_dict.timestamp

    # Normalize timestamp to ISO format UTC.
    # Must be identical for timezone-aware (write path) and naive/SQLite-readback
    # (verify path) datetimes. Using strftime uniformly drops microseconds and
    # ensures the same string regardless of tzinfo presence.
    if isinstance(timestamp, datetime):
        ts_str = timestamp.strftime("%Y-%m-%dT%H:%M:%SZ")
    else:
        ts_str = str(timestamp) if timestamp else ""

    # Normalize input_state: ensure dict type and consistent representation
    # SQLite JSON column returns various types; we normalize to a consistent form
    if input_state is None:
        input_state = {}
    elif not isinstance(input_state, dict):
        # If it's a string (JSON), parse it
        if isinstance(input_state, str):
            try:
                input_state = json.loads(input_state)
            except (json.JSONDecodeError, TypeError):
                input_state = {"raw": str(input_state)}
        else:
            input_state = {"raw": str(input_state)}

    return {
        "case_id": case_id,
        "stage": stage,
        "agent_reasoning": agent_reasoning,
        "input_state": input_state,
        "decision": decision,
        "action_taken": action_taken,
        "outcome": outcome,
        "fallback_triggered": fallback_triggered,
        "rule_override": rule_override,
        "timestamp": ts_str,
    }


def compute_entry_hash(prev_hash: str, entry_dict: dict) -> str:
    """
    Compute the hash for an entry given prev_hash and its canonical dict.

    entry_hash = sha256(prev_hash + canonical_json_sorted)

    Args:
        prev_hash: The hash of the previous entry (or GENESIS_HASH for first entry)
        entry_dict: Canonical dict from _canonical_entry_dict

    Returns:
        64-character hexadecimal SHA256 hash
    """
    canonical_json = json.dumps(entry_dict, sort_keys=True, separators=(",", ":"))
    combined = prev_hash + canonical_json
    return hashlib.sha256(combined.encode()).hexdigest()


def chain_rows(entries: list) -> list[tuple[str, str, str]]:
    """
    Compute the full hash chain for an ordered list of entries in one pass.

    This is the single source of truth for chain linkage. It is purely
    functional (no DB access) and sequential: each link is derived from the
    *previous entry's computed hash*, never from a concurrent read of "the
    most recent row". That makes it race-free by construction — the audit
    chain can never fork because two entries both claim the same predecessor.

    Args:
        entries: Rows / dicts in insertion order (by autoincrement id).

    Returns:
        A parallel list of ``(id, prev_hash, entry_hash)`` tuples.
    """
    prev_hash = GENESIS_HASH
    out: list[tuple[str, str, str]] = []
    for entry in entries:
        entry_dict = _canonical_entry_dict(entry)
        entry_hash = compute_entry_hash(prev_hash, entry_dict)
        out.append((entry.id, prev_hash, entry_hash))
        prev_hash = entry_hash
    return out