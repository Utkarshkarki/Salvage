"""Webhook boundary: signature verification, schema parsing, and dedupe."""

from __future__ import annotations

import json

import pytest
from sqlalchemy import func, select

from reclaim.config import Settings
from reclaim.db import Database, RecoveryCaseRow
from reclaim.models import CaseState
from reclaim.webhook import (
    RazorpayWebhookException,
    compute_signature,
    event_to_case,
    ingest_event,
    parse_event,
    verify_signature,
)

SECRET = "test-webhook-secret"


def _body(*, event_id: str = "evt_1", amount: int = 100000) -> bytes:
    data = {
        "event": "payment.failed",
        "entity": {
            "id": "pay_1",
            "subscription_id": "sub_1",
            "customer_id": "cust_1",
            "amount": amount,
            "attempt_number": 2,
            "error_code": "R01",
            "error_description": "insufficient",
            "status": "failed",
            "created_at": 1700000000,
        },
    }
    return json.dumps(data).encode("utf-8")


# ---------------------------------------------------------------------------
# Signature verification
# ---------------------------------------------------------------------------


def test_valid_signature_accepted() -> None:
    body = _body()
    sig = compute_signature(SECRET, body)
    assert verify_signature(SECRET, body, sig) is True


def test_wrong_signature_rejected() -> None:
    body = _body()
    sig = compute_signature("different-secret", body)
    assert verify_signature(SECRET, body, sig) is False


def test_tampered_body_rejected() -> None:
    body = bytearray(_body())
    body[-1] = ord("2") if body[-1] == ord("1") else ord("1")
    sig = compute_signature(SECRET, _body())  # signature of the ORIGINAL
    assert verify_signature(SECRET, bytes(body), sig) is False


def test_missing_signature_rejected() -> None:
    assert verify_signature(SECRET, _body(), None) is False


def test_empty_secret_raises() -> None:
    with pytest.raises(RazorpayWebhookException, match="not configured"):
        verify_signature("", _body(), "abc")


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def test_parse_valid_body() -> None:
    event = parse_event(_body(), event_id_hint="evt_1")
    assert event.event_id == "evt_1"
    assert event.type == "payment.failed"
    assert event.subscription_id() == "sub_1"
    assert event.amount() == 1000.0


def test_parse_bad_json_rejected() -> None:
    with pytest.raises(RazorpayWebhookException, match="malformed JSON"):
        parse_event(b"not json")


def test_parse_missing_event_type_rejected() -> None:
    body = json.dumps({"entity": {"id": "pay_x"}}).encode("utf-8")
    with pytest.raises(RazorpayWebhookException, match="schema validation"):
        parse_event(body)


def test_parse_accepts_payment_captured() -> None:
    """Phase 6.1: payment.captured is a subscribed, accepted event type — it is
    parsed fine (the API layer acknowledges it but never ingests it as a case)."""
    body = json.dumps(
        {
            "event": "payment.captured",
            "entity": {"id": "pay_cap", "amount": 100000, "status": "captured"},
        }
    ).encode("utf-8")
    event = parse_event(body, event_id_hint="evt_cap")
    assert event.type == "payment.captured"


def test_deterministic_event_id_without_hint() -> None:
    body = _body()
    e1 = parse_event(body)
    e2 = parse_event(body)
    assert e1.event_id == e2.event_id
    assert e1.event_id.startswith("evt_")


# ---------------------------------------------------------------------------
# Event -> case mapping
# ---------------------------------------------------------------------------


def test_event_to_case_mapping(settings: Settings) -> None:
    case = event_to_case(parse_event(_body()))
    assert case.case_id == "sub_1"
    assert case.amount == 1000.0
    assert case.attempt_number == 2
    assert case.state == CaseState.INGESTED


def test_unmappable_amount_rejected(settings: Settings) -> None:
    body = json.dumps({"event": "payment.failed", "entity": {}}).encode("utf-8")
    event = parse_event(body)
    with pytest.raises(RazorpayWebhookException, match="unmappable"):
        event_to_case(event)


# ---------------------------------------------------------------------------
# Dedupe
# ---------------------------------------------------------------------------


def test_new_event_for_existing_subscription_raises_gracefully(settings: Settings, db: Database) -> None:
    """A second failure for an already-tracked subscription (same case_id, NEW
    event_id) is a deliberate boundary rejection, never a 500.

    RecoveryCaseRow.case_id is UNIQUE; a genuine recurring failure arrives as a
    fresh event_id (so the dedupe fast-path misses) but the same subscription_id
    -> same case_id -> IntegrityError on INSERT. ingest_event must convert that
    into an explicit RazorpayWebhookException (mapped to 422 by the API layer),
    not let the raw IntegrityError escape.
    """
    first = parse_event(_body(amount=9900), event_id_hint="evt_first")
    case, is_new, _ = ingest_event(db, first, settings)
    assert is_new
    assert case.case_id == "sub_1"

    second = parse_event(_body(amount=9900), event_id_hint="evt_second")
    assert second.case_id() == case.case_id  # same subscription -> same case_id

    with pytest.raises(RazorpayWebhookException, match="already tracked"):
        ingest_event(db, second, settings)


def test_ingest_creates_case_then_duplicate_is_noop(settings: Settings, db: Database) -> None:
    body = _body()
    sig = compute_signature(SECRET, body)
    assert verify_signature(SECRET, body, sig)
    event = parse_event(body, event_id_hint="evt_alpha")

    case_new, is_new, pk = ingest_event(db, event, settings)
    assert is_new is True
    assert case_new.state == CaseState.INGESTED

    case_dup, is_new2, pk2 = ingest_event(db, event, settings)
    assert is_new2 is False
    assert case_dup.case_id == case_new.case_id
    assert pk2 == pk  # same physical row

    with db.create_session() as session:
        count = session.execute(select(func.count()).select_from(RecoveryCaseRow)).scalar_one()
    assert count == 1


def test_dedupe_existing_row_has_single_state(settings: Settings, db: Database) -> None:
    """Replaying the same event must not re-trigger or mutate the case."""
    body = _body()
    event = parse_event(body, event_id_hint="evt_dup")
    _case, is_new, _ = ingest_event(db, event, settings)
    assert is_new

    # Simulate the pipeline having advanced the case's persisted state.
    with db.create_session() as session:
        row = session.query(RecoveryCaseRow).filter_by(event_id="evt_dup").first()
        assert row is not None
        row.state = CaseState.RESOLVED.value
        session.commit()

    _dup, is_new2, _ = ingest_event(db, event, settings)
    assert is_new2 is False
    with db.create_session() as session:
        row = session.query(RecoveryCaseRow).filter_by(event_id="evt_dup").first()
        assert row is not None
        assert row.state == CaseState.RESOLVED.value  # untouched by the replay