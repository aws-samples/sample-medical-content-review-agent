// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

// facet filters: OR within a facet, AND across facets; text search narrows both tabs

export interface Facet<T> {
  id: string;
  label: string;
  // the item's values for this facet; null or [] when the facet does not apply
  values: (item: T) => string[] | null;
  valueLabels?: Record<string, string>;
  // display order of the values; values not listed follow, numerically then by name
  order?: string[];
}

export interface FilterState {
  selected: Record<string, string[]>;
  search: string;
}

export const EMPTY_FILTERS: FilterState = { selected: {}, search: "" };

export function hasActiveFilters(state: FilterState): boolean {
  return (
    state.search.trim() !== "" ||
    Object.values(state.selected).some((values) => values.length > 0)
  );
}

export function toggleValue(
  state: FilterState,
  facetId: string,
  value: string,
): FilterState {
  const current = state.selected[facetId] ?? [];
  const next = current.includes(value)
    ? current.filter((v) => v !== value)
    : [...current, value];
  return { ...state, selected: { ...state.selected, [facetId]: next } };
}

// show only `value` in `facetId`, or clear that facet when it is already the only one
export function selectOnly(
  state: FilterState,
  facetId: string,
  value: string | null,
): FilterState {
  const current = state.selected[facetId] ?? [];
  const only =
    value !== null && !(current.length === 1 && current[0] === value);
  return {
    ...state,
    selected: { ...state.selected, [facetId]: only ? [value] : [] },
  };
}

function matches<T>(
  item: T,
  facets: Facet<T>[],
  state: FilterState,
  searchText: (item: T) => string,
  skipFacet?: string,
): boolean {
  for (const facet of facets) {
    if (facet.id === skipFacet) continue;
    const wanted = state.selected[facet.id];
    if (!wanted || wanted.length === 0) continue;
    const values = facet.values(item) ?? [];
    if (!values.some((v) => wanted.includes(v))) return false;
  }
  const query = state.search.trim().toLowerCase();
  return !query || searchText(item).toLowerCase().includes(query);
}

export function applyFilters<T>(
  items: T[],
  facets: Facet<T>[],
  state: FilterState,
  searchText: (item: T) => string,
): T[] {
  return items.filter((item) => matches(item, facets, state, searchText));
}

export interface FacetOption {
  value: string;
  label: string;
  count: number;
  selected: boolean;
}

function compareValues(order: string[] | undefined) {
  return (a: string, b: string): number => {
    const ia = order ? order.indexOf(a) : -1;
    const ib = order ? order.indexOf(b) : -1;
    if (ia !== -1 || ib !== -1) {
      if (ia === -1) return 1;
      if (ib === -1) return -1;
      return ia - ib;
    }
    const na = Number(a);
    const nb = Number(b);
    if (!Number.isNaN(na) && !Number.isNaN(nb)) return na - nb;
    return a.localeCompare(b);
  };
}

// each facet's options, counted over items passing the other facets and the search
export function facetOptions<T>(
  items: T[],
  facets: Facet<T>[],
  state: FilterState,
  searchText: (item: T) => string,
): { facet: Facet<T>; options: FacetOption[] }[] {
  return facets.map((facet) => {
    const counts = new Map<string, number>();
    for (const item of items) {
      for (const value of new Set(facet.values(item) ?? [])) {
        if (!counts.has(value)) counts.set(value, 0);
      }
      if (!matches(item, facets, state, searchText, facet.id)) continue;
      for (const value of new Set(facet.values(item) ?? [])) {
        counts.set(value, (counts.get(value) ?? 0) + 1);
      }
    }
    const selected = state.selected[facet.id] ?? [];
    const options = [...counts.keys()]
      .sort(compareValues(facet.order))
      .map((value) => ({
        value,
        label: facet.valueLabels?.[value] ?? value,
        count: counts.get(value) ?? 0,
        selected: selected.includes(value),
      }));
    return { facet, options };
  });
}

export interface QuoteCard<T> {
  key: string;
  items: T[];
}

// findings sharing a quote_group (same normalized quote) share a card, first-seen order
export function groupByQuote<T extends { quote_group?: string }>(
  items: T[],
): QuoteCard<T>[] {
  const cards: QuoteCard<T>[] = [];
  const byKey = new Map<string, QuoteCard<T>>();
  items.forEach((item, idx) => {
    const key = item.quote_group || `#${idx}`;
    let card = byKey.get(key);
    if (!card) {
      card = { key, items: [] };
      byKey.set(key, card);
      cards.push(card);
    }
    card.items.push(item);
  });
  return cards;
}

interface Scored {
  score: number;
  page: number;
}

function bySeverity(a: Scored, b: Scored): number {
  return b.score - a.score || a.page - b.page;
}

// worst finding first, then earliest page; the findings within a card the same way
export function sortCardsBySeverity<T extends Scored>(
  cards: QuoteCard<T>[],
): QuoteCard<T>[] {
  const sorted = cards.map((card) => ({
    ...card,
    items: [...card.items].sort(bySeverity),
  }));
  return sorted.sort((a, b) => bySeverity(a.items[0], b.items[0]));
}
