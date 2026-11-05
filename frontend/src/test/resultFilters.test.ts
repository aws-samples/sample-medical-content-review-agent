// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import { describe, it, expect } from "vitest";
import {
  type Facet,
  EMPTY_FILTERS,
  applyFilters,
  facetOptions,
  groupByQuote,
  selectOnly,
  sortCardsBySeverity,
  toggleValue,
} from "../components/chat/resultFilters";

interface Item {
  id: number;
  type: string;
  check: string;
  pages: number[];
  text: string;
  quote_group?: string;
}

const items: Item[] = [
  {
    id: 1,
    type: "mandatory",
    check: "library_deviation",
    pages: [1],
    text: "alpha",
    quote_group: "F1",
  },
  {
    id: 2,
    type: "optional",
    check: "substantiation",
    pages: [1],
    text: "alpha",
    quote_group: "F1",
  },
  {
    id: 3,
    type: "mandatory",
    check: "substantiation",
    pages: [2, 5],
    text: "beta",
    quote_group: "F3",
  },
  { id: 4, type: "optional", check: "editorial", pages: [3], text: "gamma" },
];

const facets: Facet<Item>[] = [
  {
    id: "type",
    label: "Type",
    values: (i) => [i.type],
    order: ["mandatory", "optional"],
  },
  { id: "check", label: "Check", values: (i) => [i.check] },
  { id: "page", label: "Page", values: (i) => i.pages.map(String) },
];

const text = (i: Item) => i.text;
const ids = (list: Item[]) => list.map((i) => i.id);

describe("result filters", () => {
  it("shows everything with no filter", () => {
    expect(ids(applyFilters(items, facets, EMPTY_FILTERS, text))).toEqual([
      1, 2, 3, 4,
    ]);
  });

  it("combines chips in one facet with OR", () => {
    let state = toggleValue(EMPTY_FILTERS, "check", "editorial");
    state = toggleValue(state, "check", "library_deviation");
    expect(ids(applyFilters(items, facets, state, text))).toEqual([1, 4]);
  });

  it("combines facets with AND, and search narrows further", () => {
    let state = toggleValue(EMPTY_FILTERS, "type", "mandatory");
    state = toggleValue(state, "check", "substantiation");
    expect(ids(applyFilters(items, facets, state, text))).toEqual([3]);
    expect(
      ids(applyFilters(items, facets, { ...state, search: "ALP" }, text)),
    ).toEqual([]);
  });

  it("matches a merged finding on any of its pages", () => {
    const state = toggleValue(EMPTY_FILTERS, "page", "5");
    expect(ids(applyFilters(items, facets, state, text))).toEqual([3]);
  });

  it("toggles a chip off again", () => {
    const state = toggleValue(
      toggleValue(EMPTY_FILTERS, "type", "optional"),
      "type",
      "optional",
    );
    expect(ids(applyFilters(items, facets, state, text))).toEqual([1, 2, 3, 4]);
  });

  it("selectOnly sets a single value and clears it on a second click", () => {
    const once = selectOnly(
      toggleValue(EMPTY_FILTERS, "type", "optional"),
      "type",
      "mandatory",
    );
    expect(once.selected.type).toEqual(["mandatory"]);
    expect(selectOnly(once, "type", "mandatory").selected.type).toEqual([]);
    expect(selectOnly(once, "type", null).selected.type).toEqual([]);
  });

  it("counts each facet's options over the other facets' filters", () => {
    const state = toggleValue(EMPTY_FILTERS, "type", "mandatory");
    const options = facetOptions(items, facets, state, text);
    const type = options.find((o) => o.facet.id === "type")!.options;
    expect(type.map((o) => [o.value, o.count, o.selected])).toEqual([
      ["mandatory", 2, true],
      ["optional", 2, false],
    ]);
    const check = options.find((o) => o.facet.id === "check")!.options;
    expect(Object.fromEntries(check.map((o) => [o.value, o.count]))).toEqual({
      editorial: 0,
      library_deviation: 1,
      substantiation: 1,
    });
    const page = options.find((o) => o.facet.id === "page")!.options;
    expect(page.map((o) => o.value)).toEqual(["1", "2", "3", "5"]);
  });

  it("groups findings on the same quote onto one card, in order", () => {
    const cards = groupByQuote(items);
    expect(cards.map((c) => ids(c.items))).toEqual([[1, 2], [3], [4]]);
  });

  it("hides a card whose findings are all filtered out", () => {
    const state = toggleValue(EMPTY_FILTERS, "check", "substantiation");
    const cards = groupByQuote(applyFilters(items, facets, state, text));
    expect(cards.map((c) => ids(c.items))).toEqual([[2], [3]]);
  });

  it("sorts cards by their worst finding, then page, and findings within a card", () => {
    const scored = [
      { id: 1, page: 1, score: 40, quote_group: "A" },
      { id: 2, page: 1, score: 80, quote_group: "A" },
      { id: 3, page: 2, score: 95, quote_group: "B" },
      { id: 4, page: 3, score: 80, quote_group: "C" },
    ];
    const cards = sortCardsBySeverity(groupByQuote(scored));
    expect(cards.map((c) => c.items.map((i) => i.id))).toEqual([
      [3],
      [2, 1],
      [4],
    ]);
  });
});
