"""Shared server-side view logic used by BOTH the legacy HTML routes (api.py)
and the new JSON API (api_v1.py).

Kept free of any FastAPI ``app``/routing so it can be imported without a
circular dependency: ``api.py`` mounts ``api_v1.py``'s router, so ``api_v1.py``
must not import from ``api.py``. This module is where the Jinja2 dashboard and
the JSON API agree on the *derived* views (simulator batch re-run, plain-
language customer status) so their behavior cannot drift.

``api.py`` re-imports ``_run_simulated_batch``, ``_sim_metric_key`` and
``_SIM_THRESHOLD_FIELDS`` from here so that ``tests/test_simulator.py`` (which
imports them as ``reclaim.api._run_simulated_batch`` / ``reclaim.api._sim_metric_key``)
keeps resolving — this is a behavior-preserving move, not a rewrite.
"""

from __future__ import annotations

import os
import tempfile
from typing import Any

from .config import Settings
from .db import Database, RecoveryCaseRow, init_schema
from .models import AuditLogEntry

# ---------------------------------------------------------------------------
# Rule Sensitivity Simulator (B1): shared by the HTML /simulator and the JSON
# POST /api/v1/simulator/run.
#
# Note on the throwaway temp-file DB: a shared in-memory SQLite engine cannot be
# read concurrently across the thread pool run_batch uses, so the simulated batch
# is run on a per-submission temp file that is torn down afterwards.
# ---------------------------------------------------------------------------

# Editable rule thresholds (subset of config.py that the simulator exposes).
_SIM_THRESHOLD_FIELDS = (
    "escalation_amount_threshold",
    "escalation_days_threshold",
    "max_retries_per_cycle",
    "cooldown_hours",
    "email_cap_per_7d",
)


def _run_simulated_batch(settings: Settings, overrides: dict[str, Any]) -> dict[str, Any]:
    """Run the seed-42 synthetic batch on a throwaway DB with ``overrides``.

    Returns the same shape as :func:`reclaim.metrics.compute_metrics`. The real
    ``settings`` object passed in is NEVER mutated — ``overrides`` are applied
    through a throwaway copy pointed at a throwaway temp-file SQLite DB. This
    is what lets the simulator answer "what if we tightened the threshold?"
    without any effect on production state.
    """
    from .pipeline import run_batch
    from .metrics import compute_metrics
    from .synthetic import generate_batch
    from .batch import ingest_batch

    with tempfile.TemporaryDirectory() as td:
        url = f"sqlite:///{os.path.join(td, 'sim.db')}"
        sim_settings = settings.model_copy(update={"database_url": url, **overrides})
        db = Database(sim_settings)
        init_schema(db.engine)
        batch = generate_batch(seed=42, webhook_secret=settings.razorpay_webhook_secret)
        new_ids, _, _ = ingest_batch(db, batch, sim_settings)
        run_batch(new_ids, settings=sim_settings, db=db)
        metrics = compute_metrics(db, sim_settings)
        db.close()
        return metrics


def _sim_metric_key(metrics: dict[str, Any]) -> list[tuple[str, str]]:
    """Human labels for the comparison rows, read straight from compute_metrics."""

    def _pct(v: Any) -> str:
        return f"{float(v) * 100:.1f}%"

    esc = int(metrics.get("escalated_cases", 0))
    stopped = int(metrics.get("stopped_cases", 0))
    return [
        ("Total cases", str(metrics.get("total_cases", 0))),
        ("Recovery rate", _pct(metrics.get("recovery_rate", 0.0))),
        ("Amount recovered (INR)", f"{float(metrics.get('recovered_amount', 0.0)):,.2f}"),
        ("Escalated (human)", str(esc)),
        ("Stopped (deliberate halt)", str(stopped)),
        ("LLM call failures", str(metrics.get("llm_call_failures", 0))),
        ("Stopping-rule overrides", str(metrics.get("stopping_rule_overrides", 0))),
        ("Stub-mode actions", str(metrics.get("stub_mode_actions", 0))),
    ]


# ---------------------------------------------------------------------------
# Customer status view (B3): shared by the HTML /status and the JSON
# GET /api/v1/status/{case_id}. Both expose the SAME plain-language rendering so
# the customer-facing surface never carries internal jargon.
# ---------------------------------------------------------------------------

_CAUSE_PLAIN: dict[str, str] = {
    "insufficient_funds": "Your payment was declined because the account had insufficient funds.",
    "card_expired": "The card we had on file has expired, so it could not be used.",
    "bank_timeout": "Your bank did not respond in time to the payment request.",
    "do_not_honor": "Your bank declined the payment.",
    "mandate_revoked": "The standing authorization (mandate) for this subscription was revoked.",
    "unknown": "We could not pinpoint the exact reason for the payment failure.",
}


def customer_view(row: RecoveryCaseRow, trail: list[AuditLogEntry]) -> dict[str, str]:
    """Build a plain-language snapshot from the audit trail (customer-safe).

    Pure data: returns ``{heading, reason, next_step}`` with NO internal rule
    ids, stage names, or LLM/fallback jargon. ``row``/``trail`` are the same
    case + audit data the merchant dashboard reads — there is no separate model.
    """
    cause = "unknown"
    for e in trail:
        if e.stage == "diagnose" and e.decision.startswith("cause="):
            cause = e.decision.split("cause=")[1].split()[0]
            break
    reason = _CAUSE_PLAIN.get(cause, _CAUSE_PLAIN["unknown"])

    scheduled_at: str | None = None
    last_action = ""
    recovered = False
    stopped = False
    for e in trail:
        if isinstance(e.input_state, dict) and e.input_state.get("scheduled_at"):
            scheduled_at = str(e.input_state["scheduled_at"])
        if e.stage == "act" and e.action_taken:
            last_action = e.action_taken or last_action
        if e.stage == "act" and e.outcome and "retry_succeeded" in e.outcome:
            recovered = True
        if e.stage == "act" and e.action_taken == "stop":
            stopped = True

    from .models import CaseState

    state = row.state
    if state == CaseState.ESCALATED.value:
        heading = "Under review by our team"
        next_step = "Our team is looking into this and will reach out if we need anything from you."
    elif recovered:
        heading = "Resolved"
        next_step = "Your payment has been successfully processed. Thank you."
    elif stopped:
        heading = "Closed"
        next_step = "No further automatic payment attempts will be made for this charge."
    elif state == CaseState.FAILED.value:
        heading = "Payment unsuccessful"
        next_step = "We were unable to complete this payment. Please check your payment method."
    elif scheduled_at:
        heading = "We're on it"
        next_step = f"We'll automatically retry this payment on {scheduled_at.replace('T', ' ')[:16]} UTC."
    else:
        heading = "In progress"
        next_step = "We're working to resolve this. No action is needed from you."
    return {"heading": heading, "reason": reason, "next_step": next_step}