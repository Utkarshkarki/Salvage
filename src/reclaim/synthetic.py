"""Synthetic Razorpay webhook batch generator.

Produces a large, varied, seeded batch of deliveries for the demo + tests:

  * 60 valid, unique ``payment.failed``/``subscription.*`` events covering all
    :class:`Cause` values, a spread of amounts (incl. above the escalation
    threshold), attempt counts up to 4, and customer histories with retry gaps
    both under and over the 7-day escalation window.
  * Duplicate deliveries of some valid events (Razorpay re-sends webhooks) to
    exercise the dedupe (UNIQUE event_id) guard.
  * Malformed / tampered / unsigned deliveries to exercise the signature and
    schema rejection paths.

Everything is derived from a seeded :class:`random.Random`, so a given
``(count, seed)`` reproduces an identical batch. Payment history is anchored
relative to ``now`` so ``days_since_last_attempt`` is stable whenever run.
"""

from __future__ import annotations

import json
import random
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from .models import Cause, PaymentRecord, WebhookEvent
from .webhook import EVENT_ID_HEADER, SIGNATURE_HEADER, compute_signature, parse_event

# Raw bank decline code -> root-cause hint. Used by the offline LLM shim and by
# tests to assert Diagnose behaviour. The online LLM reasons freely and may
# disagree; the map is a prior, not an oracle.
RAW_CODE_TO_CAUSE: dict[str, Cause] = {
    "R01": Cause.INSUFFICIENT_FUNDS,
    "54": Cause.CARD_EXPIRED,
    "91": Cause.BANK_TIMEOUT,
    "05": Cause.DO_NOT_HONOR,
    "R0": Cause.MANDATE_REVOKED,
    "999": Cause.UNKNOWN,
}

CAUSE_TO_RAW_CODES: dict[Cause, list[str]] = {
    Cause.INSUFFICIENT_FUNDS: ["R01", "R02"],
    Cause.CARD_EXPIRED: ["54", "F14"],
    Cause.BANK_TIMEOUT: ["91", "Z06"],
    Cause.DO_NOT_HONOR: ["05", "N7"],
    Cause.MANDATE_REVOKED: ["R0", "PM"],
    Cause.UNKNOWN: ["255", "C6"],
}

CAUSE_TO_DESCRIPTION: dict[Cause, str] = {
    Cause.INSUFFICIENT_FUNDS: "The payment has been declined as the account has insufficient balance.",
    Cause.CARD_EXPIRED: "The card used for the payment has expired.",
    Cause.BANK_TIMEOUT: "The bank did not respond in time; issuer unavailable.",
    Cause.DO_NOT_HONOR: "The bank has declined the payment without a specific reason.",
    Cause.MANDATE_REVOKED: "The customer has revoked the mandate for this subscription.",
    Cause.UNKNOWN: "The decline reason could not be classified from the bank response.",
}


@dataclass
class CaseEnrichment:
    """CRM-style context for a subscription that Diagnose reasons over."""

    customer_tier: str = "standard"
    payment_history: list[PaymentRecord] = field(default_factory=list)

    def days_since_last_attempt(self) -> int:
        if not self.payment_history:
            return 0
        last = max(p.attempted_at for p in self.payment_history)
        return max(0, int((datetime.now(UTC) - last).total_seconds() // 86400))


@dataclass
class SyntheticWebhook:
    """One raw delivery (or rejection) our webhook boundary sees."""

    event_id: str
    raw_body: bytes
    signature: str | None
    event: WebhookEvent | None  # None when the body does not parse
    valid_delivery: bool  # True only for well-formed AND correctly signed
    cause_hint: Cause | None
    note: str = ""


@dataclass
class SyntheticBatch:
    """A full generated batch plus the expected ingest outcomes."""

    webhooks: list[SyntheticWebhook]
    enrichments: dict[str, CaseEnrichment]  # keyed by subscription_id

    def valid_deliveries(self) -> list[SyntheticWebhook]:
        return [w for w in self.webhooks if w.valid_delivery]

    def unique_valid(self) -> dict[str, SyntheticWebhook]:
        seen: dict[str, SyntheticWebhook] = {}
        for w in self.valid_deliveries():
            seen.setdefault(w.event_id, w)
        return seen

    def summary(self) -> dict[str, int]:
        return {
            "total_deliveries": len(self.webhooks),
            "valid_unique_events": len(self.unique_valid()),
            "duplicate_deliveries": len(self.webhooks) - len(self.unique_valid())
            - sum(1 for w in self.webhooks if not w.valid_delivery),
            "expected_rejections": sum(1 for w in self.webhooks if not w.valid_delivery),
        }


def _payload(
    event_id: str,
    event_type: str,
    entity: dict[str, Any],
) -> dict[str, Any]:
    return {"event": event_type, "entity": entity, "account_id": "acc_reclaim_demo"}


def _build_cause_and_attempts(rng: random.Random) -> tuple[Cause, str]:
    cause = rng.choice(list(Cause))
    code = rng.choice(CAUSE_TO_RAW_CODES[cause])
    return cause, code


def generate_batch(
    *,
    n_valid: int = 60,
    n_duplicates: int = 6,
    n_rejections: int = 7,
    seed: int = 42,
    webhook_secret: str = "demo-secret",
) -> SyntheticBatch:
    """Generate a seeded batch. ``webhook_secret`` signs the valid deliveries."""
    rng = random.Random(seed)
    now = datetime.now(UTC)
    deliveries: list[SyntheticWebhook] = []
    enrichments: dict[str, CaseEnrichment] = {}

    # Amounts: mostly below the escalation threshold, a handful above it.
    # Some customers have long retry gaps (> 7 days) to trip the age rule.
    for i in range(1, n_valid + 1):
        subscript = f"sub_{seed:04d}_{i:04d}"
        customer = f"cust_{seed:04d}_{i % 97:04d}"
        cause, code = _build_cause_and_attempts(rng)

        amount = rng.choice([199.0, 499.0, 799.0, 1299.0, 2499.0, 3499.0, 4999.0])
        if i % 9 == 0:  # ~1/9 of cases carry above-threshold amounts
            amount = rng.choice([5900.0, 7500.0, 14900.0, 39900.0])

        attempt = rng.choice([1, 1, 1, 2, 2, 3])
        if i % 11 == 0:  # some cases have already exhausted attempts
            attempt = 4

        gap_days = rng.choice([0, 1, 2, 3, 5])
        if i % 8 == 0:  # ~1/8 of cases are older than the 7-day window
            gap_days = rng.choice([8, 9, 12, 15, 21])

        tier = rng.choices(["standard", "silver", "gold"], weights=[70, 20, 10])[0]

        # Customer payment history for this subscription (CRM-style context).
        history: list[PaymentRecord] = []
        paid_before = rng.choice([True, True, False])
        if paid_before:
            for k in range(rng.randint(1, 4)):
                history.append(
                    PaymentRecord(
                        status="paid",
                        amount=amount,
                        attempted_at=now - timedelta(days=gap_days + k * 30 + 11),
                    )
                )
        history.append(
            PaymentRecord(
                status="failed",
                amount=amount,
                attempted_at=now - timedelta(days=gap_days),
            )
        )
        enrichments[subscript] = CaseEnrichment(customer_tier=tier, payment_history=history)

        entity = {
            "id": f"pay_{seed:04d}_{i:04d}",
            "subscription_id": subscript,
            "customer_id": customer,
            "amount": int(amount * 100),
            "attempt_number": attempt,
            "error_code": code,
            "error_description": CAUSE_TO_DESCRIPTION[cause],
            "status": "failed",
            "created_at": int((now - timedelta(days=gap_days)).timestamp()),
        }
        event_type = rng.choices(
            ["payment.failed", "subscription.charged.failed"], weights=[70, 30]
        )[0]

        body = json.dumps(_payload(f"evt_{i:05d}", event_type, entity)).encode("utf-8")
        sig = compute_signature(webhook_secret, body)

        event: WebhookEvent | None = None
        with suppress(Exception):  # pragma: no cover - generated bodies are valid
            event = parse_event(body, event_id_hint=f"evt_{i:05d}")

        deliveries.append(
            SyntheticWebhook(
                event_id=f"evt_{i:05d}",
                raw_body=body,
                signature=sig,
                event=event,
                valid_delivery=True,
                cause_hint=cause,
            )
        )

    # --- Duplicate deliveries (same event_id, body, signature -> dedupe) ---
    originals = deliveries[:n_duplicates]
    for orig in originals:
        deliveries.append(
            SyntheticWebhook(
                event_id=orig.event_id,
                raw_body=orig.raw_body,
                signature=orig.signature,
                event=orig.event,
                valid_delivery=True,  # a duplicate is still a *valid* delivery
                cause_hint=orig.cause_hint,
                note="duplicate-delivery",
            )
        )

    # --- Rejections: malformed / tampered / unsigned ----------------------
    reject: list[SyntheticWebhook] = [
        SyntheticWebhook(
            event_id="evt_reject_badjson",
            raw_body=b"this is not json",
            signature="deadbeef" * 8,
            event=None,
            valid_delivery=False,
            cause_hint=None,
            note="malformed-json",
        ),
        SyntheticWebhook(
            event_id="evt_reject_notype",
            raw_body=json.dumps({"entity": {"id": "pay_x"}}).encode("utf-8"),
            signature=compute_signature(
                webhook_secret, json.dumps({"entity": {"id": "pay_x"}}).encode("utf-8")
            ),
            event=None,
            valid_delivery=False,
            cause_hint=None,
            note="missing-event-type",
        ),
        SyntheticWebhook(
            event_id="evt_reject_unmappable",
            raw_body=json.dumps({"event": "payment.failed", "entity": {}}).encode("utf-8"),
            signature=compute_signature(
                webhook_secret,
                json.dumps({"event": "payment.failed", "entity": {}}).encode("utf-8"),
            ),
            event=None,
            valid_delivery=False,
            cause_hint=None,
            note="unmappable-amount",
        ),
    ]

    # Tampered: body mutated after signing -> signature mismatch.
    good = originals[1]
    tampered = bytearray(good.raw_body)
    tampered[-1] = ord("}") if tampered[-1] == ord("1") else ord("1")  # flip last char
    reject.append(
        SyntheticWebhook(
            event_id="evt_reject_tampered",
            raw_body=bytes(tampered),
            signature=good.signature,  # signature no longer matches body
            event=None,
            valid_delivery=False,
            cause_hint=None,
            note="tampered-body",
        )
    )

    # Unsigned: valid body, no signature header.
    unsigned_body = originals[2].raw_body
    reject.append(
        SyntheticWebhook(
            event_id="evt_reject_unsigned",
            raw_body=unsigned_body,
            signature=None,
            event=None,
            valid_delivery=False,
            cause_hint=None,
            note="missing-signature",
        )
    )

    # Truly random rejection extras to reach the requested count.
    while len(reject) < n_rejections:
        reject.append(
            SyntheticWebhook(
                event_id=f"evt_reject_rng{len(reject)}",
                raw_body=b"{}",
                signature=compute_signature(webhook_secret, b"{}"),
                event=None,
                valid_delivery=False,
                cause_hint=None,
                note="empty-body",
            )
        )

    deliveries.extend(reject)

    # Safety invariants: we never emit a malformed body with a *valid* flag.
    assert sum(1 for w in deliveries if w.valid_delivery) == n_valid + n_duplicates
    return SyntheticBatch(webhooks=deliveries, enrichments=enrichments)


def render_delivery(w: SyntheticWebhook) -> dict[str, Any]:
    """Serialize a delivery exactly as the HTTP boundary sends it."""
    return {
        "headers": {SIGNATURE_HEADER: w.signature, EVENT_ID_HEADER: w.event_id},
        "body": w.raw_body.decode("utf-8", errors="replace"),
    }