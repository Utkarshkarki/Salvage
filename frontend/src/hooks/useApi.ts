/**
 * TanStack Query hooks over the /api/v1 endpoints.
 *
 * Why React Query (and not hand-rolled useEffect/useState fetching or a
 * heavyweight store): this is *server state* — caching, request
 * deduplication, loading/error states, and, critically for the override
 * buttons, automatic refetch of a detail after a successful mutation
 * (invalidate the "case" key, and the list re-queries too). That is exactly
 * what the library gives for free and what B5.2's optimistic-but-verified
 * flow needs. It is deliberately NOT a client-state store (no Redux), which
 * would be over-engineering for a read-mostly tool.
 */

import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  approveRetry,
  fetchCaseDetail,
  fetchCases,
  fetchCustomerStatus,
  fetchMetrics,
  fetchRules,
  resolveHuman,
  type ListCasesParams,
} from "../api/endpoints";

export const QUERY_KEYS = {
  cases: (params: ListCasesParams) => ["cases", params] as const,
  caseDetail: (caseId: string) => ["case", caseId] as const,
  metrics: () => ["metrics"] as const,
  rules: () => ["rules"] as const,
  customerStatus: (caseId: string) => ["customer-status", caseId] as const,
} as const;

export function useCases(params: ListCasesParams) {
  return useQuery({
    queryKey: QUERY_KEYS.cases(params),
    queryFn: ({ signal }) => fetchCases(params, signal),
    // Keep the previous page's rows visible while a new page loads, instead of
    // flashing a full skeleton on every pagination click.
    placeholderData: keepPreviousData,
  });
}

export function useCaseDetail(caseId: string) {
  return useQuery({
    queryKey: QUERY_KEYS.caseDetail(caseId),
    queryFn: ({ signal }) => fetchCaseDetail(caseId, signal),
  });
}

export function useMetrics() {
  return useQuery({
    queryKey: QUERY_KEYS.metrics(),
    queryFn: ({ signal }) => fetchMetrics(signal),
  });
}

export function useRules() {
  return useQuery({
    queryKey: QUERY_KEYS.rules(),
    queryFn: ({ signal }) => fetchRules(signal),
  });
}

export function useCustomerStatus(caseId: string) {
  return useQuery({
    queryKey: QUERY_KEYS.customerStatus(caseId),
    queryFn: ({ signal }) => fetchCustomerStatus(caseId, signal),
  });
}

/** A6 override actions — invalidate the case on success so the UI re-queries. */
export function useManualAction(caseId: string) {
  const queryClient = useQueryClient();
  const invalidateCase = () => {
    void queryClient.invalidateQueries({ queryKey: QUERY_KEYS.caseDetail(caseId) });
    void queryClient.invalidateQueries({ queryKey: ["cases"] });
  };

  return {
    approveRetry: useMutation({
      mutationFn: () => approveRetry(caseId),
      onSuccess: invalidateCase,
    }),
    resolveHuman: useMutation({
      mutationFn: () => resolveHuman(caseId),
      onSuccess: invalidateCase,
    }),
  };
}
