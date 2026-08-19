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
  // Set when the finding is about a claim that was checked against the
  // pre-approved claims library ("" / undefined when it was not)
  claim_match?: string;
  claim_id?: string;
  // Which kind of exact match it is: "verbatim", "reordered", or "" for the rest
  claim_precision?: string;
}

export type ClaimMatchStatus = "exact" | "partial" | "none";

// An exact match is either the approved copy as written, or the same words rearranged
export type MatchPrecision = "verbatim" | "reordered";

export interface ClaimMatch {
  claim_ref: string;
  page: number;
  text: string;
  claim_type: string;
  match_status: ClaimMatchStatus;
  match_precision?: string;
  matched_claim_id: string;
  matched_claim_text: string;
  library_status: string;
  library_claim_usable: boolean | null;
  deviation: string;
  unusable_reason: string;
  requires_verification: boolean;
}

export interface ClaimsReport {
  total_claims: number;
  counts: Record<ClaimMatchStatus, number>;
  requires_verification: number;
  claims: ClaimMatch[];
}

export interface Phase {
  icon: string;
  text: string;
  // Bare tool names (gateway prefix stripped) that count towards this phase
  tools: string[];
}

export interface ActivityEntry {
  timestamp: string;
  icon: string;
  label: string;
  detail?: string;
  status: "running" | "done" | "error";
  output?: string;
}

// One row of the parsed pre-approved claims library, as `load_claims_library` writes it
export interface ClaimsLibraryEntry {
  claim_id: string;
  claim_text: string;
  claim_type?: string;
  status?: string;
  approved_date?: string;
  expiry_date?: string;
  reference?: string;
  restrictions?: string;
  job_code?: string;
}

export interface PreviewDoc {
  name: string;
  // Blob or pre-signed URL of a PDF; absent for the claims library, which is rendered
  // as a table rather than embedded
  url?: string;
  kind: "content" | "reference" | "claims";
  claims?: ClaimsLibraryEntry[];
  // Which spreadsheet column the parser read each field from, and the columns it kept
  // as-is. Shown above the table so a guessed header is visible rather than implied.
  columnMapping?: Record<string, string>;
  unmappedColumns?: string[];
}

interface ReviewResultsPanelProps {
  issues: ReviewIssue[];
  claimsReport?: ClaimsReport | null;
  isLoading: boolean;
  activityLog?: ActivityEntry[];
  phases?: Phase[];
  phaseStarted?: number[];
  phaseDone?: number[];
  startedAt?: number | null;
  documentUrl?: string | null;
  previewDocs?: PreviewDoc[];
  onNewReview: () => void;
}

// Fallback phase list for a run without a pre-approved claims library. The caller
// normally passes the phase list it actually used.
const DEFAULT_PHASES: Phase[] = [
  { icon: "📄", text: "Reading documents", tools: ["process_pdf"] },
  { icon: "✂️", text: "Splitting into batches", tools: ["batch_content"] },
  { icon: "🔍", text: "Running reviewers in parallel", tools: [] },
  { icon: "🧩", text: "Merging reviewer findings", tools: ["get_reviews"] },
  { icon: "✅", text: "Writing final report", tools: ["file_write"] },
];

const CLAIM_MATCH_LABELS: Record<ClaimMatchStatus, string> = {
  exact: "Exact match",
  partial: "Partial match",
  none: "No match",
};

// Only an exact match is qualified: verbatim reuse needs no note, rearranged copy does
const MATCH_PRECISION_LABELS: Record<MatchPrecision, string> = {
  verbatim: "verbatim",
  reordered: "reordered",
};

const CLAIM_MATCH_BADGES: Record<ClaimMatchStatus, string> = {
  exact: "bg-green-100 text-green-800 border-green-300",
  partial: "bg-amber-100 text-amber-900 border-amber-300",
  none: "bg-slate-100 text-slate-700 border-slate-300",
};

// One-liners for the compact counts shown while the review is still running
const CLAIM_MATCH_TOOLTIPS: Record<ClaimMatchStatus, string> = {
  exact:
    "The approved wording, verbatim or with the same words reordered — already cleared by a human, so it is not checked again",
  partial:
    "The same assertion as an approved claim but with deviating wording, so the approval does not carry over — verified like an unapproved claim",
  none: "Not covered by the claims library — not a violation, but verified against the references and external sources instead",
};

// The full definitions, shown next to the counts so the panel explains its own tags.
// The wording mirrors what the matcher actually does.
const CLAIM_MATCH_DEFINITIONS: {
  label: string;
  badge: string;
  text: string;
}[] = [
  {
    label: "Exact match · verbatim",
    badge: CLAIM_MATCH_BADGES.exact,
    text: "Word for word the approved claim, ignoring typography only (quotes, dashes, spacing, capitalisation). A human already cleared this exact wording, so it needs no further substantiation.",
  },
  {
    label: "Exact match · reordered",
    badge: CLAIM_MATCH_BADGES.exact,
    text: "Exactly the same words as the approved claim, in a different order — approved copy that was re-laid-out. Also treated as approved, with the rearrangement recorded in the report.",
  },
  {
    label: "Partial match",
    badge: CLAIM_MATCH_BADGES.partial,
    text: "Makes the same assertion as an approved claim, but the wording deviates, so the approval does not carry over. The deviation is recorded and the claim is verified like an unapproved one.",
  },
  {
    label: "No match",
    badge: CLAIM_MATCH_BADGES.none,
    text: "Not covered by the claims library. This is not a violation in itself: the library being silent about a claim says nothing about whether it is true, so the claim is checked against the references and external sources instead.",
  },
];

// Why the tags can be trusted, and the one case where an exact match is still flagged
function ClaimMatchLegend() {
  return (
    <details className="rounded-xl border border-gray-200 bg-gray-50 mb-4">
      <summary className="cursor-pointer select-none px-4 py-2.5 text-xs font-semibold text-gray-600 uppercase tracking-wide">
        How these tags are decided
      </summary>
      <div className="px-4 pb-3 space-y-2">
        {CLAIM_MATCH_DEFINITIONS.map((definition) => (
          <div key={definition.label} className="flex items-start gap-2">
            <span
              className={`text-[10px] font-bold px-2 py-0.5 rounded-full border whitespace-nowrap ${definition.badge}`}
            >
              {definition.label}
            </span>
            <p className="text-xs text-gray-600 flex-1">{definition.text}</p>
          </div>
        ))}
        <p className="text-xs text-gray-500 pt-1 border-t border-gray-200 mt-2">
          Both exact tags are decided in Python by comparing the words
          themselves, never by a model, because an exact match is the only tag
          that lets a claim skip verification. An exact match against a library
          claim that is withdrawn, expired, superseded, or still in draft is
          reported as an issue regardless. Nothing in this review is ever
          written back to the claims library.
        </p>
      </div>
    </details>
  );
}

function isClaimMatchStatus(value: unknown): value is ClaimMatchStatus {
  return value === "exact" || value === "partial" || value === "none";
}

function isMatchPrecision(value: unknown): value is MatchPrecision {
  return value === "verbatim" || value === "reordered";
}

// Small pill showing a claim's match status, the kind of exact match it is, and the
// approved claim id if any
function ClaimMatchBadge({
  status,
  claimId,
  precision,
}: {
  status: ClaimMatchStatus;
  claimId?: string;
  precision?: string;
}) {
  const qualifier =
    status === "exact" && isMatchPrecision(precision)
      ? ` (${MATCH_PRECISION_LABELS[precision]})`
      : "";
  return (
    <span
      className={`text-[10px] font-bold px-2 py-0.5 rounded-full border cursor-help ${CLAIM_MATCH_BADGES[status]}`}
      title={CLAIM_MATCH_TOOLTIPS[status]}
    >
      {CLAIM_MATCH_LABELS[status]}
      {qualifier}
      {claimId ? ` · ${claimId}` : ""}
    </span>
  );
}

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

const PREVIEW_KIND_LABELS: Record<PreviewDoc["kind"], string> = {
  content: "Medical content",
  reference: "Reference",
  claims: "Pre-approved claims library",
};

const PREVIEW_KIND_ICONS: Record<PreviewDoc["kind"], string> = {
  content: "📄",
  reference: "📎",
  claims: "📗",
};

// Library statuses that mean the claim may not be reused as-is, mirroring the matcher
const UNUSABLE_LIBRARY_STATUSES = [
  "withdrawn",
  "retired",
  "rejected",
  "expired",
  "superseded",
  "draft",
  "pending",
  "in review",
];

// Canonical field -> the label used in the mapping note, in display order
const CLAIMS_FIELD_LABELS: Record<string, string> = {
  claim_id: "ID",
  claim_text: "Approved claim",
  claim_type: "Type",
  status: "Status",
  expiry_date: "Expiry",
  approved_date: "Approved",
  reference: "Reference",
  restrictions: "Restrictions",
};

// The claims library is a spreadsheet, so it gets a table instead of an embedded
// viewer. It shows what the review was matched against, exactly as the parser read it.
export function ClaimsLibraryPreview({
  claims,
  columnMapping,
  unmappedColumns,
}: {
  claims: ClaimsLibraryEntry[];
  columnMapping?: Record<string, string>;
  unmappedColumns?: string[];
}) {
  if (claims.length === 0) {
    return (
      <div className="flex items-center justify-center h-full">
        <p className="text-gray-400">No approved claims parsed</p>
      </div>
    );
  }
  const mapped = Object.entries(columnMapping || {}).filter(
    ([, header]) => !!header,
  );
  return (
    <div className="h-full overflow-auto">
      {mapped.length > 0 && (
        <div className="px-3 py-2 bg-indigo-50 border-b border-indigo-100 text-[10px] text-indigo-900">
          <span className="font-semibold">Columns read as: </span>
          {mapped
            .map(
              ([field, header]) =>
                `${header} → ${CLAIMS_FIELD_LABELS[field] || field}`,
            )
            .join(", ")}
          {unmappedColumns && unmappedColumns.length > 0 && (
            <span className="text-indigo-700">
              {" · kept as extra: "}
              {unmappedColumns.join(", ")}
            </span>
          )}
        </div>
      )}
      <table className="w-full text-xs border-collapse">
        <thead className="sticky top-0 bg-gray-50 text-gray-600 uppercase tracking-wide text-[10px]">
          <tr>
            <th className="text-left font-semibold px-3 py-2 border-b border-gray-200">
              ID
            </th>
            <th className="text-left font-semibold px-3 py-2 border-b border-gray-200">
              Approved claim
            </th>
            <th className="text-left font-semibold px-3 py-2 border-b border-gray-200">
              Type
            </th>
            <th className="text-left font-semibold px-3 py-2 border-b border-gray-200">
              Status
            </th>
          </tr>
        </thead>
        <tbody>
          {claims.map((claim, idx) => {
            const status = (claim.status || "Approved").trim();
            const unusable = UNUSABLE_LIBRARY_STATUSES.includes(
              status.toLowerCase(),
            );
            return (
              <tr key={claim.claim_id || idx} className="align-top">
                <td className="px-3 py-2 border-b border-gray-100 font-mono text-[10px] text-gray-500 whitespace-nowrap">
                  {claim.claim_id}
                </td>
                <td className="px-3 py-2 border-b border-gray-100 text-gray-800">
                  {claim.claim_text}
                  {(claim.reference || claim.restrictions) && (
                    <p className="text-[10px] text-gray-500 mt-1">
                      {claim.reference}
                      {claim.reference && claim.restrictions ? " — " : ""}
                      {claim.restrictions}
                    </p>
                  )}
                </td>
                <td className="px-3 py-2 border-b border-gray-100 text-gray-600 whitespace-nowrap">
                  {claim.claim_type || "—"}
                </td>
                <td className="px-3 py-2 border-b border-gray-100 whitespace-nowrap">
                  <span
                    className={`text-[10px] font-bold px-2 py-0.5 rounded-full border ${
                      unusable
                        ? "bg-red-100 text-red-800 border-red-300"
                        : "bg-green-100 text-green-800 border-green-300"
                    }`}
                  >
                    {status}
                  </span>
                  {claim.expiry_date && (
                    <p className="text-[10px] text-gray-500 mt-1">
                      until {claim.expiry_date}
                    </p>
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
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
          {PREVIEW_KIND_LABELS[tabs[0].kind]}
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
                {PREVIEW_KIND_ICONS[tab.kind]}
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
  claimsReport = null,
  isLoading,
  activityLog = [],
  phases = DEFAULT_PHASES,
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

  const activeTab = previewTabs[activePreviewIdx] ?? null;
  const activeDocUrl = activeTab?.url ?? null;
  const activeClaims = activeTab?.kind === "claims" ? activeTab.claims : null;
  const isContentActive = activeTab?.kind === "content";

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
                {activeClaims ? (
                  <ClaimsLibraryPreview
                    claims={activeClaims}
                    columnMapping={activeTab?.columnMapping}
                    unmappedColumns={activeTab?.unmappedColumns}
                  />
                ) : activeDocUrl ? (
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
                    for (let i = 0; i < phases.length; i++) {
                      const s = phaseStarted[i] ?? 0;
                      const d = phaseDone[i] ?? 0;
                      if (s > 0 && s === d) lastDoneIdx = i;
                      else break;
                    }
                    return phases.map((step, idx) => {
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

                {/* Claim-match summary — published before the reviewers finish */}
                {claimsReport && (
                  <div className="rounded-lg border border-amber-200 bg-amber-50 px-3 py-2.5">
                    <div className="flex items-center justify-between mb-1.5">
                      <p className="text-xs font-semibold text-amber-900 uppercase tracking-wide">
                        Pre-approved claims
                      </p>
                      <p className="text-xs text-amber-800">
                        {claimsReport.total_claims} claims extracted
                      </p>
                    </div>
                    {/* The counts carry their definition, so a tag is never just a
                        colour — the full legend sits in the results panel */}
                    <div className="flex gap-3 text-xs text-gray-700">
                      <span
                        className="cursor-help underline decoration-dotted decoration-gray-400"
                        title={CLAIM_MATCH_TOOLTIPS.exact}
                      >
                        <strong className="text-green-700">
                          {claimsReport.counts.exact}
                        </strong>{" "}
                        exact
                      </span>
                      <span
                        className="cursor-help underline decoration-dotted decoration-gray-400"
                        title={CLAIM_MATCH_TOOLTIPS.partial}
                      >
                        <strong className="text-amber-700">
                          {claimsReport.counts.partial}
                        </strong>{" "}
                        partial
                      </span>
                      <span
                        className="cursor-help underline decoration-dotted decoration-gray-400"
                        title={CLAIM_MATCH_TOOLTIPS.none}
                      >
                        <strong className="text-slate-700">
                          {claimsReport.counts.none}
                        </strong>{" "}
                        no match
                      </span>
                      <span className="ml-auto text-gray-500">
                        {claimsReport.requires_verification} still being
                        verified
                      </span>
                    </div>
                  </div>
                )}

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
                        const rowClass = `text-xs rounded px-2 py-1.5 border ${
                          entry.status === "running"
                            ? "bg-white border-indigo-200"
                            : entry.status === "error"
                            ? "bg-red-50 border-red-300"
                            : "bg-white border-gray-200"
                        }`;
                        // A failed tool call must not look like a completed one: the
                        // agent may carry on without it, and the user has to see that
                        const statusBadge =
                          entry.status === "running" ? (
                            <span className="inline-block w-3 h-3 rounded-full border-2 border-indigo-600 border-t-transparent animate-spin mt-0.5 shrink-0" />
                          ) : entry.status === "error" ? (
                            <svg
                              className="w-3.5 h-3.5 text-red-600 mt-0.5 shrink-0"
                              fill="none"
                              stroke="currentColor"
                              viewBox="0 0 24 24"
                            >
                              <path
                                strokeLinecap="round"
                                strokeLinejoin="round"
                                strokeWidth={3}
                                d="M6 18L18 6M6 6l12 12"
                              />
                            </svg>
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
                                    : entry.status === "error"
                                    ? "text-red-800 font-semibold"
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
          {claimsReport && (
            <p className="text-sm text-gray-500 mb-6">
              {claimsReport.total_claims} claims extracted —{" "}
              {claimsReport.counts.exact} exact, {claimsReport.counts.partial}{" "}
              partial, {claimsReport.counts.none} with no pre-approved match.
            </p>
          )}
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

        {/* Pre-approved claims matching */}
        {claimsReport && (
          <details
            open
            className="bg-white rounded-2xl shadow-lg border border-gray-200 overflow-hidden mb-6 group"
          >
            <summary className="bg-gradient-to-r from-amber-50 to-yellow-50 px-6 py-4 border-b border-gray-200 cursor-pointer select-none list-none flex items-center justify-between">
              <div>
                <h3 className="text-lg font-semibold text-gray-900">
                  Pre-Approved Claims Matching
                </h3>
                <p className="text-xs text-gray-500 mt-0.5">
                  {claimsReport.total_claims} claims extracted ·{" "}
                  {claimsReport.counts.exact} exact ·{" "}
                  {claimsReport.counts.partial} partial ·{" "}
                  {claimsReport.counts.none} no match
                </p>
              </div>
              <svg
                className="w-5 h-5 text-gray-500 transition-transform group-open:rotate-90"
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
            <div className="p-4">
              <div className="grid grid-cols-2 lg:grid-cols-4 gap-3 mb-4">
                <div className="rounded-xl border border-gray-200 p-4 text-center">
                  <p className="text-3xl font-bold text-indigo-600">
                    {claimsReport.total_claims}
                  </p>
                  <p className="text-xs font-semibold text-gray-600 mt-1">
                    Claims Extracted
                  </p>
                </div>
                <div className="rounded-xl border border-green-200 p-4 text-center">
                  <p className="text-3xl font-bold text-green-600">
                    {claimsReport.counts.exact}
                  </p>
                  <p className="text-xs font-semibold text-gray-600 mt-1">
                    Exact Match
                  </p>
                </div>
                <div className="rounded-xl border border-amber-200 p-4 text-center">
                  <p className="text-3xl font-bold text-amber-600">
                    {claimsReport.counts.partial}
                  </p>
                  <p className="text-xs font-semibold text-gray-600 mt-1">
                    Partial Match
                  </p>
                </div>
                <div className="rounded-xl border border-slate-200 p-4 text-center">
                  <p className="text-3xl font-bold text-slate-600">
                    {claimsReport.counts.none}
                  </p>
                  <p className="text-xs font-semibold text-gray-600 mt-1">
                    No Match — Verified Elsewhere
                  </p>
                </div>
              </div>
              <ClaimMatchLegend />
              <div className="max-h-80 overflow-y-auto space-y-2">
                {claimsReport.claims.map((claim) => (
                  <div
                    key={claim.claim_ref}
                    className="border border-gray-200 rounded-lg px-3 py-2"
                  >
                    <div className="flex items-center gap-2 mb-1.5 flex-wrap">
                      <span className="text-[10px] font-bold bg-gray-800 text-white px-2 py-0.5 rounded-full">
                        Page {claim.page}
                      </span>
                      {isClaimMatchStatus(claim.match_status) && (
                        <ClaimMatchBadge
                          status={claim.match_status}
                          claimId={claim.matched_claim_id}
                          precision={claim.match_precision}
                        />
                      )}
                      {claim.claim_type && (
                        <span className="text-[10px] text-gray-500 uppercase tracking-wide">
                          {claim.claim_type.replace(/_/g, " ")}
                        </span>
                      )}
                      {claim.library_claim_usable === false && (
                        <span className="text-[10px] font-bold px-2 py-0.5 rounded-full border bg-red-100 text-red-800 border-red-300">
                          {claim.library_status || "Not usable"}
                        </span>
                      )}
                    </div>
                    <p className="text-xs text-gray-800 italic">
                      &ldquo;{claim.text}&rdquo;
                    </p>
                    {claim.deviation && (
                      <p className="text-xs text-amber-800 mt-1">
                        <strong>Deviation:</strong> {claim.deviation}
                      </p>
                    )}
                    {claim.matched_claim_text && (
                      <p className="text-xs text-gray-500 mt-1">
                        <strong>Approved wording:</strong> &ldquo;
                        {claim.matched_claim_text}&rdquo;
                      </p>
                    )}
                  </div>
                ))}
              </div>
              <p className="text-xs text-gray-500 mt-3">
                Claims with no pre-approved match are not violations — they
                carry the tag so you can see they were verified against the
                references and external sources instead. The claims library
                itself is never modified by this review.
              </p>
            </div>
          </details>
        )}

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
              {activeClaims ? (
                <ClaimsLibraryPreview
                  claims={activeClaims}
                  columnMapping={activeTab?.columnMapping}
                  unmappedColumns={activeTab?.unmappedColumns}
                />
              ) : activeDocUrl ? (
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
                    <h4 className="font-bold text-gray-900 text-xs mb-2 flex items-center gap-1.5 flex-wrap">
                      <span className="text-base">⚠️</span>
                      {issue.type}
                      {isClaimMatchStatus(issue.claim_match) && (
                        <ClaimMatchBadge
                          status={issue.claim_match}
                          claimId={issue.claim_id}
                          precision={issue.claim_precision}
                        />
                      )}
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
                  {isClaimMatchStatus(selectedIssue.claim_match) && (
                    <ClaimMatchBadge
                      status={selectedIssue.claim_match}
                      claimId={selectedIssue.claim_id}
                      precision={selectedIssue.claim_precision}
                    />
                  )}
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
