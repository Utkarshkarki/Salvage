"""Captured-fixture writing for real Razorpay webhook payloads.

Every payload that passes signature verification at the real ``/webhook/razorpay``
route is written VERBATIM (the exact wire bytes) to a captured-fixtures directory
when capture is enabled:

    {capture_dir}/{event_type}/{event_id}.json

Genuine test-mode payloads recorded here are what make any future replay data
honestly traceable to a real payload shape — a replay must be copied from a
captured fixture, never hand-invented. When the operator confirms a real delivery
has been captured, the fixture is committed to git deliberately (review + redact
sensitive fields first — see ``_sensitive_scan``).

Hard rules:
  * Capture is fault-isolated: it can NEVER break the money flow it observes. Any
    error is logged and swallowed.
  * Capture is opt-in: an empty ``razorpay_webhook_capture_dir`` means no writing
    (the hermetic demo/test default).
  * Sensitive data is WARNED about, never silently auto-redacted — a redacted
    fixture would poison a replay. The operator reviews fixtures before commit.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

logger = logging.getLogger("reclaim.capture")

# PAN-like continuous digit runs (13–19 digits) — the shape of a full card number.
_PAN_RE = re.compile(r"(?<!\d)\d{13,19}(?!\d)")
# A ticky "cvv/cvc" token adjacent to a short number (3–4 digits).
_CVV_RE = re.compile(r"""(?i)(cvv|cvc|cvv2)[^\d]{0,8}(\d{3,4})""")


def _sensitive_scan(raw_body: bytes) -> str | None:
    """Scan a raw payload for card-number-like material; return the first match.

    Returns a short description of the suspicious content, or None if the payload
    looks clean. Razorpay test-mode webhook payloads should NOT carry full card
    numbers (only ``last4``/``bank``/``method``) — a hit here means something
    unexpected slipped through and the operator must review before committing the
    fixture. Warnings only; never auto-redact.
    """
    if not raw_body:
        return None
    try:
        text = raw_body.decode("utf-8", errors="replace")
    except Exception:  # pragma: no cover - decode already has errors="replace"
        return None
    pan = _PAN_RE.search(text)
    if pan is not None:
        return f"card-number-like digit run ({len(pan.group(0))} chars) found"
    cvv = _CVV_RE.search(text)
    if cvv is not None:
        return f"cvv-like token found near a {len(cvv.group(2))}-digit number"
    return None


def capture_webhook(
    capture_dir: str,
    raw_body: bytes,
    *,
    event_id: str | None,
    event_type: str | None = None,
) -> Path | None:
    """Write one signature-passing payload verbatim to the captured-fixtures dir.

    Returns the written ``Path``, or ``None`` when capture is disabled or the
    write failed. Never raises: capture is observational and must never break the
    ingest flow it records. ``event_type`` is the webhook ``event`` value
    (e.g. ``payment.failed``); ``_unparsed`` is used when parse failed after a
    valid signature so even those payloads leave a trace.
    """
    if not capture_dir:
        return None
    if not raw_body:
        return None
    if not event_id:
        # A Sha-256 stem is a safe fallback name if no event id was derivable.
        import hashlib

        event_id = hashlib.sha256(raw_body).hexdigest()[:24]

    suspicious = _sensitive_scan(raw_body)
    if suspicious is not None:
        logger.warning(
            "CAPTURED_SENSITIVE_SCAN event_id=%s: %s — review %s before committing",
            event_id,
            suspicious,
            capture_dir,
        )

    try:
        root = Path(capture_dir)
        subdir = root / (event_type or "_unparsed")
        subdir.mkdir(parents=True, exist_ok=True)
        target = subdir / f"{event_id}.json"
        target.write_bytes(raw_body)
    except OSError as exc:
        logger.warning("CAPTURE_FAILED event_id=%s err=%s", event_id, exc)
        return None
    logger.info("WEBHOOK_CAPTURED event_id=%s type=%s -> %s", event_id, event_type, target)
    return target


def list_captured(capture_dir: str) -> list[Path]:
    """All captured fixture files under ``capture_dir`` (sorted by path)."""
    if not capture_dir:
        return []
    root = Path(capture_dir)
    if not root.exists():
        return []
    return sorted(p for p in root.rglob("*.json") if p.is_file())


def summarize_captured(path: Path) -> dict[str, Any]:
    """Best-effort summary of one captured fixture for the operator's dump view.

    Reads the top-level ``event`` + ``entity.id/order_id/amount/error_code`` and the
    file path, so the operator can verify WHAT actually arrived without opening every
    file. Never raises; missing fields fall back to None/"".
    """
    summary: dict[str, Any] = {"path": str(path), "event": None, "entity_id": None}
    try:
        data = json.loads(path.read_bytes().decode("utf-8", errors="replace"))
        if isinstance(data, dict):
            summary["event"] = data.get("event")
            entity = data.get("entity")
            if isinstance(entity, dict):
                summary["entity_id"] = entity.get("id")
                summary["order_id"] = entity.get("order_id")
                summary["amount"] = entity.get("amount")  # paise
                summary["error_code"] = entity.get("error_code")
    except (OSError, ValueError):
        # Leave the bare path summary — a partial/unparseable fixture is still worth
        # listing; the dump is informational, never a hard failure.
        pass
    return summary