// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import { describe, it, expect } from "vitest";
import {
  locateQuote,
  renderItemWithHit,
  zoomForHit,
} from "../components/chat/quoteLocator";

const items = [
  { str: "Neuroxalyn reduced HbA1c by 1.8%¹ at" },
  { str: "24 weeks versus placebo." },
  { str: "Talk to your doctor." },
];

describe("quote locator", () => {
  it("finds a quote across text items, ignoring footnote markers and case", () => {
    const ranges = locateQuote(
      items,
      "reduced HbA1c by 1.8% at 24 Weeks versus placebo",
    );
    expect(ranges?.map((r) => r.item)).toEqual([0, 1]);
    expect(items[0].str.slice(ranges![0].start, ranges![0].end)).toBe(
      "reduced HbA1c by 1.8%¹ at",
    );
  });

  it("tolerates a few words the transcription got wrong", () => {
    expect(
      locateQuote(items, "Neuroxalyn lowered HbA1c by 1.8% at 24 weeks"),
    ).not.toBeNull();
  });

  it("returns null when the quote is not on the page", () => {
    expect(locateQuote(items, "No interactions with other drugs")).toBeNull();
  });

  it("marks the words at issue inside the hit", () => {
    const ranges = locateQuote(items, "Neuroxalyn reduced HbA1c", ["reduced"]);
    const html = renderItemWithHit(items[0].str, ranges![0]);
    expect(html).toContain('<mark class="pdf-quote-strong">reduced</mark>');
    expect(html.startsWith('<mark class="pdf-quote-hit">')).toBe(true);
  });

  it("escapes text it renders", () => {
    expect(renderItemWithHit("p<0.05 & more")).toBe("p&lt;0.05 &amp; more");
  });

  it("zooms small print up and never below fit-width", () => {
    const small = zoomForHit({ x0: 0, x1: 100, fontHeight: 6 }, 600, 600, 6);
    const large = zoomForHit({ x0: 0, x1: 500, fontHeight: 40 }, 600, 600, 6);
    expect(small).toBeGreaterThan(2);
    expect(large).toBe(1);
  });
});
