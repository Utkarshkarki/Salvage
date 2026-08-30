"""Metrics semantics: the three counters are separate and mean what they say.

This test pins the definitions a stakeholder would ask about, so the report
can never drift back into conflation without a failing test:

- ``llm_call_failures``      -> the LLM call itself failed (timeout, invalid
                                JSON, ...) and fell back to a deterministic
                                default. NOT a stopping-rule override.
- ``stopping_rule_overrides``-> a valid LLM proposal was accepted, then a rule
                                R1-R6 overrode it in code. NOT an LLM failure.
- ``stub_mode_actions``      -> cases that actually executed a side-effecting
                                action through the stub (ACT_MODE=stub). A
                                deliberate-halt (STOP) case executes nothing,
                                so it is NOT counted.
- ``cases_resolved_without_retry`` -> stopped + escalated. Explicitly defined.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from reclaim.db import Database
from reclaim.models import CaseState
from reclaim.webhook import (
    compute_signature,
    ingest_event,
    parse_event,
    verify_signature,
)

SECRET = "test-webhook-secret"


def _webhook_body(
    *,
    error_code: str = "R01",
    attempt: int = 1,
    days_ago: int = 3,
    amount: int = 10000,
    sub: str = "sub_m",
) -> bytes:
    data = {
        "event": "payment.failed",
        "entity": {
            "id": f"pay_{sub}",
            "subscription_id": sub,
            "customer_id": f"cust_{sub}",
            "amount": amount,
            "attempt_number": attempt,
            "error_code": error_code,
            "error_description": "declined",
            "status": "failed",
            "created_at": int((datetime.now(UTC) - timedelta(days=days_ago)).timestamp()),
        },
    }
    return json.dumps(data).encode("utf-8")


def _ingest(db: Database, settings, body: bytes, event_id: str):
    sig = compute_signature(SECRET, body)
    assert verify_signature(SECRET, body, sig)
    event = parse_event(body, event_id_hint=event_id)
    return ingest_event(db, event, settings)


# ---------------------------------------------------------------------------
# The three counters are independent
# ---------------------------------------------------------------------------


def test_rule_override_is_not_an_llm_failure(settings, db: Database) -> None:
    """A mandate-revoked R1 override must land ONLY in stopping_rule_overrides,
    never in llm_call_failures (the LLM did not fail — it was overridden)."""
    from reclaim.pipeline import run_case
    from reclaim.metrics import compute_metrics

    case, is_new, _ = _ingest(db, settings, _webhook_body(error_code="R0"), "evt_m1")
    assert is_new
    out = run_case(case.case_id, settings=settings, db=db)
    assert out.stopping_rule_override is True
    assert out.llm_failure is False

    m = compute_metrics(db, settings)
    assert m["stopping_rule_overrides"] == 1
    assert m["stopping_rule_overrides_by_rule"] == {"R1": 1}
    assert m["llm_call_failures"] == 0


def test_llm_failure_is_not_a_rule_override(
    settings, db: Database, monkeypatch
) -> None:
    """A genuine LLM decide failure must land ONLY in llm_call_failures. The
    deterministic fallback (escalate_human) is NOT a stopping-rule override."""
    from reclaim import llm_client
    from reclaim.metrics import compute_metrics
    from reclaim.pipeline import run_case

    monkeypatch.setattr(
        llm_client.LLMClient,
        "decide",
        lambda self, i: (_ for _ in ()).throw(RuntimeError("ollama unreachable")),
    )

    case, is_new, _ = _ingest(db, settings, _webhook_body(error_code="R01"), "evt_m2")
    assert is_new
    out = run_case(case.case_id, settings=settings, db=db)
    assert out.llm_failure is True
    assert out.stopping_rule_override is False

    m = compute_metrics(db, settings)
    assert m["llm_call_failures"] == 1
    assert m["stopping_rule_overrides"] == 0


def test_stop_case_executes_no_stub_action(settings, db: Database) -> None:
    """An attempt-exhausted STOP resolves with NO side effect: it counts in
    cases_resolved_without_retry but NOT in stub_mode_actions."""
    from reclaim.metrics import compute_metrics
    from reclaim.pipeline import run_case

    case, is_new, _ = _ingest(
        db, settings, _webhook_body(error_code="05", attempt=4, days_ago=3), "evt_m3"
    )
    assert is_new
    out = run_case(case.case_id, settings=settings, db=db)
    assert out.action == "stop"
    assert out.terminal_state == CaseState.RESOLVED

    m = compute_metrics(db, settings)
    assert m["stopped_cases"] == 1
    assert m["cases_resolved_without_retry"] == 1
    assert m["stub_mode_actions"] == 0  # no side effect executed


def test_recovered_retry_counts_as_stub_action(settings, db: Database) -> None:
    """A healthy retry_now->recovered executes exactly one stub action and is
    counted neither as a failure nor a rule override."""
    from reclaim.metrics import compute_metrics
    from reclaim.pipeline import run_case

    case, is_new, _ = _ingest(
        db, settings, _webhook_body(error_code="R01", days_ago=3, amount=5000), "evt_m4"
    )
    assert is_new
    out = run_case(case.case_id, settings=settings, db=db)
    assert out.terminal_state == CaseState.RESOLVED
    assert out.action_taken == "retry_now"

    m = compute_metrics(db, settings)
    assert m["stub_mode_actions"] == 1
    assert m["llm_call_failures"] == 0
    assert m["stopping_rule_overrides"] == 0
    assert m["recovered_cases"] == 1


def test_escalated_because_r2_amount_is_override_and_stub_action(
    settings, db: Database
) -> None:
    """An R2 (high amount) escalation is BOTH an override AND a stub-executed
    action (the escalate email stub fires) — the two counters are independent,
    not mutually exclusive."""
    from reclaim.metrics import compute_metrics
    from reclaim.pipeline import run_case

    # 9999 rupees (999900 paise) is above the R2 threshold (5000).
    case, is_new, _ = _ingest(
        db, settings, _webhook_body(error_code="R01", days_ago=3, amount=999900), "evt_m5"
    )
    assert is_new
    out = run_case(case.case_id, settings=settings, db=db)
    assert out.terminal_state == CaseState.ESCALATED
    assert out.stopping_rule_override is True

    m = compute_metrics(db, settings)
    assert m["stopping_rule_overrides"] == 1
    assert m["escalated_cases"] == 1
    assert m["stub_mode_actions"] == 1  # the escalate stub did fire
    assert m["cases_resolved_without_retry"] == 1