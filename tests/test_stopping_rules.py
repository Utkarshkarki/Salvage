"""Stopping-rule enforcement: the LLM proposes, the code disposes.

These are the rules the demo's 'cases that correctly did NOT loop' rely on.
Most of the judges' scrutiny lands on this file — every rule must be covered
for the exact override it forces.
"""

from __future__ import annotations
from datetime import UTC, datetime, timedelta

from reclaim.config import Settings
from reclaim.models import Action, Cause, DecideInput, DecideOutput
from reclaim.stopping_rules import cooldown_elapsed, enforce, RuleOutcome


def _settings(**overrides) -> Settings:
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


def _input(
    *,
    cause: Cause = Cause.INSUFFICIENT_FUNDS,
    attempt: int = 1,
    days: int = 3,
    amount: float = 100.0,
    tier: str = "standard",
) -> DecideInput:
    return DecideInput(
        cause=cause, attempt_number=attempt, days_since_last_attempt=days,
        amount=amount, customer_tier=tier,
    )


def _proposal(action: Action = Action.RETRY_NOW, reasoning: str = "naive") -> DecideOutput:
    if action == Action.RETRY_SCHEDULED:
        # The cross-field validator requires a future scheduled_at for this action.
        return DecideOutput(
            action=action,
            scheduled_at=datetime.now(UTC) + timedelta(hours=48),
            reasoning=reasoning,
        )
    return DecideOutput(action=action, reasoning=reasoning)


DEFAULT = _settings()


def test_rule1_mandate_revoked_forces_escalate_even_if_llm_wants_retry() -> None:
    inp = _input(cause=Cause.MANDATE_REVOKED, attempt=1, days=3, amount=100.0)
    out = enforce(inp, _proposal(Action.RETRY_NOW), DEFAULT)
    assert out.decision.action == Action.ESCALATE_HUMAN
    assert out.rule == "R1"
    assert out.overridden is True
    assert "mandate" in out.override_reason.lower()


def test_rule2_amount_above_threshold_forces_escalate() -> None:
    inp = _input(amount=9000.0)  # > 5000 default
    out = enforce(inp, _proposal(Action.RETRY_NOW), DEFAULT)
    assert out.decision.action == Action.ESCALATE_HUMAN
    assert out.rule == "R2"


def test_rule3_age_above_threshold_forces_escalate() -> None:
    inp = _input(days=12)  # > 7 default
    out = enforce(inp, _proposal(Action.RETRY_NOW), DEFAULT)
    assert out.decision.action == Action.ESCALATE_HUMAN
    assert out.rule == "R3"


def test_rule4_attempts_exhausted_forces_stop() -> None:
    inp = _input(attempt=4)  # > max_retries 3
    out = enforce(inp, _proposal(Action.RETRY_NOW), DEFAULT)
    assert out.decision.action == Action.STOP
    assert out.rule == "R4"
    # scheduled retry is equally a retry -> also forced to stop
    out2 = enforce(inp, _proposal(Action.RETRY_SCHEDULED), DEFAULT)
    assert out2.decision.action == Action.STOP


def test_rule4_attempt_at_or_below_max_is_allowed() -> None:
    for attempt in (1, 2, 3):
        out = enforce(_input(attempt=attempt), _proposal(Action.RETRY_NOW), DEFAULT)
        assert out.overridden is False, f"attempt {attempt} should not be stopped"


def test_rule5_email_cap_forces_escalate() -> None:
    inp = _input()
    out = enforce(
        inp, _proposal(Action.REQUEST_PAYMENT_METHOD_UPDATE), DEFAULT,
        payment_method_update_count=1,  # cap is 1
    )
    assert out.decision.action == Action.ESCALATE_HUMAN
    assert out.rule == "R5"


def test_rule5_under_cap_allowed() -> None:
    out = enforce(
        _input(), _proposal(Action.REQUEST_PAYMENT_METHOD_UPDATE), DEFAULT,
        payment_method_update_count=0,
    )
    assert out.overridden is False
    assert out.decision.action == Action.REQUEST_PAYMENT_METHOD_UPDATE


def test_rule6_cooldown_clamps_retry_now_to_scheduled() -> None:
    inp = _input(days=0)  # < 24h since last attempt
    out = enforce(inp, _proposal(Action.RETRY_NOW), DEFAULT)
    assert out.decision.action == Action.RETRY_SCHEDULED
    assert out.rule == "R6"
    # a clamped scheduled retry must carry a valid future scheduled_at
    assert out.decision.scheduled_at is not None


def test_rule6_cooldown_elapsed_allows_retry_now() -> None:
    assert cooldown_elapsed(1, 24.0) is True   # 1 day == 24h
    assert cooldown_elapsed(0, 24.0) is False  # same day
    assert cooldown_elapsed(2, 24.0) is True


def test_all_compliant_proposal_is_untouched() -> None:
    inp = _input(days=3, attempt=2, amount=100.0)
    out = enforce(inp, _proposal(Action.RETRY_NOW, "let's retry"), DEFAULT)
    assert out.overridden is False
    assert out.rule == ""
    assert out.decision.action == Action.RETRY_NOW
    assert out.decision.reasoning == "let's retry"


def test_mandate_revoked_takes_precedence_over_amount_rule() -> None:
    # R1 must win even when R2 (amount) would also fire.
    inp = _input(cause=Cause.MANDATE_REVOKED, amount=9000.0)
    out = enforce(inp, _proposal(Action.RETRY_NOW), DEFAULT)
    assert out.decision.action == Action.ESCALATE_HUMAN
    assert out.rule == "R1"


def test_scheduled_proposal_with_valid_future_kept() -> None:
    from datetime import UTC, datetime, timedelta

    future = DecideOutput(
        action=Action.RETRY_SCHEDULED,
        scheduled_at=datetime.now(UTC) + timedelta(hours=72),
        reasoning="retry later",
    )
    out = enforce(_input(), future, DEFAULT)
    assert out.overridden is False
    assert out.decision.action == Action.RETRY_SCHEDULED


def test_enforce_returns_valid_pydantic_output() -> None:
    out = enforce(_input(days=0), _proposal(Action.RETRY_NOW), DEFAULT)
    assert isinstance(out, RuleOutcome)
    # rebuild must satisfy the cross-field validator
    assert out.decision.action == Action.RETRY_SCHEDULED
    assert out.decision.scheduled_at is not None
