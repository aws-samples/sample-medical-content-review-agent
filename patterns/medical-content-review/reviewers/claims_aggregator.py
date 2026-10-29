# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Aggregates the per-batch matched-claim files into one claims report."""

import json

from strands import tool
from utils.s3 import (
    STAGING_BUCKET,
    list_keys,
    presign,
    put_json,
    read_json,
    require_bucket,
    safe_session_id,
)

from reviewers._common import CLAIMS_PREFIX, claims_report_key
from reviewers.claim_tags import CLAIM_MATCH_STATUSES


@tool
def get_claims(session_id: str) -> str:
    """Merge every per-batch matched-claims file into a single claims report.

    Reads `s3://{STAGING_BUCKET}/claims/{session_id}/matched_*.json`, sorts the
    claims by page, writes the report to S3, and hands back a pre-signed URL so the
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
        A JSON pointer of shape `{"claims_report_s3_uri": "s3://...", "total_claims": N,
        "counts": {"exact": N, "partial": N, "none": N},
        "requires_verification": N}`, followed by a `[CLAIMS_URL:...]` tag holding a
        pre-signed URL of the report.
    """
    require_bucket()
    prefix = f"{CLAIMS_PREFIX}/{safe_session_id(session_id)}/"
    claims: list[dict] = []
    for key in sorted(list_keys(prefix)):
        if key.rsplit("/", 1)[-1].startswith("matched_"):
            data = read_json(key)
            if isinstance(data, list):
                claims.extend(item for item in data if isinstance(item, dict))

    def _sort_key(claim: dict) -> tuple:
        page_no = claim.get("page")
        return (
            (0, page_no) if isinstance(page_no, int) else (1, 0),
            str(claim.get("claim_ref", "")),
        )

    claims.sort(key=_sort_key)

    counts = dict.fromkeys(CLAIM_MATCH_STATUSES, 0)
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
    report_key = claims_report_key(session_id)
    pointer = {
        "claims_report_s3_uri": put_json(report_key, report),
        "total_claims": report["total_claims"],
        "counts": counts,
        "requires_verification": report["requires_verification"],
    }
    result = json.dumps(pointer)

    try:
        result += f"\n\n[CLAIMS_URL:{presign(STAGING_BUCKET, report_key)}]"
    except Exception as e:  # noqa: BLE001 - the report itself is already persisted
        print(f"[CLAIMS] Failed to pre-sign the claims report URL: {e}")

    return result
