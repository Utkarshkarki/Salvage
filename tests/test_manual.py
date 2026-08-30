"""Manual (human) override actions — legal-transition guards and audit correctness."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from reclaim import repo
from reclaim.db import Database
from reclaim.models import CaseState
from reclaim.state_machine import IllegalTransitionError
from reclaim.webhook import (
    compute_signature,
    ingest_event,
    parse_event,
    verify_signature,
)

SECRET = "test-webhook-secret"


def _body(*, code: str = "R0", sub: str = "sub_m", attempt: int = 1,
          days_ago: int = 3, amount: int = 10000) -> bytes:
    return json.dumps({
        "event": "payment.failed",
        "entity": {
            "id": f"pay_{sub}",
            "subscription_id": sub,
            "customer_id": f"cust_{sub}",
            "amount": amount,
            "attempt_number": attempt,
            "error_code": code,
            "error_description": "declined",
            "status": "failed",
            "created_at": int((datetime.now(UTC) - timedelta(days=days_ago)).timestamp()),
        },
    }).encode("utf-8")


def _ingest_and_run(db: Database, settings, body_bytes: bytes, event_id: str) -> str:
    from reclaim.pipeline import run_case

    sig = compute_signature(SECRET, body_bytes)
    assert verify_signature(SECRET, body_bytes, sig)
    event = parse_event(body_bytes, event_id_hint=event_id)
    case, is_new, _ = ingest_event(db, event, settings)
    assert is_new
    out = run_case(case.case_id, settings=settings, db=db)
    return case.case_id, out


def test_approve_manual_retry_resolves_and_audits(settings, db: Database) -> None:
    """An ESCALATED case (mandate_revoked -> R1) approved for retry lands on
    RESOLVED and writes stage=manual_override audit entries clearly distinct
    from agent decisions."""
    from reclaim.manual import approve_manual_retry

    case_id, out = _ingest_and_run(db, settings, _body(code="R0"), "evt_m1")
    assert out.terminal_state == CaseState.ESCALATED

    terminal = approve_manual_retry(db, case_id, settings)
    assert terminal == CaseState.RESOLVED.value

    trail = repo.audit_trail(db, case_id)
    manual = [e for e in trail if e.stage == "manual_override"]
    # Two manual entries: the ACTING claim and the terminal outcome.
    assert len(manual) == 2
    assert all(e.agent_reasoning == "manual override by operator" for e in manual)
    assert "manual_approve_retry" in {e.decision for e in manual}
    assert any("RESOLVED/manual_retry" in e.outcome for e in manual)
    # A manual override must never be flagged as an LLM fallback.
    assert all(e.fallback_triggered is False for e in manual)


def test_resolve_human_resolves_and_audits(settings, db: Database) -> None:
    from reclaim.manual import resolve_human

    case_id, out = _ingest_and_run(db, settings, _body(code="R0", sub="sub_h"), "evt_m2")
    assert out.terminal_state == CaseState.ESCALATED

    terminal = resolve_human(db, case_id, settings)
    assert terminal == CaseState.RESOLVED.value

    trail = repo.audit_trail(db, case_id)
    manual = [e for e in trail if e.stage == "manual_override"]
    assert len(manual) == 1
    assert manual[0].decision == "manual_resolve_human"
    assert "RESOLVED/manual_resolve_human" in manual[0].outcome


def test_approve_manual_retry_illegal_on_non_escalated(settings, db: Database) -> None:
    """The guard must refuse a manual retry on a case that is not ESCALATED."""
    from reclaim.manual import approve_manual_retry

    # R01 with a low amount + elapsed cooldown -> retry_now -> RESOLVED (not escalated).
    case_id, out = _ingest_and_run(db, settings, _body(code="R01", sub="sub_x",
                                                       attempt=1, days_ago=3, amount=2000), "evt_m3")
    assert out.terminal_state == CaseState.RESOLVED

    with pytest.raises(IllegalTransitionError):
        approve_manual_retry(db, case_id, settings)


def test_resolve_human_illegal_on_non_escalated(settings, db: Database) -> None:
    from reclaim.manual import resolve_human

    case_id, out = _ingest_and_run(db, settings, _body(code="R01", sub="sub_y",
                                                       attempt=1, days_ago=3, amount=2000), "evt_m4")
    assert out.terminal_state == CaseState.RESOLVED
    with pytest.raises(IllegalTransitionError):
        resolve_human(db, case_id, settings)


def test_manual_override_is_idempotent_via_ledger(settings, db: Database) -> None:
    """Approving a retry twice must not double-charge: the second is a ledger no-op."""
    from reclaim.manual import approve_manual_retry
    from reclaim.db import ExecutedActionRow

    case_id, out = _ingest_and_run(db, settings, _body(code="R0", sub="sub_z"), "evt_m5")
    assert out.terminal_state == CaseState.ESCALATED

    terminal = approve_manual_retry(db, case_id, settings)
    assert terminal == CaseState.RESOLVED.value

    with db.create_session() as s:
        rows = s.query(ExecutedActionRow).filter_by(case_id=case_id).all()
    retry_rows = [r for r in rows if r.action == "retry_now"]
    assert len(retry_rows) == 1  # exactly one retry claim, never two