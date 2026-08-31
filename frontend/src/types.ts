/**
 * TypeScript mirror of the backend's Pydantic/enum vocabulary (models.py,
 * metrics.py, stopping_rules.py, api_v1.py). These MUST match the Python
 * source of truth exactly — the enum values here are the machine values used
 * by the API (lowercase for Cause/Action, uppercase for CaseState).
 *
 * Decision on codegen vs. hand-maintained:
 *   We hand-maintain these rather than generating from the FastAPI OpenAPI
 *   schema (openapi-typescript). Rationale: the surface is small (~10
 *   response shapes), stable, and already mirrors Python StrEnums whose values
 *   are the contract; a codegen step would add a build-time dependency and a
 *   chicken-and-egg requirement (the backend must be running to emit the
 *   schema) for zero current benefit. To keep drift honest, we keep the enums
 *   centralized here (single file) and the backend tests (test_api_v1.py)
 *   assert the wire shapes — which is where any drift would surface. If the API
 *   grows past a handful of endpoints, switching to openapi-typescript would be
 *   the right call.
 */

export type CaseState =
  | "INGESTED"
  | "DIAGNOSED"
  | "DECIDED"
  | "ACTING"
  | "RESOLVED"
  | "ESCALATED"
  | "FAILED";

export type Cause =
  | "insufficient_funds"
  | "card_expired"
  | "bank_timeout"
  | "do_not_honor"
  | "mandate_revoked"
  | "unknown";

export type Action =
  | "retry_now"
  | "retry_scheduled"
  | "request_payment_method_update"
  | "escalate_human"
  | "stop";

/** A1 — one row of the paginated case list. */
export interface RecoveryCaseSummary {
  case_id: string;
  state: CaseState;
  amount: number;
  customer_id: string;
  subscription_id: string;
  failure_reason: string;
  attempt_number: number;
  created_at: string | null;
}

export interface CaseListResponse {
  items: RecoveryCaseSummary[];
  count: number;
}

/** Provenance recorded when the LLM was consulted (input_state.llm_provenance). */
export interface LLMProvenance {
  model: string;
  prompt_version: string;
  prompt_hash: string;
  mode: "offline" | "online";
}

/** One immutable append-only audit row (A2 audit_trail entry). */
export interface AuditLogEntry {
  stage: string;
  agent_reasoning: string;
  input_state: Record<string, unknown>;
  decision: string | null;
  action_taken: string | null;
  outcome: string | null;
  fallback_triggered: boolean;
  timestamp: string | null;
}

/** A2 — full per-case detail with the audit trail. */
export interface CaseDetail {
  case_id: string;
  state: CaseState;
  amount: number;
  customer_id: string;
  fallback_any_stage: boolean;
  audit_trail: AuditLogEntry[];
}

/** A3 — full metrics shape from compute_metrics. */
export interface Metrics {
  total_cases: number;
  state_distribution: Record<string, number>;
  amount_at_risk: number;
  recovered_cases: number;
  recovered_amount: number;
  recovery_rate: number;
  cause_breakdown: Record<string, number>;
  llm_call_failures: number;
  llm_failure_cases: number;
  stopping_rule_overrides: number;
  stopping_rule_overrides_by_rule: Record<string, number>;
  rule_override_cases: number;
  stub_mode_actions: number;
  stub_mode_cases: number;
  cases_resolved_without_retry: number;
  stopped_cases: number;
  escalated_cases: number;
}

/** A4 — one stopping rule in the R1–R7 registry (live values interpolated). */
export interface RuleSpec {
  rule_id: string;
  priority: number;
  action: Action;
  description: string;
}

/** A5 — the editable threshold subset (mirrors _SIM_THRESHOLD_FIELDS). */
export interface SimulatorOverrides {
  escalation_amount_threshold?: number;
  escalation_days_threshold?: number;
  max_retries_per_cycle?: number;
  cooldown_hours?: number;
  email_cap_per_7d?: number;
}

/** A5 — before/after result of the simulator run. */
export interface SimulatorComparison {
  baseline: Metrics;
  simulated: Metrics;
}

/** A7 — plain-language customer-facing status view. */
export interface CustomerStatusView {
  heading: string;
  reason: string;
  next_step: string;
}
