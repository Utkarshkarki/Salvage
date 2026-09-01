"""Batch-run demo: ingest a synthetic batch end-to-end and print metrics.

Run with::

    python -m reclaim.batch

Ingests the seeded synthetic batch (valid, duplicate + rejection deliveries)
through the REAL webhook boundary (signature verify, parse, dedupe), then runs
every new case through the pipeline under the configured concurrency cap, then
prints summary metrics + one example of a case that correctly stopped or
escalated instead of looping.

Set ``RECLAIM_FRESH=1`` (or a fresh ``DATABASE_URL``) for a clean metrics run
on repeated executions.
"""

from __future__ import annotations

import logging
import os
import sys
import uuid
from pathlib import Path
from typing import Any

from .config import Settings, get_settings
from .db import Base, Database, init_schema
from .webhook import (
    RazorpayWebhookException,
    ingest_event,
    parse_event,
    verify_signature,
)

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
logger = logging.getLogger("reclaim.batch")

_DEFAULT_FRESH = Path(__file__).resolve().parent / "reclaim_fresh.db"


def _resolve_url(fresh: bool) -> str:
    if fresh:
        # A dedicated per-run file so re-runs always start clean.
        return f"sqlite:///{_DEFAULT_FRESH.parent / f'reclaim_fresh_{uuid.uuid4().hex[:8]}.db'}"
    return get_settings().database_url


def _build_db() -> tuple[Database, bool]:
    fresh = os.environ.get("RECLAIM_FRESH", "0") == "1"
    url = _resolve_url(fresh)
    db_settings = get_settings().model_copy(update={"database_url": url})
    db = Database(db_settings)
    if fresh:
        Base.metadata.drop_all(db.engine)  # clean slate for the demo run
    init_schema(db.engine)
    return db, fresh


def ingest_batch(
    db: Database, batch: Any, settings: Settings
) -> tuple[list[str], int, int]:
    """Ingest a SyntheticBatch through the REAL webhook boundary.

    Returns ``(new_case_ids, duplicate_count, rejected_count)``.

    Shared by the ``python -m reclaim.batch`` CLI and the ``/simulator`` page so
    simulated runs exercise the exact same signature/parse/dedupe boundary logic
    as the real batch — no duplicated pipeline.
    """
    new_ids: list[str] = []
    rejected = 0
    duplicates = 0
    for w in batch.webhooks:
        if not verify_signature(settings.razorpay_webhook_secret, w.raw_body, w.signature):
            rejected += 1
            continue
        try:
            event = parse_event(w.raw_body, event_id_hint=w.event_id)
        except RazorpayWebhookException:
            rejected += 1
            continue
        try:
            _case, is_new, _ = ingest_event(db, event, settings)
        except RazorpayWebhookException:
            rejected += 1
            continue
        if is_new:
            new_ids.append(_case.case_id)
        else:
            duplicates += 1
    return new_ids, duplicates, rejected


def main() -> int:
    settings = get_settings()
    db, _fresh = _build_db()

    from .synthetic import generate_batch

    logger.info("generating synthetic batch (seed=42)...")
    batch = generate_batch(seed=42, webhook_secret=settings.razorpay_webhook_secret)

    new_ids, duplicates, rejected = ingest_batch(db, batch, settings)

    logger.info("ingested: %d new, %d duplicates, %d rejected", len(new_ids), duplicates, rejected)

    from .metrics import compute_metrics
    from .pipeline import run_batch

    run_batch(new_ids, settings=settings, db=db)

    # The hash chain is derived in a single sequential pass AFTER the
    # concurrent writes complete (compute-at-write would race and fork the
    # chain). See audit.finalize_audit_chain / audit_chain module doc.
    from .audit import finalize_audit_chain
    finalize_audit_chain(db)

    metrics = compute_metrics(db, settings)
    _print_report(metrics, db, new_ids)
    db.close()
    return 0


def _print_report(metrics: dict[str, object], db: Database, new_ids: list[str]) -> None:
    from . import repo

    line = "=" * 64
    print(line)
    print("RECLAIM - AI Revenue Recovery: synthetic batch report")
    print(line)
    print(f"Total cases                 : {metrics['total_cases']}")
    print(f"Amount at risk              : Rs.{metrics['amount_at_risk']:,.2f}")
    print(f"Recovered (retry success)   : {metrics['recovered_cases']} cases / "
          f"Rs.{metrics['recovered_amount']:,.2f}")
    rate: float = float(metrics['recovery_rate'])
    print(f"Recovery rate               : {rate * 100:.1f}%")
    print(f"Stopped (deliberate halt)   : {metrics['stopped_cases']}")
    print(f"Escalated (human)           : {metrics['escalated_cases']}")

    # Three separate, unambiguous counters
    print(f"\nDeterministic fallbacks:")
    print(f"  LLM call failures           : {metrics['llm_call_failures']} cases "
          f"(LLM timeout/validation failed)")
    print(f"  Stopping rule overrides     : {metrics['stopping_rule_overrides']} cases "
          f"{metrics['stopping_rule_overrides_by_rule']}")
    print(f"  Stub mode actions           : {metrics['stub_mode_actions']} cases "
          f"(demo/test mode, not a fallback)")

    print(f"\nCases resolved without retry: {metrics['cases_resolved_without_retry']}")
    print(f"  (stopped=deliberate halt + escalated=human review)")

    print("State distribution          :", metrics["state_distribution"])
    print("Root-cause breakdown        :", metrics["cause_breakdown"])

    chosen = next((c for c in new_ids if _resolved_without_retry(db, c)), None)
    print(line)
    if chosen is None:
        print("(no stopped/escalated case found in this batch)")
        return
    print(f"EXAMPLE graceful case {chosen} (agent stopped/escalated instead of retrying):")
    for e in repo.audit_trail(db, chosen):
        tag = " <-- LLM call failed; deterministic fallback used" if e.fallback_triggered else ""
        print(f"  [{e.stage}] {e.decision} -> {e.outcome}{tag}")
        if e.agent_reasoning:
            print(f"        reasoning: {e.agent_reasoning}")


def _resolved_without_retry(db: Database, case_id: str) -> bool:
    """True if the case reached a non-retry terminal path: either a deliberate
    stop (action_taken=stop) or an escalation to human review (ESCALATED)."""
    from . import repo

    for e in repo.audit_trail(db, case_id):
        if e.stage == "act" and (e.action_taken == "stop" or "ESCALATED" in e.outcome):
            return True
    return False


if __name__ == "__main__":
    sys.exit(main())
