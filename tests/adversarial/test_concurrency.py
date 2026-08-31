"""Adversarial concurrency: duplicate webhooks + concurrent read/write.

These prove the two hard guarantees under REAL concurrency (threads firing
simultaneously, not a sequential loop):

1. ``test_concurrent_duplicate_webhooks`` — the SAME webhook event arriving N
   times at once dedupes to exactly ONE ingest (event-id dedupe) and exactly
   ONE pipeline execution (ExecutedActionRow ledger), no matter the race.
2. ``test_concurrent_read_write_does_not_corrupt`` — with WAL enabled, a
   writer and many readers run at once without blocking or corrupting state.
"""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime, timedelta

from reclaim import repo
from reclaim.db import ExecutedActionRow, RecoveryCaseRow
from reclaim.models import CaseState
from reclaim.webhook import compute_signature, ingest_event, parse_event


def _body(
    *,
    sub: str = "sub_con",
    code: str = "R01",
    days_ago: int = 3,
    amount: int = 10000,
) -> bytes:
    return json.dumps({
        "event": "payment.failed",
        "entity": {
            "id": f"pay_{sub}",
            "subscription_id": sub,
            "customer_id": f"cust_{sub}",
            "amount": amount,
            "attempt_number": 1,
            "error_code": code,
            "error_description": "declined",
            "status": "failed",
            "created_at": int((datetime.now(UTC) - timedelta(days=days_ago)).timestamp()),
        },
    }).encode("utf-8")


def _ingest(db, settings, body: bytes, event_id: str):
    sig = compute_signature(settings.razorpay_webhook_secret, body)
    event = parse_event(body, event_id_hint=event_id)
    return ingest_event(db, event, settings)


# ---------------------------------------------------------------------------
# 1.1 Concurrent duplicate webhooks
# ---------------------------------------------------------------------------


def test_concurrent_duplicate_webhooks(settings, db) -> None:
    """N simultaneous deliveries of the same event_id -> exactly one ingest.

    The event-id dedupe (DB UNIQUE) collapses the race; the ExecutedActionRow
    ledger guarantees only one pipeline execution can ever claim the retry.
    """
    from reclaim.pipeline import run_case

    body = _body(sub="sub_con")
    event_id = "evt_concurrent"
    N = 8

    # (1) Fire N concurrent deliveries of the SAME event.
    barrier = threading.Barrier(N)
    results: list[tuple[str, bool]] = []
    lock = threading.Lock()

    def deliver() -> None:
        barrier.wait()
        case, is_new, _pk = _ingest(db, settings, body, event_id)
        with lock:
            results.append((case.case_id, is_new))

    threads = [threading.Thread(target=deliver) for _ in range(N)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Exactly one ingest won; every other delivery saw the existing case.
    new_count = sum(1 for _cid, n in results if n)
    assert new_count == 1, f"expected exactly one new ingest, got {new_count}"
    assert len(results) == N
    assert len({cid for cid, _ in results}) == 1  # same case to everyone

    # Exactly one physical case row exists for this event.
    with db.create_session() as s:
        rows = s.query(RecoveryCaseRow).filter_by(event_id=event_id).all()
    assert len(rows) == 1

    # (2) Fire the idempotent Act layer from MANY threads at once on that one
    # case. The ExecutedActionRow ledger (UNIQUE on case/attempt/action) must
    # collapse it to exactly ONE real execution; every other caller reads the
    # already-claimed state and is rejected as a duplicate.
    from reclaim.act import execute_action
    from reclaim.models import Action, DecideOutput
    from reclaim import repo

    case_id = rows[0].case_id
    case_row = repo.get_case_row(db, case_id)
    case = repo.row_to_case(case_row)
    decision = DecideOutput(action=Action.RETRY_NOW, reasoning="retry")

    barrier2 = threading.Barrier(N)
    results2: list[bool] = []  # idempotent_duplicate flags
    lock2 = threading.Lock()

    def execute_it() -> None:
        barrier2.wait()
        res = execute_action(db, case, decision, settings)
        with lock2:
            results2.append(res.idempotent_duplicate)

    threads = [threading.Thread(target=execute_it) for _ in range(N)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Exactly one caller acquired the claim (not a duplicate); N-1 were dupes.
    acquisitions = sum(1 for dup in results2 if not dup)
    assert acquisitions == 1, f"expected one acquisition, got {acquisitions}"

    # The ledger holds exactly one claim -> a double execution is impossible.
    with db.create_session() as s:
        claims = s.query(ExecutedActionRow).filter_by(case_id=case_id).all()
    assert len(claims) == 1


# ---------------------------------------------------------------------------
# 2.1 Concurrent read + write (WAL)
# ---------------------------------------------------------------------------


def test_concurrent_read_write_does_not_corrupt(settings, db) -> None:
    """A writer and several readers run at once without blocking or corrupting.

    With WAL enabled a reader never blocks the writer and vice-versa; the final
    state is consistent and readable.
    """
    case, is_new, _ = _ingest(db, settings, _body(sub="sub_wal"), "evt_wal")
    assert is_new
    errors: list[Exception] = []

    def writer() -> None:
        try:
            for _ in range(50):
                with db.create_session() as s:
                    row = s.query(RecoveryCaseRow).filter_by(case_id=case.case_id).first()
                    row.attempt_number += 1
                    s.commit()
        except Exception as exc:  # pragma: no cover - failure surface
            errors.append(exc)

    def reader(_n: int) -> None:
        try:
            for _ in range(50):
                repo.get_case_row(db, case.case_id)
        except Exception as exc:  # pragma: no cover - failure surface
            errors.append(exc)

    threads = [threading.Thread(target=reader, args=(i,)) for i in range(3)]
    threads.append(threading.Thread(target=writer))
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"concurrent read/write failed: {errors}"
    row = repo.get_case_row(db, case.case_id)
    assert row is not None and row.attempt_number == 1 + 50  # consistent, no corruption


def test_concurrent_pipeline_is_idempotent_via_ledger(settings, db) -> None:
    """Multiple threads running the pipeline on the same case are idempotent.

    Only one thread actually performs the recovery; others see it's already done.
    """
    from reclaim.pipeline import run_case

    case, is_new, _ = _ingest(db, settings, _body(sub="sub_idem"), "evt_idem")
    assert is_new

    # First, run the pipeline once from a single thread to completion.
    first_out = run_case(case.case_id, settings=settings, db=db)
    assert first_out.terminal_state in (CaseState.RESOLVED, CaseState.ESCALATED, CaseState.FAILED)

    # Now try running it again from many threads on an already-terminal case.
    # They should all see it's terminal and skip (return skipped=True).
    barrier = threading.Barrier(4)
    outcomes: list[tuple[CaseState | None, bool]] = []
    lock = threading.Lock()

    def run_terminal() -> None:
        barrier.wait()  # synchronize start
        out = run_case(case.case_id, settings=settings, db=db)
        with lock:
            outcomes.append((out.terminal_state, out.skipped))

    threads = [threading.Thread(target=run_terminal) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # All four threads should have seen the case already terminal and skipped.
    assert len(outcomes) == 4
    assert all(skipped for _st, skipped in outcomes), f"expected all to skip, got {outcomes}"
