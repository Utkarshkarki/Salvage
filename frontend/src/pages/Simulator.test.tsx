import { describe, expect, it, vi } from "vitest";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import Simulator from "./Simulator";
import type { SimulatorComparison, SimulatorOverrides } from "../types";

// Mock the A5 endpoint so we can assert the submitted payload shape and render
// the comparison without a real backend.
vi.mock("../api/endpoints", () => ({
  runSimulation: vi.fn(),
}));

import { runSimulation } from "../api/endpoints";

const BASELINE = {
  total_cases: 60,
  state_distribution: {},
  amount_at_risk: 192246,
  recovered_cases: 24,
  recovered_amount: 39776,
  recovery_rate: 0.207,
  cause_breakdown: {},
  llm_call_failures: 0,
  llm_failure_cases: 0,
  stopping_rule_overrides: 32,
  stopping_rule_overrides_by_rule: {},
  rule_override_cases: 32,
  stub_mode_actions: 55,
  stub_mode_cases: 55,
  cases_resolved_without_retry: 28,
  stopped_cases: 5,
  escalated_cases: 23,
} as const;

const SIMULATED = { ...BASELINE, escalated_cases: 40 } as const;

function renderPage() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <Simulator />
    </QueryClientProvider>,
  );
}

describe("Simulator", () => {
  it("submits the correct payload shape (only filled fields) on submit", async () => {
    const user = userEvent.setup();
    const mock = vi
      .mocked(runSimulation)
      .mockResolvedValue({ baseline: BASELINE, simulated: SIMULATED } satisfies SimulatorComparison);

    renderPage();
    const amountInput = screen.getByLabelText(/escalation amount threshold/i);
    await user.clear(amountInput);
    await user.type(amountInput, "1200");
    await user.click(screen.getByRole("button", { name: /run simulation/i }));

    const expected: SimulatorOverrides = { escalation_amount_threshold: 1200 };
    expect(mock).toHaveBeenCalledTimes(1);
    expect(mock).toHaveBeenCalledWith(expected);
  });

  it("renders the before/after comparison on success", async () => {
    const user = userEvent.setup();
    vi.mocked(runSimulation).mockResolvedValue({
      baseline: BASELINE,
      simulated: SIMULATED,
    } satisfies SimulatorComparison);

    renderPage();
    await user.click(screen.getByRole("button", { name: /run simulation/i }));

    const heading = await screen.findByRole("heading", { name: /before \/ after comparison/i });
    expect(heading).toBeInTheDocument();

    // "Escalated (human)" row shows baseline 23 → simulated 40.
    const escalatedRow = screen.getByText("Escalated (human)").closest("li");
    expect(escalatedRow && within(escalatedRow as HTMLElement).getByText("40")).toBeInTheDocument();
  });
});
