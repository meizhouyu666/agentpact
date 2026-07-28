import { describe, it, expect } from "vitest";
import { render } from "@testing-library/react";
import { Timeline, type TimelineItem } from "@/components/enterprise/Timeline";

describe("Timeline component", () => {
  const items: TimelineItem[] = [
    {
      id: "1",
      title: "Step 1",
      description: "First action",
      timestamp: "10:00:00",
      status: "success",
    },
    {
      id: "2",
      title: "Step 2",
      description: "Second action",
      timestamp: "10:00:05",
      status: "error",
    },
    {
      id: "3",
      title: "Step 3",
      timestamp: "10:00:10",
      icon: "task",
      status: "info",
    },
  ];

  it("renders all timeline items", () => {
    const { getByText } = render(<Timeline items={items} />);
    expect(getByText("Step 1")).not.toBeNull();
    expect(getByText("Step 2")).not.toBeNull();
    expect(getByText("Step 3")).not.toBeNull();
  });

  it("shows descriptions when provided", () => {
    const { getAllByText } = render(<Timeline items={items} />);
    expect(getAllByText("First action").length).toBeGreaterThanOrEqual(1);
    expect(getAllByText("Second action").length).toBeGreaterThanOrEqual(1);
  });

  it("shows timestamps", () => {
    const { getAllByText } = render(<Timeline items={items} />);
    expect(getAllByText("10:00:00").length).toBeGreaterThanOrEqual(1);
    expect(getAllByText("10:00:05").length).toBeGreaterThanOrEqual(1);
  });

  it("renders icon when specified", () => {
    const { container } = render(<Timeline items={items} />);
    const svgs = container.querySelectorAll("svg");
    // Step 3 has an icon, so there should be at least one SVG
    expect(svgs.length).toBeGreaterThan(0);
  });

  it("renders empty timeline without error", () => {
    const { container } = render(<Timeline items={[]} />);
    expect(container.firstChild).not.toBeNull();
  });
});
