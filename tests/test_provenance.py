"""Provenance tier: live/replay cases are tagged at ingest, never silently blended.

Pins the Phase 6.1 provenance contract:
  * the real webhook boundary (ingest_event WITHOUT an explicit provenance) tags
    cases ``live`` — the honest default, since only genuine external deliveries
    reach that path;
  * every synthetic batch path (ingest_batch / baseline / robustness) passes
    ``provenance=REPLAY`` explicitly — a synthetic case can never masquerade as
    live by forgetting one argument;
  * ``mocked`` is reserved for evaluation-only fake clients and has no production
    ingest path;
  * capture writes every signature-passing payload verbatim (fixtures), with a
    loud warning if card-number-like material appears (never auto-redacted).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from reclaim.config import Settings
from reclaim.db import Database, RecoveryCaseRow
from reclaim.models import Cause, Provenance, WebhookEvent
from reclaim.webhook import (
    compute_signature,
    event_to_case,
    ingest_event,
    parse_event,
    verify_signature,
)


def _webhook_body(*, sub: str = "sub_p", error_code: str = "R01") -> bytes:
    return json.dumps(
        {
            "event": "payment.failed",
            "entity": {
                "id": f"pay_{sub}",
                "subscription_id": sub,
                "customer_id": f"cust_{sub}",
                "amount": 20000,
                "attempt_number": 1,
                "error_code": error_code,
                "error_description": "declined",
                "status": "failed",
                "created_at": int((datetime.now(UTC) - timedelta(days=3)).timestamp()),
            },
        }
    ).encode("utf-8")


def _ingest(db: Database, settings: Settings, *, sub: str, event_id: str, provenance=None):
    body = _webhook_body(sub=sub)
    sig = compute_signature(settings.razorpay_webhook_secret, body)
    assert verify_signature(settings.razorpay_webhook_secret, body, sig)
    event = parse_event(body, event_id_hint=event_id)
    return ingest_event(
        db, event, settings, **({"provenance": provenance} if provenance is not None else {})
    )


def _row(db: Database, event_id: str) -> RecoveryCaseRow:
    with db.create_session() as session:
        row = session.query(RecoveryCaseRow).filter_by(event_id=event_id).first()
    assert row is not None, f"no row for {event_id}"
    return row


# ---------------------------------------------------------------------------
# Ingest default + threading
# ---------------------------------------------------------------------------


def test_default_ingest_is_live(settings, db: Database) -> None:
    """The real boundary (no explicit provenance) tags the case live."""
    case, is_new, _ = _ingest(db, settings, sub="sub_live", event_id="evt_live")
    assert is_new
    assert case.provenance == Provenance.LIVE
    assert _row(db, "evt_live").provenance == "live"


def test_explicit_replay_provenance(settings, db: Database) -> None:
    """A synthetic path that forgets to pass REPLAY is the bug this pins."""
    case, is_new, _ = _ingest(
        db, settings, sub="sub_replay", event_id="evt_replay", provenance=Provenance.REPLAY
    )
    assert is_new
    assert case.provenance == Provenance.REPLAY
    assert _row(db, "evt_replay").provenance == "replay"


def test_event_to_case_accepts_provenance(settings) -> None:
    body = _webhook_body(sub="sub_etc")
    event = parse_event(body, event_id_hint="evt_etc")
    assert event_to_case(event, provenance=Provenance.REPLAY).provenance == Provenance.REPLAY
    assert event_to_case(event).provenance == Provenance.LIVE


def test_row_to_case_round_trips_provenance(settings, db: Database) -> None:
    from reclaim import repo

    _ingest(db, settings, sub="sub_rt", event_id="evt_rt", provenance=Provenance.REPLAY)
    row = _row(db, "evt_rt")
    assert repo.row_to_case(row).provenance == Provenance.REPLAY


def test_legacy_row_without_provenance_falls_back_live(settings, db: Database) -> None:
    """A pre-provenance row (provenance None) reads as live — never crashes."""
    from reclaim import repo

    _ingest(db, settings, sub="sub_leg", event_id="evt_leg")
    with db.create_session() as session:
        row = session.query(RecoveryCaseRow).filter_by(event_id="evt_leg").first()
        row.provenance = None  # simulate a row written before the column existed
        session.commit()
    row = _row(db, "evt_leg")
    assert repo.row_to_case(row).provenance == Provenance.LIVE


# ---------------------------------------------------------------------------
# The synthetic batch is REPLAY — and none of it is ever silently LIVE
# ---------------------------------------------------------------------------


def test_synthetic_batch_ingested_as_replay_not_live(settings, db: Database) -> None:
    from reclaim.batch import ingest_batch
    from reclaim.synthetic import generate_batch

    batch = generate_batch(seed=42, webhook_secret=settings.razorpay_webhook_secret)
    new_ids, duplicates, rejected = ingest_batch(db, batch, settings)

    assert len(new_ids) == 60  # the 60 valid unique deliveries
    assert duplicates == 6
    # Every ingested case is REPLAY provenance — a synthetic batch can never be live.
    for case_id in new_ids:
        with db.create_session() as session:
            row = session.query(RecoveryCaseRow).filter_by(case_id=case_id).first()
        assert row is not None
        assert row.provenance == Provenance.REPLAY.value, f"{case_id} not tagged replay"
    # And no live cases should exist in this DB at all.
    with db.create_session() as session:
        lives = session.query(RecoveryCaseRow).filter_by(provenance="live").count()
    assert lives == 0


# ---------------------------------------------------------------------------
# Captured fixtures: verbatim wire payloads + sensitive-data warning
# ---------------------------------------------------------------------------


def test_capture_writes_verbatim(tmp_path) -> None:
    from reclaim.capture import capture_webhook

    body = _webhook_body(sub="sub_cap")
    path = capture_webhook(str(tmp_path), body, event_id="evt_cap", event_type="payment.failed")
    assert path is not None
    assert path.exists()
    assert path.read_bytes() == body  # byte-exact, usable for honest replay
    assert path.parent.name == "payment.failed"
    assert path.name == "evt_cap.json"


def test_capture_disabled_when_dir_empty(tmp_path) -> None:
    from reclaim.capture import capture_webhook

    assert capture_webhook("", _webhook_body(sub="sub_x"), event_id="evt_x") is None


def test_capture_warns_on_card_number_like_material(tmp_path, caplog) -> None:
    from reclaim.capture import capture_webhook

    pan_body = json.dumps({"card": {"number": "4111111111111111"}}).encode("utf-8")
    with caplog.at_level("WARNING", logger="reclaim.capture"):
        capture_webhook(str(tmp_path), pan_body, event_id="evt_pan")
    assert any("CAPTURED_SENSITIVE_SCAN" in r.message for r in caplog.records)


def test_list_captured_and_summarize(tmp_path) -> None:
    from reclaim.capture import capture_webhook, list_captured, summarize_captured

    body = _webhook_body(sub="sub_sum")
    capture_webhook(str(tmp_path), body, event_id="evt_sum", event_type="payment.failed")
    fixtures = list_captured(str(tmp_path))
    assert len(fixtures) == 1
    summary = summarize_captured(fixtures[0])
    assert summary["event"] == "payment.failed"
    assert summary["entity_id"].startswith("pay_")