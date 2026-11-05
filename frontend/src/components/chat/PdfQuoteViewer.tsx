// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import type { PointerEvent as ReactPointerEvent } from "react";
import { Hand, Maximize2, Minus, Plus, TextCursor } from "lucide-react";
import { Document, Page, pdfjs } from "react-pdf";
import type { PDFDocumentProxy } from "pdfjs-dist";
import "react-pdf/dist/Page/TextLayer.css";
import {
  type HitRange,
  type TextItemLike,
  HIT_CLASS,
  STRONG_CLASS,
  hitBox,
  locateQuote,
  renderItemWithHit,
  zoomForHit,
} from "./quoteLocator";

pdfjs.GlobalWorkerOptions.workerSrc = new URL(
  "pdfjs-dist/build/pdf.worker.min.mjs",
  import.meta.url,
).toString();

export interface QuoteTarget {
  page: number;
  quote: string;
  // the words at issue within the quote, highlighted more strongly
  spans?: string[];
  // changes on every click, so clicking the same finding again goes back to it
  nonce: number;
}

const MIN_ZOOM = 0.5;
const MAX_ZOOM = 6;
const ZOOM_STEP = 1.25;
const PAGE_GAP = 8;
const PADDING = 8;
// canvas limits (32,767 px a side, ~268 MP): tall zoomed pages get a lower pixel ratio
const MAX_CANVAS_SIDE = 32000;
const MAX_CANVAS_AREA = 200_000_000;
// pages drawn beyond the visible ones, in viewport heights, so scrolling stays smooth
const OVERSCAN = 1;

interface Pending {
  nonce: number;
  zoom: number;
}

export default function PdfQuoteViewer({
  url,
  target,
}: {
  url: string;
  target: QuoteTarget | null;
}) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const [viewWidth, setViewWidth] = useState(0);
  const [viewHeight, setViewHeight] = useState(0);
  const [scrollTop, setScrollTop] = useState(0);
  const [zoom, setZoom] = useState(1);
  // size of every page in PDF units, read once the document loads
  const [sizes, setSizes] = useState<{ w: number; h: number }[]>([]);
  const [hits, setHits] = useState<{
    nonce: number;
    ranges: HitRange[] | null;
  } | null>(null);
  const textItems = useRef(new Map<number, TextItemLike[]>());
  // the scroll to make once the target page is drawn at the zoom chosen for it
  const pending = useRef<Pending | null>(null);
  // the target whose quote has been looked for, so each click is located once
  const located = useRef<number | null>(null);
  const zoomRef = useRef(zoom);
  zoomRef.current = zoom;

  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    const measure = () => {
      setViewWidth(el.clientWidth - 2 * PADDING);
      setViewHeight(el.clientHeight);
    };
    const observer = new ResizeObserver(measure);
    observer.observe(el);
    measure();
    return () => observer.disconnect();
  }, []);

  const pageWidth = viewWidth * zoom;
  const tops = useMemo(() => {
    const out: number[] = [];
    let y = PADDING;
    for (const size of sizes) {
      out.push(y);
      y += (pageWidth * size.h) / size.w + PAGE_GAP;
    }
    return out;
  }, [sizes, pageWidth]);
  const totalHeight =
    sizes.length > 0
      ? tops[tops.length - 1] +
        (pageWidth * sizes[sizes.length - 1].h) / sizes[sizes.length - 1].w +
        PADDING
      : 0;

  // keep the point at the center of the view in place when the zoom changes
  const prevZoom = useRef(zoom);
  useLayoutEffect(() => {
    const el = containerRef.current;
    const ratio = zoom / prevZoom.current;
    prevZoom.current = zoom;
    if (!el || ratio === 1) return;
    el.scrollLeft =
      (el.scrollLeft + el.clientWidth / 2) * ratio - el.clientWidth / 2;
    el.scrollTop =
      (el.scrollTop + el.clientHeight / 2) * ratio - el.clientHeight / 2;
  }, [zoom]);

  // ctrl + wheel (and trackpad pinch) zooms the document, not the whole app
  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    const onWheel = (e: WheelEvent) => {
      if (!e.ctrlKey) return;
      e.preventDefault();
      setZoom((z) =>
        Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, z * Math.exp(-e.deltaY * 0.002))),
      );
    };
    el.addEventListener("wheel", onWheel, { passive: false });
    return () => el.removeEventListener("wheel", onWheel);
  }, []);

  const scrollToPageTop = useCallback(
    (page: number) => {
      const el = containerRef.current;
      if (el && tops[page - 1] !== undefined) {
        el.scrollTop = tops[page - 1] - PADDING;
        el.scrollLeft = 0;
      }
    },
    [tops],
  );

  // locate the quote on a page whose text is known, then pick the zoom to show it at
  const locate = useCallback(
    (page: number) => {
      const items = textItems.current.get(page);
      const size = sizes[page - 1];
      if (!target || target.page !== page || !items || !size || !viewWidth)
        return;
      located.current = target.nonce;
      const ranges = locateQuote(items, target.quote, target.spans);
      setHits({ nonce: target.nonce, ranges });
      const box = ranges ? hitBox(items, ranges) : null;
      const next = box ? zoomForHit(box, size.w, viewWidth, MAX_ZOOM) : 1;
      pending.current = { nonce: target.nonce, zoom: next };
      setZoom(next);
    },
    [target, sizes, viewWidth],
  );

  // a new target: go to its page at once, so it is drawn and its text can be read
  const lastNonce = useRef<number | null>(null);
  useEffect(() => {
    if (!target || !sizes.length || lastNonce.current === target.nonce) return;
    lastNonce.current = target.nonce;
    scrollToPageTop(target.page);
    locate(target.page);
  }, [target, sizes, scrollToPageTop, locate]);

  // center the highlighted quote once its page is drawn at the chosen zoom
  const scrollToHit = useCallback(() => {
    const want = pending.current;
    const el = containerRef.current;
    if (!want || !target || want.nonce !== target.nonce || !el) return;
    if (Math.abs(zoomRef.current - want.zoom) > 1e-6) return;
    pending.current = null;
    const marks = el.querySelectorAll(
      `[data-page="${target.page}"] .${HIT_CLASS}`,
    );
    if (marks.length === 0) {
      scrollToPageTop(target.page);
      return;
    }
    const box = el.getBoundingClientRect();
    let top = Infinity;
    let bottom = -Infinity;
    let left = Infinity;
    let right = -Infinity;
    marks.forEach((m) => {
      const r = m.getBoundingClientRect();
      top = Math.min(top, r.top);
      bottom = Math.max(bottom, r.bottom);
      left = Math.min(left, r.left);
      right = Math.max(right, r.right);
    });
    const width = right - left;
    el.scrollTop += (top + bottom) / 2 - (box.top + box.height / 2);
    // a quote wider than the view starts at its left edge
    el.scrollLeft +=
      width > el.clientWidth
        ? left - box.left - 24
        : (left + right) / 2 - (box.left + box.width / 2);
  }, [target, scrollToPageTop]);

  const rangesByItem = useMemo(() => {
    const map = new Map<number, HitRange>();
    if (target && hits?.nonce === target.nonce)
      for (const r of hits.ranges ?? []) map.set(r.item, r);
    return map;
  }, [hits, target]);

  const renderTargetText = useCallback(
    ({ str, itemIndex }: { str: string; itemIndex: number }) =>
      renderItemWithHit(str, rangesByItem.get(itemIndex)),
    [rangesByItem],
  );

  const onDocumentLoad = async (pdf: PDFDocumentProxy) => {
    textItems.current.clear();
    const out: { w: number; h: number }[] = [];
    for (let n = 1; n <= pdf.numPages; n++) {
      const view = (await pdf.getPage(n)).getViewport({ scale: 1 });
      out.push({ w: view.width, h: view.height });
    }
    setSizes(out);
  };

  // only the pages in or near the view are drawn; the rest keep their place empty
  const drawn = new Set<number>();
  const lo = scrollTop - OVERSCAN * viewHeight;
  const hi = scrollTop + (1 + OVERSCAN) * viewHeight;
  sizes.forEach((size, i) => {
    const top = tops[i];
    const bottom = top + (pageWidth * size.h) / size.w;
    if (bottom >= lo && top <= hi) drawn.add(i + 1);
  });
  if (target) drawn.add(target.page);

  // hand tool: press and drag to move the page; off, the text can be selected
  const [hand, setHand] = useState(true);
  const [dragging, setDragging] = useState(false);
  const onPointerDown = (e: ReactPointerEvent<HTMLDivElement>) => {
    const el = containerRef.current;
    if (!hand || !el || e.button !== 0) return;
    e.preventDefault();
    const start = {
      x: e.clientX,
      y: e.clientY,
      left: el.scrollLeft,
      top: el.scrollTop,
    };
    setDragging(true);
    const move = (ev: PointerEvent) => {
      el.scrollLeft = start.left - (ev.clientX - start.x);
      el.scrollTop = start.top - (ev.clientY - start.y);
    };
    const up = () => {
      setDragging(false);
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
  };

  const zoomBy = (factor: number) =>
    setZoom((z) => Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, z * factor)));

  return (
    <div className="relative h-full">
      <style>{`.${HIT_CLASS}{background:rgba(250,204,21,0.45);color:transparent;border-radius:2px;outline:2px solid rgba(234,88,12,0.7)}.${STRONG_CLASS}{background:rgba(220,38,38,0.45);color:transparent;border-radius:2px;outline:2px solid rgba(185,28,28,0.9)}`}</style>
      <div
        ref={containerRef}
        onScroll={(e) => setScrollTop(e.currentTarget.scrollTop)}
        onPointerDown={onPointerDown}
        className={`h-full overflow-auto bg-gray-100 ${
          hand
            ? `select-none ${dragging ? "cursor-grabbing" : "cursor-grab"}`
            : ""
        }`}
      >
        <Document
          file={url}
          onLoadSuccess={onDocumentLoad}
          loading={<p className="text-gray-400 p-4">Loading document…</p>}
          error={
            <p className="text-red-600 p-4">The PDF could not be shown.</p>
          }
        >
          {viewWidth > 0 && (
            <div
              className="relative"
              style={{
                height: totalHeight,
                width: Math.max(viewWidth, pageWidth) + 2 * PADDING,
              }}
            >
              {sizes.map((size, i) => {
                const n = i + 1;
                const height = (pageWidth * size.h) / size.w;
                const isTarget = target?.page === n;
                const dpr = Math.max(
                  0.25,
                  Math.min(
                    window.devicePixelRatio || 1,
                    MAX_CANVAS_SIDE / Math.max(height, pageWidth),
                    Math.sqrt(MAX_CANVAS_AREA / (height * pageWidth)),
                  ),
                );
                return (
                  <div
                    key={n}
                    data-page={n}
                    className="absolute bg-white shadow"
                    style={{
                      top: tops[i],
                      left: PADDING + Math.max(0, (viewWidth - pageWidth) / 2),
                      width: pageWidth,
                      height,
                    }}
                  >
                    {drawn.has(n) && (
                      <Page
                        pageNumber={n}
                        width={pageWidth}
                        devicePixelRatio={dpr}
                        renderAnnotationLayer={false}
                        onGetTextSuccess={({ items }) => {
                          textItems.current.set(n, items as TextItemLike[]);
                          if (isTarget && located.current !== target?.nonce)
                            locate(n);
                        }}
                        customTextRenderer={
                          isTarget ? renderTargetText : undefined
                        }
                        onRenderTextLayerSuccess={
                          isTarget ? scrollToHit : undefined
                        }
                      />
                    )}
                  </div>
                );
              })}
            </div>
          )}
        </Document>
      </div>
      <div className="absolute bottom-3 right-5 flex items-center gap-1 rounded-lg border border-gray-300 bg-white/95 px-1.5 py-1 shadow">
        <button
          type="button"
          aria-label="Zoom out"
          onClick={() => zoomBy(1 / ZOOM_STEP)}
          className="p-1 rounded hover:bg-gray-100 text-gray-700"
        >
          <Minus className="w-4 h-4" />
        </button>
        <span className="text-xs tabular-nums w-11 text-center text-gray-700">
          {Math.round(zoom * 100)}%
        </span>
        <button
          type="button"
          aria-label="Zoom in"
          onClick={() => zoomBy(ZOOM_STEP)}
          className="p-1 rounded hover:bg-gray-100 text-gray-700"
        >
          <Plus className="w-4 h-4" />
        </button>
        <button
          type="button"
          aria-label="Fit width"
          title="Fit width"
          onClick={() => setZoom(1)}
          className="p-1 rounded hover:bg-gray-100 text-gray-700"
        >
          <Maximize2 className="w-4 h-4" />
        </button>
        <span className="mx-0.5 h-4 w-px bg-gray-300" />
        <button
          type="button"
          aria-label={hand ? "Select text" : "Hand tool"}
          aria-pressed={hand}
          title={
            hand
              ? "Hand tool on: drag to move the page. Click to select text instead"
              : "Select text. Click for the hand tool, to drag the page"
          }
          onClick={() => setHand((h) => !h)}
          className={`p-1 rounded text-gray-700 ${
            hand ? "bg-indigo-100 text-indigo-700" : "hover:bg-gray-100"
          }`}
        >
          {hand ? (
            <Hand className="w-4 h-4" />
          ) : (
            <TextCursor className="w-4 h-4" />
          )}
        </button>
      </div>
    </div>
  );
}
