// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import { useState } from "react";
import { ChevronDown, SlidersHorizontal } from "lucide-react";
import {
  type Facet,
  type FilterState,
  EMPTY_FILTERS,
  facetOptions,
  hasActiveFilters,
  toggleValue,
} from "./resultFilters";

// Pinned to the top of the results pane: the search and the count stay visible while
// the list scrolls, and the chips open and close on demand
export function ResultFilterBar<T>({
  items,
  facets,
  state,
  onChange,
  searchText,
  searchPlaceholder,
  shown,
}: {
  items: T[];
  facets: Facet<T>[];
  state: FilterState;
  onChange: (state: FilterState) => void;
  searchText: (item: T) => string;
  searchPlaceholder: string;
  // How many items are shown after filtering, and of how many
  shown: { count: number; total: number; noun: string };
}) {
  const [open, setOpen] = useState(false);
  const groups = facetOptions(items, facets, state, searchText).filter(
    // a facet with a single value cannot narrow anything, unless it is in use
    ({ options }) =>
      options.length > 1 || options.some((option) => option.selected),
  );
  const activeChips = Object.values(state.selected).reduce(
    (n, values) => n + values.length,
    0,
  );
  return (
    // -top-4 / -mx-4 cancel the pane's padding, so the bar sticks to its very top
    <div className="sticky -top-4 z-10 -mx-4 mb-3 px-4 pt-4 pb-2 bg-white border-b border-gray-200 shadow-sm">
      <div className="flex items-center gap-2">
        <input
          type="search"
          value={state.search}
          onChange={(e) => onChange({ ...state, search: e.target.value })}
          placeholder={searchPlaceholder}
          aria-label={searchPlaceholder}
          className="flex-1 min-w-0 text-xs px-2 py-1 rounded border border-gray-300 bg-white text-gray-800"
        />
        <button
          type="button"
          aria-expanded={open}
          onClick={() => setOpen((o) => !o)}
          className={`flex items-center gap-1 text-[11px] font-semibold px-2 py-1 rounded border whitespace-nowrap ${
            activeChips > 0
              ? "border-indigo-400 text-indigo-700 bg-indigo-50"
              : "border-gray-300 text-gray-700 bg-white hover:border-indigo-400"
          }`}
        >
          <SlidersHorizontal className="w-3.5 h-3.5" />
          Filters{activeChips > 0 ? ` · ${activeChips}` : ""}
          <ChevronDown
            className={`w-3.5 h-3.5 transition-transform ${open ? "rotate-180" : ""}`}
          />
        </button>
        <span className="text-[11px] text-gray-500 whitespace-nowrap">
          {shown.count === shown.total
            ? `${shown.total} ${shown.noun}`
            : `${shown.count} of ${shown.total} ${shown.noun}`}
        </span>
        {hasActiveFilters(state) && (
          <button
            type="button"
            onClick={() => onChange(EMPTY_FILTERS)}
            className="text-[11px] font-semibold text-indigo-600 hover:text-indigo-800 whitespace-nowrap"
          >
            Clear filters
          </button>
        )}
      </div>
      {open && (
        <div className="mt-2 space-y-1.5 max-h-56 overflow-y-auto">
          {groups.map(({ facet, options }) => (
            <div key={facet.id} className="flex items-start gap-1.5 flex-wrap">
              <span className="text-[10px] font-semibold text-gray-500 uppercase tracking-wide pt-0.5 w-20 shrink-0">
                {facet.label}
              </span>
              {options.map((option) => (
                <button
                  key={option.value}
                  type="button"
                  aria-pressed={option.selected}
                  onClick={() =>
                    onChange(toggleValue(state, facet.id, option.value))
                  }
                  className={`text-[10px] font-semibold px-2 py-0.5 rounded-full border transition-colors ${
                    option.selected
                      ? "bg-indigo-600 text-white border-indigo-600"
                      : option.count === 0
                        ? "bg-white text-gray-400 border-gray-200"
                        : "bg-white text-gray-700 border-gray-300 hover:border-indigo-400"
                  }`}
                >
                  {option.label}{" "}
                  <span
                    className={
                      option.selected ? "text-indigo-100" : "text-gray-400"
                    }
                  >
                    {option.count}
                  </span>
                </button>
              ))}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
