import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import "@testing-library/jest-dom/vitest";
import { afterEach, describe, expect, it, vi } from "vitest";
import { AgentRunsPage } from "./AgentRunsPage";

const projection = {
  run_id: "run_m10_test",
  pack_id: "synthetic.payment",
  pack_version: "1.0.0",
  pack_display_name: "Synthetic Payment Reference Pack",
  provider_mode: "recorded",
  state: "AWAITING_APPROVAL",
  legal_actions: ["approve", "reject"],
  plan: [
    { sequence: 1, role: "precheck", state: "completed" },
    { sequence: 2, role: "submit", state: "active" },
    { sequence: 3, role: "confirm", state: "pending" },
  ],
  completed_steps: 1,
  total_steps: 3,
};

function response(body: unknown) {
  return Promise.resolve(new Response(JSON.stringify(body), { status: 200 }));
}

afterEach(() => vi.restoreAllMocks());

describe("AgentRunsPage", () => {
  it("renders server-declared legal actions, redacted plan, and timeline", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation((input) => {
      const url = String(input);
      if (url.endsWith("/events")) {
        return response([{ sequence: 1, stage: "approval_required", state: "AWAITING_APPROVAL" }]);
      }
      return response(projection);
    });

    render(<AgentRunsPage />);
    fireEvent.change(screen.getByLabelText("Request ID"), { target: { value: "req-1" } });
    fireEvent.change(screen.getByLabelText("Intent"), { target: { value: "Submit the payment" } });
    fireEvent.change(screen.getByLabelText("Trusted business inputs"), {
      target: { value: '{"payment_id":"secret-value"}' },
    });
    fireEvent.click(screen.getByText("Create governed run"));

    await waitFor(() => expect(screen.getByTestId("run-state").textContent).toBe("AWAITING_APPROVAL"));
    expect(screen.getByRole("button", { name: "approve" })).not.toBeDisabled();
    expect(screen.getByRole("button", { name: "probe" })).toBeDisabled();
    expect(screen.getByText("Synthetic Payment Reference Pack")).toBeTruthy();
    expect(screen.getByText("recorded")).toBeTruthy();
    expect(screen.getByText("1. approval_required — AWAITING_APPROVAL")).toBeTruthy();
    expect(screen.queryByText("secret-value")).toBeNull();
    expect(fetchMock).toHaveBeenCalled();
  });

  it("exposes UNKNOWN as probe-only and shows terminal report download", async () => {
    const unknown = { ...projection, state: "UNKNOWN", legal_actions: ["probe"] };
    const succeeded = { ...projection, state: "SUCCEEDED", legal_actions: [] };
    let runReads = 0;
    vi.spyOn(globalThis, "fetch").mockImplementation((input, init) => {
      const url = String(input);
      if (url.endsWith("/events")) return response([]);
      if (url.endsWith("/probe") && init?.method === "POST") return response(succeeded);
      if (url.includes("run_m10_test")) return response(runReads++ === 0 ? unknown : succeeded);
      return response(unknown);
    });

    render(<AgentRunsPage />);
    fireEvent.change(screen.getByLabelText("Request ID"), { target: { value: "req-2" } });
    fireEvent.change(screen.getByLabelText("Intent"), { target: { value: "Submit" } });
    fireEvent.change(screen.getByLabelText("Trusted business inputs"), { target: { value: "{}" } });
    fireEvent.click(screen.getByText("Create governed run"));

    await waitFor(() => expect(screen.getByRole("button", { name: "probe" })).not.toBeDisabled());
    fireEvent.click(screen.getByRole("button", { name: "probe" }));
    await waitFor(() => expect(screen.getByText("Download redacted report")).toBeTruthy());
  });
});
