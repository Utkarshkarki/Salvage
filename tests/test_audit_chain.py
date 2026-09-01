"""Tests for hash-chained audit log verification."""

import pytest
from datetime import datetime, UTC

from reclaim.models import AuditLogEntry
from reclaim.audit import finalize_audit_chain, write_audit
from reclaim.verify_audit_chain import (
    verify_audit_chain,
    AuditChainVerificationResult,
    _canonical_entry_dict,
    _compute_entry_hash,
    GENESIS_HASH,
)
from reclaim.db import AuditLogRow, utcnow


def test_canonical_entry_dict():
    """Test that canonical dict representation works."""
    entry = AuditLogEntry(
        case_id="case_123",
        stage="diagnose",
        agent_reasoning="test reasoning",
        input_state={"key": "value"},
        decision="DIAGNOSED",
        action_taken=None,
        outcome="SUCCESS",
        fallback_triggered=False,
        timestamp=utcnow(),
    )

    canonical = _canonical_entry_dict(entry)

    assert canonical["case_id"] == "case_123"
    assert canonical["stage"] == "diagnose"
    assert canonical["agent_reasoning"] == "test reasoning"
    assert canonical["input_state"] == {"key": "value"}
    assert canonical["decision"] == "DIAGNOSED"
    assert canonical["action_taken"] is None
    assert canonical["outcome"] == "SUCCESS"
    assert canonical["fallback_triggered"] is False
    # Timestamp is ISO format string
    assert isinstance(canonical["timestamp"], str)


def test_compute_entry_hash():
    """Test hash computation."""
    # Test with known inputs
    entry_dict = {
        "case_id": "case_1",
        "stage": "ingest",
        "agent_reasoning": "",
        "input_state": {},
        "decision": "",
        "action_taken": None,
        "outcome": "INGESTED",
        "fallback_triggered": False,
        "timestamp": "2026-01-01T00:00:00+00:00",
    }

    # First entry uses genesis hash
    hash1 = _compute_entry_hash(GENESIS_HASH, entry_dict)
    assert isinstance(hash1, str)
    assert len(hash1) == 64  # SHA256 hex digest

    # Different prev_hash should give different result
    hash2 = _compute_entry_hash("different_hash", entry_dict)
    assert hash2 != hash1

    # Same inputs should give same hash (deterministic)
    hash3 = _compute_entry_hash(GENESIS_HASH, entry_dict)
    assert hash3 == hash1


def test_verify_empty_chain(db):
    """Test that empty audit log verifies as valid."""
    with db.create_session() as session:
        result = verify_audit_chain(session)

    assert result.is_valid
    assert result.total_entries == 0
    assert result.first_broken_entry_id is None
    assert result.first_broken_entry_reason is None


def test_verify_healthy_chain(db):
    """Test that a healthy chain verifies clean."""
    # Write a few audit entries
    entry1 = AuditLogEntry(
        case_id="case_1",
        stage="ingest",
        agent_reasoning="",
        input_state={},
        decision="",
        action_taken=None,
        outcome="INGESTED",
        fallback_triggered=False,
        timestamp=utcnow(),
    )
    write_audit(db, entry1)

    entry2 = AuditLogEntry(
        case_id="case_1",
        stage="diagnose",
        agent_reasoning="test",
        input_state={},
        decision="INSUFFICIENT_FUNDS",
        action_taken=None,
        outcome="DIAGNOSED",
        fallback_triggered=False,
        timestamp=utcnow(),
    )
    write_audit(db, entry2)

    # The chain is derived by a single sequential finalize pass (not at write
    # time), so finalize before verifying.
    finalize_audit_chain(db)

    # Verify the chain
    with db.create_session() as session:
        result = verify_audit_chain(session)

    assert result.is_valid
    assert result.total_entries == 2
    assert result.first_broken_entry_id is None
    assert result.first_broken_entry_reason is None


def test_verify_detects_mutated_entry(db):
    """Test that mutating an entry breaks the chain from that point."""
    # Write initial entries
    entry1 = AuditLogEntry(
        case_id="case_1",
        stage="ingest",
        agent_reasoning="",
        input_state={},
        decision="",
        action_taken=None,
        outcome="INGESTED",
        fallback_triggered=False,
        timestamp=utcnow(),
    )
    write_audit(db, entry1)

    entry2 = AuditLogEntry(
        case_id="case_1",
        stage="diagnose",
        agent_reasoning="test",
        input_state={},
        decision="INSUFFICIENT_FUNDS",
        action_taken=None,
        outcome="DIAGNOSED",
        fallback_triggered=False,
        timestamp=utcnow(),
    )
    write_audit(db, entry2)

    # Chain is set by a sequential finalize pass, THEN we tamper — so
    # verification must still catch the mutation.
    finalize_audit_chain(db)

    # Mutate the first entry's outcome
    with db.create_session() as session:
        row = session.query(AuditLogRow).filter(AuditLogRow.id == 1).first()
        if row:
            # Mutate the outcome field
            row.outcome = "TAMPERED"
            session.commit()

    # Verify the chain - should detect the break
    with db.create_session() as session:
        result = verify_audit_chain(session)

    assert not result.is_valid
    # The second entry should be flagged as broken (since first entry's hash is wrong)
    assert result.first_broken_entry_id is not None
    assert result.first_broken_entry_reason is not None


def test_verify_detects_broken_prev_hash(db):
    """Test that mutating prev_hash is detected."""
    # Write initial entries
    entry1 = AuditLogEntry(
        case_id="case_1",
        stage="ingest",
        agent_reasoning="",
        input_state={},
        decision="",
        action_taken=None,
        outcome="INGESTED",
        fallback_triggered=False,
        timestamp=utcnow(),
    )
    write_audit(db, entry1)

    entry2 = AuditLogEntry(
        case_id="case_1",
        stage="diagnose",
        agent_reasoning="test",
        input_state={},
        decision="INSUFFICIENT_FUNDS",
        action_taken=None,
        outcome="DIAGNOSED",
        fallback_triggered=False,
        timestamp=utcnow(),
    )
    write_audit(db, entry2)

    # Chain is set by a sequential finalize pass, THEN we tamper prev_hash.
    finalize_audit_chain(db)

    # Mutate the second entry's prev_hash
    with db.create_session() as session:
        row = session.query(AuditLogRow).filter(AuditLogRow.id == 2).first()
        if row:
            row.prev_hash = "tampered_hash"
            session.commit()

    # Verify the chain - should detect the break
    with db.create_session() as session:
        result = verify_audit_chain(session)

    assert not result.is_valid
    assert result.first_broken_entry_id == 2


def test_audit_chain_verification_result_dataclass():
    """Test the AuditChainVerificationResult dataclass."""
    result = AuditChainVerificationResult(
        is_valid=True,
        total_entries=5,
        first_broken_entry_id=None,
        first_broken_entry_reason=None,
    )

    assert result.is_valid
    assert result.total_entries == 5
    assert result.first_broken_entry_id is None
    assert result.first_broken_entry_reason is None


def test_hash_chain_with_multiple_cases(db):
    """Test that hash chain works correctly with multiple cases."""
    # Case 1
    entry_c1_1 = AuditLogEntry(
        case_id="case_1",
        stage="ingest",
        agent_reasoning="",
        input_state={},
        decision="",
        action_taken=None,
        outcome="INGESTED",
        fallback_triggered=False,
        timestamp=utcnow(),
    )
    write_audit(db, entry_c1_1)

    # Case 2
    entry_c2_1 = AuditLogEntry(
        case_id="case_2",
        stage="ingest",
        agent_reasoning="",
        input_state={},
        decision="",
        action_taken=None,
        outcome="INGESTED",
        fallback_triggered=False,
        timestamp=utcnow(),
    )
    write_audit(db, entry_c2_1)

    # Case 1 again
    entry_c1_2 = AuditLogEntry(
        case_id="case_1",
        stage="diagnose",
        agent_reasoning="",
        input_state={},
        decision="UNKNOWN",
        action_taken=None,
        outcome="DIAGNOSED",
        fallback_triggered=False,
        timestamp=utcnow(),
    )
    write_audit(db, entry_c1_2)

    # Chain is derived by the sequential finalize pass across ALL cases.
    finalize_audit_chain(db)

    # Verify the chain
    with db.create_session() as session:
        result = verify_audit_chain(session)

    assert result.is_valid
    assert result.total_entries == 3


def test_unfinalized_log_is_detected(db):
    """Chain linkage is established only by finalize_audit_chain (sequential),
    never at write time (which would race). An unfinalized log has empty hash
    columns and must therefore FAIL verification — not silently pass."""
    write_audit(
        db,
        AuditLogEntry(
            case_id="case_1", stage="ingest", agent_reasoning="",
            input_state={}, decision="", action_taken=None,
            outcome="INGESTED", fallback_triggered=False, timestamp=utcnow(),
        ),
    )

    with db.create_session() as session:
        result = verify_audit_chain(session)

    assert not result.is_valid  # not chained yet
    assert result.first_broken_entry_id is not None
    assert result.total_entries == 1


def test_finalize_chain_is_deterministic(db):
    """Re-running finalize over unchanged content yields identical hashes."""
    write_audit(
        db,
        AuditLogEntry(
            case_id="case_1", stage="ingest", agent_reasoning="a",
            input_state={}, decision="", action_taken=None,
            outcome="INGESTED", fallback_triggered=False, timestamp=utcnow(),
        ),
    )
    write_audit(
        db,
        AuditLogEntry(
            case_id="case_1", stage="diagnose", agent_reasoning="b",
            input_state={}, decision="INSUFFICIENT_FUNDS", action_taken=None,
            outcome="DIAGNOSED", fallback_triggered=False, timestamp=utcnow(),
        ),
    )

    first = finalize_audit_chain(db)
    with db.create_session() as s:
        before = [
            (r.id, r.prev_hash, r.entry_hash)
            for r in s.query(AuditLogRow).order_by(AuditLogRow.id).all()
        ]

    again = finalize_audit_chain(db)
    with db.create_session() as s:
        after = [
            (r.id, r.prev_hash, r.entry_hash)
            for r in s.query(AuditLogRow).order_by(AuditLogRow.id).all()
        ]

    assert first == again == 2
    assert before == after  # deterministic: unchanged content -> unchanged chain


def test_verify_audit_chain_import():
    """Test that the verify_audit_chain module can be imported."""
    import importlib
    module = importlib.import_module("reclaim.verify_audit_chain")
    assert hasattr(module, "verify_audit_chain")
    assert hasattr(module, "AuditChainVerificationResult")
    assert hasattr(module, "_compute_entry_hash")
    assert hasattr(module, "_canonical_entry_dict")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
