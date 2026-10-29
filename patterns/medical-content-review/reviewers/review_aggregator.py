# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Aggregates per-batch reviewer JSONs, persists them, and returns a pointer."""

import json
import re

from strands import tool
from utils.s3 import list_keys, put_json, read_json, require_bucket, safe_session_id

from reviewers._common import REVIEWS_PREFIX, claims_report_key
from reviewers.claim_tags import tag_findings
from reviewers.report import AGGREGATE_NAME, merge_repeats

REVIEWER_KIND_RE = re.compile(
    r"/(?P<kind>generic|external|internal|claims)_[^/]+\.json$"
)
REVIEWER_KINDS = ("generic", "external", "internal", "claims")


def load_claim_records(session_id: str) -> list[dict]:
    """The session's tagged claims, or [] when the run had no claims library"""
    report = read_json(claims_report_key(session_id))
    claims = report.get("claims") if isinstance(report, dict) else report
    if not isinstance(claims, list):
        return []
    return [claim for claim in claims if isinstance(claim, dict)]


@tool
def get_reviews(session_id: str) -> str:
    """Aggregate all per-batch reviewer JSONs, save them, and return a pointer.

    Reads every file under `s3://{STAGING_BUCKET}/reviews/{session_id}/`, tags each
    finding with the reviewer that produced it (`"generic" | "external" | "internal"
    | "claims"`), sorts findings by page number, merges same-kind repeats of a quote into
    one finding listing every page, gives each finding an id (`F1`, ...), and writes
    the result to `s3://{STAGING_BUCKET}/reviews/{session_id}/all_findings.json`, which
    the editor reads and `publish_review` publishes from.

    Parameters
    ----------
    session_id : str
        The same `session_id` that was passed to the reviewer tools.

    Returns
    -------
    str
        A JSON pointer of shape:
        `{"aggregate_s3_uri": "s3://...",
          "counts": {"generic": N, "external": N, "internal": N, "claims": N},
          "total": N, "repeats_merged": N}`
    """
    require_bucket()
    prefix = f"{REVIEWS_PREFIX}/{safe_session_id(session_id)}/"

    findings: list[dict] = []
    counts = dict.fromkeys(REVIEWER_KINDS, 0)
    for key in list_keys(prefix):
        match = REVIEWER_KIND_RE.search(key)
        data = read_json(key) if match else None
        if not isinstance(data, list):
            continue
        kind = match.group("kind")
        for item in data:
            if isinstance(item, dict):
                findings.append({**item, "reviewer": kind})
                counts[kind] += 1

    # Sort by page (missing/non-int page sinks to the end), preserving within-page
    # order for stability.
    def _sort_key(f: dict) -> tuple:
        page = f.get("page")
        return (0, page) if isinstance(page, int) else (1, 0)

    findings.sort(key=_sort_key)

    # Re-derive the claim match tag in Python: a reviewer may have left it out, and the
    # tag has to be on every finding, not only the ones the model remembered to mark.
    findings = tag_findings(findings, load_claim_records(session_id))
    reported = len(findings)
    findings = [
        {"id": f"F{index + 1}", **finding}
        for index, finding in enumerate(merge_repeats(findings))
    ]

    pointer = {
        "aggregate_s3_uri": put_json(
            f"{prefix}{AGGREGATE_NAME}", {"findings": findings, "counts": counts}
        ),
        "counts": counts,
        "total": len(findings),
        "repeats_merged": reported - len(findings),
    }
    return json.dumps(pointer)
