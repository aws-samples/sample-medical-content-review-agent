// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

interface UploadResponse {
  uploadUrl: string;
  s3Uri: string;
  key: string;
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
  const config = await fetch("/aws-exports.json").then((r) => r.json());
  const apiUrl = config.feedbackApiUrl?.replace(/\/+$/, "") + "/";
  if (!apiUrl) throw new Error("API URL not configured");

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
