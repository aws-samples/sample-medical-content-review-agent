# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Merges repeated findings and writes the final review report in code.

The editor only decides which findings to drop and which to merge, by id; the report
itself is written here, so a long asset cannot run the editor out of output tokens and
the published fields are the ones the reviewers produced.
"""

import json

from strands import tool
from tools.run_record import note_issue
from utils.s3 import (
    STAGING_BUCKET,
    presign,
    put_bytes,
    put_json,
    read_json,
    require_bucket,
    safe_session_id,
)

from reviewers._common import REVIEWS_PREFIX, claims_report_key
from reviewers.claim_tags import normalise
from reviewers.readiness_report import build_workbook

REPORT_NAME = "review_results.json"
AGGREGATE_NAME = "all_findings.json"
WORKBOOK_NAME = "readiness_report.xlsx"
XLSX_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
MANDATORY_SCORE = 70
# A finding raised by two reviewers is mandatory from this score
CORROBORATED_SCORE = 40
# Internal-only fields that never reach the published report
PRIVATE_FIELDS = ("id", "reviewer")


def check_kind(finding: dict) -> str:
    """Name the kind of check that raised a finding

    Parameters
    ----------
    finding : dict
        An aggregated finding, with its `reviewer` tag

    Returns
    -------
    str
        `library_deviation`, `library_status` or `library_audience` for the claims
        library's own findings, `editorial` for the generic reviewer's, and
        `substantiation` for the internal and external reviewers'
    """
    if finding.get("reviewer") == "claims":
        if finding.get("library_finding"):
            return f"library_{finding['library_finding']}"
        return "library"
    if finding.get("reviewer") == "generic":
        return "editorial"
    return "substantiation"


def _score(finding: dict) -> int:
    score = finding.get("score")
    return score if isinstance(score, int) else 0


def _pages(finding: dict) -> list[int]:
    pages = finding.get("pages") or [finding.get("page")]
    return [p for p in pages if isinstance(p, int)]


def _join(values: list[object]) -> str:
    parts: list[str] = []
    for value in values:
        for part in str(value or "").split(" | "):
            if part and part not in parts:
                parts.append(part)
    return " | ".join(parts)


def _combine(group: list[dict], join_sources: bool = False) -> dict:
    """One finding standing for a group: the most severe member, on every page"""
    base = max(group, key=lambda f: (f.get("type") == "mandatory", _score(f)))
    merged = {**base}
    pages = sorted({p for f in group for p in _pages(f)})
    if pages:
        merged["page"] = pages[0]
        merged["pages"] = pages
    if join_sources:
        merged["reference"] = _join([f.get("reference") for f in group])
        merged["source"] = _join([f.get("source") for f in group])
        reviewers = {f.get("reviewer") for f in group}
        if len(reviewers) > 1 and _score(merged) >= CORROBORATED_SCORE:
            merged["type"] = "mandatory"
    return merged


def merge_repeats(findings: list[dict]) -> list[dict]:
    """Report each problem once: merge same-kind findings on the same quote

    Copy repeated on several pages is flagged once per page, and each repeat is the same
    problem. Different kinds of check on the same quote stay separate findings, since
    they report different problems.

    Parameters
    ----------
    findings : list[dict]
        Aggregated findings, tagged with their claim match

    Returns
    -------
    list[dict]
        The findings, with each group of repeats replaced by one finding whose `pages`
        lists every page it occurs on, in the position of its first member
    """
    groups: dict[tuple, list[dict]] = {}
    order: list[tuple] = []
    for index, finding in enumerate(findings):
        quote = normalise(finding.get("quote"))
        key = (
            (check_kind(finding), quote, finding.get("claim_id") or "")
            if quote
            else ("single", index)
        )
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(finding)
    return [_combine(groups[key]) for key in order]


def _apply_decisions(
    findings: list[dict], drop: list[str], merge: list[list[str]]
) -> tuple[list[dict], dict]:
    """Apply the editor's drop and merge decisions, within the report's hard rules"""
    by_id = {f["id"]: f for f in findings if f.get("id")}
    ignored: list[dict] = []
    replaced: dict[str, dict] = {}
    removed: set[str] = set()

    for group in merge:
        ids = list(dict.fromkeys(str(i) for i in group or []))
        members = [by_id.get(i) for i in ids]
        reason = ""
        if len(ids) < 2 or any(m is None for m in members):
            reason = "unknown id or fewer than two findings"
        elif any(i in removed or i in replaced for i in ids):
            reason = "a finding is already in another merge"
        elif any(m.get("reviewer") == "claims" for m in members):
            reason = "claims-library findings are never merged"
        elif len({check_kind(m) for m in members}) > 1:
            reason = "different kinds of check report different problems"
        elif not set.intersection(*(set(_pages(m)) for m in members)):
            reason = "the findings share no page"
        if reason:
            ignored.append({"merge": ids, "reason": reason})
            continue
        replaced[ids[0]] = _combine(members, join_sources=True)
        removed.update(ids[1:])

    dropped: list[str] = []
    for raw in drop:
        finding_id = str(raw)
        finding = by_id.get(finding_id)
        reason = ""
        if finding is None:
            reason = "unknown id"
        elif finding.get("reviewer") == "claims":
            reason = "claims-library findings are never dropped"
        elif finding.get("type") == "mandatory" or _score(finding) >= MANDATORY_SCORE:
            reason = "mandatory findings are never dropped"
        elif finding_id in replaced:
            reason = "the finding is merged"
        if reason:
            ignored.append({"drop": finding_id, "reason": reason})
            continue
        removed.add(finding_id)
        dropped.append(finding_id)

    published = []
    for finding in findings:
        finding_id = finding.get("id")
        if finding_id in removed:
            continue
        published.append(replaced.get(finding_id, finding))
    return published, {
        "dropped": len(dropped),
        "merged": len(replaced),
        "ignored": ignored,
    }


def _published_form(finding: dict, quote_group: str) -> dict:
    out = {k: v for k, v in finding.items() if k not in PRIVATE_FIELDS}
    out["check"] = check_kind(finding)
    out["quote_group"] = quote_group
    return out


def quote_groups(findings: list[dict]) -> list[str]:
    """Key the findings on the same quote alike, so the UI can show them on one card

    Parameters
    ----------
    findings : list[dict]
        Findings in report order, each with its `id`

    Returns
    -------
    list[str]
        For each finding, the id of the first finding on the same normalised quote; a
        finding without a quote is a group of its own
    """
    first: dict[str, str] = {}
    groups = []
    for finding in findings:
        quote = normalise(finding.get("quote"))
        own = str(finding.get("id") or "")
        groups.append(first.setdefault(quote, own) if quote else own)
    return groups


def _publish_workbook(session_id: str, prefix: str, report: list[dict]) -> str:
    """Write the readiness report XLSX next to the review report; return its URL tag"""
    claims_report = read_json(claims_report_key(session_id))
    key = prefix + WORKBOOK_NAME
    put_bytes(key, build_workbook(claims_report, report, session_id), XLSX_TYPE)
    url = presign(
        STAGING_BUCKET,
        key,
        ResponseContentDisposition=f'attachment; filename="{WORKBOOK_NAME}"',
    )
    return f"\n\n[REPORT_XLSX_URL:{url}]"


@tool
def publish_review(
    session_id: str,
    drop: list[str] | None = None,
    merge: list[list[str]] | None = None,
) -> str:
    """Publish the final review report from the aggregated findings and the editor's
    decisions.

    The editor names only the ids (`"F12"`) of the findings to drop and the groups of
    ids to merge; every finding it does not name is published as it is. Decisions that
    break the report's hard rules are ignored and listed in the reply: claims-library
    findings are never dropped or merged, mandatory findings are never dropped, and
    merged findings must be the same kind of check and share a page.

    Parameters
    ----------
    session_id : str
        The same `session_id` that was passed to `get_reviews`.
    drop : list[str] | None
        Ids of low-severity findings that are trivially wrong or duplicate another one.
    merge : list[list[str]] | None
        Groups of ids that report the same specific issue; each group becomes one
        finding, keeping its most detailed member and joining the references.

    Returns
    -------
    str
        A JSON summary (findings published, dropped, merged, and ignored decisions),
        followed by a `[REVIEW_URL:...]` tag holding a pre-signed URL of the report and
        a `[REPORT_XLSX_URL:...]` tag for the readiness workbook.
    """
    require_bucket()
    prefix = f"{REVIEWS_PREFIX}/{safe_session_id(session_id)}/"
    aggregate = read_json(prefix + AGGREGATE_NAME)
    if not isinstance(aggregate, dict):
        raise RuntimeError(f"No aggregated findings at {prefix}{AGGREGATE_NAME}")
    findings = [f for f in aggregate.get("findings", []) if isinstance(f, dict)]

    published, decisions = _apply_decisions(findings, drop or [], merge or [])
    published.sort(
        key=lambda f: (0, f["page"]) if isinstance(f.get("page"), int) else (1, 0)
    )
    report = [
        _published_form(f, group)
        for f, group in zip(published, quote_groups(published))
    ]

    report_key = prefix + REPORT_NAME
    result = json.dumps(
        {
            "review_results_s3_uri": put_json(report_key, report),
            "aggregated": len(findings),
            "published": len(report),
            **decisions,
        }
    )
    try:
        result += f"\n\n[REVIEW_URL:{presign(STAGING_BUCKET, report_key)}]"
    except Exception as e:  # noqa: BLE001 - the report itself is already persisted
        print(f"[REPORT] Failed to pre-sign the review report URL: {e}")

    try:
        result += _publish_workbook(session_id, prefix, report)
    except Exception as e:  # noqa: BLE001 - the JSON report is already persisted
        note_issue(session_id, "report", f"the readiness report XLSX failed: {e}")
    return result
