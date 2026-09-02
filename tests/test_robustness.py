"""Tests for multi-seed robustness reporting."""

import pytest

from reclaim.config import Settings
from reclaim.robustness import (
    RobustnessResult,
    RobustnessReport,
    _build_report,
    run_robustness_suite,
)


def _result(seed: int, rate: float, amount: float) -> RobustnessResult:
    """Compact crafted per-seed result for unit-testing the report plumbing."""
    return RobustnessResult(
        seed=seed,
        recovery_rate=rate,
        recovered_amount=amount,
        amount_at_risk=10000.0,
        recovered_cases=round(rate * 60),
        total_cases=60,
    )


def test_robustness_result_dataclass():
    """Test the RobustnessResult dataclass."""
    result = RobustnessResult(
        seed=42,
        recovery_rate=0.5,
        recovered_amount=1000.0,
        amount_at_risk=2000.0,
        recovered_cases=5,
        total_cases=10,
    )
    assert result.seed == 42
    assert result.recovery_rate == 0.5
    assert result.recovered_amount == 1000.0
    assert result.amount_at_risk == 2000.0
    assert result.recovered_cases == 5
    assert result.total_cases == 10


def test_robustness_report_dataclass():
    """Test the RobustnessReport dataclass."""
    runs = [
        RobustnessResult(
            seed=1,
            recovery_rate=0.2,
            recovered_amount=500.0,
            amount_at_risk=2500.0,
            recovered_cases=1,
            total_cases=5,
        ),
        RobustnessResult(
            seed=2,
            recovery_rate=0.4,
            recovered_amount=1000.0,
            amount_at_risk=2500.0,
            recovered_cases=2,
            total_cases=5,
        ),
        RobustnessResult(
            seed=3,
            recovery_rate=0.6,
            recovered_amount=1500.0,
            amount_at_risk=2500.0,
            recovered_cases=3,
            total_cases=5,
        ),
    ]

    report = RobustnessReport(
        runs=runs,
        recovery_rate_median=0.4,
        recovery_rate_p5=0.2,
        recovery_rate_p95=0.6,
        recovery_rate_stddev=0.2,
        recovered_amount_median=1000.0,
        recovered_amount_p5=500.0,
        recovered_amount_p95=1500.0,
        recovered_amount_stddev=500.0,
        headline_batch_seed=42,
        headline_batch_percentile=50.0,
    )

    assert report.runs == runs
    assert report.recovery_rate_median == 0.4
    assert report.recovery_rate_p5 == 0.2
    assert report.recovery_rate_p95 == 0.6
    assert report.recovery_rate_stddev == 0.2
    assert report.recovered_amount_median == 1000.0
    assert report.recovered_amount_p5 == 500.0
    assert report.recovered_amount_p95 == 1500.0
    assert report.recovered_amount_stddev == 500.0
    assert report.headline_batch_seed == 42
    assert report.headline_batch_percentile == 50.0


def test_run_robustness_suite_smoke_test(settings, db):
    """Test that the robustness suite runs without error (smoke test)."""
    report = run_robustness_suite(num_seeds=3, settings=settings, db=db)

    assert len(report.runs) == 3
    assert all(isinstance(r, RobustnessResult) for r in report.runs)
    assert all(r.seed in {0, 1, 2} for r in report.runs)

    # Check that distribution stats are computed
    assert isinstance(report.recovery_rate_median, float)
    assert isinstance(report.recovery_rate_p5, float)
    assert isinstance(report.recovery_rate_p95, float)
    assert isinstance(report.recovery_rate_stddev, float)
    assert isinstance(report.recovered_amount_median, float)
    assert isinstance(report.recovered_amount_p5, float)
    assert isinstance(report.recovered_amount_p95, float)
    assert isinstance(report.recovered_amount_stddev, float)

    # Headline batch should be seed 42, but it's not in our small run
    assert report.headline_batch_seed == 42
    # Percentile might be 0.0 if headline seed not in runs
    assert isinstance(report.headline_batch_percentile, float)


def test_robustness_suite_different_outcomes(settings, db):
    """Regression: genuinely independent per-seed runs must DIFFER.

    Each seed runs on its own fresh, isolated temp DB against a distinct
    synthetic batch, so the metric distribution must have spread — by no means
    a single sample repeated N times. Pre-fix, a shared DB deduped every seed
    beyond the first (event ids are seed-invariant) and all runs reported
    identical rates/amounts with stddev provably 0; this test is the guard.
    """
    report = run_robustness_suite(num_seeds=3, settings=settings, db=db)

    assert len(report.runs) == 3
    distinct_outcomes = {(r.recovery_rate, r.recovered_amount) for r in report.runs}
    assert len(distinct_outcomes) >= 2, (
        "all seeds reported identical recovery — the distribution is one "
        f"sample repeated: {sorted(distinct_outcomes)}"
    )

    # Rates are valid
    for run in report.runs:
        assert 0.0 <= run.recovery_rate <= 1.0
        assert run.recovered_amount >= 0.0
        assert run.amount_at_risk > 0.0
        assert run.recovered_cases >= 0
        assert run.total_cases > 0


def test_robustness_cli_import():
    """Test that the module can be imported for CLI execution."""
    # This tests that the module can be imported (no syntax errors)
    # and that the __main__ block would execute
    import importlib
    module = importlib.import_module("reclaim.robustness")
    assert hasattr(module, "run_robustness_suite")
    assert hasattr(module, "RobustnessResult")
    assert hasattr(module, "RobustnessReport")


def test_robustness_distribution_stats_math():
    """Test the distribution statistics math with a known small dataset."""
    # Test percentiles
    import statistics

    test_rates = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0]
    median = statistics.median(test_rates)
    p5 = sorted(test_rates)[int(len(test_rates) * 0.05)]
    p95 = sorted(test_rates)[int(len(test_rates) * 0.95)]

    assert median == 0.55  # (0.5 + 0.6) / 2
    assert p5 == 0.1  # 5th percentile
    assert p95 == 1.0  # 95th percentile

    # Test standard deviation
    stddev = statistics.stdev(test_rates)
    assert isinstance(stddev, float)
    assert stddev > 0.0


def test_headline_batch_percentile_calculation():
    """Headline percentile = share of runs whose rate beats seed 42's.

    Unit-tested on crafted runs so the distribution plumbing is verified
    without paying the 43-real-seed cost of a full ``run_robustness_suite``
    call (which the old integration test did — free only under the pre-fix
    shared-DB dedup, expensive once seeds are genuinely independent).
    """
    report = _build_report(
        [
            _result(seed=0, rate=0.10, amount=4_000.0),
            _result(seed=1, rate=0.20, amount=8_000.0),
            _result(seed=2, rate=0.30, amount=12_000.0),
            _result(seed=42, rate=0.40, amount=16_000.0),
            _result(seed=43, rate=0.50, amount=20_000.0),
        ],
        headline_seed=42,
    )
    assert report.headline_batch_seed == 42
    # 3 of 5 runs sit below seed 42's rate (0.10/0.20/0.30 < 0.40)
    assert report.headline_batch_percentile == 60.0
    # Spread is real, not one repeated sample.
    assert report.recovery_rate_stddev > 0.0
    assert report.recovered_amount_stddev > 0.0
    assert report.recovery_rate_median == 0.30


def test_headline_batch_percentile_when_seed_absent():
    """Seed 42 absent from the run -> percentile collapses to 0.0, no crash."""
    report = _build_report(
        [_result(seed=0, rate=0.10, amount=1.0), _result(seed=1, rate=0.20, amount=2.0)],
        headline_seed=42,
    )
    assert report.headline_batch_percentile == 0.0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
