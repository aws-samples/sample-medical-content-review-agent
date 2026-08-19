// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

interface UploadResponse {
  uploadUrl: string;
  s3Uri: string;
  key: string;
}

// What POST /claims/parse returns for an uploaded claims spreadsheet. The rows come
// back inline so the preview can be shown immediately, without a second round trip.
export interface ParsedClaimsLibrary {
  claimsS3Uri: string;
  totalClaims: number;
  byStatus: Record<string, number>;
  columns: string[];
  // canonical field -> the spreadsheet's own header, so the mapping is auditable
  columnMapping: Record<string, string>;
  unmappedColumns: string[];
  headerRow: number;
  claims: Record<string, unknown>[];
}

async function apiBaseUrl(): Promise<string> {
  const config = await fetch("/aws-exports.json").then((r) => r.json());
  const apiUrl = config.feedbackApiUrl?.replace(/\/+$/, "");
  if (!apiUrl) throw new Error("API URL not configured");
  return `${apiUrl}/`;
}

// Remember the original filename for each uploaded S3 object so the UI can
// display user-friendly names (the backend renames to UUIDs on upload)
const uploadedNames = new Map<string, string>();

export function registerOriginalName(
  s3Uri: string,
  originalName: string,
): void {
  uploadedNames.set(s3Uri, originalName);
  const key = s3Uri.split("/").pop();
  if (key) uploadedNames.set(key, originalName);
}

export function getOriginalName(s3UriOrKey: string): string | null {
  if (uploadedNames.has(s3UriOrKey)) return uploadedNames.get(s3UriOrKey)!;
  const base = s3UriOrKey.split("/").pop();
  if (base && uploadedNames.has(base)) return uploadedNames.get(base)!;
  return null;
}

/**
 * Upload a file to S3 via pre-signed URL from the backend API.
 */
export async function uploadFileToS3(
  file: File,
  idToken: string,
): Promise<string> {
  const apiUrl = await apiBaseUrl();

  // Get pre-signed upload URL
  const res = await fetch(`${apiUrl}upload`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${idToken}`,
    },
    body: JSON.stringify({
      filename: file.name,
      content_type: file.type || "application/octet-stream",
    }),
  });

  if (!res.ok) throw new Error(`Failed to get upload URL: ${res.status}`);
  const { uploadUrl, s3Uri }: UploadResponse = await res.json();

  // Upload file directly to S3
  const putRes = await fetch(uploadUrl, {
    method: "PUT",
    headers: { "Content-Type": file.type || "application/octet-stream" },
    body: file,
  });

  if (!putRes.ok) throw new Error(`Failed to upload file: ${putRes.status}`);
  registerOriginalName(s3Uri, file.name);
  return s3Uri;
}

/**
 * Parse an already-uploaded claims spreadsheet so it can be previewed straight away.
 *
 * This is the same parser the agent runs during the review, exposed as an endpoint
 * purely so the user does not have to start a review to see what was read. A failure
 * here is not fatal: the agent parses the file again when the review starts.
 */
export async function parseClaimsFile(
  s3Uri: string,
  filename: string,
  idToken: string,
): Promise<ParsedClaimsLibrary> {
  const apiUrl = await apiBaseUrl();
  const res = await fetch(`${apiUrl}claims/parse`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${idToken}`,
    },
    body: JSON.stringify({ s3Uri, filename }),
  });

  if (!res.ok) {
    // The backend explains what it read and what it needs — surface that verbatim
    // rather than a status code the user cannot act on
    let detail = `HTTP ${res.status}`;
    try {
      const body = await res.json();
      if (typeof body?.message === "string") detail = body.message;
    } catch {
      /* non-JSON error body */
    }
    throw new Error(detail);
  }

  return (await res.json()) as ParsedClaimsLibrary;
}
