// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

interface UploadResponse {
  uploadUrl: string;
  s3Uri: string;
  key: string;
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
    body: JSON.stringify({ filename: file.name, content_type: file.type || "application/octet-stream" }),
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
  return s3Uri;
}
