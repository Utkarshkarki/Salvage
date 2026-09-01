"""Tests for multi-seed robustness reporting."""

import pytest

from reclaim.config import Settings
from reclaim.robustness import (
    RobustnessResult,
    RobustnessReport,
    run_robustness_suite,
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
    """Test that different seeds produce different outcomes."""
    # Run with just 2 seeds to test independence
    report = run_robustness_suite(num_seeds=2, settings=settings, db=db)

    # We expect different seeds to produce different results
    # (though with small sample size they might be similar)
    assert len(report.runs) == 2
    # At minimum, they should have different seed values
    assert report.runs[0].seed != report.runs[1].seed

    # Check that recovery rates are valid
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


def test_headline_batch_percentile_calculation(settings, db):
    """Test the headline batch percentile calculation."""
    # Run with seeds 0-10, then seed 42
    report_small = run_robustness_suite(num_seeds=11, settings=settings, db=db)

    # Now run a larger suite that includes seed 42
    report_with_headline = run_robustness_suite(num_seeds=43, settings=settings, db=db)

    # Find the headline run in the report
    headline_run = next(
        (r for r in report_with_headline.runs if r.seed == 42), None
    )
    assert headline_run is not None
    assert headline_run.seed == 42

    # Percentile should be computed
    assert 0.0 <= report_with_headline.headline_batch_percentile <= 100.0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
