"""Broker-mode deferred-retry fire — the money path outside the interactive
state machine must be just as auditable as every other Act.

``retry_payment_task`` runs from the Celery worker (eager in tests), and every
fire moves money, so each invocation must write a ``scheduled_retry`` audit
entry. These tests pin that on the real task body so the guarantee can't drift.
"""

from __future__ import annotations

import json

import pytest

from reclaim import repo
from reclaim.tasks import retry_payment_task
from reclaim.webhook import ingest_event, parse_event


def _payment_body() -> bytes:
    return json.dumps(
        {
            "event": "payment.failed",
            "entity": {
                "id": "pay_fire",
                "subscription_id": "sub_fire",
                "customer_id": "cust_fire",
                "amount": 49900,
                "attempt_number": 2,
                "error_code": "91",
                "error_description": "bank timeout",
                "status": "failed",
                "created_at": 1700000000,
            },
        }
    ).encode("utf-8")


def test_retry_payment_task_audits_the_fire(settings, db, monkeypatch):
    """A deferred retry writes pre-fire + post-execution audit entries with the
    safety booleans pinned False (a scheduler fire is never an LLM failure or a
    rule override), and the stub wallet succeeds."""
    import reclaim.config

    monkeypatch.setattr(reclaim.config, "get_settings", lambda: settings)
    # The `db` fixture registers itself as the process-wide default via
    # reset_db_for_tests, so the task's internal get_db() resolves to it.

    event = parse_event(_payment_body(), event_id_hint="evt_fire")
    case, is_new, _ = ingest_event(db, event, settings)
    assert is_new

    result = retry_payment_task(case.case_id, case.attempt_number)

    assert result["case_id"] == case.case_id
    assert result["terminal_state"] == "RESOLVED"  # stub retry succeeds
    assert result["outcome"] == "retry_succeeded"

    trail = repo.audit_trail(db, case.case_id)
    fires = [e for e in trail if e.stage == "scheduled_retry"]
    assert len(fires) == 2, f"expected 2 scheduled_retry entries, got {len(fires)}"
    assert fires[0].outcome == "SCHEDULED_RETRY_FIRING"
    assert fires[1].action_taken == "retry_now"
    assert all(e.fallback_triggered is False for e in fires)
    assert all(e.rule_override is False for e in fires)


def test_retry_payment_task_idempotent_duplicate_flag(settings, db, monkeypatch):
    """Re-invoking the same fire is a logged no-op with the duplicate flag set —
    never a double charge — and is still written to the trail."""
    import reclaim.config

    monkeypatch.setattr(reclaim.config, "get_settings", lambda: settings)

    event = parse_event(_payment_body(), event_id_hint="evt_fire_2")
    case, is_new, _ = ingest_event(db, event, settings)
    assert is_new

    first = retry_payment_task(case.case_id, case.attempt_number)
    assert first["idempotent_duplicate"] is False

    second = retry_payment_task(case.case_id, case.attempt_number)
    assert second["idempotent_duplicate"] is True

    trail = repo.audit_trail(db, case.case_id)
    fires = [e for e in trail if e.stage == "scheduled_retry"]
    assert len(fires) == 4  # pre+post for each invocation


def test_retry_payment_task_unknown_case(settings, db, monkeypatch):
    """Unknown case is a clean dict error, not a crash."""
    import reclaim.config

    monkeypatch.setattr(reclaim.config, "get_settings", lambda: settings)

    result = retry_payment_task("no_such_case", 1)
    assert result["error"] == "unknown_case"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])