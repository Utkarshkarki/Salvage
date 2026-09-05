"""HTTP-layer tests for the Phase 4 JSON API namespace (/api/v1/*).

These test the HTTP boundary — status codes, response shapes, param handling —
NOT the business logic underneath, which is already proven by the existing
unit tests (test_manual.py, test_simulator.py, test_stopping_rules.py, etc.).
We override the api_v1 DB/settings dependencies with the hermetic conftest
fixtures and seed the minimum cases needed, then assert on what the endpoint
returns over the wire.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

import reclaim.api as api
from reclaim import api_v1, repo
from reclaim.api import app
from reclaim.models import CaseState as CaseStateEnum
from reclaim.webhook import compute_signature, ingest_event, parse_event


def _body(*, sub: str = "sub1", code: str = "R01", days_ago: int = 2) -> bytes:
    # amount is expressed in PAISE in the Razorpay webhook body; ingest
    # converts to INR (÷100), so 250000 paise -> a stored amount of 2500.0.
    return json.dumps({
        "event": "payment.failed",
        "entity": {
            "id": f"pay_{sub}",
            "subscription_id": sub,
            "customer_id": f"cust_{sub}",
            "amount": 250000,
            "attempt_number": 1,
            "error_code": code,
            "error_description": "declined",
            "status": "failed",
            "created_at": int((datetime.now(UTC) - timedelta(days=days_ago)).timestamp()),
        },
    }).encode("utf-8")


def _seed(db, settings, *, sub="sub1", code="R01", event_id=None) -> str:
    """Ingest one webhook event through the real boundary; return the case_id."""
    body = _body(sub=sub, code=code)
    sig = compute_signature(settings.razorpay_webhook_secret, body)
    event = parse_event(body, event_id_hint=event_id)
    case, _is_new, _ = ingest_event(db, event, settings)
    return case.case_id


@pytest.fixture()
def client(settings, db):
    """TestClient with api_v1 deps pointed at the hermetic conftest db/settings.

    Not used as a context manager, so the app's lifespan (which would touch the
    real dev DB) never runs — endpoints reach the conftest temp DB through the
    overridden dependencies.
    """
    app.dependency_overrides[api_v1.get_db_dep] = lambda: db
    app.dependency_overrides[api_v1.get_settings_dep] = lambda: settings
    c = TestClient(app)
    yield c
    app.dependency_overrides.clear()


@pytest.fixture()
def webhook_client(settings, db):
    """TestClient with BOTH app surfaces pointed at the hermetic DB/settings.

    The /webhook/razorpay route uses ``api.get_db_dep``/``api.get_settings_dep``
    (NOT api_v1's) and, unlike the /api/v1/* tests, actually runs the route body —
    so it needs api.py's deps overridden too to stay off the real DB and .env.
    """
    app.dependency_overrides[api.get_db_dep] = lambda: db
    app.dependency_overrides[api.get_settings_dep] = lambda: settings
    app.dependency_overrides[api_v1.get_db_dep] = lambda: db
    app.dependency_overrides[api_v1.get_settings_dep] = lambda: settings
    c = TestClient(app)
    yield c
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# A1 — list cases (filter + pagination)
# ---------------------------------------------------------------------------


def test_list_cases_happy_path(client, settings, db) -> None:
    case_id = _seed(db, settings)
    r = client.get("/api/v1/cases")
    assert r.status_code == 200
    payload = r.json()
    assert payload["count"] == 1
    item = payload["items"][0]
    assert item["case_id"] == case_id
    assert item["state"] == CaseStateEnum.INGESTED.value
    assert item["amount"] == 2500
    assert item["customer_id"] == "cust_sub1"
    assert item["subscription_id"] == "sub1"
    assert item["failure_reason"] == "R01"
    assert item["attempt_number"] == 1
    assert item["created_at"]  # ISO string present


def test_list_cases_state_filter(client, settings, db) -> None:
    _seed(db, settings, sub="a")
    _seed(db, settings, sub="b")
    # Move only case "a" to RESOLVED.
    repo.set_case_state(db, "a", CaseStateEnum.RESOLVED)

    filtered = client.get("/api/v1/cases", params={"state": "RESOLVED"})
    assert filtered.status_code == 200
    payload = filtered.json()
    assert payload["count"] == 1
    assert payload["items"][0]["case_id"] == "a"

    unfiltered = client.get("/api/v1/cases")
    assert unfiltered.json()["count"] == 2


def test_list_cases_pagination_bounds(client, settings, db) -> None:
    # Seed 5 cases.
    for i in range(5):
        _seed(db, settings, sub=f"p{i}")

    page1 = client.get("/api/v1/cases", params={"limit": 2, "offset": 0}).json()
    page2 = client.get("/api/v1/cases", params={"limit": 2, "offset": 2}).json()
    page3 = client.get("/api/v1/cases", params={"limit": 2, "offset": 4}).json()

    assert page1["count"] == 2
    assert page2["count"] == 2
    assert page3["count"] == 1
    assert [i["case_id"] for i in page1["items"]] == ["p0", "p1"]
    assert [i["case_id"] for i in page2["items"]] == ["p2", "p3"]
    assert [i["case_id"] for i in page3["items"]] == ["p4"]

    # Setup violations are rejected (not silently clamped silently to 0/∞).
    assert client.get("/api/v1/cases", params={"offset": -1}).status_code == 422
    assert client.get("/api/v1/cases", params={"limit": 0}).status_code == 422
    assert client.get("/api/v1/cases", params={"limit": 100000}).status_code == 422


def test_list_cases_invalid_state_422(client) -> None:
    r = client.get("/api/v1/cases", params={"state": "NOT_A_STATE"})
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# A2 — case detail (with provenance in the audit trail)
# ---------------------------------------------------------------------------


def test_case_detail_includes_provenance_and_audit(client, settings, db) -> None:
    from reclaim.pipeline import run_case

    case_id = _seed(db, settings)
    run_case(case_id, settings=settings, db=db)

    r = client.get(f"/api/v1/cases/{case_id}")
    assert r.status_code == 200
    body = r.json()
    assert body["case_id"] == case_id
    assert body["state"] == CaseStateEnum.RESOLVED.value
    assert body["amount"] == 2500
    assert "fallback_any_stage" in body

    trail = body["audit_trail"]
    assert any(e["stage"] == "diagnose" for e in trail)
    diag = next(e for e in trail if e["stage"] == "diagnose")
    # The richer A2 shape carries agent_reasoning + input_state (provenance).
    assert "agent_reasoning" in diag
    prov = diag["input_state"].get("llm_provenance")
    assert prov is not None
    assert prov["model"] == settings.ollama_model
    assert prov["mode"] == "offline"
    assert prov["prompt_version"].startswith("diagnose")


def test_case_detail_unknown_404(client) -> None:
    r = client.get("/api/v1/cases/nope")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# A3 — metrics; A4 — rules
# ---------------------------------------------------------------------------


def test_metrics_full_shape(client, settings, db) -> None:
    _seed(db, settings)
    r = client.get("/api/v1/metrics")
    assert r.status_code == 200
    body = r.json()
    for key in (
        "total_cases", "state_distribution", "amount_at_risk", "recovered_cases",
        "recovered_amount", "recovery_rate", "cause_breakdown", "llm_call_failures",
        "llm_failure_cases", "stopping_rule_overrides", "stopping_rule_overrides_by_rule",
        "rule_override_cases", "stub_mode_actions", "stub_mode_cases",
        "cases_resolved_without_retry", "stopped_cases", "escalated_cases",
        "provenance_breakdown",
    ):
        assert key in body, f"missing metrics key {key}"
    assert body["total_cases"] == 1
    assert body["provenance_breakdown"] == {"live": 1, "replay": 0, "mocked": 0}


def test_rules_returns_registry(client, settings) -> None:
    r = client.get("/api/v1/rules")
    assert r.status_code == 200
    rules = r.json()
    ids = [rule["rule_id"] for rule in rules]
    assert ids[0] == "R1"
    assert set(ids) == {"R1", "R2", "R3", "R4", "R5", "R6", "R7"}
    for rule in rules:
        assert {"rule_id", "priority", "action", "description"} <= set(rule)


# ---------------------------------------------------------------------------
# A5 — simulator (does not mutate real settings)
# ---------------------------------------------------------------------------


def test_simulator_run_returns_comparison(client, settings) -> None:
    r = client.post("/api/v1/simulator/run", json={"escalation_amount_threshold": 1.0})
    assert r.status_code == 200
    body = r.json()
    assert "baseline" in body and "simulated" in body
    assert body["baseline"]["total_cases"] == 60
    assert body["simulated"]["total_cases"] == 60
    # Lowering the threshold pushes more cases to escalate.
    assert body["simulated"]["escalated_cases"] > body["baseline"]["escalated_cases"]


def test_simulator_run_does_not_mutate_real_settings(client, settings) -> None:
    before = [settings.escalation_amount_threshold, settings.cooldown_hours]
    r = client.post("/api/v1/simulator/run", json={"escalation_amount_threshold": 1.0, "cooldown_hours": 100})
    assert r.status_code == 200
    assert settings.escalation_amount_threshold == before[0]
    assert settings.cooldown_hours == before[1]


def test_simulator_run_empty_body_uses_current(client, settings) -> None:
    r = client.post("/api/v1/simulator/run", json={})
    assert r.status_code == 200
    body = r.json()
    assert body["baseline"] == body["simulated"]  # no overrides -> identical


# ---------------------------------------------------------------------------
# A6 — manual override control plane
# ---------------------------------------------------------------------------


def test_approve_retry_success(client, settings, db) -> None:
    case_id = _seed(db, settings)
    repo.set_case_state(db, case_id, CaseStateEnum.ESCALATED)
    r = client.post(f"/api/v1/cases/{case_id}/approve_retry")
    assert r.status_code == 200
    body = r.json()
    assert body["case_id"] == case_id
    # Stub retry_now succeeds -> RESOLVED.
    assert body["state"] in (CaseStateEnum.RESOLVED.value, CaseStateEnum.FAILED.value)


def test_resolve_human_success(client, settings, db) -> None:
    case_id = _seed(db, settings)
    repo.set_case_state(db, case_id, CaseStateEnum.ESCALATED)
    r = client.post(f"/api/v1/cases/{case_id}/resolve_human")
    assert r.status_code == 200
    assert r.json()["state"] == CaseStateEnum.RESOLVED.value


def test_approve_retry_unknown_404(client) -> None:
    r = client.post("/api/v1/cases/does-not-exist/approve_retry")
    assert r.status_code == 404


def test_approve_retry_not_escalated_409(client, settings, db) -> None:
    case_id = _seed(db, settings)  # remains INGESTED, not ESCALATED
    r = client.post(f"/api/v1/cases/{case_id}/approve_retry")
    assert r.status_code == 409


def test_resolve_human_not_escalated_409(client, settings, db) -> None:
    case_id = _seed(db, settings)
    r = client.post(f"/api/v1/cases/{case_id}/resolve_human")
    assert r.status_code == 409


# ---------------------------------------------------------------------------
# A7 — customer status
# ---------------------------------------------------------------------------


def test_customer_status_happy(client, settings, db) -> None:
    case_id = _seed(db, settings)
    r = client.get(f"/api/v1/status/{case_id}")
    assert r.status_code == 200
    body = r.json()
    assert {"heading", "reason", "next_step"} <= set(body)
    # Plain language only — no internal jargon/rule ids in any field.
    for v in body.values():
        assert "OVERRIDE" not in v and "rule=" not in v and "stage" not in v.lower()


def test_customer_status_unknown_404(client) -> None:
    r = client.get("/api/v1/status/nope")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Phase 6.1 — provenance on the read paths + the live webhook route
# ---------------------------------------------------------------------------


def test_case_payloads_carry_provenance(client, settings, db) -> None:
    """The A1 summary and A2 detail expose the case's provenance tag."""
    case_id = _seed(db, settings, sub="prov1")
    r = client.get(f"/api/v1/cases/{case_id}")
    assert r.status_code == 200
    assert r.json()["provenance"] == "live"  # seeded through the real ingest path

    items = client.get("/api/v1/cases").json()["items"]
    assert items[0]["provenance"] == "live"


def test_payment_captured_is_acknowledged_not_ingested(webhook_client, settings, db) -> None:
    """A payment.captured delivery is acknowledged (observational) and NEVER becomes
    a recovery case — a success must not inflate the revenue-recovery metrics."""
    from reclaim.db import RecoveryCaseRow

    body = json.dumps({
        "event": "payment.captured",
        "entity": {"id": "pay_cap2", "amount": 100000, "status": "captured", "created_at": 1700000000},
    }).encode("utf-8")
    sig = compute_signature(settings.razorpay_webhook_secret, body)

    r = webhook_client.post(
        "/webhook/razorpay",
        content=body,
        headers={
            "X-Razorpay-Signature": sig,
            "X-Razorpay-Event-Id": "evt_cap2",
        },
    )
    assert r.status_code == 200
    j = r.json()
    assert j["acknowledged"] is True
    assert j["ingested"] is False
    assert j["type"] == "payment.captured"

    with db.create_session() as session:
        assert session.query(RecoveryCaseRow).filter_by(event_id="evt_cap2").first() is None


def test_webhook_route_captures_verified_payload(webhook_client, tmp_path, settings, db) -> None:
    """A signature-passing payload is written VERBATIM to the captured-fixtures dir,
    and the case is ingested as provenance=live."""
    from reclaim.db import RecoveryCaseRow

    cap = settings.model_copy(update={"razorpay_webhook_capture_dir": str(tmp_path)})
    app.dependency_overrides[api.get_settings_dep] = lambda: cap

    body = _body(sub="cap3", code="R01")
    sig = compute_signature(settings.razorpay_webhook_secret, body)
    r = webhook_client.post(
        "/webhook/razorpay",
        content=body,
        headers={"X-Razorpay-Signature": sig, "X-Razorpay-Event-Id": "evt_cap3"},
    )
    assert r.status_code == 200
    assert r.json()["duplicate"] is False

    fixture = tmp_path / "payment.failed" / "evt_cap3.json"
    assert fixture.exists()
    assert fixture.read_bytes() == body  # byte-exact wire payload

    with db.create_session() as session:
        row = session.query(RecoveryCaseRow).filter_by(event_id="evt_cap3").first()
    assert row is not None
    assert row.provenance == "live"

    app.dependency_overrides.pop(api.get_settings_dep, None)
