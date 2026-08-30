"""End-to-end pipeline: fallback paths, idempotency, and state transitions.

The three things judges and technical reviewers scrutinize most:
  1. LLM-failure fallbacks are deterministic and never crash a case.
  2. Retries are idempotent — a duplicate call can never double-charge.
  3. State transitions are the only progress signal and are persisted+audited.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from reclaim import repo
from reclaim.db import Database, ExecutedActionRow, RecoveryCaseRow
from reclaim.models import Action, CaseState
from reclaim.webhook import (
    compute_signature,
    ingest_event,
    parse_event,
    verify_signature,
)

SECRET = "test-webhook-secret"


def _webhook_body(
    *,
    amount: int = 10000,  # paise -> Rs.100.00
    error_code: str = "R01",
    attempt: int = 1,
    days_ago: int = 3,
    event: str = "payment.failed",
    sub: str = "sub_t1",
    customer: str = "cust_t1",
) -> bytes:
    data = {
        "event": event,
        "entity": {
            "id": f"pay_{sub}",
            "subscription_id": sub,
            "customer_id": customer,
            "amount": amount,
            "attempt_number": attempt,
            "error_code": error_code,
            "error_description": "declined",
            "status": "failed",
            "created_at": int((datetime.now(UTC) - timedelta(days=days_ago)).timestamp()),
        },
    }
    return json.dumps(data).encode("utf-8")


def _ingest_unique(db: Database, body: bytes, event_id: str):
    sig = compute_signature(SECRET, body)
    assert verify_signature(SECRET, body, sig)
    event = parse_event(body, event_id_hint=event_id)
    return ingest_event(db, event, _settings())


def _settings(**overrides):
    from reclaim.config import Settings

    base = dict(
        _env_file=None,
        razorpay_webhook_secret=SECRET,
        llm_mode="offline",
        act_mode="stub",
        reclaim_celery_eager=True,
        database_url="sqlite:///:memory:",
    )
    base.update(overrides)
    return Settings(**base)


# ---------------------------------------------------------------------------
# Full flow
# ---------------------------------------------------------------------------


def test_healthy_case_resolves_recovered(settings, db: Database) -> None:
    """A fresh, low-risk cause with an elapsed cooldown -> retry now -> recovered."""
    case, is_new, _ = _ingest_unique(
        db, _webhook_body(error_code="R01", days_ago=3, attempt=1), "evt_flow1"
    )
    assert is_new

    from reclaim.pipeline import run_case

    out = run_case(case.case_id, settings=settings, db=db)
    assert out.terminal_state == CaseState.RESOLVED
    assert out.action_taken == "retry_now"
    assert out.amount_recovered == case.amount

    # Audit trail captures the full path, and the recovery is recorded.
    trail = repo.audit_trail(db, case.case_id)
    stages = [e.stage for e in trail]
    assert "diagnose" in stages and "decide" in stages and stages.count("act") >= 1
    assert any(e.outcome.startswith("RESOLVED/retry_succeeded") for e in trail)
    # The returned case state matches the audit + ledger.
    with db.create_session() as s:
        row = s.query(RecoveryCaseRow).filter_by(case_id=case.case_id).first()
    assert row is not None and row.state == CaseState.RESOLVED.value


def test_mandate_revoked_escalates_and_is_audited(settings, db: Database) -> None:
    """R1 override -> escalate_human, and the audit marks the stopping-rule override."""
    case, _, _ = _ingest_unique(db, _webhook_body(error_code="R0"), "evt_flow2")

    from reclaim.pipeline import run_case

    out = run_case(case.case_id, settings=settings, db=db)
    assert out.terminal_state == CaseState.ESCALATED
    assert out.action_taken == "escalate_human"
    assert out.stopping_rule_override is True

    trail = repo.audit_trail(db, case.case_id)
    # Audit should show the decide stage with override (fallback_triggered only for LLM failures)
    assert any(e.stage == "decide" and "OVERRIDE" in e.outcome for e in trail)
    assert any("ESCALATED" in e.outcome for e in trail)


def test_exhausted_attempts_stop_and_do_not_retry(settings, db: Database) -> None:
    """attempt=4 > max 3 -> rule R4 forces stop; state RESOLVED via deliberate halt."""
    case, _, _ = _ingest_unique(
        db, _webhook_body(error_code="05", attempt=4, days_ago=3), "evt_flow3"
    )

    from reclaim.pipeline import run_case

    out = run_case(case.case_id, settings=settings, db=db)
    assert out.action == "stop"
    assert out.action_taken == "stop"
    assert out.terminal_state == CaseState.RESOLVED
    assert out.amount_recovered == 0.0


# ---------------------------------------------------------------------------
# LLM-failure fallbacks
# ---------------------------------------------------------------------------


def test_diagnose_llm_failure_falls_back_to_unknown(settings, db: Database, monkeypatch) -> None:
    from reclaim import llm_client

    def boom(self, input_):
        raise RuntimeError("ollama unreachable")

    monkeypatch.setattr(llm_client.LLMClient, "diagnose", boom)

    case, _, _ = _ingest_unique(db, _webhook_body(error_code="R01"), "evt_fb1")
    from reclaim.pipeline import run_case

    out = run_case(case.case_id, settings=settings, db=db)
    assert out.llm_failure is True
    assert out.cause == "unknown"
    # The case must NOT crash — it proceeds and lands somewhere terminal.
    assert out.terminal_state is not None


def test_decide_llm_failure_falls_back_to_escalate(settings, db: Database, monkeypatch) -> None:
    from reclaim import llm_client

    def boom(self, input_):
        raise RuntimeError("ollama unreachable")

    monkeypatch.setattr(llm_client.LLMClient, "decide", boom)

    case, _, _ = _ingest_unique(db, _webhook_body(error_code="R01"), "evt_fb2")
    from reclaim.pipeline import run_case

    out = run_case(case.case_id, settings=settings, db=db)
    assert out.llm_failure is True
    # LLM failure fallback = escalate_human (never silently retry on failure).
    assert out.action == Action.ESCALATE_HUMAN.value
    assert out.terminal_state == CaseState.ESCALATED

    trail = repo.audit_trail(db, case.case_id)
    # The escalate was a deterministic fallback from LLM failure, so fallback_triggered is set
    assert any(e.stage == "decide" and "escalate_human" in e.decision and e.fallback_triggered
               for e in trail)


def test_decision_fallback_is_idempotent_across_rerun(settings, db: Database, monkeypatch) -> None:
    from reclaim import llm_client

    monkeypatch.setattr(llm_client.LLMClient, "decide", lambda self, i: (_ for _ in ()).throw(RuntimeError("x")))

    case, _, _ = _ingest_unique(db, _webhook_body(error_code="R01"), "evt_fb3")
    from reclaim.pipeline import run_case

    first = run_case(case.case_id, settings=settings, db=db)
    second = run_case(case.case_id, settings=settings, db=db)
    assert second.skipped is True  # terminal cases are no-op on rerun
    assert first.terminal_state == CaseState.ESCALATED


# ---------------------------------------------------------------------------
# Idempotency on retry
# ---------------------------------------------------------------------------


def _deal_case(db: Database, case_id: str, decision):
    from reclaim.db import RecoveryCaseRow

    with db.create_session() as s:
        row = s.query(RecoveryCaseRow).filter_by(case_id=case_id).first()
        return repo.row_to_case(row)


def test_retry_is_idempotent_via_ledger(settings, db: Database) -> None:
    """Two identical Act calls -> exactly one side effect + one ledger row."""
    from reclaim.act import execute_action
    from reclaim.models import DecideOutput

    case, _, _ = _ingest_unique(db, _webhook_body(error_code="R01", days_ago=3), "evt_idem")
    decision = DecideOutput(action=Action.RETRY_NOW, reasoning="retry")

    r1 = execute_action(db, case, decision, settings)
    assert r1.idempotent_duplicate is False
    assert r1.amount_recovered == case.amount

    case2 = _deal_case(db, case.case_id, decision)
    r2 = execute_action(db, case2, decision, settings)
    assert r2.idempotent_duplicate is True  # replayed -> skipped, no double charge
    assert r2.amount_recovered == 0.0

    with db.create_session() as s:
        rows = s.query(ExecutedActionRow).all()
    assert len(rows) == 1  # the UNIQUE ledger guard


def test_dependent_cases_have_distinct_retry_claims(settings, db: Database) -> None:
    from reclaim.act import execute_action
    from reclaim.models import DecideOutput

    c1, _, _ = _ingest_unique(db, _webhook_body(sub="sub_a", days_ago=3), "evt_d1")
    c2, _, _ = _ingest_unique(db, _webhook_body(sub="sub_b", days_ago=3), "evt_d2")
    decision = DecideOutput(action=Action.RETRY_NOW, reasoning="retry")

    execute_action(db, c1, decision, settings)
    r2 = execute_action(db, c2, decision, settings)
    assert r2.idempotent_duplicate is False  # different case -> fresh claim

    with db.create_session() as s:
        assert s.query(ExecutedActionRow).count() == 2


# ---------------------------------------------------------------------------
# Concurrency throttle
# ---------------------------------------------------------------------------


def test_concurrency_limiter_bounds_peak(settings) -> None:
    import threading
    import time

    from reclaim.pipeline import ConcurrencyLimiter

    limiter = ConcurrencyLimiter(max_workers=3)

    def work(_i: int) -> None:
        limiter.run(time.sleep, 0.1)

    threads = [threading.Thread(target=work, args=(i,)) for i in range(9)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert limiter.peak >= 2  # it actually ran concurrently
    assert limiter.peak <= 3  # but never beyond the cap
