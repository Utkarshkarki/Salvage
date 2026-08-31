import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ApiError } from "../api/client";
import { ManualActions } from "./ManualActions";

// The component delegates to the useManualAction hook (which wires mutations
// to A6); we mock the hook to exercise the component's own logic: rendering
// buttons, disabling during pending, and surfacing 409 vs generic errors.
vi.mock("../hooks/useApi", () => ({
  useManualAction: vi.fn(),
}));

import { useManualAction } from "../hooks/useApi";

type MutationShape = ReturnType<typeof useManualAction>["approveRetry"];

// The component only consumes isPending + mutateAsync from the mutation result,
// so a minimal mock object is sufficient. Cast through `unknown` because the
// real UseMutationResult has many unrelated fields a mocked value won't have.
function makeMutation(
  overrides: Partial<{ isPending: boolean; mutateAsync: () => Promise<void> }> = {},
): MutationShape {
  return {
    isPending: false,
    isError: false,
    mutateAsync: vi.fn().mockResolvedValue(undefined),
    ...overrides,
  } as unknown as MutationShape;
}

function renderWithClient() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <ManualActions caseId="sub-abc" />
    </QueryClientProvider>,
  );
}

describe("ManualActions", () => {
  beforeEach(() => {
    vi.mocked(useManualAction).mockReturnValue({
      approveRetry: makeMutation(),
      resolveHuman: makeMutation(),
    });
  });

  it("renders both override action buttons", () => {
    renderWithClient();
    expect(screen.getByRole("button", { name: /approve manual retry/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /mark resolved by human/i })).toBeInTheDocument();
  });

  it("disables both buttons while a request is pending", () => {
    vi.mocked(useManualAction).mockReturnValue({
      approveRetry: makeMutation({ isPending: true }),
      resolveHuman: makeMutation({ isPending: true }),
    });
    renderWithClient();
    expect(screen.getByRole("button", { name: /approving retry/i })).toBeDisabled();
    expect(screen.getByRole("button", { name: /resolving/i })).toBeDisabled();
  });

  it("surface the distinct 409 case (already resolved elsewhere)", async () => {
    const user = userEvent.setup();
    const approve = makeMutation({
      mutateAsync: vi.fn().mockRejectedValue(new ApiError(409, "action not legal: ...")),
    });
    vi.mocked(useManualAction).mockReturnValue({
      approveRetry: approve,
      resolveHuman: makeMutation(),
    });
    renderWithClient();
    await user.click(screen.getByRole("button", { name: /approve manual retry/i }));

    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toMatch(/no longer being escalated/i);
    expect(alert.textContent).toMatch(/already resolved/i);
  });

  it("surfaces a generic failure message for non-409 errors", async () => {
    const user = userEvent.setup();
    const resolve = makeMutation({
      mutateAsync: vi.fn().mockRejectedValue(new Error("boom")),
    });
    vi.mocked(useManualAction).mockReturnValue({
      approveRetry: makeMutation(),
      resolveHuman: resolve,
    });
    renderWithClient();
    await user.click(screen.getByRole("button", { name: /mark resolved by human/i }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/mark resolved by human failed/i);
    expect(alert).toHaveTextContent(/boom/);
  });
});
