# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Aggregates the per-batch matched-claim files into one claims report."""

import json
import os

import boto3
from botocore.config import Config
from strands import tool

from reviewers._common import (
    CLAIMS_PREFIX,
    STAGING_BUCKET,
    safe_session_id,
    write_local_file,
)
from reviewers.claim_tags import claims_report_path

URL_EXPIRATION = 3600
MATCH_STATUSES = ("exact", "partial", "none")

s3_client = boto3.client(
    "s3",
    region_name=os.environ.get(
        "AWS_REGION", os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
    ),
    config=Config(s3={"addressing_style": "virtual"}),
)


@tool
def get_claims(session_id: str) -> str:
    """Merge every per-batch matched-claims file into a single claims report.

    Reads `s3://{STAGING_BUCKET}/claims/{session_id}/matched_*.json`, sorts the
    claims by page, and writes the report to S3, to
    `/tmp/claims_report_{session_id}.json`, and hands back a pre-signed URL so the
    frontend can display the claim-by-claim match status while the reviewers are still
    running.

    The report is the record of what the content claimed and how much of it the
    human-curated library already covered. It is an input to the review, never an
    update to the library.

    Parameters
    ----------
    session_id : str
        The same `session_id` that was passed to `match_claims`.

    Returns
    -------
    str
        A JSON pointer of shape `{"claims_report_s3_uri": "s3://...",
        "local_path": "/tmp/claims_report_{session_id}.json", "total_claims": N,
        "counts": {"exact": N, "partial": N, "none": N},
        "requires_verification": N}`, followed by a `[CLAIMS_URL:...]` tag holding a
        pre-signed URL of the report.
    """
    if not STAGING_BUCKET:
        raise RuntimeError("STAGING_BUCKET_NAME environment variable is not set")

    prefix = f"{CLAIMS_PREFIX}/{safe_session_id(session_id)}/"
    paginator = s3_client.get_paginator("list_objects_v2")

    claims: list[dict] = []
    for page in paginator.paginate(Bucket=STAGING_BUCKET, Prefix=prefix):
        for obj in page.get("Contents") or []:
            key = obj["Key"]
            if not key.rsplit("/", 1)[-1].startswith("matched_"):
                continue
            body = s3_client.get_object(Bucket=STAGING_BUCKET, Key=key)["Body"].read()
            try:
                data = json.loads(body)
            except json.JSONDecodeError:
                continue
            if isinstance(data, list):
                claims.extend(item for item in data if isinstance(item, dict))

    def _sort_key(claim: dict) -> tuple:
        page_no = claim.get("page")
        return (
            (0, page_no) if isinstance(page_no, int) else (1, 0),
            str(claim.get("claim_ref", "")),
        )

    claims.sort(key=_sort_key)

    counts = dict.fromkeys(MATCH_STATUSES, 0)
    for claim in claims:
        status = claim.get("match_status")
        if status in counts:
            counts[status] += 1

    report = {
        "total_claims": len(claims),
        "counts": counts,
        "requires_verification": sum(
            1 for claim in claims if claim.get("requires_verification")
        ),
        "claims": claims,
    }
    body = json.dumps(report, indent=2).encode("utf-8")

    report_key = f"{prefix}claims_report.json"
    s3_client.put_object(
        Bucket=STAGING_BUCKET,
        Key=report_key,
        Body=body,
        ContentType="application/json",
    )
    # Keyed by session, so a container serving a later review with no claims library
    # cannot pick this report up and tag that review against these claims
    local_path = claims_report_path(session_id)
    write_local_file(local_path, body)

    pointer = {
        "claims_report_s3_uri": f"s3://{STAGING_BUCKET}/{report_key}",
        "local_path": local_path,
        "total_claims": report["total_claims"],
        "counts": counts,
        "requires_verification": report["requires_verification"],
    }
    result = json.dumps(pointer)

    try:
        url = s3_client.generate_presigned_url(
            "get_object",
            Params={"Bucket": STAGING_BUCKET, "Key": report_key},
            ExpiresIn=URL_EXPIRATION,
        )
        result += f"\n\n[CLAIMS_URL:{url}]"
    except Exception as e:  # noqa: BLE001 - the report itself is already persisted
        print(f"[CLAIMS] Failed to pre-sign the claims report URL: {e}")

    return result
