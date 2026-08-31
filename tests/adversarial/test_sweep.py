"""Adversarial crash recovery: stale ACTING locks and network-drop idempotency.

These tests prove:

1. ``test_sweep_finds_stale_acting_cases`` — the sweep correctly identifies
   cases stuck in ACTING past the timeout and reconciles them to ESCALATED,
   without touching legitimately in-progress ones.
2. ``test_sweep_ignores_recently_acting_cases`` — a case that entered ACTING
   very recently (but hasn't finished) is NOT swept, even though it is in
   ACTING state.
3. ``test_network_drop_idempotency_intercepted`` — a retry call with the SAME
   idempotency key that MAY have already succeeded server-side is correctly
   intercepted on the client side and does NOT double-execute (the ledger
   enforces this).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from reclaim import repo
from reclaim.config import Settings
from reclaim.db import Database, ExecutedActionRow
from reclaim.models import CaseState
from reclaim.sweep import find_stale_acting, reconcile_stale_acting


def _settings(**overrides):
    base = dict(
        _env_file=None,
        razorpay_webhook_secret="test-secret",
        llm_mode="offline",
        act_mode="stub",
        reclaim_celery_eager=True,
        database_url="sqlite:///:memory:",
    )
    base.update(overrides)
    return Settings(**base)


# ---------------------------------------------------------------------------
# 1.2 Mid-pipeline crash recovery (stale-lock sweep)
# ---------------------------------------------------------------------------


def test_sweep_finds_stale_acting_cases(settings, db: Database) -> None:
    """The sweep finds a case in ACTING past the timeout and escalates it."""
    from reclaim.webhook import compute_signature, ingest_event, parse_event

    body = json.dumps({
        "event": "payment.failed",
        "entity": {
            "id": "pay_stale",
            "subscription_id": "sub_stale",
            "customer_id": "cust_stale",
            "amount": 10000,
            "attempt_number": 1,
            "error_code": "R01",
            "error_description": "declined",
            "status": "failed",
            "created_at": int((datetime.now(UTC) - timedelta(days=1)).timestamp()),
        },
    }).encode("utf-8")
    sig = compute_signature(settings.razorpay_webhook_secret, body)
    event = parse_event(body, event_id_hint="evt_stale")
    case, is_new, _ = ingest_event(db, event, settings)
    assert is_new

    # Advance it to ACTING (the state machine enforces INGESTED -> DIAGNOSED
    # -> DECIDED -> ACTING, so we cheat via a direct set for the test).
    repo.set_case_state(db, case.case_id, CaseState.ACTING)
    from reclaim.audit import write_audit
    from reclaim.models import AuditLogEntry

    write_audit(
        db,
        AuditLogEntry(
            case_id=case.case_id,
            stage="act",
            agent_reasoning="simulated ACTING entry",
            input_state={},
            decision="retry_now",
            action_taken="retry_now",
            outcome="ACTING",
            timestamp=datetime.now(UTC) - timedelta(seconds=400),  # older than 300s
            fallback_triggered=False,
        ),
    )

    # The sweep should find it.
    stale = find_stale_acting(db, settings)
    assert len(stale) == 1
    assert stale[0].case_id == case.case_id

    # Reconcile it.
    swept = reconcile_stale_acting(db, settings)
    assert swept == [case.case_id]

    # Now it's ESCALATED, not ACTING.
    row = repo.get_case_row(db, case.case_id)
    assert row is not None and row.state == CaseState.ESCALATED.value

    # And it has a sweep audit entry.
    trail = repo.audit_trail(db, case.case_id)
    sweep_entries = [e for e in trail if e.stage == "sweep"]
    assert len(sweep_entries) == 1
    assert "sweep_stale_lock" in sweep_entries[0].outcome


def test_sweep_ignores_recently_acting_cases(settings, db: Database) -> None:
    """A case in ACTING that started very recently is NOT swept."""
    from reclaim.webhook import compute_signature, ingest_event, parse_event

    body = json.dumps({
        "event": "payment.failed",
        "entity": {
            "id": "pay_fresh",
            "subscription_id": "sub_fresh",
            "customer_id": "cust_fresh",
            "amount": 10000,
            "attempt_number": 1,
            "error_code": "R01",
            "error_description": "declined",
            "status": "failed",
            "created_at": int((datetime.now(UTC) - timedelta(hours=1)).timestamp()),
        },
    }).encode("utf-8")
    sig = compute_signature(settings.razorpay_webhook_secret, body)
    event = parse_event(body, event_id_hint="evt_fresh")
    case, is_new, _ = ingest_event(db, event, settings)
    assert is_new

    repo.set_case_state(db, case.case_id, CaseState.ACTING)
    from reclaim.audit import write_audit
    from reclaim.models import AuditLogEntry

    # ACTING entry is fresh (within the 300s timeout).
    write_audit(
        db,
        AuditLogEntry(
            case_id=case.case_id,
            stage="act",
            agent_reasoning="fresh ACTING",
            input_state={},
            decision="retry_now",
            action_taken="retry_now",
            outcome="ACTING",
            timestamp=datetime.now(UTC),  # current time
            fallback_triggered=False,
        ),
    )

    # No stale cases.
    stale = find_stale_acting(db, settings)
    assert len(stale) == 0

    # Reconciliation is a no-op.
    swept = reconcile_stale_acting(db, settings)
    assert swept == []

    # Still ACTING, not swept.
    row = repo.get_case_row(db, case.case_id)
    assert row is not None and row.state == CaseState.ACTING.value


# ---------------------------------------------------------------------------
# 1.3 Network-drop-before-dispatch (duplicate executor idempotency)
# ---------------------------------------------------------------------------


def test_network_drop_idempotency_intercepted(settings, db: Database) -> None:
    """A retry with the SAME idempotency key is intercepted, never double-executes.

    This simulates: the API call went to the server, the server processed it,
    but the response never reached us. A subsequent call with the same
    idempotency key is correctly intercepted (ledger exists) and does NOT
    create a second action claim.
    """
    from reclaim import repo
    from reclaim.razorpay_client import idempotency_key
    from reclaim.webhook import compute_signature, ingest_event, parse_event

    body = json.dumps({
        "event": "payment.failed",
        "entity": {
            "id": "pay_drop",
            "subscription_id": "sub_drop",
            "customer_id": "cust_drop",
            "amount": 10000,
            "attempt_number": 1,
            "error_code": "R01",
            "error_description": "declined",
            "status": "failed",
            "created_at": int((datetime.now(UTC) - timedelta(days=1)).timestamp()),
        },
    }).encode("utf-8")
    sig = compute_signature(settings.razorpay_webhook_secret, body)
    event = parse_event(body, event_id_hint="evt_drop")
    case, is_new, _ = ingest_event(db, event, settings)
    assert is_new

    # The idempotency key is deterministic for (case, attempt, action).
    key = idempotency_key(case.case_id, case.attempt_number, "retry_now")

    # Simulate: the retry was ALREADY claimed in a previous run (possibly the
    # network dropped before we saw the response). This is the ledger entry
    # that makes the second call a no-op.
    with db.create_session() as s:
        s.add(
            ExecutedActionRow(
                case_id=case.case_id,
                attempt_number=case.attempt_number,
                action="retry_now",
                idempotency_key=key,
                executed_at=datetime.now(UTC) - timedelta(seconds=10),
            )
        )
        s.commit()

    # Now if the pipeline runs again on this case, the Act layer should see
    # an existing ledger entry and skip the side effect (return duplicate).
    from reclaim.act import execute_action
    from reclaim.models import DecideOutput, Action

    result = execute_action(
        db, case,
        DecideOutput(action=Action.RETRY_NOW, reasoning="retry simulation"),
        settings,
    )

    # It MUST be flagged as a duplicate — the ledger prevented double execution.
    assert result.idempotent_duplicate is True
    assert result.terminal_state == CaseState.RESOLVED  # duplicate -> resolved
    assert result.amount_recovered == 0.0  # no new recovery (the original already succeeded)


# ---------------------------------------------------------------------------
# 2.3 The stale-lock sweep is wired as a Celery periodic task
# ---------------------------------------------------------------------------


def test_sweep_is_scheduled_periodically(settings, monkeypatch) -> None:
    """The reconciliation sweep runs on a Celery beat schedule every 5 minutes.

    Verifies the periodic task is registered and its schedule is explicit, so a
    process dying mid-ACTING is always swept, not left wedged forever.
    """
    from celery.schedules import crontab

    # Ensure the module-level app can build hermetically (env var overrides .env).
    monkeypatch.setenv("RAZORPAY_WEBHOOK_SECRET", "test-secret")

    from reclaim.celery_app import build_app

    app = build_app(settings)
    beat = app.conf.beat_schedule

    assert "reclaim-sweep-stale-acting" in beat
    entry = beat["reclaim-sweep-stale-acting"]
    assert entry["task"] == "reclaim.tasks.sweep_stale_acting_task"
    assert isinstance(entry["schedule"], crontab)
    # crontab("*/5") expands to minutes divisible by 5.
    minutes = entry["schedule"].minute
    assert {0, 5, 55} <= minutes and 1 not in minutes and 57 not in minutes

    # And that task entrypoint resolves to the sweep reconciler.
    from reclaim.tasks import sweep_stale_acting_task
    assert sweep_stale_acting_task.name == "reclaim.tasks.sweep_stale_acting_task"