"""Hash-chain integrity under REAL concurrent audit writes (Phase 5 regression).

The audit chain must be derived by a single sequential pass
(``audit.finalize_audit_chain``) AFTER the concurrent write phase. Computing
chain linkage at write time — read "the most recent row by id", then insert —
is a read-then-write race: under real concurrency (e.g. the batch's
``ConcurrencyLimiter(max_concurrency=5)``) two writers can both read the same
stale "latest row" before either commits, forking the chain into two entries
that claim the same predecessor. Verification (which walks by id and requires
each entry to chain onto the previous one) then reports a broken chain on an
otherwise-healthy batch — which is exactly what ``verify_audit_chain`` reported
on a real concurrent batch run.

This regression writes through the REAL write path from many threads firing at
once (mirroring ``tests/adversarial/test_concurrency.py``), then finalizes
sequentially and asserts the resulting chain still verifies cleanly. Sequential
write-then-verify tests cannot catch this class of bug — only real concurrency
can.
"""

from __future__ import annotations

import threading

from reclaim.audit import finalize_audit_chain, write_audit
from reclaim.db import AuditLogRow, utcnow
from reclaim.models import AuditLogEntry
from reclaim.verify_audit_chain import verify_audit_chain


def test_chain_verifies_after_concurrent_writes(db) -> None:
    """Many threads writing audit entries at once must not fork the chain."""
    n_threads = 8
    per_thread = 25
    total = n_threads * per_thread

    barrier = threading.Barrier(n_threads)  # synchronize start so writes collide

    def worker(t: int) -> None:
        barrier.wait()
        for i in range(per_thread):
            # write_audit is best-effort (never raises), so a swallowed write
            # would silently drop a row — the row-count assertion below catches
            # that rather than relying on exceptions propagating.
            write_audit(
                db,
                AuditLogEntry(
                    case_id=f"case_{t}_{i}",
                    stage="test",
                    agent_reasoning="",
                    input_state={},
                    decision="",
                    action_taken=None,
                    outcome="INGESTED",
                    fallback_triggered=False,
                    timestamp=utcnow(),
                ),
            )

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Every concurrent append landed (no rows silently lost to lock contention).
    with db.create_session() as s:
        row_count = s.query(AuditLogRow).count()
    assert row_count == total, f"lost concurrent writes: {row_count} != {total}"

    # The chain is derived in one sequential pass, then must verify clean.
    finalized = finalize_audit_chain(db)
    assert finalized == total

    with db.create_session() as s:
        result = verify_audit_chain(s)
    assert result.is_valid, (
        f"chain broken after concurrent writes: "
        f"{result.first_broken_entry_reason}"
    )
    assert result.total_entries == total
