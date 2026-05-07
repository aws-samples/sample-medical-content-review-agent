# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Aggregates per-batch reviewer JSONs, persists them, and returns a pointer."""

import json
import re

import boto3
from strands import tool

from reviewers._common import REVIEWS_PREFIX, STAGING_BUCKET

s3_client = boto3.client("s3")

REVIEWER_KIND_RE = re.compile(r"/(?P<kind>generic|external|internal)_[^/]+\.json$")
LOCAL_AGGREGATE_PATH = "/tmp/all_findings.json"  # noqa: S108  # nosec B108


@tool
def get_reviews(session_id: str) -> str:
    """Aggregate all per-batch reviewer JSONs, save them, and return a pointer.

    Reads every file under `s3://{STAGING_BUCKET}/reviews/{session_id}/`, tags each
    finding with the reviewer that produced it (`"generic" | "external" | "internal"`),
    sorts findings by page number, then persists the result in TWO places:

    1. `s3://{STAGING_BUCKET}/reviews/{session_id}/all_findings.json` — the durable
       record, used for auditing the editor's dedupe decisions after the fact.
    2. `/tmp/all_findings.json` — local to the AgentCore Runtime container, so the
       orchestrator can load the full aggregate with `file_read` instead of having
       the whole payload flow through its context window (where the model may
       implicitly summarise or drop items).

    Returns a short JSON pointer — the editor is expected to call
    `file_read("/tmp/all_findings.json")` next to see every finding verbatim.

    Parameters
    ----------
    session_id : str
        The same `session_id` that was passed to the reviewer tools.

    Returns
    -------
    str
        A JSON pointer of shape:
        `{"local_path": "/tmp/all_findings.json",
          "aggregate_s3_uri": "s3://...",
          "counts": {"generic": N, "external": N, "internal": N},
          "total": N,
          "instruction": "Call file_read('/tmp/all_findings.json') to load every
                          finding verbatim before editing."}`
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

    # Sort by page (missing/non-int page sinks to the end), preserving within-page
    # order for stability.
    def _sort_key(f: dict) -> tuple:
        page = f.get("page")
        return (0, page) if isinstance(page, int) else (1, 0)

    findings.sort(key=_sort_key)

    aggregate_key = f"{prefix}all_findings.json"
    body = json.dumps({"findings": findings, "counts": counts}, indent=2).encode(
        "utf-8"
    )
    s3_client.put_object(
        Bucket=STAGING_BUCKET,
        Key=aggregate_key,
        Body=body,
        ContentType="application/json",
    )
    with open(LOCAL_AGGREGATE_PATH, "wb") as f:
        f.write(body)

    pointer = {
        "local_path": LOCAL_AGGREGATE_PATH,
        "aggregate_s3_uri": f"s3://{STAGING_BUCKET}/{aggregate_key}",
        "counts": counts,
        "total": len(findings),
        "instruction": (
            f"Call file_read('{LOCAL_AGGREGATE_PATH}') next to load every finding"
            " verbatim before you start editing the final report."
        ),
    }
    return json.dumps(pointer)
