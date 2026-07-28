import { describe, it, expect } from "vitest";
import { render } from "@testing-library/react";
import { RiskBadge } from "@/components/enterprise/RiskBadge";

describe("RiskBadge component", () => {
  it("renders Low badge", () => {
    const { getByText } = render(<RiskBadge level="low" />);
    expect(getByText("Low")).not.toBeNull();
  });

  it("renders Medium badge", () => {
    const { getByText } = render(<RiskBadge level="medium" />);
    expect(getByText("Medium")).not.toBeNull();
  });

  it("renders High badge", () => {
    const { getByText } = render(<RiskBadge level="high" />);
    expect(getByText("High")).not.toBeNull();
  });

  it("renders Critical badge", () => {
    const { getByText } = render(<RiskBadge level="critical" />);
    expect(getByText("Critical")).not.toBeNull();
  });

  it("falls back to raw level for unknown values", () => {
    const { getByText } = render(<RiskBadge level="unknown_level" />);
    expect(getByText("unknown_level")).not.toBeNull();
  });

  it("applies glass-badge class", () => {
    const { container } = render(<RiskBadge level="high" />);
    const badge = container.firstChild as HTMLElement;
    expect(badge.classList.contains("glass-badge")).toBe(true);
  });
});
