"""Synthetic batch generator invariants (seed stability, coverage, dedupe)."""

from __future__ import annotations

from reclaim.models import Cause
from reclaim.synthetic import generate_batch


def test_batch_shape_and_counts() -> None:
    batch = generate_batch(n_valid=60, n_duplicates=6, n_rejections=7, seed=42)
    summary = batch.summary()
    assert summary["total_deliveries"] == 73
    assert summary["valid_unique_events"] == 60
    assert summary["duplicate_deliveries"] == 6
    assert summary["expected_rejections"] == 7
    assert len(batch.unique_valid()) == 60


def test_at_least_50_unique_events() -> None:
    batch = generate_batch(seed=1)
    assert len(batch.unique_valid()) >= 50


def test_every_cause_is_represented() -> None:
    batch = generate_batch(seed=42)
    seen = {w.cause_hint for w in batch.valid_deliveries()}
    assert seen == set(Cause)


def test_includes_above_threshold_amounts_and_long_gaps() -> None:
    batch = generate_batch(seed=42)
    above = 0
    for w in batch.valid_deliveries():
        if w.event is not None and w.event.amount() > 5000.0:
            above += 1
        sub = w.event.subscription_id() if w.event else ""
        if sub and batch.enrichments[sub].days_since_last_attempt() > 7:
            assert True  # at least one long-gap case exists (checked below wholesale)
    assert above >= 5, f"expected >=5 above-threshold cases, got {above}"
    assert any(
        batch.enrichments[w.event.subscription_id()].days_since_last_attempt() > 7
        for w in batch.valid_deliveries()
        if w.event is not None
    )


def test_seeded_batch_is_deterministic() -> None:
    a = generate_batch(seed=42)
    b = generate_batch(seed=42)
    assert [w.raw_body for w in a.valid_deliveries()] == [
        w.raw_body for w in b.valid_deliveries()
    ]
    assert a.summary() == b.summary()


def test_different_seeds_differ() -> None:
    a = generate_batch(seed=42)
    b = generate_batch(seed=7)
    assert [w.raw_body for w in a.valid_deliveries()] != [
        w.raw_body for w in b.valid_deliveries()
    ]


def test_valid_deliveries_parse_and_sign() -> None:
    batch = generate_batch(seed=42, webhook_secret="demo-secret")
    from reclaim.webhook import compute_signature, parse_event, verify_signature

    for w in batch.valid_deliveries():
        parse_event(w.raw_body, event_id_hint=w.event_id)  # must not raise
        assert verify_signature("demo-secret", w.raw_body, w.signature) is True
        assert compute_signature("demo-secret", w.raw_body) == w.signature


def test_duplicates_are_exact_replays() -> None:
    batch = generate_batch(seed=42)
    dupes = [w for w in batch.webhooks if w.note == "duplicate-delivery"]
    assert len(dupes) == 6
    originals = {w.event_id: w for w in batch.unique_valid().values()}
    for d in dupes:
        orig = originals[d.event_id]
        assert d.raw_body == orig.raw_body
        assert d.signature == orig.signature


def test_rejections_cover_all_rejection_classes() -> None:
    batch = generate_batch(seed=42)
    bad = [w for w in batch.webhooks if not w.valid_delivery]
    notes = {w.note for w in bad}
    assert {"malformed-json", "missing-event-type", "tampered-body", "missing-signature"} <= notes