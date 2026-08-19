// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { ChatHeader } from "./ChatHeader";
import { Message, MessageSegment, ToolCall } from "./types";
import {
  ReviewResultsPanel,
  ClaimsLibraryPreview,
  ReviewIssue,
  ActivityEntry,
  ClaimsLibraryEntry,
  ClaimsReport,
  Phase,
  PreviewDoc,
} from "./ReviewResultsPanel";
import { FileUploadCards } from "./FileUploadCards";
import { DataSourceBar } from "./DataSourceBar";
import { useGlobal } from "@/app/context/GlobalContext";
import { AgentCoreClient } from "@/lib/agentcore-client";
import type { AgentPattern } from "@/lib/agentcore-client";
import {
  uploadFileToS3,
  getOriginalName,
  parseClaimsFile,
} from "@/services/uploadService";
import { useAuth } from "react-oidc-context";

// Strip any gateway prefix (e.g. "gateway_pubmed-search-target___pubmed_search" -> "pubmed_search")
function stripGatewayPrefix(name: string): string {
  const sep = name.lastIndexOf("___");
  return sep >= 0 ? name.slice(sep + 3) : name;
}

// The reviewer phase absorbs every tool we don't map explicitly (gateway searches
// inside sub-agents, file_read, ...), so unknown tools never stall the checklist.
const REVIEWERS_PHASE: Phase = {
  icon: "🔍",
  text: "Running reviewers in parallel",
  tools: [
    "run_generic_review",
    "run_external_review",
    "run_internal_review",
    "pubmed_search",
    "openfda_drug_search",
    "clinicaltrials_search",
    "nova_web_search",
    "read_reference_markdown",
  ],
};

// The claim phases only exist when a pre-approved claims library was uploaded —
// otherwise the backend never exposes those tools and the phases would never
// complete, stalling the checklist.
const CLAIM_PHASES: Phase[] = [
  { icon: "📝", text: "Extracting claims", tools: ["extract_claims"] },
  {
    icon: "🏷️",
    text: "Matching pre-approved claims",
    tools: ["match_claims", "get_claims"],
  },
];

function buildPhases(withClaims: boolean): Phase[] {
  return [
    {
      icon: "📄",
      text: "Reading documents",
      tools: ["process_pdf", "load_claims_library"],
    },
    { icon: "✂️", text: "Splitting into batches", tools: ["batch_content"] },
    ...(withClaims ? CLAIM_PHASES : []),
    REVIEWERS_PHASE,
    { icon: "🧩", text: "Merging reviewer findings", tools: ["get_reviews"] },
    { icon: "✅", text: "Writing final report", tools: ["file_write"] },
  ];
}

// Map a tool name to the index of the progress phase it belongs to
function toolToPhaseIdx(name: string, phases: Phase[]): number {
  const core = stripGatewayPrefix(name);
  const idx = phases.findIndex((phase) => phase.tools.includes(core));
  if (idx >= 0) return idx;
  return phases.findIndex((phase) => phase.text === REVIEWERS_PHASE.text);
}

// Display metadata keyed by the bare tool name (after stripping any gateway prefix)
const TOOL_META: Record<string, { label: string; icon: string }> = {
  process_pdf: { label: "Processing PDF", icon: "📄" },
  load_claims_library: { label: "Loading pre-approved claims", icon: "📗" },
  batch_content: { label: "Splitting into batches", icon: "✂️" },
  extract_claims: { label: "Extracting claims", icon: "📝" },
  match_claims: { label: "Matching pre-approved claims", icon: "🏷️" },
  get_claims: { label: "Merging claim matches", icon: "🗂️" },
  run_generic_review: { label: "Editorial", icon: "🧐" },
  run_external_review: { label: "External Evidence", icon: "🔬" },
  run_internal_review: { label: "Internal References", icon: "📚" },
  get_reviews: { label: "Merging reviews", icon: "🧩" },
  file_read: { label: "Reading file", icon: "📂" },
  file_write: { label: "Writing a review report", icon: "✅" },
  pubmed_search: { label: "Searching PubMed", icon: "🔬" },
  openfda_drug_search: { label: "Searching OpenFDA", icon: "💊" },
  clinicaltrials_search: { label: "Searching ClinicalTrials.gov", icon: "🏥" },
  nova_web_search: { label: "Nova Web Search", icon: "🌐" },
  read_reference_markdown: { label: "Reading reference", icon: "📎" },
};

// Turn "some_tool_name" into "Some tool name"
function humanize(name: string): string {
  const cleaned = name.replace(/_/g, " ").trim();
  return cleaned.charAt(0).toUpperCase() + cleaned.slice(1);
}

function getToolMeta(name: string): { label: string; icon: string } {
  const core = stripGatewayPrefix(name);
  return TOOL_META[core] ?? { label: humanize(core), icon: "🔧" };
}

// Grab just the filename from an s3:// URI or a local path, preferring the
// user-facing original name if the file was uploaded from this session
function basename(uri: string): string {
  const stripped = uri.split("?")[0];
  const original = getOriginalName(stripped);
  if (original) return original;
  const parts = stripped.split("/");
  return parts[parts.length - 1] || stripped;
}

// Pull a human-readable detail (query or filename) out of the accumulated tool input JSON
function extractDetailFromInput(
  toolName: string,
  input: string,
): string | null {
  try {
    const parsed = JSON.parse(input);
    const core = stripGatewayPrefix(toolName);
    // File/S3 tools — surface the filename
    const pathKeys = [
      "s3_uri",
      "s3_url",
      "uri",
      "path",
      "file_path",
      "key",
      "markdown_s3_uri",
      "batch_md_s3_uri",
      "extracted_claims_s3_uri",
    ];
    if (
      [
        "process_pdf",
        "load_claims_library",
        "batch_content",
        "extract_claims",
        "match_claims",
        "run_generic_review",
        "run_external_review",
        "run_internal_review",
        "read_reference_markdown",
        "file_read",
        "file_write",
      ].includes(core)
    ) {
      for (const key of pathKeys) {
        const value = parsed?.[key];
        if (typeof value === "string" && value.trim())
          return basename(value.trim());
      }
    }
    // Search tools — surface the query
    for (const key of [
      "query",
      "search_term",
      "term",
      "drug_name",
      "q",
      "text",
      "search",
    ]) {
      const value = parsed?.[key];
      if (typeof value === "string" && value.trim()) return value.trim();
    }
    // Generic fallback — any path-like value
    for (const key of pathKeys) {
      const value = parsed?.[key];
      if (typeof value === "string" && value.trim())
        return basename(value.trim());
    }
  } catch {
    /* input may be incomplete JSON */
  }
  return null;
}

function formatClock(date: Date): string {
  return date.toTimeString().slice(0, 8);
}

function truncate(s: string, n: number): string {
  return s.length > n ? s.slice(0, n - 1) + "…" : s;
}

// Extract review results URL from tool result
function extractReviewUrl(result: string): string | null {
  const match = result.match(/\[REVIEW_URL:(https?:\/\/[^\]]+)\]/);
  return match ? match[1] : null;
}

// Extract the claims report URL that `get_claims` appends to its own result
function extractClaimsUrl(result: string): string | null {
  const match = result.match(/\[CLAIMS_URL:(https?:\/\/[^\]]+)\]/);
  return match ? match[1] : null;
}

// Extract the parsed claims library URL that `load_claims_library` appends
function extractClaimsLibUrl(result: string): string | null {
  const match = result.match(/\[CLAIMS_LIB_URL:(https?:\/\/[^\]]+)\]/);
  return match ? match[1] : null;
}

// Strip the URL tags we inject into tool results before showing them in the UI
const URL_TAG_RE = /\n*\[(?:REVIEW|CLAIMS|CLAIMS_LIB)_URL:[^\]]+\]/g;

// Fetch content from pre-signed S3 URL
async function fetchUrl(url: string): Promise<string | null> {
  try {
    const response = await fetch(url);
    if (!response.ok) {
      console.warn(
        "[ReviewFetch] Failed to fetch review URL:",
        response.status,
        response.statusText,
      );
      return null;
    }
    return await response.text();
  } catch (err) {
    console.warn("[ReviewFetch] Error fetching review URL:", err);
    return null;
  }
}

// Try to parse review issues from file_write tool input JSON
function tryParseIssuesFromInput(input: string): ReviewIssue[] | null {
  try {
    const parsed = JSON.parse(input);
    // The input might be { path: "...", content: "..." } or { file_path: "...", data: "..." }
    const content = parsed.content || parsed.data;
    if (typeof content === "string") {
      const issues = JSON.parse(content);
      if (
        Array.isArray(issues) &&
        issues.length > 0 &&
        issues[0].page !== undefined
      ) {
        return issues;
      }
    }
    // Or the input itself might be the array
    if (
      Array.isArray(parsed) &&
      parsed.length > 0 &&
      parsed[0].page !== undefined
    ) {
      return parsed;
    }
  } catch {
    /* not valid JSON */
  }
  return null;
}

// Tool config from aws-exports.json (populated on mount)
interface ToolConfig {
  enabled: boolean;
  default_on: boolean;
}

// Fallback defaults for the external-source toggles (used until aws-exports.json loads)
const FALLBACK_TOOLS: Record<string, ToolConfig> = {
  pubmed: { enabled: true, default_on: true },
  openfda: { enabled: true, default_on: true },
  clinicaltrials: { enabled: true, default_on: true },
  nova: { enabled: true, default_on: true },
};

// Produce a human-sortable session id, e.g.
//   2026-05-06_14-12-34_a3f4e2b19c8d4a6b90f172ec35dea811
// AgentCore requires runtimeSessionId length >= 33, so we pad with a full
// UUID (32 hex chars) after the timestamp. The timestamp keeps S3 folders
// sortable in the console; the UUID guarantees uniqueness.
function newSessionId(): string {
  const d = new Date();
  const pad = (n: number) => n.toString().padStart(2, "0");
  const ts =
    `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}` +
    `_${pad(d.getHours())}-${pad(d.getMinutes())}-${pad(d.getSeconds())}`;
  const suffix = crypto.randomUUID().replace(/-/g, "");
  return `${ts}_${suffix}`;
}

// Outcome of parsing the claims spreadsheet at upload time. The review does not depend
// on it — the agent parses the file itself — so an error here only informs the user.
interface ClaimsPreviewState {
  status: "parsing" | "ready" | "error";
  totalClaims?: number;
  byStatus?: Record<string, number>;
  columnMapping?: Record<string, string>;
  unmappedColumns?: string[];
  message?: string;
}

// "12 claims · 10 approved, 2 expired" for the upload card
function claimsSummaryText(preview: ClaimsPreviewState): string {
  const total = `${preview.totalClaims ?? 0} claim${
    preview.totalClaims === 1 ? "" : "s"
  } parsed`;
  const byStatus = Object.entries(preview.byStatus || {})
    .map(([status, count]) => `${count} ${status.toLowerCase()}`)
    .join(", ");
  return byStatus ? `${total} · ${byStatus}` : total;
}

export default function ChatInterface() {
  const [messages, setMessages] = useState<Message[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [client, setClient] = useState<AgentCoreClient | null>(null);
  const [sessionId, setSessionId] = useState(() => newSessionId());

  const [toolsConfig, setToolsConfig] =
    useState<Record<string, ToolConfig>>(FALLBACK_TOOLS);
  const [enabledSources, setEnabledSources] = useState<Record<string, boolean>>(
    {},
  );
  // File upload state
  const [documentFile, setDocumentFile] = useState<File | null>(null);
  const [documentUrl, setDocumentUrl] = useState<string | null>(null);
  const [referenceFiles, setReferenceFiles] = useState<File[]>([]);
  const [claimsFile, setClaimsFile] = useState<File | null>(null);
  // Where the claims file was uploaded to when it was picked, reused at review start
  // so the same object is handed to the agent instead of being uploaded twice
  const [claimsS3Uri, setClaimsS3Uri] = useState<string | null>(null);
  const [claimsPreview, setClaimsPreview] = useState<ClaimsPreviewState | null>(
    null,
  );
  // Bumped on every claims-file change so a slow parse for a replaced file is ignored
  const claimsRequestRef = useRef(0);
  const [isUploading, setIsUploading] = useState(false);
  const [landingPreviewIdx, setLandingPreviewIdx] = useState<number>(0);

  // Review results state
  const [reviewIssues, setReviewIssues] = useState<ReviewIssue[]>([]);
  const [claimsReport, setClaimsReport] = useState<ClaimsReport | null>(null);
  // The parsed claims library, previewed beside the documents once it is loaded
  const [claimsLibrary, setClaimsLibrary] = useState<
    ClaimsLibraryEntry[] | null
  >(null);
  const [showReviewPanel, setShowReviewPanel] = useState<boolean>(false);
  const [activityLog, setActivityLog] = useState<ActivityEntry[]>([]);
  // Phase list for this run — grows by two steps when a claims library is used
  const [phases, setPhases] = useState<Phase[]>(() => buildPhases(false));
  // Per-phase counters: how many tool runs have started vs. completed per phase
  const [phaseStarted, setPhaseStarted] = useState<number[]>(() =>
    buildPhases(false).map(() => 0),
  );
  const [phaseDone, setPhaseDone] = useState<number[]>(() =>
    buildPhases(false).map(() => 0),
  );
  const [reviewStartedAt, setReviewStartedAt] = useState<number | null>(null);
  const lastReviewUrlRef = useRef<string | null>(null);
  const toolStartMap = useRef<
    Map<string, { name: string; activityIdx: number }>
  >(new Map());
  // Read inside the stream callback, where the `phases` state may be stale
  const phasesRef = useRef<Phase[]>(phases);

  const getEnabledSourceIds = () =>
    Object.entries(enabledSources)
      .filter(([, enabled]) => enabled)
      .map(([id]) => id);

  // Build a combined preview list (main doc + references) with blob URLs.
  // Managed in useMemo so object URLs are revoked when files change.
  const pdfPreviewDocs = useMemo(() => {
    const docs: { name: string; url: string; kind: "content" | "reference" }[] =
      [];
    if (documentFile && documentFile.type === "application/pdf") {
      docs.push({
        name: documentFile.name,
        url: URL.createObjectURL(documentFile),
        kind: "content",
      });
    }
    for (const f of referenceFiles) {
      if (f.type === "application/pdf") {
        docs.push({
          name: f.name,
          url: URL.createObjectURL(f),
          kind: "reference",
        });
      }
    }
    return docs;
  }, [documentFile, referenceFiles]);

  useEffect(() => {
    return () => {
      for (const d of pdfPreviewDocs) URL.revokeObjectURL(d.url);
    };
  }, [pdfPreviewDocs]);

  // The claims spreadsheet cannot be embedded like a PDF, so it becomes a table tab
  // as soon as it is parsed — at upload time, or during the run for a library the
  // agent loaded itself
  const previewDocs: PreviewDoc[] = useMemo(() => {
    if (!claimsLibrary) return pdfPreviewDocs;
    return [
      ...pdfPreviewDocs,
      {
        name: claimsFile?.name || "Pre-approved claims",
        kind: "claims" as const,
        claims: claimsLibrary,
        columnMapping: claimsPreview?.columnMapping,
        unmappedColumns: claimsPreview?.unmappedColumns,
      },
    ];
  }, [pdfPreviewDocs, claimsLibrary, claimsFile, claimsPreview]);

  const { isLoading, setIsLoading } = useGlobal();
  const auth = useAuth();

  // Upload and parse the claims library the moment it is picked, so the table can be
  // reviewed before starting a run. Failures are reported but never block the review.
  const handleClaimsChange = async (file: File | null) => {
    const seq = ++claimsRequestRef.current;
    setClaimsFile(file);
    setClaimsS3Uri(null);
    setClaimsLibrary(null);
    setClaimsPreview(file ? { status: "parsing" } : null);
    if (!file) return;

    const idToken = auth.user?.id_token;
    if (!idToken) {
      setClaimsPreview({
        status: "error",
        message: "Authentication required.",
      });
      return;
    }

    try {
      const uri = await uploadFileToS3(file, idToken);
      if (seq !== claimsRequestRef.current) return;
      setClaimsS3Uri(uri);
      const parsed = await parseClaimsFile(uri, file.name, idToken);
      if (seq !== claimsRequestRef.current) return;
      setClaimsLibrary(parsed.claims as unknown as ClaimsLibraryEntry[]);
      setClaimsPreview({
        status: "ready",
        totalClaims: parsed.totalClaims,
        byStatus: parsed.byStatus,
        columnMapping: parsed.columnMapping,
        unmappedColumns: parsed.unmappedColumns,
      });
    } catch (err) {
      if (seq !== claimsRequestRef.current) return;
      setClaimsPreview({
        status: "error",
        message: err instanceof Error ? err.message : "Unknown error",
      });
    }
  };

  // Load agent configuration and create client on mount
  useEffect(() => {
    async function loadConfig() {
      try {
        const response = await fetch("/aws-exports.json");
        if (!response.ok) throw new Error("Failed to load configuration");
        const config = await response.json();

        if (!config.agentRuntimeArn) {
          throw new Error("Agent Runtime ARN not found in configuration");
        }

        const agentClient = new AgentCoreClient({
          runtimeArn: config.agentRuntimeArn,
          region: config.awsRegion || "us-east-1",
          pattern: (config.agentPattern ||
            "medical-content-review") as AgentPattern,
        });
        setClient(agentClient);

        const tools: Record<string, ToolConfig> =
          config.tools || FALLBACK_TOOLS;
        setToolsConfig(tools);

        const defaults: Record<string, boolean> = {};
        for (const [id, cfg] of Object.entries(tools)) {
          if (cfg.enabled) defaults[id] = cfg.default_on;
        }
        setEnabledSources(defaults);
      } catch (err) {
        const errorMessage =
          err instanceof Error ? err.message : "Unknown error";
        setError(`Configuration error: ${errorMessage}`);
      }
    }
    loadConfig();
  }, []);

  const sendMessage = async (
    userMessage: string,
    overrideContentUri?: string,
    overrideReferenceUris?: string[],
    contentPdfName?: string,
    referenceNames?: string[],
    overrideSessionId?: string,
    claimsUri?: string,
    claimsName?: string,
  ) => {
    if (!userMessage.trim() || !client) return;
    setError(null);

    const newUserMessage: Message = {
      role: "user",
      content: userMessage,
      timestamp: new Date().toISOString(),
    };
    setMessages((prev) => [...prev, newUserMessage]);
    setIsLoading(true);
    setReviewStartedAt(Date.now());
    // The phase list depends on whether this run has a claims library, so build
    // it here and keep it in a ref for the stream callback.
    const runPhases = buildPhases(Boolean(claimsUri));
    phasesRef.current = runPhases;
    setPhases(runPhases);
    setPhaseStarted(runPhases.map(() => 0));
    setPhaseDone(runPhases.map(() => 0));
    setClaimsReport(null);
    // Keep a library that was already parsed at upload time — the agent reads the same
    // file and writes the same JSON, so there is nothing to re-fetch before showing it
    if (!claimsUri) setClaimsLibrary(null);
    setActivityLog([]);
    toolStartMap.current.clear();

    const assistantResponse: Message = {
      role: "assistant",
      content: "",
      timestamp: new Date().toISOString(),
    };
    setMessages((prev) => [...prev, assistantResponse]);

    try {
      const accessToken = auth.user?.access_token;
      if (!accessToken)
        throw new Error("Authentication required. Please log in again.");

      const segments: MessageSegment[] = [];
      const toolCallMap = new Map<string, ToolCall>();

      const updateMessage = () => {
        const content = segments
          .filter(
            (s): s is Extract<MessageSegment, { type: "text" }> =>
              s.type === "text",
          )
          .map((s) => s.content)
          .join("");

        setMessages((prev) => {
          const updated = [...prev];
          updated[updated.length - 1] = {
            ...updated[updated.length - 1],
            content,
            segments: [...segments],
          };
          return updated;
        });
      };

      const enabledSourceIds = getEnabledSourceIds();
      const contentPdfUri = overrideContentUri;
      const referenceUris =
        overrideReferenceUris && overrideReferenceUris.length > 0
          ? overrideReferenceUris
          : undefined;

      await client.invoke(
        userMessage,
        overrideSessionId ?? sessionId,
        accessToken,
        (event) => {
          switch (event.type) {
            case "text": {
              const prev = segments[segments.length - 1];
              if (prev && prev.type === "tool") {
                for (const tc of toolCallMap.values()) {
                  if (tc.status === "streaming" || tc.status === "executing")
                    tc.status = "complete";
                }
              }
              const last = segments[segments.length - 1];
              if (last && last.type === "text") {
                last.content += event.content;
              } else {
                segments.push({ type: "text", content: event.content });
              }
              updateMessage();
              break;
            }
            case "tool_use_start": {
              const tc: ToolCall = {
                toolUseId: event.toolUseId,
                name: event.name,
                input: "",
                status: "streaming",
              };
              toolCallMap.set(event.toolUseId, tc);
              segments.push({ type: "tool", toolCall: tc });

              const meta = getToolMeta(event.name);
              const phaseIdx = toolToPhaseIdx(event.name, phasesRef.current);
              if (phaseIdx >= 0) {
                setPhaseStarted((prev) => {
                  const next = [...prev];
                  next[phaseIdx] += 1;
                  return next;
                });
              }

              setActivityLog((prev) => {
                const next: ActivityEntry = {
                  timestamp: formatClock(new Date()),
                  icon: meta.icon,
                  label: meta.label,
                  status: "running",
                };
                toolStartMap.current.set(event.toolUseId, {
                  name: event.name,
                  activityIdx: prev.length,
                });
                return [...prev, next];
              });
              if (event.name === "file_write") {
                setShowReviewPanel(true);
              }
              updateMessage();
              break;
            }
            case "tool_use_delta": {
              const tc = toolCallMap.get(event.toolUseId);
              if (tc) {
                tc.input += event.input;
                const detail = extractDetailFromInput(tc.name, tc.input);
                const started = toolStartMap.current.get(event.toolUseId);
                if (detail && started) {
                  setActivityLog((prev) => {
                    if (started.activityIdx >= prev.length) return prev;
                    const existing = prev[started.activityIdx];
                    if (existing.detail === detail) return prev;
                    const next = [...prev];
                    next[started.activityIdx] = {
                      ...existing,
                      detail: truncate(detail, 80),
                    };
                    return next;
                  });
                }
              }
              updateMessage();
              break;
            }
            case "tool_result": {
              const tc = toolCallMap.get(event.toolUseId);
              if (tc) {
                const failed = event.status === "error";
                tc.result = event.result;
                tc.status = "complete";
                const started = toolStartMap.current.get(event.toolUseId);
                if (started) {
                  const phaseIdx = toolToPhaseIdx(
                    started.name,
                    phasesRef.current,
                  );
                  // Only a successful call advances its phase — a phase ticked off by a
                  // failed tool would hide the very step that did not happen
                  if (phaseIdx >= 0 && !failed) {
                    setPhaseDone((prev) => {
                      const next = [...prev];
                      next[phaseIdx] += 1;
                      return next;
                    });
                  }
                  const rawOutput =
                    typeof event.result === "string"
                      ? event.result
                      : JSON.stringify(event.result, null, 2);
                  const cleanOutput = rawOutput.replace(URL_TAG_RE, "").trim();
                  setActivityLog((prev) => {
                    if (started.activityIdx >= prev.length) return prev;
                    const next = [...prev];
                    next[started.activityIdx] = {
                      ...next[started.activityIdx],
                      status: failed ? "error" : "done",
                      output: cleanOutput || undefined,
                    };
                    return next;
                  });
                }

                // The parsed claims library becomes a preview tab, so the user can
                // see what their content was matched against
                if (tc.name === "load_claims_library") {
                  const libUrl = extractClaimsLibUrl(tc.result || "");
                  if (libUrl) {
                    fetchUrl(libUrl).then((content) => {
                      if (!content) return;
                      try {
                        const parsed = JSON.parse(content);
                        if (Array.isArray(parsed)) {
                          setClaimsLibrary(parsed as ClaimsLibraryEntry[]);
                        }
                      } catch {
                        /* ignore */
                      }
                    });
                  }
                  if (tc.result) {
                    tc.result = tc.result.replace(URL_TAG_RE, "").trim();
                  }
                }

                // The claims report is published while the reviewers are still
                // running, so the match tags show up in the UI early.
                if (tc.name === "get_claims") {
                  const claimsUrl = extractClaimsUrl(tc.result || "");
                  if (claimsUrl) {
                    fetchUrl(claimsUrl).then((content) => {
                      if (!content) return;
                      try {
                        const parsed = JSON.parse(content);
                        if (parsed && Array.isArray(parsed.claims)) {
                          setClaimsReport(parsed as ClaimsReport);
                        }
                      } catch {
                        /* ignore */
                      }
                    });
                  }
                  if (tc.result) {
                    tc.result = tc.result.replace(URL_TAG_RE, "").trim();
                  }
                }

                if (tc.name === "file_write") {
                  const reviewUrl = extractReviewUrl(tc.result || "");
                  if (reviewUrl) {
                    lastReviewUrlRef.current = reviewUrl;
                    setShowReviewPanel(true);
                    fetchUrl(reviewUrl).then((content) => {
                      if (content) {
                        try {
                          const parsed = JSON.parse(content);
                          setReviewIssues(Array.isArray(parsed) ? parsed : []);
                        } catch {
                          /* ignore */
                        }
                      }
                    });
                  }
                  // Fallback: try to parse issues directly from the tool input
                  if (!reviewUrl && tc.input) {
                    const fallbackIssues = tryParseIssuesFromInput(tc.input);
                    if (fallbackIssues) {
                      setReviewIssues(fallbackIssues);
                      setShowReviewPanel(true);
                    }
                  }
                  if (tc.result) {
                    tc.result = tc.result.replace(URL_TAG_RE, "").trim();
                  }
                }
              }
              updateMessage();
              break;
            }
            case "message": {
              if (event.role === "assistant") {
                for (const tc of toolCallMap.values()) {
                  if (tc.status === "streaming") tc.status = "executing";
                }
                updateMessage();
              }
              break;
            }
          }
        },
        enabledSourceIds,
        contentPdfUri?.startsWith("s3://") ? contentPdfUri : undefined,
        referenceUris?.length ? referenceUris : undefined,
        contentPdfName || undefined,
        referenceNames && referenceNames.length > 0
          ? referenceNames
          : undefined,
        claimsUri?.startsWith("s3://") ? claimsUri : undefined,
        claimsName || undefined,
      );
    } catch (err) {
      if (err instanceof DOMException && err.name === "AbortError") return;
      const errorMessage = err instanceof Error ? err.message : "Unknown error";
      setError(`Failed to get response: ${errorMessage}`);

      setMessages((prev) => {
        const updated = [...prev];
        updated[updated.length - 1] = {
          ...updated[updated.length - 1],
          content:
            "I apologize, but I encountered an error processing your request. Please try again.",
        };
        return updated;
      });
    } finally {
      setIsLoading(false);
    }
  };

  const startReview = async () => {
    const idToken = auth.user?.id_token;
    if (!idToken) {
      setError("Authentication required.");
      return;
    }

    if (!documentFile) {
      setError(
        "Please attach a medical content PDF before starting the review.",
      );
      return;
    }

    // Stamp the session id with the moment the review actually starts, so the
    // S3 folder name reflects "now" rather than page-load time.
    const freshSessionId = newSessionId();
    setSessionId(freshSessionId);

    let contentUri: string | undefined;
    let refUris: string[] = [];
    let claimsUri: string | undefined;

    setIsUploading(true);
    try {
      contentUri = await uploadFileToS3(documentFile, idToken);
      if (referenceFiles.length > 0) {
        refUris = await Promise.all(
          referenceFiles.map((f) => uploadFileToS3(f, idToken)),
        );
      }
      if (claimsFile) {
        // Already uploaded when the file was picked, unless that upload failed
        claimsUri = claimsS3Uri ?? (await uploadFileToS3(claimsFile, idToken));
      }
    } catch (err) {
      setError(
        `Upload failed: ${
          err instanceof Error ? err.message : "Unknown error"
        }`,
      );
      setIsUploading(false);
      return;
    }
    setIsUploading(false);

    const prompt = claimsFile
      ? "Please review the attached medical content document for adherence issues. Analyze all pages, extract the claims and match them against the pre-approved claims library first, cross-check the remaining claims against references, and produce a detailed review report."
      : "Please review the attached medical content document for adherence issues. Analyze all pages, cross-check claims against references, and produce a detailed review report.";
    const contentName = documentFile.name;
    const refNames = referenceFiles.map((f) => f.name);
    setShowReviewPanel(true);
    sendMessage(
      prompt,
      contentUri,
      refUris.length > 0 ? refUris : undefined,
      contentName,
      refNames.length > 0 ? refNames : undefined,
      freshSessionId,
      claimsUri,
      claimsFile?.name,
    );
  };

  const startNewChat = () => {
    client?.abort();
    setSessionId(newSessionId());
    setMessages([]);
    setError(null);
    setIsLoading(false);
    setReviewIssues([]);
    setClaimsReport(null);
    setClaimsLibrary(null);
    setShowReviewPanel(false);
    setActivityLog([]);
    const freshPhases = buildPhases(false);
    phasesRef.current = freshPhases;
    setPhases(freshPhases);
    setPhaseStarted(freshPhases.map(() => 0));
    setPhaseDone(freshPhases.map(() => 0));
    setReviewStartedAt(null);
    toolStartMap.current.clear();
    lastReviewUrlRef.current = null;
    const defaults: Record<string, boolean> = {};
    for (const [id, cfg] of Object.entries(toolsConfig)) {
      if (cfg.enabled) defaults[id] = cfg.default_on;
    }
    setEnabledSources(defaults);
    setDocumentFile(null);
    setDocumentUrl(null);
    setReferenceFiles([]);
    claimsRequestRef.current += 1;
    setClaimsFile(null);
    setClaimsS3Uri(null);
    setClaimsPreview(null);
    setLandingPreviewIdx(0);
    setIsUploading(false);
  };

  const isInitialState = messages.length === 0;
  const hasAssistantMessages = messages.some((m) => m.role === "assistant");

  return (
    <div className="flex flex-col h-screen w-full">
      <div className="flex-none">
        <ChatHeader
          onNewChat={startNewChat}
          canStartNewChat={hasAssistantMessages}
        />
        {error && (
          <div className="bg-red-50 dark:bg-red-950/50 border-l-4 border-red-500 p-4 mx-4 mt-2">
            <p className="text-sm text-red-700 dark:text-red-400">{error}</p>
          </div>
        )}
      </div>

      {isInitialState && !isLoading ? (
        <div className="grow overflow-auto bg-gradient-to-br from-slate-800 via-slate-700 to-slate-900">
          <div className="container mx-auto px-6 py-8 max-w-7xl space-y-6">
            <FileUploadCards
              documentFile={documentFile}
              onDocumentChange={(file) => {
                setDocumentFile(file);
                if (file && file.type === "application/pdf") {
                  setDocumentUrl(URL.createObjectURL(file));
                } else {
                  setDocumentUrl(null);
                }
              }}
              referenceFiles={referenceFiles}
              onReferenceFilesChange={setReferenceFiles}
              claimsFile={claimsFile}
              onClaimsChange={handleClaimsChange}
              claimsStatus={claimsPreview?.status}
              claimsSummary={
                claimsPreview?.status === "ready"
                  ? claimsSummaryText(claimsPreview)
                  : undefined
              }
              claimsError={claimsPreview?.message}
            />

            {/* Preview of everything attached: PDFs plus the parsed claims table */}
            {previewDocs.length > 0 && (
              <details
                open
                className="bg-white dark:bg-gray-800 rounded-2xl shadow-lg border border-gray-200 dark:border-gray-700 overflow-hidden group"
              >
                <summary className="bg-gradient-to-r from-purple-50 to-pink-50 dark:from-purple-950/40 dark:to-pink-950/40 px-6 py-4 border-b border-gray-200 dark:border-gray-700 cursor-pointer select-none list-none flex items-center justify-between">
                  <h3 className="text-lg font-semibold text-gray-900 dark:text-white">
                    Attachment Preview
                  </h3>
                  <svg
                    className="w-5 h-5 text-gray-500 dark:text-gray-400 transition-transform group-open:rotate-90"
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
                {previewDocs.length > 1 && (
                  <div className="flex items-end gap-1 px-3 pt-2 bg-gray-50 dark:bg-gray-900/40 border-b border-gray-200 dark:border-gray-700 overflow-x-auto">
                    {previewDocs.map((doc, idx) => {
                      const isActive =
                        idx ===
                        Math.min(landingPreviewIdx, previewDocs.length - 1);
                      return (
                        <button
                          key={idx}
                          type="button"
                          onClick={(e) => {
                            e.preventDefault();
                            setLandingPreviewIdx(idx);
                          }}
                          className={`max-w-[18rem] shrink-0 px-3 py-2 rounded-t-lg text-xs font-medium flex items-center gap-1.5 border-t border-x transition-colors ${
                            isActive
                              ? "bg-white dark:bg-gray-800 text-gray-900 dark:text-white border-gray-200 dark:border-gray-700 shadow-sm"
                              : "bg-transparent text-gray-600 dark:text-gray-400 hover:bg-white/60 border-transparent"
                          }`}
                          title={doc.name}
                        >
                          <span className="text-sm leading-none">
                            {doc.kind === "content"
                              ? "📄"
                              : doc.kind === "claims"
                              ? "📗"
                              : "📎"}
                          </span>
                          <span className="truncate">{doc.name}</span>
                        </button>
                      );
                    })}
                  </div>
                )}
                <div className="h-[600px] overflow-hidden">
                  {(() => {
                    const active =
                      previewDocs[
                        Math.min(landingPreviewIdx, previewDocs.length - 1)
                      ];
                    if (active?.kind === "claims") {
                      return (
                        <ClaimsLibraryPreview
                          claims={active.claims || []}
                          columnMapping={active.columnMapping}
                          unmappedColumns={active.unmappedColumns}
                        />
                      );
                    }
                    return (
                      <iframe
                        src={active?.url}
                        className="w-full h-full"
                        title="PDF Document"
                      />
                    );
                  })()}
                </div>
              </details>
            )}

            {/* External data source toggles */}
            <DataSourceBar
              toolsConfig={toolsConfig}
              enabledSources={enabledSources}
              onToggle={(id) =>
                setEnabledSources((prev) => ({ ...prev, [id]: !prev[id] }))
              }
            />

            {/* Start AI Review Button */}
            <div className="text-center py-4">
              <button
                onClick={startReview}
                disabled={!documentFile || isUploading}
                className="group relative inline-flex items-center gap-3 bg-gradient-to-r from-indigo-600 to-blue-600 hover:from-indigo-700 hover:to-blue-700 disabled:from-gray-400 disabled:to-gray-500 text-white font-bold py-5 px-12 rounded-xl text-lg shadow-xl transform transition-all hover:scale-105 disabled:hover:scale-100 disabled:cursor-not-allowed"
              >
                <svg
                  className="w-6 h-6"
                  fill="none"
                  stroke="currentColor"
                  viewBox="0 0 24 24"
                >
                  <path
                    strokeLinecap="round"
                    strokeLinejoin="round"
                    strokeWidth={2}
                    d="M14.752 11.168l-3.197-2.132A1 1 0 0010 9.87v4.263a1 1 0 001.555.832l3.197-2.132a1 1 0 000-1.664z"
                  />
                  <path
                    strokeLinecap="round"
                    strokeLinejoin="round"
                    strokeWidth={2}
                    d="M21 12a9 9 0 11-18 0 9 9 0 0118 0z"
                  />
                </svg>
                {isUploading ? "Uploading files..." : "Start AI Review"}
                <svg
                  className="w-5 h-5 transform group-hover:translate-x-1 transition-transform"
                  fill="none"
                  stroke="currentColor"
                  viewBox="0 0 24 24"
                >
                  <path
                    strokeLinecap="round"
                    strokeLinejoin="round"
                    strokeWidth={2}
                    d="M9 5l7 7-7 7"
                  />
                </svg>
              </button>
              {!documentFile && (
                <p className="text-sm text-gray-400 mt-3">
                  Please upload a document to begin
                </p>
              )}
            </div>
          </div>
        </div>
      ) : showReviewPanel ? (
        <div className="grow overflow-hidden">
          <ReviewResultsPanel
            issues={reviewIssues}
            claimsReport={claimsReport}
            isLoading={isLoading}
            activityLog={activityLog}
            phases={phases}
            phaseStarted={phaseStarted}
            phaseDone={phaseDone}
            startedAt={reviewStartedAt}
            documentUrl={documentUrl}
            previewDocs={previewDocs}
            onNewReview={startNewChat}
          />
        </div>
      ) : isLoading ? null : null}
    </div>
  );
}
