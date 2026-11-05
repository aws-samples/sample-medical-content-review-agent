# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Builds the readiness report: one row per claim with its outcome, plus the findings.

Written in code from the claims report and the published findings, as an XLSX workbook
with a summary sheet, a claims sheet and a findings sheet.
"""

import io
from collections import Counter
from datetime import UTC, datetime

from openpyxl import Workbook
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from reviewers.claim_tags import best_claim, normalise

# the limit Excel puts on the characters in one cell
MAX_CELL_CHARS = 32767

CHECK_LABELS = {
    "library_deviation": "Library deviation",
    "library_status": "Library status",
    "library_audience": "Other audience",
    "library": "Claims library",
    "editorial": "Editorial",
    "substantiation": "Substantiation",
}
MATCH_LABELS = {"exact": "Exact", "partial": "Partial", "none": "No match"}

APPROVED = "Approved wording"
USE_APPROVED = "Use approved wording"
NOT_USABLE = "Library claim not usable"
OTHER_AUDIENCE = "Approved for another audience"
NEW_CLAIM = "New claim"
OUTCOMES = (APPROVED, USE_APPROVED, NOT_USABLE, OTHER_AUDIENCE, NEW_CLAIM)
OUTCOME_FILLS = {
    APPROVED: "C6EFCE",
    USE_APPROVED: "FFEB9C",
    NOT_USABLE: "FFC7CE",
    OTHER_AUDIENCE: "FFC7CE",
    NEW_CLAIM: "DDEBF7",
}

HEADER_FILL = PatternFill(start_color="D9E1F2", end_color="D9E1F2", fill_type="solid")
WRAP = Alignment(wrap_text=True, vertical="top", horizontal="left")

# the (header, width) of each column
CLAIM_COLUMNS = (
    ("Page", 7),
    ("Claim", 50),
    ("Claim type", 14),
    ("Outcome", 22),
    ("Recommendation", 50),
    ("Library match", 11),
    ("Exact match kind", 11),
    ("Library claim ID", 15),
    ("Approved library wording", 45),
    ("Library status", 12),
    ("Usable for this asset", 10),
    ("Why not usable", 30),
    ("Similarity", 10),
    ("Change from the approved wording", 45),
    ("Numeric changes", 25),
    ("Findings", 25),
)
FINDING_COLUMNS = (
    ("Pages", 9),
    ("Quote", 45),
    ("Check", 16),
    ("Type", 11),
    ("Score", 7),
    ("Headline", 35),
    ("Issue", 55),
    ("Fix", 40),
    ("Reference", 45),
    ("Source", 25),
    ("Claim match", 11),
    ("Library claim ID", 15),
)


def claim_outcome(claim: dict) -> tuple[str, str]:
    """Name a claim's outcome and recommend what to do with it

    Parameters
    ----------
    claim : dict
        A claim record from the claims report

    Returns
    -------
    tuple[str, str]
        The outcome, one of `OUTCOMES`, and the recommendation
    """
    status = claim.get("match_status")
    claim_id = claim.get("matched_claim_id") or ""
    usable = claim.get("library_claim_usable") is not False
    reason = claim.get("unusable_reason") or ""

    if status in ("exact", "partial") and claim_id and not usable:
        # an expired claim for another audience carries its status as the reason
        if claim.get("audience_approved") is False and not reason.startswith(
            "the approved claim"
        ):
            return OTHER_AUDIENCE, (
                f"{claim_id} is not usable here: {reason}. Use a claim approved for"
                " this asset's audience, or route this one to medical/regulatory review"
                " for this audience."
            )
        return NOT_USABLE, (
            f"{claim_id} is not usable: {reason}. Remove the claim or route it to"
            " medical/regulatory review for re-approval."
        )
    if status == "exact" and claim_id:
        return APPROVED, f"Approved wording of {claim_id}."
    if status == "partial" and claim_id:
        approved = claim.get("matched_claim_text", "")
        return USE_APPROVED, f'Use the approved wording of {claim_id}: "{approved}"'
    return NEW_CLAIM, (
        "Not in the claims library. Substantiate it against the references and the"
        " external evidence, then route it to the new-claim process."
    )


def link_findings(claims: list[dict], findings: list[dict]) -> list[list[dict]]:
    """Group the findings by the claim they quote

    Parameters
    ----------
    claims : list[dict]
        Claim records from the claims report
    findings : list[dict]
        Published findings

    Returns
    -------
    list[list[dict]]
        For each claim, in order, the findings about it
    """
    pairs = [(claim, normalise(claim.get("text"))) for claim in claims]
    index = {id(claim): i for i, claim in enumerate(claims)}
    linked: list[list[dict]] = [[] for _ in claims]
    for finding in findings:
        claim = best_claim(finding, pairs)
        if claim is not None:
            linked[index[id(claim)]].append(finding)
    return linked


def _findings_summary(findings: list[dict]) -> str:
    """Summarize a claim's findings for its row of the claims sheet

    Parameters
    ----------
    findings : list[dict]
        The published findings about one claim

    Returns
    -------
    str
        The mandatory and optional counts and the kinds of check, or "" for none
    """
    if not findings:
        return ""
    mandatory = sum(1 for f in findings if f.get("type") == "mandatory")
    checks = sorted({CHECK_LABELS.get(f.get("check", ""), "Other") for f in findings})
    return f"{mandatory} mandatory, {len(findings) - mandatory} optional: {', '.join(checks)}"


def claim_rows(claims: list[dict], findings: list[dict]) -> list[list[object]]:
    """One row per claim, in the order of `CLAIM_COLUMNS`

    Parameters
    ----------
    claims : list[dict]
        Claim records from the claims report
    findings : list[dict]
        Published findings

    Returns
    -------
    list[list[object]]
        The claims sheet rows, without the header
    """
    rows = []
    for claim, about in zip(claims, link_findings(claims, findings)):
        outcome, recommendation = claim_outcome(claim)
        usable = claim.get("library_claim_usable")
        rows.append(
            [
                claim.get("page"),
                claim.get("text", ""),
                claim.get("claim_type", ""),
                outcome,
                recommendation,
                MATCH_LABELS.get(claim.get("match_status", ""), ""),
                claim.get("match_precision", ""),
                claim.get("matched_claim_id", ""),
                claim.get("matched_claim_text", ""),
                claim.get("library_status", ""),
                "" if usable is None else ("Yes" if usable else "No"),
                claim.get("unusable_reason", ""),
                claim.get("similarity"),
                claim.get("deviation", "")
                if claim.get("match_status") == "partial"
                else "",
                "; ".join(claim.get("numeric_changes") or []),
                _findings_summary(about),
            ]
        )
    return rows


def finding_rows(findings: list[dict]) -> list[list[object]]:
    """One row per published finding, in the order of `FINDING_COLUMNS`

    Parameters
    ----------
    findings : list[dict]
        Published findings, each with its `check`

    Returns
    -------
    list[list[object]]
        The findings sheet rows, without the header
    """
    rows = []
    for f in findings:
        pages = f.get("pages") or [f.get("page")]
        rows.append(
            [
                ", ".join(str(p) for p in pages if p is not None),
                f.get("quote", ""),
                CHECK_LABELS.get(f.get("check", ""), f.get("check", "")),
                f.get("type", ""),
                f.get("score"),
                f.get("headline", ""),
                f.get("issue", ""),
                f.get("fix", ""),
                f.get("reference", ""),
                f.get("source", ""),
                MATCH_LABELS.get(f.get("claim_match", ""), ""),
                f.get("claim_id", ""),
            ]
        )
    return rows


def _cell_value(value: object) -> object:
    """A value made safe for a cell: no illegal characters, within Excel's cell limit

    Parameters
    ----------
    value : object
        The value to write

    Returns
    -------
    object
        The value, with a string stripped of illegal characters and cut to length
    """
    if not isinstance(value, str):
        return value
    return ILLEGAL_CHARACTERS_RE.sub("", value)[:MAX_CELL_CHARS]


def _write_table(ws, columns: tuple, rows: list[list[object]]) -> None:
    """Write a header and rows to a sheet, styled, filtered and frozen

    Parameters
    ----------
    ws : Worksheet
        The sheet to write to
    columns : tuple
        The (header, width) of each column
    rows : list[list[object]]
        The rows, in the order of `columns`
    """
    ws.append([header for header, _ in columns])
    for cell in ws[1]:
        cell.font = Font(bold=True)
        cell.fill = HEADER_FILL
        cell.alignment = WRAP
    for row in rows:
        ws.append([_cell_value(v) for v in row])
    for row in ws.iter_rows(min_row=2):
        for cell in row:
            cell.alignment = WRAP
            # content copied from the asset is text, never a formula
            if isinstance(cell.value, str) and cell.value.startswith("="):
                cell.data_type = "s"
    for i, (_, width) in enumerate(columns, start=1):
        ws.column_dimensions[get_column_letter(i)].width = width
    ws.freeze_panes = "C2"
    ws.auto_filter.ref = ws.dimensions


def _summary_rows(
    claims_report: dict, findings: list[dict], outcomes: list[str], session_id: str
) -> list[tuple[str, object]]:
    """The label and value rows of the summary sheet

    Parameters
    ----------
    claims_report : dict
        The session's claims report, {} when there is none
    findings : list[dict]
        Published findings, each with its `check`
    outcomes : list[str]
        The outcome of each claim
    session_id : str
        Session the review ran under

    Returns
    -------
    list[tuple[str, object]]
        The summary sheet's (label, value) rows
    """
    audience = claims_report.get("asset_audience") or {}
    counts = claims_report.get("counts") or {}
    by_outcome = Counter(outcomes)
    by_check = Counter(CHECK_LABELS.get(f.get("check", ""), "Other") for f in findings)
    mandatory = sum(1 for f in findings if f.get("type") == "mandatory")
    return [
        ("Readiness report", ""),
        (
            (
                "Advisory pre-MLR check. It flags what a reviewer should look at; it"
                " never approves, rejects or edits content."
            ),
            "",
        ),
        ("", ""),
        ("Session", session_id),
        ("Generated (UTC)", datetime.now(UTC).strftime("%Y-%m-%d %H:%M")),
        ("Asset audience", audience.get("audience") or "not decided"),
        ("Why", audience.get("reason") or ""),
        ("", ""),
        ("Claims", claims_report.get("total_claims", len(outcomes))),
        ("Exact library match", counts.get("exact", 0)),
        ("Partial library match", counts.get("partial", 0)),
        ("No library match", counts.get("none", 0)),
        ("", ""),
        ("Outcome", "Claims"),
        *((outcome, by_outcome.get(outcome, 0)) for outcome in OUTCOMES),
        ("", ""),
        ("Findings", len(findings)),
        ("Mandatory", mandatory),
        ("Optional", len(findings) - mandatory),
        *((label, n) for label, n in sorted(by_check.items())),
    ]


def build_workbook(
    claims_report: dict | None, findings: list[dict], session_id: str
) -> bytes:
    """Write the readiness report as an XLSX workbook

    Parameters
    ----------
    claims_report : dict | None
        The session's claims report; without one, the claims sheet is empty
    findings : list[dict]
        Published findings, each with its `check`
    session_id : str
        Session the review ran under, named on the summary sheet

    Returns
    -------
    bytes
        The workbook
    """
    claims_report = claims_report or {}
    claims = [c for c in claims_report.get("claims") or [] if isinstance(c, dict)]
    rows = claim_rows(claims, findings)

    wb = Workbook()
    summary = wb.active
    summary.title = "Summary"
    for label, value in _summary_rows(
        claims_report, findings, [r[3] for r in rows], session_id
    ):
        summary.append([_cell_value(label), _cell_value(value)])
    summary["A1"].font = Font(bold=True, size=14)
    for row in summary.iter_rows():
        if row[0].value in ("Claims", "Outcome", "Findings"):
            row[0].font = Font(bold=True)
        for cell in row:
            cell.alignment = WRAP
    summary.column_dimensions["A"].width = 40
    summary.column_dimensions["B"].width = 80

    ws = wb.create_sheet("Claims")
    _write_table(ws, CLAIM_COLUMNS, rows)
    outcome_col = [h for h, _ in CLAIM_COLUMNS].index("Outcome") + 1
    for row in ws.iter_rows(min_row=2, min_col=outcome_col, max_col=outcome_col):
        color = OUTCOME_FILLS.get(row[0].value)
        if color:
            row[0].fill = PatternFill(
                start_color=color, end_color=color, fill_type="solid"
            )

    _write_table(wb.create_sheet("Findings"), FINDING_COLUMNS, finding_rows(findings))

    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()
