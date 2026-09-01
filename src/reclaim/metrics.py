"""Batch-level metrics computed from the audit trail + case states.

Everything here is derived from what the pipeline actually recorded — no
guesses. The demo reads this to show: total cases, recovery % and ₹ recovered,
root-cause breakdown, and three separate fallback/override/stub counts.

Metrics computed:
- llm_call_failures: LLM call itself failed and fell back to deterministic default
- stopping_rule_overrides: stopping rule (R1-R6) overrode a valid LLM proposal
- stub_mode_actions: actions executed in stub/demo mode (not a fallback at all)
- cases_resolved_without_retry: stopped + escalated (no retry action taken)
"""

from __future__ import annotations

from collections import Counter

from . import repo
from .config import Settings
from .db import Database
from .models import Action, CaseState

# Actions that actually hit an external side-effect path (vs. Action.STOP,
# which resolves with NO side effect). Used to scope the stub-mode counter to
# actions that were truly executed via the stub.
_EXECUTED_ACTIONS = {
    Action.RETRY_NOW.value,
    Action.RETRY_SCHEDULED.value,
    Action.REQUEST_PAYMENT_METHOD_UPDATE.value,
    Action.ESCALATE_HUMAN.value,
}


def compute_metrics(db: Database, settings: Settings) -> dict[str, object]:
    """Compute batch-level metrics from the audit trail + case states.

    Every metric is derived from what the pipeline actually recorded — no guesses.
    Field labels are precise about what each metric proves vs. what requires external
    confirmation (see README "Precision principle" section).

    Returns a dict with:
    - total_cases: total cases processed
    - amount_at_risk: sum of all case amounts
    - recovered_amount: sum of amounts on cases where retry_now succeeded
      (PRECISION: This proves Reclaim made a call; see verification_enabled setting for settlement confirmation)
    - recovery_rate: recovered_amount / amount_at_risk
    - llm_call_failures: cases where Diagnose/Decide LLM call itself failed → deterministic fallback
    - stopping_rule_overrides: cases where R1–R7 overrode an LLM proposal
    - stub_mode_actions: in ACT_MODE=stub, actions executed (environment property, not model property)
    - cases_resolved_without_retry: stopped + escalated (no retry action taken)
    """
    rows = repo.all_case_rows(db)
    total = len(rows)

    state_dist: Counter[str] = Counter()
    cause_breakdown: Counter[str] = Counter()

    # Three separate counters (not conflated):
    # 1. LLM failures: diagnose or decide LLM call failed (fallback_triggered in audit)
    llm_failure_cases: list[str] = []
    # 2. Stopping rule overrides: rule overrode a valid LLM proposal
    rule_override_cases: list[str] = []
    rule_override_by_rule: Counter[str] = Counter()
    # 3. Stub mode: actions executed in stub mode (ACT_MODE=stub)
    stub_mode_cases: list[str] = []

    stopped_cases: list[str] = []
    escalated_cases: list[str] = []
    recovered_cases: list[str] = []
    recovered_amount = 0.0
    amount_at_risk = 0.0

    is_stub_mode = settings.act_mode == "stub"

    for r in rows:
        case = repo.row_to_case(r)
        amount_at_risk += case.amount
        state_dist[case.state.value] += 1

        # Per-case cause + flags from the append-only trail (single scan).
        trail = repo.audit_trail(db, case.case_id)
        cause: str | None = None
        had_llm_failure = False
        had_rule_override = False
        stopped = False
        executed_in_stub = False

        for entry in trail:
            if entry.stage == "diagnose" and entry.decision.startswith("cause="):
                cause = entry.decision.split("cause=")[1].split()[0]

            # LLM failure: fallback_triggered is set when LLM itself fails
            if entry.fallback_triggered:
                # For diagnose/decide stages, this is an LLM failure
                if entry.stage in ("diagnose", "decide"):
                    had_llm_failure = True

            # Rule override: explicitly flagged by boolean
            if entry.stage == "decide" and entry.rule_override:
                had_rule_override = True
                # Extract which rule fired from the outcome string for the breakdown
                if "rule=" in (entry.outcome or ""):
                    rule_part = entry.outcome.split("rule=")[1].split()[0]
                    rule_override_by_rule[rule_part] += 1

            if entry.stage == "act" and entry.action_taken == "stop":
                stopped = True

            # Stub mode: the act stage actually executed a side-effecting
            # action. Action.STOP resolves with NO side effect, so a
            # deliberate-halt case is never counted.
            if (
                is_stub_mode
                and entry.stage == "act"
                and entry.action_taken in _EXECUTED_ACTIONS
            ):
                executed_in_stub = True

        if is_stub_mode and executed_in_stub:
            stub_mode_cases.append(case.case_id)

        if cause:
            cause_breakdown[cause] += 1

        if had_llm_failure:
            llm_failure_cases.append(case.case_id)
        if had_rule_override:
            rule_override_cases.append(case.case_id)

        # Recovered == resolved by an actual successful retry (money in).
        # PRECISION: This proves Reclaim's gateway call succeeded. Settlement confirmation
        # comes from optional verification_enabled setting + razorpay_client.settlement_reconciliation.
        if case.state == CaseState.RESOLVED and _last_act_outcome(
            db, case.case_id, "retry_succeeded"
        ):
            recovered_cases.append(case.case_id)
            recovered_amount += case.amount

        if stopped:
            stopped_cases.append(case.case_id)
        if case.state == CaseState.ESCALATED:
            escalated_cases.append(case.case_id)

    # Recovery rate = recovered ₹ / total at-risk ₹ (reported as a fraction).
    recovery_rate = (recovered_amount / amount_at_risk) if amount_at_risk else 0.0

    # Cases resolved without retry: stopped (decision=stop) OR escalated
    # These are cases where no retry action was taken - the case ended via stop or escalate
    cases_resolved_without_retry = len(stopped_cases) + len(escalated_cases)

    return {
        # Basic counts
        "total_cases": total,
        "state_distribution": dict(state_dist),
        "amount_at_risk": round(amount_at_risk, 2),
        # Recovery metrics (PRECISION: gateway call success, not settlement confirmation)
        "recovered_cases": len(recovered_cases),
        "recovered_amount": round(recovered_amount, 2),
        "recovery_rate": round(recovery_rate, 4),
        "cause_breakdown": dict(cause_breakdown),
        # Three separate counters (deliberately unambiguous and not conflated)
        "llm_call_failures": len(llm_failure_cases),
        "llm_failure_cases": llm_failure_cases,
        "stopping_rule_overrides": len(rule_override_cases),
        "stopping_rule_overrides_by_rule": dict(rule_override_by_rule),
        "rule_override_cases": rule_override_cases,
        "stub_mode_actions": len(stub_mode_cases),
        "stub_mode_cases": stub_mode_cases,
        # Explicitly defined metric
        "cases_resolved_without_retry": cases_resolved_without_retry,
        "stopped_cases": len(stopped_cases),
        "escalated_cases": len(escalated_cases),
    }


def _last_act_outcome(db: Database, case_id: str, needle: str) -> bool:
    for entry in reversed(repo.audit_trail(db, case_id)):
        if entry.stage == "act" and entry.outcome:
            return needle in entry.outcome
    return False
