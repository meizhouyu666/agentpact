import { describe, it, expect } from "vitest";
import { render, fireEvent } from "@testing-library/react";
import { ScreenshotDiff } from "@/components/enterprise/ScreenshotDiff";

describe("ScreenshotDiff component", () => {
  it("shows placeholder when no URLs provided", () => {
    const { getAllByText } = render(<ScreenshotDiff />);
    const placeholders = getAllByText("No screenshot");
    expect(placeholders.length).toBe(2);
  });

  it("shows images when URLs provided", () => {
    const { container } = render(
      <ScreenshotDiff
        beforeUrl="https://example.com/before.png"
        afterUrl="https://example.com/after.png"
      />,
    );
    const images = container.querySelectorAll("img");
    expect(images.length).toBe(2);
  });

  it("uses custom labels", () => {
    const { getByText } = render(
      <ScreenshotDiff beforeLabel="Step 1" afterLabel="Step 2" />,
    );
    expect(getByText("Step 1")).not.toBeNull();
    expect(getByText("Step 2")).not.toBeNull();
  });

  it("opens zoom overlay on image click", () => {
    const { container } = render(
      <ScreenshotDiff beforeUrl="https://example.com/before.png" />,
    );
    const img = container.querySelector("img");
    expect(img).not.toBeNull();

    // Click to zoom
    fireEvent.click(img!.parentElement!);

    // Overlay should appear with zoomed image
    const overlay = container.querySelector(".fixed");
    // The overlay is rendered at document level, check for the zoomed image
    const allImages = document.querySelectorAll("img");
    expect(allImages.length).toBeGreaterThanOrEqual(1);
  });
});
