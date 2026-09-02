"""Tests for counterfactual baseline comparison."""

import pytest

from reclaim.baseline import (
    StrategyResult,
    BaselineComparison,
    run_baseline_comparison,
    _simulate_do_nothing,
    _simulate_retry_everything,
    _identify_retry_eligible_cases,
    _case_success,
    _build_fresh_db,
    _run_baseline_comparison,
)


def test_strategy_result_dataclass():
    """Test the StrategyResult dataclass."""
    result = StrategyResult(
        name="do_nothing",
        gateway_calls=0,
        cases_succeeded=0,
        gross_recovered=0.0,
        policy_blocked_value=0.0,
        net_recovered=0.0,
    )
    assert result.name == "do_nothing"
    assert result.gateway_calls == 0
    assert result.cases_succeeded == 0
    assert result.gross_recovered == 0.0
    assert result.policy_blocked_value == 0.0
    assert result.net_recovered == 0.0


def test_baseline_comparison_dataclass():
    """Test the BaselineComparison dataclass."""
    do_nothing = StrategyResult(
        name="do_nothing",
        gateway_calls=0,
        cases_succeeded=0,
        gross_recovered=0.0,
        policy_blocked_value=0.0,
        net_recovered=0.0,
    )

    retry_everything = StrategyResult(
        name="retry_everything",
        gateway_calls=10,
        cases_succeeded=5,
        gross_recovered=1000.0,
        policy_blocked_value=200.0,
        net_recovered=830.0,  # 1000 - (200 * 0.85)
    )

    reclaim = StrategyResult(
        name="reclaim",
        gateway_calls=5,
        cases_succeeded=4,
        gross_recovered=800.0,
        policy_blocked_value=0.0,
        net_recovered=800.0,
    )

    comparison = BaselineComparison(
        seed=42,
        do_nothing=do_nothing,
        retry_everything=retry_everything,
        reclaim=reclaim,
    )

    assert comparison.seed == 42
    assert comparison.do_nothing == do_nothing
    assert comparison.retry_everything == retry_everything
    assert comparison.reclaim == reclaim


def test_do_nothing_strategy(settings, db):
    """Test that do_nothing strategy has zero calls and zero recovery."""
    # Create a few case IDs for testing
    case_ids = ["test_case_1", "test_case_2", "test_case_3"]

    result = _simulate_do_nothing(db, case_ids, settings)

    assert result.name == "do_nothing"
    assert result.gateway_calls == 0
    assert result.gross_recovered == 0.0
    assert result.policy_blocked_value == 0.0
    assert result.net_recovered == 0.0


def test_retry_everything_strategy(settings, db):
    """Test that retry_everything strategy counts calls correctly and simulates
    realistic retry success based on decline codes, not 100% success."""
    # This test doesn't actually need real cases since we're just checking
    # the counting logic
    case_ids = ["test_case_1", "test_case_2", "test_case_3"]

    result = _simulate_retry_everything(db, case_ids, settings, seed=42)

    assert result.name == "retry_everything"
    assert result.gateway_calls == len(case_ids)
    assert result.gross_recovered >= 0.0
    # Policy blocked value should be 0 in this simplified test
    assert result.policy_blocked_value == 0.0
    # Net recovered should be computed correctly
    assert result.net_recovered >= 0.0


def test_identify_retry_eligible_cases(settings, db):
    """Test identification of cases that would be retry-eligible under real policy."""
    # Smoke test: empty case list should yield empty set, no error
    case_ids = []
    eligible = _identify_retry_eligible_cases(db, case_ids, settings)
    assert isinstance(eligible, set)
    assert len(eligible) == 0


def test_run_baseline_comparison_smoke_test(settings, db):
    """Test that baseline comparison runs without error (smoke test)."""
    comparison = run_baseline_comparison(seed=42, settings=settings, db=db)

    assert isinstance(comparison, BaselineComparison)
    assert comparison.seed == 42

    # Check all strategies are present
    assert comparison.do_nothing.name == "do_nothing"
    assert comparison.retry_everything.name == "retry_everything"
    assert comparison.reclaim.name == "reclaim"

    # Verify metrics types
    for strategy in [comparison.do_nothing, comparison.retry_everything, comparison.reclaim]:
        assert isinstance(strategy.gateway_calls, int)
        assert isinstance(strategy.gross_recovered, float)
        assert isinstance(strategy.policy_blocked_value, float)
        assert isinstance(strategy.net_recovered, float)


def test_retry_everything_makes_more_calls_than_reclaim(settings, db):
    """
    Test that retry_everything makes at least as many gateway calls as reclaim.

    This validates that the real policy's stopping rules reduce calls.
    """
    comparison = run_baseline_comparison(seed=42, settings=settings, db=db)

    # retry_everything should make one call per case
    # reclaim should make fewer or equal calls (due to stopping rules)
    assert comparison.retry_everything.gateway_calls >= comparison.reclaim.gateway_calls


def test_chargeback_netting_math():
    """Test the chargeback-netting math with known values."""
    # Gross recovered: 1000
    # Policy blocked value: 200
    # Chargeback rate: 85%
    # Expected net: 1000 - (200 * 0.85) = 1000 - 170 = 830

    gross = 1000.0
    blocked = 200.0
    chargeback_rate = 0.85

    net = gross - (blocked * chargeback_rate)
    assert net == 830.0

    # Test edge cases
    assert gross - (0.0 * chargeback_rate) == gross  # No blocked value
    assert 0.0 - (blocked * chargeback_rate) == -170.0  # Negative net


def test_retry_everything_counts_full_batch_against_preexisting_data(settings, tmp_path):
    """Regression: baseline must not be zeroed by a DB already populated with a
    prior seed-42 batch — the real CLI scenario (running against the collected
    ``reclaim.db``).

    Pre-fix, ``run_baseline_comparison`` re-ingested the seeded events into the
    real/already-populated DB; ``ingest_event`` deduped all 60 (UNIQUE event_id),
    leaving ``case_ids`` empty and retry_everything at 0 calls / ₹0 gross. We
    reproduce that exactly: build a fresh DB, ingest + run the FULL real batch
    through the real webhook boundary + pipeline, then run the baseline against
    that populated DB. The comparison must isolate itself on a fresh DB and
    still count every generated case — the regression that the minimal hand-
    built fixtures (which always run on a fresh DB) could not catch.
    """
    from reclaim.db import Database, init_schema
    from reclaim.pipeline import run_batch
    from reclaim.synthetic import generate_batch
    from reclaim.webhook import ingest_event

    # Populate a DB exactly like a prior `python -m reclaim.batch` run.
    url = f"sqlite:///{tmp_path / 'already_populated.db'}"
    populated = Database(settings.model_copy(update={"database_url": url}))
    init_schema(populated.engine)
    try:
        batch = generate_batch(
            n_valid=60, n_duplicates=6, n_rejections=7, seed=42,
            webhook_secret=settings.razorpay_webhook_secret,
        )
        ids = []
        for d in batch.valid_deliveries():
            if d.event is not None:
                case, is_new, _ = ingest_event(populated, d.event, settings)
                if is_new:
                    ids.append(case.case_id)
        run_batch(ids, settings=settings, db=populated)
        assert len(ids) == 60  # the real batch ingested all 60 valid cases
    finally:
        populated.close()

    # Run the baseline against that populated DB (pre-fix CLI scenario).
    comparison = run_baseline_comparison(seed=42, settings=settings, db=populated)

    assert comparison.retry_everything.gateway_calls == 60
    assert comparison.retry_everything.gross_recovered > 0
    assert comparison.do_nothing.gateway_calls == 0
    assert comparison.do_nothing.gross_recovered == 0.0


def test_case_success_is_pure_and_order_independent():
    """The controlled counterfactual's core guarantee: a (seed, case) draws ONE
    outcome, independent of how many cases came before it.

    Pre-fix, each strategy drained its own ``Random(seed)`` stream, so the draw
    for case X depended on how many prior cases that strategy happened to
    attempt — the same case could 'succeed' for one strategy and 'fail' for the
    other. A per-case seeded draw (``Random("seed:case")``) makes the outcome a
    pure function of the case, which is the only way 'same world, different
    policy' can be literal. This test pins that: results are identical no
    matter the iteration order, and repeatable.
    """
    cases = [("case_a", "R01"), ("case_b", "54"), ("case_c", "91"), ("case_d", "N7")]
    forward = [_case_success(42, c, r) for c, r in cases]
    reversed_then_forward = [
        _case_success(42, c, r) for c, r in reversed(cases)
    ][::-1]
    assert forward == reversed_then_forward, (
        "the outcome for a case changed when it was drawn after different "
        "preceding cases — the draw is stream-position dependent (not a "
        "controlled counterfactual)"
    )
    # Determinism: same key, same result on re-call.
    assert [_case_success(42, c, r) for c, r in cases] == forward


def test_same_world_counterfactual_both_strategies_read_same_draw(settings):
    """Both strategies must see the SAME per-case realization in a real run.

    After running the real counterfactual on a fresh isolated DB, recompute the
    expected success counts independently from the per-case model and check
    both strategies' reported counts match. retry_everything attempts every
    case; reclaim attempts only its retry-eligible subset — so under a shared
    per-case draw, retry_everything's successes are a strict superset of
    reclaim's. The pre-fix two-streams implementation violated exactly this.
    """
    db, tmp_path = _build_fresh_db(settings)
    try:
        seed = 42
        comparison = _run_baseline_comparison(seed=seed, settings=settings, db=db)

        from reclaim.db import RecoveryCaseRow

        with db.create_session() as s:
            rows = s.query(RecoveryCaseRow).all()
        assert len(rows) >= 2  # sanity: a real batch was ingested

        case_ids = [r.case_id for r in rows]

        expected_retry_all = sum(
            1 for r in rows if _case_success(seed, r.case_id, r.failure_reason)
        )
        eligible = _identify_retry_eligible_cases(db, case_ids, settings)
        expected_reclaim = sum(
            1
            for r in rows
            if r.case_id in eligible
            and _case_success(seed, r.case_id, r.failure_reason)
        )

        # Same world, different policy: counts reconcile exactly.
        assert comparison.retry_everything.cases_succeeded == expected_retry_all
        assert comparison.reclaim.cases_succeeded == expected_reclaim
        # Superset: retry_everything cannot under-recover vs reclaim for the
        # cases reclaim attempts — the same draw is read by both.
        assert (
            comparison.retry_everything.cases_succeeded
            >= comparison.reclaim.cases_succeeded
        )
        # And the simulated policy actually differs from naive retrying.
        assert comparison.reclaim.gateway_calls < comparison.retry_everything.gateway_calls
    finally:
        db.close()
        import os
        try:
            os.remove(tmp_path)
        except OSError:  # pragma: no cover - best-effort cleanup
            pass


def test_baseline_cli_import():
    """Test that the module can be imported for CLI execution."""
    import importlib
    module = importlib.import_module("reclaim.baseline")
    assert hasattr(module, "run_baseline_comparison")
    assert hasattr(module, "StrategyResult")
    assert hasattr(module, "BaselineComparison")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
