// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import { describe, it, expect, vi } from "vitest";
import { fireEvent, render, screen, within } from "@testing-library/react";

vi.mock("react-pdf", () => ({
  pdfjs: { GlobalWorkerOptions: {} },
  Document: () => null,
  Page: () => null,
}));
vi.mock("react-pdf/dist/Page/TextLayer.css", () => ({}));

import {
  ReviewResultsPanel,
  type ReviewIssue,
} from "../components/chat/ReviewResultsPanel";

const base = { fix: "", reference: "", source: "", issue: "details" };
const issues: ReviewIssue[] = [
  {
    ...base,
    page: 1,
    quote: "Best drug ever",
    headline: "Superlative: 'Best'",
    issue_spans: ["Best"],
    type: "mandatory",
    score: 80,
    check: "editorial",
    quote_group: "F1",
  },
  {
    ...base,
    page: 1,
    quote: "Best drug ever",
    headline: "Unsupported claim",
    type: "mandatory",
    score: 75,
    check: "substantiation",
    quote_group: "F1",
  },
  {
    ...base,
    page: 2,
    quote: "Excelent safety",
    headline: "Misspelled 'Excelent'",
    type: "optional",
    score: 20,
    check: "editorial",
    quote_group: "F3",
  },
];

function renderPanel() {
  return render(
    <ReviewResultsPanel issues={issues} isLoading={false} onNewReview={() => {}} />,
  );
}

describe("result cards", () => {
  it("puts findings on the same quote on one card and hides optional notes by default", () => {
    renderPanel();
    expect(screen.getByText("Superlative: 'Best'")).toBeTruthy();
    expect(screen.getByText("Unsupported claim")).toBeTruthy();
    expect(screen.queryByText("Misspelled 'Excelent'")).toBeNull();
    expect(screen.getAllByText("Mark addressed")).toHaveLength(1);
    // the word at issue is marked in the quote
    expect(screen.getByText("Best", { selector: "mark" })).toBeTruthy();
  });

  it("moves a card to Addressed and back", () => {
    renderPanel();
    fireEvent.click(screen.getByText("Mark addressed"));
    expect(screen.queryByText("Superlative: 'Best'")).toBeNull();
    const show = screen.getByRole("group", { name: "Show" });
    fireEvent.click(within(show).getByText("Addressed 2"));
    expect(screen.getByText("Superlative: 'Best'")).toBeTruthy();
    fireEvent.click(screen.getByText("Reopen"));
    fireEvent.click(within(show).getByText("Open 3"));
    expect(screen.getByText("Superlative: 'Best'")).toBeTruthy();
  });

  it("searches across findings and clears the type filter", () => {
    renderPanel();
    fireEvent.click(screen.getByRole("button", { name: /Filters/ }));
    fireEvent.click(screen.getByRole("button", { name: /^Optional/ }));
    expect(screen.getByText("Misspelled 'Excelent'")).toBeTruthy();
    fireEvent.change(screen.getByLabelText("Search findings"), {
      target: { value: "excelent" },
    });
    expect(screen.queryByText("Superlative: 'Best'")).toBeNull();
    expect(screen.getByText("Misspelled 'Excelent'")).toBeTruthy();
  });
});
