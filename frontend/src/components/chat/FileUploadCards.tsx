// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
"use client";

import { useCallback, useRef } from "react";

const ACCEPTED_TYPES = [
  "application/pdf",
  "text/plain",
  "text/csv",
  "text/markdown",
  "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
  "application/msword",
];
const ACCEPTED_EXTENSIONS = ".pdf,.txt,.csv,.md,.doc,.docx";

// The pre-approved claims library is a spreadsheet, not a document. Legacy .xls is
// left out because the backend parser reads the OOXML formats and CSV only.
const CLAIMS_EXTENSIONS = ".xlsx,.xlsm,.csv";

interface FileUploadCardsProps {
  documentFile: File | null;
  onDocumentChange: (file: File | null) => void;
  referenceFiles: File[];
  onReferenceFilesChange: (files: File[]) => void;
  claimsFile: File | null;
  onClaimsChange: (file: File | null) => void;
  // Result of parsing the claims spreadsheet on upload, so a header the parser could
  // not read is visible before the review runs rather than after
  claimsStatus?: "parsing" | "ready" | "error";
  claimsSummary?: string;
  claimsError?: string;
}

export function FileUploadCards({
  documentFile,
  onDocumentChange,
  referenceFiles,
  onReferenceFilesChange,
  claimsFile,
  onClaimsChange,
  claimsStatus,
  claimsSummary,
  claimsError,
}: FileUploadCardsProps) {
  const documentInputRef = useRef<HTMLInputElement>(null);
  const referenceInputRef = useRef<HTMLInputElement>(null);
  const claimsInputRef = useRef<HTMLInputElement>(null);

  const isAccepted = (file: File) =>
    ACCEPTED_TYPES.includes(file.type) ||
    ACCEPTED_EXTENSIONS.split(",").some((ext) =>
      file.name.toLowerCase().endsWith(ext),
    );

  const isClaimsAccepted = (file: File) =>
    CLAIMS_EXTENSIONS.split(",").some((ext) =>
      file.name.toLowerCase().endsWith(ext),
    );

  const handleClaimsDrop = useCallback(
    (e: React.DragEvent<HTMLDivElement>) => {
      e.preventDefault();
      const file = e.dataTransfer.files[0];
      if (file && isClaimsAccepted(file)) onClaimsChange(file);
    },
    [onClaimsChange],
  );

  const handleDocumentDrop = useCallback(
    (e: React.DragEvent<HTMLDivElement>) => {
      e.preventDefault();
      const file = e.dataTransfer.files[0];
      if (file && isAccepted(file)) onDocumentChange(file);
    },
    [onDocumentChange],
  );

  const handleReferenceDrop = useCallback(
    (e: React.DragEvent<HTMLDivElement>) => {
      e.preventDefault();
      const files = Array.from(e.dataTransfer.files).filter(isAccepted);
      if (files.length) onReferenceFilesChange([...referenceFiles, ...files]);
    },
    [referenceFiles, onReferenceFilesChange],
  );

  const handleDragOver = (e: React.DragEvent<HTMLDivElement>) =>
    e.preventDefault();

  return (
    <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
      {/* Document Upload */}
      <div className="bg-white dark:bg-gray-800 rounded-2xl shadow-lg border border-gray-200 dark:border-gray-700 overflow-hidden">
        <div className="bg-gradient-to-r from-indigo-50 to-blue-50 dark:from-indigo-950/40 dark:to-blue-950/40 px-6 py-4 border-b border-gray-200 dark:border-gray-700">
          <div className="flex justify-between items-center">
            <h2 className="text-lg font-semibold text-gray-900 dark:text-white flex items-center gap-2">
              <svg
                className="w-5 h-5 text-indigo-600"
                fill="none"
                stroke="currentColor"
                viewBox="0 0 24 24"
              >
                <path
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  strokeWidth={2}
                  d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z"
                />
              </svg>
              Document to Review
            </h2>
            {documentFile && (
              <button
                onClick={(e) => {
                  e.stopPropagation();
                  onDocumentChange(null);
                }}
                className="text-gray-500 hover:text-red-600 transition-colors"
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
                    d="M6 18L18 6M6 6l12 12"
                  />
                </svg>
              </button>
            )}
          </div>
        </div>
        <div
          onDrop={handleDocumentDrop}
          onDragOver={handleDragOver}
          onClick={() => documentInputRef.current?.click()}
          className="p-8 border-4 border-dashed border-indigo-200 dark:border-indigo-800 m-6 rounded-xl hover:border-indigo-400 hover:bg-indigo-50/50 dark:hover:bg-indigo-950/30 transition-all cursor-pointer h-64 flex items-center justify-center"
        >
          <input
            ref={documentInputRef}
            type="file"
            accept={ACCEPTED_EXTENSIONS}
            onChange={(e) => {
              const f = e.target.files?.[0];
              if (f && isAccepted(f)) onDocumentChange(f);
            }}
            className="hidden"
          />
          <div className="text-center">
            <svg
              className="mx-auto h-12 w-12 text-indigo-400 mb-3"
              fill="none"
              stroke="currentColor"
              viewBox="0 0 24 24"
            >
              <path
                strokeLinecap="round"
                strokeLinejoin="round"
                strokeWidth={2}
                d="M7 16a4 4 0 01-.88-7.903A5 5 0 1115.9 6L16 6a5 5 0 011 9.9M15 13l-3-3m0 0l-3 3m3-3v12"
              />
            </svg>
            {documentFile ? (
              <div>
                <p className="text-lg font-semibold text-indigo-600 mb-2">
                  ✓ {documentFile.name}
                </p>
                <p className="text-xs text-gray-500 dark:text-gray-400">
                  Click to change or drop new file
                </p>
              </div>
            ) : (
              <div>
                <p className="text-lg font-semibold text-gray-700 dark:text-gray-300 mb-2">
                  Drop file or click to select
                </p>
                <p className="text-xs text-gray-500 dark:text-gray-400">
                  The medical content document to review
                </p>
                <p className="text-xs text-gray-500 dark:text-gray-400">
                  Supported formats: PDF, TXT, CSV, MD, DOC, DOCX
                </p>
              </div>
            )}
          </div>
        </div>
      </div>

      {/* References Upload */}
      <div className="bg-white dark:bg-gray-800 rounded-2xl shadow-lg border border-gray-200 dark:border-gray-700 overflow-hidden">
        <div className="bg-gradient-to-r from-green-50 to-emerald-50 dark:from-green-950/40 dark:to-emerald-950/40 px-6 py-4 border-b border-gray-200 dark:border-gray-700">
          <div className="flex justify-between items-center">
            <h2 className="text-lg font-semibold text-gray-900 dark:text-white flex items-center gap-2">
              <svg
                className="w-5 h-5 text-green-600"
                fill="none"
                stroke="currentColor"
                viewBox="0 0 24 24"
              >
                <path
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  strokeWidth={2}
                  d="M12 6.253v13m0-13C10.832 5.477 9.246 5 7.5 5S4.168 5.477 3 6.253v13C4.168 18.477 5.754 18 7.5 18s3.332.477 4.5 1.253m0-13C13.168 5.477 14.754 5 16.5 5c1.747 0 3.332.477 4.5 1.253v13C19.832 18.477 18.247 18 16.5 18c-1.746 0-3.332.477-4.5 1.253"
                />
              </svg>
              Reference Materials
            </h2>
            {referenceFiles.length > 0 && (
              <button
                onClick={(e) => {
                  e.stopPropagation();
                  onReferenceFilesChange([]);
                }}
                className="text-gray-500 hover:text-red-600 transition-colors"
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
                    d="M6 18L18 6M6 6l12 12"
                  />
                </svg>
              </button>
            )}
          </div>
        </div>
        <div
          onDrop={handleReferenceDrop}
          onDragOver={handleDragOver}
          onClick={() => referenceInputRef.current?.click()}
          className="p-8 border-4 border-dashed border-green-200 dark:border-green-800 m-6 rounded-xl hover:border-green-400 hover:bg-green-50/50 dark:hover:bg-green-950/30 transition-all cursor-pointer h-64 flex items-center justify-center"
        >
          <input
            ref={referenceInputRef}
            type="file"
            accept={ACCEPTED_EXTENSIONS}
            multiple
            onChange={(e) => {
              const files = Array.from(e.target.files || []).filter(isAccepted);
              if (files.length)
                onReferenceFilesChange([...referenceFiles, ...files]);
            }}
            className="hidden"
          />
          <div className="text-center w-full">
            <svg
              className="mx-auto h-12 w-12 text-green-400 mb-3"
              fill="none"
              stroke="currentColor"
              viewBox="0 0 24 24"
            >
              <path
                strokeLinecap="round"
                strokeLinejoin="round"
                strokeWidth={2}
                d="M19 11H5m14 0a2 2 0 012 2v6a2 2 0 01-2 2H5a2 2 0 01-2-2v-6a2 2 0 012-2m14 0V9a2 2 0 00-2-2M5 11V9a2 2 0 012-2m0 0V5a2 2 0 012-2h6a2 2 0 012 2v2M7 7h10"
              />
            </svg>
            <p className="text-lg font-semibold text-gray-700 dark:text-gray-300 mb-2">
              {referenceFiles.length > 0
                ? `✓ ${referenceFiles.length} reference(s) added`
                : "Drop references or click to select"}
            </p>
            {referenceFiles.length === 0 && (
              <p className="text-xs text-gray-500 dark:text-gray-400">
                Source documents to verify claims against (PDF, TXT, CSV, MD,
                DOC, DOCX)
              </p>
            )}
            {referenceFiles.length > 0 && (
              <div className="mt-3 max-h-32 overflow-y-auto px-2">
                {referenceFiles.map((file, idx) => (
                  <div
                    key={idx}
                    className="text-xs text-gray-600 dark:text-gray-400 bg-gray-50 dark:bg-gray-700 rounded px-2 py-1 mb-1 flex items-center gap-1"
                  >
                    <svg
                      className="w-3 h-3 text-green-600 flex-shrink-0"
                      fill="none"
                      stroke="currentColor"
                      viewBox="0 0 24 24"
                    >
                      <path
                        strokeLinecap="round"
                        strokeLinejoin="round"
                        strokeWidth={2}
                        d="M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z"
                      />
                    </svg>
                    <span className="truncate">{file.name}</span>
                  </div>
                ))}
              </div>
            )}
            <p className="text-xs text-gray-500 dark:text-gray-400 mt-2">
              Multiple files supported
            </p>
          </div>
        </div>
      </div>

      {/* Pre-approved Claims Upload */}
      <div className="bg-white dark:bg-gray-800 rounded-2xl shadow-lg border border-gray-200 dark:border-gray-700 overflow-hidden">
        <div className="bg-gradient-to-r from-amber-50 to-yellow-50 dark:from-amber-950/40 dark:to-yellow-950/40 px-6 py-4 border-b border-gray-200 dark:border-gray-700">
          <div className="flex justify-between items-center">
            <h2 className="text-lg font-semibold text-gray-900 dark:text-white flex items-center gap-2">
              <svg
                className="w-5 h-5 text-amber-600"
                fill="none"
                stroke="currentColor"
                viewBox="0 0 24 24"
              >
                <path
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  strokeWidth={2}
                  d="M9 12l2 2 4-4M7.835 4.697a3.42 3.42 0 001.946-.806 3.42 3.42 0 014.438 0 3.42 3.42 0 001.946.806 3.42 3.42 0 013.138 3.138 3.42 3.42 0 00.806 1.946 3.42 3.42 0 010 4.438 3.42 3.42 0 00-.806 1.946 3.42 3.42 0 01-3.138 3.138 3.42 3.42 0 00-1.946.806 3.42 3.42 0 01-4.438 0 3.42 3.42 0 00-1.946-.806 3.42 3.42 0 01-3.138-3.138 3.42 3.42 0 00-.806-1.946 3.42 3.42 0 010-4.438 3.42 3.42 0 00.806-1.946 3.42 3.42 0 013.138-3.138z"
                />
              </svg>
              Pre-Approved Claims
            </h2>
            <div className="flex items-center gap-2">
              {/* The parser resolves headers by alias, and none of that is visible from a
                  file dialog. Spelling it out inline made this card taller than the other
                  two, so it hides behind a hint that stays reachable after upload too. */}
              <div className="relative group">
                <button
                  type="button"
                  aria-label="Which columns the claims file needs"
                  className="w-5 h-5 flex items-center justify-center rounded-full border border-amber-400/70 dark:border-amber-600/70 text-[11px] font-bold leading-none text-amber-700 dark:text-amber-300 hover:bg-amber-100 dark:hover:bg-amber-900/50 transition-colors"
                >
                  ?
                </button>
                {/* w-64, not wider: the card clips overflow, and at the lg breakpoint a
                    column is only ~310px, so a wider panel would lose its right edge */}
                <div className="pointer-events-none absolute right-0 top-7 z-20 w-64 rounded-lg border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 shadow-xl px-3 py-2 text-left text-[11px] leading-snug text-gray-600 dark:text-gray-400 opacity-0 invisible transition-opacity group-hover:opacity-100 group-hover:visible group-focus-within:opacity-100 group-focus-within:visible">
                  <p>
                    <span className="font-semibold text-gray-700 dark:text-gray-300">
                      Required column:
                    </span>{" "}
                    the claim text — a header like Claim Text, Claim, Approved
                    Claim, Statement or Wording.
                  </p>
                  <p className="mt-1">
                    <span className="font-semibold text-gray-700 dark:text-gray-300">
                      Optional:
                    </span>{" "}
                    Claim ID, Status, Type, Approved Date, Expiry Date,
                    Reference, Source, Audience, Restrictions, MLR Job Code. Any
                    other column is kept as an extra.
                  </p>
                </div>
              </div>
              {claimsFile && (
                <button
                  onClick={(e) => {
                    e.stopPropagation();
                    onClaimsChange(null);
                  }}
                  className="text-gray-500 hover:text-red-600 transition-colors"
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
                      d="M6 18L18 6M6 6l12 12"
                    />
                  </svg>
                </button>
              )}
            </div>
          </div>
        </div>
        <div
          onDrop={handleClaimsDrop}
          onDragOver={handleDragOver}
          onClick={() => claimsInputRef.current?.click()}
          className="p-8 border-4 border-dashed border-amber-200 dark:border-amber-800 m-6 rounded-xl hover:border-amber-400 hover:bg-amber-50/50 dark:hover:bg-amber-950/30 transition-all cursor-pointer h-64 flex items-center justify-center"
        >
          <input
            ref={claimsInputRef}
            type="file"
            accept={CLAIMS_EXTENSIONS}
            onChange={(e) => {
              const f = e.target.files?.[0];
              if (f && isClaimsAccepted(f)) onClaimsChange(f);
            }}
            className="hidden"
          />
          <div className="text-center">
            <svg
              className="mx-auto h-12 w-12 text-amber-400 mb-3"
              fill="none"
              stroke="currentColor"
              viewBox="0 0 24 24"
            >
              <path
                strokeLinecap="round"
                strokeLinejoin="round"
                strokeWidth={2}
                d="M9 17v-2m3 2v-4m3 4v-6m2 10H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z"
              />
            </svg>
            {claimsFile ? (
              <div>
                <p className="text-lg font-semibold text-amber-600 mb-2">
                  ✓ {claimsFile.name}
                </p>
                {claimsStatus === "parsing" && (
                  <p className="text-xs text-gray-500 dark:text-gray-400">
                    Reading the claims library...
                  </p>
                )}
                {claimsStatus === "ready" && claimsSummary && (
                  <p className="text-xs text-green-700 dark:text-green-400">
                    {claimsSummary}
                  </p>
                )}
                {claimsStatus === "error" && (
                  <p className="text-xs text-red-600 dark:text-red-400 whitespace-pre-line">
                    {claimsError ||
                      "Could not read this file — the review will try again."}
                  </p>
                )}
                <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">
                  Click to change or drop new file
                </p>
              </div>
            ) : (
              <div>
                <p className="text-lg font-semibold text-gray-700 dark:text-gray-300 mb-2">
                  Optional claims library
                </p>
                <p className="text-xs text-gray-500 dark:text-gray-400">
                  Human-approved claims to match the content against first
                </p>
                <p className="text-xs text-gray-500 dark:text-gray-400">
                  Supported formats: XLSX, XLSM, CSV
                </p>
                <p className="text-xs text-gray-500 dark:text-gray-400 mt-2">
                  Needs a claim text column —{" "}
                  <span className="text-amber-600 dark:text-amber-400">
                    see ? above
                  </span>
                </p>
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
