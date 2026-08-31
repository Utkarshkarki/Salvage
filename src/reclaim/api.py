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
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from . import repo
from .api_views import (
    _CAUSE_PLAIN,  # noqa: F401  (re-exported so existing imports resolve)
    _SIM_THRESHOLD_FIELDS,
    _run_simulated_batch,
    _sim_metric_key,
    customer_view as _customer_view,
)
from .config import Settings, get_settings
from .db import Database, get_db, init_schema, RecoveryCaseRow
from .models import AuditLogEntry, CaseState
from .state_machine import IllegalTransitionError
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

# CORS for the Phase 4 React SPA.
#
# ⚠️  WARNING — LOCAL DEMO CONFIG, NOT DEPLOYMENT-SAFE AS-IS:
# The origin list below (the Vite dev server) is explicitly NOT a wildcard, and
# it must remain that way. A wildcard ("*") would let ANY origin read/write the
# API from a browser. For any real deployment behind a reverse proxy the frontend
# origin(s) must be narrowed to exactly the deployed site (and ideally the API and
# SPA are served from the same origin so CORS is unnecessary entirely). Override
# RECLAIM_CORS_ORIGINS (comma-separated) via env for anything beyond this dev
# default. "allow_credentials=True" is only meaningful together with explicit,
# non-wildcard origins.
_cors_origins = [
    o.strip()
    for o in get_settings().cors_origins.split(",")
    if o.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount the Phase 4 JSON API namespace (/api/v1/*). It is a parallel surface to
# the HTML routes — thin wrappers over the same tested business logic.
from .api_v1 import router as api_v1_router

app.include_router(api_v1_router)


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

    controls = _ESCALATED_CONTROLS(case_id) if row.state == CaseState.ESCALATED.value else ""

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
{controls}
<div class="footer"><a href="/dashboard">&larr; back to all cases</a></div>
</body></html>"""
    return HTMLResponse(page)


def _ESCALATED_CONTROLS(case_id: str) -> str:
    """Operator buttons for an ESCALATED case.

    Clearly framed as MANUAL decision — the audit trail marks these
    ``stage=manual_override`` so a viewer sees a human act, never the agent's.
    """
    cid = html.escape(case_id)
    return (
        '<div class="card control-plane">'
        '<h3 class="case-title">Operator actions <span class="state st-other">MANUAL</span></h3>'
        '<p class="sub">These are HUMAN decisions, not the agent\'s — they are written to the '
        'audit trail as <code>manual_override</code>.</p>'
        f'<form method="post" action="/cases/{cid}/approve_retry" style="display:inline">'
        '<button type="submit">Approve manual retry</button></form> '
        f'<form method="post" action="/cases/{cid}/resolve_human" style="display:inline">'
        '<button type="submit">Mark resolved by human</button></form>'
        "</div>"
    )


@app.post("/cases/{case_id}/approve_retry")
def approve_retry_endpoint(case_id: str) -> Response:
    """Operator: authorise a retry for an ESCALATED case (manual_override)."""
    from .manual import approve_manual_retry

    db = get_db_dep()
    settings = get_settings_dep()
    try:
        approve_manual_retry(db, case_id, settings)
    except (KeyError, IllegalTransitionError) as exc:
        return JSONResponse(status_code=409, content={"error": f"action not legal: {exc}"})
    return RedirectResponse(url=f"/cases/{case_id}", status_code=303)


@app.post("/cases/{case_id}/resolve_human")
def resolve_human_endpoint(case_id: str) -> Response:
    """Operator: mark an ESCALATED case resolved by a human (manual_override)."""
    from .manual import resolve_human

    db = get_db_dep()
    settings = get_settings_dep()
    try:
        resolve_human(db, case_id, settings)
    except (KeyError, IllegalTransitionError) as exc:
        return JSONResponse(status_code=409, content={"error": f"action not legal: {exc}"})
    return RedirectResponse(url=f"/cases/{case_id}", status_code=303)


# ---------------------------------------------------------------------------
# B3: Customer-facing status page — /status/{case_id}
# ---------------------------------------------------------------------------
# A simplified, plain-language view read from the SAME underlying audit/case
# data as the merchant dashboard — no separate data model, just a filtered
# rendering. Deliberately exposes NO internal rule IDs, stage names, or
# LLM/fallback jargon.
#
# NOTE (tradeoff, flagged): this page is addressed by the raw case_id (the
# subscription id), which could be guessed. A production deployment should key
# it on a non-guessable, per-case share token. Kept as case_id for consistency
# with the merchant dashboard and to avoid a schema migration in this phase.

# _CAUSE_PLAIN / _customer_view moved to api_views.py (shared with the JSON
# API so the HTML and JSON status surfaces cannot drift). Imported above.


@app.get("/status/{case_id}", response_class=HTMLResponse)
def customer_status(case_id: str) -> HTMLResponse:
    """Customer-facing, plain-language status for one case."""
    db = get_db_dep()
    row = repo.get_case_row(db, case_id)
    if row is None:
        return HTMLResponse(
            "<html><body><h1>Status not found</h1><p>We couldn't find that reference.</p></body></html>",
            status_code=404,
        )
    trail = repo.audit_trail(db, case_id)
    view = _customer_view(row, trail)
    return HTMLResponse(
        _STATUS_PAGE(case_id, view["heading"], view["reason"], view["next_step"])
    )


def _STATUS_PAGE(case_id: str, heading: str, reason: str, next_step: str) -> str:
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Payment status</title>
<style>{_DASH_CSS} .status{{max-width:560px;margin:28px auto}}
</style></head><body>
<div class="status">
  <h1>{html.escape(heading)}</h1>
  <p class="sub">Reference: {html.escape(case_id)}</p>
  <p>{html.escape(reason)}</p>
  <p><strong>What happens next:</strong> {html.escape(next_step)}</p>
  <p class="sub">Need help? Contact your service provider. (This is a demo view of your
  payment recovery status.)</p>
</div>
</body></html>"""



@app.get("/metrics")
def metrics() -> JSONResponse:
    from .metrics import compute_metrics

    return JSONResponse(compute_metrics(get_db_dep(), get_settings_dep()))


# ---------------------------------------------------------------------------
# 3.6 Policy-as-code: /rules — the active stopping rules in plain language
# ---------------------------------------------------------------------------
# The stopping rules are a declarative registry (see stopping_rules.py), so the
# policy itself is an auditable, introspectable artifact. This page renders what
# IS currently enforced (id, priority, forced action, plain-English statement
# with the live threshold values) — not just the outputs of the rules.


def _RULES_PAGE(settings: Settings, rules: list[dict[str, object]]) -> str:
    rows = "".join(
        f'<tr><td class="rid">{html.escape(str(r["rule_id"]))}</td>'
        f'<td class="prio">{r["priority"]}</td>'
        f'<td class="act">{html.escape(str(r["action"]))}</td>'
        f'<td class="desc">{html.escape(str(r["description"]))}</td></tr>'
        for r in rules
    )
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Reclaim — Active stopping rules</title>
<style>{_DASH_CSS} .rules{{width:100%;border-collapse:collapse}}
.rules th,.rules td{{text-align:left;padding:8px 12px;border-bottom:1px solid #e2e8f0;font-size:13px}}
.rules th{{color:#475569;font-size:12px;text-transform:uppercase;letter-spacing:.04em}}
.rid{{font-weight:700;color:#7c3aed}}
.prio{{color:#64748b;text-align:center}}
.act{{font-weight:600}}
</style></head><body>
<h1>Reclaim — Active stopping rules (policy-as-code)</h1>
<p class="sub">These rules are enforced in code over every LLM proposal. They are
expressed declaratively and rendered here in plain language so the policy itself
is an auditable artifact. A rule INSERT (first matching rule wins) overrides the
LLM's proposal.</p>
<p class="back"><a href="/dashboard">&larr; back to dashboard</a></p>
<div class="card"><table class="rules"><thead><tr>
<th>Rule</th><th>Priority</th><th>Forced action</th><th>Policy (live values)</th>
</tr></thead><tbody>{rows}</tbody></table></div>
</body></html>"""


@app.get("/rules", response_class=HTMLResponse)
def rules_page() -> HTMLResponse:
    """Render the active policy as plain language (auditable rules artifact)."""
    from .stopping_rules import describe_rules

    settings = get_settings_dep()
    return HTMLResponse(_RULES_PAGE(settings, describe_rules(settings)))


# ---------------------------------------------------------------------------
# B1: Rule Sensitivity Simulator — /simulator
# ---------------------------------------------------------------------------
# Lets an operator re-run the SAME seeded synthetic batch (seed=42) under a
# set of on-the-fly threshold overrides and compare the resulting metrics with
# the current settings. It deliberately REUSES pipeline.run_batch and
# metrics.compute_metrics as-is — nothing is duplicated, and the real settings
# are never mutated (overrides are passed through on a throwaway Settings copy
# pointed at a throwaway DB).

# _SIM_THRESHOLD_FIELDS / _run_simulated_batch / _sim_metric_key moved to
# api_views.py (shared with the JSON API so both surfaces cannot drift). They
# are re-imported at the top of this module so the /simulator HTML route and
# tests/test_simulator.py keep working unchanged.


@app.get("/simulator", response_class=HTMLResponse)
def simulator_form() -> HTMLResponse:
    settings = get_settings_dep()
    return HTMLResponse(_SIMULATOR_PAGE(settings, baseline=None, simulated=None, error=None))


@app.post("/simulator", response_class=HTMLResponse)
async def simulator_run(request: Request) -> HTMLResponse:
    settings = get_settings_dep()
    form = await request.form()
    overrides: dict[str, object] = {}
    error: str | None = None
    # Build overrides from the submitted values, coercing to the field's type.
    for f in _SIM_THRESHOLD_FIELDS:
        raw = form.get(f)
        if raw is None or str(raw).strip() == "":
            continue
        current = getattr(settings, f)
        try:
            if isinstance(current, bool):
                overrides[f] = str(raw).strip().lower() in ("1", "true", "yes", "on")
            else:
                num = float(str(raw).strip())
                overrides[f] = int(num) if isinstance(current, int) else num
        except (TypeError, ValueError):
            error = f"Could not parse a number for '{f}'."
            break

    baseline: dict[str, object] | None = None
    simulated: dict[str, object] | None = None
    if error is None:
        try:
            baseline = _run_simulated_batch(settings, {})  # current thresholds
            simulated = _run_simulated_batch(settings, overrides)  # proposed
        except Exception as exc:  # never let a simulation crash the page
            logger.error("SIMULATOR_ERROR err=%s", exc)
            error = f"Simulation failed: {type(exc).__name__}: {exc}"
    return HTMLResponse(_SIMULATOR_PAGE(settings, baseline, simulated, error))


def _sim_threshold_inputs(settings: Settings) -> str:
    """Render the editable threshold inputs, prefilled with current values."""
    rows: list[str] = []
    for f in _SIM_THRESHOLD_FIELDS:
        val = getattr(settings, f)
        step = "1" if isinstance(val, int) else "0.01"
        rows.append(
            f'<label><span class="sim-label">{html.escape(f)}</span>'
            f'<input type="number" name="{html.escape(f)}" value="{html.escape(str(val))}" '
            f'step="{step}" class="sim-input"></label>'
        )
    return "".join(rows)


def _sim_comparison(baseline: dict[str, object], simulated: dict[str, object]) -> str:
    rows_html: list[str] = []
    for label, bval in _sim_metric_key(baseline):
        sval = dict(_sim_metric_key(simulated))[label]
        changed = bval != sval
        cls = " class='chg'" if changed else ""
        rows_html.append(
            f"<tr><td>{html.escape(label)}</td>"
            f"<td>{html.escape(bval)}</td>"
            f"<td{cls}>{html.escape(sval)}</td></tr>"
        )
    return "".join(rows_html)


def _SIMULATOR_PAGE(
    settings: Settings,
    baseline: dict[str, object] | None,
    simulated: dict[str, object] | None,
    error: str | None,
) -> str:
    err_html = f'<p class="sim-error">{html.escape(error)}</p>' if error else ""
    result_html = ""
    if baseline is not None and simulated is not None:
        result_html = (
            '<div class="card"><h3 class="case-title">Before / After comparison</h3>'
            '<p class="sub">Both columns run the same seed-42 synthetic batch — '
            'left is current thresholds, right is your simulated thresholds.</p>'
            '<table class="sim-table"><thead><tr>'
            '<th>Metric</th><th>Current thresholds</th><th>Simulated thresholds</th>'
            "</tr></thead><tbody>"
            f"{_sim_comparison(baseline, simulated)}"
            "</tbody></table></div>"
        )
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Reclaim — Rule Sensitivity Simulator</title>
<style>{_DASH_CSS} .sim-form{{display:flex;flex-direction:column;gap:8px;max-width:420px}}
.sim-label{{font-size:12.5px;color:#475569;margin-bottom:2px;display:block}}
.sim-input{{padding:6px 8px;border:1px solid #cbd5e1;border-radius:6px;width:100%}}
.chg{{color:#166534;font-weight:700}}
.sim-table{{width:100%;border-collapse:collapse;margin-top:8px}}
.sim-table th,.sim-table td{{text-align:left;padding:7px 10px;border-bottom:1px solid #e2e8f0;font-size:13px}}
.sim-table th{{color:#475569;font-size:12px;text-transform:uppercase;letter-spacing:.04em}}
.sim-error{{color:#991b1b;font-weight:600}}
</style></head><body>
<h1>Reclaim — Rule Sensitivity Simulator</h1>
<p class="sub">Tune the stopping-rule thresholds and re-run the same seed-42 synthetic batch
(before/after comparison against current thresholds). No settings are mutated; each run uses a
throwaway database.</p>
<p class="back"><a href="/dashboard">&larr; back to dashboard</a></p>
{err_html}
<div class="card">
  <h3 class="case-title">Stopping-rule thresholds</h3>
  <form method="post" action="/simulator" class="sim-form">
    {_sim_threshold_inputs(settings)}
    <button type="submit" style="margin-top:10px">Run simulation</button>
  </form>
</div>
{result_html}
</body></html>"""


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