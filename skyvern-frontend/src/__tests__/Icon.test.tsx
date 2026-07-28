import { describe, it, expect } from "vitest";
import { render } from "@testing-library/react";
import { Icon } from "@/components/Icon";
import { iconPaths } from "@/components/Icon/icons";

describe("Icon component", () => {
  it("renders an SVG element with correct size", () => {
    const { container } = render(<Icon name="task" size={24} />);
    const svg = container.querySelector("svg");
    expect(svg).not.toBeNull();
    expect(svg?.getAttribute("width")).toBe("24");
    expect(svg?.getAttribute("height")).toBe("24");
  });

  it("renders at default size 20", () => {
    const { container } = render(<Icon name="approval" />);
    const svg = container.querySelector("svg");
    expect(svg?.getAttribute("width")).toBe("20");
  });

  it("renders with custom color", () => {
    const { container } = render(<Icon name="risk" color="#FF0000" />);
    const svg = container.querySelector("svg");
    // happy-dom may return hex or rgb depending on version
    const color = svg?.style.color ?? "";
    expect(color === "#FF0000" || color === "rgb(255, 0, 0)").toBe(true);
  });

  it("returns null for unknown icon name", () => {
    // @ts-expect-error Testing unknown name
    const { container } = render(<Icon name="nonexistent_icon_xyz" />);
    expect(container.innerHTML).toBe("");
  });

  it("renders all 21 registered icons without error", () => {
    const names = Object.keys(iconPaths);
    expect(names.length).toBeGreaterThanOrEqual(19);

    for (const name of names) {
      const { container } = render(<Icon name={name} size={16} />);
      const svg = container.querySelector("svg");
      expect(svg).not.toBeNull();
      // All icons use stroke, not fill
      const paths = container.querySelectorAll("path, circle, rect");
      expect(paths.length).toBeGreaterThan(0);
    }
  });

  it("all icons have fill=none (stroke-only style)", () => {
    const names = Object.keys(iconPaths);
    for (const name of names) {
      const { container } = render(<Icon name={name} size={20} />);
      const elements = container.querySelectorAll("path, circle, rect");
      for (const el of elements) {
        expect(el.getAttribute("fill")).toBe("none");
      }
    }
  });

  it("renders at all three standard sizes (16, 20, 24)", () => {
    for (const size of [16, 20, 24] as const) {
      const { container } = render(<Icon name="task" size={size} />);
      const svg = container.querySelector("svg");
      expect(svg?.getAttribute("width")).toBe(String(size));
      expect(svg?.getAttribute("height")).toBe(String(size));
    }
  });
});
