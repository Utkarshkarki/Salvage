"""Pydantic schema validation at the boundaries (ZERO-HALO checks)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from reclaim.models import (
    Action,
    CaseState,
    Cause,
    DecideOutput,
    DiagnoseOutput,
    PaymentRecord,
    RecoveryCase,
    WebhookEvent,
)


def _case(**overrides) -> RecoveryCase:
    base = dict(
        case_id="sub_A1",
        event_id="evt_1",
        customer_id="cust_1",
        subscription_id="sub_A1",
        failure_reason="R01",
        amount=1000.0,
        attempt_number=1,
    )
    base.update(overrides)
    return RecoveryCase(**base)


def test_negative_amount_rejected() -> None:
    with pytest.raises(ValidationError):
        _case(amount=-5.0)


def test_zero_attempt_number_rejected() -> None:
    with pytest.raises(ValidationError):
        _case(attempt_number=0)


def test_diagnose_output_confidence_bounds() -> None:
    with pytest.raises(ValidationError):
        DiagnoseOutput(cause=Cause.BANK_TIMEOUT, confidence=1.5, reasoning="x")
    with pytest.raises(ValidationError):
        DiagnoseOutput(cause=Cause.BANK_TIMEOUT, confidence=-0.1, reasoning="x")


def test_scheduled_retry_requires_datetime() -> None:
    with pytest.raises(ValidationError, match="scheduled_at"):
        DecideOutput(action=Action.RETRY_SCHEDULED, reasoning="r")


def test_scheduled_at_rejected_for_other_actions() -> None:
    with pytest.raises(ValidationError, match="only allowed"):
        DecideOutput(
            action=Action.RETRY_NOW,
            scheduled_at=datetime.now(UTC) + timedelta(hours=1),
            reasoning="r",
        )


def test_scheduled_at_must_be_future() -> None:
    with pytest.raises(ValidationError, match="future"):
        DecideOutput(
            action=Action.RETRY_SCHEDULED,
            scheduled_at=datetime.now(UTC) - timedelta(minutes=5),
            reasoning="r",
        )


def test_days_since_last_attempt_uses_history() -> None:
    past = datetime.now(UTC) - timedelta(days=3)
    case = RecoveryCase(
        case_id="sub_1",
        event_id="evt_1",
        customer_id="c1",
        subscription_id="sub_1",
        failure_reason="R01",
        amount=100.0,
        attempt_number=1,
        payment_history=[PaymentRecord(status="failed", amount=100.0, attempted_at=past)],
    )
    assert case.days_since_last_attempt() == 3


def test_webhook_amount_paise_to_inr() -> None:
    event = WebhookEvent(
        event_id="evt_x",
        type="payment.failed",
        payload={"entity": {"id": "pay_1", "customer_id": "c1", "amount": 125000}},
    )
    assert event.amount() == 1250.0
    assert event.case_id() == "pay_1"
    assert event.failure_reason() == "unknown"


def test_default_state_is_ingested() -> None:
    c = _case()
    assert c.state == CaseState.INGESTED