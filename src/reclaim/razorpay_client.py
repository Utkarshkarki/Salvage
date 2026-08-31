"""Razorpay test-mode client (stub or live) with idempotency keys.

STUB mode (the demo default): logs the would-be HTTP call and returns success,
so no credentials are needed and the whole pipeline is hermetic.
LIVE mode: posts to the configured Razorpay retry endpoint with test-mode keys
and an idempotency key header so a duplicate call can never double-charge.

The live endpoint path is deliberately NOT hardcoded — Razorpay's exact
payment-retry route must be confirmed against current docs (it changes and
varies by API version). If ``razorpay_retry_path`` is empty, live mode refuses
to run rather than guess an API shape (ZERO-HALO: never invent a wire format).
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from .config import Settings, get_settings

logger = logging.getLogger("reclaim.razorpay")

IDEMPOTENCY_HEADER = "X-Razorpay-Idempotency-Key"


def idempotency_key(case_id: str, attempt_number: int, action: str) -> str:
    """Deterministic key derived from case + attempt + action.

    The SAME (case, attempt, action) always yields the SAME key, so a
    duplicated Act call is a no-op — the ledger's UNIQUE constraint (see
    :class:`reclaim.db.ExecutedActionRow`) backstops this at the DB level.
    """
    return f"reclaim:{case_id}:{attempt_number}:{action}"


class RazorpayClient:
    """Thin, defensively-typed Razorpay test-mode client."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def _require_live(self) -> None:
        self.settings.require_live_credentials()
        if not self.settings.razorpay_retry_path:
            raise RuntimeError(
                "ACT_MODE=live requires RAZORPAY_RETRY_PATH set to Razorpay's current "
                "payment/subscription retry endpoint. Refusing to guess the API shape."
            )

    def subscription_status(self, subscription_id: str) -> dict[str, Any]:
        """Verify a subscription's current status (e.g. whether mandate revoked).

        VERIFICATION-ONLY: a read against the configured Subscriptions endpoint.
        Never blocks or reverses an action. Raises on any failure so the caller
        (verify.py) can fault-isolate and record it.
        """
        if self.settings.act_mode == "stub":
            return {"subscription_id": subscription_id, "status": "active",
                    "actor": "stub"}
        self._require_live()
        if not self.settings.razorpay_subscription_path:
            raise RuntimeError(
                "ACT_MODE=live subscription verification requires "
                "RAZORPAY_SUBSCRIPTION_PATH set to Razorpay's Subscriptions "
                "endpoint. Refusing to guess the API shape."
            )
        url = (f"{self.settings.razorpay_base_url.rstrip('/')}"
               f"{self.settings.razorpay_subscription_path.format(subscription_id=subscription_id)}")
        resp = httpx.get(url, auth=(self.settings.razorpay_key_id,
                                    self.settings.razorpay_key_secret), timeout=15.0)
        resp.raise_for_status()
        return resp.json()

    def settlement_reconciliation(self, settlement_id: str) -> dict[str, Any]:
        """Reconcile a settlement after a retry_now recovery.

        VERIFICATION-ONLY read of the settlement state — it never blocks,
        reverses, or re-dispatches a payment. Used to confirm that money
        recovered actually settled.
        """
        if self.settings.act_mode == "stub":
            return {"settlement_id": settlement_id, "status": "settled",
                    "actor": "stub"}
        self._require_live()
        if not self.settings.razorpay_settlement_path:
            raise RuntimeError(
                "ACT_MODE=live settlement reconciliation requires "
                "RAZORPAY_SETTLEMENT_PATH set to Razorpay's Settlement endpoint. "
                "Refusing to guess the API shape."
            )
        url = (f"{self.settings.razorpay_base_url.rstrip('/')}"
               f"{self.settings.razorpay_settlement_path.format(settlement_id=settlement_id)}")
        resp = httpx.get(url, auth=(self.settings.razorpay_key_id,
                                    self.settings.razorpay_key_secret), timeout=15.0)
        resp.raise_for_status()
        return resp.json()

    def retry_payment(
        self,
        *,
        case_id: str,
        subscription_id: str,
        amount: float,
        attempt_number: int,
    ) -> bool:
        """Trigger a payment/subscription retry. Returns True on success.

        The retry is keyed by ``idempotency_key(case_id, attempt, 'retry_now')``
        so a replay can never double-charge. Any transport/HTTP error surfaces
        as this method raising; the caller (Act layer) turns that into FAILED
        after bounded retries.
        """
        key = idempotency_key(case_id, attempt_number, "retry_now")

        if self.settings.act_mode == "stub":
            logger.info(
                "RAZORPAY_RETRY_STUB case=%s sub=%s attempt=%d amount=%.2f "
                "idempotency_key=%s -> (would POST %s with %s: %s)",
                case_id, subscription_id, attempt_number, amount, key,
                self.settings.razorpay_retry_path or "<retry-endpoint>",
                IDEMPOTENCY_HEADER, key,
            )
            return True

        self._require_live()
        url = f"{self.settings.razorpay_base_url.rstrip('/')}{self.settings.razorpay_retry_path}"
        headers = {IDEMPOTENCY_HEADER: key}
        auth = (self.settings.razorpay_key_id, self.settings.razorpay_key_secret)
        # NOTE: body shape intentionally deferred to the configured endpoint's
        # contract; we POST the subscription id so actual retry semantics stay
        # razorpay-documented rather than guessed here.
        body: dict[str, Any] = {"subscription_id": subscription_id}
        resp = httpx.post(url, headers=headers, auth=auth, json=body, timeout=15.0)
        resp.raise_for_status()
        logger.info("RAZORPAY_RETRY_OK status=%s case=%s", resp.status_code, case_id)
        return True
