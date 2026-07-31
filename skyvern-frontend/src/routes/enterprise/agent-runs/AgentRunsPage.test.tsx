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

const summary = {
  ...projection,
  legal_actions: undefined,
  plan: undefined,
  created_at: "2026-07-31T01:00:00Z",
  modified_at: "2026-07-31T01:01:00Z",
};

function response(body: unknown, status = 200) {
  return Promise.resolve(new Response(JSON.stringify(body), { status }));
}

function installApi(run = projection, items = [summary]) {
  return vi.spyOn(globalThis, "fetch").mockImplementation((input) => {
    const url = String(input);
    if (url.includes("?limit=20")) return response({ items, next_cursor: null });
    if (url.endsWith("/events")) {
      return response([{ sequence: 1, stage: "approval_required", state: "AWAITING_APPROVAL" }]);
    }
    return response(run);
  });
}

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
  window.history.replaceState(null, "", "/enterprise/agent-runs");
});

describe("AgentRunsPage", () => {
  it("loads history, restores the URL selection, and renders only authoritative legal actions", async () => {
    window.history.replaceState(null, "", "/enterprise/agent-runs?run=run_m10_test");
    installApi();

    render(<AgentRunsPage />);

    await waitFor(() => expect(screen.getByTestId("run-state")).toHaveTextContent("AWAITING_APPROVAL"));
    expect(screen.getByRole("button", { name: "run_m10_test" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "approve" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "reject" })).toBeEnabled();
    expect(screen.queryByRole("button", { name: "probe" })).toBeNull();
    expect(screen.getByText("1. approval_required - AWAITING_APPROVAL")).toBeTruthy();
    expect(window.location.search).toBe("?run=run_m10_test");
    expect(screen.queryByText("secret-value")).toBeNull();
  });

  it("falls back to the newest visible run when the URL selection is absent", async () => {
    window.history.replaceState(null, "", "/enterprise/agent-runs?run=run_missing");
    installApi();

    render(<AgentRunsPage />);

    await waitFor(() => expect(screen.getByTestId("run-state")).toBeTruthy());
    expect(window.location.search).toBe("?run=run_m10_test");
  });

  it("stops polling a terminal selected run", async () => {
    vi.useFakeTimers();
    const terminal = { ...projection, state: "SUCCEEDED", legal_actions: [] };
    const fetchMock = installApi(terminal);

    render(<AgentRunsPage />);
    await vi.runAllTimersAsync();
    await waitFor(() => expect(screen.getByTestId("run-state")).toHaveTextContent("SUCCEEDED"));
    const calls = fetchMock.mock.calls.length;
    await vi.advanceTimersByTimeAsync(6000);

    expect(fetchMock.mock.calls.length).toBe(calls);
    expect(screen.getByRole("link", { name: "Download redacted report" })).toBeTruthy();
  });

  it("surfaces a redacted history error without selecting a run", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(() =>
      response({ detail: { code: "INVALID_CURSOR" } }, 422),
    );

    render(<AgentRunsPage />);

    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("INVALID_CURSOR"));
    expect(screen.queryByTestId("run-state")).toBeNull();
  });

  it("selects a newly created run and refreshes history", async () => {
    let listCalls = 0;
    vi.spyOn(globalThis, "fetch").mockImplementation((input, init) => {
      const url = String(input);
      if (url.includes("?limit=20")) {
        listCalls += 1;
        return response({ items: [summary], next_cursor: null });
      }
      if (init?.method === "POST") return response(projection);
      if (url.endsWith("/events")) return response([]);
      return response(projection);
    });

    render(<AgentRunsPage />);
    fireEvent.change(screen.getByLabelText("Request ID"), { target: { value: "req-1" } });
    fireEvent.change(screen.getByLabelText("Intent"), { target: { value: "Submit" } });
    fireEvent.change(screen.getByLabelText("Trusted business inputs"), { target: { value: "{}" } });
    fireEvent.click(screen.getByRole("button", { name: "Create run" }));

    await waitFor(() => expect(listCalls).toBeGreaterThan(1));
    expect(window.location.search).toBe("?run=run_m10_test");
  });
});
