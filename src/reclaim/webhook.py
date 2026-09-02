"""Razorpay webhook boundary: signature verification, schema parsing, dedupe.

Security posture (fintech context):
  * HMAC-SHA256 over the RAW request body, compared constant-time.
  * A missing signature header is a hard reject (401), never a pass-through.
  * The signing secret is required at config load — an empty secret refuses
    to start rather than silently accepting unsigned events.

Dedupe posture:
  * UNIQUE(event_id) is the authoritative guard. Insert-then-catch avoids a
    check-then-insert race under concurrent webhook delivery.
  * A duplicate returns the *existing* case and never re-triggers stages.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from datetime import UTC, datetime
from typing import Any

from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError

from .config import Settings
from .db import Database, RecoveryCaseRow
from .models import CaseState, PaymentRecord, RecoveryCase, WebhookEvent

logger = logging.getLogger("reclaim.webhook")

SIGNATURE_HEADER = "X-Razorpay-Signature"
EVENT_ID_HEADER = "X-Razorpay-Event-Id"


class RazorpayWebhookException(Exception):
    """Rejected at the webhook boundary (signature / parse / schema)."""


# ---------------------------------------------------------------------------
# Signature verification
# ---------------------------------------------------------------------------


def compute_signature(secret: str, raw_body: bytes) -> str:
    """HMAC-SHA256 hex digest of the raw webhook body."""
    return hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()


def verify_signature(secret: str, raw_body: bytes, signature: str | None) -> bool:
    """Constant-time check of a Razorpay webhook signature.

    A missing header is a rejection (returns False), never an acceptance.
    """
    if not secret:
        raise RazorpayWebhookException("webhook secret not configured; refusing to verify")
    if not signature:
        logger.warning("WEBHOOK_REJECTED reason=missing_signature_header")
        return False
    expected = compute_signature(secret, raw_body)
    try:
        return hmac.compare_digest(expected, signature)
    except (TypeError, ValueError):
        logger.warning("WEBHOOK_REJECTED reason=malformed_signature_header")
        return False


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def parse_event(raw_body: bytes, event_id_hint: str | None = None) -> WebhookEvent:
    """Parse + validate a Razorpay webhook body. Raises on any schema issue."""
    try:
        data = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RazorpayWebhookException(f"malformed JSON body: {exc}") from exc

    if not isinstance(data, dict):
        raise RazorpayWebhookException("webhook body must be a JSON object")

    event_type = data.get("event")
    event_id = event_id_hint or _deterministic_event_id(raw_body, data)

    try:
        # model_validate lets Pydantic coerce + reject an unknown event type.
        return WebhookEvent.model_validate(
            {"event_id": event_id, "type": event_type, "payload": data}
        )
    except ValidationError as exc:
        raise RazorpayWebhookException(f"webhook schema validation failed: {exc}") from exc


def _deterministic_event_id(raw_body: bytes, data: dict[str, Any]) -> str:
    """Derive a stable event id from the body when the delivery has no id.

    Identical bodies hash identically, so replays dedupe even without the
    event-id header — a deliberate safety property.
    """
    entity = data.get("entity") or {}
    canonical = json.dumps(
        {
            "entity_id": entity.get("id"),
            "event": data.get("event"),
            "created_at": entity.get("created_at"),
        },
        sort_keys=True,
    ).encode("utf-8")
    digest = hashlib.sha256(canonical).hexdigest()[:24]
    return f"evt_{digest}"


# ---------------------------------------------------------------------------
# Case creation + dedupe
# ---------------------------------------------------------------------------


def event_to_case(event: WebhookEvent) -> RecoveryCase:
    """Map a validated webhook event onto the RecoveryCase schema.

    Derives attempt_number from the entity when present; defaults to 1.
    """
    entity = event.payload.get("entity") or {}
    raw_attempt = entity.get("attempt_number") or entity.get("attempts") or 1
    try:
        attempt = max(1, int(raw_attempt))
    except (TypeError, ValueError):
        attempt = 1

    # Anchor the case's payment history on this failed attempt's timestamp so
    # ``days_since_last_attempt`` (and thus the 24h cooldown rule) reflects the
    # real retry gap instead of always being 0 for a fresh ingest.
    history: list[PaymentRecord] = []
    created_ts = entity.get("created_at")
    if created_ts is not None:
        try:
            attempted_at = datetime.fromtimestamp(int(created_ts), tz=UTC)
        except (TypeError, ValueError, OSError):
            attempted_at = None
        if attempted_at is not None:
            history.append(
                PaymentRecord(status="failed", amount=event.amount(), attempted_at=attempted_at)
            )

    try:
        return RecoveryCase(
            case_id=event.case_id(),
            event_id=event.event_id,
            customer_id=event.customer_id(),
            subscription_id=event.subscription_id(),
            failure_reason=event.failure_reason(),
            amount=event.amount(),
            attempt_number=attempt,
            customer_tier="standard",  # enriched from CRM in the ingest pipeline
            payment_history=history,
        )
    except ValidationError as exc:
        # ZERO-HALO: an event that cannot map to a valid case is rejected.
        raise RazorpayWebhookException(f"unmappable event payload: {exc}") from exc


def _payment_history_to_json(history: list[PaymentRecord]) -> list[dict[str, Any]]:
    """SQLite's JSON column cannot store datetime — serialize to ISO strings."""
    return [
        {
            "status": p.status,
            "amount": p.amount,
            "attempted_at": p.attempted_at.isoformat(),
        }
        for p in history
    ]


def _payment_history_from_json(history: list[dict[str, Any]]) -> list[PaymentRecord]:
    out: list[PaymentRecord] = []
    for h in history or []:
        raw = dict(h)
        iso = raw.get("attempted_at")
        if isinstance(iso, str):
            raw["attempted_at"] = datetime.fromisoformat(iso)
        out.append(PaymentRecord(**raw))
    return out


def _row_to_case(row: Any) -> RecoveryCase:
    """Rebuild a RecoveryCase view from a persisted row (read path)."""
    history = _payment_history_from_json(row.payment_history or [])
    return RecoveryCase(
        case_id=row.case_id,
        event_id=row.event_id,
        customer_id=row.customer_id,
        subscription_id=row.subscription_id,
        failure_reason=row.failure_reason,
        amount=row.amount,
        attempt_number=row.attempt_number,
        customer_tier=row.customer_tier,
        payment_history=history,
        state=CaseState(row.state),
        created_at=row.created_at,
    )


def ingest_event(db: Database, event: WebhookEvent, settings: Settings) -> tuple[RecoveryCase, bool, int]:
    """Persist a verified event as a new RecoveryCase (state=INGESTED).

    Returns ``(case, is_new, row_pk)``. A duplicate event_id returns the
    existing case with ``is_new=False`` and never re-triggers stages.
    """
    # Fast path: already present.
    with db.create_session() as session:
        existing = session.query(RecoveryCaseRow).filter_by(event_id=event.event_id).first()
        if existing is not None:
            logger.info(
                "DUPLICATE_DROPPED event_id=%s case_id=%s",
                event.event_id,
                existing.case_id,
            )
            return _row_to_case(existing), False, int(existing.id)

    case = event_to_case(event)
    from .db import utcnow  # local import avoids an import cycle at module load

    row = RecoveryCaseRow(
        case_id=case.case_id,
        event_id=case.event_id,
        customer_id=case.customer_id,
        subscription_id=case.subscription_id,
        failure_reason=case.failure_reason,
        failure_reason_raw=case.failure_reason,
        amount=case.amount,
        attempt_number=case.attempt_number,
        customer_tier=case.customer_tier,
        payment_history=_payment_history_to_json(case.payment_history),
        state=case.state.value,
        created_at=utcnow(),
    )
    with db.create_session() as session:
        session.add(row)
        try:
            session.commit()
            session.refresh(row)
            logger.info(
                "CASE_INGESTED case_id=%s event_id=%s amount=%.2f attempt=%d",
                row.case_id,
                row.event_id,
                row.amount,
                row.attempt_number,
            )
            return case, True, int(row.id)
        except IntegrityError as exc:
            # Lost a race against a concurrent delivery of the same event.
            session.rollback()
            with db.create_session() as session2:
                existing = (
                    session2.query(RecoveryCaseRow)
                    .filter_by(event_id=event.event_id)
                    .first()
                )
            if existing is None:
                # The INSERT collided on a DIFFERENT uniqueness constraint:
                # the same subscription (case_id UNIQUE) arriving as a NEW
                # event_id is a second failure for an already-tracked
                # subscription. Reclaim models one recovery case per
                # subscription (single-cycle), so this is a deliberate,
                # graceful boundary rejection — never a 500 crash. The API
                # layer maps RazorpayWebhookException -> 422.
                with db.create_session() as session3:
                    by_case = (
                        session3.query(RecoveryCaseRow)
                        .filter_by(case_id=case.case_id)
                        .first()
                    )
                if by_case is not None:
                    raise RazorpayWebhookException(
                        f"case_id {case.case_id} is already tracked (event "
                        f"{by_case.event_id}); a new event for an existing "
                        "subscription is a fresh attempt-cycle, which the "
                        "single-case-per-subscription model does not ingest"
                    ) from exc
                raise RazorpayWebhookException(
                    f"integrity error without existing row: {exc}"
                ) from exc
            return _row_to_case(existing), False, int(existing.id)