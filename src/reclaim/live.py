"""Live Razorpay test-mode failure generator + captured-fixture dump.

This is the tool that makes Reclaim's webhook data REAL, not synthetic:

  * ``python -m reclaim.live create``  (or ``reclaim-live create``)
      Creates a real test-mode Payment Link via the Razorpay Payments API and
      prints its ``short_url``. A human (the operator) opens the link in an
      incognito window and completes checkout with a Razorpay-tested
      *error-simulation* card, deliberately producing a ``payment.failed``
      webhook delivered through Reclaim's real public endpoint. That case is
      ingested with provenance=``live``.

  * ``python -m reclaim.live dump``  (or ``reclaim-live dump``)
      Lists every captured webhook fixture under
      ``RAZORPAY_WEBHOOK_CAPTURE_DIR`` (e.g. ``fixtures/captured``) with a
      per-file summary, so the operator can verify what actually arrived after
      triggering a failure.

This module is NEVER hermetic-testable by design: it requires the operator's
live Razorpay test session (keys + a human checkout). No test depends on it.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

import httpx

from .capture import list_captured, summarize_captured
from .config import Settings, get_settings

# Amount in PAISE for the test Payment Link — ₹499: above the R7 economic floor
# (₹100) so the case is retry-eligible, below the R2 escalation threshold (₹5000)
# so it flows to the normal retry path rather than straight to a human.
_TEST_AMOUNT_PAISE = 49900
_TEST_DESCRIPTION = "Reclaim AI Buildathon — live provenance test"

# Razorpay's documented error-simulation test cards are exercised at checkout by
# the human operator. We deliberately hardcode NO card numbers here — see the docs
# link printed by `create` — so neither this repo nor this tool can drift from
# Razorpay's actual simulation list.
TEST_MODE_CARDS_DOC_URL = "https://razorpay.com/docs/payments/payments/test-card-details/"


# ---------------------------------------------------------------------------
# Payment Link creation
# ---------------------------------------------------------------------------


def create_test_payment_link(settings: Settings) -> dict[str, Any]:
    """Create one real test-mode Payment Link; return the API response.

    ZERO-HALO by design: refuses to POST without test-mode credentials
    (``require_live_credentials``) and without a confirmed
    ``razorpay_payment_link_path`` — this code never guesses a wire format.
    """
    settings.require_live_credentials()
    if not settings.razorpay_payment_link_path:
        raise RuntimeError(
            "RAZORPAY_PAYMENT_LINK_PATH is not set; set it to Razorpay's documented "
            "Payment Links endpoint (e.g. /payment_links) before creating a link. "
            "Refusing to guess the API shape."
        )

    url = (
        f"{settings.razorpay_base_url.rstrip('/')}"
        f"{settings.razorpay_payment_link_path}"
    )
    body = {
        "amount": _TEST_AMOUNT_PAISE,
        "currency": "INR",
        "description": _TEST_DESCRIPTION,
        "accept_partial": False,
        "notes": {
            "project": "reclaim-ai-buildathon",
            "purpose": "trigger a genuine payment.failed via error-simulation card",
        },
    }
    resp = httpx.post(
        url,
        auth=(settings.razorpay_key_id, settings.razorpay_key_secret),
        json=body,
        timeout=15.0,
    )
    resp.raise_for_status()
    return resp.json()


def _fmt_capture_dir(settings: Settings) -> str:
    return settings.razorpay_webhook_capture_dir or "(capture disabled — set RAZORPAY_WEBHOOK_CAPTURE_DIR=fixtures/captured)"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _reconfigure_stdout() -> None:
    """Windows cp1252 consoles crash printing ₹ / ✓ — force UTF-8 (same pattern as
    the other Phase 5 CLIs)."""
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def _cmd_create() -> int:
    settings = get_settings()
    _reconfigure_stdout()
    link = create_test_payment_link(settings)
    short_url = link.get("short_url") or "(no short_url in response)"
    link_id = link.get("id") or "(no id in response)"
    capture_dir = _fmt_capture_dir(settings)

    print("=" * 68)
    print("RECLAIM — LIVE TEST-MODE FAILURE")
    print("=" * 68)
    print(f"Payment Link id       : {link_id}")
    print(f"Checkout (short_url)  : {short_url}")
    print()
    print("NEXT STEP (you, the operator):")
    print("  1. Open the checkout URL above in a PRIVATE/INCOGNITO browser window.")
    print("  2. Complete checkout with a Razorpay-documented ERROR-SIMULATION test")
    print("     card so the payment is intentionally DECLINED.")
    print(f"     Cards: {TEST_MODE_CARDS_DOC_URL}")
    print("  3. Razorpay delivers payment.failed to <zrok-url>/webhook/razorpay;")
    print("     Reclaim captures it and ingests it as provenance=live.")
    print()
    print("WATCH FOR:")
    print(f"  • captured fixture  : {capture_dir}/payment.failed/<event_id>.json")
    print("  • case (provenance=live) on the dashboard or GET /cases/<case_id>")
    print("  • confirm with `python -m reclaim.live dump`")
    print()
    print("IMPORTANT: the webhook endpoint must be publicly reachable (zrok) and")
    print("registered under Settings → Webhooks with your own RAZORPAY_WEBHOOK_SECRET,")
    print("subscribed to payment.failed (see README 'Live Razorpay Integration').")
    return 0


def _cmd_dump() -> int:
    settings = get_settings()
    _reconfigure_stdout()
    capture_dir = settings.razorpay_webhook_capture_dir
    fixtures = list_captured(capture_dir)

    print("=" * 68)
    print("RECLAIM — CAPTURED WEBHOOK FIXTURES")
    print("=" * 68)
    if not fixtures:
        print("No captured fixtures found.")
        if not capture_dir:
            print("Capture is DISABLED — set RAZORPAY_WEBHOOK_CAPTURE_DIR=fixtures/captured")
            print("so signature-passing deliveries are written for traceability.")
        else:
            print(f"Nothing captured under {capture_dir} yet.")
        return 0

    print(f"{len(fixtures)} fixture(s) under {capture_dir}:\n")
    for path in fixtures:
        summary = summarize_captured(path)
        print(f"  {summary['path']}")
        print(
            f"      {summary.get('event') or '?'}  entity_id={summary.get('entity_id') or '?'}"
        )
        extra = []
        if summary.get("order_id"):
            extra.append(f"order_id={summary['order_id']}")
        if summary.get("amount") is not None:
            extra.append(f"amount={summary['amount']} paise")
        if summary.get("error_code"):
            extra.append(f"error_code={summary['error_code']}")
        if extra:
            print("      " + "  ".join(extra))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="reclaim-live",
        description="Create a real test-mode Razorpay failure and inspect what arrived.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_create = sub.add_parser("create", help="create a test Payment Link + print checkout URL")
    p_create.set_defaults(func=_cmd_create)

    p_dump = sub.add_parser("dump", help="list captured webhook fixtures")
    p_dump.set_defaults(func=_cmd_dump)

    args = parser.parse_args(argv)
    return args.func()


if __name__ == "__main__":
    sys.exit(main())