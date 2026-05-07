// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
"use client";

interface S3UploadCardsProps {
  contentPdfInput: string;
  onContentPdfInputChange: (value: string) => void;
  referenceInput: string;
  onReferenceInputChange: (value: string) => void;
  claimsInput: string;
  onClaimsInputChange: (value: string) => void;
}

export function S3UploadCards({
  contentPdfInput,
  onContentPdfInputChange,
  referenceInput,
  onReferenceInputChange,
  claimsInput,
  onClaimsInputChange,
}: S3UploadCardsProps) {
  return (
    <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
      {/* Content to Review */}
      <div className="bg-card rounded-2xl shadow-lg border border-border overflow-hidden">
        <div className="bg-gradient-to-r from-indigo-50 to-blue-50 dark:from-indigo-950/40 dark:to-blue-950/40 px-5 py-3 border-b border-border">
          <h2 className="text-sm font-semibold text-foreground flex items-center gap-2">
            <svg
              className="w-4 h-4 text-indigo-600 dark:text-indigo-400"
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
            Content to Review
          </h2>
        </div>
        <div className="p-4">
          <input
            type="text"
            placeholder="s3://bucket/path/to/content.pdf"
            value={contentPdfInput}
            onChange={(e) => onContentPdfInputChange(e.target.value)}
            className="w-full px-3 py-2 border-2 border-dashed border-indigo-200 dark:border-indigo-800 rounded-xl text-xs font-mono placeholder:text-muted-foreground bg-background text-foreground focus:outline-none focus:border-indigo-400 dark:focus:border-indigo-600 transition-colors"
          />
          <p className="text-[10px] text-muted-foreground mt-2">
            The medical content PDF to review
          </p>
        </div>
      </div>

      {/* Reference Materials */}
      <div className="bg-card rounded-2xl shadow-lg border border-border overflow-hidden">
        <div className="bg-gradient-to-r from-green-50 to-emerald-50 dark:from-green-950/40 dark:to-emerald-950/40 px-5 py-3 border-b border-border">
          <h2 className="text-sm font-semibold text-foreground flex items-center gap-2">
            <svg
              className="w-4 h-4 text-green-600 dark:text-green-400"
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
        </div>
        <div className="p-4">
          <textarea
            placeholder={
              "s3://bucket/path/to/reference1.pdf\ns3://bucket/path/to/reference2.pdf"
            }
            value={referenceInput}
            onChange={(e) => onReferenceInputChange(e.target.value)}
            rows={2}
            className="w-full px-3 py-2 border-2 border-dashed border-green-200 dark:border-green-800 rounded-xl text-xs font-mono placeholder:text-muted-foreground bg-background text-foreground focus:outline-none focus:border-green-400 dark:focus:border-green-600 transition-colors"
          />
          <p className="text-[10px] text-muted-foreground mt-2">
            Source documents to verify claims against
          </p>
        </div>
      </div>

      {/* Approved Claims */}
      <div className="bg-card rounded-2xl shadow-lg border border-border overflow-hidden">
        <div className="bg-gradient-to-r from-amber-50 to-yellow-50 dark:from-amber-950/40 dark:to-yellow-950/40 px-5 py-3 border-b border-border">
          <h2 className="text-sm font-semibold text-foreground flex items-center gap-2">
            <svg
              className="w-4 h-4 text-amber-600 dark:text-amber-400"
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
            Approved Claims
          </h2>
        </div>
        <div className="p-4">
          <textarea
            placeholder={
              "s3://bucket/path/to/approved-claims.pdf\ns3://bucket/path/to/claims.csv"
            }
            value={claimsInput}
            onChange={(e) => onClaimsInputChange(e.target.value)}
            rows={2}
            className="w-full px-3 py-2 border-2 border-dashed border-amber-200 dark:border-amber-800 rounded-xl text-xs font-mono placeholder:text-muted-foreground bg-background text-foreground focus:outline-none focus:border-amber-400 dark:focus:border-amber-600 transition-colors"
          />
          <p className="text-[10px] text-muted-foreground mt-2">
            Pre-approved claims to check adherence against
          </p>
        </div>
      </div>
    </div>
  );
}
