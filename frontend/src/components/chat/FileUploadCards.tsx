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

interface FileUploadCardsProps {
  documentFile: File | null;
  onDocumentChange: (file: File | null) => void;
  referenceFiles: File[];
  onReferenceFilesChange: (files: File[]) => void;
}

export function FileUploadCards({
  documentFile,
  onDocumentChange,
  referenceFiles,
  onReferenceFilesChange,
}: FileUploadCardsProps) {
  const documentInputRef = useRef<HTMLInputElement>(null);
  const referenceInputRef = useRef<HTMLInputElement>(null);

  const isAccepted = (file: File) =>
    ACCEPTED_TYPES.includes(file.type) ||
    ACCEPTED_EXTENSIONS.split(",").some((ext) =>
      file.name.toLowerCase().endsWith(ext),
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
    <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
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
    </div>
  );
}
