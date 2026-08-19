# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
import json
import os
import threading
from pathlib import Path

import boto3
from botocore.config import Config
from reviewers.claim_tags import load_claim_records, tag_findings
from strands.hooks import AfterToolCallEvent, HookProvider, HookRegistry

REVIEW_RESULTS_PATH = "/tmp/review_results.json"  # noqa: S108  # nosec B108
STAGING_BUCKET = os.environ.get("STAGING_BUCKET_NAME", "")
URL_EXPIRATION = 3600

# Keys the final report may nest its findings under, when it is not a bare array
FINDINGS_KEYS = ("issues", "findings", "results")


def _tag_report(body: bytes, session_id: str) -> bytes:
    """Make sure every finding in the final report carries its claim match tag

    The editor rewrites the findings freely, so `claim_match` is re-derived here, on the
    last artefact before the UI and the download see it. The report is passed through
    untouched when it cannot be parsed or when the run had no claims library.

    Parameters
    ----------
    body : bytes
        Raw `review_results.json` as the orchestrator wrote it
    session_id : str
        Session the review ran under, which keys its claims report

    Returns
    -------
    bytes
        The same report with `claim_match` and `claim_id` set on every finding
    """
    claims = load_claim_records(session_id)
    if not claims:
        return body

    try:
        report = json.loads(body)
    except json.JSONDecodeError:
        return body

    if isinstance(report, list):
        tagged: list | dict = tag_findings(
            [f for f in report if isinstance(f, dict)], claims
        )
    elif isinstance(report, dict):
        key = next(
            (k for k in FINDINGS_KEYS if isinstance(report.get(k), list)),
            None,
        )
        if not key:
            return body
        tagged = {
            **report,
            key: tag_findings([f for f in report[key] if isinstance(f, dict)], claims),
        }
    else:
        return body

    return json.dumps(tagged, indent=2).encode("utf-8")


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
            body = _tag_report(local.read_bytes(), session_id)
            # Keep the local copy in step, so a re-publish does not undo the tags
            local.write_bytes(body)
            self.s3_client.put_object(
                Bucket=STAGING_BUCKET,
                Key=s3_key,
                Body=body,
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
