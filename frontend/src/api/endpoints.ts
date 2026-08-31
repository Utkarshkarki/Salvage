/**
 * Endpoint definitions for the /api/v1/* JSON namespace. Each function maps to
 * exactly one backend route; the response types come from ../types.ts so the
 * frontend and backend vocabularies cannot drift silently.
 */

import { apiClient } from "./client";
import type {
  CaseDetail,
  CaseListResponse,
  CustomerStatusView,
  Metrics,
  RuleSpec,
  SimulatorComparison,
  SimulatorOverrides,
} from "../types";

export interface ListCasesParams {
  state?: string;
  limit?: number;
  offset?: number;
}

/** A1 */
export function fetchCases(params: ListCasesParams = {}, signal?: AbortSignal) {
  const q = new URLSearchParams();
  if (params.state) q.set("state", params.state);
  q.set("limit", String(params.limit ?? 50));
  q.set("offset", String(params.offset ?? 0));
  const qs = q.toString();
  return apiClient.get<CaseListResponse>(`/api/v1/cases${qs ? `?${qs}` : ""}`, signal);
}

/** A2 */
export function fetchCaseDetail(caseId: string, signal?: AbortSignal) {
  return apiClient.get<CaseDetail>(`/api/v1/cases/${encodeURIComponent(caseId)}`, signal);
}

/** A3 */
export function fetchMetrics(signal?: AbortSignal) {
  return apiClient.get<Metrics>("/api/v1/metrics", signal);
}

/** A4 */
export function fetchRules(signal?: AbortSignal) {
  return apiClient.get<RuleSpec[]>("/api/v1/rules", signal);
}

/** A5 */
export function runSimulation(overrides: SimulatorOverrides) {
  return apiClient.post<SimulatorComparison>("/api/v1/simulator/run", overrides);
}

/** A6 — returns the updated CaseDetail. */
export function approveRetry(caseId: string) {
  return apiClient.post<CaseDetail>(`/api/v1/cases/${encodeURIComponent(caseId)}/approve_retry`);
}

/** A6 */
export function resolveHuman(caseId: string) {
  return apiClient.post<CaseDetail>(`/api/v1/cases/${encodeURIComponent(caseId)}/resolve_human`);
}

/** A7 */
export function fetchCustomerStatus(caseId: string, signal?: AbortSignal) {
  return apiClient.get<CustomerStatusView>(`/api/v1/status/${encodeURIComponent(caseId)}`, signal);
}
