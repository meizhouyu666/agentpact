import { describe, it, expect } from "vitest";
import { render } from "@testing-library/react";
import { GlassCard } from "@/components/enterprise/GlassCard";

describe("GlassCard component", () => {
  it("renders children", () => {
    const { getByText } = render(<GlassCard>Hello</GlassCard>);
    expect(getByText("Hello")).not.toBeNull();
  });

  it("applies glass-card class when hoverable (default)", () => {
    const { container } = render(<GlassCard>Content</GlassCard>);
    const card = container.firstChild as HTMLElement;
    expect(card.classList.contains("glass-card")).toBe(true);
  });

  it("applies glass-card-static class when not hoverable", () => {
    const { container } = render(<GlassCard hoverable={false}>Content</GlassCard>);
    const card = container.firstChild as HTMLElement;
    expect(card.classList.contains("glass-card-static")).toBe(true);
  });

  it("applies padding classes correctly", () => {
    const { container } = render(<GlassCard padding="lg">Content</GlassCard>);
    const card = container.firstChild as HTMLElement;
    expect(card.classList.contains("p-8")).toBe(true);
  });

  it("adds cursor-pointer when onClick is provided", () => {
    const { container } = render(<GlassCard onClick={() => {}}>Click</GlassCard>);
    const card = container.firstChild as HTMLElement;
    expect(card.classList.contains("cursor-pointer")).toBe(true);
  });
});
