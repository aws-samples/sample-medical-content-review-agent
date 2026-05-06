// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { X } from "lucide-react";

export interface ReviewIssue {
  page: number;
  quote: string;
  issue: string;
  fix: string;
  reference: string;
  source: string;
  type: string;
  score: number;
}

export interface ActivityEntry {
  timestamp: string;
  icon: string;
  label: string;
  detail?: string;
  status: "running" | "done";
  output?: string;
}

export interface PreviewDoc {
  name: string;
  url: string;
  kind: "content" | "reference";
}

interface ReviewResultsPanelProps {
  issues: ReviewIssue[];
  isLoading: boolean;
  activityLog?: ActivityEntry[];
  phaseStarted?: number[];
  phaseDone?: number[];
  startedAt?: number | null;
  documentUrl?: string | null;
  previewDocs?: PreviewDoc[];
  onNewReview: () => void;
}

const PHASES: { icon: string; text: string }[] = [
  { icon: "📄", text: "Reading documents" },
  { icon: "✂️", text: "Splitting into batches" },
  { icon: "🔍", text: "Running reviewers in parallel" },
  { icon: "🧩", text: "Merging reviewer findings" },
  { icon: "✅", text: "Writing final report" },
];

function formatElapsed(ms: number): string {
  const total = Math.max(0, Math.floor(ms / 1000));
  const m = Math.floor(total / 60);
  const s = total % 60;
  return `${m.toString().padStart(2, "0")}:${s.toString().padStart(2, "0")}`;
}

function ElapsedTimer({ startedAt }: { startedAt: number }) {
  const [now, setNow] = useState<number>(() => Date.now());
  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(id);
  }, []);
  return (
    <span className="tabular-nums font-mono text-indigo-600 font-bold">
      {formatElapsed(now - startedAt)}
    </span>
  );
}

function DocumentPreviewHeader({
  tabs,
  activeIdx,
  onSelect,
}: {
  tabs: PreviewDoc[];
  activeIdx: number;
  onSelect: (idx: number) => void;
}) {
  // No tabs — plain header
  if (tabs.length === 0) {
    return (
      <div className="bg-gradient-to-r from-purple-50 to-pink-50 px-6 py-4 border-b border-gray-200 shrink-0">
        <h3 className="text-lg font-semibold text-gray-900">
          Document Preview
        </h3>
      </div>
    );
  }
  // Single doc — show the file name, no tab bar
  if (tabs.length === 1) {
    return (
      <div className="bg-gradient-to-r from-purple-50 to-pink-50 px-6 py-3 border-b border-gray-200 shrink-0">
        <h3 className="text-base font-semibold text-gray-900 truncate">
          {tabs[0].name}
        </h3>
        <p className="text-[11px] text-gray-500 uppercase tracking-wide">
          {tabs[0].kind === "content" ? "Medical content" : "Reference"}
        </p>
      </div>
    );
  }
  // Multiple docs — tab strip
  return (
    <div className="bg-gradient-to-r from-purple-50 to-pink-50 border-b border-gray-200 shrink-0">
      <div className="flex items-end gap-1 px-3 pt-3 overflow-x-auto">
        {tabs.map((tab, idx) => {
          const isActive = idx === activeIdx;
          return (
            <button
              key={idx}
              type="button"
              onClick={() => onSelect(idx)}
              className={`group max-w-[18rem] shrink-0 px-3 py-2 rounded-t-lg text-xs font-medium flex items-center gap-1.5 border-t border-x transition-colors ${
                isActive
                  ? "bg-white text-gray-900 border-gray-200 shadow-sm"
                  : "bg-transparent text-gray-600 hover:text-gray-900 hover:bg-white/60 border-transparent"
              }`}
              title={tab.name}
            >
              <span className="text-sm leading-none">
                {tab.kind === "content" ? "📄" : "📎"}
              </span>
              <span className="truncate">{tab.name}</span>
            </button>
          );
        })}
      </div>
    </div>
  );
}

function getSeverityColor(score: number) {
  if (score >= 90) return "bg-red-50 border-l-red-600";
  if (score >= 70) return "bg-orange-50 border-l-orange-600";
  if (score >= 50) return "bg-yellow-50 border-l-yellow-600";
  return "bg-blue-50 border-l-blue-600";
}

function getSeverityBadge(score: number) {
  if (score >= 90) return "bg-red-600";
  if (score >= 70) return "bg-orange-600";
  if (score >= 50) return "bg-yellow-600";
  return "bg-blue-600";
}

export function ReviewResultsPanel({
  issues,
  isLoading,
  activityLog = [],
  phaseStarted = [0, 0, 0, 0, 0],
  phaseDone = [0, 0, 0, 0, 0],
  startedAt = null,
  documentUrl,
  previewDocs,
  onNewReview,
}: ReviewResultsPanelProps) {
  const [selectedIssue, setSelectedIssue] = useState<ReviewIssue | null>(null);
  const [currentPage, setCurrentPage] = useState<number>(1);
  const [activePreviewIdx, setActivePreviewIdx] = useState<number>(0);
  const activityScrollRef = useRef<HTMLDivElement | null>(null);

  // Effective preview tab list: prefer previewDocs, fall back to legacy documentUrl
  const previewTabs: PreviewDoc[] = useMemo(() => {
    if (previewDocs && previewDocs.length > 0) return previewDocs;
    if (documentUrl)
      return [
        { name: "Medical content", url: documentUrl, kind: "content" as const },
      ];
    return [];
  }, [previewDocs, documentUrl]);

  const activeDocUrl = previewTabs[activePreviewIdx]?.url ?? null;
  const isContentActive = previewTabs[activePreviewIdx]?.kind === "content";

  useEffect(() => {
    const el = activityScrollRef.current;
    if (!el) return;
    // Only stick-to-bottom if the user is already near the bottom, so they can freely
    // scroll up to inspect earlier events without being yanked down.
    const nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
    if (nearBottom) el.scrollTop = el.scrollHeight;
  }, [activityLog]);

  const stats = useMemo(
    () => ({
      total: issues.length,
      critical: issues.filter((i) => i.score >= 90).length,
      high: issues.filter((i) => i.score >= 70 && i.score < 90).length,
      medium: issues.filter((i) => i.score < 70).length,
    }),
    [issues],
  );

  const handleDownload = () => {
    const blob = new Blob([JSON.stringify(issues, null, 2)], {
      type: "application/json",
    });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = "review_results.json";
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  };

  // Loading state — show document preview alongside activity log
  if (isLoading) {
    return (
      <div className="h-full overflow-auto bg-gradient-to-br from-slate-800 via-slate-700 to-slate-900">
        <div className="container mx-auto px-6 py-8 max-w-7xl">
          <div className="grid grid-cols-1 lg:grid-cols-2 gap-6 lg:h-[800px]">
            {/* Left: Document Preview (with tabs for content + references) */}
            <div className="bg-white rounded-2xl shadow-lg border border-gray-200 overflow-hidden flex flex-col h-[800px] lg:h-full">
              <DocumentPreviewHeader
                tabs={previewTabs}
                activeIdx={activePreviewIdx}
                onSelect={setActivePreviewIdx}
              />
              <div className="flex-1 overflow-hidden">
                {activeDocUrl ? (
                  <iframe
                    src={activeDocUrl}
                    className="w-full h-full"
                    title="PDF Document"
                  />
                ) : (
                  <div className="flex items-center justify-center h-full">
                    <p className="text-gray-400">No document loaded</p>
                  </div>
                )}
              </div>
            </div>

            {/* Right: Review Progress */}
            <div className="bg-white rounded-2xl shadow-2xl border border-gray-200 overflow-hidden flex flex-col h-[800px] lg:h-full">
              <div className="bg-gradient-to-r from-indigo-50 to-blue-50 px-6 py-4 border-b border-gray-200 flex justify-between items-center">
                <div>
                  <h3 className="text-lg font-semibold text-gray-900">
                    Review in Progress
                  </h3>
                  <p className="text-xs text-gray-500 mt-0.5">
                    Multi-agent analysis running…
                  </p>
                </div>
                {startedAt !== null && (
                  <div className="text-right">
                    <p className="text-xs font-semibold text-gray-500 uppercase tracking-wide">
                      Elapsed
                    </p>
                    <p className="text-2xl">
                      <ElapsedTimer startedAt={startedAt} />
                    </p>
                  </div>
                )}
              </div>
              <div className="p-6 flex-1 flex flex-col gap-6 overflow-hidden">
                {/* Phase checklist */}
                <div className="space-y-2">
                  {(() => {
                    // Compute the highest contiguous phase that has actually finished.
                    // A later phase can't be marked done before earlier ones have completed,
                    // even if one of its tools happens to return first.
                    let lastDoneIdx = -1;
                    for (let i = 0; i < PHASES.length; i++) {
                      const s = phaseStarted[i] ?? 0;
                      const d = phaseDone[i] ?? 0;
                      if (s > 0 && s === d) lastDoneIdx = i;
                      else break;
                    }
                    return PHASES.map((step, idx) => {
                      const started = phaseStarted[idx] ?? 0;
                      const done = phaseDone[idx] ?? 0;
                      const isDone = idx <= lastDoneIdx;
                      const isActive =
                        !isDone &&
                        (started > done || (started > 0 && started === done));
                      const base =
                        "flex items-center gap-3 rounded-lg px-3 py-2 border transition-all";
                      const cls = isActive
                        ? `${base} bg-indigo-50 border-indigo-300 shadow-sm`
                        : isDone
                        ? `${base} bg-green-50 border-green-200`
                        : `${base} bg-gray-50 border-gray-200 opacity-60`;
                      return (
                        <div key={idx} className={cls}>
                          <span className="text-xl">{step.icon}</span>
                          <span
                            className={`flex-1 text-sm ${
                              isActive
                                ? "font-semibold text-indigo-900"
                                : isDone
                                ? "text-green-800"
                                : "text-gray-600"
                            }`}
                          >
                            {step.text}
                          </span>
                          {isActive && (
                            <span className="flex gap-0.5">
                              <span
                                className="w-1.5 h-1.5 rounded-full bg-indigo-600 animate-pulse"
                                style={{ animationDelay: "0ms" }}
                              />
                              <span
                                className="w-1.5 h-1.5 rounded-full bg-indigo-600 animate-pulse"
                                style={{ animationDelay: "200ms" }}
                              />
                              <span
                                className="w-1.5 h-1.5 rounded-full bg-indigo-600 animate-pulse"
                                style={{ animationDelay: "400ms" }}
                              />
                            </span>
                          )}
                          {isDone && (
                            <svg
                              className="w-5 h-5 text-green-600"
                              fill="none"
                              stroke="currentColor"
                              viewBox="0 0 24 24"
                            >
                              <path
                                strokeLinecap="round"
                                strokeLinejoin="round"
                                strokeWidth={3}
                                d="M5 13l4 4L19 7"
                              />
                            </svg>
                          )}
                        </div>
                      );
                    });
                  })()}
                </div>

                {/* Live activity timeline */}
                <div className="flex-1 flex flex-col min-h-0">
                  <div className="flex items-center justify-between mb-2">
                    <p className="text-xs font-semibold text-gray-500 uppercase tracking-wide">
                      Live Activity
                    </p>
                    <p className="text-xs text-gray-400">
                      {activityLog.length} events
                    </p>
                  </div>
                  <div
                    ref={activityScrollRef}
                    className="flex-1 overflow-auto space-y-1.5 bg-gray-50 rounded-lg px-3 pt-3 pb-4 border border-gray-200 min-h-[200px]"
                  >
                    {activityLog.length === 0 ? (
                      <p className="text-xs text-gray-400 italic text-center py-6">
                        Waiting for first event…
                      </p>
                    ) : (
                      activityLog.map((entry, idx) => {
                        const rowClass = `text-xs rounded px-2 py-1.5 bg-white border ${
                          entry.status === "running"
                            ? "border-indigo-200"
                            : "border-gray-200"
                        }`;
                        const statusBadge =
                          entry.status === "running" ? (
                            <span className="inline-block w-3 h-3 rounded-full border-2 border-indigo-600 border-t-transparent animate-spin mt-0.5 shrink-0" />
                          ) : (
                            <svg
                              className="w-3.5 h-3.5 text-green-600 mt-0.5 shrink-0"
                              fill="none"
                              stroke="currentColor"
                              viewBox="0 0 24 24"
                            >
                              <path
                                strokeLinecap="round"
                                strokeLinejoin="round"
                                strokeWidth={3}
                                d="M5 13l4 4L19 7"
                              />
                            </svg>
                          );
                        const header = (
                          <div className="flex items-start gap-2">
                            <span className="font-mono text-gray-400 tabular-nums">
                              {entry.timestamp}
                            </span>
                            <span className="text-base leading-none">
                              {entry.icon}
                            </span>
                            <div className="flex-1 min-w-0">
                              <p
                                className={`${
                                  entry.status === "running"
                                    ? "text-indigo-900 font-semibold"
                                    : "text-gray-700"
                                }`}
                              >
                                {entry.label}
                              </p>
                              {entry.detail && (
                                <p className="text-gray-500 font-mono truncate">
                                  &ldquo;{entry.detail}&rdquo;
                                </p>
                              )}
                            </div>
                            {statusBadge}
                          </div>
                        );
                        if (!entry.output) {
                          return (
                            <div key={idx} className={rowClass}>
                              {header}
                            </div>
                          );
                        }
                        return (
                          <details key={idx} className={`${rowClass} group`}>
                            <summary className="cursor-pointer list-none select-none flex items-start gap-2">
                              <div className="flex-1 min-w-0">{header}</div>
                              <svg
                                className="w-3.5 h-3.5 text-gray-400 mt-0.5 shrink-0 transition-transform group-open:rotate-90"
                                fill="none"
                                stroke="currentColor"
                                viewBox="0 0 24 24"
                              >
                                <path
                                  strokeLinecap="round"
                                  strokeLinejoin="round"
                                  strokeWidth={2.5}
                                  d="M9 5l7 7-7 7"
                                />
                              </svg>
                            </summary>
                            <pre className="mt-1.5 p-2 bg-gray-900 text-gray-100 rounded font-mono text-[10.5px] leading-relaxed whitespace-pre-wrap break-words max-h-48 overflow-auto">
                              {entry.output}
                            </pre>
                          </details>
                        );
                      })
                    )}
                  </div>
                </div>
              </div>
            </div>
          </div>
        </div>
      </div>
    );
  }

  // No issues found after review completed
  if (issues.length === 0) {
    return (
      <div className="flex items-center justify-center h-full bg-gradient-to-br from-slate-800 via-slate-700 to-slate-900">
        <div className="bg-white rounded-2xl shadow-2xl border border-gray-200 p-12 max-w-lg w-full mx-6 text-center">
          <svg
            className="mx-auto w-16 h-16 text-green-500 mb-4"
            fill="none"
            stroke="currentColor"
            viewBox="0 0 24 24"
          >
            <path
              strokeLinecap="round"
              strokeLinejoin="round"
              strokeWidth={2}
              d="M9 12l2 2 4-4m5.618-4.016A11.955 11.955 0 0112 2.944a11.955 11.955 0 01-8.618 3.04A12.02 12.02 0 003 9c0 5.591 3.824 10.29 9 11.622 5.176-1.332 9-6.03 9-11.622 0-1.042-.133-2.052-.382-3.016z"
            />
          </svg>
          <h2 className="text-2xl font-bold text-gray-900 mb-2">
            Review Complete
          </h2>
          <p className="text-gray-600 mb-6">
            No issues were detected in your document.
          </p>
          <button
            onClick={onNewReview}
            className="inline-flex items-center gap-2 bg-indigo-600 hover:bg-indigo-700 text-white font-semibold py-3 px-6 rounded-lg transition-colors"
          >
            <svg
              className="w-5 h-5"
              fill="none"
              stroke="currentColor"
              viewBox="0 0 24 24"
            >
              <path
                strokeLinecap="round"
                strokeLinejoin="round"
                strokeWidth={2}
                d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15"
              />
            </svg>
            New Review
          </button>
        </div>
      </div>
    );
  }

  // Results view — matches old UI from Picture1.png
  return (
    <div className="h-full overflow-auto bg-gradient-to-br from-slate-800 via-slate-700 to-slate-900">
      <div className="container mx-auto px-6 py-8 max-w-7xl">
        {/* Review Complete Banner */}
        <div className="bg-white rounded-2xl shadow-lg border border-gray-200 p-6 mb-6">
          <div className="flex justify-between items-center">
            <div>
              <h2 className="text-2xl font-bold text-gray-900">
                Review Complete
              </h2>
              <p className="text-gray-600 mt-1">
                {stats.total} issues detected in your document
              </p>
            </div>
            <div className="flex gap-3">
              <button
                onClick={handleDownload}
                className="flex items-center gap-2 bg-indigo-600 hover:bg-indigo-700 text-white font-semibold py-3 px-6 rounded-lg transition-colors"
              >
                <svg
                  className="w-5 h-5"
                  fill="none"
                  stroke="currentColor"
                  viewBox="0 0 24 24"
                >
                  <path
                    strokeLinecap="round"
                    strokeLinejoin="round"
                    strokeWidth={2}
                    d="M12 10v6m0 0l-3-3m3 3l3-3m2 8H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z"
                  />
                </svg>
                Download JSON
              </button>
              <button
                onClick={onNewReview}
                className="flex items-center gap-2 bg-gray-600 hover:bg-gray-700 text-white font-semibold py-3 px-6 rounded-lg transition-colors"
              >
                <svg
                  className="w-5 h-5"
                  fill="none"
                  stroke="currentColor"
                  viewBox="0 0 24 24"
                >
                  <path
                    strokeLinecap="round"
                    strokeLinejoin="round"
                    strokeWidth={2}
                    d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15"
                  />
                </svg>
                New Review
              </button>
            </div>
          </div>
        </div>

        {/* Stats Bar */}
        <div className="grid grid-cols-4 gap-4 mb-6">
          <div className="bg-white rounded-xl shadow-lg border border-gray-200 p-6 text-center">
            <p className="text-4xl font-bold text-indigo-600 mb-2">
              {stats.total}
            </p>
            <p className="text-sm font-semibold text-gray-600">Total Issues</p>
          </div>
          <div className="bg-white rounded-xl shadow-lg border border-red-200 p-6 text-center">
            <p className="text-4xl font-bold text-red-600 mb-2">
              {stats.critical}
            </p>
            <p className="text-sm font-semibold text-gray-600">Critical</p>
          </div>
          <div className="bg-white rounded-xl shadow-lg border border-orange-200 p-6 text-center">
            <p className="text-4xl font-bold text-orange-600 mb-2">
              {stats.high}
            </p>
            <p className="text-sm font-semibold text-gray-600">High</p>
          </div>
          <div className="bg-white rounded-xl shadow-lg border border-yellow-200 p-6 text-center">
            <p className="text-4xl font-bold text-yellow-600 mb-2">
              {stats.medium}
            </p>
            <p className="text-sm font-semibold text-gray-600">Medium/Low</p>
          </div>
        </div>

        {/* Split-Pane: PDF left, Issues right */}
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
          {/* Left: Document Preview (with tabs for content + references) */}
          <div className="bg-white rounded-2xl shadow-lg border border-gray-200 overflow-hidden">
            <DocumentPreviewHeader
              tabs={previewTabs}
              activeIdx={activePreviewIdx}
              onSelect={setActivePreviewIdx}
            />
            <div className="h-[800px] overflow-hidden">
              {activeDocUrl ? (
                <iframe
                  src={
                    isContentActive
                      ? `${activeDocUrl}#page=${currentPage}`
                      : activeDocUrl
                  }
                  className="w-full h-full"
                  title="PDF Document"
                />
              ) : (
                <div className="flex items-center justify-center h-full">
                  <p className="text-gray-400">No document loaded</p>
                </div>
              )}
            </div>
          </div>

          {/* Right: Detected Issues */}
          <div className="bg-white rounded-2xl shadow-lg border border-gray-200 overflow-hidden">
            <div className="bg-gradient-to-r from-red-50 to-orange-50 px-6 py-4 border-b border-gray-200">
              <h3 className="text-lg font-semibold text-gray-900">
                Detected Issues
              </h3>
            </div>
            <div className="h-[800px] overflow-y-auto p-4">
              <div className="space-y-4">
                {issues.map((issue, idx) => (
                  <div
                    key={idx}
                    className={`border-l-4 p-3 rounded-lg ${getSeverityColor(
                      issue.score,
                    )} cursor-pointer hover:shadow-lg transition-all`}
                    onClick={() => {
                      setCurrentPage(issue.page);
                      const contentIdx = previewTabs.findIndex(
                        (t) => t.kind === "content",
                      );
                      if (contentIdx >= 0) setActivePreviewIdx(contentIdx);
                      setSelectedIssue(issue);
                    }}
                  >
                    <div className="flex justify-between items-start mb-2">
                      <span className="text-xs font-bold bg-gray-800 text-white px-2 py-0.5 rounded-full">
                        Page {issue.page}
                      </span>
                      <span
                        className={`text-xs font-bold ${getSeverityBadge(
                          issue.score,
                        )} text-white px-2 py-0.5 rounded-full`}
                      >
                        {issue.score}/100
                      </span>
                    </div>
                    <h4 className="font-bold text-gray-900 text-xs mb-2 flex items-center gap-1.5">
                      <span className="text-base">⚠️</span>
                      {issue.type}
                    </h4>
                    <div className="bg-white bg-opacity-60 rounded p-2 mb-2">
                      <p className="text-xs text-gray-800 italic">
                        &ldquo;{issue.quote}&rdquo;
                      </p>
                    </div>
                    <div className="space-y-1.5 text-xs">
                      <div>
                        <strong className="text-gray-900">Issue:</strong>
                        <p className="text-gray-700 mt-0.5">{issue.issue}</p>
                      </div>
                      <div>
                        <strong className="text-green-700">Fix:</strong>
                        <p className="text-gray-700 mt-0.5">{issue.fix}</p>
                      </div>
                      <div className="pt-1.5 border-t border-gray-200">
                        <p className="text-xs text-gray-600">
                          <strong>Reference:</strong> {issue.reference}
                        </p>
                        <p className="text-xs text-gray-500 mt-0.5">
                          <strong>Source:</strong> {issue.source}
                        </p>
                      </div>
                    </div>
                  </div>
                ))}
              </div>
            </div>
          </div>
        </div>
      </div>

      {/* Issue Detail Modal */}
      {selectedIssue && (
        <div
          className="fixed inset-0 bg-white bg-opacity-80 backdrop-blur-sm flex items-center justify-center z-50 p-4"
          onClick={() => setSelectedIssue(null)}
        >
          <div
            className={`bg-white rounded-2xl shadow-2xl max-w-3xl w-full max-h-[90vh] overflow-y-auto border-l-8 ${getSeverityColor(
              selectedIssue.score,
            )}`}
            onClick={(e) => e.stopPropagation()}
          >
            <div className="sticky top-0 bg-gradient-to-r from-gray-50 to-gray-100 px-8 py-6 border-b border-gray-200 flex justify-between items-start">
              <div className="flex-1">
                <div className="flex items-center gap-3 mb-3">
                  <span className="text-sm font-bold bg-gray-800 text-white px-3 py-1 rounded-full">
                    Page {selectedIssue.page}
                  </span>
                  <span
                    className={`text-sm font-bold ${getSeverityBadge(
                      selectedIssue.score,
                    )} text-white px-3 py-1 rounded-full`}
                  >
                    Severity: {selectedIssue.score}/100
                  </span>
                </div>
                <h3 className="text-2xl font-bold text-gray-900 flex items-center gap-2">
                  <span className="text-3xl">⚠️</span>
                  {selectedIssue.type}
                </h3>
              </div>
              <button
                onClick={() => setSelectedIssue(null)}
                className="text-gray-400 hover:text-gray-600 transition-colors p-2"
              >
                <X className="w-6 h-6" />
              </button>
            </div>
            <div className="px-8 py-6 space-y-6">
              <div>
                <h4 className="text-sm font-bold text-gray-500 uppercase tracking-wide mb-2">
                  Quoted Text
                </h4>
                <div className="bg-gray-50 rounded-lg p-4 border-l-4 border-gray-300">
                  <p className="text-lg text-gray-800 italic">
                    &ldquo;{selectedIssue.quote}&rdquo;
                  </p>
                </div>
              </div>
              <div>
                <h4 className="text-sm font-bold text-red-600 uppercase tracking-wide mb-2">
                  Issue Description
                </h4>
                <p className="text-base text-gray-700 leading-relaxed">
                  {selectedIssue.issue}
                </p>
              </div>
              <div>
                <h4 className="text-sm font-bold text-green-600 uppercase tracking-wide mb-2">
                  Recommended Fix
                </h4>
                <p className="text-base text-gray-700 leading-relaxed">
                  {selectedIssue.fix}
                </p>
              </div>
              <div className="pt-4 border-t border-gray-200 space-y-3">
                <div>
                  <h4 className="text-sm font-bold text-gray-500 uppercase tracking-wide mb-1">
                    Reference
                  </h4>
                  <p className="text-sm text-gray-600">
                    {selectedIssue.reference}
                  </p>
                </div>
                <div>
                  <h4 className="text-sm font-bold text-gray-500 uppercase tracking-wide mb-1">
                    Source Document
                  </h4>
                  <p className="text-sm text-gray-600">
                    {selectedIssue.source}
                  </p>
                </div>
              </div>
            </div>
            <div className="sticky bottom-0 bg-gray-50 px-8 py-4 border-t border-gray-200 flex justify-end">
              <button
                onClick={() => setSelectedIssue(null)}
                className="bg-gray-600 hover:bg-gray-700 text-white font-semibold py-2 px-6 rounded-lg transition-colors"
              >
                Close
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
