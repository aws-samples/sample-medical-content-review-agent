// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import { describe, it, expect, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";

vi.mock("react-pdf", () => ({
  pdfjs: { GlobalWorkerOptions: {} },
  Document: ({ children }: { children: React.ReactNode }) => (
    <div>{children}</div>
  ),
  Page: () => null,
}));
vi.mock("react-pdf/dist/Page/TextLayer.css", () => ({}));

import PdfQuoteViewer from "../components/chat/PdfQuoteViewer";

// jsdom has no PointerEvent; a MouseEvent carries the coordinates the viewer reads
globalThis.PointerEvent ??=
  class extends MouseEvent {} as unknown as typeof PointerEvent;

globalThis.ResizeObserver ??= class {
  observe() {}
  disconnect() {}
  unobserve() {}
} as unknown as typeof ResizeObserver;

describe("PDF viewer hand tool", () => {
  it("moves the page when dragged, and not in text-selection mode", () => {
    const { container } = render(<PdfQuoteViewer url="blob:x" target={null} />);
    const pane = container.querySelector(".overflow-auto") as HTMLDivElement;
    pane.scrollLeft = 100;
    pane.scrollTop = 200;
    fireEvent.pointerDown(pane, { button: 0, clientX: 300, clientY: 300 });
    fireEvent.pointerMove(window, { clientX: 260, clientY: 250 });
    fireEvent.pointerUp(window);
    expect(pane.scrollLeft).toBe(140);
    expect(pane.scrollTop).toBe(250);

    fireEvent.click(screen.getByRole("button", { name: "Select text" }));
    fireEvent.pointerDown(pane, { button: 0, clientX: 300, clientY: 300 });
    fireEvent.pointerMove(window, { clientX: 200, clientY: 200 });
    fireEvent.pointerUp(window);
    expect(pane.scrollLeft).toBe(140);
    expect(screen.getByRole("button", { name: "Hand tool" })).toBeTruthy();
  });
});
