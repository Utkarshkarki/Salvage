"""Code-enforced stopping rules — the "LLM proposes, code disposes" layer.

The Decide agent proposes exactly ONE bounded action, but autonomous agents
must never be the last word on money movement. This module is a deterministic
post-hoc validation layer that may OVERRIDE the proposed action in hard code,
enumerated by rule, entirely independent of the LLM prompt.

Priority (first matching rule wins):

  R1  mandate_revoked          -> retries are disallowed -> force ESCALATE_HUMAN
  R2  amount > threshold       -> force ESCALATE_HUMAN
  R3  days since last attempt
      > threshold              -> force ESCALATE_HUMAN
  R4  retry proposed but
      attempts exhausted       -> force STOP
  R5  payment-method-update
      email cap reached        -> force ESCALATE_HUMAN
  R6  retry_now proposed but
      cooldown not elapsed     -> force RETRY_SCHEDULED (never retry_now)

Every override sets ``overridden=True`` with a human-readable reason and a
machine-readable ``rule`` id, so the audit trail (and the demo) can show a
naive/hallucinated proposal being clamped by a business rule.

This module is deliberately pure: no I/O, no DB. Callers pass in the context
they already have (e.g. ``payment_method_update_count``), keeping every rule
independently unit-testable.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from .config import Settings
from .models import Action, Cause, DecideInput, DecideOutput

# Action forms that count as a "retry" for the attempt-exhaustion rule.
RETRY_ACTIONS = frozenset({Action.RETRY_NOW, Action.RETRY_SCHEDULED})


@dataclass(frozen=True)
class RuleOutcome:
    """Result of enforcing stopping rules over an LLM proposal."""

    decision: DecideOutput
    overridden: bool  # did a rule change/force the action?
    override_reason: str  # human-readable; "" when not overridden
    rule: str  # machine id like "R4"; "" when not overridden


def cooldown_elapsed(days_since_last_attempt: int, cooldown_hours: float) -> bool:
    """True when at least ``cooldown_hours`` have passed since the last attempt.

    ``days_since_last_attempt`` is whole-day granular, so we compare on an
    hour basis — a 24h cooldown is satisfied once a full day has elapsed.
    """
    return days_since_last_attempt * 24.0 >= cooldown_hours


def _rebuild(proposed: DecideOutput, *, action: Action, reason: str, rule: str) -> RuleOutcome:
    """Rebuild a ``DecideOutput`` carrying a clamps' reasoning, then model-
    validate it so schema guarantees (scheduled_at rules) still hold."""
    reasoning = f"[{rule} {reason}] overrode LLM proposal '{proposed.action.value}': {proposed.reasoning}"
    kwargs: dict[str, object] = {"action": action, "reasoning": reasoning}
    if action == Action.RETRY_SCHEDULED:
        kwargs["scheduled_at"] = datetime.now(UTC) + timedelta(hours=rule_default_schedule_hours())
    decision = DecideOutput.model_validate(kwargs)
    return RuleOutcome(decision=decision, overridden=True, override_reason=reason, rule=rule)


def _schedule_future(at: datetime) -> datetime:
    if at.tzinfo is None:
        at = at.replace(tzinfo=UTC)
    if at <= datetime.now(UTC):
        at = datetime.now(UTC) + timedelta(hours=rule_default_schedule_hours())
    return at


def rule_default_schedule_hours() -> float:
    """Default lead time for a forced/re-proposed scheduled retry (24h)."""
    return 24.0


def enforce(
    input_: DecideInput,
    proposed: DecideOutput,
    settings: Settings,
    *,
    payment_method_update_count: int = 0,
) -> RuleOutcome:
    """Clamp ``proposed`` against the code-enforced stopping rules.

    ``payment_method_update_count`` is the number of payment-method-update
    emails already sent to this customer within the current window (the caller
    derives it from executed actions); 0 means none, so the rule is inert
    unless a caller actually counts a hit.
    """
    action = proposed.action

    # R1 — mandate revoked: retries are disallowed, period.
    if input_.cause == Cause.MANDATE_REVOKED:
        return _rebuild(proposed, action=Action.ESCALATE_HUMAN,
                        reason="mandate_revoked: retries disallowed", rule="R1")

    # R2 — amount above the escalation threshold.
    if input_.amount > settings.escalation_amount_threshold:
        return _rebuild(proposed, action=Action.ESCALATE_HUMAN,
                        reason=f"amount {input_.amount:.2f} above threshold "
                               f"{settings.escalation_amount_threshold}", rule="R2")

    # R3 — case older than the escalation window.
    if input_.days_since_last_attempt > settings.escalation_days_threshold:
        return _rebuild(proposed, action=Action.ESCALATE_HUMAN,
                        reason=f"{input_.days_since_last_attempt}d since last attempt > "
                               f"{settings.escalation_days_threshold}d", rule="R3")

    # R4 — retry attempts exhausted.
    if action in RETRY_ACTIONS and input_.attempt_number > settings.max_retries_per_cycle:
        return _rebuild(proposed, action=Action.STOP,
                        reason=f"attempts exhausted (attempt={input_.attempt_number}) "
                               f"> max {settings.max_retries_per_cycle}", rule="R4")

    # R5 — payment-method-update email cap reached.
    if (
        action == Action.REQUEST_PAYMENT_METHOD_UPDATE
        and payment_method_update_count >= settings.email_cap_per_7d
    ):
        return _rebuild(proposed, action=Action.ESCALATE_HUMAN,
                        reason=f"payment-method-update email cap "
                               f"({settings.email_cap_per_7d}/7d) reached", rule="R5")

    # R6 — retry_now while still inside the cooldown window -> schedule instead.
    if (
        action == Action.RETRY_NOW
        and not cooldown_elapsed(input_.days_since_last_attempt, settings.cooldown_hours)
    ):
        return _rebuild(proposed, action=Action.RETRY_SCHEDULED,
                        reason=f"cooldown of {settings.cooldown_hours}h not elapsed "
                               f"({input_.days_since_last_attempt}d since last attempt)",
                        rule="R6")

    # No rule fired — the proposal stands (but re-validate its scheduled_at).
    if action == Action.RETRY_SCHEDULED and proposed.scheduled_at is not None:
        safe = proposed.model_copy(
            update={"scheduled_at": _schedule_future(proposed.scheduled_at)}
        )
        return RuleOutcome(decision=safe, overridden=False, override_reason="", rule="")
    return RuleOutcome(decision=proposed, overridden=False, override_reason="", rule="")
