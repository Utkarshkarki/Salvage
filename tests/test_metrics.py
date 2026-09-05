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

    # 20000 paise = Rs.200: above the R7 economic floor (100), below R2 (5000),
    # with the cooldown elapsed -> a healthy retry_now -> recovered.
    case, is_new, _ = _ingest(
        db, settings, _webhook_body(error_code="R01", days_ago=3, amount=20000), "evt_m4"
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


def test_rule_override_boolean_flag_is_accurate(settings, db: Database) -> None:
    """Regression test: the rule_override boolean is explicitly set on the audit
    entry for a rule override, and NOT set for a normal LLM-driven decision."""
    from reclaim.pipeline import run_case
    from reclaim import repo

    # 1. Normal LLM decision (amount below threshold, mandate intact, etc.)
    # 200 rupees (20000 paise)
    case1, is_new1, _ = _ingest(
        db, settings, _webhook_body(error_code="R01", days_ago=3, amount=20000, sub="sub_bool_1"), "evt_bool_1"
    )
    assert is_new1
    run_case(case1.case_id, settings=settings, db=db)
    
    trail1 = repo.audit_trail(db, case1.case_id)
    decide_entry1 = next(e for e in trail1 if e.stage == "decide")
    assert decide_entry1.rule_override is False

    # 2. Rule override (mandate revoked -> R1)
    case2, is_new2, _ = _ingest(
        db, settings, _webhook_body(error_code="R0", sub="sub_bool_2"), "evt_bool_2"
    )
    assert is_new2
    run_case(case2.case_id, settings=settings, db=db)

    trail2 = repo.audit_trail(db, case2.case_id)
    decide_entry2 = next(e for e in trail2 if e.stage == "decide")
    assert decide_entry2.rule_override is True


def test_audit_entry_disambiguates_llm_failure_from_rule_override(
    settings, db: Database, monkeypatch
) -> None:
    """Regression test: the two booleans on the DECIDE audit entry are disjoint
    and mean different things — ``fallback_triggered`` is True ONLY when the LLM
    call itself failed, ``rule_override`` is True ONLY when a rule rejected a
    *valid* LLM proposal. A rule override must never set ``fallback_triggered``.

    Asserts the full pair on the real audit row for each case:
      - genuine LLM failure:        fallback_triggered=True , rule_override=False
      - valid proposal overridden:  fallback_triggered=False, rule_override=True
    """
    from reclaim import llm_client, repo
    from reclaim.pipeline import run_case

    # Case A: the decide LLM genuinely fails -> fallback used, no rule fired.
    with monkeypatch.context() as m:
        m.setattr(
            llm_client.LLMClient,
            "decide",
            lambda self, i: (_ for _ in ()).throw(RuntimeError("ollama unreachable")),
        )
        case_a, is_new_a, _ = _ingest(
            db, settings,
            _webhook_body(error_code="R01", sub="sub_disc_a"), "evt_disc_a",
        )
        assert is_new_a
        out_a = run_case(case_a.case_id, settings=settings, db=db)
        assert out_a.llm_failure is True
        assert out_a.stopping_rule_override is False
        decide_a = next(
            e for e in repo.audit_trail(db, case_a.case_id) if e.stage == "decide"
        )
        assert decide_a.fallback_triggered is True
        assert decide_a.rule_override is False

    # Case B: the LLM proposes validly, but R1 (mandate revoked) overrides it.
    # The patch is undone; the wrapper's normal success path runs.
    case_b, is_new_b, _ = _ingest(
        db, settings, _webhook_body(error_code="R0", sub="sub_disc_b"), "evt_disc_b"
    )
    assert is_new_b
    out_b = run_case(case_b.case_id, settings=settings, db=db)
    assert out_b.stopping_rule_override is True
    assert out_b.llm_failure is False
    decide_b = next(
        e for e in repo.audit_trail(db, case_b.case_id) if e.stage == "decide"
    )
    assert decide_b.fallback_triggered is False
    assert decide_b.rule_override is True


# ---------------------------------------------------------------------------
# Provenance tier (Phase 6.1): metrics are never silently provenance-blended
# ---------------------------------------------------------------------------


def test_provenance_breakdown_counts_by_provenance(settings, db: Database) -> None:
    """Aggregates disclose their composition: live + replay + mocked, always."""
    from reclaim.metrics import compute_metrics
    from reclaim.models import Provenance

    _ingest(db, settings, _webhook_body(sub="pbl_live"), "evt_pbl_live")          # default LIVE
    _ingest(db, settings, _webhook_body(sub="pbl_replay"), "evt_pbl_replay", provenance=Provenance.REPLAY)

    m = compute_metrics(db, settings)
    assert m["provenance_breakdown"] == {
        "live": 1,
        "replay": 1,
        "mocked": 0,
    }
    assert m["total_cases"] == 2


def test_provenance_filter_derives_homogeneous_metric(settings, db: Database) -> None:
    """``compute_metrics(provenance="live")`` must differ from the blended aggregate:
    a live case and a synthetic case are never silently merged into one recovery rate."""
    from reclaim.metrics import compute_metrics
    from reclaim.models import Provenance
    from reclaim.pipeline import run_case

    # Live: a healthy retry_now -> recovered (20000 paise = Rs.200).
    _ingest(db, settings, _webhook_body(error_code="R01", days_ago=3, amount=20000, sub="pf_live"), "evt_pf_live")
    # Replay: ANOTHER recovery (30000 paise = Rs.300) — synthetic, so if the two
    # were silently blended the recovered amount would look like Rs.500 as if it
    # were all live traffic.
    _ingest(db, settings, _webhook_body(error_code="R01", days_ago=3, amount=30000, sub="pf_replay"), "evt_pf_replay", provenance=Provenance.REPLAY)

    run_case("sub_pf_live", settings=settings, db=db)
    run_case("sub_pf_replay", settings=settings, db=db)

    blended = compute_metrics(db, settings)
    live_only = compute_metrics(db, settings, provenance="live")

    # The blended aggregate mixes both; the filtered view is homogeneous.
    assert blended["total_cases"] == 2
    assert blended["recovered_amount"] == 500.0  # 200 live + 300 replay
    assert live_only["total_cases"] == 1
    assert live_only["recovered_cases"] == 1
    assert live_only["recovered_amount"] == 200.0  # live traffic only, never the blend
    assert live_only["recovery_rate"] > 0.0
    assert live_only["provenance_breakdown"] == {"live": 1, "replay": 0, "mocked": 0}