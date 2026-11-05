// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

// tolerant word-by-word quote match in a text layer (footnotes, ligatures, case ignored)

export interface TextItemLike {
  str?: string;
  // pdf.js text geometry, in PDF units: [a, b, c, d, x, y], and the run's width/height
  transform?: number[];
  width?: number;
  height?: number;
}

// a span of one text item to highlight (offsets into its `str`), with its words at issue
export interface HitRange {
  item: number;
  start: number;
  end: number;
  strong?: { start: number; end: number }[];
}

interface Word {
  item: number;
  start: number;
  end: number;
  norm: string;
}

// share of the quote's words that must be found, in order, for a match
const MIN_MATCHED = 0.6;
// how much longer than the quote the matched span may be, in words
const SPAN_SLACK = 1.5;

const MARKERS_RE = /[¹²³⁰-₟†‡§¶‖*®™©]/g;

export function normalizeWord(word: string): string {
  return word
    .replace(MARKERS_RE, "")
    .normalize("NFKC")
    .toLowerCase()
    .replace(/[‘’‚‛]/g, "'")
    .replace(/[‐-―]/g, "-")
    .replace(/^[^a-z0-9~<>≥≤+]+|[^a-z0-9%+]+$/g, "");
}

function sameWord(a: string, b: string): boolean {
  if (a === b) return true;
  // e.g. "hba1c" vs "hba1c1": a footnote number the text layer runs into the word
  return (
    Math.min(a.length, b.length) >= 3 && (a.startsWith(b) || b.startsWith(a))
  );
}

function pageWords(items: TextItemLike[]): Word[] {
  const words: Word[] = [];
  items.forEach((item, idx) => {
    const str = item.str ?? "";
    for (const m of str.matchAll(/\S+/g)) {
      const norm = normalizeWord(m[0]);
      if (norm)
        words.push({
          item: idx,
          start: m.index ?? 0,
          end: (m.index ?? 0) + m[0].length,
          norm,
        });
    }
  });
  return words;
}

export function quoteWords(quote: string): string[] {
  return quote
    .split(/\s+/)
    .map(normalizeWord)
    .filter((w) => w);
}

// marks each span's first occurrence among the matched words as strong
function markSpans(
  words: Word[],
  first: number,
  last: number,
  spans: string[],
  ranges: Map<number, HitRange>,
) {
  for (const span of spans) {
    const s = quoteWords(span);
    if (s.length === 0) continue;
    for (let p = first; p + s.length - 1 <= last; p++) {
      if (!s.every((w, k) => sameWord(words[p + k].norm, w))) continue;
      for (let k = 0; k < s.length; k++) {
        const w = words[p + k];
        const range = ranges.get(w.item);
        if (!range) continue;
        const strong = (range.strong ??= []);
        const prev = strong[strong.length - 1];
        if (k > 0 && prev && words[p + k - 1].item === w.item) prev.end = w.end;
        else strong.push({ start: w.start, end: w.end });
      }
      break;
    }
  }
}

// text-layer ranges covering the quote here, or null; words at issue marked `strong`
export function locateQuote(
  items: TextItemLike[],
  quote: string,
  spans: string[] = [],
): HitRange[] | null {
  const q = quoteWords(quote);
  const words = pageWords(items);
  if (q.length === 0 || words.length === 0) return null;
  const maxSpan = Math.ceil(q.length * SPAN_SLACK) + 2;

  let best: { matched: number; first: number; last: number } | null = null;
  for (let i = 0; i < words.length; i++) {
    if (
      !sameWord(words[i].norm, q[0]) &&
      !(q.length > 1 && sameWord(words[i].norm, q[1]))
    )
      continue;
    let j = sameWord(words[i].norm, q[0]) ? 0 : 1;
    let p = i;
    let matched = 0;
    let last = i;
    while (p < words.length && p < i + maxSpan && j < q.length) {
      if (sameWord(words[p].norm, q[j])) {
        matched++;
        last = p;
        j++;
        p++;
      } else if (j + 1 < q.length && sameWord(words[p].norm, q[j + 1])) {
        j++;
      } else {
        p++;
      }
    }
    const better =
      !best ||
      matched > best.matched ||
      (matched === best.matched && last - i < best.last - best.first);
    if (better) best = { matched, first: i, last };
    if (matched === q.length && last - i === q.length - 1) break;
  }
  if (!best || best.matched / q.length < MIN_MATCHED) return null;

  const ranges = new Map<number, HitRange>();
  for (let p = best.first; p <= best.last; p++) {
    const w = words[p];
    const range = ranges.get(w.item);
    if (range) {
      range.start = Math.min(range.start, w.start);
      range.end = Math.max(range.end, w.end);
    } else {
      ranges.set(w.item, { item: w.item, start: w.start, end: w.end });
    }
  }
  markSpans(words, best.first, best.last, spans, ranges);
  return [...ranges.values()];
}

function escapeHtml(text: string): string {
  return text
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

export const HIT_CLASS = "pdf-quote-hit";
export const STRONG_CLASS = "pdf-quote-strong";

// text-layer HTML for one item: hit range in <mark>, words at issue in a nested <mark>
export function renderItemWithHit(str: string, range?: HitRange): string {
  if (!range) return escapeHtml(str);
  let inner = "";
  let at = range.start;
  const strong = [...(range.strong ?? [])].sort((a, b) => a.start - b.start);
  for (const s of strong) {
    const start = Math.max(at, s.start);
    const end = Math.min(range.end, s.end);
    if (end <= start) continue;
    inner +=
      escapeHtml(str.slice(at, start)) +
      `<mark class="${STRONG_CLASS}">` +
      escapeHtml(str.slice(start, end)) +
      "</mark>";
    at = end;
  }
  inner += escapeHtml(str.slice(at, range.end));
  return (
    escapeHtml(str.slice(0, range.start)) +
    `<mark class="${HIT_CLASS}">` +
    inner +
    "</mark>" +
    escapeHtml(str.slice(range.end))
  );
}

// hit's horizontal extent and tallest font (PDF units), enough to pick a readable zoom
export interface HitBox {
  x0: number;
  x1: number;
  fontHeight: number;
}

export function hitBox(
  items: TextItemLike[],
  ranges: HitRange[],
): HitBox | null {
  let x0 = Infinity;
  let x1 = -Infinity;
  let fontHeight = 0;
  for (const r of ranges) {
    const item = items[r.item];
    const len = (item?.str ?? "").length;
    if (!item?.transform || !item.width || !len) continue;
    const x = item.transform[4];
    x0 = Math.min(x0, x + (item.width * r.start) / len);
    x1 = Math.max(x1, x + (item.width * r.end) / len);
    fontHeight = Math.max(
      fontHeight,
      item.height || Math.hypot(item.transform[2], item.transform[3]),
    );
  }
  return x1 > x0 ? { x0, x1, fontHeight } : null;
}

// zoom (1 = fit width) at which the hit is readable and fits 3/4 of the view
export function zoomForHit(
  box: HitBox,
  pageWidth: number,
  viewWidth: number,
  maxZoom: number,
  targetFontPx = 20,
  minFontPx = 14,
): number {
  const fontZoom = (px: number) =>
    box.fontHeight ? (px * pageWidth) / (box.fontHeight * viewWidth) : 1;
  const fill = (0.75 * pageWidth) / (box.x1 - box.x0);
  const zoom = Math.max(
    fontZoom(minFontPx),
    Math.min(fill, fontZoom(targetFontPx)),
  );
  return Math.max(1, Math.min(maxZoom, zoom));
}
