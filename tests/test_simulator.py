"""Rule Sensitivity Simulator (/simulator) — comparison + reuse invariants."""

from __future__ import annotations

from reclaim.api import _run_simulated_batch, _sim_metric_key
from reclaim.config import Settings


def _settings(**overrides) -> Settings:
    base = dict(
        _env_file=None,
        razorpay_webhook_secret="test-secret",
        llm_mode="offline",
        act_mode="stub",
        reclaim_celery_eager=True,
        database_url="sqlite:///:memory:",
    )
    base.update(overrides)
    return Settings(**base)


def test_simulator_baseline_reproduces_reference_metrics() -> None:
    """seed=42 baseline must match the documented demo report (recovery 0.2069,
    ₹39,776 recovered, 23 escalated, 5 stopped, 60 total)."""
    metrics = _run_simulated_batch(_settings(), {})
    assert metrics["total_cases"] == 60
    assert metrics["recovered_amount"] == 39776.0
    assert round(metrics["recovery_rate"], 4) == 0.2069
    assert metrics["escalated_cases"] == 23
    assert metrics["stopped_cases"] == 5


def test_simulator_tightens_escalation_when_threshold_lowered() -> None:
    """Lowering the amount threshold must push more cases to escalate — the
    comparison reflects real rule sensitivity, not a no-op."""
    baseline = _run_simulated_batch(_settings(), {})
    tight = _run_simulated_batch(_settings(), {"escalation_amount_threshold": 1.0})
    assert tight["escalated_cases"] > baseline["escalated_cases"]
    assert tight["recovered_amount"] < baseline["recovered_amount"]


def test_simulator_metric_key_labelling() -> None:
    """The before/after row builder reads straight from compute_metrics and
    renders fractions as percentages."""
    metrics = _run_simulated_batch(_settings(), {})
    rows = _sim_metric_key(metrics)
    labels = [label for label, _ in rows]
    assert "Recovery rate" in labels
    assert "Escalated (human)" in labels
    assert "Stopped (deliberate halt)" in labels
    assert "Amount recovered (INR)" in labels
    rate_row = next(v for l, v in rows if l == "Recovery rate")
    assert rate_row.endswith("%")


def test_simulator_does_not_mutate_real_settings() -> None:
    """Overrides are passed through a throwaway Settings copy; the passed
    settings object must be untouched."""
    settings = _settings()
    before = settings.escalation_amount_threshold
    _run_simulated_batch(settings, {"escalation_amount_threshold": 1.0})
    assert settings.escalation_amount_threshold == before