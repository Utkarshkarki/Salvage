"""
Hash-chained audit log verification — walks the full audit log and confirms
every entry's hash matches a fresh recomputation from prev_hash + its content.

Uses shared canonicalization from audit_chain.py to ensure write and verify
paths compute identical hashes.
"""

import sys
from dataclasses import dataclass

from sqlalchemy.orm import Session

from .db import get_db, AuditLogRow
from .audit_chain import (
    _canonical_entry_dict,
    compute_entry_hash as _compute_entry_hash,
    GENESIS_HASH,
)


@dataclass(frozen=True)
class AuditChainVerificationResult:
    """Result of audit chain verification."""

    is_valid: bool
    total_entries: int
    first_broken_entry_id: int | None
    first_broken_entry_reason: str | None


def verify_audit_chain(session: Session) -> AuditChainVerificationResult:
    """
    Walk the full audit log in order and verify every entry's hash.

    Uses the shared _canonical_entry_dict from audit_chain.py to ensure
    the verification path produces identical hashes to the write path.

    Args:
        session: SQLAlchemy session to query from.

    Returns:
        AuditChainVerificationResult indicating validity and any breaks.
    """
    # Fetch all audit entries ordered by id (insertion order)
    entries = session.query(AuditLogRow).order_by(AuditLogRow.id).all()

    if not entries:
        # Empty log is valid
        return AuditChainVerificationResult(
            is_valid=True,
            total_entries=0,
            first_broken_entry_id=None,
            first_broken_entry_reason=None,
        )

    # Walk the chain
    prev_hash = GENESIS_HASH

    for entry in entries:
        # Compute what this entry's hash should be using shared canonicalization
        entry_dict = _canonical_entry_dict(entry)
        expected_hash = _compute_entry_hash(prev_hash, entry_dict)

        # Compare against stored hash
        if entry.entry_hash != expected_hash:
            return AuditChainVerificationResult(
                is_valid=False,
                total_entries=len(entries),
                first_broken_entry_id=entry.id,
                first_broken_entry_reason=(
                    f"Hash mismatch at entry {entry.id}: expected {expected_hash}, "
                    f"got {entry.entry_hash}"
                ),
            )

        # Verify that prev_hash matches what we expect
        if entry.prev_hash != prev_hash:
            return AuditChainVerificationResult(
                is_valid=False,
                total_entries=len(entries),
                first_broken_entry_id=entry.id,
                first_broken_entry_reason=(
                    f"Prev hash mismatch at entry {entry.id}: expected {prev_hash}, "
                    f"got {entry.prev_hash}"
                ),
            )

        # Move to next
        prev_hash = entry.entry_hash

    # All entries valid
    return AuditChainVerificationResult(
        is_valid=True,
        total_entries=len(entries),
        first_broken_entry_id=None,
        first_broken_entry_reason=None,
    )


if __name__ == "__main__":
    # The report prints ✓ (U+2713); a Windows console on the default cp1252
    # codec raises UnicodeEncodeError on it. Force UTF-8 so verification never
    # crashes on output regardless of the terminal's default codec.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    db = get_db()
    session = db.create_session()

    try:
        result = verify_audit_chain(session)

        print("\n" + "=" * 60)
        print("Audit Chain Verification")
        print("=" * 60)

        if result.is_valid:
            print(f"✓ Chain is valid ({result.total_entries} entries)")
            sys.exit(0)
        else:
            print(f"✗ Chain is BROKEN")
            print(f"  Total entries: {result.total_entries}")
            print(f"  First broken entry ID: {result.first_broken_entry_id}")
            print(f"  Reason: {result.first_broken_entry_reason}")
            print("=" * 60)
            sys.exit(1)
    finally:
        session.close()
