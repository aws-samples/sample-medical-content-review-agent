# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Aggregates per-batch reviewer JSONs written under reviews/{session_id}/ into one payload."""

import json
import re

import boto3
from strands import tool

from reviewers._common import REVIEWS_PREFIX, STAGING_BUCKET

s3_client = boto3.client("s3")

REVIEWER_KIND_RE = re.compile(r"/(?P<kind>generic|external|internal)_[^/]+\.json$")


@tool
def get_reviews(session_id: str) -> str:
    """Load every per-batch review JSON for the current session and return them inline.

    Reads all files under `s3://{STAGING_BUCKET}/reviews/{session_id}/`, parses each
    one as a JSON array of findings, tags each finding with which reviewer produced
    it (`"reviewer": "generic" | "external" | "internal"`), and returns the
    concatenation as a JSON string. Use this once all per-batch reviewer calls
    have finished, before writing the final aggregated `review.json`.

    Parameters
    ----------
    session_id : str
        The same `session_id` that was passed to the reviewer tools.

    Returns
    -------
    str
        A JSON string of shape
        `{"findings": [...], "counts": {"generic": N, "external": N, "internal": N}}`.
    """
    if not STAGING_BUCKET:
        raise RuntimeError("STAGING_BUCKET_NAME environment variable is not set")

    safe_session = re.sub(r"[^a-zA-Z0-9_-]", "_", session_id)
    prefix = f"{REVIEWS_PREFIX}/{safe_session}/"

    paginator = s3_client.get_paginator("list_objects_v2")
    findings: list[dict] = []
    counts = {"generic": 0, "external": 0, "internal": 0}
    for page in paginator.paginate(Bucket=STAGING_BUCKET, Prefix=prefix):
        for obj in page.get("Contents") or []:
            key = obj["Key"]
            match = REVIEWER_KIND_RE.search(key)
            if not match:
                continue
            kind = match.group("kind")
            body = s3_client.get_object(Bucket=STAGING_BUCKET, Key=key)["Body"].read()
            try:
                data = json.loads(body)
            except json.JSONDecodeError:
                continue
            if not isinstance(data, list):
                continue
            for item in data:
                if isinstance(item, dict):
                    item = {**item, "reviewer": kind}
                    findings.append(item)
                    counts[kind] += 1

    return json.dumps({"findings": findings, "counts": counts})
