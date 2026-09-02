"""
Multi-seed robustness reporting — runs the batch across N independently seeded
synthetic batches, collecting recovery metrics to demonstrate statistical rigor.

Each seed runs on its OWN freshly-created, file-backed temp database. That
isolation is load-bearing, not a convenience:

* Batches are only "independent" if the metrics for seed N reflect only that
  seed's cases. The synthetic generator reuses event ids across seeds
  (``evt_00001``…, see synthetic.py), so ingesting a second seed's batch into a
  shared DB dedupes it against the first (UNIQUE event_id) — every seed reports
  the SAME metrics and the "distribution" is one sample repeated (stddev 0).
* It also protects the real ``reclaim.db`` from being polluted with N×60 cases
  by a CLI invocation.

File-backed (not in-memory) for the same reason as the simulator/baseline:
``run_batch`` fans out across threads and a shared in-memory SQLite engine
fragments into per-connection empty databases.
"""

import os
import random
import statistics
import tempfile
from dataclasses import dataclass

from reclaim.config import Settings, get_settings
from reclaim.db import Database, init_schema
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


def _run_one_seed(seed: int, settings: Settings) -> RobustnessResult:
    """Ingest + run + measure one seed on a fresh, isolated temp database.

    Isolation per seed is required for independence (see module docstring):
    a shared DB would dedupe every seed beyond the first on UNIQUE event_id,
    collapsing the distribution. The temp file is removed on exit.
    """
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    tmp_settings = settings.model_copy(update={"database_url": f"sqlite:///{path}"})
    db = Database(tmp_settings)
    init_schema(db.engine)
    try:
        # Generate batch with this seed
        batch = generate_batch(
            n_valid=60,
            n_duplicates=6,
            n_rejections=7,
            seed=seed,
            webhook_secret=settings.razorpay_webhook_secret,
        )

        # Ingest all valid deliveries into THIS seed's fresh DB.
        case_ids = []
        for delivery in batch.valid_deliveries():
            event = delivery.event
            if event is not None:
                case, is_new, _ = ingest_event(db, event, settings)
                if is_new:
                    case_ids.append(case.case_id)

        # Run the pipeline
        run_batch(case_ids, settings=settings, db=db)

        # Compute metrics over ONLY this seed's isolated DB.
        metrics = compute_metrics(db, settings)

        return RobustnessResult(
            seed=seed,
            recovery_rate=metrics["recovery_rate"],
            recovered_amount=metrics["recovered_amount"],
            amount_at_risk=metrics["amount_at_risk"],
            recovered_cases=metrics["recovered_cases"],
            total_cases=metrics["total_cases"],
        )
    finally:
        db.close()
        try:
            os.remove(path)
        except OSError:  # pragma: no cover - best-effort cleanup
            pass


def _build_report(
    results: list[RobustnessResult], headline_seed: int = 42
) -> RobustnessReport:
    """Compute distribution statistics from a list of per-seed results.

    Extracted from ``run_robustness_suite`` so the percentile/stat plumbing is
    unit-testable without paying for N full batch runs — the expensive part is
    producing the results, not averaging them.
    """
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

    # Find the headline batch in the distribution.
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


def run_robustness_suite(
    num_seeds: int = 100,
    *,
    settings: Settings | None = None,
    db: Database | None = None,
) -> RobustnessReport:
    """
    Run the batch across N independently seeded synthetic batches.

    Every seed runs on its own fresh, isolated temp database (see
    ``_run_one_seed``), so the result is a genuine distribution of independent
    runs — never a single sample repeated N times, and never data polluted by
    pre-existing cases. The ``db`` argument is accepted for source-compatibity
    but is not used for the computation, for the same reason a counterfactual
    must run on the batch alone.

    Args:
        num_seeds: Number of independent seeds to run (default 100).
        settings: Optional Settings override (uses cached default if None).
        db: Ignored for computation (kept for signature stability).

    Returns:
        RobustnessReport with distribution statistics and headline-batch percentile.
    """
    if settings is None:
        settings = get_settings()

    results: list[RobustnessResult] = []
    for seed in range(num_seeds):
        results.append(_run_one_seed(seed, settings))

    return _build_report(results, headline_seed=42)


if __name__ == "__main__":
    import sys

    # The report prints ₹ (U+20B9); a Windows console on the default cp1252
    # codec raises UnicodeEncodeError on it. Force UTF-8 so the CLI never
    # crashes on output (mojibake > crash). Mirrors the same fix in
    # ``baseline.py``'s ``__main__``.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

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
