# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
import os
import threading
from pathlib import Path

import boto3
from botocore.config import Config
from strands.hooks import AfterToolCallEvent, HookProvider, HookRegistry

REVIEW_RESULTS_PATH = "/tmp/review_results.json"  # noqa: S108  # nosec B108
STAGING_BUCKET = os.environ.get("STAGING_BUCKET_NAME", "")
URL_EXPIRATION = 3600


class ReviewS3UploadHook(HookProvider):
    """Upload review_results.json to S3 after file_write calls."""

    def __init__(self):
        region = os.environ.get(
            "AWS_REGION", os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
        )
        self.s3_client = boto3.client(
            "s3", region_name=region, config=Config(s3={"addressing_style": "virtual"})
        )
        self._last_review_url: str | None = None
        self._lock = threading.Lock()

    def register_hooks(self, registry: HookRegistry) -> None:
        registry.add_callback(AfterToolCallEvent, self.upload_review_to_s3)

    def take_pending_urls(self) -> dict[str, str]:
        with self._lock:
            urls = {}
            if self._last_review_url:
                urls["review"] = self._last_review_url
                self._last_review_url = None
            return urls

    def upload_review_to_s3(self, event: AfterToolCallEvent) -> None:
        tool_name = event.tool_use.get("name", "")
        if tool_name != "file_write":
            return

        tool_input = event.tool_use.get("input", {})
        file_path = tool_input.get("path", "") or tool_input.get("file_path", "")
        if "review_results" not in file_path:
            return

        local = Path(REVIEW_RESULTS_PATH)
        if not local.exists() or not STAGING_BUCKET:
            return

        session_id = event.invocation_state.get("session_id", "default")
        try:
            s3_key = f"reviews/{session_id}/review_results.json"
            self.s3_client.put_object(
                Bucket=STAGING_BUCKET,
                Key=s3_key,
                Body=local.read_bytes(),
                ContentType="application/json",
            )
            url = self.s3_client.generate_presigned_url(
                "get_object",
                Params={"Bucket": STAGING_BUCKET, "Key": s3_key},
                ExpiresIn=URL_EXPIRATION,
            )
            with self._lock:
                self._last_review_url = url
        except Exception as e:
            print(f"[HOOK ERROR] Failed to upload review results: {e}")
