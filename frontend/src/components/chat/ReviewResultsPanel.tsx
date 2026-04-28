// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
"use client";

import { useMemo, useState } from "react";
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

interface ReviewResultsPanelProps {
  issues: ReviewIssue[];
  isLoading: boolean;
  activityLog?: string[];
  documentUrl?: string | null;
  onNewReview: () => void;
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

export function ReviewResultsPanel({ issues, isLoading, activityLog = [], documentUrl, onNewReview }: ReviewResultsPanelProps) {
  const [selectedIssue, setSelectedIssue] = useState<ReviewIssue | null>(null);
  const [currentPage, setCurrentPage] = useState<number>(1);

  const stats = useMemo(() => ({
    total: issues.length,
    critical: issues.filter((i) => i.score >= 90).length,
    high: issues.filter((i) => i.score >= 70 && i.score < 90).length,
    medium: issues.filter((i) => i.score < 70).length,
  }), [issues]);

  const handleDownload = () => {
    const blob = new Blob([JSON.stringify(issues, null, 2)], { type: "application/json" });
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
          <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
            {/* Left: Document Preview */}
            <div className="bg-white rounded-2xl shadow-lg border border-gray-200 overflow-hidden">
              <div className="bg-gradient-to-r from-purple-50 to-pink-50 px-6 py-4 border-b border-gray-200">
                <h3 className="text-lg font-semibold text-gray-900">Document Preview</h3>
              </div>
              <div className="h-[800px] overflow-hidden">
                {documentUrl ? (
                  <iframe src={documentUrl} className="w-full h-full" title="PDF Document" />
                ) : (
                  <div className="flex items-center justify-center h-full">
                    <p className="text-gray-400">No document loaded</p>
                  </div>
                )}
              </div>
            </div>

            {/* Right: Review Progress */}
            <div className="bg-white rounded-2xl shadow-2xl border border-gray-200 overflow-hidden flex flex-col">
              <div className="bg-gradient-to-r from-indigo-50 to-blue-50 px-6 py-4 border-b border-gray-200">
                <h3 className="text-lg font-semibold text-gray-900">Review in Progress</h3>
              </div>
              <div className="p-8 flex-1 flex flex-col items-center justify-center">
                <div className="relative w-16 h-16 mx-auto mb-6">
                  <div className="absolute inset-0 border-4 border-indigo-200 rounded-full"></div>
                  <div className="absolute inset-0 border-4 border-indigo-600 rounded-full border-t-transparent animate-spin"></div>
                  <svg className="absolute inset-0 m-auto w-8 h-8 text-indigo-600" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9.663 17h4.673M12 3v1m6.364 1.636l-.707.707M21 12h-1M4 12H3m3.343-5.657l-.707-.707m2.828 9.9a5 5 0 117.072 0l-.548.547A3.374 3.374 0 0014 18.469V19a2 2 0 11-4 0v-.531c0-.895-.356-1.754-.988-2.386l-.548-.547z" />
                  </svg>
                </div>
                <h2 className="text-xl font-bold text-gray-900 mb-2">Analyzing Document</h2>
                <p className="text-gray-600 text-sm mb-6">Multi-agent review in progress...</p>
                <div className="space-y-2 w-full max-w-sm">
                  {[
                    { icon: "📄", text: "Processing PDFs" },
                    { icon: "✂️", text: "Splitting into batches" },
                    { icon: "📝", text: "Extracting statements" },
                    { icon: "🔍", text: "Analyzing content" },
                    { icon: "✅", text: "Generating report" },
                  ].map((step, idx) => (
                    <div key={idx} className="flex items-center gap-2 bg-gray-50 rounded-lg px-3 py-2">
                      <span className="text-lg">{step.icon}</span>
                      <span className="text-sm text-gray-700">{step.text}</span>
                    </div>
                  ))}
                </div>
                {activityLog.length > 0 && (
                  <div className="mt-6 w-full max-w-sm">
                    <p className="text-xs font-semibold text-gray-500 uppercase tracking-wide mb-2">Activity</p>
                    <div className="space-y-1 max-h-48 overflow-auto">
                      {activityLog.map((entry, idx) => (
                        <div key={idx} className="text-xs text-gray-600 bg-gray-50 rounded px-3 py-1.5 font-mono">{entry}</div>
                      ))}
                    </div>
                  </div>
                )}
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
          <svg className="mx-auto w-16 h-16 text-green-500 mb-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 12l2 2 4-4m5.618-4.016A11.955 11.955 0 0112 2.944a11.955 11.955 0 01-8.618 3.04A12.02 12.02 0 003 9c0 5.591 3.824 10.29 9 11.622 5.176-1.332 9-6.03 9-11.622 0-1.042-.133-2.052-.382-3.016z" />
          </svg>
          <h2 className="text-2xl font-bold text-gray-900 mb-2">Review Complete</h2>
          <p className="text-gray-600 mb-6">No issues were detected in your document.</p>
          <button onClick={onNewReview} className="inline-flex items-center gap-2 bg-indigo-600 hover:bg-indigo-700 text-white font-semibold py-3 px-6 rounded-lg transition-colors">
            <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
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
              <h2 className="text-2xl font-bold text-gray-900">Review Complete</h2>
              <p className="text-gray-600 mt-1">{stats.total} issues detected in your document</p>
            </div>
            <div className="flex gap-3">
              <button onClick={handleDownload} className="flex items-center gap-2 bg-indigo-600 hover:bg-indigo-700 text-white font-semibold py-3 px-6 rounded-lg transition-colors">
                <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 10v6m0 0l-3-3m3 3l3-3m2 8H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z" />
                </svg>
                Download JSON
              </button>
              <button onClick={onNewReview} className="flex items-center gap-2 bg-gray-600 hover:bg-gray-700 text-white font-semibold py-3 px-6 rounded-lg transition-colors">
                <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
                </svg>
                New Review
              </button>
            </div>
          </div>
        </div>

        {/* Stats Bar */}
        <div className="grid grid-cols-4 gap-4 mb-6">
          <div className="bg-white rounded-xl shadow-lg border border-gray-200 p-6 text-center">
            <p className="text-4xl font-bold text-indigo-600 mb-2">{stats.total}</p>
            <p className="text-sm font-semibold text-gray-600">Total Issues</p>
          </div>
          <div className="bg-white rounded-xl shadow-lg border border-red-200 p-6 text-center">
            <p className="text-4xl font-bold text-red-600 mb-2">{stats.critical}</p>
            <p className="text-sm font-semibold text-gray-600">Critical</p>
          </div>
          <div className="bg-white rounded-xl shadow-lg border border-orange-200 p-6 text-center">
            <p className="text-4xl font-bold text-orange-600 mb-2">{stats.high}</p>
            <p className="text-sm font-semibold text-gray-600">High</p>
          </div>
          <div className="bg-white rounded-xl shadow-lg border border-yellow-200 p-6 text-center">
            <p className="text-4xl font-bold text-yellow-600 mb-2">{stats.medium}</p>
            <p className="text-sm font-semibold text-gray-600">Medium/Low</p>
          </div>
        </div>

        {/* Split-Pane: PDF left, Issues right */}
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
          {/* Left: Document Preview */}
          <div className="bg-white rounded-2xl shadow-lg border border-gray-200 overflow-hidden">
            <div className="bg-gradient-to-r from-purple-50 to-pink-50 px-6 py-4 border-b border-gray-200">
              <h3 className="text-lg font-semibold text-gray-900">Document Preview</h3>
            </div>
            <div className="h-[800px] overflow-hidden">
              {documentUrl ? (
                <iframe src={`${documentUrl}#page=${currentPage}`} className="w-full h-full" title="PDF Document" />
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
              <h3 className="text-lg font-semibold text-gray-900">Detected Issues</h3>
            </div>
            <div className="h-[800px] overflow-y-auto p-4">
              <div className="space-y-4">
                {issues.map((issue, idx) => (
                  <div
                    key={idx}
                    className={`border-l-4 p-3 rounded-lg ${getSeverityColor(issue.score)} cursor-pointer hover:shadow-lg transition-all`}
                    onClick={() => { setCurrentPage(issue.page); setSelectedIssue(issue); }}
                  >
                    <div className="flex justify-between items-start mb-2">
                      <span className="text-xs font-bold bg-gray-800 text-white px-2 py-0.5 rounded-full">Page {issue.page}</span>
                      <span className={`text-xs font-bold ${getSeverityBadge(issue.score)} text-white px-2 py-0.5 rounded-full`}>{issue.score}/100</span>
                    </div>
                    <h4 className="font-bold text-gray-900 text-xs mb-2 flex items-center gap-1.5">
                      <span className="text-base">⚠️</span>
                      {issue.type}
                    </h4>
                    <div className="bg-white bg-opacity-60 rounded p-2 mb-2">
                      <p className="text-xs text-gray-800 italic">&ldquo;{issue.quote}&rdquo;</p>
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
                        <p className="text-xs text-gray-600"><strong>Reference:</strong> {issue.reference}</p>
                        <p className="text-xs text-gray-500 mt-0.5"><strong>Source:</strong> {issue.source}</p>
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
        <div className="fixed inset-0 bg-white bg-opacity-80 backdrop-blur-sm flex items-center justify-center z-50 p-4" onClick={() => setSelectedIssue(null)}>
          <div className={`bg-white rounded-2xl shadow-2xl max-w-3xl w-full max-h-[90vh] overflow-y-auto border-l-8 ${getSeverityColor(selectedIssue.score)}`} onClick={(e) => e.stopPropagation()}>
            <div className="sticky top-0 bg-gradient-to-r from-gray-50 to-gray-100 px-8 py-6 border-b border-gray-200 flex justify-between items-start">
              <div className="flex-1">
                <div className="flex items-center gap-3 mb-3">
                  <span className="text-sm font-bold bg-gray-800 text-white px-3 py-1 rounded-full">Page {selectedIssue.page}</span>
                  <span className={`text-sm font-bold ${getSeverityBadge(selectedIssue.score)} text-white px-3 py-1 rounded-full`}>Severity: {selectedIssue.score}/100</span>
                </div>
                <h3 className="text-2xl font-bold text-gray-900 flex items-center gap-2">
                  <span className="text-3xl">⚠️</span>
                  {selectedIssue.type}
                </h3>
              </div>
              <button onClick={() => setSelectedIssue(null)} className="text-gray-400 hover:text-gray-600 transition-colors p-2">
                <X className="w-6 h-6" />
              </button>
            </div>
            <div className="px-8 py-6 space-y-6">
              <div>
                <h4 className="text-sm font-bold text-gray-500 uppercase tracking-wide mb-2">Quoted Text</h4>
                <div className="bg-gray-50 rounded-lg p-4 border-l-4 border-gray-300">
                  <p className="text-lg text-gray-800 italic">&ldquo;{selectedIssue.quote}&rdquo;</p>
                </div>
              </div>
              <div>
                <h4 className="text-sm font-bold text-red-600 uppercase tracking-wide mb-2">Issue Description</h4>
                <p className="text-base text-gray-700 leading-relaxed">{selectedIssue.issue}</p>
              </div>
              <div>
                <h4 className="text-sm font-bold text-green-600 uppercase tracking-wide mb-2">Recommended Fix</h4>
                <p className="text-base text-gray-700 leading-relaxed">{selectedIssue.fix}</p>
              </div>
              <div className="pt-4 border-t border-gray-200 space-y-3">
                <div>
                  <h4 className="text-sm font-bold text-gray-500 uppercase tracking-wide mb-1">Reference</h4>
                  <p className="text-sm text-gray-600">{selectedIssue.reference}</p>
                </div>
                <div>
                  <h4 className="text-sm font-bold text-gray-500 uppercase tracking-wide mb-1">Source Document</h4>
                  <p className="text-sm text-gray-600">{selectedIssue.source}</p>
                </div>
              </div>
            </div>
            <div className="sticky bottom-0 bg-gray-50 px-8 py-4 border-t border-gray-200 flex justify-end">
              <button onClick={() => setSelectedIssue(null)} className="bg-gray-600 hover:bg-gray-700 text-white font-semibold py-2 px-6 rounded-lg transition-colors">Close</button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
