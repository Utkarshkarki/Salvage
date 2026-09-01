"""
Multi-seed robustness reporting — runs the batch across N independently seeded
synthetic batches, collecting recovery metrics to demonstrate statistical rigor.
"""

import statistics
from dataclasses import dataclass

from reclaim.config import Settings, get_settings
from reclaim.db import Database, get_db
from reclaim.metrics import compute_metrics
from reclaim.pipeline import run_batch
from reclaim.synthetic import generate_batch
from reclaim.webhook import ingest_event


@dataclass(frozen=True)
class RobustnessResult:
    """One run's outcome."""

    seed: int
    recovery_rate: float
    recovered_amount: float
    amount_at_risk: float
    recovered_cases: int
    total_cases: int


@dataclass(frozen=True)
class RobustnessReport:
    """Distribution statistics across N runs."""

    runs: list[RobustnessResult]
    recovery_rate_median: float
    recovery_rate_p5: float
    recovery_rate_p95: float
    recovery_rate_stddev: float
    recovered_amount_median: float
    recovered_amount_p5: float
    recovered_amount_p95: float
    recovered_amount_stddev: float
    # Position of the default headline batch in the distribution
    headline_batch_seed: int
    headline_batch_percentile: float


def run_robustness_suite(
    num_seeds: int = 100,
    *,
    settings: Settings | None = None,
    db: Database | None = None,
) -> RobustnessReport:
    """
    Run the batch across N independently seeded synthetic batches.

    Args:
        num_seeds: Number of independent seeds to run (default 100).
        settings: Optional Settings override (uses cached default if None).
        db: Optional Database override (uses cached default if None).

    Returns:
        RobustnessReport with distribution statistics and headline-batch percentile.
    """
    if settings is None:
        settings = get_settings()
    if db is None:
        db = get_db()

    results: list[RobustnessResult] = []
    webhook_secret = settings.razorpay_webhook_secret

    for seed in range(num_seeds):
        # Generate batch with this seed
        batch = generate_batch(
            n_valid=60,
            n_duplicates=6,
            n_rejections=7,
            seed=seed,
            webhook_secret=webhook_secret,
        )

        # Ingest all valid deliveries
        case_ids = []
        for delivery in batch.valid_deliveries():
            event = delivery.event
            if event is not None:
                case, is_new, _ = ingest_event(db, event, settings)
                if is_new:
                    case_ids.append(case.case_id)

        # Run the pipeline
        run_batch(case_ids, settings=settings, db=db)

        # Compute metrics
        metrics = compute_metrics(db, settings)

        results.append(
            RobustnessResult(
                seed=seed,
                recovery_rate=metrics["recovery_rate"],
                recovered_amount=metrics["recovered_amount"],
                amount_at_risk=metrics["amount_at_risk"],
                recovered_cases=metrics["recovered_cases"],
                total_cases=metrics["total_cases"],
            )
        )

    # Compute distribution statistics
    recovery_rates = [r.recovery_rate for r in results]
    recovered_amounts = [r.recovered_amount for r in results]

    recovery_rate_median = statistics.median(recovery_rates)
    recovery_rate_p5 = sorted(recovery_rates)[int(len(recovery_rates) * 0.05)]
    recovery_rate_p95 = sorted(recovery_rates)[int(len(recovery_rates) * 0.95)]
    recovery_rate_stddev = (
        statistics.stdev(recovery_rates) if len(recovery_rates) > 1 else 0.0
    )

    recovered_amount_median = statistics.median(recovered_amounts)
    recovered_amount_p5 = sorted(recovered_amounts)[int(len(recovered_amounts) * 0.05)]
    recovered_amount_p95 = sorted(recovered_amounts)[int(len(recovered_amounts) * 0.95)]
    recovered_amount_stddev = (
        statistics.stdev(recovered_amounts) if len(recovered_amounts) > 1 else 0.0
    )

    # Find the headline batch (seed 42) in the distribution
    headline_seed = 42
    headline_rate = next(
        (r.recovery_rate for r in results if r.seed == headline_seed),
        None,
    )
    if headline_rate is not None:
        percentile = (
            sum(1 for r in results if r.recovery_rate < headline_rate) / len(results)
        ) * 100
    else:
        percentile = 0.0

    return RobustnessReport(
        runs=results,
        recovery_rate_median=recovery_rate_median,
        recovery_rate_p5=recovery_rate_p5,
        recovery_rate_p95=recovery_rate_p95,
        recovery_rate_stddev=recovery_rate_stddev,
        recovered_amount_median=recovered_amount_median,
        recovered_amount_p5=recovered_amount_p5,
        recovered_amount_p95=recovered_amount_p95,
        recovered_amount_stddev=recovered_amount_stddev,
        headline_batch_seed=headline_seed,
        headline_batch_percentile=percentile,
    )


if __name__ == "__main__":
    import sys

    num_seeds = 100
    if len(sys.argv) > 1:
        try:
            num_seeds = int(sys.argv[1])
        except ValueError:
            print(f"Usage: python -m reclaim.robustness [num_seeds]")
            sys.exit(1)

    print(f"Running robustness suite with {num_seeds} seeds...")
    report = run_robustness_suite(num_seeds=num_seeds)

    print("\n" + "=" * 60)
    print("Robustness Report")
    print("=" * 60)
    print(f"\nRecovery Rate Distribution (across {num_seeds} runs):")
    print(f"  Median:        {report.recovery_rate_median:.4f} ({report.recovery_rate_median*100:.2f}%)")
    print(f"  5th percentile: {report.recovery_rate_p5:.4f} ({report.recovery_rate_p5*100:.2f}%)")
    print(f"  95th percentile:{report.recovery_rate_p95:.4f} ({report.recovery_rate_p95*100:.2f}%)")
    print(f"  Std dev:        {report.recovery_rate_stddev:.4f}")

    print(f"\nRecovered Amount Distribution (across {num_seeds} runs):")
    print(f"  Median:         ₹{report.recovered_amount_median:,.2f}")
    print(f"  5th percentile:  ₹{report.recovered_amount_p5:,.2f}")
    print(f"  95th percentile: ₹{report.recovered_amount_p95:,.2f}")
    print(f"  Std dev:         ₹{report.recovered_amount_stddev:,.2f}")

    print(f"\nHeadline Batch (seed {report.headline_batch_seed}):")
    headline_run = next(
        (r for r in report.runs if r.seed == report.headline_batch_seed), None
    )
    if headline_run:
        print(f"  Recovery rate:  {headline_run.recovery_rate:.4f} ({headline_run.recovery_rate*100:.2f}%)")
        print(
            f"  Percentile:     {report.headline_batch_percentile:.1f}th "
            f"({'above' if report.headline_batch_percentile > 50 else 'below'} median)"
        )
        print(f"  Recovered:      {headline_run.recovered_cases} / ₹{headline_run.recovered_amount:,.2f}")
    print("=" * 60)
