"""Phase 4 JSON API namespace: ``/api/v1/*`` (mounted by :mod:`reclaim.api`).

This is a **parallel surface** to the existing HTML routes — a set of thin
wrappers over the same, already-tested business logic in ``repo``, ``metrics``,
``manual`` and ``stopping_rules``. It deliberately does NOT reimplement any
logic: an endpoint here calls the exact same function the corresponding HTML
route calls, so behavior cannot drift between surfaces.

Why is this a separate file (and not additional routes in ``api.py``)?
Because ``api.py`` imports and mounts this module's router below the CORS
middleware; keeping the JSON surface in its own module avoids a circular import
(``api_v1`` cannot import ``get_db_dep``/``get_settings_dep`` from ``api.py``
without recursing), and keeps the legacy HTML routes byte-for-byte untouched.

The DB/settings dependencies here read ``request.app.state`` with a fallback to
the process-wide singletons, mirroring ``api.get_db_dep``/``get_settings_dep``
without importing from ``api``. They are plain module-level functions so tests
can swap them via ``app.dependency_overrides``.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

from . import repo
from .api_views import _run_simulated_batch, customer_view
from .config import Settings, get_settings
from .db import Database, get_db, RecoveryCaseRow
from .metrics import compute_metrics
from .models import AuditLogEntry
from .state_machine import IllegalTransitionError
from .stopping_rules import describe_rules

router = APIRouter(prefix="/api/v1")

# Pagination safety: never return an unbounded result set. 200/page is a
# generous cap for this tool (visual inspection, not bulk sync).
_MAX_PAGE = 200
_DEFAULT_PAGE = 50


# ---------------------------------------------------------------------------
# Dependencies (overridable by tests via app.dependency_overrides)
# ---------------------------------------------------------------------------


def get_db_dep(request: Request) -> Database:
    return getattr(request.app.state, "db", get_db())


def get_settings_dep(request: Request) -> Settings:
    return getattr(request.app.state, "settings", get_settings())


def _audit_entry_dict(e: AuditLogEntry) -> dict[str, Any]:
    """Serialize one append-only audit entry to JSON.

    Carries the full record — including ``input_state`` (which holds
    ``llm_provenance`` when the LLM was consulted: model / prompt_version /
    prompt_hash / mode) — so the frontend's Case Detail can render provenance
    and overrides without a second round-trip. Serialized from the SAME
    :class:`AuditLogEntry` objects the legacy ``case_detail(fmt=json)`` route
    reads (via :func:`reclaim.repo.audit_trail`); no business logic is
    reimplemented here.
    """
    return {
        "stage": e.stage,
        "agent_reasoning": e.agent_reasoning,
        "input_state": e.input_state or {},
        "decision": e.decision,
        "action_taken": e.action_taken,
        "outcome": e.outcome,
        "fallback_triggered": e.fallback_triggered,
        "timestamp": e.timestamp.isoformat() if e.timestamp else None,
    }


def _case_summary(r: RecoveryCaseRow) -> dict[str, Any]:
    """Serialize one case row to the A1 summary shape."""
    return {
        "case_id": r.case_id,
        "state": r.state,
        "amount": r.amount,
        "customer_id": r.customer_id,
        "subscription_id": r.subscription_id,
        "failure_reason": r.failure_reason,
        "attempt_number": r.attempt_number,
        "provenance": r.provenance or "live",
        "created_at": r.created_at.isoformat() if r.created_at else None,
    }


def _case_detail_json(db: Database, case_id: str) -> dict[str, Any]:
    """The shared A2 detail shape — the same base fields as the legacy
    ``case_detail(fmt=json)`` route, read from the same repo functions."""
    row = repo.get_case_row(db, case_id)
    if row is None:
        raise HTTPException(status_code=404, detail="case_not_found")
    trail = repo.audit_trail(db, case_id)
    return {
        "case_id": row.case_id,
        "state": row.state,
        "amount": row.amount,
        "customer_id": row.customer_id,
        "provenance": row.provenance or "live",
        "fallback_any_stage": any(e.fallback_triggered for e in trail),
        "audit_trail": [_audit_entry_dict(e) for e in trail],
    }


# ---------------------------------------------------------------------------
# A1 — list cases (filtered + paginated at the query layer)
# ---------------------------------------------------------------------------


@router.get("/cases")
def list_cases(
    state: str | None = Query(default=None, description="Optional exact CaseState filter"),
    limit: int = Query(default=_DEFAULT_PAGE, ge=1, le=_MAX_PAGE),
    offset: int = Query(default=0, ge=0),
    db: Database = Depends(get_db_dep),
) -> dict[str, Any]:
    """Case summaries, optionally filtered by state and paginated.

    Filtering + pagination are pushed to the SQL query layer
    (:func:`reclaim.repo.list_cases`), so the response stays bounded as the
    dataset grows.
    """
    from .models import CaseState

    if state is not None:
        valid = {s.value for s in CaseState}
        if state not in valid:
            raise HTTPException(status_code=422, detail=f"invalid state: {state}")
    rows = repo.list_cases(db, state=state, limit=limit, offset=offset)
    return {"items": [_case_summary(r) for r in rows], "count": len(rows)}


# ---------------------------------------------------------------------------
# A2 — case detail (+ full audit trail with provenance)
# ---------------------------------------------------------------------------


@router.get("/cases/{case_id}")
def case_detail_v1(case_id: str, db: Database = Depends(get_db_dep)) -> dict[str, Any]:
    return _case_detail_json(db, case_id)


# ---------------------------------------------------------------------------
# A3 — metrics
# ---------------------------------------------------------------------------


@router.get("/metrics")
def metrics_v1(db: Database = Depends(get_db_dep), settings: Settings = Depends(get_settings_dep)) -> dict[str, Any]:
    return compute_metrics(db, settings)


# ---------------------------------------------------------------------------
# A4 — active stopping rules (policy-as-code)
# ---------------------------------------------------------------------------


@router.get("/rules")
def rules_v1(settings: Settings = Depends(get_settings_dep)) -> list[dict[str, Any]]:
    return describe_rules(settings)


# ---------------------------------------------------------------------------
# A5 — rule-sensitivity simulator (shared with the HTML /simulator route)
# ---------------------------------------------------------------------------


class SimulatorRunRequest(BaseModel):
    """The same editable threshold subset as the HTML /simulator form.

    All fields optional — absent fields fall back to the current settings, and
    ``_run_simulated_batch`` applies them through a throwaway Settings copy, so
    real settings are never mutated.
    """

    escalation_amount_threshold: float | None = None
    escalation_days_threshold: int | None = None
    max_retries_per_cycle: int | None = None
    cooldown_hours: float | None = None
    email_cap_per_7d: int | None = None


@router.post("/simulator/run")
def simulator_run_v1(
    body: SimulatorRunRequest,
    settings: Settings = Depends(get_settings_dep),
) -> dict[str, Any]:
    """Re-run the seed-42 batch under proposed thresholds; return before/after.

    Baseline = current thresholds (empty override set); simulated = the
    submitted overrides. Both run on throwaway temp-file DBs; the endpoint never
    mutates the real ``settings``.
    """
    overrides = {k: v for k, v in body.model_dump().items() if v is not None}
    try:
        baseline = _run_simulated_batch(settings, {})
        simulated = _run_simulated_batch(settings, overrides)
    except HTTPException:
        raise
    except Exception as exc:  # never let a simulation crash the API
        raise HTTPException(status_code=500, detail=f"simulation_failed: {type(exc).__name__}") from exc
    return {"baseline": baseline, "simulated": simulated}


# ---------------------------------------------------------------------------
# A6 — manual-override control plane (human-in-the-loop)
# ---------------------------------------------------------------------------
# Thin wrappers over the SAME manual.py functions the HTML routes use. Unlike
# the HTML versions (303 redirect), these return the updated case JSON.
# Error semantics match the HTML routes: 404 unknown case, 409 not-ESCALATED.


@router.post("/cases/{case_id}/approve_retry")
def approve_retry_v1(case_id: str, db: Database = Depends(get_db_dep), settings: Settings = Depends(get_settings_dep)) -> dict[str, Any]:
    from .manual import approve_manual_retry

    try:
        approve_manual_retry(db, case_id, settings)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="case_not_found") from exc
    except IllegalTransitionError as exc:
        raise HTTPException(status_code=409, detail=f"action not legal: {exc}") from exc
    return _case_detail_json(db, case_id)


@router.post("/cases/{case_id}/resolve_human")
def resolve_human_v1(case_id: str, db: Database = Depends(get_db_dep), settings: Settings = Depends(get_settings_dep)) -> dict[str, Any]:
    from .manual import resolve_human

    try:
        resolve_human(db, case_id, settings)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="case_not_found") from exc
    except IllegalTransitionError as exc:
        raise HTTPException(status_code=409, detail=f"action not legal: {exc}") from exc
    return _case_detail_json(db, case_id)


# ---------------------------------------------------------------------------
# A7 — customer-facing status (plain language, no internal jargon)
# ---------------------------------------------------------------------------


@router.get("/status/{case_id}")
def customer_status_v1(case_id: str, db: Database = Depends(get_db_dep)) -> dict[str, str]:
    row = repo.get_case_row(db, case_id)
    if row is None:
        raise HTTPException(status_code=404, detail="case_not_found")
    trail = repo.audit_trail(db, case_id)
    return customer_view(row, trail)
