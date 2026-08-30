"""FastAPI application for Reclaim.

Registered routes:
  - GET  /health
  - POST /webhook/razorpay          (Step 2)
  - GET  /cases/{case_id}           (Step 6)
  - GET  /dashboard                 (Step 6)
  - GET  /metrics                   (Step 6)
"""

from __future__ import annotations

import html
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse

from . import repo
from .config import Settings, get_settings
from .db import Database, get_db, init_schema, RecoveryCaseRow
from .models import AuditLogEntry
from .webhook import (
    EVENT_ID_HEADER,
    SIGNATURE_HEADER,
    RazorpayWebhookException,
    ingest_event,
    parse_event,
    verify_signature,
)

logger = logging.getLogger("reclaim.api")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    db = Database(settings)
    init_schema(db.engine)
    app.state.db = db
    app.state.settings = settings
    logger.info(
        "Reclaim API started (llm_mode=%s, act_mode=%s)",
        settings.llm_mode,
        settings.act_mode,
    )
    try:
        yield
    finally:
        db.close()


app = FastAPI(title="Reclaim — AI Revenue Recovery", lifespan=lifespan)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "reclaim"}


@app.post("/webhook/razorpay")
async def razorpay_webhook(
    request: Request,
    x_razorpay_signature: str | None = Header(default=None, alias=SIGNATURE_HEADER),
    x_razorpay_event_id: str | None = Header(default=None, alias=EVENT_ID_HEADER),
) -> JSONResponse:
    """Ingest a verified, deduplicated Razorpay payment-failure event.

    Boundary order is strict: signature first, then schema, then dedupe.
    A replay of an already-ingested event returns the existing case with
    ``duplicate=true`` and never re-triggers downstream stages.
    """
    settings = get_settings_dep()
    db = get_db_dep()

    raw_body = await request.body()

    if not verify_signature(settings.razorpay_webhook_secret, raw_body, x_razorpay_signature):
        return JSONResponse(
            status_code=401,
            content={"error": "invalid_webhook_signature"},
        )

    try:
        event = parse_event(raw_body, event_id_hint=x_razorpay_event_id)
    except RazorpayWebhookException as exc:
        logger.warning("WEBHOOK_REJECTED reason=%s", exc)
        return JSONResponse(status_code=422, content={"error": "invalid_webhook_payload"})

    try:
        case, is_new, _row_pk = ingest_event(db, event, settings)
    except RazorpayWebhookException as exc:
        logger.warning("WEBHOOK_REJECTED reason=%s", exc)
        return JSONResponse(status_code=422, content={"error": "unmappable_webhook_payload"})

    if not is_new:
        return JSONResponse(
            status_code=200,
            content={
                "duplicate": True,
                "case_id": case.case_id,
                "event_id": event.event_id,
                "state": case.state.value,
            },
        )

    from .dispatcher import submit_case

    submit_case(case.case_id, event.event_id, settings)
    return JSONResponse(
        status_code=200,
        content={
            "duplicate": False,
            "case_id": case.case_id,
            "event_id": event.event_id,
            "state": case.state.value,
        },
    )


def get_settings_dep() -> Settings:
    return getattr(app.state, "settings", get_settings())


def get_db_dep() -> Database:
    return getattr(app.state, "db", get_db())


# ---------------------------------------------------------------------------
# Read-only audit surfacing (Step 6) — the demo's decision-trail view
# ---------------------------------------------------------------------------


@app.get("/cases/{case_id}")
def case_detail(case_id: str, fmt: str = "html") -> Response:
    """Per-case detail: the full decision trail, top to bottom.

    Defaults to a human-readable HTML page — exactly what the /dashboard
    links open. Pass ``?fmt=json`` for the machine-readable payload.
    """
    db = get_db_dep()
    row = repo.get_case_row(db, case_id)
    if row is None:
        return JSONResponse(status_code=404, content={"error": "case_not_found"})
    trail = repo.audit_trail(db, case_id)

    if fmt == "json":
        return JSONResponse(
            {
                "case_id": case_id,
                "state": row.state,
                "amount": row.amount,
                "customer_id": row.customer_id,
                "fallback_any_stage": any(e.fallback_triggered for e in trail),
                "audit_trail": [_entry_to_dict(e) for e in trail],
            }
        )

    page = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Case {html.escape(case_id)} — Reclaim</title>
<style>{_DASH_CSS}</style></head><body>
<h1>Reclaim — recovery decision trail</h1>
<p class="back"><a href="/dashboard">&larr; back to all cases</a></p>
<div class="card">
  <h3 class="case-title">{html.escape(row.case_id)}
    <span class="state st-{_state_class(row.state)}">{html.escape(row.state)}</span>
    <span class="amt">Rs.{row.amount:,.2f}</span>
  </h3>
  <div class="meta">customer <b>{html.escape(row.customer_id)}</b>
    &middot; subscription <b>{html.escape(row.subscription_id)}</b>
    &middot; attempt <b>{row.attempt_number}</b>
    &middot; decline code <b>{html.escape(row.failure_reason)}</b>
    &middot; ingested {row.created_at:%Y-%m-%d %H:%M:%S} UTC</div>
</div>
{_trail_html(trail)}
<div class="footer"><a href="/dashboard">&larr; back to all cases</a></div>
</body></html>"""
    return HTMLResponse(page)


@app.get("/metrics")
def metrics() -> JSONResponse:
    from .metrics import compute_metrics

    return JSONResponse(compute_metrics(get_db_dep(), get_settings_dep()))


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard() -> HTMLResponse:
    """Read-only dashboard: every case + its full decision trail."""
    db = get_db_dep()

    def _rows() -> list[tuple[RecoveryCaseRow, list[AuditLogEntry]]]:
        return [(r, repo.audit_trail(db, r.case_id)) for r in repo.all_case_rows(db)]

    cards: list[str] = []
    for row, trail in _rows():
        cards.append(
            f'<div class="card"><div class="case-title">'
            f'<a href="/cases/{html.escape(row.case_id)}">{html.escape(row.case_id)}</a>'
            f' <span class="state st-{_state_class(row.state)}">{html.escape(row.state)}</span>'
            f' <span class="amt">Rs.{row.amount:,.2f}</span></div>'
            f'{_trail_html(trail)}</div>'
        )
    page = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Reclaim dashboard</title>
<style>{_DASH_CSS}</style></head><body>
<h1>Reclaim — recovery decision trails</h1>
<p class="sub">Click a case id for the full per-case trail. {len(_rows())} case(s).</p>
{''.join(cards) or '<p>No cases yet. POST /webhook/razorpay or run python -m reclaim.batch</p>'}
</body></html>"""
    return HTMLResponse(page)


def _entry_to_dict(e: AuditLogEntry) -> dict[str, object]:
    return {
        "stage": e.stage,
        "decision": e.decision,
        "action_taken": e.action_taken,
        "outcome": e.outcome,
        "reasoning": e.agent_reasoning,
        "fallback_triggered": e.fallback_triggered,
        "timestamp": e.timestamp.isoformat(),
    }


def _trail_html(trail: list[AuditLogEntry]) -> str:
    """Render each audit stage as its own block — never concatenated text."""
    return "".join(_stage_block(e) for e in trail) or (
        "<p class='empty'>no decision trail recorded</p>"
    )


def _stage_block(e: AuditLogEntry) -> str:
    """One stage = one block: a colored stage badge, the decision, the
    outcome chip, and the reasoning on its own indented line."""
    badge = "b-" + e.stage if e.stage in ("diagnose", "decide", "act") else "b-other"
    fb_tag = (
        '<span class="fb-tag" title="The LLM call itself failed; a '
        'deterministic fallback was used">LLM failure fallback</span>'
        if e.fallback_triggered
        else ""
    )
    rationale = html.escape(e.agent_reasoning) if e.agent_reasoning else "&mdash;"
    return (
        f'<div class="stage"><div class="stage-head">'
        f'<span class="badge {badge}">{html.escape(e.stage)}</span>'
        f'<span class="decision">{html.escape(e.decision)}</span>'
        f'<span class="outcome oc-{_outcome_class(e.outcome)}">'
        f'{html.escape(e.outcome)}</span>{fb_tag}'
        f'</div><div class="reason">{rationale}</div></div>'
    )


def _state_class(state: str) -> str:
    low = state.lower()
    return low if low in ("resolved", "escalated", "failed") else "other"


def _outcome_class(outcome: str | None) -> str:
    """Color-code the outcome chip so the interesting terminal states read at
    a glance: overrides, deliberate halts, recoveries, escalations."""
    o = outcome or ""
    if "OVERRIDE" in o:
        return "override"
    if o == "STOPPED":
        return "stop"
    if "retry_succeeded" in o:
        return "recovered"
    if "ESCALATED" in o:
        return "escalated"
    return "plain"


_DASH_CSS = """
  :root{color-scheme:light}
  *{box-sizing:border-box}
  body{font-family:"Segoe UI",system-ui,-apple-system,sans-serif;margin:28px;
    background:#f1f5f9;color:#0f172a;line-height:1.45}
  h1{font-size:22px;margin:0 0 4px}
  p.sub{font-size:13px;color:#475569;margin:0 0 4px}
  p.back{font-size:13px;margin:4px 0}
  a{color:#2563eb;text-decoration:none}
  a:hover{text-decoration:underline}
  .card{background:#fff;border:1px solid #e2e8f0;border-radius:10px;padding:14px 16px;
    margin:12px 0;box-shadow:0 1px 2px rgba(15,23,42,.06)}
  .case-title{font-size:16px;font-weight:700;margin:0 0 8px;display:flex;
    align-items:center;gap:10px;flex-wrap:wrap}
  .case-title a{color:#0f172a;text-decoration:underline;text-underline-offset:2px}
  .case-title a:hover{color:#2563eb}
  .meta{font-size:12.5px;color:#475569}
  .state{font-size:12px;font-weight:700;border-radius:12px;padding:2px 10px;border:1px solid}
  .st-resolved{color:#166534;background:#f0fdf4;border-color:#86efac}
  .st-escalated{color:#92400e;background:#fffbeb;border-color:#fcd34d}
  .st-failed{color:#991b1b;background:#fef2f2;border-color:#fca5a5}
  .st-other{color:#1e40af;background:#eff6ff;border-color:#93c5fd}
  .amt{color:#0d9488;font-weight:700;font-size:15px}
  .stage{border-left:3px solid #cbd5e1;padding:7px 0 7px 14px;margin:0 0 3px}
  .stage-head{display:flex;align-items:center;gap:10px;flex-wrap:wrap;min-height:22px}
  .badge{display:inline-block;font-size:11px;font-weight:700;letter-spacing:.05em;
    text-transform:uppercase;border-radius:4px;padding:2px 7px;color:#fff}
  .b-diagnose{background:#2563eb}
  .b-decide{background:#7c3aed}
  .b-act{background:#0d9488}
  .b-other{background:#64748b}
  .decision{font-weight:600;font-size:13px;min-width:0;overflow-wrap:anywhere}
  .outcome{font-size:12px;font-weight:600;margin-left:auto;border-radius:10px;
    padding:1px 9px;background:#f1f5f9;color:#475569;border:1px solid #e2e8f0}
  .oc-override{background:#fffbeb;color:#92400e;border-color:#fcd34d}
  .oc-stop{background:#f8fafc;color:#334155;border-color:#cbd5e1}
  .oc-recovered{background:#f0fdf4;color:#166534;border-color:#86efac}
  .oc-escalated{background:#fef2f2;color:#991b1b;border-color:#fca5a5}
  .reason{margin-top:3px;font-size:12.5px;color:#475569}
  .fb-tag{font-size:10.5px;font-weight:700;color:#9a3412;background:#fff7ed;
    border:1px solid #fed7aa;border-radius:8px;padding:1px 6px;text-transform:uppercase;
    letter-spacing:.03em;white-space:nowrap}
  .footer{margin:18px 0;font-size:13px}
  .empty{color:#64748b;font-style:italic}
"""