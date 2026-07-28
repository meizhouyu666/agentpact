import { describe, it, expect } from "vitest";
import { render } from "@testing-library/react";
import { StatusBadge } from "@/components/enterprise/StatusBadge";

describe("StatusBadge component", () => {
  const statuses = [
    { status: "running", label: "Running" },
    { status: "completed", label: "Completed" },
    { status: "failed", label: "Failed" },
    { status: "pending_approval", label: "Pending Approval" },
    { status: "needs_human", label: "Needs Human" },
    { status: "paused", label: "Paused" },
    { status: "queued", label: "Queued" },
    { status: "timeout", label: "Timeout" },
  ];

  for (const { status, label } of statuses) {
    it(`renders "${label}" for status "${status}"`, () => {
      const { getByText } = render(<StatusBadge status={status} />);
      expect(getByText(label)).not.toBeNull();
    });
  }

  it("shows raw status for unknown values", () => {
    const { getByText } = render(<StatusBadge status="custom_status" />);
    expect(getByText("custom_status")).not.toBeNull();
  });

  it("has glass-badge class", () => {
    const { container } = render(<StatusBadge status="running" />);
    const badge = container.firstChild as HTMLElement;
    expect(badge.classList.contains("glass-badge")).toBe(true);
  });
});
