"""Verification-only Razorpay integrations (Subscriptions + Settlements).

These are NON-BLOCKING, best-effort, VERIFICATION-ONLY lookups against real
Razorpay test-mode endpoints. They NEVER block or reverse an action and NEVER
change a case's terminal state — a verification failure merely records a
``verify`` audit entry so an auditor can see the external state (subscription
status / settlement status) at the time of the action. Every external call is
fault-isolated: it can never crash the money flow it is observing.

  * ``verify_subscription_status``  (A2): confirm a subscription's current
    status (e.g. mandate revoked).
  * ``verify_settlement_reconciliation`` (A3): after a ``retry_now`` recovery,
    confirm the resulting settlement state so "recovered" is corroborated.

Both are gated by ``settings.verification_enabled`` (off => silent, hermetic)
and ``act_mode`` (stub => deterministic placeholders, no network).
"""

from __future__ import annotations

import logging

from . import repo
from .audit import write_audit
from .config import Settings
from .db import Database
from .models import AuditLogEntry
from .razorpay_client import RazorpayClient

logger = logging.getLogger("reclaim.verify")


def _record(db: Database, case_id: str, stage: str, detail: str, outcome: str) -> None:
    """Append one verification audit entry (best-effort, never raises)."""
    write_audit(
        db,
        AuditLogEntry(
            case_id=case_id,
            stage=stage,
            agent_reasoning="verification-only external lookup (never blocks/reverses)",
            input_state={"detail": detail},
            decision=stage,
            action_taken=None,
            outcome=outcome,
            fallback_triggered=False,
        ),
    )


def verify_subscription_status(
    db: Database, settings: Settings, case_id: str
) -> dict[str, object] | None:
    """Best-effort subscription status verification. Returns the raw response
    dict (or None on failure/disabled). Never raises."""
    if not settings.verification_enabled:
        return None
    row = repo.get_case_row(db, case_id)
    if row is None:
        return None
    try:
        client = RazorpayClient(settings)
        data = client.subscription_status(row.subscription_id)
        status = data.get("status", "unknown")
        _record(db, case_id, "verify_subscription",
                f"subscription={row.subscription_id} status={status}",
                f"VERIFY subscription={status}")
        logger.info("VERIFY_SUBSCRIPTION case=%s status=%s", case_id, status)
        return data
    except Exception as exc:  # fault-isolated: never crash the flow
        logger.error("VERIFY_SUBSCRIPTION_FAILED case=%s err=%s", case_id, exc)
        _record(db, case_id, "verify_subscription", str(exc),
                f"VERIFY_FAILED {type(exc).__name__}")
        return None


def verify_settlement_reconciliation(
    db: Database, settings: Settings, case_id: str
) -> dict[str, object] | None:
    """Best-effort settlement reconciliation after a retry recovery. Returns
    the raw response dict (or None on failure/disabled). Never raises."""
    if not settings.verification_enabled:
        return None
    row = repo.get_case_row(db, case_id)
    if row is None:
        return None
    try:
        client = RazorpayClient(settings)
        data = client.settlement_reconciliation(
            f"settle_{row.subscription_id}_{row.attempt_number}"
        )
        status = data.get("status", "unknown")
        _record(db, case_id, "verify_settlement",
                f"settlement={data.get('settlement_id', '?')} status={status}",
                f"VERIFY settlement={status}")
        logger.info("VERIFY_SETTLEMENT case=%s status=%s", case_id, status)
        return data
    except Exception as exc:  # fault-isolated: never crash the flow
        logger.error("VERIFY_SETTLEMENT_FAILED case=%s err=%s", case_id, exc)
        _record(db, case_id, "verify_settlement", str(exc),
                f"VERIFY_FAILED {type(exc).__name__}")
        return None
