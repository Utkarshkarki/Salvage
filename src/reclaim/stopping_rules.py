"""Code-enforced stopping rules — the "LLM proposes, code disposes" layer.

The Decide agent proposes exactly ONE bounded action, but autonomous agents
must never be the last word on money movement. This module is a deterministic
post-hoc validation layer that may OVERRIDE the proposed action in hard code,
enumerated by rule, entirely independent of the LLM prompt.

The rules are expressed **declaratively** (policy-as-code): each rule is a
:class:`RuleSpec` with a human-readable description, a priority, a pure
``condition`` predicate, and a forced ``action`` + reason. ``enforce()`` walks
``STOPPING_RULES`` in priority order and applies the first matching rule. This
makes the rules themselves an auditable, introspectable artifact — rendered as
plain language on the ``/rules`` page — not just inline conditionals.

Priority (first matching rule wins):

  R1  mandate_revoked          -> retries are disallowed -> force ESCALATE_HUMAN
  R7  amount below the economic -> retry cost/risk outweighs value -> force STOP
      floor (< min_recovery_amount)
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
from typing import Callable

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
    rule: str  # machine id like "R1"; "" when not overridden


@dataclass(frozen=True)
class RuleContext:
    """Everything a rule's pure predicate/reason needs to decide."""

    input_: DecideInput
    proposed: DecideOutput
    settings: Settings
    payment_method_update_count: int = 0


@dataclass(frozen=True)
class RuleSpec:
    """One declarative stopping rule — self-describing and introspectable.

    ``condition`` is a pure predicate over the context; when it holds, the rule
    fires and forces ``action`` with the human-readable ``reason``.
    """

    rule_id: str  # e.g. "R1"
    priority: int  # lower = higher precedence; the first firing rule wins
    description: str  # plain-language policy statement (threshold placeholders)
    condition: Callable[[RuleContext], bool]
    action: Action  # the action forced when the rule fires
    reason: Callable[[RuleContext], str]


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


# ---------------------------------------------------------------------------
# Declarative rule registry (policy-as-code).
# Order = precedence: the first rule whose condition holds wins.
# ---------------------------------------------------------------------------


def _r1_reason(ctx: RuleContext) -> str:
    return "mandate_revoked: retries disallowed"


def _r7_reason(ctx: RuleContext) -> str:
    return (f"amount {ctx.input_.amount:.2f} below economic floor "
            f"{ctx.settings.min_recovery_amount} (retry cost outweighs value)")


def _r2_reason(ctx: RuleContext) -> str:
    return (f"amount {ctx.input_.amount:.2f} above threshold "
            f"{ctx.settings.escalation_amount_threshold}")


def _r3_reason(ctx: RuleContext) -> str:
    return (f"{ctx.input_.days_since_last_attempt}d since last attempt > "
            f"{ctx.settings.escalation_days_threshold}d")


def _r4_reason(ctx: RuleContext) -> str:
    return (f"attempts exhausted (attempt={ctx.input_.attempt_number}) "
            f"> max {ctx.settings.max_retries_per_cycle}")


def _r5_reason(ctx: RuleContext) -> str:
    return (f"payment-method-update email cap "
            f"({ctx.settings.email_cap_per_7d}/7d) reached")


def _r6_reason(ctx: RuleContext) -> str:
    return (f"cooldown of {ctx.settings.cooldown_hours}h not elapsed "
            f"({ctx.input_.days_since_last_attempt}d since last attempt)")


STOPPING_RULES: tuple[RuleSpec, ...] = (
    RuleSpec(
        rule_id="R1", priority=1,
        description="Cause is {mandate} (mandate revoked): retries are disallowed "
                    "and the case is always escalated to a human.",
        condition=lambda ctx: ctx.input_.cause == Cause.MANDATE_REVOKED,
        action=Action.ESCALATE_HUMAN, reason=_r1_reason,
    ),
    RuleSpec(
        rule_id="R7", priority=2,
        description="Amount below {min_recovery_amount} (the economic floor): the "
                    "retry cost/risk outweighs the value, so no automated action "
                    "is taken — the case is left for a human decision (STOP).",
        condition=lambda ctx: ctx.input_.amount < ctx.settings.min_recovery_amount,
        action=Action.STOP, reason=_r7_reason,
    ),
    RuleSpec(
        rule_id="R2", priority=3,
        description="Amount above {escalation_amount_threshold}: a high-value case "
                    "is escalated for human review rather than auto-retried.",
        condition=lambda ctx: ctx.input_.amount > ctx.settings.escalation_amount_threshold,
        action=Action.ESCALATE_HUMAN, reason=_r2_reason,
    ),
    RuleSpec(
        rule_id="R3", priority=4,
        description="More than {escalation_days_threshold} days since the last "
                    "attempt: an overdue case is escalated for human review.",
        condition=lambda ctx: ctx.input_.days_since_last_attempt > ctx.settings.escalation_days_threshold,
        action=Action.ESCALATE_HUMAN, reason=_r3_reason,
    ),
    RuleSpec(
        rule_id="R4", priority=5,
        description="A retry is proposed but attempts are exhausted "
                    "(> {max_retries_per_cycle}): the case is deliberately STOPPED "
                    "with no side effect.",
        condition=lambda ctx: (
            ctx.proposed.action in RETRY_ACTIONS
            and ctx.input_.attempt_number > ctx.settings.max_retries_per_cycle
        ),
        action=Action.STOP, reason=_r4_reason,
    ),
    RuleSpec(
        rule_id="R5", priority=6,
        description="The payment-method-update email cap ({email_cap_per_7d}/7d) "
                    "is reached: the case is escalated for human review.",
        condition=lambda ctx: (
            ctx.proposed.action == Action.REQUEST_PAYMENT_METHOD_UPDATE
            and ctx.payment_method_update_count >= ctx.settings.email_cap_per_7d
        ),
        action=Action.ESCALATE_HUMAN, reason=_r5_reason,
    ),
    RuleSpec(
        rule_id="R6", priority=7,
        description="A retry_now is proposed but the cooldown "
                    "({cooldown_hours}h) has not elapsed: the retry is clamped to "
                    "a SCHEDULED retry instead.",
        condition=lambda ctx: (
            ctx.proposed.action == Action.RETRY_NOW
            and not cooldown_elapsed(ctx.input_.days_since_last_attempt,
                                     ctx.settings.cooldown_hours)
        ),
        action=Action.RETRY_SCHEDULED, reason=_r6_reason,
    ),
)


def describe_rules(settings: Settings) -> list[dict[str, object]]:
    """Render every active stopping rule in plain language (for the /rules page).

    Each entry carries the rule id, its priority, the current threshold values,
    the forced action, and a plain-English description with live values filled
    in — so the audit/review surface shows WHAT the policy is, not just its
    outputs.
    """
    values = {
        "mandate": Cause.MANDATE_REVOKED.value,
        "min_recovery_amount": f"Rs.{settings.min_recovery_amount:,.2f}",
        "escalation_amount_threshold": f"Rs.{settings.escalation_amount_threshold:,.2f}",
        "escalation_days_threshold": settings.escalation_days_threshold,
        "max_retries_per_cycle": settings.max_retries_per_cycle,
        "email_cap_per_7d": settings.email_cap_per_7d,
        "cooldown_hours": settings.cooldown_hours,
    }
    return [
        {
            "rule_id": r.rule_id,
            "priority": r.priority,
            "action": r.action.value,
            "description": r.description.format(**values),
        }
        for r in STOPPING_RULES
    ]


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
    ctx = RuleContext(
        input_, proposed, settings, payment_method_update_count=payment_method_update_count
    )
    action = proposed.action

    # Walk the declarative registry in priority order; first match wins.
    for rule in STOPPING_RULES:
        if rule.condition(ctx):
            return _rebuild(
                proposed, action=rule.action, reason=rule.reason(ctx), rule=rule.rule_id
            )

    # No rule fired — the proposal stands (but re-validate its scheduled_at).
    if action == Action.RETRY_SCHEDULED and proposed.scheduled_at is not None:
        safe = proposed.model_copy(
            update={"scheduled_at": _schedule_future(proposed.scheduled_at)}
        )
        return RuleOutcome(decision=safe, overridden=False, override_reason="", rule="")
    return RuleOutcome(decision=proposed, overridden=False, override_reason="", rule="")
